#!/usr/bin/env python3
"""Smoke: a first knowledge-base install resumes after a restart, is paced, and stops at shutdown (prb_2e7713d4).

Before this fix a KB with no install record took the full-install path at
every start. That path ignores the per-file digest tags already stored, so a
restart threw away everything the interrupted install had embedded. The
workbench KB (2,561 files) and the project_solet KB never finished between
deploys, re-indexed from file one at every start and kept a core busy.

This smoke drives the real auto-install path over a scratch KB with in-memory
stand-ins for the state and memory services. It checks:

* an install killed part way resumes: the next start embeds only the
  remainder, deletes the half-embedded file's remnants and writes the record;
* repeated kills never embed a finished file twice;
* duplicate chunk sets left by the old path are reduced to one copy without
  embedding the file again;
* a shutdown stops the pass between chunks with one log line and no record,
  and an embedding error raised while shutting down is not logged per KB;
* the duty setting parses, and an invalid value uses the default with a warning;
* the pause stops early at shutdown, and a paced install measured on a real
  clock keeps its thread under the configured share of one core;
* the plugin wires the orchestrator's shutdown event into the install.
* a record-free startup is unthrottled and smallest-first, later passes retain
  configured pacing and order, and the first small KB enables real search.

Project policy: no pytest. Exits 0 on success, 1 on the first failed run.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/resumable_kb_install_smoke.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

import default_knowledge_plugin.kb_lifecycle as kb  # noqa: E402
import default_knowledge_plugin.plugin as plugin_module  # noqa: E402
from default_knowledge_plugin.constants import PLUGIN_NAME, document_tag, kb_id_tag  # noqa: E402
from default_knowledge_plugin.kb_indexing import collect_files, plan_file_chunks, resolve_manifest  # noqa: E402
from default_knowledge_plugin.kb_install_control import (  # noqa: E402
    DEFAULT_KB_INSTALL_DUTY,
    KB_INSTALL_DUTY_ENV,
    InstallCancelledError,
    InstallControl,
    kb_install_duty,
    paced_install_control,
)
from default_knowledge_plugin.kb_search import collect_tiered_results, get_active_names  # noqa: E402
from default_knowledge_plugin.plugin import DefaultKnowledgePlugin  # noqa: E402

_passed = 0
_failed: list[str] = []

_KB = "alpha_kb"
_SECOND_KB = "beta_kb"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class ProcessKilled(BaseException):
    """Stands in for the process dying: no ``except Exception`` in the install path catches it."""


class FakeMemoryService:
    """Chunk memories in a dict; counts remembers and deletes, and can fail after a set number of remembers."""

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}
        self.remembered: list[str] = []
        self.deleted: list[str] = []
        self.fail_after: int | None = None
        self.failure: BaseException = ProcessKilled()
        self.work: Callable[[], None] = lambda: None
        self._next = 0

    def reset_counts(self) -> None:
        self.remembered = []
        self.deleted = []

    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        if self.fail_after is not None and len(self.remembered) >= self.fail_after:
            raise self.failure
        self.work()
        self._next += 1
        memory_id = f"mem-{self._next}"
        self.memories[memory_id] = {"id": memory_id, "content": content, "tags": list(tags), "status": "active"}
        self.remembered.append(memory_id)
        return {"memory_id": memory_id}

    def recall(self, query: str, top_k: int, tags: list[str], exclude_ids: list[str] | None = None, score_by_similarity: bool = False) -> dict[str, Any]:
        del query, score_by_similarity
        hits = [m for m in self.memories.values() if all(t in m["tags"] for t in tags) and m["id"] not in (exclude_ids or [])]
        return {"memories": hits[:top_k]}

    def get_memories_by_tag(self, tag: str, include_archived: bool = False) -> dict[str, Any]:
        hits = [dict(m) for m in self.memories.values() if tag in m["tags"] and (include_archived or m["status"] == "active")]
        return {"memories": hits, "count": len(hits)}

    def delete_memories_by_ids(self, ids: list[str]) -> dict[str, Any]:
        for memory_id in ids:
            if self.memories.pop(memory_id, None) is not None:
                self.deleted.append(memory_id)
        return {"deleted": len(ids)}

    def delete_memories_by_tag(self, tag: str) -> dict[str, Any]:
        return self.delete_memories_by_ids([mid for mid, m in self.memories.items() if tag in m["tags"]])

    def ids_for_file(self, name: str, relative: str) -> set[str]:
        doc, kb_tag = document_tag(relative), kb_id_tag(name)
        return {mid for mid, m in self.memories.items() if doc in m["tags"] and kb_tag in m["tags"]}

    def ids_for_kb(self, name: str) -> set[str]:
        return {mid for mid, m in self.memories.items() if kb_id_tag(name) in m["tags"]}


class _FakeTxn:
    def __init__(self, state: FakeStateService) -> None:
        self._state = state

    def delete_records(self, namespace: str, query: dict[str, Any]) -> None:
        assert namespace == PLUGIN_NAME
        self._state.records = [r for r in self._state.records if r["name"] != query["filters"]["name"]]

    def write_state(self, namespace: str, data: dict[str, Any]) -> None:
        assert namespace == PLUGIN_NAME
        self._state.records.append(dict(data["record"]))


class FakeStateService:
    """Install records in a list, matching the read/transaction calls kb_lifecycle makes."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []
        self._next = 0

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        assert namespace == PLUGIN_NAME
        filters = query.get("filters", {})
        return {"data": {"records": [dict(r) for r in self.records if all(r.get(k) == v for k, v in filters.items())]}}

    @contextmanager
    def transactional(self) -> Generator[_FakeTxn]:
        yield _FakeTxn(self)

    def generate_id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next}"

    def prepare_record(self, namespace: str, table: str, record: dict[str, Any]) -> dict[str, Any]:
        del namespace, table
        return dict(record)

    def record(self, name: str) -> dict[str, Any] | None:
        rows = [r for r in self.records if r["name"] == name]
        assert len(rows) <= 1, rows
        return rows[0] if rows else None


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno == level]


