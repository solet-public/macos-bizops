#!/usr/bin/env python3
"""Smoke for `model_capability_harness` (iss_0e805222 / iss_52fdb8e5, design unit U1; rul_473f772f).

Offline and read-only. Every Codex call goes through a STUB process runner: the
live `codex debug models` is never run (it rewrites Codex's own cache file).
The Codex and Claude fixtures have the shapes measured on 2026-10-10 (design
part 0, M6 and M7), written into temporary directories that stand in for HOME.

Covers: the four statuses on both readers, the cache-file fallback (degraded,
with fetched_at and client_version), the binary path never coming from PATH,
the `cc` surface selection with ignored surfaces reported, the age advisories,
the adapter table, `join_key`, the date-stem join, the join on the 12 of 13
roster models, the one alias row, ambiguity reporting and alias candidates.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_harness_smoke.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))

from agent_messaging_plugin import model_capability_harness as harness  # noqa: E402
from agent_messaging_plugin.model_capability_harness import (  # noqa: E402
    HarnessContext,
    HarnessError,
    HarnessList,
    HarnessModel,
    ProcessResult,
)

_passed = 0
_failed: list[str] = []
_current = ""
_NOW = datetime(2026, 10, 10, 5, 30, tzinfo=UTC)
_ALL_EFFORTS = ("low", "medium", "high", "xhigh", "max")
_CODEX_VERSION = "codex-cli 0.159.0"

# --------------------------------------------------------------------------- fixtures

# (slug, visibility, efforts): the 0.159.0 list (M6): 7 visible, 4 hidden; `ultra` on 5 of the 7 visible.
_CODEX_LIST_0159: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("gpt-6.1-sol", "list", (*_ALL_EFFORTS, "ultra")),
    ("gpt-6-astra", "list", (*_ALL_EFFORTS, "ultra")),
    ("gpt-6-sol", "list", (*_ALL_EFFORTS, "ultra")),
    ("gpt-6-luna", "list", (*_ALL_EFFORTS, "ultra")),
    ("gpt-5.6-sol", "list", (*_ALL_EFFORTS, "ultra")),
    ("gpt-5.6-terra", "list", _ALL_EFFORTS),
    ("gpt-5.6-luna", "list", _ALL_EFFORTS),
    ("memory4-dream", "hide", ("low",)),
    ("gpt-reserve", "hide", ("low",)),
    ("gpt-5.5", "hide", _ALL_EFFORTS),
    ("codex-auto-review", "hide", ("low",)),
)
# The 0.156.0 list the shared cache file held at times: no gpt-6.1-sol.
_CODEX_LIST_0156 = tuple(row for row in _CODEX_LIST_0159 if row[0] != "gpt-6.1-sol")

# (id, section, thinking type, efforts): the cc cache (M7); the 4.5 Haiku entry has no effort axis.
_CLAUDE_LIST: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("claude-opus-5-5", "main", "effort", _ALL_EFFORTS),
    ("claude-fable-5-1", "main", "effort", _ALL_EFFORTS),
    ("claude-sonnet-5-5", "main", "effort", _ALL_EFFORTS),
    ("claude-haiku-5-5", "main", "effort", _ALL_EFFORTS),
    ("claude-haiku-4-5-20251001", "overflow", "none", ()),
    ("claude-sonnet-5", "overflow", "effort", _ALL_EFFORTS),
    ("claude-opus-5", "overflow", "effort", _ALL_EFFORTS),
    ("claude-fable-5", "overflow", "effort", _ALL_EFFORTS),
    ("claude-opus-4-8", "overflow", "effort", _ALL_EFFORTS),
    ("claude-opus-4-7", "overflow", "effort", _ALL_EFFORTS),
    ("claude-opus-4-6", "overflow", "effort", ("low", "medium", "high", "max")),
    ("claude-sonnet-4-6", "overflow", "effort", ("low", "medium", "high", "max")),
)

# The 13 roster models: (creator, leaderboard base name, catalog model id).
_ROSTER: tuple[tuple[str, str, str], ...] = (
    ("anthropic", "Claude 4.5 Haiku", "claude-haiku-4.5"),
    ("anthropic", "Claude Sonnet 5", "claude-sonnet-5"),
    ("anthropic", "Claude Sonnet 5.5", "claude-sonnet-5-5"),
    ("anthropic", "Claude Opus 5", "claude-opus-5"),
    ("anthropic", "Claude Opus 5.5", "claude-opus-5-5"),
    ("anthropic", "Claude Fable 5.1", "claude-fable-5-1"),
    ("openai", "GPT-5.6 Sol", "gpt-5.6-sol"),
    ("openai", "GPT-5.6 Terra", "gpt-5.6-terra"),
    ("openai", "GPT-5.6 Luna", "gpt-5.6-luna"),
    ("openai", "GPT-6 Astra", "gpt-6-astra"),
    ("openai", "GPT-6 Sol", "gpt-6-sol"),
    ("openai", "GPT-6.1 Sol", "gpt-6.1-sol"),
    ("openai", "GPT-6 Luna", "gpt-6-luna"),
)


def _codex_payload(rows: Sequence[tuple[str, str, tuple[str, ...]]]) -> dict[str, object]:
    return {"models": [{"slug": slug, "display_name": slug, "visibility": visibility, "supported_reasoning_levels": [{"effort": effort, "description": effort} for effort in efforts]} for slug, visibility, efforts in rows]}


def _claude_entry(model_id: str, section: str, kind: str, efforts: Sequence[str]) -> dict[str, object]:
    thinking: dict[str, object] = {"type": kind}
    if kind == "effort":
        thinking["effort_options"] = [{"id": effort, "name": effort.title()} for effort in efforts]
    return {"id": model_id, "name": model_id, "section": section, "thinking": thinking}


def _claude_file(surface: str, fetched: datetime, stale_at: datetime, version: int = 2, entries: Sequence[tuple[str, str, str, tuple[str, ...]]] = _CLAUDE_LIST) -> dict[str, object]:
    models = [_claude_entry(*entry) for entry in entries]
    return {"version": version, "fetchedAt": int(fetched.timestamp() * 1000), "staleAt": int(stale_at.timestamp() * 1000), "catalog": {"surface": surface, "config": {"id": "cfg", "models": models}, "state": {}}}


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _claude_dir(home: Path) -> Path:
    return home / ".claude" / "cache" / "model-catalog"


def _write_claude(home: Path, name: str, payload: dict[str, object]) -> None:
    _write_json(_claude_dir(home) / name, payload)


def _write_codex_cache(home: Path, rows: Sequence[tuple[str, str, tuple[str, ...]]], fetched_at: str = "2026-10-10T05:06:00.123456Z", client_version: str = "0.156.0") -> None:
    payload = {"fetched_at": fetched_at, "etag": 'W/"x"', "client_version": client_version, "identity": "id", "models": _codex_payload(rows)["models"]}
    _write_json(home / ".codex" / "models_cache.json", payload)


class _StubRunner:
    """Answers `--version` and `debug models` from fixtures and records every call; never starts a process."""

    def __init__(self, listing: ProcessResult | Exception, version: ProcessResult | Exception | None = None) -> None:
        self.listing = listing
        self.version = version if version is not None else ProcessResult(0, _CODEX_VERSION + "\n", "")
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Sequence[str], timeout_seconds: float) -> ProcessResult:
        self.calls.append(tuple(argv))
        answer = self.version if tuple(argv[1:]) == ("--version",) else self.listing
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ok_listing(rows: Sequence[tuple[str, str, tuple[str, ...]]] = _CODEX_LIST_0159) -> ProcessResult:
    return ProcessResult(0, json.dumps(_codex_payload(rows)), "")


def _fake_binary(root: Path) -> Path:
    binary = root / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")  # a stub never runs it; the reader only checks it is executable
    binary.chmod(0o755)
    return binary


def _context(home: Path, runner: Callable[[Sequence[str], float], ProcessResult], binary: Path | None = None) -> HarnessContext:
    return HarnessContext(home=home, codex_binary=binary if binary is not None else _fake_binary(home), run=runner, now=_NOW, list_stale_after=timedelta(days=7))


def _snapshot(home: Path) -> dict[str, tuple[bytes, int]]:
    return {str(p.relative_to(home)): (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(home.rglob("*")) if p.is_file() and p.suffix == ".json"}


def _model(result: HarnessList, model_id: str) -> HarnessModel:
    return next(model for model in result.models if model.model_id == model_id)


def _ids(result: HarnessList) -> set[str]:
    return {model.model_id for model in result.models}


# --------------------------------------------------------------------------- Codex reader


def test_codex_listing_from_the_launching_binary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        binary = _fake_binary(home)
        runner = _StubRunner(_ok_listing())
        result = harness.read_codex_harness(_context(home, runner, binary))
        _check(result.status == "ok" and result.runtime == "codex", "a readable listing from the binary is ok")
        _check(runner.calls == [(str(binary), "--version"), (str(binary), "debug", "models")], "the binary path given is the one run, for --version and debug models only")
        _check(result.client_version == _CODEX_VERSION, "the binary's --version is recorded")
        _check(len(result.models) == 11 and len(result.offered_models) == 7, "11 models listed, 7 offered")
        _check(not _model(result, "gpt-reserve").offered and _model(result, "gpt-reserve").visibility == "hide", "a hidden model is known but not offered")
        _check("ultra" in _model(result, "gpt-6.1-sol").efforts and "ultra" not in _model(result, "gpt-5.6-terra").efforts, "efforts are carried verbatim, ultra included where offered")
        _check(_model(result, "gpt-6.1-sol").model_id == "gpt-6.1-sol", "the launch id is the slug, dots kept")
        _check(result.fetched_at == "2026-10-10T05:30:00Z", "a command read is stamped with the read time")


def test_codex_binary_path_is_never_looked_up_on_path() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _write_codex_cache(home, _CODEX_LIST_0156)
        runner = _StubRunner(_ok_listing())
        result = harness.read_codex_harness(_context(home, runner, Path("codex")))
        _check(runner.calls == [], "a bare binary name is never run (no PATH lookup)")
        _check(result.status == "degraded" and "never resolved from PATH" in result.detail, "a non-absolute binary is degraded and says why")
        missing = harness.read_codex_harness(_context(home, runner, home / "bin" / "nope"))
        _check(runner.calls == [] and missing.status == "degraded" and "not an executable file" in missing.detail, "an unreachable absolute path is degraded and loud, never a guess")


def test_codex_command_failures_fall_back_to_the_cache_as_degraded() -> None:
    failures: tuple[ProcessResult | Exception, ...] = (
        ProcessResult(2, "", "boom"),
        subprocess.TimeoutExpired("codex", 60.0),
        FileNotFoundError("gone"),
        ProcessResult(0, "not json", ""),
        ProcessResult(0, "[]", ""),
        ProcessResult(0, json.dumps({"models": "nope"}), ""),
    )
    for failure in failures:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            _write_codex_cache(home, _CODEX_LIST_0156)
            result = harness.read_codex_harness(_context(home, _StubRunner(failure)))
            _check(result.status == "degraded" and len(result.offered_models) == 6, f"a cache read replaces a failed command ({type(failure).__name__ if isinstance(failure, Exception) else failure.stdout[:8]!r})")
            _check("2026-10-10T05:06:00.123456Z" in result.detail and "0.156.0" in result.detail, "the degraded detail names the file's fetched_at and client_version")
            _check(result.client_version == "0.156.0" and result.fetched_at.startswith("2026-10-10T05:06:00"), "fetched_at and client_version are fields too")


def test_a_cache_file_list_is_degraded_and_cannot_prove_absence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _write_codex_cache(home, _CODEX_LIST_0156)
        result = harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(1, "", "no"))))
        _check(result.status != "ok", "a cache-file Codex list is never ok")
        _check(result.status == "degraded" and "cannot prove a model absent" in result.detail, "the degraded read says it cannot prove absence")
        _check("gpt-6.1-sol" not in _ids(result) and "gpt-6-sol" in _ids(result), "the 0.156.0 cache lacks gpt-6.1-sol: the disagreement M6 measured")


def test_an_empty_list_is_empty_not_ok() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        result = harness.read_codex_harness(_context(home, _StubRunner(_ok_listing(()))))
        _check(result.status == "empty" and result.models == (), "a command that lists zero models is empty, not ok")
        hidden_only = harness.read_codex_harness(_context(home, _StubRunner(_ok_listing((("gpt-reserve", "hide", ("low",)),)))))
        _check(hidden_only.status == "empty" and len(hidden_only.models) == 1, "only hidden models offers nothing: empty")
        _write_codex_cache(home, ())
        cached = harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(1, "", "no"))))
        _check(cached.status == "empty", "an empty cache file is empty too")


def test_codex_unreadable_when_neither_source_works() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        result = harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(3, "", "denied"))))
        _check(result.status == "unreadable" and result.models == (), "no command and no cache file is unreadable with zero models")
        _check("exited 3" in result.detail and "cache file also unreadable" in result.detail, "both faults are named")
        (home / ".codex").mkdir()
        (home / ".codex" / "models_cache.json").write_text("{not json", encoding="utf-8")
        garbled = harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(3, "", "denied"))))
        _check(garbled.status == "unreadable", "a garbled cache file is unreadable")


def test_codex_entries_without_a_slug_are_skipped_loudly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        body = {"models": [{"slug": "a-model", "visibility": "list", "supported_reasoning_levels": []}, {"visibility": "list"}, "junk"]}
        result = harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(0, json.dumps(body), ""))))
        _check(result.status == "ok" and _ids(result) == {"a-model"}, "an entry with no slug is dropped, the rest is read")
        _check(any("no slug" in note for note in result.advisories), "the dropped entry is an advisory")
        silent = harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(0, json.dumps(body), ""), ProcessResult(1, "", "no"))))
        _check(silent.status == "ok" and any("version" in note for note in silent.advisories) and silent.client_version == "", "an unanswered --version is an advisory, not a fault")


# --------------------------------------------------------------------------- Claude reader


def _fresh_claude(home: Path, name: str = "org-hash-cc.json", surface: str = "cc", **kwargs: object) -> None:
    fetched = kwargs.pop("fetched", _NOW - timedelta(minutes=61))
    stale_at = kwargs.pop("stale_at", _NOW + timedelta(minutes=57))
    version = kwargs.pop("version", 2)
    assert isinstance(fetched, datetime) and isinstance(stale_at, datetime) and isinstance(version, int)
    _write_claude(home, name, _claude_file(surface, fetched, stale_at, version))


def test_claude_reads_the_cc_surface() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _fresh_claude(home)
        result = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(result.status == "ok" and result.runtime == "claude_code" and len(result.models) == 12, "12 cc models read, ok")
        _check(all(model.offered for model in result.models), "every Claude entry is offered")
        _check(_model(result, "claude-haiku-4-5-20251001").efforts == ("non_reasoning",), "thinking type none is non_reasoning")
        _check(_model(result, "claude-opus-5-5").efforts == _ALL_EFFORTS and _model(result, "claude-opus-4-6").efforts == ("low", "medium", "high", "max"), "efforts come from effort_options, verbatim")
        _check(_model(result, "claude-opus-5-5").visibility == "main" and _model(result, "claude-fable-5").visibility == "overflow", "the section is carried")
        _check(result.advisories == () and result.fetched_at == "2026-10-10T04:29:00Z", "a fresh file has no advisory; fetchedAt epoch milliseconds are read")


def test_claude_ignores_other_surfaces_and_reports_them() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _fresh_claude(home)
        other = _claude_file("ccd", _NOW, _NOW + timedelta(hours=1), 2, (("claude-only-on-ccd", "main", "effort", ("low",)),))
        _write_claude(home, "tok-abc-ccd.json", other)
        result = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check("claude-only-on-ccd" not in _ids(result), "a model only another surface lists is not read")
        _check(result.ignored_surfaces == ("ccd:tok-abc-ccd.json",), "the ignored surface is reported by surface and file")
        only_ccd = Path(tmp) / "second"
        _write_claude(only_ccd, "tok-abc-ccd.json", other)
        none = harness.read_claude_harness(_context(only_ccd, _StubRunner(_ok_listing())))
        _check(none.status == "unreadable" and "ccd:tok-abc-ccd.json" in none.detail, "no cc file is unreadable and the detail names what was ignored")


def test_claude_takes_the_newest_cc_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        older = _claude_file("cc", _NOW - timedelta(hours=5), _NOW, 2, (("claude-old-only", "main", "effort", ("low",)),))
        newer = _claude_file("cc", _NOW - timedelta(hours=1), _NOW + timedelta(hours=1), 2, (("claude-new-only", "main", "effort", ("low",)),))
        _write_claude(home, "a-cc.json", newer)
        _write_claude(home, "b-cc.json", older)
        result = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(_ids(result) == {"claude-new-only"}, "the cc file with the newest fetchedAt wins, whatever its name")
        _check(result.ignored_surfaces == ("cc:b-cc.json (older)",), "the older cc file is reported as ignored")


def test_claude_unreadable_cases() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        missing = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(missing.status == "unreadable" and missing.models == (), "no cache directory is unreadable")
        _fresh_claude(home, version=3)
        wrong_version = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(wrong_version.status == "unreadable" and "version 3" in wrong_version.detail, "a cache version it does not understand is unreadable with that reason, not a guess")
        (_claude_dir(home) / "broken-cc.json").write_text("{nope", encoding="utf-8")
        _fresh_claude(home)
        recovered = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(recovered.status == "ok" and any("broken-cc.json" in note for note in recovered.advisories), "a garbled file beside a good one is an advisory, not a fault")
        no_models = _claude_file("cc", _NOW, _NOW + timedelta(hours=1))
        catalog = no_models["catalog"]
        assert isinstance(catalog, dict)
        catalog["config"] = {"id": "x"}
        _write_claude(home, "z-cc.json", no_models)
        shapeless = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(shapeless.status == "unreadable" and "catalog.config.models" in shapeless.detail, "a newest cc file with no models array is unreadable")


def test_claude_empty_and_odd_entries() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _write_claude(home, "x-cc.json", _claude_file("cc", _NOW, _NOW + timedelta(hours=1), 2, ()))
        empty = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(empty.status == "empty" and empty.models == (), "a cc file with zero models is empty")
        payload = _claude_file("cc", _NOW, _NOW + timedelta(hours=1), 2, (("claude-a", "main", "effort", ("low",)),))
        config = payload["catalog"]
        assert isinstance(config, dict)
        models = config["config"]["models"]
        models.append({"name": "no id"})
        models.append({"id": "claude-odd", "section": "main", "thinking": {"type": "mystery"}})
        _write_claude(home, "x-cc.json", payload)
        result = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(_ids(result) == {"claude-a", "claude-odd"} and _model(result, "claude-odd").efforts == (), "an entry with no id is dropped; an unknown thinking type keeps the model with no efforts")
        _check(any("no id" in note for note in result.advisories) and any("mystery" in note for note in result.advisories), "both are advisories")


def test_claude_age_is_an_advisory_not_a_refusal() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _fresh_claude(home, fetched=_NOW - timedelta(hours=3), stale_at=_NOW - timedelta(hours=1))
        past_stale = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(past_stale.status == "ok" and any("past its own staleAt" in note for note in past_stale.advisories), "a list past its own staleAt stays ok with an advisory")
        _fresh_claude(home, fetched=_NOW - timedelta(days=8), stale_at=_NOW - timedelta(days=8) + timedelta(hours=1))
        week_old = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
        _check(week_old.status == "ok" and any("older than 7 days, 0:00:00" in note for note in week_old.advisories), "a list older than seven days stays ok with an advisory")


def test_the_readers_write_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _fresh_claude(home)
        _write_codex_cache(home, _CODEX_LIST_0156)
        binary = _fake_binary(home)
        before = _snapshot(home)
        listing = set(home.rglob("*"))
        harness.read_claude_harness(_context(home, _StubRunner(_ok_listing()), binary))
        harness.read_codex_harness(_context(home, _StubRunner(_ok_listing()), binary))
        harness.read_codex_harness(_context(home, _StubRunner(ProcessResult(1, "", "no")), binary))
        _check(_snapshot(home) == before and set(home.rglob("*")) == listing, "no cache file or directory changed, in a command read and in a fallback read")


def test_the_default_runner_returns_the_process_result() -> None:
    result = harness.run_process((sys.executable, "-I", "-c", "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"), 30.0)
    _check(result == ProcessResult(3, "out\n", "err\n"), "run_process reports returncode, stdout and stderr")


# --------------------------------------------------------------------------- adapters


def test_the_adapter_table() -> None:
    _check(set(harness.RUNTIME_ADAPTERS) == {"claude_code", "codex"}, "two runtimes")
    claude, codex = harness.RUNTIME_ADAPTERS["claude_code"], harness.RUNTIME_ADAPTERS["codex"]
    _check((claude.provider, codex.provider) == ("anthropic", "openai"), "the provider comes from the table")
    _check(claude.read_harness is harness.read_claude_harness and codex.read_harness is harness.read_codex_harness, "each adapter holds its own reader")
    _check(harness.adapter_for_runtime("codex") is codex, "lookup by runtime")
    _check(harness.adapter_for_creator(" OpenAI ") is codex and harness.adapter_for_creator("Anthropic") is claude, "lookup by leaderboard creator label ignores case and spaces")
    _check(harness.adapter_for_creator("Meta") is None, "a creator with no runtime has no adapter: listed, not dispatchable")


def test_an_unknown_runtime_raises() -> None:
    raised = False
    try:
        harness.adapter_for_runtime("gemini_cli")
    except HarnessError as exc:
        raised = "gemini_cli" in str(exc)
    _check(raised, "an unknown runtime raises and names itself; it never defaults to a provider")


def test_id_shapes_and_derivation() -> None:
    claude, codex = harness.RUNTIME_ADAPTERS["claude_code"], harness.RUNTIME_ADAPTERS["codex"]
    _check(claude.derive_id("Claude Sonnet 5.5") == "claude-sonnet-5-5" and codex.derive_id("GPT-6.1 Sol") == "gpt-6.1-sol", "Anthropic dots become hyphens; OpenAI dots are kept")
    _check(bool(claude.id_shape.match("claude-sonnet-5-5")) and not claude.id_shape.match("claude-sonnet-5.5"), "the Anthropic shape has no dots")
    _check(bool(codex.id_shape.match("gpt-6.1-sol")) and not codex.id_shape.match("gpt 6"), "the OpenAI shape keeps dots and rejects spaces")


# --------------------------------------------------------------------------- join


def test_join_key_normalizes() -> None:
    _check(harness.join_key("GPT-6.1 Sol") == "gpt-6-1-sol" and harness.join_key("gpt-6.1-sol") == "gpt-6-1-sol", "dots, spaces and hyphens all reduce to one hyphen")
    _check(harness.join_key("  --Claude   Opus__5.5!! ") == "claude-opus-5-5", "runs collapse and the ends are stripped")
    _check(harness.join_key("") == "" and harness.join_key("!!!") == "", "nothing alphanumeric is the empty key")


def test_a_date_stamped_harness_id_joins_on_its_stem() -> None:
    _check(harness.harness_join_keys("claude-haiku-4-5-20251001") == ("claude-haiku-4-5-20251001", "claude-haiku-4-5"), "an eight-digit date stamp adds the stem key")
    _check(harness.harness_join_keys("gpt-6.1-sol") == ("gpt-6-1-sol",), "no stamp, one key")
    _check(harness.harness_join_keys("model-1234567") == ("model-1234567",), "seven digits is not a date stamp")
    models = (HarnessModel("claude-haiku-4-5-20251001", (), True, "overflow"),)
    result = harness.join_models("anthropic", ("Claude Haiku 4.5",), models, {})
    _check(result.joined == {"Claude Haiku 4.5": "claude-haiku-4-5-20251001"}, "a name whose key equals the stem joins the stamped id")


def test_joins_a_dotted_codex_slug_to_the_leaderboard_name() -> None:
    models = (HarnessModel("gpt-6.1-sol", _ALL_EFFORTS, True, "list"), HarnessModel("gpt-6-sol", _ALL_EFFORTS, True, "list"))
    result = harness.join_models("openai", ("GPT-6.1 Sol", "GPT-6 Sol"), models)
    _check(result.joined == {"GPT-6.1 Sol": "gpt-6.1-sol", "GPT-6 Sol": "gpt-6-sol"}, "gpt-6.1-sol joins `GPT-6.1 Sol` and does not collide with gpt-6-sol")


def _roster_join(creator: str, models: Sequence[HarnessModel], aliases: Mapping[tuple[str, str], str]) -> harness.JoinResult:
    names = tuple(name for owner, name, _ in _ROSTER if owner == creator)
    return harness.join_models(creator, names, models, aliases)


def _fixture_models(tmp: str) -> tuple[tuple[HarnessModel, ...], tuple[HarnessModel, ...]]:
    home = Path(tmp)
    _fresh_claude(home)
    claude = harness.read_claude_harness(_context(home, _StubRunner(_ok_listing())))
    codex = harness.read_codex_harness(_context(home, _StubRunner(_ok_listing())))
    return claude.models, codex.models


def test_the_join_covers_12_of_13_roster_models_and_the_alias_makes_13() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        claude_models, codex_models = _fixture_models(tmp)
        no_alias = {**_roster_join("anthropic", claude_models, {}).joined, **_roster_join("openai", codex_models, {}).joined}
        _check(len(no_alias) == 12, "12 of the 13 roster models join by the single rule")
        expected = {name: model for _, name, model in _ROSTER if model != "claude-haiku-4.5"}
        _check(all(harness.join_key(no_alias[name]) == harness.join_key(model) for name, model in expected.items()), "each joins to the harness id whose key equals the catalog id's key")
        _check("Claude 4.5 Haiku" not in no_alias, "the one that does not join is claude-haiku-4.5 (word order differs)")
        with_alias = _roster_join("anthropic", claude_models, harness.JOIN_ALIASES)
        _check(with_alias.joined["Claude 4.5 Haiku"] == "claude-haiku-4-5-20251001" and with_alias.via_alias == ("Claude 4.5 Haiku",), "the alias row joins it, and reports that it was by alias")
        _check(len(with_alias.joined) == 6 and with_alias.unmatched_names == () and with_alias.ambiguous == (), "all 6 Anthropic roster names join, none ambiguous")
        codex_join = _roster_join("openai", codex_models, harness.JOIN_ALIASES)
        _check(len(codex_join.joined) == 7 and codex_join.via_alias == (), "all 7 OpenAI roster names join with no alias")


def test_models_without_a_leaderboard_row_are_listed_and_hidden_models_never_join() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        claude_models, codex_models = _fixture_models(tmp)
        claude = _roster_join("anthropic", claude_models, harness.JOIN_ALIASES)
        _check(set(claude.unmatched_model_ids) == {"claude-haiku-5-5", "claude-fable-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6"}, "harness models with no leaderboard name are reported, not dropped")
        gpt55 = harness.join_models("openai", ("GPT-5.5",), codex_models, {})
        _check(gpt55.joined == {} and gpt55.unmatched_names == ("GPT-5.5",), "a hidden model is not offered, so a leaderboard row for it is listed-not-launchable")
        _check("gpt-5.5" not in harness.join_models("openai", (), codex_models).unmatched_model_ids, "a hidden model is not reported as an unmatched offered model")


def test_without_the_alias_row_the_pair_is_reported_as_an_alias_candidate() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        claude_models, _ = _fixture_models(tmp)
        result = _roster_join("anthropic", claude_models, {})
        _check(result.unmatched_names == ("Claude 4.5 Haiku",), "without its alias the name is unmatched")
        _check(result.alias_candidates == (harness.AliasCandidate("Claude 4.5 Haiku", "claude-haiku-4-5-20251001"),), "the pair is reported as an alias candidate and not joined")
        _check(all(c.model_id != "claude-haiku-5-5" for c in result.alias_candidates), "a different-token model (5.5) is not a candidate")


def test_ambiguity_is_reported_and_never_guessed() -> None:
    collide = (HarnessModel("gpt-5-6-sol", (), True, "list"), HarnessModel("gpt-5.6-sol", (), True, "list"))
    result = harness.join_models("openai", ("GPT-5.6 Sol",), collide, {})
    _check(result.joined == {} and len(result.ambiguous) == 1, "two models with one join key: nothing joined, one ambiguity")
    _check(result.ambiguous[0] == harness.JoinAmbiguity("gpt-5-6-sol", ("GPT-5.6 Sol",), ("gpt-5-6-sol", "gpt-5.6-sol")), "the ambiguity names the key, the name and both ids")
    _check(result.unmatched_model_ids == () and result.unmatched_names == (), "ambiguous names and models are not also reported as unmatched")
    twins = harness.join_models("openai", ("GPT-6 Sol", "GPT 6 Sol"), (HarnessModel("gpt-6-sol", (), True, "list"),), {})
    _check(twins.joined == {} and twins.ambiguous[0].names == ("GPT-6 Sol", "GPT 6 Sol"), "two names sharing one key are ambiguous too")
    swapped = harness.join_models("openai", ("GPT-6.5 Sol",), (HarnessModel("gpt-5.6-sol", (), True, "list"),), {})
    _check(swapped.joined == {} and len(swapped.alias_candidates) == 1, "equal sorted tokens (5.6 vs 6.5) are only a candidate, never a join")


def test_an_exact_key_beats_a_date_stem() -> None:
    models = (HarnessModel("claude-sonnet-5-20260101", (), True, "overflow"), HarnessModel("claude-sonnet-5", (), True, "main"))
    result = harness.join_models("anthropic", ("Claude Sonnet 5",), models, {})
    _check(result.joined == {"Claude Sonnet 5": "claude-sonnet-5"} and result.ambiguous == (), "the exact id wins over the stamped id's stem")
    only_stamped = harness.join_models("anthropic", ("Claude Sonnet 5",), models[:1], {})
    _check(only_stamped.joined == {"Claude Sonnet 5": "claude-sonnet-5-20260101"}, "with only the stamped id, the stem joins")


def test_the_alias_table_has_one_row_and_its_target_must_be_offered() -> None:
    _check(dict(harness.JOIN_ALIASES) == {("anthropic", "Claude 4.5 Haiku"): "claude-haiku-4-5-20251001"}, "JOIN_ALIASES is exactly the one reviewed row")
    absent = harness.join_models("anthropic", ("Claude 4.5 Haiku",), (HarnessModel("claude-opus-5", (), True, "main"),))
    _check(absent.joined == {} and absent.unmatched_names == ("Claude 4.5 Haiku",), "an alias whose target the harness does not offer joins nothing")
    other_creator = harness.join_models("openai", ("Claude 4.5 Haiku",), (HarnessModel("claude-haiku-4-5-20251001", (), True, "main"),))
    _check(other_creator.joined == {}, "an alias is keyed by creator")


# --------------------------------------------------------------------------- runner


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(f"{_current}: {label}")
        print(f"  FAIL  {label}")


_TESTS: tuple[Callable[[], None], ...] = (
    test_codex_listing_from_the_launching_binary,
    test_codex_binary_path_is_never_looked_up_on_path,
    test_codex_command_failures_fall_back_to_the_cache_as_degraded,
    test_a_cache_file_list_is_degraded_and_cannot_prove_absence,
    test_an_empty_list_is_empty_not_ok,
    test_codex_unreadable_when_neither_source_works,
    test_codex_entries_without_a_slug_are_skipped_loudly,
    test_claude_reads_the_cc_surface,
    test_claude_ignores_other_surfaces_and_reports_them,
    test_claude_takes_the_newest_cc_file,
    test_claude_unreadable_cases,
    test_claude_empty_and_odd_entries,
    test_claude_age_is_an_advisory_not_a_refusal,
    test_the_readers_write_nothing,
    test_the_default_runner_returns_the_process_result,
    test_the_adapter_table,
    test_an_unknown_runtime_raises,
    test_id_shapes_and_derivation,
    test_join_key_normalizes,
    test_a_date_stamped_harness_id_joins_on_its_stem,
    test_joins_a_dotted_codex_slug_to_the_leaderboard_name,
    test_the_join_covers_12_of_13_roster_models_and_the_alias_makes_13,
    test_models_without_a_leaderboard_row_are_listed_and_hidden_models_never_join,
    test_without_the_alias_row_the_pair_is_reported_as_an_alias_candidate,
    test_ambiguity_is_reported_and_never_guessed,
    test_an_exact_key_beats_a_date_stem,
    test_the_alias_table_has_one_row_and_its_target_must_be_offered,
)


def main() -> int:
    global _current
    print("model_capability_harness_smoke")
    for test in _TESTS:
        _current = test.__name__
        print(f"{_current}")
        try:
            test()
        except Exception as exc:  # a raising test is a failing test, named in the summary
            _failed.append(f"{_current}: raised {type(exc).__name__}: {exc}")
            print(f"  FAIL  raised {type(exc).__name__}: {exc}")
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for failure in _failed:
        print(f"  FAILED {failure}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
