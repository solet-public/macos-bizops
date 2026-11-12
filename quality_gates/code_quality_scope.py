#!/usr/bin/env python3
"""Touched-surface scoping for the code-quality gate on a landing merge.

``code_quality_check.py`` runs twenty-one classes. Three of them -- ``syntax``,
``radon_cc`` and ``radon_mi`` -- decide each file from that file's own bytes,
so on a landing merge they only need the files the merge touched. Two more --
``god_class`` and ``pyright`` -- also read the modules a file imports, so their
scoped set is the touched files plus the files that import them (see
``ImportIndex``). Every other class walks the tree, so it stays full. This
module derives the touched surface from the merge's first parent, plans every
class, and keeps one CLASS REPORT row per class.

Nothing here refuses. Where scoping cannot be proven -- HEAD is not a merge,
the working tree differs from HEAD, a git call fails or times out, a class's
global input is touched, the import index cannot be built, or the pyright
closure exceeds its cap -- that class runs full with an ADVISORY line. A scoped
set that is empty is ``inherited``: the class does not run and its verdict is
the tip's.

Equivalence premise: an untouched file has the tip's verdict because every
landing is gated. A direct unlanded push to master, or a toolchain change
(venv Python, a radon upgrade, or a Node or pyright upgrade) with no touched
file, voids that premise, and the advisory line says so.
"""

from __future__ import annotations

import ast
import os
import subprocess
import time
import tomllib
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal, TypeVar

ClassMode = Literal["scoped", "full", "inherited"]

# Report order is run order in code_quality_check.main() and its helpers.
CLASS_NAMES: tuple[str, ...] = (
    "pyright",
    "ruff",
    "god_class",
    "radon_cc",
    "radon_mi",
    "whole_tree_integration",
    "flow_probe_registry",
    "service_interface_ast",
    "return_shape",
    "embedding_description_bound",
    "shipped_doc",
    "bundle_license",
    "schema_init",
    "smoke_contract_lint",
    "temp_directory_census",
    "wint2_driver_import",
    "wint2_vault_key_declaration",
    "syntax",
    "debugger_scan",
    "fixture_replay",
    "readable_freshness",
)

SCOPED_CLASSES: tuple[str, ...] = ("syntax", "radon_cc", "radon_mi")
# Scoped by the import graph: the touched files plus the files that import them.
CROSS_FILE_SCOPED_CLASSES: tuple[str, ...] = ("god_class", "pyright")

_NOT_SCOPED_REASON = "tree-walking or cross-file: not scoped in this slice"
_PREMISE_ADVISORY = "ADVISORY code_quality_scope: scoped and inherited verdicts assume the tip passed its own gate; a direct unlanded push to master or a toolchain change (venv Python, a radon upgrade, or a Node or pyright upgrade) voids that premise"
_RADON_SHARED_GLOBALS = (
    "pyproject.toml",
    "quality_gates/gate_scope.py",
    "quality_gates/code_quality_check.py",
    "quality_gates/code_quality_scope.py",
)
# Touching any of these runs the class full, because its verdict is no longer
# a function of the touched files alone. pyright also runs full on any touched
# .pyi or py.typed file (see _tripped_globals).
GLOBAL_INPUTS: dict[str, tuple[str, ...]] = {
    "syntax": (),
    "radon_cc": (*_RADON_SHARED_GLOBALS, "quality_gates/radon_cc_check.py", "quality_gates/radon_cc_allowlist.txt"),
    "radon_mi": (*_RADON_SHARED_GLOBALS, "quality_gates/radon_mi_check.py", "quality_gates/radon_mi_allowlist.txt"),
    "god_class": (
        "pyproject.toml",
        "quality_gates/god_class_check.py",
        "quality_gates/god_class_allowlist.txt",
        "quality_gates/gate_scope.py",
        "quality_gates/code_quality_check.py",
        "quality_gates/code_quality_scope.py",
    ),
    "pyright": (
        "pyproject.toml",
        "deployment/scripts/check_gate_toolchain.sh",
        "quality_gates/code_quality_check.py",
        "quality_gates/code_quality_scope.py",
        "quality_gates/pyright_repo.py",
    ),
}

