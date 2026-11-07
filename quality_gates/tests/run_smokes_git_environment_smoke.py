#!/usr/bin/env python3
"""Prove caller exclusion and runner-tree precedence over editable sources."""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from editable_finder_smoke import (
    _DESCENDANT,
    _assert_missing_setup_advises,
    _origin_layout,
    _shared_bytes,
)

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))
_REPO_ROOT = _ROOT

from quality_gates.tests.candidate_repo_smoke import (  # noqa: E402
    _build_fixture_repo,
    _fixture_env,
    _foreign_repo,
    _git_snapshot,
    _hostile_git_cases,
)


def _assert_preflight_git_environment() -> None:
    from quality_gates import run_smokes

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        source = workspace / "source"
        source.mkdir()
        head = _build_fixture_repo(source)
        # Tracked intended content must survive a foreign index's ignore rules.
        (source / ".gitignore").write_text("scripts/run.sh\n", encoding="utf-8")
        foreign = _foreign_repo(workspace)
        before = _git_snapshot(source, foreign)
        expected = run_smokes._candidate_paths_for_digest(source)
        for hostile in _hostile_git_cases(foreign):
            with patch.dict(os.environ, _fixture_env() | hostile, clear=True):
                ambient = dict(os.environ)
                paths = run_smokes._candidate_paths_for_digest(source)
                observed = run_smokes._candidate_head_commit(source)
                assert (paths, observed) == (expected, head), ("RECEIPT_HEAD_AND_PATHS_ARE_REQUESTED", hostile.keys(), paths, observed)
                assert dict(os.environ) == ambient, "CALLER_ENVIRONMENT_UNCHANGED"
        tree_only = workspace / "tree-only"
        tree_only.mkdir()
        (tree_only / "only.txt").write_text("tree-only\n")
        diagnostics = StringIO()
        with patch.dict(os.environ, _fixture_env() | _hostile_git_cases(foreign)[-2], clear=True), redirect_stderr(diagnostics):
            assert run_smokes._candidate_head_commit(tree_only) is None, "GIT_FREE_IDENTITY_STAYS_TREE_ONLY"
            assert run_smokes._candidate_paths_for_digest(tree_only) == (tree_only / "only.txt",)
        assert "ADVISORY:" in diagnostics.getvalue()
        assert _git_snapshot(source, foreign) == before, "PREFLIGHT_SENTINELS_UNCHANGED"


def _assert_private_child_origins(*, wheel: bool = False) -> None:
    """Exercise main, real children and a scrubbed launchd-style ``-m`` child."""
    from quality_gates import run_smokes

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        shared, runner = _origin_layout(workspace, wheel=wheel)
        before = _shared_bytes(shared)
        observed = workspace / "observed.json"
        child = runner / "child.py"
        child.write_text(
            "import json, os, subprocess, sys\n"
            "from pathlib import Path\n"
            f"source = {_DESCENDANT!r}\n"
            "environment = dict(os.environ)\nenvironment.pop('PYTHONPATH', None)\n"
            "direct = subprocess.run([sys.executable, '-c', source], check=True, capture_output=True, text=True)\n"
            "descendant = subprocess.run([sys.executable, '-m', 'macos_inference_plugin'], env=environment, check=True, capture_output=True, text=True)\n"
            f"Path({str(observed)!r}).write_text(json.dumps([json.loads(direct.stdout), json.loads(descendant.stdout)]))\n"
        )
        register = workspace / "register"
        register.write_text(str(child) + "\n")
        with patch.object(run_smokes, "_REPO_ROOT", runner), patch.object(sys, "argv", ["run_smokes", "--register", str(register), "--jobs", "1"]):
            status = run_smokes.main()
        assert status == 0, ("ORIGIN_FIXTURE_VALID_SETUP", status)
        rows = json.loads(observed.read_text())
        print("ORIGIN_OBSERVATIONS", json.dumps(rows), flush=True)
        assert all(row["marker"] == "RUNNER" and Path(row["origin"]).is_relative_to(runner) for row in rows), ("CHILD_AND_DESCENDANT_ORIGIN_IS_RUNNER_TREE", rows)
        assert all(value["bytes"] == "RUNNER" and Path(value["path"]).is_relative_to(runner) for row in rows for value in row["resources"].values()), ("RESOURCE_BYTES_AND_ORIGIN_ARE_RUNNER_TREE", rows)
        assert all(row["dependency"] == "DEPENDENCY" for row in rows), rows
        assert all(Path(row["prefix"]).name == "import-environment" for row in rows), ("PRIVATE_INTERPRETER_PREFIX", rows)
        assert all(not Path(row["prefix"]).exists() for row in rows), "OWNED_ENVIRONMENT_REMOVED_AFTER_EXIT"
        assert _shared_bytes(shared) == before, "SHARED_ENVIRONMENT_UNCHANGED"


