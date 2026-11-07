"""Git-backed regression fixture for full-battery receipt commit identity."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from quality_gates import run_smokes

_GIT_ROUTING_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
)


def _runner_environment(receipt_directory: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for variable in _GIT_ROUTING_VARIABLES:
        environment.pop(variable, None)
    environment["RUN_SMOKES_BATTERY_RECEIPT_DIR"] = str(receipt_directory)
    return environment


def _git(root: Path, environment: dict[str, str], *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, (arguments, completed.stdout, completed.stderr)
    return completed.stdout.strip()


def _call_main(arguments: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), patch.object(sys, "argv", arguments):
        result = run_smokes.main()
    return result, stdout.getvalue(), stderr.getvalue()


def _assert_initial_run_and_same_head_duplicate(arguments: list[str]) -> None:
    first, first_stdout, first_stderr = _call_main(arguments)
    assert first == 0, first_stderr
    assert "smokes: 1/1 passed, 0/1 skipped" in first_stdout, first_stdout

    same_head, _, same_head_stderr = _call_main(arguments)
    assert same_head == 2, same_head_stderr
    assert "already ran" in same_head_stderr, same_head_stderr
    assert "prior result=passed" in same_head_stderr, same_head_stderr


def _assert_changed_commit_runs_again(
    root: Path,
    environment: dict[str, str],
    arguments: list[str],
    first_commit: str,
    first_tree: str,
) -> None:
    _git(root, environment, "commit", "--allow-empty", "-q", "-m", "Advance receipt fixture HEAD")
    second_commit = _git(root, environment, "rev-parse", "--verify", "HEAD^{commit}")
    second_tree = _git(root, environment, "rev-parse", "--verify", "HEAD^{tree}")
    assert first_commit != second_commit, (first_commit, second_commit)
    assert first_tree == second_tree, (first_tree, second_tree)

    changed_head, changed_stdout, changed_stderr = _call_main(arguments)
    assert changed_head == 0, (
        "HEAD_CHANGED_SAME_TREE_MUST_NOT_REUSE_FULL_BATTERY_RECEIPT",
        first_commit,
        second_commit,
        first_tree,
        second_tree,
        changed_stdout,
        changed_stderr,
    )
    assert "smokes: 1/1 passed, 0/1 skipped" in changed_stdout, changed_stdout


def _assert_new_head_duplicate(arguments: list[str], receipt_directory: Path) -> None:
    repeated_head, _, repeated_stderr = _call_main(arguments)
    assert repeated_head == 2, repeated_stderr
    assert "already ran" in repeated_stderr, repeated_stderr
    assert "prior result=passed" in repeated_stderr, repeated_stderr
    receipts = list(receipt_directory.glob("*.json"))
    assert len(receipts) == 2, receipts


def assert_changed_head_gets_distinct_receipt() -> int:
    """Verify one candidate tree receives separate receipts for two commits."""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_root = Path(temporary)
        root = temporary_root / "candidate"
        root.mkdir()
        (root / "fixture.py").write_text("print('fixture pass')\n", encoding="utf-8")
        register = temporary_root / "evidence-register.txt"
        register.write_text("fixture.py\n", encoding="utf-8")
        receipt_directory = temporary_root / "receipts"

        environment = _runner_environment(receipt_directory)
        _git(root, environment, "init", "-q")
        _git(root, environment, "config", "user.name", "Run Smokes Fixture")
        _git(root, environment, "config", "user.email", "run-smokes-fixture@example.invalid")
        _git(root, environment, "config", "commit.gpgsign", "false")
        _git(root, environment, "add", "fixture.py")
        _git(root, environment, "commit", "-q", "-m", "Create receipt fixture")
        first_commit = _git(root, environment, "rev-parse", "--verify", "HEAD^{commit}")
        first_tree = _git(root, environment, "rev-parse", "--verify", "HEAD^{tree}")

        arguments = [
            "run_smokes.py",
            "--register",
            str(register),
            "--campaign",
            "head-receipt-fixture",
            "--jobs",
            "1",
        ]
        with (
            patch.object(run_smokes, "_REPO_ROOT", root),
            patch.object(run_smokes, "_DEFAULT_REGISTER", register),
            patch.object(run_smokes, "_venv_python", return_value=Path(sys.executable)),
            patch.dict(os.environ, environment, clear=True),
        ):
            _assert_initial_run_and_same_head_duplicate(arguments)
            _assert_changed_commit_runs_again(root, environment, arguments, first_commit, first_tree)
            _assert_new_head_duplicate(arguments, receipt_directory)
    return 4
