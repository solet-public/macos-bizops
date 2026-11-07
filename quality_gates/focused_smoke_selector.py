#!/usr/bin/env python3
"""Advisory-only focused landing smoke selector (wgr_3690c324, chg_6dd931a3).

WHAT THIS DOES. Given the repo-relative paths a landing changes, it names the
registered gate smokes (``quality_gates/gate_smokes.txt``) that exercise them,
from facts each smoke's own source declares. No smoke declares an input
manifest, so every mapping is derived. The output is a register file that
``run_smokes.py --register`` already accepts, plus a JSON report that names the
rule and the evidence for every selected smoke.

RULES. Each rule only ADDS smokes; none excludes or narrows.

- ``named``: a review-named registered smoke (``--named-file``).
- ``literal``: the smoke source contains the changed path, its basename or,
  for ``.py``, its dotted module form (whole path, and the part after the last
  ``/src/``), with ``grep -F`` substring semantics. A changed path that is
  itself a registered smoke selects that smoke under this rule as well.
- ``scanned_root``: the smoke calls a scanner API (rglob, glob, iterdir,
  os.walk/listdir/scandir/fwalk, glob.glob/iglob) and the changed path's first
  segment is ``.`` (a top-level file), equals the smoke's first segment, or
  occurs in the smoke source; or that segment is ``plugins`` and the smoke is
  ``solet_cli/tests/real_style_tree_2_smoke.py``, which derives its scan root
  from an installed package.
- ``real_root_evaluator``: the smoke evaluates the real repository root
  (``scan_repository``/``evaluate_repository`` on ``_REPO_ROOT``, or a call
  with ``cwd=_REPO_ROOT``), matched over the whole source so a call that spans
  lines counts. It is selected for every non-empty changed scope.
- ``imports_changed_module``: the smoke reaches a changed python module
  through imports. Every git-listed python file is parsed once; absolute
  imports, relative imports (resolved from the importing package) and bare
  imports (resolved against the importing file's own directory, the sibling
  test-fixture idiom) feed one reverse index that is searched backwards from
  the changed paths. A deleted or renamed path still seeds the search by its
  derived module names, because importers in the candidate tree still use them.
- ``entry_point_plugin``: the smoke loads plugins through ``entry_points``
  and the quoted ``"ananta.plugins"`` group, and the changed path is
  ``plugins/<name>/src/...`` or ``plugins/<name>/pyproject.toml``.

PASS CONDITION (rul_becb4bed). The selector never refuses or blocks a landing.
A stale, unknown or missing input becomes one ``ADVISORY`` line naming it, and
the selection is the union of the rules that still apply. That covers a
changed path no rule maps, a register row whose file is missing, a python file
that does not parse, and a root with no usable git file list. Nothing is
guessed for an unmapped path; the full register still runs once on the release
bundle. A broad match is reported, never narrowed. Exit status is 0 for every
selection outcome; only an argparse usage error exits 2.

LIFECYCLE. Each run overwrites its two outputs, which are sorted and
byte-identical for identical inputs. The selector holds no register, database
or shared mutable state, so stale-write refusal and reversal do not apply;
retiring a selection is deleting its evidence directory.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

# This file is dual-invoked: directly as a script (sys.path[0] is
# quality_gates/) and imported as `quality_gates.focused_smoke_selector` by
# smoke_proof.py and the full-battery producer (sys.path[0] is the repo root).
# Inserting the repo root makes the package import below resolve identically in
# both cases -- the same bind `main()` already does for the shared producer.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quality_gates.gate_smoke_register import REGISTER_RELPATH as _REGISTER_RELPATH  # noqa: E402
from quality_gates.gate_smoke_register import load as _load_effective_register  # noqa: E402
from quality_gates.gate_smoke_register import load_from_sources as _load_register_sources  # noqa: E402
from quality_gates.gate_smoke_register import parse_register_text as _parse_register_text  # noqa: E402

SCHEMA: Final[str] = "focused-smoke-selector/v1"
RULES: Final[tuple[str, ...]] = (
    "named",
    "literal",
    "scanned_root",
    "real_root_evaluator",
    "imports_changed_module",
    "entry_point_plugin",
)
#: Re-exported from the loader, which owns it. smoke_proof, smoke_identity and
#: the full-battery producer import this name from here.
REGISTER_RELPATH: Final[str] = _REGISTER_RELPATH
BROAD_MATCH_LIMIT: Final[int] = 100

_ADVISORY: Final[str] = "ADVISORY focused_smoke_selector:"
_SCANNER_API_RE: Final[re.Pattern[str]] = re.compile(r"(\.rglob\(|\.glob\(|\.iterdir\(|os\.(walk|listdir|scandir|fwalk)\(|glob\.(glob|iglob)\()")
_REAL_ROOT_RE: Final[re.Pattern[str]] = re.compile(r"(evaluate_repository|scan_repository)\s*\(\s*_REPO_ROOT|\w+\s*\([^)]*cwd\s*=\s*_REPO_ROOT")
_ENTRY_POINT_GROUP_LITERAL: Final[str] = '"ananta.plugins"'
_PLUGIN_SOURCE_RE: Final[re.Pattern[str]] = re.compile(r"^plugins/[^/]+/(src/.+|pyproject\.toml)$")
_PLUGIN_IMPORT_ROOT_RE: Final[re.Pattern[str]] = re.compile(r"^(plugins/[^/]+/src)/")
_FIXED_IMPORT_ROOTS: Final[tuple[str, ...]] = ("ananta/src", "solet_cli/src")
_CROSS_ROOT_SCANNER: Final[str] = "solet_cli/tests/real_style_tree_2_smoke.py"
_GIT_ROUTING_VARIABLES: Final[tuple[str, ...]] = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


@dataclass(frozen=True)
class Reason:
    """Why one smoke was selected: the rule id and the evidence it matched."""

    rule: str
    detail: str


@dataclass(frozen=True)
class Selection:
    """Selected smokes with their reasons, plus every advisory and unmapped path."""

    reasons: dict[str, tuple[Reason, ...]]
    advisories: tuple[str, ...]
    unmapped: tuple[str, ...]


@dataclass(frozen=True)
class _Smoke:
    path: str
    text: str


@dataclass
class _ImportIndex:
    """Reverse import index: an imported name to the files that import it."""

    absolute: dict[str, set[str]] = field(default_factory=dict[str, set[str]])
    sibling: dict[tuple[str, str], set[str]] = field(default_factory=dict[tuple[str, str], set[str]])

    def add(self, importer: str, absolute: Iterable[str], bare: Iterable[str]) -> None:
        directory = _parent(importer)
        for name in absolute:
            self.absolute.setdefault(name, set()).add(importer)
        for name in bare:
            self.sibling.setdefault((directory, name), set()).add(importer)

    def importers_of(self, path: str) -> set[str]:
        found: set[str] = set()
        for name in _module_names(path):
            found |= self.absolute.get(name, set())
        for key in _sibling_keys(path):
            found |= self.sibling.get(key, set())
        found.discard(path)
        return found


class _Collector:
    """Accumulates reasons per smoke and the smokes each changed path maps."""

    def __init__(self, changed: Sequence[str]) -> None:
        self.reasons: dict[str, set[Reason]] = {}
        self.per_path: dict[str, set[str]] = {path: set() for path in changed}

    def add(self, smoke: str, reason: Reason, changed: str | None = None) -> None:
        self.reasons.setdefault(smoke, set()).add(reason)
        if changed is not None:
            self.per_path[changed].add(smoke)


def parse_register_text(text: str) -> tuple[str, ...]:
    """Rows in register grammar: text after ``#`` and blank lines ignored, first occurrence kept.

    Re-exported from :mod:`quality_gates.gate_smoke_register`, which owns the one
    copy of the grammar. This name stays because ``smoke_proof`` and
    ``platform_dev_surface_plugin``'s full-battery producer import it from here.
    It parses ONE register body; the UNION of the register and its fragments
    comes from the loader, which ``_load_register`` below calls.
    """
    return _parse_register_text(text)


def select_focused_smokes(
    repo_root: Path,
    changed_paths: Sequence[str],
    named_smokes: Sequence[str] = (),
    *,
    source_files: Mapping[str, bytes] | None = None,
) -> Selection:
    """Map changed repo-relative paths to registered smokes; never raises on stale inputs."""
    advisories: list[str] = []
    changed = _normalise_paths(changed_paths)
    smokes = _load_register(repo_root, advisories, source_files)
    collector = _Collector(changed)
    _apply_named(smokes, named_smokes, collector, advisories)
    for path in changed:
        for smoke in smokes:
            for reason in _text_reasons(smoke, path):
                collector.add(smoke.path, reason, path)
    _apply_real_root(smokes, changed, collector)
    _apply_imports(repo_root, changed, smokes, collector, advisories, source_files)
    unmapped = tuple(path for path in changed if not collector.per_path[path])
    advisories.extend(_outcome_advisories(collector, unmapped))
    reasons = {smoke: tuple(sorted(found, key=lambda reason: (RULES.index(reason.rule), reason.detail))) for smoke, found in sorted(collector.reasons.items())}
    return Selection(reasons=reasons, advisories=tuple(advisories), unmapped=unmapped)


def build_report(changed_paths: Sequence[str], selection: Selection) -> dict[str, object]:
    """The JSON report (schema ``focused-smoke-selector/v1``) for one selection."""
    return {
        "schema": SCHEMA,
        "changed_paths": list(_normalise_paths(changed_paths)),
        "selected": [{"smoke": smoke, "reasons": [{"rule": reason.rule, "detail": reason.detail} for reason in reasons]} for smoke, reasons in selection.reasons.items()],
        "rule_counts": _rule_counts(selection),
        "advisories": list(selection.advisories),
        "unmapped": list(selection.unmapped),
    }


def _normalise_paths(paths: Iterable[str]) -> tuple[str, ...]:
    normalised: set[str] = set()
    for raw in paths:
        path = raw.strip().removeprefix("./")
        if path:
            normalised.add(path)
    return tuple(sorted(normalised))


def _load_register(repo_root: Path, advisories: list[str], source_files: Mapping[str, bytes] | None = None) -> tuple[_Smoke, ...]:
    """Load the EFFECTIVE register -- ``gate_smokes.txt`` plus its fragments.

    A smoke registered only in a fragment is therefore selectable, which is the
    point of the fragment route. The main register staying unreadable is still
    one advisory and an empty selection, exactly as before; an unreadable
    fragment is its own advisory and the rest of the register is still used.
    """
    register = repo_root / REGISTER_RELPATH
    try:
        if source_files is None:
            effective = _load_effective_register(register)
        else:
            effective = _load_register_sources(source_files, REGISTER_RELPATH)
        rows = effective.entries
        advisories.extend(effective.advisories)
    except (OSError, KeyError, UnicodeError) as error:
        advisories.append(f"{_ADVISORY} register {REGISTER_RELPATH} is unreadable ({error}); nothing is selected")
        return ()
    smokes: list[_Smoke] = []
    for row in rows:
        try:
            body = (repo_root / row).read_bytes() if source_files is None else source_files[row]
            smokes.append(_Smoke(row, body.decode("utf-8", errors="replace")))
        except (OSError, KeyError) as error:
            advisories.append(f"{_ADVISORY} register row {row} has no readable file ({error}); row skipped")
    return tuple(smokes)


def _apply_named(
    smokes: Sequence[_Smoke],
    named_smokes: Sequence[str],
    collector: _Collector,
    advisories: list[str],
) -> None:
    registered = {smoke.path for smoke in smokes}
    for name in _normalise_paths(named_smokes):
        if name in registered:
            collector.add(name, Reason("named", "named by review"))
        else:
            advisories.append(f"{_ADVISORY} review-named smoke {name} is not registered; not added")


def _literal_patterns(path: str) -> tuple[str, ...]:
    patterns = [path, path.rsplit("/", 1)[-1]]
    if path.endswith(".py"):
        patterns.append(path.removesuffix(".py").replace("/", "."))
        if "/src/" in path:
            patterns.append(path.rsplit("/src/", 1)[1].removesuffix(".py").replace("/", "."))
    return tuple(dict.fromkeys(pattern for pattern in patterns if pattern))


def _path_root(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else "."


def _text_reasons(smoke: _Smoke, path: str) -> list[Reason]:
    reasons: list[Reason] = []
    if smoke.path == path:
        reasons.append(Reason("literal", f"{path} is this registered smoke"))
    for pattern in _literal_patterns(path):
        if pattern in smoke.text:
            reasons.append(Reason("literal", f"source contains {pattern!r} for {path}"))
    if _SCANNER_API_RE.search(smoke.text) and _scans_root(smoke, _path_root(path)):
        reasons.append(Reason("scanned_root", f"calls a scanner API and covers root {_path_root(path)!r} of {path}"))
    if _PLUGIN_SOURCE_RE.match(path) and "entry_points" in smoke.text and _ENTRY_POINT_GROUP_LITERAL in smoke.text:
        reasons.append(Reason("entry_point_plugin", f"loads every plugin through ananta.plugins; {path} is plugin source"))
    return reasons


def _scans_root(smoke: _Smoke, root: str) -> bool:
    if root in (".", _path_root(smoke.path)) or root in smoke.text:
        return True
    return root == "plugins" and smoke.path == _CROSS_ROOT_SCANNER


def _apply_real_root(smokes: Sequence[_Smoke], changed: Sequence[str], collector: _Collector) -> None:
    if not changed:
        return
    for smoke in smokes:
        if _REAL_ROOT_RE.search(smoke.text):
            collector.add(smoke.path, Reason("real_root_evaluator", "evaluates the real repository root; scope-wide"))


def _apply_imports(
    repo_root: Path,
    changed: Sequence[str],
    smokes: Sequence[_Smoke],
    collector: _Collector,
    advisories: list[str],
    source_files: Mapping[str, bytes] | None = None,
) -> None:
    seeds = [path for path in changed if path.endswith(".py")]
    if not seeds:
        return
    index = _build_import_index(repo_root, advisories, source_files)
    if index is None:
        return
    registered = {smoke.path for smoke in smokes}
    for seed in seeds:
        parents = _reverse_closure(index, seed)
        for smoke in sorted(registered.intersection(parents)):
            if smoke != seed:
                chain = " -> ".join(_chain(parents, smoke))
                collector.add(smoke, Reason("imports_changed_module", f"import chain {chain}"), seed)


def _git_python_files(repo_root: Path) -> tuple[str, ...] | str:
    """Git-listed python files (cached and untracked), or the reason the list is unusable."""
    environment = {key: value for key, value in os.environ.items() if key not in _GIT_ROUTING_VARIABLES}
    try:
        top = subprocess.run(
            ("git", "-C", str(repo_root), "rev-parse", "--show-toplevel"),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != repo_root.resolve():
            return f"{repo_root} is not a git top level ({(top.stderr or top.stdout).strip()})"
        listed = subprocess.run(
            ("git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "*.py"),
            env=environment,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        return f"git is unavailable ({error})"
    if listed.returncode != 0:
        return f"git ls-files failed ({listed.stderr.decode('utf-8', errors='replace').strip()})"
    return tuple(sorted({entry for entry in listed.stdout.decode("utf-8").split("\0") if entry}))


def _build_import_index(repo_root: Path, advisories: list[str], source_files: Mapping[str, bytes] | None = None) -> _ImportIndex | None:
    files = _git_python_files(repo_root) if source_files is None else tuple(path for path in source_files if path.endswith(".py"))
    if isinstance(files, str):
        advisories.append(f"{_ADVISORY} import rule skipped: {files}")
        return None
    index = _ImportIndex()
    for path in files:
        try:
            body = (repo_root / path).read_bytes() if source_files is None else source_files[path]
            tree = ast.parse(body, filename=path)
        except OSError as error:
            advisories.append(f"{_ADVISORY} {path} is listed by git but unreadable ({error}); no import edges")
            continue
        except (SyntaxError, ValueError) as error:
            advisories.append(f"{_ADVISORY} {path} does not parse ({error}); no import edges")
            continue
        absolute, bare = _imported_names(tree, _module_names(path), path.endswith("/__init__.py"))
        index.add(path, absolute, bare)
    return index


def _imported_names(tree: ast.Module, own_names: set[str], is_package: bool) -> tuple[set[str], set[str]]:
    """Absolute names (including resolved relative imports) and bare names a module imports."""
    absolute: set[str] = set()
    bare: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {alias.name for alias in node.names}
            absolute |= names
            bare |= names
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names = _from_names(node.module, node.names)
            absolute |= names
            bare |= names
        elif isinstance(node, ast.ImportFrom):
            for own in own_names:
                base = _relative_base(own, is_package, node.level, node.module)
                if base is not None:
                    absolute |= _from_names(base, node.names)
    return absolute, bare


def _from_names(base: str, aliases: Sequence[ast.alias]) -> set[str]:
    names = {base}
    names.update(f"{base}.{alias.name}" for alias in aliases if alias.name != "*")
    return names


def _relative_base(own: str, is_package: bool, level: int, module: str | None) -> str | None:
    parts = own.split(".") if own else []
    keep = len(parts) - level + (1 if is_package else 0)
    if keep < 0:
        return None
    base = ".".join([*parts[:keep], *([module] if module else [])])
    return base or None


def _dotted(relative: str) -> str:
    if relative == "__init__.py":
        return ""
    stem = relative.removesuffix("/__init__.py") if relative.endswith("/__init__.py") else relative.removesuffix(".py")
    return stem.replace("/", ".")


def _import_roots(path: str) -> list[str]:
    roots = [root for root in _FIXED_IMPORT_ROOTS if path.startswith(f"{root}/")]
    plugin_root = _PLUGIN_IMPORT_ROOT_RE.match(path)
    if plugin_root:
        roots.append(plugin_root.group(1))
    roots.append("")
    return roots


def _module_names(path: str) -> set[str]:
    if not path.endswith(".py"):
        return set()
    names = {_dotted(path[len(root) + 1 :] if root else path) for root in _import_roots(path)}
    names.discard("")
    return names


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _sibling_keys(path: str) -> set[tuple[str, str]]:
    if not path.endswith(".py"):
        return set()
    parts = path.split("/")[:-1]
    keys: set[tuple[str, str]] = set()
    for depth in range(len(parts) + 1):
        directory = "/".join(parts[:depth])
        dotted = _dotted(path[len(directory) + 1 :] if directory else path)
        if dotted:
            keys.add((directory, dotted))
    return keys


def _reverse_closure(index: _ImportIndex, seed: str) -> dict[str, str]:
    """Every file that reaches ``seed`` through imports, mapped to the next file towards it."""
    parents: dict[str, str] = {seed: ""}
    queue: deque[str] = deque([seed])
    while queue:
        node = queue.popleft()
        for importer in sorted(index.importers_of(node)):
            if importer not in parents:
                parents[importer] = node
                queue.append(importer)
    return parents


def _chain(parents: dict[str, str], smoke: str) -> list[str]:
    chain = [smoke]
    while parents[chain[-1]]:
        chain.append(parents[chain[-1]])
    return chain


def _outcome_advisories(collector: _Collector, unmapped: Sequence[str]) -> list[str]:
    advisories = [f"{_ADVISORY} no registered smoke maps {path}; nothing is selected for it" for path in unmapped]
    for path, smokes in sorted(collector.per_path.items()):
        if len(smokes) > BROAD_MATCH_LIMIT:
            advisories.append(f"{_ADVISORY} broad match: {path} selects {len(smokes)} registered smokes; the selection is not narrowed")
    if not collector.reasons:
        advisories.append(f"{_ADVISORY} the focused register is empty; Step 7.5 runs nothing for this scope")
    return advisories


def _rule_counts(selection: Selection) -> dict[str, int]:
    return {rule: sum(1 for reasons in selection.reasons.values() if any(reason.rule == rule for reason in reasons)) for rule in RULES}


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select registered gate smokes for a changed-path list (advisory).")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--paths-file", type=Path, help="one repo-relative changed path per line (advisory)")
    source.add_argument("--request-json", type=Path, help="platform-smoke-proof/v1 immutable request")
    parser.add_argument("--register-out", type=Path, required=True, help="register file to write")
    parser.add_argument("--report-out", type=Path, required=True, help="JSON report to write")
    parser.add_argument("--named-file", type=Path, help="review-named registered smoke paths (register grammar)")
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry: write the register and the report; exit 0 for every selection outcome."""
    arguments = _parser().parse_args(argv)
    if arguments.request_json is not None:
        # Direct script invocation puts quality_gates/, not its parent, on
        # sys.path. Bind the shared producer to this script's repository.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from quality_gates.smoke_proof import select_smokes

        report = select_smokes(arguments.repo_root, json.loads(arguments.request_json.read_bytes()))
        arguments.register_out.write_text(report["register"], encoding="utf-8")
        arguments.report_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"FOCUSED-SELECTOR selected={len(report['selected'])} schema={report['schema']} report={arguments.report_out}")
        return 0
    notices: list[str] = []
    try:
        changed = _read_lines(arguments.paths_file)
    except (OSError, UnicodeError) as error:
        changed = []
        notices.append(f"{_ADVISORY} paths file {arguments.paths_file} is unreadable ({error}); nothing is selected")
    named: tuple[str, ...] = ()
    if arguments.named_file:
        try:
            named = parse_register_text(arguments.named_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as error:
            notices.append(f"{_ADVISORY} named file {arguments.named_file} is unreadable ({error}); no review-named smokes added")
    selection = select_focused_smokes(arguments.repo_root, changed, named)
    register = "".join(f"{smoke}\n" for smoke in selection.reasons)
    arguments.register_out.write_text(register, encoding="utf-8")
    report = build_report(changed, selection)
    arguments.report_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for advisory in (*notices, *selection.advisories):
        print(advisory, file=sys.stderr)
    counts = ",".join(f"{rule}:{count}" for rule, count in _rule_counts(selection).items())
    print(f"FOCUSED-SELECTOR selected={len(selection.reasons)} rules={counts} advisories={len(selection.advisories)} unmapped={len(selection.unmapped)} report={arguments.report_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
