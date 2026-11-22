#!/usr/bin/env python3
"""Classify retained homunculus lines with a reasoned source gate.

The code-quality landing battery runs this every landing. Production Python
under ananta/src and plugins/*/src is measured, excluding tests directories.
Correct or clear dispositions through reviewed source edits; lower counts and
obsolete entries advise, while new files and increased counts block.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path
from typing import cast

_CLASSES = frozenset({"wire_protocol", "cloud_resource_name", "persisted_schema", "legacy_detection", "cited_path"})


def _entry(value: object) -> tuple[str, int]:
    if not isinstance(value, dict):
        raise ValueError("entry requires an object")
    row = cast(dict[str, object], value)
    if set(row) != {"path", "class", "count", "reason"}:
        raise ValueError("entry requires exactly path, class, count and reason")
    path, classification, count, reason = (row[key] for key in ("path", "class", "count", "reason"))
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path requires a nonempty string")
    _validate_description(classification, reason)
    if type(count) is not int or count < 1:
        raise ValueError("count requires an integer >= 1")
    return path, count


def _validate_description(classification: object, reason: object) -> None:
    if not isinstance(classification, str) or classification not in _CLASSES:
        raise ValueError(f"unknown class: {classification!r}")
    if not isinstance(reason, str) or not reason.strip() or reason.splitlines() != [reason]:
        raise ValueError("reason requires a nonempty one-line string")


def load_dispositions(path: Path) -> dict[str, int]:
    """Read classified counts, rejecting malformed or duplicate entries."""
    document: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("dispositions require an object")
    body = cast(dict[str, object], document)
    if set(body) != {"policy", "entries"} or not isinstance(body["policy"], str) or not isinstance(body["entries"], list):
        raise ValueError("dispositions require exactly a policy string and entries list")
    entries: dict[str, int] = {}
    for value in cast(list[object], body["entries"]):
        name, count = _entry(value)
        if name in entries:
            raise ValueError(f"duplicate path: {name}")
        entries[name] = count
    return entries


def _raise_walk_error(error: OSError) -> None:
    raise error


def census(repo_root: Path) -> dict[str, int]:
    """Count matching lines, including repeated tokens on a line only once."""
    roots = [repo_root / "ananta/src", *sorted((repo_root / "plugins").glob("*/src"))]
    measured: dict[str, int] = {}
    for root in roots:
        for directory, children, names in os.walk(root, onerror=_raise_walk_error):
            children[:] = sorted(name for name in children if name != "tests")
            for name in sorted(names):
                path = Path(directory) / name
                if path.suffix != ".py" or "tests" in path.relative_to(repo_root).parts:
                    continue
                count = sum("homunculus" in line.lower() for line in path.read_text(encoding="utf-8").splitlines())
                if count:
                    measured[path.relative_to(repo_root).as_posix()] = count
    return measured


def _report(entries: dict[str, int], measured: dict[str, int]) -> tuple[int, int]:
    unclassified = increased = 0
    for path, count in sorted(measured.items()):
        if path not in entries:
            print(f"UNCLASSIFIED {path} {count}")
            unclassified += 1
        elif count > entries[path]:
            print(f"INCREASED {path} recorded {entries[path]} measured {count}")
            increased += 1
        elif count < entries[path]:
            print(f"ADVISORY stale {path} recorded {entries[path]} measured {count}")
    for path in sorted(entries.keys() - measured.keys()):
        print(f"ADVISORY obsolete entry {path}")
    return unclassified, increased


def run(argv: Sequence[str] | None = None) -> int:
    """Return 2 for new/increased counts, 70 for unmeasured input, else 0."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--allowlist", type=Path)
    args = parser.parse_args(argv)
    measured: dict[str, int] = {}
    unclassified = increased = 0
    try:
        root = Path(args.repo_root).resolve()
        entries = load_dispositions(args.allowlist or root / "quality_gates/homunculus_term_dispositions.json")
        measured = census(root)
        unclassified, increased = _report(entries, measured)
        result = 2 if unclassified or increased else 0
    except (OSError, UnicodeDecodeError, ValueError) as error:
        print(f"GATE CRASH: homunculus-term census unmeasured: {error}")
        result = 70
    print(f"homunculus-term census: {len(measured)} files, {sum(measured.values())} lines, {unclassified} unclassified, {increased} increased")
    return result


if __name__ == "__main__":
    raise SystemExit(run())
