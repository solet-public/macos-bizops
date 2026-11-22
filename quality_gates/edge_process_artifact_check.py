#!/usr/bin/env python3
"""Author-side check: name the missing artifact of every EDGE process (prb_a565496b).

An EDGE process is three artifacts that three different gates check in three
different places: the EDGE ``@platform_process`` method (``decorator``), its
entry in ``get_edge_process_definitions()`` (``declaration``) and the
knowledge-base process JSON (``kb_json``). A missing declaration is otherwise
found only when the landing gate goes red on a registration error. This check
measures all three at once, prints one ``MISSING`` block per incomplete process
and exits 1; it is run by the author before delivery.

It reports and exits. Whatever it cannot measure is an ``ADVISORY`` line and
never changes the exit code (rul_becb4bed). It opens no database and no network
connection, writes no file, and imports nothing from the whole-tree integration
gate: it does for itself what that gate does to find each plugin class.

Usage: ``edge_process_artifact_check.py [--repo-root DIR] [--allowlist FILE] [PATH ...]``.
A PATH under ``plugins/<X>/`` selects plugin directory X; no PATH selects every
plugin; any other PATH selects nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import sys
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

_SELF_ROOT = Path(__file__).resolve().parents[1]
if str(_SELF_ROOT) not in sys.path:
    sys.path.insert(0, str(_SELF_ROOT))

from quality_gates.allowlist_schema import load_allowlist  # noqa: E402

ENTRY_POINT_GROUP: Final[str] = "ananta.plugins"
DEFAULT_ALLOWLIST_REL: Final[str] = "quality_gates/whole_tree_integration_gate_allowlist.txt"
EXIT_OK: Final[int] = 0
EXIT_INCOMPLETE: Final[int] = 1


class _UnmeasuredError(Exception):
    """One provider cannot be measured; the reason becomes an ADVISORY line."""


@dataclass(frozen=True)
class Measured:
    """What one provider class holds of each EDGE process, read from disk and from the class."""

    provider: str
    source_rel: str
    kb_dir_rel: str
    decorated: frozenset[str]
    declared: frozenset[str]
    kb_present: frozenset[str]

    @property
    def names(self) -> list[str]:
        """Every EDGE process name this provider decorates or declares, sorted."""
        return sorted(self.decorated | self.declared)


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Name the missing artifact of every EDGE process.")
    parser.add_argument("--repo-root", type=Path, default=_SELF_ROOT, help="checkout to measure (default: this script's repository)")
    parser.add_argument("--allowlist", type=Path, default=None, help=f"tolerated C2.2 keys (default: <root>/{DEFAULT_ALLOWLIST_REL})")
    parser.add_argument("paths", nargs="*", help="files you touched; a path under plugins/<X>/ selects plugin X (default: every plugin)")
    return parser.parse_args(list(argv))


def _selected_plugins(root: Path, paths: Sequence[str]) -> list[str]:
    """Plugin directory names selected by PATH arguments, or every plugin when there are none."""
    if not paths:
        return sorted(manifest.parent.name for manifest in root.glob("plugins/*/pyproject.toml"))
    selected: set[str] = set()
    for raw in paths:
        candidate = Path(raw)
        absolute = candidate if candidate.is_absolute() else root / candidate
        try:
            parts = absolute.resolve().relative_to(root).parts
        except ValueError:
            continue
        if len(parts) >= 2 and parts[0] == "plugins" and (root / "plugins" / parts[1]).is_dir():
            selected.add(parts[1])
    return sorted(selected)


def _entry_points(root: Path, plugin: str) -> list[tuple[str, str]]:
    """``(provider, "module:attribute")`` for each ``ananta.plugins`` entry point of one plugin directory."""
    manifest = root / "plugins" / plugin / "pyproject.toml"
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        group = data.get("project", {}).get("entry-points", {}).get(ENTRY_POINT_GROUP, {})
        return [(str(provider), str(target)) for provider, target in group.items()]
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError, AttributeError) as exc:
        raise _UnmeasuredError(f"pyproject.toml cannot be read: {type(exc).__name__}: {exc}") from exc


def _load_class(root: Path, target: str) -> type[object]:
    """The completed plugin class behind one entry point, imported from the tree under test."""
    from ananta.core.plugins.plugin_base import PluginBase

    module_name, _, attribute = target.partition(":")
    try:
        loaded: object = getattr(importlib.import_module(module_name), attribute)
    except (Exception, SystemExit) as exc:
        raise _UnmeasuredError(f"{target} did not import: {type(exc).__name__}: {exc}") from exc
    if not isinstance(loaded, type) or not issubclass(loaded, PluginBase):
        raise _UnmeasuredError(f"{target} is not a PluginBase class, so runtime discovery skips it")
    if not Path(inspect.getfile(loaded)).resolve().is_relative_to(root):
        raise _UnmeasuredError(f"{target} imported from {inspect.getfile(loaded)}, outside the tree under test")
    if inspect.getattr_static(loaded, "get_available_actions") is not inspect.getattr_static(PluginBase, "get_available_actions"):
        raise _UnmeasuredError(f"{target} overrides get_available_actions, so its runtime action set is dynamic")
    return loaded


def _declared_names(plugin_class: type[object]) -> frozenset[str]:
    """Keys of ``get_edge_process_definitions()`` when the class is an EdgeProcessProvider, else none."""
    from ananta.interfaces.edge_process_provider import EdgeProcessProvider

    if not issubclass(plugin_class, EdgeProcessProvider):
        return frozenset()
    construct = cast("Callable[[], EdgeProcessProvider]", plugin_class)
    try:
        return frozenset(construct().get_edge_process_definitions())
    except (Exception, SystemExit) as exc:
        raise _UnmeasuredError(f"constructing it or calling get_edge_process_definitions() raised {type(exc).__name__}: {exc}") from exc


def _measure_class(root: Path, provider: str, plugin_class: type[object]) -> Measured:
    """Measure one provider class: decorated names, declared names and the JSON files on disk."""
    from ananta.core.domain.enums import ProcessorPolicyCategory
    from ananta.core.process_registry.registration_census import class_registration_census

    actions = class_registration_census(plugin_class, provider).actions
    decorated = frozenset(action.name for action in actions if action.processor_policy_category is ProcessorPolicyCategory.EDGE)
    declared = _declared_names(plugin_class)
    source = Path(inspect.getfile(plugin_class)).resolve()
    kb_dir = source.parents[2] / "knowledge_base" / "processes"
    present = frozenset(name for name in decorated | declared if (kb_dir / f"{name}.json").is_file())
    return Measured(provider, _shown(root, source), _shown(root, kb_dir), decorated, declared, present)


def _shown(root: Path, path: Path) -> str:
    """``path`` relative to the root when it lies under it, else as given."""
    return path.relative_to(root).as_posix() if path.is_relative_to(root) else path.as_posix()


def _measure_plugin(root: Path, plugin: str) -> tuple[list[Measured], list[str]]:
    """Measure every provider of one plugin directory; one ADVISORY line per provider that cannot be measured."""
    try:
        entry_points = _entry_points(root, plugin)
    except _UnmeasuredError as exc:
        return [], [f"ADVISORY {plugin}: {exc}"]
    measured: list[Measured] = []
    advisories: list[str] = []
    for provider, target in entry_points:
        try:
            measured.append(_measure_class(root, provider, _load_class(root, target)))
        except _UnmeasuredError as exc:
            advisories.append(f"ADVISORY {provider}: {exc}")
    return measured, advisories


def _measure(root: Path, plugins: Sequence[str]) -> tuple[list[Measured], list[str]]:
    """Measure every selected plugin with the tree's own import roots first on ``sys.path``."""
    sys.path[:0] = [str(path) for path in (root / "ananta" / "src", *sorted(root.glob("plugins/*/src")))]
    measured: list[Measured] = []
    advisories: list[str] = []
    for plugin in plugins:
        found, lost = _measure_plugin(root, plugin)
        measured.extend(found)
        advisories.extend(lost)
    return measured, advisories


