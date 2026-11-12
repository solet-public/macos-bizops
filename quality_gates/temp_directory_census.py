#!/usr/bin/env python3
"""Require a lifecycle disposition for shipped temporary-directory creators.

Test directories (any ``tests`` path component) are excluded; Step7 test support
is adopted separately. Stable keys are path, enclosing function and prefix,
never line numbers. Pass by adopting the work-folder primitive or recording a
reasoned disposition. The code-quality landing battery runs this every landing.
This measures lifecycle coverage only, not the safety of a site's contents.
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast


@dataclass(frozen=True, order=True)
class Site:
    """A location-independent creator identity."""

    path: str
    function: str
    prefix: str


def census(repo_root: Path, detector: Callable[[ast.expr], bool]) -> list[Site]:
    """Measure production creators with the shared shipped-smoke detector."""
    roots = [repo_root / "ananta/src", repo_root / "quality_gates", repo_root / "deployment/scripts", *sorted((repo_root / "plugins").glob("*/src"))]
    sites: list[Site] = []
    for root in roots:
        for path in sorted(root.rglob("*.py")):
            relative = path.relative_to(repo_root)
            if "tests" in relative.parts:
                continue
            _visit(ast.parse(path.read_text(encoding="utf-8")), relative.as_posix(), "", detector, sites)
    return sorted(sites)


def _visit(node: ast.AST, path: str, scope: str, detector: Callable[[ast.expr], bool], sites: list[Site]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        scope = f"{scope}.{node.name}".lstrip(".")
    if isinstance(node, ast.Call) and detector(node):
        prefix = next((ast.unparse(keyword.value) for keyword in node.keywords if keyword.arg == "prefix"), "<default>")
        sites.append(Site(path, scope or "<module>", prefix))
    for child in ast.iter_child_nodes(node):
        _visit(child, path, scope, detector, sites)


def load_dispositions(path: Path) -> dict[Site, str]:
    """Read reviewed dispositions; correction and removal are ordinary source edits."""
    document: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, list):
        raise ValueError("dispositions must be a list")
    entries: dict[Site, str] = {}
    for value in cast(list[object], document):
        if not isinstance(value, dict):
            raise ValueError("disposition must be an object")
        row = cast(dict[str, object], value)
        if set(row) != {"path", "function", "prefix", "reason"} or any(not isinstance(item, str) or not item.strip() for item in row.values()):
            raise ValueError("disposition requires nonempty path, function, prefix and reason")
        site = Site(cast(str, row["path"]), cast(str, row["function"]), cast(str, row["prefix"]))
        if site in entries:
            raise ValueError(f"duplicate disposition: {site}")
        entries[site] = cast(str, row["reason"])
    return entries


def run(argv: Sequence[str] | None = None) -> int:
    """Return 2 for undisposed sites and 70 for an unmeasured census."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--allowlist", type=Path)
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    sys.path.insert(0, str(root / "plugins/seed_factory_plugin/src"))
    try:
        from seed_factory_plugin.shipped_smoke_contract import is_temp_factory  # noqa: PLC0415

        entries = load_dispositions(args.allowlist or root / "quality_gates/temp_directory_dispositions.json")
        sites = census(root, is_temp_factory)
    except (OSError, ValueError, SyntaxError, ImportError) as error:
        print(f"GATE CRASH: temporary-directory census unmeasured: {error}")
        return 70
    missing = sorted(set(sites) - entries.keys())
    for site in missing:
        print(f"UNDISPOSED {site.path} | {site.function} | {site.prefix}: adopt the primitive or add a reasoned disposition")
    for site in sorted(entries.keys() - set(sites)):
        print(f"ADVISORY obsolete disposition: {site}")
    print(f"temporary-directory census: {len(sites)} sites, {len(missing)} undisposed")
    return 2 if missing else 0


if __name__ == "__main__":
    raise SystemExit(run())
