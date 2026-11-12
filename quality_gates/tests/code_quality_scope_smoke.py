#!/usr/bin/env python3
"""Regression for the touched-surface scoping of the code-quality gate (no pytest, per project rule).

``code_quality_check.py`` scopes three classes -- ``syntax``, ``radon_cc`` and
``radon_mi`` -- to the files a landing merge touched. This runs the real
planner, the real syntax check and the real radon gates over throwaway git
repositories: a true integration merge, a non-merge HEAD, a dirty merge HEAD, a
global input, an empty scope, and git failure. Every assertion carries a label,
so a failure names the outcome it guards.

Every fixture is built in a temp directory with the git routing variables
scrubbed, so the run never reads or writes this checkout's Git state.

Routines, in run order:
  R1  an integration merge scopes syntax and radon to its two touched files
  R2  a HEAD with one parent runs every scoped class full, with an advisory
  R3  syntax compiles only the touched files and catches a defect in one
  R4  the radon gates block a seeded complex function in a touched file
  R5  a radon global input forces full, with an advisory naming the file
  R6  an empty scope is inherited, and nothing is refused
  R7  a non-repository and a timed-out git call fall back to full
  R8  every class has a report row and is wired into the run
  R9  the report carries no vetting-parser marker and no glyph at line start
  R10 a dirty merge HEAD runs every scoped class full, with an advisory

Run: ``.venv/bin/python3 quality_gates/tests/code_quality_scope_smoke.py``
"""

from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

_GATE_DIR = Path(__file__).resolve().parent.parent
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

import code_quality_check as cqc  # noqa: E402

_FAILURES: list[str] = []
_GIT_ROUTING_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
_SCOPED = ("syntax", "radon_cc", "radon_mi")
_EXISTING = "ananta/src/existing_module.py"
_ALPHA = "ananta/src/fixture_alpha.py"
_BETA = "ananta/src/fixture_beta.py"
_BROKEN_UNTOUCHED = "ananta/src/broken_untouched.py"
_NOTES = "docs/notes.md"
_CLEAN = "def clean() -> int:\n    return 1\n"
_COMPLEX = "def complex_fn(a: int) -> int:\n" + "".join(f"    if a == {i}:\n        return {i}\n" for i in range(12)) + "    return 0\n"
_EMPTY_ALLOWLIST = "# smoke fixture: no tracked debt\n"
_GATE_SCRIPTS = (
    "gate_scope.py",
    "god_class_check.py",
    "radon_cc_check.py",
    "radon_mi_check.py",
)
_GATE_ALLOWLISTS = (
    "god_class_allowlist.txt",
    "radon_cc_allowlist.txt",
    "radon_mi_allowlist.txt",
)


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


def _load_scope() -> ModuleType | None:
    """The sibling planner module, or None at a base that does not have it yet."""
    try:
        import code_quality_scope
    except ModuleNotFoundError:
        return None
    return code_quality_scope


def _require_scope() -> ModuleType:
    scope = _load_scope()
    assert scope is not None, "SCOPE_MODULE_PRESENT"
    return scope


def _git(root: Path, *args: str) -> str:
    env = {name: value for name, value in os.environ.items() if name not in _GIT_ROUTING_VARIABLES}
    completed = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=smoke",
            "-c",
            "user.email=smoke@example.invalid",
            *args,
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    return completed.stdout


def _write_all(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _build_repo(root: Path, base: Mapping[str, str], topic: Mapping[str, str], *, no_ff: bool = True) -> None:
    """Commit ``base`` on main, commit ``topic`` on a branch, then merge it: HEAD is the landing merge.

    ``no_ff=False`` fast-forwards instead, so HEAD keeps a single parent.
    """
    _git(root, "init", "-q", "-b", "main")
    _write_all(root, base)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "topic")
    _write_all(root, topic)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "topic")
    _git(root, "checkout", "-q", "main")
    if no_ff:
        _git(root, "merge", "--no-ff", "-q", "-m", "landing merge", "topic")
    else:
        _git(root, "merge", "--ff-only", "-q", "topic")


@contextmanager
def _scratch(prefix: str) -> Generator[Path]:
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield Path(tmp).resolve()