def _load_tolerated(path: Path) -> tuple[frozenset[str], list[str]]:
    """Active allowlist keys, or none plus one ADVISORY line when the file cannot be parsed."""
    if not path.is_file():
        return frozenset(), []
    try:
        return load_allowlist(path), []
    except (OSError, ValueError) as exc:
        return frozenset(), [f"ADVISORY allowlist: {type(exc).__name__}: {exc}"]


def _missing_artifacts(found: Measured, name: str, tolerated: frozenset[str]) -> tuple[list[str], bool]:
    """``(artifacts missing in the order decorator, declaration, kb_json, kb_json was tolerated)``."""
    missing: list[str] = []
    if name not in found.decorated:
        missing.append("decorator")
    if name not in found.declared:
        missing.append("declaration")
    if name in found.kb_present:
        return missing, False
    if f"C2.2::{found.provider}::{name}" in tolerated:
        return missing, True
    return [*missing, "kb_json"], False


def _fix_line(found: Measured, name: str, artifact: str) -> str:
    """The indented line that names the file to change for one missing artifact."""
    if artifact == "decorator":
        return f'  decorator: add the EDGE @platform_process method "{name}" in {found.source_rel}'
    if artifact == "declaration":
        return f'  declaration: add "{name}" to get_edge_process_definitions() in {found.source_rel}'
    return f"  kb_json: add {found.kb_dir_rel}/{name}.json"