@contextmanager
def _captured_logs() -> Generator[_LogCapture]:
    handler = _LogCapture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def _article(title: str, sections: int) -> str:
    parts = [f"# {title}\n\nArticle Role: reference\n"]
    for index in range(sections):
        parts.append(f"## Section {index} of {title}\n\n{title} body {index}. " + "Detail sentence. " * 90 + "\n")
    return "\n".join(parts)


def _make_kb(root: Path, name: str, files: int) -> Path:
    kb_dir = root / name
    kb_dir.mkdir()
    for index in range(files):
        (kb_dir / f"file_{index:02d}.md").write_text(_article(f"{name} file {index}", 3 + index % 3), encoding="utf-8")
    return kb_dir


def _file_chunks(kb_dir: Path, name: str) -> list[tuple[str, int]]:
    """Each manifest file's relative path and chunk count, in install order."""
    manifest = resolve_manifest(kb_dir, name, None)
    plans = [plan_file_chunks(kb_dir, path, name, manifest, budget=None) for path in collect_files(kb_dir, manifest)]
    return [(plan.relative, len(plan.texts)) for plan in plans]


def _auto_install(root: Path, state: FakeStateService, memory: FakeMemoryService, control: InstallControl | None = None) -> None:
    memory.reset_counts()
    kb.auto_install_knowledge_bases(root, state, memory, None, budget=None, control=control)


def _killed_install(root: Path, state: FakeStateService, memory: FakeMemoryService, after: int) -> bool:
    memory.fail_after = after
    try:
        _auto_install(root, state, memory)
    except ProcessKilled:
        return True
    finally:
        memory.fail_after = None
    return False