# Roots whose modules import under their own name: ``ananta/src/ananta/x.py``
# imports as ``ananta.x``. ``plugins/<name>/src`` follows the same rule.
_SOURCE_ROOTS: tuple[str, ...] = ("ananta/src", "solet_cli/src", "solet_setup_contracts/src", "initialization/src")
# Top-level quality_gates modules import each other by bare name.
_BARE_NAME_DIR = PurePosixPath("quality_gates")
# A scoped pyright run above this share of the pyright surface runs full. The
# scoped cost is roughly proportional to the files checked, but pyright still
# parses and binds the downward import closure of every listed file, so the
# saving per file is less than linear (read, not run; the CLASS REPORT pyright
# seconds of the first scoped landings confirm or refute the cap).
_PYRIGHT_CLOSURE_FRACTION = 0.25

_GIT_ROUTING_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
_GIT_TIMEOUT_SECONDS = 30

_T = TypeVar("_T")


@dataclass(frozen=True)
class TouchedSurface:
    """Repo-relative files a landing merge touched; ``base`` is None when unscoped."""

    base: str | None
    paths: tuple[str, ...]
    advisory: str | None


@dataclass(frozen=True)
class ClassPlan:
    """How one class runs. ``paths`` is the scoped set; None means the whole surface (full)."""

    name: str
    mode: ClassMode
    paths: tuple[Path, ...] | None
    reason: str


@dataclass(frozen=True)
class ScopeRow:
    name: str
    mode: ClassMode
    files: int | None
    seconds: float
    reason: str


@dataclass
class ScopeReport:
    """One row per class that ran or was skipped as inherited, keyed by class name."""

    rows: dict[str, ScopeRow] = field(default_factory=dict)

    def record(self, name: str, mode: ClassMode, files: int | None, seconds: float, reason: str) -> None:
        self.rows[name] = ScopeRow(name=name, mode=mode, files=files, seconds=seconds, reason=reason)

    def run(self, name: str, plan: ClassPlan, fn: Callable[..., _T], *args: object) -> _T | None:
        """Run ``fn`` for one class and record its row; an inherited class is recorded and not run."""
        if plan.mode == "inherited":
            self.record(name, plan.mode, 0, 0.0, plan.reason)
            return None
        started = time.monotonic()
        value = fn(*args)
        files = None if plan.paths is None else len(plan.paths)
        self.record(name, plan.mode, files, time.monotonic() - started, plan.reason)
        return value

    def render(self) -> list[str]:
        """Header plus one row per class in CLASS_NAMES; a class with no row says so."""
        lines = ["CLASS REPORT (code_quality_scope)"]
        for name in CLASS_NAMES:
            row = self.rows.get(name)
            if row is None:
                lines.append(f"   {name:<28} NOT RECORDED")
                lines.append(f"ADVISORY code_quality_scope: {name} was not recorded: its gate did not run before this run returned")
                continue
            files = "all" if row.files is None else str(row.files)
            lines.append(f"   {name:<28} mode={row.mode:<9} files={files:<4} seconds={row.seconds:8.2f} reason={row.reason}")
        return lines


class _GitCallError(Exception):
    """A git call that did not complete with a usable answer."""


def full_plan(reason: str) -> dict[str, ClassPlan]:
    """Every class full. ``reason`` is the scoped classes' reason; others say why they are not scoped."""
    scoped = (*SCOPED_CLASSES, *CROSS_FILE_SCOPED_CLASSES)
    return {name: ClassPlan(name=name, mode="full", paths=None, reason=reason if name in scoped else _NOT_SCOPED_REASON) for name in CLASS_NAMES}


def _git(project_root: Path, *args: str) -> str:
    env = {name: value for name, value in os.environ.items() if name not in _GIT_ROUTING_VARIABLES}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitCallError(f"git {args[0]} did not complete ({exc})") from exc
    if result.returncode != 0:
        raise _GitCallError(f"git {args[0]} exited {result.returncode}: {' '.join(result.stderr.split())}")
    return result.stdout


def _unscoped(advisory: str) -> TouchedSurface:
    return TouchedSurface(base=None, paths=(), advisory=advisory)


