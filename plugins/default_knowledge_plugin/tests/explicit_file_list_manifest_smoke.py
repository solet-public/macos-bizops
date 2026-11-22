#!/usr/bin/env python3
"""Smoke: an explicit file list indexes only what the manifest includes.

chg_d096d684 (unt_83e24fe4): the principle publish path (``create_file`` /
``edit_file`` -> ``reindex_file`` -> ``index_files`` with a one-file list)
bypassed the manifest's include and exclude patterns, which only the bulk walk
(``collect_files``) applied. A principle's ``*.retrieval_test.yaml`` companion,
which holds the quiz questions verbatim, was therefore embedded and outranked
the article it tests. ``index_files`` is the one seam every explicit-list caller
passes through, so it now applies the same per-file rule the bulk walk does.

Drives the real ``index_files`` and ``kb_file_ops`` verbs against a temporary
knowledge base directory with in-memory memory and state fakes at the service
boundary (the same fake-source / real-logic split ``file_ops_posture_smoke``
uses). Checks:

- a companion in an explicit list is not indexed, an included markdown file is;
- ``create_file`` of a companion writes the file and indexes nothing, and
  ``edit_file`` of a companion removes chunks an earlier build had stored;
- an explicit list of every file in a tree indexes exactly the files the bulk
  walk indexes, for the platform manifest and the thinking_plans manifest
  (so a thinking plan written to ``plans/<date>/<id>.md`` is still indexed);
- ``content.selection`` is a scope for the bulk walk only: an explicit write to
  a path selection would skip (``archive_file`` re-keys ``archive/<nested>``) is
  still indexed.

Project policy: no pytest. Exits 0 on success, 1 on failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/explicit_file_list_manifest_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

import default_knowledge_plugin.kb_file_ops as fo  # noqa: E402
from default_knowledge_plugin.constants import document_tag, kb_id_tag  # noqa: E402
from default_knowledge_plugin.kb_indexing import index_files, now_iso, resolve_manifest  # noqa: E402


class _FakeMemory:
    """In-memory chunk store exercising the owning-service verbs the indexer uses."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self._n = 0

    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        self._n += 1
        memory_id = f"mem-{self._n}"
        self.store[memory_id] = {"id": memory_id, "content": content, "tags": list(tags)}
        return {"memory_id": memory_id}

    def get_memories_by_tag(self, tag: str, include_archived: bool = False) -> dict[str, Any]:
        del include_archived
        return {"memories": [m for m in self.store.values() if tag in m["tags"]]}

    def delete_memories_by_ids(self, ids: list[str]) -> None:
        for memory_id in ids:
            self.store.pop(str(memory_id), None)

    def indexed_documents(self, relatives: list[str]) -> set[str]:
        """The files among ``relatives`` that own at least one stored chunk."""
        return {relative for relative in relatives if any(document_tag(relative) in m["tags"] for m in self.store.values())}


class _FakeTxn:
    def __init__(self, record: dict[str, Any]) -> None:
        self._record = record

    def __enter__(self) -> _FakeTxn:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> None:
        del namespace, query
        self._record.update(updates)


class _FakeState:
    def __init__(self, record: dict[str, Any]) -> None:
        self.record = record

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        name = query.get("filters", {}).get("name")
        if name is not None and self.record.get("name") != name:
            return {"data": {"records": []}}
        return {"data": {"records": [self.record]}}

    def transactional(self) -> _FakeTxn:
        return _FakeTxn(self.record)


# The shipped ananta_platform manifest's patterns.
PLATFORM_MANIFEST = """name: {name}
content:
  patterns:
    include:
      - "**/*.md"
      - "**/*.txt"
    exclude:
      - "**/.git/**"
      - "**/.archive/**"
      - "**/scripts/**"
  chunking:
    strategy: headers
    max_chars: 3000
    overlap_chars: 200
"""