def _assert_wheel_smoke_and_owned_descendant_origins() -> None:
    """Run the real wheel smoke beside an ordinary owned-source child."""
    from quality_gates import run_smokes

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        observed = workspace / "ordinary-origin.txt"
        child = workspace / "ordinary_descendant.py"
        child.write_text(
            f"from pathlib import Path\nimport coreai_embeddings_plugin\nPath({str(observed)!r}).write_text(str(Path(coreai_embeddings_plugin.__file__).resolve()), encoding='utf-8')\n",
            encoding="utf-8",
        )
        register = workspace / "register"
        register.write_text(
            f"plugins/coreai_embeddings_plugin/tests/wheel_resources_smoke.py\n{child}\n",
            encoding="utf-8",
        )
        with patch.object(run_smokes, "_REPO_ROOT", _REPO_ROOT), patch.object(sys, "argv", ["run_smokes", "--register", str(register), "--jobs", "1"]):
            status = run_smokes.main()
        assert status == 0, ("WHEEL_SMOKE_AND_ORDINARY_DESCENDANT_PASS", status)
        expected = (_REPO_ROOT / "plugins/coreai_embeddings_plugin/src/coreai_embeddings_plugin/__init__.py").resolve()
        assert Path(observed.read_text(encoding="utf-8")).resolve() == expected, ("ORDINARY_DESCENDANT_RETAINS_OWNED_SOURCE", observed.read_text())


def _assert_unknown_origin_advises_and_runs(*, wheel: bool = False) -> None:
    """Unavailable probe information cannot turn a working smoke into refusal."""
    from quality_gates import run_smokes
    from quality_gates import smoke_import_environment as imports

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        _, runner = _origin_layout(workspace, wheel=wheel)
        scratch = workspace / "scratch"
        scratch.mkdir()
        diagnostics = StringIO()
        error: Exception | None = None
        verdict = "not_run"
        output = ""
        try:
            with patch.object(imports, "probe_origins", side_effect=ValueError("UNKNOWN_PROBE_FIXTURE")), patch.object(run_smokes, "_REPO_ROOT", runner), redirect_stderr(diagnostics):
                with imports.SmokeImportEnvironment(runner, runner / ".venv/bin/python3", scratch) as environment:
                    child = workspace / "advisory_child.py"
                    child.write_text(_DESCENDANT)
                    verdict, output, _exit_code = run_smokes._run_one(environment.python, child)
                    assert environment.receipt["state"] == "advisory", environment.receipt
        except Exception as exc:
            error = exc
        assert error is None and verdict == "passed", ("UNKNOWN_ORIGIN_ADVISES_AND_RUNS", error, verdict)
        observed = json.loads(output)
        print("UNKNOWN_PROBE_CHILD_OBSERVATION", output, flush=True)
        assert observed["marker"] == "RUNNER" and Path(observed["origin"]).is_relative_to(runner), ("CHILD_AND_DESCENDANT_ORIGIN_IS_RUNNER_TREE", observed)
        assert all(value["bytes"] == "RUNNER" and Path(value["path"]).is_relative_to(runner) for value in observed["resources"].values()), ("RESOURCE_BYTES_AND_ORIGIN_ARE_RUNNER_TREE", observed)
        assert diagnostics.getvalue().count("ADVISORY run_smokes UNKNOWN_ORIGIN_ADVISES_AND_RUNS") == 1, diagnostics.getvalue()
        if not wheel:
            _assert_missing_setup_advises(workspace, runner)


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _assert_register_validation() -> None:
    """Duplicate and dangling registrations fail before any smoke runs."""
    sys.path.insert(0, str(_REPO_ROOT))
    from quality_gates.run_smokes import _read_register

    with tempfile.TemporaryDirectory() as temp_dir:
        register = Path(temp_dir) / "gate_smokes.txt"
        valid = "quality_gates/tests/run_smokes_git_environment_smoke.py"
        register.write_text(f"{valid}\n{valid}  # duplicate\n", encoding="utf-8")
        try:
            _read_register(register)
        except ValueError as exc:
            assert "duplicate smoke path" in str(exc), exc
        else:
            raise AssertionError("duplicate register entry was accepted")

        register.write_text("quality_gates/tests/does_not_exist_smoke.py\n", encoding="utf-8")
        try:
            _read_register(register)
        except ValueError as exc:
            assert "does not exist" in str(exc), exc
        else:
            raise AssertionError("dangling register entry was accepted")