def _record_matches_store(state: FakeStateService, memory: FakeMemoryService, name: str) -> bool:
    record = state.record(name)
    live = memory.ids_for_kb(name)
    return record is not None and set(record["memory_ids"]) == live and record["chunk_count"] == len(live)


def _files_done_before(chunks: list[tuple[str, int]], embedded: int) -> tuple[list[str], str]:
    """The files fully embedded by ``embedded`` remembers, and the one cut part way."""
    done: list[str] = []
    total = 0
    for relative, count in chunks:
        if total + count > embedded:
            return done, relative
        total += count
        done.append(relative)
    raise AssertionError("the kill point is past the last chunk")


def _step_kill_and_resume() -> None:
    print("[1] an install killed part way resumes with only the remainder")
    with tempfile.TemporaryDirectory(prefix="resumable_kb_smoke_") as tmp:
        root = Path(tmp)
        kb_dir = _make_kb(root, _KB, 8)
        chunks = _file_chunks(kb_dir, _KB)
        total = sum(count for _, count in chunks)
        state, memory = FakeStateService(), FakeMemoryService()
        kill_at = chunks[0][1] + chunks[1][1] + chunks[2][1] + 1
        _check(_killed_install(root, state, memory, kill_at), f"first start killed after {kill_at} of {total} chunks")
        _check(state.record(_KB) is None, "no install record after the kill: the KB is not valid yet")
        done, cut = _files_done_before(chunks, kill_at)
        done_ids = {mid for relative in done for mid in memory.ids_for_file(_KB, relative)}
        remnant = memory.ids_for_file(_KB, cut)
        _check(kb.kb_install_status(_KB, state, memory, root) == "missing", "the interrupted KB still reads 'missing'")

        _auto_install(root, state, memory)
        remainder = total - sum(count for relative, count in chunks if relative in done)
        _check(len(memory.remembered) == remainder, f"restart embedded only the remainder ({len(memory.remembered)} == {remainder} of {total})")
        _check(done_ids <= memory.ids_for_kb(_KB), "every finished file kept its chunk memories")
        _check(set(memory.deleted) == remnant, "the half-embedded file's remnants were deleted")
        _check(_record_matches_store(state, memory, _KB), "the install record is written and lists exactly the stored chunks")
        _check(len(memory.ids_for_kb(_KB)) == total, "the store holds one chunk per planned chunk")


def _step_repeated_kills() -> None:
    print("[2] repeated kills never embed a finished file twice")
    with tempfile.TemporaryDirectory(prefix="resumable_kb_smoke_") as tmp:
        root = Path(tmp)
        kb_dir = _make_kb(root, _KB, 8)
        chunks = _file_chunks(kb_dir, _KB)
        total = sum(count for _, count in chunks)
        state, memory = FakeStateService(), FakeMemoryService()
        embedded = 0
        for attempt in range(3):
            killed = _killed_install(root, state, memory, chunks[attempt][1] + 2)
            embedded += len(memory.remembered)
            _check(killed and state.record(_KB) is None, f"start {attempt + 1} killed with no record")
        _auto_install(root, state, memory)
        embedded += len(memory.remembered)
        rework = sum(count for _, count in chunks[:3])
        _check(embedded <= total + rework, f"chunks embedded over four starts ({embedded}) <= total ({total}) + the three cut files ({rework})")
        _check(_record_matches_store(state, memory, _KB) and len(memory.ids_for_kb(_KB)) == total, "the final store and record are exact")


