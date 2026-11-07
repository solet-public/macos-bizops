#!/usr/bin/env python3
"""Regression for the import-graph scoping of god_class and pyright on a landing merge (no pytest, per project rule).

``code_quality_scope.py`` scopes ``god_class`` to the touched files and their
direct importers, and ``pyright`` to the touched files and their transitive
importers inside the pyright surface. This runs the real import index, the real
planner and the real god_class gate over throwaway git repositories, and reads
the live pyproject.toml. Every assertion carries a label, so a failure names the
outcome it guards.

Every fixture is built in a temp directory with the git routing variables
scrubbed, so the run never reads or writes this checkout's Git state.

Routines, in run order:
  D1  the import index resolves absolute, relative, bare-name, TYPE_CHECKING, in-function and package-prefix imports, and a deleted module's importers
  D2  an untouched child that a touched parent breaks is in the god_class scope, and the real god_class_check.py catches it there
  D3  pyright's scoped list is the touched module and its transitive importers inside the surface, and a tests-only landing is inherited
  D4  a pyright closure above the quarter-surface cap runs full, with an advisory naming the count and the limit
  D5  an unparseable tracked file makes the index unavailable: both classes run full, the advisory names the file, nothing raises
  D6  the pyright surface predicate matches the live pyproject.toml on a truth table of twelve paths
  D7  a touched pyproject.toml, god_class allowlist or .pyi file runs god_class or pyright full, with an advisory

Run: ``.venv/bin/python3 quality_gates/tests/code_quality_scope_dependents_smoke.py``
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

_GATE_DIR = Path(__file__).resolve().parent.parent
if str(_GATE_DIR) not in sys.path:
    sys.path.insert(0, str(_GATE_DIR))

import code_quality_check as cqc  # noqa: E402
import code_quality_scope as cqs  # noqa: E402

_FAILURES: list[str] = []
_GIT_ROUTING_VARIABLES = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY")
_GOD_CLASS_SUPPORT = ("__init__.py", "allowlist_schema.py", "gate_scope.py", "god_class_check.py")
_EMPTY_ALLOWLIST = "# smoke fixture: no tracked debt\n"
_PYPROJECT = '[tool.pyright]\ninclude = ["plugins"]\nexclude = ["**/tests"]\n'
_PKG = "plugins/p/src/pkg"
_PACKAGE_INIT = f"{_PKG}/__init__.py"
_CHILD = f"{_PKG}/child.py"
_PARENT = f"{_PKG}/parent.py"

_D1_FILES = {
    _PACKAGE_INIT: "",
    _PARENT: "class Parent:\n    pass\n",
    f"{_PKG}/child_abs.py": "from pkg.parent import Parent\n",
    f"{_PKG}/child_rel.py": "from .parent import Parent\n",
    f"{_PKG}/child_rel2.py": "from . import parent\n",
    f"{_PKG}/typed_only.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from pkg.parent import Parent\n",
    f"{_PKG}/in_function.py": "def load():\n    from pkg.parent import Parent\n    return Parent\n",
    f"{_PKG}/deleted_user.py": "from pkg.gone import Gone\n",
    f"{_PKG}/prefix_user.py": "import pkg.deep.mod\n",
    "quality_gates/helper_mod.py": "VALUE = 1\n",
    "quality_gates/user_bare.py": "import helper_mod\n",
}

_D3_FILES = {
    _PACKAGE_INIT: "",
    f"{_PKG}/a.py": "VALUE = 1\n",
    f"{_PKG}/b.py": "from pkg import a\n",
    f"{_PKG}/c.py": "from pkg import b\n",
    **{f"{_PKG}/filler{index}.py": "VALUE = 0\n" for index in range(10)},
    "plugins/p/tests/t.py": "from pkg import a\n",
    "pyproject.toml": _PYPROJECT,
}

_D4_FILES = {
    _PACKAGE_INIT: "",
    f"{_PKG}/core.py": "VALUE = 1\n",
    **{f"{_PKG}/user{index}.py": "from pkg import core\n" for index in range(1, 5)},
    **{f"{_PKG}/filler{index}.py": "VALUE = 0\n" for index in range(1, 4)},
    "pyproject.toml": _PYPROJECT,
}

_D6_TRUTH = (
    ("plugins/x/src/x/a.py", True),
    ("plugins/x/tests/a.py", False),
    ("plugins/x/research/a.py", False),
    ("plugins/x/migrations/a.py", False),
    ("quality_gates/a.py", True),
    ("quality_gates/tests/a.py", False),
    ("ananta/src/ananta/a.py", True),
    ("initialization/src/a.py", True),
    ("initialization/tests/a.py", False),
    ("solet_cli/src/a.py", False),
    ("dev_tools/a.py", False),
    ("disabled_plugins/a.py", False),
)


def check(name: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}")
    if not condition:
        _FAILURES.append(name)


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


def _build_repo(root: Path, base: Mapping[str, str], topic: Mapping[str, str]) -> None:
    """Commit ``base`` on main, commit ``topic`` on a branch, then merge it with --no-ff: HEAD is the landing merge."""
    _git(root, "init", "-q", "-b", "main")
    _write_all(root, base)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "topic")
    _write_all(root, topic)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "topic")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "--no-ff", "-q", "-m", "landing merge", "topic")


@contextmanager
def _scratch(prefix: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield Path(tmp).resolve()


def _gate_fixtures() -> dict[str, str]:
    """The real god-class gate and what it imports, with an empty allowlist, so a seeded run measures the tree alone."""
    files = {f"quality_gates/{name}": (_GATE_DIR / name).read_text(encoding="utf-8") for name in _GOD_CLASS_SUPPORT}
    files["quality_gates/god_class_allowlist.txt"] = _EMPTY_ALLOWLIST
    return files


def _parent_source(*, abstract: bool) -> str:
    decorator = "    @abstractmethod\n" if abstract else ""
    methods = "".join(f"{decorator}    def {name}(self) -> int:\n        return 0\n\n" for name in ("a", "b", "c"))
    return f"from abc import ABC, abstractmethod\n\n\nclass Parent(ABC):\n{methods}"


def _child_source() -> str:
    names = ("a", "b", "c", *(f"m{index}" for index in range(1, 14)))
    methods = "".join(f"    def {name}(self) -> int:\n        return 0\n\n" for name in names)
    return f"from pkg.parent import Parent\n\n\nclass Child(Parent):\n{methods}"


def _run_god_class(root: Path, paths: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """The real god_class_check.py over ``paths``, importing ``pkg`` from the fixture's plugins/p/src."""
    env = {name: value for name, value in os.environ.items() if name not in _GIT_ROUTING_VARIABLES}
    env["PYTHONPATH"] = str(root / "plugins/p/src")
    return subprocess.run(
        [sys.executable, str(root / "quality_gates/god_class_check.py"), *paths, "--allowlist", str(root / "quality_gates/god_class_allowlist.txt")],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _assert_import_index_resolves_absolute_relative_and_bare_imports() -> None:
    print("D1: the import index resolves absolute, relative, bare-name, TYPE_CHECKING, in-function and package-prefix imports, and a deleted module's importers")
    with _scratch("code_quality_scope_dependents_d1_") as root:
        _write_all(root, _D1_FILES)
        assert hasattr(cqs, "importers_of"), "IMPORTERS_API_PRESENT"
        index = cqs.build_import_index(root, sorted(_D1_FILES))
        assert index is not None, "INDEX_BUILDS_FROM_PARSEABLE_FILES"

        def importers(seeds: list[str]) -> frozenset[str]:
            return cqs.importers_of(index, seeds, transitive=False)

        check("an absolute from-import is an importer of its module", f"{_PKG}/child_abs.py" in importers([_PARENT]))
        check("a relative from-import is an importer of its module", f"{_PKG}/child_rel.py" in importers([_PARENT]))
        check("a from-dot import of a sibling is an importer of its module", f"{_PKG}/child_rel2.py" in importers([_PARENT]))
        check("a TYPE_CHECKING-only import is an importer", f"{_PKG}/typed_only.py" in importers([_PARENT]))
        check("an in-function import is an importer", f"{_PKG}/in_function.py" in importers([_PARENT]))
        check("a bare-name import reaches a quality_gates module", "quality_gates/user_bare.py" in importers(["quality_gates/helper_mod.py"]))
        check("a package prefix reaches a deeper import, and a touched __init__ seeds its package", f"{_PKG}/prefix_user.py" in importers([_PACKAGE_INIT]))
        check("a deleted module's importers are found from its path", f"{_PKG}/deleted_user.py" in importers([f"{_PKG}/gone.py"]))


def _assert_god_class_dependent_defect_is_caught_via_importer() -> None:
    print("D2: a touched parent that breaks an untouched child puts the child in the god_class scope, and the real god_class_check.py catches it there")
    base = {_PACKAGE_INIT: "", _PARENT: _parent_source(abstract=True), _CHILD: _child_source(), "pyproject.toml": _PYPROJECT, **_gate_fixtures()}
    with _scratch("code_quality_scope_dependents_d2_") as root:
        _build_repo(root, base=base, topic={_PARENT: _parent_source(abstract=False)})
        plan = cqc.plan_run_scope(root)
        scoped = sorted(path.relative_to(root).as_posix() for path in plan["god_class"].paths or ())
        assert _CHILD in scoped, "GOD_CLASS_DEPENDENT_IN_SCOPE"
        caught = _run_god_class(root, [str(root / rel) for rel in scoped])
        assert caught.returncode == 2 and _CHILD in caught.stdout + caught.stderr, "GOD_CLASS_DEPENDENT_DEFECT_CAUGHT"
        touched_only = _run_god_class(root, [str(root / _PARENT)])
        check("the touched-only negative control passes, so the dependent is what is caught", touched_only.returncode == 0)


def _pyright_verdict(root: Path, plan: dict[str, cqc.ClassPlan], output: tuple[bool, str]) -> tuple[cqc._CheckResults, list[str]]:
    """Run the real pyright path with run_command patched to ``output``; return the results and the commands it saw."""
    results = cqc._CheckResults(plan=plan)
    commands: list[str] = []

    def fake_run(command: str, *_args: object, **_kwargs: object) -> tuple[bool, str]:
        commands.append(command)
        return output

    with patch.object(cqc, "run_command", side_effect=fake_run), redirect_stdout(io.StringIO()):
        cqc._run_pyright_checked(root, Path(sys.executable), results)
    return results, commands


def _assert_pyright_scope_includes_transitive_importers_and_filters_surface() -> None:
    print("D3: pyright's scoped list is the touched module and its transitive importers inside the surface, and a tests-only landing is inherited")
    with _scratch("code_quality_scope_dependents_d3_") as root:
        _build_repo(root, base=_D3_FILES, topic={f"{_PKG}/a.py": "VALUE = 2\n"})
        plan = cqc.plan_run_scope(root)
        files = [path.as_posix() for path in plan["pyright"].paths or ()]
        assert files == [f"{_PKG}/a.py", f"{_PKG}/b.py", f"{_PKG}/c.py"], "PYRIGHT_CLOSURE_IS_TRANSITIVE"
        clean, commands = _pyright_verdict(root, plan, (True, ""))
        assert commands and f"{_PKG}/a.py {_PKG}/b.py {_PKG}/c.py" in commands[0], "PYRIGHT_ARGV_IS_THE_SCOPED_LIST"
        check("a clean scoped run records no type errors", not clean.has_type_errors)
        failing, _ = _pyright_verdict(root, plan, (False, f"{_PKG}/b.py:1:1 - error: boom\n"))
        check("a parsed error from a scoped run stays blocking", failing.has_type_errors)
    with _scratch("code_quality_scope_dependents_d3_tests_") as root:
        _build_repo(root, base=_D3_FILES, topic={"plugins/p/tests/t.py": "from pkg import a\n\nVALUE = 3\n"})
        plan = cqc.plan_run_scope(root)
        results = cqc._CheckResults(plan=plan)
        with patch.object(cqc, "run_command", return_value=(True, "")) as run, redirect_stdout(io.StringIO()):
            cqc._run_class(results, "pyright", cqc._run_pyright_checked, root, Path(sys.executable), results)
        assert run.call_count == 0, "INHERITED_PYRIGHT_IS_NOT_RUN"
        check("a tests-only landing inherits pyright", results.report.rows["pyright"].mode == "inherited")
        check("an inherited pyright verdict is not reported unmeasured", results.pyright_unmeasured is False)


def _assert_closure_over_cap_falls_back_to_full_with_advisory() -> None:
    print("D4: a pyright closure above the quarter-surface cap runs full, with an advisory naming the count and the limit")
    with _scratch("code_quality_scope_dependents_d4_") as root:
        _build_repo(root, base=_D4_FILES, topic={_CHILD.replace("child.py", "core.py"): "VALUE = 2\n"})
        plan = cqc.plan_run_scope(root)
        assert plan["pyright"].mode == "full", "CLOSURE_OVER_CAP_RUNS_FULL"
        advisory = [line for line in cqs.scope_advisories(plan) if "pyright ran FULL" in line]
        check("the advisory names the closure count and the limit", bool(advisory) and "closure 5 files" in advisory[0] and "2.25" in advisory[0])
        _, commands = _pyright_verdict(root, plan, (True, ""))
        check("the full pyright run passes no file list", bool(commands) and f"{_PKG}/core.py" not in commands[0])


def _assert_unparseable_file_falls_back_to_full_with_advisory() -> None:
    print("D5: an unparseable tracked file makes the index unavailable: god_class and pyright run full, the advisory names the file, and nothing raises")
    broken = f"{_PKG}/broken.py"
    base = {_PACKAGE_INIT: "", f"{_PKG}/a.py": "VALUE = 1\n", broken: "def broken(:\n    pass\n", "pyproject.toml": _PYPROJECT}
    with _scratch("code_quality_scope_dependents_d5_") as root:
        _build_repo(root, base=base, topic={f"{_PKG}/a.py": "VALUE = 2\n"})
        assert cqs.build_import_index(root, [f"{_PKG}/a.py", broken]) is None, "UNPARSEABLE_INDEX_IS_NONE"
        plan = cqc.plan_run_scope(root)
        check("god_class runs full", plan["god_class"].mode == "full")
        check("pyright runs full", plan["pyright"].mode == "full")
        advisories = "\n".join(cqs.scope_advisories(plan))
        check("the advisory names the unparseable file", broken in plan["pyright"].reason and broken in advisories)


def _assert_pyright_surface_predicate_matches_pyproject_truth_table() -> None:
    print("D6: the pyright surface predicate matches the live pyproject.toml on a truth table of twelve paths")
    repo = _GATE_DIR.parent
    for path, expected in _D6_TRUTH:
        check(f"{path} is {'inside' if expected else 'outside'} the pyright surface", cqs.pyright_surface_covers(repo, path) is expected)


def _assert_global_input_forces_full_for_god_class_and_pyright() -> None:
    print("D7: a touched pyproject.toml, god_class allowlist or .pyi file runs god_class or pyright full, with an advisory naming the file")
    base = {_PACKAGE_INIT: "", f"{_PKG}/a.py": "VALUE = 1\n", "pyproject.toml": _PYPROJECT, **_gate_fixtures()}
    cases = (
        ("pyproject.toml", {"pyproject.toml": _PYPROJECT + "# touched\n"}, ("god_class", "pyright")),
        ("god_class_allowlist.txt", {"quality_gates/god_class_allowlist.txt": _EMPTY_ALLOWLIST + "# touched\n", f"{_PKG}/a.py": "VALUE = 2\n"}, ("god_class",)),
        ("stubs.pyi", {f"{_PKG}/stubs.pyi": "VALUE: int\n", f"{_PKG}/a.py": "VALUE = 2\n"}, ("pyright",)),
    )
    for label, topic, classes in cases:
        with _scratch("code_quality_scope_dependents_d7_") as root:
            _build_repo(root, base=base, topic=topic)
            plan = cqc.plan_run_scope(root)
            for name in classes:
                check(f"{name} runs full for a touched {label}", plan[name].mode == "full" and label in plan[name].reason)


_ROUTINES = (
    _assert_import_index_resolves_absolute_relative_and_bare_imports,
    _assert_god_class_dependent_defect_is_caught_via_importer,
    _assert_pyright_scope_includes_transitive_importers_and_filters_surface,
    _assert_closure_over_cap_falls_back_to_full_with_advisory,
    _assert_unparseable_file_falls_back_to_full_with_advisory,
    _assert_pyright_surface_predicate_matches_pyproject_truth_table,
    _assert_global_input_forces_full_for_god_class_and_pyright,
)


def main() -> int:
    print("Code-quality import-graph scope smoke (god_class and pyright on a landing merge)\n")
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