# The shipped thinking_plans manifest's patterns: plans are written to plans/<date>/<id>.md.
PLANS_MANIFEST = """name: {name}
content:
  patterns:
    include:
      - "plans/**/*.md"
      - "manifests/**/*.md"
      - "wbs/**/*.md"
    exclude:
      - "**/.git/**"
      - "**/.archive/**"
      - "processes/**"
  chunking:
    strategy: headers
    max_chars: 3000
    overlap_chars: 200
"""

# The workbench manifest's shape: a content.selection scope plus an archive subdirectory.
SELECTION_MANIFEST = """name: {name}
archive_subdir: archive
content:
  patterns:
    include:
      - "*.md"
      - "**/*.md"
    exclude:
      - "**/.git/**"
      - "**/.doc_history/**"
      - "**/.archive/**"
  selection:
    include_directories:
      archive: 1
    exclude_directories:
      - ".*"
  chunking:
    strategy: headers
    max_chars: 3000
    overlap_chars: 200
"""

ARTICLE = "# Separation of concerns\n\nEach module has one job and depends only on contracts.\n\n## Why\n\nAny module can be replaced without changing the rest.\n"
COMPANION = "ruling_id: rul_00000000\nquestions:\n  - id: q1\n    text: A setup plugin also starts background services. Should it be split?\n    situational: true\n"
NOTE = "# Note\n\nSome prose.\n"

TREE: dict[str, str] = {
    "principles/rul_a.md": ARTICLE,
    "principles/rul_a.retrieval_test.yaml": COMPANION,
    "principles/rul_b_retrieval_test.yaml": COMPANION,
    "01_overview/intro.md": NOTE,
    "01_overview/notes.txt": "plain text note\n",
    "01_overview/data.json": '{"a": 1}\n',
    "scripts/tool.md": NOTE,
    ".archive/old.md": NOTE,
    "processes/p.md": NOTE,
    ".doc_history/intro.md/20261107T000000Z.md": NOTE,
    "plans/2026-11-12/plan-1.md": NOTE,
    "wbs/w.md": NOTE,
    "manifests/m.md": NOTE,
    "other/o.md": NOTE,
}


def _make_kb(root: Path, name: str, manifest_yaml: str) -> tuple[Path, _FakeState, _FakeMemory]:
    """Write a manifest and the TREE under ``root`` and return the directory with fresh fakes."""
    kb_dir = root / name
    kb_dir.mkdir()
    (kb_dir / "manifest.yaml").write_text(manifest_yaml.format(name=name), encoding="utf-8")
    for relative, content in TREE.items():
        path = kb_dir / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    record: dict[str, Any] = {
        "name": name,
        "resolved_path": str(kb_dir.resolve()),
        "source_type": "symlink",
        "memory_ids": [],
        "chunk_count": 0,
        "is_active": 1,
        "indexed_at": now_iso(),
    }
    return kb_dir, _FakeState(record), _FakeMemory()


def _all_files(kb_dir: Path) -> list[Path]:
    return sorted(path for path in kb_dir.rglob("*") if path.is_file())