def _step_legacy_duplicates() -> None:
    print("[3] duplicate chunk sets left by the old full-install path are reduced without re-embedding")
    with tempfile.TemporaryDirectory(prefix="resumable_kb_smoke_") as tmp:
        root = Path(tmp)
        kb_dir = _make_kb(root, _KB, 4)
        chunks = _file_chunks(kb_dir, _KB)
        state, memory = FakeStateService(), FakeMemoryService()
        manifest = resolve_manifest(kb_dir, _KB, None)
        first = plan_file_chunks(kb_dir, kb_dir / chunks[0][0], _KB, manifest, budget=None)
        for _ in range(2):
            for text in first.texts:
                memory.remember(content=text, tags=list(first.tags))
        duplicated = memory.ids_for_file(_KB, chunks[0][0])
        _auto_install(root, state, memory)
        kept = memory.ids_for_file(_KB, chunks[0][0])
        _check(len(memory.remembered) == sum(count for _, count in chunks[1:]), "the duplicated file was not embedded again")
        _check(kept < duplicated and len(kept) == len(first.texts), "one copy of each chunk was kept")
        _check(set(memory.deleted) == duplicated - kept, "the surplus copy was deleted")
        _check(_record_matches_store(state, memory, _KB), "the install record lists one copy")


def _counting_control(stop_after: int) -> tuple[InstallControl, list[int]]:
    calls = [0]

    def pace() -> float:
        calls[0] += 1
        return 0.0

    return InstallControl(pace=pace, should_stop=lambda: calls[0] >= stop_after), calls


def _step_shutdown_stops_between_chunks() -> None:
    print("[4] a shutdown stops the pass between chunks with one line and resumes later")
    with tempfile.TemporaryDirectory(prefix="resumable_kb_smoke_") as tmp:
        root = Path(tmp)
        alpha = _file_chunks(_make_kb(root, _KB, 3), _KB)
        beta = _file_chunks(_make_kb(root, _SECOND_KB, 6), _SECOND_KB)
        state, memory = FakeStateService(), FakeMemoryService()
        # Checkpoints run before each file and each chunk: file 0's checkpoint
        # and its chunks, then file 1's checkpoint and one chunk, then stop.
        # Alpha is smaller so it remains first under fresh-start ordering.
        stop_after = 1 + alpha[0][1] + 1 + 2
        control, _ = _counting_control(stop_after=stop_after)
        with _captured_logs() as logs:
            _auto_install(root, state, memory, control)
        stopped = [m for m in logs.messages(logging.INFO) if "auto-install stopped at shutdown" in m]
        _check(len(memory.remembered) == alpha[0][1] + 1, f"the pass stopped between chunks ({len(memory.remembered)} == file 0 + one chunk of file 1)")
        _check(len(stopped) == 1 and _KB in stopped[0], "exactly one 'stopped at shutdown' line, naming the KB")
        _check(logs.messages(logging.ERROR) == [], "no error lines at shutdown")
        _check(state.records == [] and memory.ids_for_kb(_SECOND_KB) == set(), "no record written and the next KB was not started")
        _auto_install(root, state, memory)
        expected = sum(count for _, count in alpha[1:]) + sum(count for _, count in beta)
        _check(len(memory.remembered) == expected, f"the next start embedded only what was missing ({len(memory.remembered)} == {expected})")
        _check(len(memory.deleted) == 1, "the one chunk of the cut file was deleted")
        _check(_record_matches_store(state, memory, _KB) and _record_matches_store(state, memory, _SECOND_KB), "both KBs then have exact records")


def _step_rejection_while_stopping() -> None:
    print("[5] an embedding rejected because the platform is stopping is not logged per KB")
    with tempfile.TemporaryDirectory(prefix="resumable_kb_smoke_") as tmp:
        root = Path(tmp)
        _make_kb(root, _KB, 3)
        _make_kb(root, _SECOND_KB, 3)
        state, memory = FakeStateService(), FakeMemoryService()
        stopping = [False]

        def rejected() -> None:
            stopping[0] = True
            raise RuntimeError("Core AI executor rejected operation submission: cannot schedule new futures after shutdown")

        memory.work = rejected
        control = InstallControl(pace=lambda: 0.0, should_stop=lambda: stopping[0])
        with _captured_logs() as logs:
            _auto_install(root, state, memory, control)
        _check(logs.messages(logging.ERROR) == [], "no 'auto-install failed' error for any KB")
        _check(sum("stopped at shutdown" in m for m in logs.messages(logging.INFO)) == 1, "one 'stopped at shutdown' line")
        with _captured_logs() as logs:
            kb.auto_install_knowledge_bases(root, state, FailingMemory(), None, budget=None, control=InstallControl(pace=lambda: 0.0, should_stop=lambda: False))
        _check(len(logs.messages(logging.ERROR)) == 2, "a failure while NOT stopping is still logged once per KB")