def _assert_git_environment_is_scrubbed() -> None:
    """Nested Git commands see the worktree, not inherited Git variables."""
    from quality_gates.run_smokes import _run_one

    with tempfile.TemporaryDirectory() as temp_dir:
        foreign_root = Path(temp_dir) / "foreign"
        foreign_root.mkdir()
        _run(["git", "init"], foreign_root)
        fixture = Path(temp_dir) / "report_toplevel.py"
        fixture.write_text(
            "from pathlib import Path\nimport subprocess\nresult = subprocess.run(\n    ['git', 'rev-parse', '--show-toplevel'],\n    check=True, capture_output=True, text=True,\n)\nprint(result.stdout.strip())\n",
            encoding="utf-8",
        )
        original_git_env = {key: value for key, value in os.environ.items() if key.startswith("GIT_")}
        os.environ["GIT_DIR"] = str(foreign_root / ".git")
        os.environ["GIT_WORK_TREE"] = str(foreign_root)
        try:
            verdict, output, _exit_code = _run_one(Path(sys.executable), fixture)
        finally:
            for key in [key for key in os.environ if key.startswith("GIT_")]:
                del os.environ[key]
            os.environ.update(original_git_env)
        assert verdict == "passed", output
        assert output.strip() == str(_REPO_ROOT), output


def _restore_environment(original: dict[str, str]) -> None:
    """Restore the test process environment after a fixture mutation."""
    os.environ.clear()
    os.environ.update(original)


def _run_fixture(source: str) -> tuple[str, str, int]:
    """Run fixture source through the runner with the active venv."""
    from quality_gates.run_smokes import _run_one

    with tempfile.TemporaryDirectory() as temp_dir:
        fixture = Path(temp_dir) / "fixture.py"
        fixture.write_text(source, encoding="utf-8")
        return _run_one(Path(sys.executable), fixture)


def _expected_import_roots(tree: Path) -> str:
    """Assemble expected layout paths independently of the production scanner."""
    roots = [tree / "ananta/src", *sorted((tree / "plugins").glob("*/src"))]
    roots.extend(path for path in (tree / "solet_setup_contracts/src", tree / "solet_cli/src") if path.is_dir())
    return os.pathsep.join(str(path) for path in roots)


def _fixture_tree(tree: Path) -> None:
    """Create harmless packages without importing platform initialization."""
    for relative in (
        "ananta/src/ananta",
        "plugins/zeta_fixture/src/zeta_fixture",
        "plugins/alpha_fixture/src/alpha_fixture",
        "solet_setup_contracts/src/solet_setup_contracts",
        "solet_cli/src/solet_manager",
    ):
        package = tree / relative
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("MARKER = 'tree fixture'\n", encoding="utf-8")


_ORIGIN_SOURCE = "import json, os, ananta, alpha_fixture, zeta_fixture, solet_setup_contracts, solet_manager\nprint(json.dumps({'origins': {p.__name__: p.__file__ for p in (ananta, alpha_fixture, zeta_fixture, solet_setup_contracts, solet_manager)}, 'path': os.environ.get('PYTHONPATH')}))\n"


