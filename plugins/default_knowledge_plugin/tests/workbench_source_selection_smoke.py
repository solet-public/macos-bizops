#!/usr/bin/env python3
"""Smoke: a manifest's ``content.selection`` block indexes only notes.

prb_2e7713d4 (unt_489e3741): the workbench knowledge base matched about 95,000
files, almost all of them generated trees, so its manifest now names which
top-level directories are read. This smoke is self-contained: it writes its
own fixture manifest, shaped like that workbench manifest, into a temporary
directory beside a scratch tree holding hand-written notes and the generated
trees a live workbench carries, collects the tree with that manifest, and
checks the exact selected set:

- top-level notes, ``archive/<note>.md`` and ``reviews/<review>/<write-up>.md``
  are selected;
- grooming waves, stock Codex homes, review snapshot subtrees (evidence,
  candidate, base, packet), dot directories and deeper archive files are not;
- a new top-level directory nobody listed is skipped with one logged advisory
  and the collection still succeeds (advisory, never a refusal);
- a manifest without the block still walks the whole tree;
- a malformed block fails fast at manifest parse with a ValueError.

It reads nothing from the checkout beyond the plugin's own source, so it runs
the same in a born solet as in the development checkout (prb_0211d1e8).

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:
    .venv/bin/python3 plugins/default_knowledge_plugin/tests/workbench_source_selection_smoke.py
"""

from __future__ import annotations

import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_knowledge_plugin" / "src"))

import yaml  # noqa: E402
from default_knowledge_plugin.kb_indexing import collect_files, parse_manifest  # noqa: E402
from default_knowledge_plugin.source_selection import SourceSelection, parse_source_selection  # noqa: E402

_passed = 0
_failed: list[str] = []

NOTES = {
    "2026-11-07_operator_note.md",
    "README.md",
    "archive/2026-05-01_retired_note.md",
    "reviews/review-a/review.md",
    "reviews/review-b/report.md",
}
GENERATED = {
    "2026-10-03_grooming_wave/lane-1/candidate/docs/a.md",
    "2026-10-03_grooming_wave/lane-1/notes.md",
    "ws4b2_stock_codex_home/sessions/s.md",
    "ws4b2_stock_codex_diag_home/diag.md",
    "reviews/review-a/evidence/run.md",
    "reviews/review-a/candidate/docs/guide.md",
    "reviews/review-b/base/README.md",
    "reviews/review-b/packet/replay-tree/x.md",
    "archive/legacy_scripts/README.md",
    ".doc_history/README.md/20261107T000000Z.md",
    ".archive/old.md",
    "generated/out.md",
    "brand_new_directory/draft.md",
}
NOT_MARKDOWN = {"2026-11-07_capture.json", "reviews/review-a/notes.txt"}

# The fixture manifest: the same keys and the same content.selection shape the
# live workbench manifest carries, with the exclude globs this tree exercises.
FIXTURE_MANIFEST_YAML = """\
name: workbench
description: Fixture knowledge base for the content.selection smoke.
category: live
default_knowledge_layer: 4
archive_subdir: archive
content:
  patterns:
    include:
      - "*.md"
      - "**/*.md"
    exclude:
      - "**/.git/**"
      - "**/__pycache__/**"
      - "**/.doc_history/**"
      - "**/.archive/**"
  selection:
    include_directories:
      archive: 1
      reviews: 2
    exclude_directories:
      - "20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]*"
      - ".*"
      - "__pycache__"
      - "ws4b2_stock_codex_*"
      - "generated"
      - "tools"
  chunking:
    strategy: title_block
    max_chars: 3000
"""
FIXTURE_EXCLUDED = ("20[0-9][0-9]-[0-9][0-9]-[0-9][0-9]*", ".*", "__pycache__", "ws4b2_stock_codex_*", "generated", "tools")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _build_tree(root: Path) -> None:
    for relative in NOTES | GENERATED | NOT_MARKDOWN:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# {relative}\n", encoding="utf-8")