class FailingMemory(FakeMemoryService):
    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        raise RuntimeError("embedding failed")


@contextmanager
def _duty_env(value: str | None) -> Generator[None]:
    saved = os.environ.pop(KB_INSTALL_DUTY_ENV, None)
    if value is not None:
        os.environ[KB_INSTALL_DUTY_ENV] = value
    try:
        yield
    finally:
        os.environ.pop(KB_INSTALL_DUTY_ENV, None)
        if saved is not None:
            os.environ[KB_INSTALL_DUTY_ENV] = saved


def _step_duty_setting() -> None:
    print("[6] the duty setting parses; an invalid value uses the default with a warning")
    with _duty_env(None):
        _check(kb_install_duty() == DEFAULT_KB_INSTALL_DUTY, f"unset uses the default {DEFAULT_KB_INSTALL_DUTY}")
    with _duty_env("0.2"):
        _check(kb_install_duty() == 0.2, "0.2 is honoured")
    for bad in ("abc", "0", "-0.1", "1.5", "nan", "inf"):
        with _duty_env(bad), _captured_logs() as logs:
            duty = kb_install_duty()
        warned = any(KB_INSTALL_DUTY_ENV in m for m in logs.messages(logging.WARNING))
        _check(duty == DEFAULT_KB_INSTALL_DUTY and warned, f"{bad!r} uses the default and warns")


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _step_pause_arithmetic_and_early_stop() -> None:
    print("[7] the pause matches the duty and ends early at shutdown")
    clock = _FakeClock()
    control = paced_install_control(lambda: False, duty=0.05, clock=clock, sleep=clock.sleep)
    clock.now += 1.0
    control.checkpoint()
    _check(abs(sum(clock.slept) - 19.0) < 1e-9, f"1 s of work at duty 0.05 pauses 19 s ({sum(clock.slept):.3f})")
    _check(max(clock.slept) <= 0.5, "the pause is sliced so shutdown is noticed within half a second")

    clock = _FakeClock()
    stop_at = 3.0
    control = paced_install_control(lambda: clock.now >= stop_at, duty=0.05, clock=clock, sleep=clock.sleep)
    clock.now += 1.0
    try:
        control.checkpoint()
        cancelled = False
    except InstallCancelledError:
        cancelled = True
    _check(cancelled and clock.now <= stop_at + 0.5, f"a shutdown during the pause ends it and cancels ({clock.now:.1f} s, not 20 s)")


def _busy(seconds: float) -> None:
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


def _step_measured_duty() -> None:
    print("[8] a paced install keeps its thread under the configured duty (real clock)")
    duty = 0.25
    with tempfile.TemporaryDirectory(prefix="resumable_kb_smoke_") as tmp:
        root = Path(tmp)
        _make_kb(root, _KB, 3)
        state, memory = FakeStateService(), FakeMemoryService()
        memory.work = lambda: _busy(0.02)
        control = paced_install_control(lambda: False, duty=duty)
        wall0, cpu0 = time.monotonic(), time.thread_time()
        _auto_install(root, state, memory, control)
        wall, cpu = time.monotonic() - wall0, time.thread_time() - cpu0
        share = cpu / wall
        print(f"        {len(memory.remembered)} chunks, thread cpu {cpu:.3f} s over {wall:.3f} s wall = {share:.3f} of one core")
        _check(len(memory.remembered) >= 20, f"enough chunks to measure ({len(memory.remembered)})")
        _check(share <= duty * 1.1, f"thread CPU share {share:.3f} <= duty {duty} (+10%)")
        _check(_record_matches_store(state, memory, _KB), "the paced install completes with an exact record")