def _assert_children_import_tree_under_test() -> None:
    """Real children import the runner tree, even with hostile caller roots."""
    from quality_gates import run_smokes

    original = dict(os.environ)
    with tempfile.TemporaryDirectory() as temp_dir:
        tree = (Path(temp_dir) / "worktree").resolve()
        _fixture_tree(tree)
        caller = Path(temp_dir) / "caller-only"
        caller.mkdir()
        try:
            os.environ["PYTHONPATH"] = str(caller)
            with patch.object(run_smokes, "_REPO_ROOT", tree):
                verdict, output, _exit_code = _run_fixture("import json, ananta\nprint(json.dumps({'ananta': ananta.__file__}))\n")
                assert verdict == "passed", output
                observed = json.loads(output)
                assert Path(observed["ananta"]).resolve() == (tree / "ananta/src/ananta/__init__.py").resolve(), ("CHILD_IMPORTS_TREE_UNDER_TEST", observed)
                verdict, output, _exit_code = _run_fixture(_ORIGIN_SOURCE)
                assert verdict == "passed", output
                observed = json.loads(output)
                assert observed["path"] == _expected_import_roots(tree), observed
                assert str(caller) not in observed["path"], observed
                expected = {
                    "ananta": "ananta/src/ananta",
                    "alpha_fixture": "plugins/alpha_fixture/src/alpha_fixture",
                    "zeta_fixture": "plugins/zeta_fixture/src/zeta_fixture",
                    "solet_setup_contracts": "solet_setup_contracts/src/solet_setup_contracts",
                    "solet_manager": "solet_cli/src/solet_manager",
                }
                assert observed["origins"] == {name: str(tree / path / "__init__.py") for name, path in expected.items()}, observed
        finally:
            _restore_environment(original)
    assert dict(os.environ) == original