def _fixture_manifest(manifest_path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return dict(raw)  # pyright: ignore[reportUnknownArgumentType]


def _selected(root: Path, raw: dict[str, Any]) -> set[str]:
    return {str(path.relative_to(root)) for path in collect_files(root, parse_manifest(raw))}


def test_fixture_manifest_selects_only_notes(root: Path, manifest_path: Path) -> None:
    capture = _Capture()
    selection_logger = logging.getLogger("default_knowledge_plugin.source_selection")
    selection_logger.addHandler(capture)
    try:
        selected = _selected(root, _fixture_manifest(manifest_path))
    finally:
        selection_logger.removeHandler(capture)
    _check(selected == NOTES, f"fixture manifest selects exactly the notes (got {sorted(selected)})")
    _check(not selected & GENERATED, "no generated tree, snapshot subtree or dot directory file is selected")
    _check(len(capture.messages) == 1, f"exactly one advisory is logged (got {len(capture.messages)})")
    advisory = capture.messages[0] if capture.messages else ""
    _check("brand_new_directory" in advisory, "the advisory names the unlisted top-level directory")
    _check("ws4b2_stock_codex_home" not in advisory and "2026-10-03" not in advisory, "known excluded directories are not in the advisory")


def test_without_selection_walks_whole_tree(root: Path, manifest_path: Path) -> None:
    raw = _fixture_manifest(manifest_path)
    content = dict(raw["content"])
    del content["selection"]
    raw["content"] = content
    selected = _selected(root, raw)
    _check("2026-10-03_grooming_wave/lane-1/notes.md" in selected, "no selection block: nested generated files are still collected")
    _check(NOTES <= selected, "no selection block: the notes are collected too")


def test_depth_limits(root: Path, manifest_path: Path) -> None:
    raw = _fixture_manifest(manifest_path)
    content = dict(raw["content"])
    content["selection"] = {"include_directories": {"reviews": 3}}
    raw["content"] = content
    selected = _selected(root, raw)
    _check("reviews/review-a/evidence/run.md" in selected, "depth 3 reaches reviews/<review>/<dir>/<file>")
    _check("reviews/review-b/packet/replay-tree/x.md" not in selected, "depth 3 does not reach a fourth level")
    _check("archive/2026-05-01_retired_note.md" not in selected, "a directory missing from include_directories is not read")


def test_selection_api_directly(root: Path, manifest_path: Path) -> None:
    selection_block = _fixture_manifest(manifest_path)["content"]["selection"]
    selection = parse_source_selection(selection_block, "workbench")
    _check(selection == SourceSelection({"archive": 1, "reviews": 2}, FIXTURE_EXCLUDED), "parse_source_selection returns the manifest's SourceSelection")
    _check(selection.include_directories == {"archive": 1, "reviews": 2}, "fixture manifest reads archive/ to depth 1 and reviews/ to depth 2")
    _check(selection.is_known_excluded("2026-10-03_grooming_wave"), "is_known_excluded matches a dated working directory")
    _check(selection.is_known_excluded("ws4b2_stock_codex_home"), "is_known_excluded matches a stock Codex home")
    _check(not selection.is_known_excluded("brand_new_directory"), "is_known_excluded does not match an unlisted directory")
    found = {str(path.relative_to(root)) for path in selection.matching_files(root, ["*.md"], "workbench")}
    _check(found == NOTES, "matching_files on its own yields exactly the notes")


def _parse_fails(selection: object, needle: str, label: str) -> None:
    try:
        parse_manifest({"name": "wb", "content": {"selection": selection}})
    except ValueError as exc:
        _check(needle in str(exc), f"{label} (message: {exc})")
        return
    _check(False, f"{label}: no ValueError raised")


def test_malformed_blocks_fail_fast() -> None:
    _parse_fails(["reviews"], "must be a mapping", "selection that is not a mapping is refused")
    _parse_fails({"include_dirs": {"reviews": 2}}, "unknown keys", "an unknown selection key is refused")
    _parse_fails({"include_directories": {"reviews": 0}}, "positive integer depth", "depth 0 is refused")
    _parse_fails({"include_directories": {"reviews": True}}, "positive integer depth", "a boolean depth is refused")
    _parse_fails({"include_directories": {"reviews/a": 1}}, "one top-level directory name", "a nested include name is refused")
    _parse_fails({"exclude_directories": "generated"}, "must be a list", "an exclude block that is not a list is refused")
    _parse_fails({"exclude_directories": ["a/b"]}, "directory name glob", "a nested exclude glob is refused")
    _parse_fails(
        {"include_directories": {"reviews": 2}, "exclude_directories": ["rev*"]},
        "matches them in exclude_directories",
        "a directory both included and excluded is refused",
    )


def main() -> int:
    print("knowledge base content.selection smoke")
    print("======================================")
    with tempfile.TemporaryDirectory(prefix="kb_selection_") as tmp:
        root = Path(tmp) / "kb"
        root.mkdir()
        _build_tree(root)
        # The manifest sits beside the scratch tree, not inside it, so it is
        # never one of the collected candidates.
        manifest_path = Path(tmp) / "manifest.yaml"
        manifest_path.write_text(FIXTURE_MANIFEST_YAML, encoding="utf-8")
        test_fixture_manifest_selects_only_notes(root, manifest_path)
        test_without_selection_walks_whole_tree(root, manifest_path)
        test_depth_limits(root, manifest_path)
        test_selection_api_directly(root, manifest_path)
    test_malformed_blocks_fail_fast()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    if _failed:
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