def _step_plugin_wiring() -> None:
    print("[9] the plugin passes the orchestrator's shutdown event to the install")

    class _Orchestrator:
        def __init__(self) -> None:
            self.shutdown_event = asyncio.Event()

    plugin = DefaultKnowledgePlugin()
    orchestrator = _Orchestrator()
    plugin.orchestrator_ref = orchestrator  # type: ignore[assignment]
    check = plugin._shutdown_check()  # pyright: ignore[reportPrivateUsage]
    _check(check() is False, "not stopping before the shutdown event is set")
    orchestrator.shutdown_event.set()
    _check(check() is True, "stopping once the shutdown event is set")

    plugin.orchestrator_ref = None
    with _captured_logs() as logs:
        check = plugin._shutdown_check()  # pyright: ignore[reportPrivateUsage]
    _check(check() is False and any("shutdown_event" in m for m in logs.messages(logging.WARNING)), "no shutdown event: the install still runs and a warning says so")


class _FakeEmbedder:
    """The startup entry point asks the embedding provider for its input budget."""

    def input_token_budget(self) -> None:
        return None


def _small_article_kb(root: Path, name: str, files: int) -> None:
    directory = root / name
    directory.mkdir()
    for index in range(files):
        (directory / f"{index}.md").write_text("# Session start orientation\n\nRead the knowledge base before work.\n", encoding="utf-8")