def _classify(found: Measured, tolerated: frozenset[str]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """The MISSING blocks (header, fix lines) and TOLERATED lines of one provider."""
    blocks: list[tuple[str, list[str]]] = []
    tolerated_lines: list[str] = []
    for name in found.names:
        missing, was_tolerated = _missing_artifacts(found, name, tolerated)
        key = f"plugin::{found.provider}::{name}"
        if missing:
            blocks.append((f"MISSING {key}: {', '.join(missing)}", [_fix_line(found, name, artifact) for artifact in missing]))
        elif was_tolerated:
            tolerated_lines.append(f"TOLERATED {key}: kb_json (allowlisted C2.2)")
    return blocks, tolerated_lines


def _report(measured: Sequence[Measured], advisories: Sequence[str], tolerated: frozenset[str]) -> tuple[list[str], int]:
    """The output lines, summary last, and the number of incomplete processes."""
    blocks: list[tuple[str, list[str]]] = []
    tolerated_lines: list[str] = []
    for found in measured:
        found_blocks, found_tolerated = _classify(found, tolerated)
        blocks.extend(found_blocks)
        tolerated_lines.extend(found_tolerated)
    lines = [line for header, fixes in sorted(blocks) for line in (header, *fixes)]
    lines += sorted(tolerated_lines) + sorted(advisories)
    processes = sum(len(found.names) for found in measured)
    plugins = sum(1 for found in measured if found.names)
    lines.append(f"EDGE ARTIFACTS: {processes} processes in {plugins} plugins measured, {len(blocks)} incomplete, {len(tolerated_lines)} tolerated, {len(advisories)} advisories")
    return lines, len(blocks)


def run(argv: Sequence[str]) -> int:
    """Select, measure, report; exit 1 only when a MISSING block was printed."""
    args = _parse_args(argv)
    root: Path = args.repo_root.resolve()
    allowlist_path: Path = args.allowlist if args.allowlist is not None else root / DEFAULT_ALLOWLIST_REL
    tolerated, allowlist_advisories = _load_tolerated(allowlist_path)
    with contextlib.redirect_stdout(sys.stderr):
        measured, advisories = _measure(root, _selected_plugins(root, args.paths))
    lines, incomplete = _report(measured, [*allowlist_advisories, *advisories], tolerated)
    print("\n".join(lines))
    return EXIT_INCOMPLETE if incomplete else EXIT_OK


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