def _gate_fixtures() -> dict[str, str]:
    """The real radon and god-class gate scripts, with empty allowlists, so a seeded run measures the touched file alone."""
    files = {f"quality_gates/{name}": (_GATE_DIR / name).read_text(encoding="utf-8") for name in _GATE_SCRIPTS}
    files.update({f"quality_gates/{name}": _EMPTY_ALLOWLIST for name in _GATE_ALLOWLISTS})
    return files


def _blocking_coherence(root: Path) -> list[str]:
    """Run the real coherence gates over ``root`` under this run's plan; return the blocking gate names."""
    results = cqc._CheckResults(plan=cqc.plan_run_scope(root))
    with patch.object(sys, "argv", ["code_quality_check.py"]), redirect_stdout(io.StringIO()):
        return cqc._check_coherence_gates(root, Path(sys.executable), results)


def _run_counting_py_compile(call: Callable[[], Any]) -> tuple[Any, list[str]]:
    """Run ``call`` and return its result with the file name each py_compile launch was given."""
    real_run = subprocess.run
    compiled: list[str] = []

    def counting_run(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "py_compile" in argv:
            compiled.append(argv[-1])
        return real_run(argv, *args, **kwargs)

    with redirect_stdout(io.StringIO()), patch.object(subprocess, "run", side_effect=counting_run):
        result = call()
    return result, compiled


def _wired_class_names() -> set[str]:
    """Literal class names passed as the second argument of a ``_run_class`` call in code_quality_check.py."""
    tree = ast.parse(Path(cqc.__file__).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_run_class" and len(node.args) >= 2):
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            names.add(arg.value)
    return names


def _assert_integration_merge_scopes_to_touched_files() -> None:
    print("R1: an integration merge scopes syntax and radon to its two touched files, and plans all twenty-one classes")
    with _scratch("code_quality_scope_r1_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN}, topic={_ALPHA: _CLEAN, _BETA: _CLEAN})
        seam = getattr(cqc, "plan_run_scope", None)
        assert seam is not None, "RUN_SCOPE_SEAM_PRESENT"
        plan = seam(root)
        touched = {path.relative_to(root).as_posix() for path in plan["syntax"].paths}
        assert touched == {_ALPHA, _BETA}, "TOUCHED_SET_IS_MERGE_DIFF"
        scope = _require_scope()
        check("all twenty-one classes are planned", len(plan) == 21 and set(plan) == set(scope.CLASS_NAMES))
        check("syntax, radon_cc and radon_mi are scoped", all(plan[name].mode == "scoped" for name in scope.SCOPED_CLASSES))
        check("radon_cc is scoped to the same two files", {p.relative_to(root).as_posix() for p in plan["radon_cc"].paths} == {_ALPHA, _BETA})
        # D2 in code_quality_scope_dependents_smoke.py proves the direct-importer case; this fixture has none, so the expected set is the touched files.
        index = scope.build_import_index(root, sorted(p.relative_to(root).as_posix() for p in root.rglob("*.py")))
        check("god_class is scoped to the touched files and their direct importers", {p.relative_to(root).as_posix() for p in plan["god_class"].paths} == touched | scope.importers_of(index, sorted(touched), transitive=False))
        check("god_class is scoped with its reason", plan["god_class"].mode == "scoped" and plan["god_class"].reason != "")


def _assert_non_merge_head_runs_every_class_full_with_advisory() -> None:
    print("R2: a HEAD with one parent runs every scoped class full, with an advisory for each")
    with _scratch("code_quality_scope_r2_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN}, topic={_ALPHA: _CLEAN}, no_ff=False)
        plan = cqc.plan_run_scope(root)
        assert all(plan[name].mode == "full" for name in _SCOPED), "NON_MERGE_RUNS_FULL"
        advisories = _require_scope().scope_advisories(plan)
        assert all(any(f"ADVISORY code_quality_scope: {name} ran FULL" in line for line in advisories) for name in _SCOPED), "NON_MERGE_ADVISORY"
        check("the reason names the missing merge", "fewer than two parents" in plan["syntax"].reason)


def _assert_syntax_catches_seeded_defect_in_touched_file() -> None:
    print("R3: syntax compiles only the touched .py files, and catches a defect seeded in one of them")
    base = {_EXISTING: _CLEAN, _BROKEN_UNTOUCHED: "def broken(:\n    pass\n"}
    with _scratch("code_quality_scope_r3_pass_") as root:
        _build_repo(root, base=base, topic={_ALPHA: _CLEAN})
        plan = cqc.plan_run_scope(root)
        errors, compiled = _run_counting_py_compile(lambda: cqc._check_syntax(root, Path(sys.executable), plan["syntax"].paths))
        compiled_rel = [Path(name).relative_to(root).as_posix() for name in compiled]
        assert len(compiled) == 1 and compiled_rel == [_ALPHA], "SYNTAX_COMPILES_ONLY_TOUCHED"
        check("a clean touched file has no syntax errors", not errors)
    with _scratch("code_quality_scope_r3_catch_") as root:
        _build_repo(root, base=base, topic={_ALPHA: "def broken_touched(:\n    pass\n"})
        plan = cqc.plan_run_scope(root)
        errors, _ = _run_counting_py_compile(lambda: cqc._check_syntax(root, Path(sys.executable), plan["syntax"].paths))
        check("a seeded syntax error in the touched file is caught", any(_ALPHA in error for error in errors))


def _assert_radon_gates_catch_seeded_complexity_in_touched_file() -> None:
    print("R4: the scoped radon gates block a seeded complex function in a touched file, and pass a clean one")
    with _scratch("code_quality_scope_r4_seeded_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN, **_gate_fixtures()}, topic={_ALPHA: _COMPLEX})
        assert "radon_cc" in _blocking_coherence(root), "RADON_CC_CATCHES_TOUCHED_DEFECT"
    with _scratch("code_quality_scope_r4_clean_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN, **_gate_fixtures()}, topic={_ALPHA: _CLEAN})
        check("a clean touched file is not blocked by radon_cc", "radon_cc" not in _blocking_coherence(root))


def _assert_global_input_forces_full_with_advisory() -> None:
    print("R5: touching a radon global input runs radon_cc full with an advisory naming the file; syntax stays scoped")
    allowlist = "quality_gates/radon_cc_allowlist.txt"
    with _scratch("code_quality_scope_r5_") as root:
        _build_repo(
            root,
            base={_EXISTING: _CLEAN, allowlist: _EMPTY_ALLOWLIST},
            topic={_ALPHA: _CLEAN, allowlist: _EMPTY_ALLOWLIST + "# touched by the smoke\n"},
        )
        plan = cqc.plan_run_scope(root)
        assert plan["radon_cc"].mode == "full", "GLOBAL_INPUT_FORCES_FULL"
        check("radon_cc's reason names the global input", allowlist in plan["radon_cc"].reason)
        check("syntax stays scoped", plan["syntax"].mode == "scoped")
        advisories = _require_scope().scope_advisories(plan)
        check("the advisory names radon_cc and the file", any("ADVISORY code_quality_scope: radon_cc ran FULL" in line and allowlist in line for line in advisories))


def _assert_empty_scope_is_inherited_not_refused() -> None:
    print("R6: a merge touching only a document is inherited for every scoped class, and nothing is refused")
    with _scratch("code_quality_scope_r6_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN, _NOTES: "# notes\n"}, topic={_NOTES: "# notes, edited\n"})
        plan = cqc.plan_run_scope(root)
        assert all(plan[name].mode == "inherited" for name in _SCOPED), "EMPTY_SCOPE_IS_INHERITED"
        results = cqc._CheckResults(plan=plan)
        ran: list[str] = []

        def spy(_venv: Path, gate: cqc._GateSpec, _root: Path, _paths: list[Path], _results: cqc._CheckResults) -> cqc._GateOutcome:
            ran.append(gate.name)
            return cqc._GateOutcome.OK

        output = io.StringIO()
        with patch.object(sys, "argv", ["code_quality_check.py"]), patch.object(cqc, "_run_coherence_gate", side_effect=spy), redirect_stdout(output):
            blocking = cqc._check_coherence_gates(root, Path(sys.executable), results)
        assert not blocking and "BLOCKING" not in output.getvalue() and "radon_cc" not in ran and "radon_mi" not in ran, "EMPTY_SCOPE_IS_NOT_REFUSED"
        check("god_class is inherited and not run for a document-only merge", "god_class" not in ran and results.report.rows["god_class"].mode == "inherited")
        check("radon_cc is recorded inherited", results.report.rows["radon_cc"].mode == "inherited")
        check("the premise advisory is printed for inherited verdicts", any("verdicts assume" in line for line in _require_scope().scope_advisories(plan)))


def _assert_git_failure_falls_back_full() -> None:
    print("R7: a non-repository, and a timed-out git call, both fall back to full with an advisory and no exception")
    scope = _require_scope()
    with tempfile.TemporaryDirectory(prefix="code_quality_scope_r7_plain_") as plain:
        plain_root = Path(plain).resolve()
        surface = scope.derive_touched_surface(plain_root)
        assert surface.base is None and surface.advisory, "GIT_FAILURE_FALLS_BACK_FULL"
        plan = scope.plan_classes(plain_root, surface, [])
        check("a non-repository runs the scoped classes full", all(plan[name].mode == "full" for name in _SCOPED))
    with _scratch("code_quality_scope_r7_timeout_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN}, topic={_ALPHA: _CLEAN})
        with patch.object(scope.subprocess, "run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=30)):
            timed_out = scope.derive_touched_surface(root)
        check("a timed-out git call is an advisory, not an exception", timed_out.base is None and "did not complete" in (timed_out.advisory or ""))


def _assert_every_class_has_a_report_row() -> None:
    print("R8: an empty report lists every class as NOT RECORDED, and every class is wired into a report row")
    scope = _require_scope()
    lines = scope.ScopeReport().render()
    assert all(any(line.startswith(f"   {name} ") and "NOT RECORDED" in line for line in lines) for name in scope.CLASS_NAMES), "EMPTY_REPORT_LISTS_EVERY_CLASS"
    coherence = {gate.name for gate in cqc._COHERENCE_GATES}
    blocking_table = {name for name, _check in cqc._BLOCKING_GATE_CHECKS}
    assert set(scope.CLASS_NAMES) <= _wired_class_names() | blocking_table | coherence, "EVERY_CLASS_IS_WIRED"
    check("the coherence gates are exactly the three per-file classes", coherence == {"god_class", "radon_cc", "radon_mi"})


def _assert_report_is_safe_for_the_vetting_parser() -> None:
    print("R9: the report and its advisories carry no vetting-parser marker and no parser glyph at line start")
    scope = _require_scope()
    report = scope.ScopeReport()
    report.record("syntax", "scoped", 2, 0.5, "2 file(s) touched by the landing merge")
    report.record("radon_cc", "inherited", 0, 0.0, "no touched path in this class's surface; verdict inherited from the tip")
    full = scope.full_plan("HEAD has fewer than two parents (not a landing merge)")
    lines = report.render() + scope.scope_advisories(full) + scope.ScopeReport().render()
    assert not any("❌ BLOCKING:" in line for line in lines), "REPORT_HAS_NO_BLOCKING_MARKER"
    assert not any(line.startswith(("💡", "✅")) for line in lines), "REPORT_HAS_NO_PARSER_GLYPH_AT_LINE_START"


def _assert_dirty_merge_head_runs_full_with_advisory() -> None:
    print("R10: a landing merge with an uncommitted edit and an untracked .py file runs every scoped class full")
    with _scratch("code_quality_scope_r10_") as root:
        _build_repo(root, base={_EXISTING: _CLEAN}, topic={_ALPHA: _CLEAN})
        (root / _EXISTING).write_text("def existing() -> int:\n    return 2\n", encoding="utf-8")
        (root / "ananta/src/untracked_module.py").write_text(_CLEAN, encoding="utf-8")
        plan = cqc.plan_run_scope(root)
        assert all(plan[name].mode == "full" for name in _SCOPED), "DIRTY_MERGE_HEAD_RUNS_FULL"
        check("the advisory names the dirty working tree", "working tree differs from HEAD" in plan["syntax"].reason)


_ROUTINES = (
    _assert_integration_merge_scopes_to_touched_files,
    _assert_non_merge_head_runs_every_class_full_with_advisory,
    _assert_syntax_catches_seeded_defect_in_touched_file,
    _assert_radon_gates_catch_seeded_complexity_in_touched_file,
    _assert_global_input_forces_full_with_advisory,
    _assert_empty_scope_is_inherited_not_refused,
    _assert_git_failure_falls_back_full,
    _assert_every_class_has_a_report_row,
    _assert_report_is_safe_for_the_vetting_parser,
    _assert_dirty_merge_head_runs_full_with_advisory,
)


def main() -> int:
    print("Code-quality touched-surface scope smoke (landing merge)\n")
    for routine in _ROUTINES:
        routine()
    print()
    if _FAILURES:
        print(f"FAILED: {len(_FAILURES)} check(s): {_FAILURES}")
        return 1
    print("ALL SMOKE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