class ExplicitFileListHonoursManifest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="explicit_list_smoke_")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_companion_in_explicit_list_is_not_indexed_but_article_is(self) -> None:
        kb_dir, _state, memory = _make_kb(self.root, "platform", PLATFORM_MANIFEST)
        manifest = resolve_manifest(kb_dir, "platform", None)
        article = kb_dir / "principles/rul_a.md"
        companion = kb_dir / "principles/rul_a.retrieval_test.yaml"

        memory_ids, chunk_count = index_files(kb_dir, "platform", manifest, memory, [article, companion], budget=None)

        documents = memory.indexed_documents(["principles/rul_a.md", "principles/rul_a.retrieval_test.yaml"])
        self.assertIn("principles/rul_a.md", documents, "an included markdown file in an explicit list is indexed")
        self.assertNotIn("principles/rul_a.retrieval_test.yaml", documents, "a companion the manifest does not include must not be embedded")
        self.assertGreater(chunk_count, 0)
        self.assertEqual(len(memory_ids), len(memory.store), "every stored chunk is accounted for in the returned ids")

    def test_create_file_of_a_companion_writes_it_and_indexes_nothing(self) -> None:
        kb_dir, state, memory = _make_kb(self.root, "platform", PLATFORM_MANIFEST)
        article_path = "principles/rul_new.md"
        companion_path = "principles/rul_new.retrieval_test.yaml"

        fo.create_file_kb("platform", article_path, ARTICLE, state, memory, budget=None)
        fo.create_file_kb("platform", companion_path, COMPANION, state, memory, budget=None)

        self.assertEqual((kb_dir / companion_path).read_text(encoding="utf-8"), COMPANION, "the companion is still written to disk")
        self.assertEqual(memory.indexed_documents([article_path, companion_path]), {article_path}, "only the article is indexed")
        self.assertEqual(state.record["chunk_count"], len(memory.store), "the install record counts exactly the stored chunks")
        self.assertEqual(sorted(state.record["memory_ids"]), sorted(memory.store), "the install record lists exactly the stored chunks")

    def test_edit_file_of_a_companion_removes_chunks_an_earlier_build_stored(self) -> None:
        kb_dir, state, memory = _make_kb(self.root, "platform", PLATFORM_MANIFEST)
        companion_path = "principles/rul_a.retrieval_test.yaml"
        stale = memory.remember("stale companion chunk", [kb_id_tag("platform"), document_tag(companion_path)])["memory_id"]
        state.record["memory_ids"] = [stale]
        state.record["chunk_count"] = 1

        fo.edit_file_kb("platform", companion_path, COMPANION + "# revised\n", state, memory, budget=None)

        self.assertEqual(memory.indexed_documents([companion_path]), set(), "a rewritten companion leaves no chunk behind")
        self.assertEqual(state.record["memory_ids"], [], "the install record no longer lists the stale chunk")
        self.assertTrue((kb_dir / companion_path).read_text(encoding="utf-8").endswith("# revised\n"))

    def test_explicit_list_of_every_file_indexes_exactly_what_the_bulk_walk_indexes(self) -> None:
        for manifest_yaml, name in ((PLATFORM_MANIFEST, "platform"), (PLANS_MANIFEST, "plans")):
            with self.subTest(manifest=name):
                kb_dir, _state, bulk_memory = _make_kb(self.root, name, manifest_yaml)
                manifest = resolve_manifest(kb_dir, name, None)
                relatives = [str(path.relative_to(kb_dir)) for path in _all_files(kb_dir)]
                explicit_memory = _FakeMemory()

                index_files(kb_dir, name, manifest, bulk_memory, budget=None)
                index_files(kb_dir, name, manifest, explicit_memory, _all_files(kb_dir), budget=None)

                bulk = bulk_memory.indexed_documents(relatives)
                explicit = explicit_memory.indexed_documents(relatives)
                self.assertTrue(bulk, "the bulk walk indexes something")
                self.assertEqual(explicit, bulk, "an explicit list must index exactly the files the bulk walk indexes")

    def test_a_thinking_plan_written_to_plans_date_is_still_indexed(self) -> None:
        kb_dir, state, memory = _make_kb(self.root, "plans", PLANS_MANIFEST)
        plan_path = "plans/2026-11-13/plan-2.md"

        fo.create_file_kb("plans", plan_path, NOTE, state, memory, budget=None)

        self.assertTrue((kb_dir / plan_path).is_file())
        self.assertEqual(memory.indexed_documents([plan_path]), {plan_path}, "the thinking_plans write path keeps indexing")

    def test_selection_scopes_the_bulk_walk_only(self) -> None:
        kb_dir, state, memory = _make_kb(self.root, "bench", SELECTION_MANIFEST)
        nested = "archive/sub/doc.md"
        (kb_dir / "archive/sub").mkdir(parents=True)
        (kb_dir / nested).write_text(NOTE, encoding="utf-8")

        fo.reindex_file(state.record, kb_dir, nested, state, memory, budget=None)

        self.assertEqual(memory.indexed_documents([nested]), {nested}, "an archived nested note stays indexed after archive_file re-keys it")


if __name__ == "__main__":
    unittest.main(verbosity=2)