def derive_touched_surface(project_root: Path) -> TouchedSurface:
    """Return the files HEAD's landing merge touched, relative to its first parent.

    Never raises. A HEAD that is not a merge, a working tree that differs from
    HEAD, or any git failure or timeout returns ``base=None`` with the reason
    that ``plan_classes`` carries into each scoped class.
    """
    try:
        parents = _git(project_root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
        if len(parents) < 2:
            return _unscoped("HEAD has fewer than two parents (not a landing merge)")
        if _git(project_root, "status", "--porcelain=v1", "-z"):
            return _unscoped("working tree differs from HEAD (uncommitted or untracked files)")
        names = _git(project_root, "diff", "--name-only", "-z", "--no-renames", parents[0], "HEAD")
    except _GitCallError as exc:
        return _unscoped(str(exc))
    return TouchedSurface(base=parents[0], paths=tuple(name for name in names.split("\0") if name), advisory=None)


class PyrightSurfaceError(ValueError):
    """pyproject.toml's [tool.pyright] include and exclude cannot be read, so the surface is unprovable."""


@dataclass(frozen=True)
class PyrightSurface:
    """The files pyright checks, read from [tool.pyright] include and exclude."""

    include: tuple[str, ...]
    exclude: tuple[str, ...]

    @classmethod
    def load(cls, project_root: Path) -> PyrightSurface:
        try:
            pyproject = tomllib.loads((project_root / "pyproject.toml").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PyrightSurfaceError(f"pyproject.toml cannot be read ({exc})") from exc
        table = pyproject.get("tool", {}).get("pyright", {})
        lists = {key: table.get(key) for key in ("include", "exclude")}
        if not all(isinstance(value, list) and all(isinstance(item, str) for item in value) for value in lists.values()):
            raise PyrightSurfaceError("pyproject.toml has no string include and exclude lists under [tool.pyright]")
        return cls(include=tuple(lists["include"]), exclude=tuple(lists["exclude"]))

    def covers(self, rel_path: str) -> bool:
        """True when an include names the file or a directory above it, and no exclude does."""
        path = PurePosixPath(rel_path)
        scopes = [path, *list(path.parents)[:-1]]
        included = any(_matches(scopes, pattern) for pattern in self.include)
        excluded = any(_matches(scopes, pattern) for pattern in self.exclude)
        return included and not excluded


def _matches(scopes: Sequence[PurePosixPath], pattern: str) -> bool:
    """A pattern matches a path, or a directory: a trailing ``/**`` does not match the directory itself, so it is also tried without it."""
    directory = pattern.removesuffix("/**")
    return any(scope.full_match(pattern) or scope.full_match(directory) for scope in scopes)


def pyright_surface_covers(project_root: Path, rel_path: str) -> bool:
    """Whether pyright checks ``rel_path``; raises PyrightSurfaceError when the surface cannot be read."""
    return PyrightSurface.load(project_root).covers(rel_path)


def _source_root(rel_path: str) -> str | None:
    """The source root a repo-relative path imports under: a fixed root, or plugins/<name>/src."""
    for root in _SOURCE_ROOTS:
        if rel_path.startswith(f"{root}/"):
            return root
    parts = rel_path.split("/")
    if len(parts) > 3 and parts[0] == "plugins" and parts[2] == "src":
        return "/".join(parts[:3])
    return None


def module_name(rel_path: str) -> str:
    """The dotted name a tracked .py file imports as.

    Under a source root the name is relative to that root. A top-level
    quality_gates file is its bare stem. Anything else is its repo-relative
    dotted path. An __init__.py names its package.
    """
    path = PurePosixPath(rel_path)
    stem = path.with_suffix("").as_posix()
    root = _source_root(rel_path)
    if root is not None:
        stem = stem[len(root) + 1 :]
    elif path.parent == _BARE_NAME_DIR:
        return path.parent.name if path.stem == "__init__" else path.stem
    return ".".join(part for part in stem.split("/") if part != "__init__")


def _from_base(node: ast.ImportFrom, rel_path: str) -> str:
    """The absolute dotted module a from-import reads, resolving relative levels from the file's package."""
    module = node.module or ""
    if node.level == 0:
        return module
    package = module_name(rel_path).split(".")
    if PurePosixPath(rel_path).name != "__init__.py":
        package = package[:-1]
    kept = len(package) - (node.level - 1)
    if kept <= 0:
        return ""
    return ".".join([*package[:kept], *([module] if module else [])])


def _imported_names(tree: ast.Module, rel_path: str) -> set[str]:
    """Every dotted name a file imports, including names under TYPE_CHECKING and inside functions."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _from_base(node, rel_path)
            if base:
                names.add(base)
                names.update(f"{base}.{alias.name}" for alias in node.names)
    return names


def _prefixes(dotted: str) -> list[str]:
    """``a.b.c`` with its package prefixes ``a`` and ``a.b``: pyright resolves each parent package on the import path."""
    parts = dotted.split(".")
    return [".".join(parts[:end]) for end in range(1, len(parts) + 1)]


def _parse(project_root: Path, rel_path: str) -> ast.Module | None:
    try:
        return ast.parse((project_root / rel_path).read_bytes(), filename=rel_path)
    except (OSError, SyntaxError, ValueError, RecursionError):
        return None


@dataclass(frozen=True)
class ImportIndex:
    """Each indexed file's dotted name, and the files that import each dotted name or one of its package prefixes."""

    modules: Mapping[str, str]
    importers: Mapping[str, frozenset[str]]


def build_import_index(project_root: Path, paths: Sequence[str]) -> ImportIndex | None:
    """Index what every path imports. None when any path fails to parse; never raises."""
    modules: dict[str, str] = {}
    importers: dict[str, set[str]] = {}
    for rel_path in paths:
        tree = _parse(project_root, rel_path)
        if tree is None:
            return None
        modules[rel_path] = module_name(rel_path)
        for imported in _imported_names(tree, rel_path):
            for prefix in _prefixes(imported):
                importers.setdefault(prefix, set()).add(rel_path)
    return ImportIndex(modules=modules, importers={name: frozenset(files) for name, files in importers.items()})


def first_unparseable(project_root: Path, paths: Sequence[str]) -> str | None:
    """The first path that does not parse, for the advisory line."""
    return next((rel_path for rel_path in paths if _parse(project_root, rel_path) is None), None)


def importers_of(index: ImportIndex, seeds: Sequence[str], *, transitive: bool) -> frozenset[str]:
    """Files that import a seed by its dotted name; ``transitive`` follows each importer's own name in turn.

    A seed is named from its path, so the importers of a deleted file are found too.
    """
    pending = deque(sorted({module_name(seed) for seed in seeds}))
    named = set(pending)
    found: set[str] = set()
    while pending:
        name = pending.popleft()
        for rel_path in sorted(index.importers.get(name, frozenset())):
            if rel_path in found:
                continue
            found.add(rel_path)
            importer = index.modules[rel_path]
            if transitive and importer not in named:
                named.add(importer)
                pending.append(importer)
    return frozenset(found)


@dataclass(frozen=True)
class _ImportGraph:
    """One index over every tracked .py file, shared by the god_class and pyright plans of a run."""

    index: ImportIndex
    tracked: tuple[str, ...]


class _GraphUnavailableError(Exception):
    """The import index cannot be built; the message is the advisory reason both classes run full with."""


def _load_graph(project_root: Path) -> _ImportGraph:
    try:
        tracked = tuple(name for name in _git(project_root, "ls-files", "-z", "--", "*.py").split("\0") if name)
    except _GitCallError as exc:
        raise _GraphUnavailableError(f"tracked .py list unavailable ({exc})") from exc
    index = build_import_index(project_root, tracked)
    if index is None:
        raise _GraphUnavailableError(f"{first_unparseable(project_root, tracked) or 'an indexed file'} does not parse, so the import index is unavailable")
    return _ImportGraph(index=index, tracked=tracked)


def _is_venv_named(path: Path) -> bool:
    return "venv" in str(path)


def _candidates(name: str, project_root: Path, touched: tuple[str, ...], per_file_paths: Sequence[Path]) -> tuple[Path, ...]:
    """Touched files this class decides from: existing .py files for syntax, per-file-gate files for radon."""
    if name == "syntax":
        return tuple(path for rel in touched if rel.endswith(".py") and (path := project_root / rel).is_file() and not _is_venv_named(path))
    resolved = {(project_root / rel).resolve() for rel in touched}
    return tuple(path for path in per_file_paths if path.resolve() in resolved)


def _tripped_globals(name: str, touched: Sequence[str]) -> list[str]:
    """Touched paths that make ``name``'s verdict no longer a function of the touched files alone."""
    touched_set = set(touched)
    tripped = [path for path in GLOBAL_INPUTS[name] if path in touched_set]
    if name == "pyright":
        tripped.extend(path for path in touched if path.endswith(".pyi") or PurePosixPath(path).name == "py.typed")
    return tripped


def _plan_scoped_class(name: str, project_root: Path, surface: TouchedSurface, per_file_paths: Sequence[Path]) -> ClassPlan:
    tripped = _tripped_globals(name, surface.paths)
    if tripped:
        return ClassPlan(name, "full", None, f"global input touched: {', '.join(tripped)}")
    candidates = _candidates(name, project_root, surface.paths, per_file_paths)
    if not candidates:
        return ClassPlan(name, "inherited", (), "no touched path in this class's surface; verdict inherited from the tip")
    return ClassPlan(name, "scoped", candidates, f"{len(candidates)} file(s) touched by the landing merge")


def _plan_god_class(project_root: Path, surface: TouchedSurface, per_file_paths: Sequence[Path], graph: _ImportGraph) -> ClassPlan:
    """god_class reads the abstract method names of the parent modules a class imports, so it needs the touched files and their direct importers."""
    tripped = _tripped_globals("god_class", surface.paths)
    if tripped:
        return ClassPlan("god_class", "full", None, f"global input touched: {', '.join(tripped)}")
    touched = [rel for rel in surface.paths if rel.endswith(".py")]
    by_rel = {os.path.relpath(path, project_root).replace(os.sep, "/"): path for path in per_file_paths}
    scoped = sorted((set(touched) | importers_of(graph.index, touched, transitive=False)) & by_rel.keys())
    if not scoped:
        return ClassPlan("god_class", "inherited", (), "no touched path in this class's surface; verdict inherited from the tip")
    return ClassPlan("god_class", "scoped", tuple(by_rel[rel] for rel in scoped), f"{len(scoped)} file(s): touched files and their direct importers")


_PYRIGHT_INHERITED_REASON = "no touched path lies in the pyright surface and no importer does; verdict inherited from the tip"


def _plan_pyright(project_root: Path, surface: TouchedSurface, graph: _ImportGraph) -> ClassPlan:
    """pyright's diagnostics in a file are a function of its bytes and of what it imports, so the scoped set is the touched files and their transitive importers, limited to the pyright surface."""
    tripped = _tripped_globals("pyright", surface.paths)
    if tripped:
        return ClassPlan("pyright", "full", None, f"global input touched: {', '.join(tripped)}")
    touched = [rel for rel in surface.paths if rel.endswith(".py")]
    closure = set(touched) | importers_of(graph.index, touched, transitive=True)
    if not closure:
        return ClassPlan("pyright", "inherited", (), _PYRIGHT_INHERITED_REASON)
    try:
        pyright_surface = PyrightSurface.load(project_root)
    except PyrightSurfaceError as exc:
        return ClassPlan("pyright", "full", None, f"pyright surface unprovable: {exc}")
    return _pyright_closure_plan(project_root, closure, pyright_surface, graph)


def _pyright_closure_plan(project_root: Path, closure: set[str], pyright_surface: PyrightSurface, graph: _ImportGraph) -> ClassPlan:
    """Keep the closure's files that exist and that pyright checks; run full when they exceed the cap."""
    scoped = sorted(rel for rel in closure if (project_root / rel).is_file() and pyright_surface.covers(rel))
    if not scoped:
        return ClassPlan("pyright", "inherited", (), _PYRIGHT_INHERITED_REASON)
    surface_files = sum(1 for rel in graph.tracked if pyright_surface.covers(rel))
    limit = _PYRIGHT_CLOSURE_FRACTION * surface_files
    if len(scoped) > limit:
        return ClassPlan("pyright", "full", None, f"closure {len(scoped)} files exceeds the quarter-surface limit of {limit:g} files ({_PYRIGHT_CLOSURE_FRACTION:.0%} of {surface_files} pyright-surface files)")
    return ClassPlan("pyright", "scoped", tuple(Path(rel) for rel in scoped), f"{len(scoped)} file(s): touched files and their transitive importers in the pyright surface")


def plan_classes(project_root: Path, surface: TouchedSurface, per_file_paths: Sequence[Path]) -> dict[str, ClassPlan]:
    """Plan every class in CLASS_NAMES. Only the scoped and cross-file classes can be scoped; the rest are full."""
    if surface.base is None:
        return full_plan(surface.advisory or "scope could not be derived")
    plans = full_plan(_NOT_SCOPED_REASON)
    for name in SCOPED_CLASSES:
        plans[name] = _plan_scoped_class(name, project_root, surface, per_file_paths)
    try:
        graph = _load_graph(project_root)
    except _GraphUnavailableError as exc:
        for name in CROSS_FILE_SCOPED_CLASSES:
            plans[name] = ClassPlan(name, "full", None, f"import index unavailable: {exc}")
        return plans
    plans["god_class"] = _plan_god_class(project_root, surface, per_file_paths, graph)
    plans["pyright"] = _plan_pyright(project_root, surface, graph)
    return plans


def scope_advisories(plans: dict[str, ClassPlan]) -> list[str]:
    """ADVISORY lines for scoped classes that fell back to full, and the premise line when any class is scoped."""
    scoped = (*SCOPED_CLASSES, *CROSS_FILE_SCOPED_CLASSES)
    lines = [f"ADVISORY code_quality_scope: {name} ran FULL: {plans[name].reason}" for name in scoped if plans[name].mode == "full"]
    if any(plans[name].mode != "full" for name in scoped):
        lines.append(_PREMISE_ADVISORY)
    return lines