def _startup_pass(root: Path, state: FakeStateService, memory: FakeMemoryService, clock: _FakeClock) -> list[tuple[str, float, list[str], bool]]:
    """Use the real plugin, indexing, records and search with one simulated second per embedding."""
    plugin = DefaultKnowledgePlugin()
    plugin._kb_root = root  # pyright: ignore[reportPrivateUsage]
    plugin._state_service = state  # pyright: ignore[reportPrivateUsage]
    plugin._memory_service = memory  # pyright: ignore[reportPrivateUsage]
    plugin._embedding_service = _FakeEmbedder()  # pyright: ignore[reportPrivateUsage]
    observations: list[tuple[str, float, list[str], bool]] = []
    real_install = kb.install_kb

    def embed() -> None:
        clock.now += 1.0

    def timed_control(should_stop: Callable[[], bool], **kwargs: Any) -> InstallControl:
        return paced_install_control(should_stop, clock=clock, sleep=clock.sleep, **kwargs)

    def install(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = real_install(*args, **kwargs)
        active = get_active_names(state)
        results = collect_tiered_results("session start orientation", active, 8, None, None, memory, honor_exclusions=True)
        observations.append((str(args[0]), clock.now, active, bool(results)))
        return result

    memory.work = embed
    with patch.object(plugin_module, "paced_install_control", side_effect=timed_control), patch.object(kb, "install_kb", side_effect=install):
        plugin.auto_install_knowledge_bases()
    return observations


def _step_fresh_startup_policy() -> None:
    print("[10] a fresh startup is unthrottled, smallest first, with name ties")
    with tempfile.TemporaryDirectory() as temporary, _duty_env(None):
        root = Path(temporary)
        for name, files in (("a_large", 4), ("z_small", 1), ("m_small", 1)):
            _small_article_kb(root, name, files)
        clock = _FakeClock()
        observations = _startup_pass(root, FakeStateService(), FakeMemoryService(), clock)
        _check([row[0] for row in observations] == ["m_small", "z_small", "a_large"], "fresh candidates use file count then name")
        _check(not clock.slept and clock.now == 6.0, "the entire first pass embeds at duty 1.0")


def _step_existing_startup_policy() -> None:
    print("[11] any existing record and later passes retain configured pacing and alphabetical order")
    for record in ({"name": "partial", "memory_ids": ["partial-chunk"]}, {"name": "inactive", "is_active": 0}):
        with tempfile.TemporaryDirectory() as temporary, _duty_env("0.2"):
            root = Path(temporary)
            _small_article_kb(root, "a_large", 3)
            _small_article_kb(root, "z_small", 1)
            state, memory, clock = FakeStateService(), FakeMemoryService(), _FakeClock()
            state.records.append(record)
            observations = _startup_pass(root, state, memory, clock)
            _check([row[0] for row in observations] == ["a_large", "z_small"], f"{record['name']} record preserves alphabetical order")
            _check(clock.now == 16.0 and bool(clock.slept), f"{record['name']} record preserves configured duty 0.2 between chunks")
    with tempfile.TemporaryDirectory() as temporary, _duty_env(None):
        root = Path(temporary)
        _small_article_kb(root, "seed", 1)
        state, memory = FakeStateService(), FakeMemoryService()
        _startup_pass(root, state, memory, _FakeClock())
        _small_article_kb(root, "a_new_large", 3)
        _small_article_kb(root, "z_new_small", 1)
        clock = _FakeClock()
        observations = _startup_pass(root, state, memory, clock)
        _check([row[0] for row in observations] == ["a_new_large", "z_new_small"], "the pass after first completion returns to alphabetical order")
        _check(abs(clock.now - 61.0) < 1e-6, "the later pass returns to shipped duty 0.05 between chunks")


def _step_explicit_startup_duty() -> None:
    print("[12] explicit duty wins on a fresh startup while smallest-first order stays")
    with tempfile.TemporaryDirectory() as temporary, _duty_env("0.25"):
        root = Path(temporary)
        _small_article_kb(root, "a_large", 3)
        _small_article_kb(root, "z_small", 1)
        clock = _FakeClock()
        observations = _startup_pass(root, FakeStateService(), FakeMemoryService(), clock)
        _check([row[0] for row in observations] == ["z_small", "a_large"], "an explicit duty keeps the fresh smallest-first order")
        _check(clock.now == 13.0 and bool(clock.slept), "an explicit 0.25 overrides the first-pass default between chunks")


def _step_first_completion_is_searchable() -> None:
    print("[13] the first small KB makes real search nonempty before the large KB finishes")
    with tempfile.TemporaryDirectory() as temporary, _duty_env(None):
        root = Path(temporary)
        _small_article_kb(root, "a_large", 200)
        _small_article_kb(root, "z_orientation", 1)
        state, memory = FakeStateService(), FakeMemoryService()
        _check(not collect_tiered_results("session start orientation", get_active_names(state), 8, None, None, memory, honor_exclusions=True), "search starts empty")
        observations = _startup_pass(root, state, memory, _FakeClock())
        _check(bool(observations), "startup completed at least one real KB install")
        if observations:
            name, elapsed, active, searchable = observations[0]
            _check(name == "z_orientation" and elapsed < 180 and active == ["z_orientation"] and searchable, "search is nonempty at the small KB completion inside 180 seconds while the large KB has no record")
        _check(len(observations) == 2 and observations[-1][1] > 180, "the larger corpus finishes after the retrieval pass condition is already satisfied")


def run() -> None:
    for step in (
        _step_kill_and_resume,
        _step_repeated_kills,
        _step_legacy_duplicates,
        _step_shutdown_stops_between_chunks,
        _step_rejection_while_stopping,
        _step_duty_setting,
        _step_pause_arithmetic_and_early_stop,
        _step_measured_duty,
        _step_plugin_wiring,
        _step_fresh_startup_policy,
        _step_existing_startup_policy,
        _step_explicit_startup_duty,
        _step_first_completion_is_searchable,
    ):
        step()


def main() -> int:
    print("resumable, paced knowledge-base install smoke (prb_2e7713d4)")
    print("============================================================")
    run()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