def _assert_shared_like_origins_are_unchanged() -> None:
    """Modeled same-root editable imports and runner imports have equal origins."""
    from quality_gates import run_smokes

    original = dict(os.environ)
    with tempfile.TemporaryDirectory() as temp_dir:
        tree = (Path(temp_dir) / "shared-like").resolve()
        _fixture_tree(tree)
        expected_path = _expected_import_roots(tree)
        baseline = subprocess.run(
            [str(Path(sys.executable)), "-c", _ORIGIN_SOURCE],
            cwd=tree,
            env=original | {"PYTHONPATH": expected_path},
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            os.environ["PYTHONPATH"] = str(Path(temp_dir) / "caller-only")
            with patch.object(run_smokes, "_REPO_ROOT", tree):
                verdict, output, _exit_code = _run_fixture(_ORIGIN_SOURCE)
        finally:
            _restore_environment(original)
        assert verdict == "passed", output
        observed = json.loads(output)
        assert observed["origins"] == json.loads(baseline.stdout)["origins"], ("SHARED_LIKE_ORIGINS_UNCHANGED", observed, baseline.stdout)
        assert observed["path"] == expected_path, observed
    assert dict(os.environ) == original


def _assert_missing_import_roots_are_advisory() -> None:
    """Missing platform sources disclose once and retain a working child env."""
    from quality_gates import run_smokes

    original = dict(os.environ)
    diagnostics = StringIO()
    with tempfile.TemporaryDirectory() as temp_dir:
        tree = Path(temp_dir).resolve()
        try:
            os.environ["PYTHONPATH"] = str(tree / "caller-only")
            with patch.object(run_smokes, "_REPO_ROOT", tree), redirect_stderr(diagnostics):
                verdict, output, _exit_code = _run_fixture("import json, os\nprint(json.dumps({'path': os.environ.get('PYTHONPATH'), 'ok': True}))\n")
        finally:
            _restore_environment(original)
        assert verdict == "passed", output
        assert json.loads(output) == {"path": None, "ok": True}, output
        lines = diagnostics.getvalue().splitlines()
        assert len(lines) == 1, lines
        assert lines[0].startswith("ADVISORY run_smokes import roots unavailable:"), lines
        assert str(tree) in lines[0] and "required source root missing or unreadable" in lines[0], lines
    assert dict(os.environ) == original


def _assert_environment_is_allowlisted() -> None:
    """Caller-only values are excluded; PYTHONPATH is computed by the runner.

    The child reports through a file: the runner keeps only a bounded output
    tail, and the computed PYTHONPATH of a deep checkout can exceed it.
    """
    original_environment = dict(os.environ)
    expected = {
        "APP_HOME": "/fixture/profile",
        "SOLET_NAME": "fixture-solet",
        "SOLET_HOME": "/fixture/solet-home",
        "SOLET_WORKSPACE_ROOT": "/fixture/workspace",
        "XDG_RUNTIME_DIR": "/fixture/runtime",
        "HOME": "/fixture/home",
        "PROJECT_SOLET_DESK_TEST_DSN": "host=127.0.0.1 port=58971 dbname=project_solet_desk_test",
        "PRIVATE_TEST_PG_MANIFEST": "/fixture/private-pg/manifest.json",
    }
    with tempfile.TemporaryDirectory() as report_directory:
        report = Path(report_directory) / "environment.json"
        try:
            os.environ.update(expected)
            os.environ["PYTHONPATH"] = "solet_cli"
            os.environ["RUN_SMOKES_UNALLOWLISTED_FIXTURE"] = "caller-only"
            os.environ["GIT_CONTROLLER_NAME"] = "caller-controller"
            os.environ["PGPASSWORD"] = "caller-only-secret"
            os.environ["PRIVATE_TEST_PG_PASSWORD"] = "caller-only-secret"
            names = (*expected, "PYTHONPATH", "RUN_SMOKES_UNALLOWLISTED_FIXTURE", "GIT_CONTROLLER_NAME", "PGPASSWORD", "PRIVATE_TEST_PG_PASSWORD")
            verdict, output, _exit_code = _run_fixture(f"import json\nimport os\nfrom pathlib import Path\nPath({str(report)!r}).write_text(json.dumps({{key: os.environ.get(key) for key in {names!r}}}), encoding='utf-8')\n")
        finally:
            _restore_environment(original_environment)
        assert verdict == "passed", output
        observed = json.loads(report.read_text(encoding="utf-8"))
    assert observed.get("PROJECT_SOLET_DESK_TEST_DSN") == expected["PROJECT_SOLET_DESK_TEST_DSN"], ("PRIVATE_PG_DSN_FORWARDED", observed)
    assert observed.get("PRIVATE_TEST_PG_MANIFEST") == expected["PRIVATE_TEST_PG_MANIFEST"], ("RUNNER_PRIVATE_PG_FORWARDED", observed)
    assert observed == expected | {
        "PYTHONPATH": _expected_import_roots(_REPO_ROOT),
        "RUN_SMOKES_UNALLOWLISTED_FIXTURE": None,
        "GIT_CONTROLLER_NAME": None,
        "PGPASSWORD": None,
        "PRIVATE_TEST_PG_PASSWORD": None,
    }, observed


def _assert_private_pg_fixture_absence_is_disclosed() -> None:
    """An absent private fixture skips the native entry point without DB work."""
    from quality_gates.run_smokes import _run_one

    original_environment = dict(os.environ)
    try:
        os.environ.pop("PROJECT_SOLET_DESK_TEST_DSN", None)
        verdict, output, _exit_code = _run_fixture("import json\nimport os\nprint(json.dumps(os.environ.get('PROJECT_SOLET_DESK_TEST_DSN')))\n")
        assert verdict == "passed", output
        assert json.loads(output) is None, output
        verdict, output, _exit_code = _run_one(
            Path(sys.executable),
            _REPO_ROOT / "plugins/agent_messaging_plugin/tests/native_spawn_owner_postgres_smoke.py",
        )
        assert verdict == "skipped", ("PRIVATE_PG_FIXTURE_ABSENCE_DISCLOSED", verdict, output)
        assert ("SKIP native spawn owner PostgreSQL fixture absent: PROJECT_SOLET_DESK_TEST_DSN; run under project-solet/scripts/provision_test_pg.py --exec") in output, output
        assert "KeyError" not in output, output
        assert "Ran " not in output, output
    finally:
        _restore_environment(original_environment)


def _assert_runner_controls_reach_fixture_children() -> None:
    """Host-lock stub controls cross the runner boundary by exact name."""
    original_environment = dict(os.environ)
    expected = {
        "RUN_SMOKES_NESTED_SELF_TEST": "1",
        "RUN_SMOKES_HOST_LOCK_STUB_MODE": "fixture-mode",
        "RUN_SMOKES_HOST_LOCK_STUB_EVENTS": "/fixture/events.jsonl",
        "RUN_SMOKES_HOST_LOCK_STUB_LABEL": "fixture-label",
        "RUN_SMOKES_HOST_LOCK_STUB_SLEEP": "0.1",
        "RUN_SMOKES_HOST_SERIAL_LOCK_PATH": "/fixture/serial-only.lock",
    }
    try:
        os.environ.update(expected)
        names = tuple(expected)
        verdict, output, _exit_code = _run_fixture(f"import json\nimport os\nprint(json.dumps({{key: os.environ.get(key) for key in {names!r}}}))\n")
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output
    assert json.loads(output) == expected, output


def _assert_runner_controls_do_not_leak_from_a_shell() -> None:
    """A shell-set stub mode cannot silently turn a top-level smoke into a fixture."""
    original_environment = dict(os.environ)
    try:
        os.environ["RUN_SMOKES_HOST_LOCK_STUB_MODE"] = "polluted-shell"
        verdict, output, _exit_code = _run_fixture("import json\nimport os\nprint(json.dumps(os.environ.get('RUN_SMOKES_HOST_LOCK_STUB_MODE')))\n")
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output
    assert json.loads(output) is None, output


def _assert_child_path_is_exactly_constructed() -> None:
    """The child PATH contains venv and fixed machine-global tool prefixes only."""
    expected = os.pathsep.join(
        (
            str(Path(sys.executable).parent),
            "/opt/homebrew/bin",
            "/opt/homebrew/sbin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        )
    )
    verdict, output, _exit_code = _run_fixture("import os\nprint(os.environ['PATH'])\n")
    assert verdict == "passed", output
    assert output.strip() == expected, output


def _assert_smoke_can_own_its_import_path() -> None:
    """Smoke-owned insertion coexists with runner-computed import roots."""
    original_environment = dict(os.environ)
    try:
        os.environ["PYTHONPATH"] = "solet_cli"
        verdict, output, _exit_code = _run_fixture("import sys\nsys.path.insert(0, 'solet_cli')\nimport homebrew.ci\n")
    finally:
        _restore_environment(original_environment)
    assert verdict == "passed", output


def _assert_editable_pth_remains_a_separate_channel() -> None:
    """The selected interpreter still processes its editable .pth files."""
    editable_paths: list[str] = []
    for package_dir in site.getsitepackages():
        for pth_file in Path(package_dir).glob("__editable__.*.pth"):
            editable_paths.extend(line for line in pth_file.read_text(encoding="utf-8").splitlines() if line.startswith("/"))
    assert editable_paths, "the test venv has no editable .pth path to prove"
    expected_path = editable_paths[0]
    verdict, output, _exit_code = _run_fixture(f"import json\nimport sys\nprint(json.dumps({expected_path!r} in sys.path))\n")
    assert verdict == "passed", output
    assert json.loads(output) is True, output


def _assert_runner_pins_child_cwd() -> None:
    """The child starts at the repository root, independently of its caller."""
    verdict, output, _exit_code = _run_fixture("from pathlib import Path\nprint(Path.cwd().resolve())\n")
    assert verdict == "passed", output
    assert output.strip() == str(_REPO_ROOT), output


def _assert_runner_owned_scratch(case: str = "normal") -> None:
    """Real children use owned short scratch and observable exit removes it."""
    from _run_smokes_scratch_fixture import _scratch_run

    _scratch_run(case)


def _assert_scratch_exception_and_advisory() -> None:
    """Exceptions clean up; allocation and removal uncertainty stays advisory."""
    from _run_smokes_scratch_fixture import _scratch_exception_and_advisory

    _scratch_exception_and_advisory()


def main() -> None:
    sys.path.insert(0, str(_REPO_ROOT))
    _assert_preflight_git_environment()
    _assert_private_child_origins()
    _assert_private_child_origins(wheel=True)
    _assert_wheel_smoke_and_owned_descendant_origins()
    _assert_unknown_origin_advises_and_runs()
    _assert_unknown_origin_advises_and_runs(wheel=True)
    _assert_children_import_tree_under_test()
    _assert_shared_like_origins_are_unchanged()
    _assert_missing_import_roots_are_advisory()
    _assert_register_validation()
    _assert_git_environment_is_scrubbed()
    _assert_environment_is_allowlisted()
    _assert_private_pg_fixture_absence_is_disclosed()
    _assert_runner_controls_reach_fixture_children()
    _assert_runner_controls_do_not_leak_from_a_shell()
    _assert_child_path_is_exactly_constructed()
    _assert_smoke_can_own_its_import_path()
    _assert_editable_pth_remains_a_separate_channel()
    _assert_runner_pins_child_cwd()
    _assert_runner_owned_scratch()
    _assert_scratch_exception_and_advisory()


if __name__ == "__main__":
    main()
