#!/usr/bin/env python3
"""Smoke: a stale KB re-embeds only the files that changed (prb_f6964132).

Before this fix, ``auto_install_knowledge_bases`` re-indexed a whole knowledge
base when any one file was newer than its install: every chunk was forgotten
and embedded again. One edit to one roster file re-embedded the 87-file
``ananta_platform`` KB on every deploy and kept a core busy for many minutes.

This smoke drives the real auto-install path (staleness gate, install record,
chunking and per-file digest tags) over a scratch KB, with in-memory stand-ins
for the state and memory services that count ``remember`` calls. It checks:

* a first install tags every chunk with exactly one per-file digest;
* an unchanged KB is still skipped (no remember, no delete);
* a one-file change embeds only that file's chunks and deletes its old ones;
* an added file embeds only its own chunks;
* a removed file's chunks are deleted when the KB is next refreshed;
* a KB installed before digests existed re-indexes in full once, then
  becomes incremental;
* a KB whose embeddings were purged still takes the full install.

Project policy: no pytest. Exits 0 on success, 1 on the first failed run.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/incremental_kb_reindex_smoke.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

import default_knowledge_plugin.kb_lifecycle as kb  # noqa: E402
from default_knowledge_plugin.constants import (  # noqa: E402
    PLUGIN_NAME,
    TAG_PREFIX_FILE_DIGEST,
    document_tag,
    file_digest_tag,
    kb_id_tag,
)
from default_knowledge_plugin.kb_indexing import (  # noqa: E402
    FileChunkPlan,
    IncrementalIndexResult,
    index_changed_files,
    plan_file_chunks,
    resolve_manifest,
)

_passed = 0
_failed: list[str] = []

_KB = "scratch_kb"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class FakeMemoryService:
    """Chunk memories in a dict; counts every remember and every deleted id."""

    def __init__(self) -> None:
        self.memories: dict[str, dict[str, Any]] = {}
        self.remembered: list[str] = []
        self.deleted: list[str] = []
        self._next = 0

    def reset_counts(self) -> None:
        self.remembered = []
        self.deleted = []

    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        self._next += 1
        memory_id = f"mem-{self._next}"
        self.memories[memory_id] = {"id": memory_id, "content": content, "tags": list(tags), "status": "active"}
        self.remembered.append(memory_id)
        return {"memory_id": memory_id}

    def recall(self, query: str, top_k: int, tags: list[str]) -> dict[str, Any]:
        del query
        hits = [m for m in self.memories.values() if all(t in m["tags"] for t in tags)]
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
        ids = [mid for mid, m in self.memories.items() if tag in m["tags"]]
        return self.delete_memories_by_ids(ids)

    def ids_for_file(self, relative: str) -> set[str]:
        doc = document_tag(relative)
        return {mid for mid, m in self.memories.items() if doc in m["tags"]}


class _FakeTxn:
    def __init__(self, state: FakeStateService) -> None:
        self._state = state

    def delete_records(self, namespace: str, query: dict[str, Any]) -> None:
        assert namespace == PLUGIN_NAME
        name = query["filters"]["name"]
        self._state.records = [r for r in self._state.records if r["name"] != name]

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
        rows = [r for r in self.records if all(r.get(k) == v for k, v in filters.items())]
        return {"data": {"records": [dict(r) for r in rows]}}

    @contextmanager
    def transactional(self) -> Generator[_FakeTxn]:
        yield _FakeTxn(self)

    def generate_id(self, prefix: str) -> str:
        self._next += 1
        return f"{prefix}{self._next}"

    def prepare_record(self, namespace: str, table: str, record: dict[str, Any]) -> dict[str, Any]:
        del namespace, table
        return dict(record)

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> None:
        assert namespace == PLUGIN_NAME
        name = query["filters"]["name"]
        for record in self.records:
            if record["name"] == name:
                record.update(updates)

    def record(self) -> dict[str, Any]:
        rows = [r for r in self.records if r["name"] == _KB]
        assert len(rows) == 1, rows
        return rows[0]


def _article(title: str, sections: int, body: str) -> str:
    parts = [f"# {title}\n\nArticle Role: reference\n"]
    for index in range(sections):
        parts.append(f"## Section {index} of {title}\n\n{body} {index}. " + "Detail sentence. " * 90 + "\n")
    return "\n".join(parts)


def _write(kb_dir: Path, relative: str, text: str) -> None:
    path = kb_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    future = time.time() + 5
    os.utime(path, (future, future))


def _make_kb(root: Path) -> Path:
    kb_dir = root / _KB
    kb_dir.mkdir()
    _write(kb_dir, "alpha.md", _article("Alpha", 3, "alpha body"))
    _write(kb_dir, "beta.md", _article("Beta", 4, "beta body"))
    _write(kb_dir, "nested/gamma.md", _article("Gamma", 2, "gamma body"))
    _write(kb_dir, "nested_gamma.md", _article("Gamma twin", 2, "same doc tag as nested/gamma.md"))
    return kb_dir


def _auto_install(root: Path, state: FakeStateService, memory: FakeMemoryService) -> None:
    memory.reset_counts()
    kb.auto_install_knowledge_bases(root, state, memory, None, budget=None)


def _digest_tags(memory: FakeMemoryService, memory_id: str) -> list[str]:
    return [t for t in memory.memories[memory_id]["tags"] if t.startswith(TAG_PREFIX_FILE_DIGEST)]


def _record_matches_store(state: FakeStateService, memory: FakeMemoryService) -> bool:
    record = state.record()
    live = {mid for mid, m in memory.memories.items() if kb_id_tag(_KB) in m["tags"]}
    return set(record["memory_ids"]) == live and record["chunk_count"] == len(live)


def _settle_install_time(state: FakeStateService, kb_dir: Path) -> None:
    """Move indexed_at just past the newest scratch file, as a real later boot would see it."""
    newest = max(path.stat().st_mtime for path in kb_dir.rglob("*") if path.is_file())
    state.record()["indexed_at"] = datetime.fromtimestamp(newest + 1, tz=UTC).isoformat()


def _touch_after_install(path: Path, state: FakeStateService) -> None:
    after = datetime.fromisoformat(state.record()["indexed_at"]).timestamp() + 10
    os.utime(path, (after, after))


@dataclass(frozen=True, slots=True)
class _Scratch:
    root: Path
    kb_dir: Path
    state: FakeStateService
    memory: FakeMemoryService

    def install(self) -> None:
        _auto_install(self.root, self.state, self.memory)

    def edit(self, relative: str, text: str) -> None:
        _write(self.kb_dir, relative, text)
        _touch_after_install(self.kb_dir / relative, self.state)

    def settle(self) -> None:
        _settle_install_time(self.state, self.kb_dir)


def _step_first_install(kb: _Scratch) -> None:
    print("[1] first install records a per-file digest on every chunk")
    memory = kb.memory
    kb.install()
    total = len(memory.memories)
    _check(total > 8, f"first install embedded the whole KB ({total} chunks)")
    _check(all(len(_digest_tags(memory, mid)) == 1 for mid in memory.memories), "every chunk carries exactly one file digest tag")
    alpha_ids = memory.ids_for_file("alpha.md")
    _check(len({tuple(_digest_tags(memory, mid)) for mid in alpha_ids}) == 1, "all chunks of one file share one digest")
    _check(_record_matches_store(kb.state, memory), "install record lists exactly the stored chunks")
    kb.settle()


def _step_unchanged(kb: _Scratch) -> None:
    print("[2] an unchanged KB is still skipped")
    kb.install()
    _check(kb.memory.remembered == [] and kb.memory.deleted == [], "no remember and no delete for an unchanged KB")


def _step_one_file_change(kb: _Scratch) -> None:
    print("[3] a one-file change embeds only that file's chunks")
    memory = kb.memory
    alpha_ids = memory.ids_for_file("alpha.md")
    beta_old = memory.ids_for_file("beta.md")
    kb.edit("beta.md", _article("Beta", 5, "beta body changed"))
    kb.install()
    beta_new = memory.ids_for_file("beta.md")
    _check(
        set(memory.remembered) == beta_new and len(beta_new) < len(memory.memories),
        f"remember calls == changed file's chunks ({len(memory.remembered)} of {len(memory.memories)})",
    )
    _check(set(memory.deleted) == beta_old, "only the changed file's old chunks were deleted")
    _check(memory.ids_for_file("alpha.md") == alpha_ids, "unchanged file kept its chunk memories")
    _check(_record_matches_store(kb.state, memory), "install record matches the store after the change")
    kb.settle()


def _step_added_file(kb: _Scratch) -> None:
    print("[4] an added file embeds only its own chunks")
    memory = kb.memory
    kb.edit("delta.md", _article("Delta", 2, "delta body"))
    kb.install()
    _check(memory.remembered and set(memory.remembered) == memory.ids_for_file("delta.md"), "remember calls == added file's chunks")
    _check(memory.deleted == [], "adding a file deletes nothing")
    _check(_record_matches_store(kb.state, memory), "install record includes the added file")
    kb.settle()


def _step_removed_file(kb: _Scratch) -> None:
    print("[5] a removed file's chunks are deleted on the next refresh")
    memory = kb.memory
    alpha_ids = memory.ids_for_file("alpha.md")
    gamma_ids = memory.ids_for_file("nested/gamma.md")
    twin_ids = {mid for mid in gamma_ids if "twin" in memory.memories[mid]["content"]}
    (kb.kb_dir / "nested" / "gamma.md").unlink()
    kb.edit("alpha.md", _article("Alpha", 3, "alpha body edited"))
    kb.install()
    _check(memory.remembered and set(memory.remembered) == memory.ids_for_file("alpha.md"), "only the edited file was embedded; the removed one was not")
    _check(set(memory.deleted) == (gamma_ids - twin_ids) | alpha_ids, "removed file's and edited file's old chunks deleted")
    _check(twin_ids and memory.ids_for_file("nested_gamma.md") == twin_ids, "a file sharing the removed file's doc tag kept its chunks")
    _check(_record_matches_store(kb.state, memory), "install record drops the removed file")
    kb.settle()


def _step_legacy_install(kb: _Scratch) -> None:
    print("[6] a KB installed before digests existed re-indexes in full once")
    memory = kb.memory
    for stored in memory.memories.values():
        stored["tags"] = [t for t in stored["tags"] if not t.startswith(TAG_PREFIX_FILE_DIGEST)]
    legacy = set(memory.memories)
    _touch_after_install(kb.kb_dir / "beta.md", kb.state)
    kb.install()
    _check(set(memory.deleted) == legacy, "every chunk without a digest was replaced")
    _check(len(memory.remembered) == len(memory.memories) == len(legacy), "full re-index embedded the whole KB once")
    kb.settle()
    kb.edit("delta.md", _article("Delta", 2, "delta body again"))
    kb.install()
    _check(memory.remembered and set(memory.remembered) == memory.ids_for_file("delta.md"), "the next one-file change is incremental again")
    kb.settle()


def _step_purged_embeddings(kb: _Scratch) -> None:
    print("[7] purged embeddings still take the full install")
    memory = kb.memory
    size = len(memory.memories)
    memory.memories.clear()
    kb.install()
    _check(len(memory.remembered) == size, "every chunk embedded again after a purge")
    _check(_record_matches_store(kb.state, memory), "install record rebuilt after a purge")


def _step_direct_api(kb_dir: Path) -> None:
    print("[8] install status, file plan and refresh counts")
    state, memory = FakeStateService(), FakeMemoryService()
    _check(kb.kb_install_status(_KB, state, memory, kb_dir.parent) == "missing", "no record is 'missing' (install, resuming from any chunks already stored)")
    kb.auto_install_knowledge_bases(kb_dir.parent, state, memory, None, budget=None)
    _settle_install_time(state, kb_dir)
    _check(kb.kb_install_status(_KB, state, memory, kb_dir.parent) == "valid", "a current install is 'valid' (skip)")
    _touch_after_install(kb_dir / "alpha.md", state)
    _check(kb.kb_install_status(_KB, state, memory, kb_dir.parent) == "stale", "a newer source file is 'stale' (refresh)")

    manifest = resolve_manifest(kb_dir, _KB, None)
    plan: FileChunkPlan = plan_file_chunks(kb_dir, kb_dir / "alpha.md", _KB, manifest, budget=None)
    again = plan_file_chunks(kb_dir, kb_dir / "alpha.md", _KB, manifest, budget=None)
    _check(plan.digest == again.digest and file_digest_tag(plan.digest) in plan.tags, "a file's digest is stable and carried as its tag")
    _check(plan.relative == "alpha.md" and len(plan.texts) == len(memory.ids_for_file("alpha.md")), "the plan holds one text per stored chunk")

    memory.reset_counts()
    _write(kb_dir, "beta.md", _article("Beta", 1, "beta shrunk"))
    result: IncrementalIndexResult = index_changed_files(kb_dir, _KB, manifest, memory, budget=None)
    beta_chunks = len(memory.ids_for_file("beta.md"))
    _check(result.files_indexed == 1 and result.chunks_embedded == beta_chunks, "refresh counts one re-embedded file and its chunks")
    _check(result.files_unchanged == 3 and result.chunks_deleted == len(memory.deleted) > 0, "refresh counts unchanged files and deleted chunks")
    _check(result.chunk_count == len(result.memory_ids) == len(memory.memories), "refresh memory ids cover every stored chunk")


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="incremental_kb_smoke_") as tmp:
        root = Path(tmp)
        kb = _Scratch(root=root, kb_dir=_make_kb(root), state=FakeStateService(), memory=FakeMemoryService())
        for step in (
            _step_first_install,
            _step_unchanged,
            _step_one_file_change,
            _step_added_file,
            _step_removed_file,
            _step_legacy_install,
            _step_purged_embeddings,
        ):
            step(kb)
    with tempfile.TemporaryDirectory(prefix="incremental_kb_smoke_api_") as tmp:
        _step_direct_api(_make_kb(Path(tmp)))


def main() -> int:
    print("incremental knowledge-base re-index smoke (prb_f6964132)")
    print("========================================================")
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
