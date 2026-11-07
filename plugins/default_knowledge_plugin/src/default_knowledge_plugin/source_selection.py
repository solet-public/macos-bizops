"""Top-level directory selection for a knowledge base source tree.

A manifest's ``content.patterns`` include and exclude globs decide which file
names match, but they cannot say "only these top-level directories":
``collect_files`` adds ``rglob`` for every include pattern, so an include always
recurses, and an exclude is tested with ``Path.match``, which matches from the
right end of a path and so cannot anchor on the first directory. A manifest's
optional ``content.selection`` block adds that rule:

- Files directly in the knowledge base root are candidates.
- Each ``include_directories`` entry names one top-level directory and the
  deepest file depth below it that is a candidate. ``reviews: 2`` selects
  ``reviews/<review>/<file>`` and nothing deeper.
- A top-level directory whose name matches an ``exclude_directories`` glob is
  skipped without a message.
- Any other top-level directory is skipped and named in one logged advisory per
  collection, so a new directory is never indexed by accident and never stops
  an install.

Only the root and the included directories are walked, so a large generated
tree costs nothing to skip. Candidates still pass through the include and
exclude patterns, with the include test reproducing the ``glob`` plus ``rglob``
semantics the whole-tree walk uses. A manifest without the block keeps that
whole-tree walk unchanged.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path, PurePath
from typing import Any

logger = logging.getLogger(__name__)

_SELECTION_KEYS = frozenset({"include_directories", "exclude_directories"})


@dataclass(frozen=True, slots=True)
class SourceSelection:
    """Parsed ``content.selection`` block: which top-level directories are read.

    ``include_directories`` maps a top-level directory name to the deepest
    file depth below it that is a candidate (1 means its direct files only).
    ``exclude_directories`` holds name globs for top-level directories that are
    known and deliberately not read.
    """

    include_directories: dict[str, int]
    exclude_directories: tuple[str, ...]

    def is_known_excluded(self, name: str) -> bool:
        """True when ``name`` matches one of the ``exclude_directories`` globs."""
        return any(fnmatchcase(name, pattern) for pattern in self.exclude_directories)

    def matching_files(self, kb_dir: Path, include_patterns: list[str], kb_name: str) -> set[Path]:
        """Return the selected candidates under ``kb_dir`` that match an include pattern.

        Logs one advisory naming every top-level directory the selection does
        not mention; those directories are skipped, never refused.
        """
        candidates: list[Path] = []
        unknown: list[str] = []
        for entry in sorted(kb_dir.iterdir()):
            if not entry.is_dir():
                candidates.append(entry)
                continue
            max_depth = self.include_directories.get(entry.name)
            if max_depth is not None:
                candidates.extend(_files_within(entry, max_depth))
            elif not self.is_known_excluded(entry.name):
                unknown.append(entry.name)
        if unknown:
            logger.warning(
                "knowledge base %r: top-level directories %s are not named in content.selection, so they are not indexed (advisory). Add each to include_directories or exclude_directories in the manifest.",
                kb_name,
                ", ".join(unknown),
            )
        return {path for path in candidates if _matches_include(path.relative_to(kb_dir), include_patterns)}


def _files_within(root: Path, max_depth: int) -> list[Path]:
    """Return the files under ``root`` at most ``max_depth`` levels down, pruning deeper directories."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        directory = Path(dirpath)
        file_depth = len(directory.relative_to(root).parts) + 1
        found.extend(directory / filename for filename in filenames)
        if file_depth >= max_depth:
            dirnames.clear()
    return found


def _matches_include(relative: PurePath, include_patterns: list[str]) -> bool:
    """Match like ``kb_dir.glob(pattern)`` or ``kb_dir.rglob(pattern)`` would."""
    return any(relative.full_match(pattern) or relative.full_match(f"**/{pattern}") for pattern in include_patterns)


def parse_source_selection(raw: Any, kb_name: str) -> SourceSelection:
    """Parse and validate a manifest's ``content.selection`` block (fast-fail on a malformed block)."""
    if not isinstance(raw, dict):
        raise ValueError(f"manifest {kb_name!r}: content.selection must be a mapping; got {type(raw).__name__}")
    block: dict[str, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    unknown_keys = sorted(set(block) - _SELECTION_KEYS)
    if unknown_keys:
        raise ValueError(f"manifest {kb_name!r}: content.selection has unknown keys {unknown_keys}; expected {sorted(_SELECTION_KEYS)}")
    include = _parse_include_directories(block.get("include_directories") or {}, kb_name)
    exclude = _parse_exclude_directories(block.get("exclude_directories") or [], kb_name)
    selection = SourceSelection(include_directories=include, exclude_directories=exclude)
    contradictory = sorted(name for name in include if selection.is_known_excluded(name))
    if contradictory:
        raise ValueError(f"manifest {kb_name!r}: content.selection names {contradictory} in include_directories and matches them in exclude_directories")
    return selection


def _parse_include_directories(raw: Any, kb_name: str) -> dict[str, int]:
    if not isinstance(raw, dict):
        raise ValueError(f"manifest {kb_name!r}: content.selection.include_directories must map a directory name to a depth")
    entries: dict[Any, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    include: dict[str, int] = {}
    for name, depth in entries.items():
        if not isinstance(name, str) or not name or "/" in name or name in {".", ".."}:
            raise ValueError(f"manifest {kb_name!r}: content.selection.include_directories key {name!r} must be one top-level directory name")
        if isinstance(depth, bool) or not isinstance(depth, int) or depth < 1:
            raise ValueError(f"manifest {kb_name!r}: content.selection.include_directories[{name!r}] must be a positive integer depth; got {depth!r}")
        include[name] = depth
    return include


def _parse_exclude_directories(raw: Any, kb_name: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"manifest {kb_name!r}: content.selection.exclude_directories must be a list of directory name globs")
    patterns: list[Any] = list(raw)  # pyright: ignore[reportUnknownArgumentType]
    for pattern in patterns:
        if not isinstance(pattern, str) or not pattern or "/" in pattern:
            raise ValueError(f"manifest {kb_name!r}: content.selection.exclude_directories entry {pattern!r} must be a non-empty directory name glob")
    return tuple(patterns)
