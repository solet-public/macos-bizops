#!/usr/bin/env python3
"""Regression coverage for full-battery receipts and the fork-rate fuse."""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import run_smokes  # noqa: E402


@contextlib.contextmanager
def _runner_root(root: Path):
    """Run the production runner against a disposable candidate tree."""
    with patch.object(run_smokes, "_REPO_ROOT", root):
        yield


def _call_main(arguments: list[str]) -> tuple[int, str]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr), patch.object(sys, "argv", arguments):
        result = run_smokes.main()
    return result, stderr.getvalue()


def _assert_duplicate_candidate_receipt_refuses() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        fixture = root / "fixture.py"
        fixture.write_text("print('fixture pass')\n", encoding="utf-8")
        register = root / "register.txt"
        register.write_text("fixture.py\n", encoding="utf-8")
        receipt_directory = Path(temporary) / "receipts"
        base_arguments = [
            "run_smokes.py",
            "--register",
            str(register),
            "--campaign",
            "receipt-fixture",
            "--jobs",
            "1",
        ]
        environment = {
            "SOLET_NAME": "run-smokes-containment-fixture",
            "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipt_directory),
        }
        with (
            _runner_root(root),
            patch.object(run_smokes, "_DEFAULT_REGISTER", register),
            patch.object(run_smokes, "_venv_python", return_value=Path(sys.executable)),
            patch.dict(os.environ, environment, clear=False),
        ):
            first, first_stderr = _call_main(base_arguments)
            second, second_stderr = _call_main(base_arguments)
            third, third_stderr = _call_main([*base_arguments, "--reauthorize"])

        assert first == 0, first_stderr
        assert second == 2, second_stderr
        assert "already ran" in second_stderr, second_stderr
        assert "prior result=passed" in second_stderr, second_stderr
        assert third == 0, third_stderr
        receipts = list(receipt_directory.glob("*.json"))
        assert len(receipts) == 1, receipts
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["result"] == "passed", receipt
        assert receipt["reauthorized_from"]["result"] == "passed", receipt
    return 7


def _fixture_source(records: Path) -> str:
    return (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"
        f"with open({str(records)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}\\n')\n"
        "    handle.flush()\n"
    )


def _kill_group_if_present(process_group: int) -> None:
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return


def _run_rate_fixture(root: Path, entries: list[str]) -> tuple[list[str], list[str], list[str]]:
    with (
        _runner_root(root),
        patch.object(run_smokes, "_MAX_SMOKE_STARTS_PER_WINDOW", 2),
    ):
        return run_smokes._run_suite(
            Path(sys.executable),
            entries,
            timeout=10,
            jobs=2,
            serial_only=frozenset(),
        )


def _recorded_groups(records: Path) -> list[tuple[int, int, int, int]]:
    return [
        tuple(int(field) for field in row.split())
        for row in records.read_text(encoding="utf-8").splitlines()
    ]


def _assert_groups_terminated(rows: list[tuple[int, int, int, int]]) -> set[int]:
    assert 1 <= len(rows) <= 2, rows
    process_groups: set[int] = set()
    for parent_pid, parent_group, child_pid, child_group in rows:
        assert parent_pid == parent_group, rows
        assert child_group == parent_group, (
            "fixture descendant escaped the runner-created process group",
            rows,
        )
        process_groups.add(parent_group)
        try:
            os.killpg(parent_group, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(
            "start-rate abort left a fixture process group alive: "
            f"parent={parent_pid} child={child_pid} group={parent_group}"
        )
    return process_groups


def _assert_rate_fuse_aborts_process_groups() -> int:
    """The third start must be refused and kill descendants of starts one/two.

    The fixture parent exits after spawning a sleeping descendant.  That forces
    the pool to schedule the third entry while the first session's child still
    exists, proving the fuse kills the process *group*, not merely the runner
    process whose exit code it can observe.
    """
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        records = root / "groups.txt"
        entries = [f"fixture-{number}.py" for number in range(3)]
        for entry in entries:
            (root / entry).write_text(_fixture_source(records), encoding="utf-8")

        process_groups: set[int] = set()
        try:
            skipped, failures, missing = _run_rate_fixture(root, entries)
            assert not skipped, skipped
            assert not missing, missing
            assert failures, "the start-rate fuse did not fail the battery"
            process_groups = _assert_groups_terminated(_recorded_groups(records))
        finally:
            for process_group in process_groups:
                _kill_group_if_present(process_group)
    return 9


def main() -> None:
    checks = _assert_duplicate_candidate_receipt_refuses()
    checks += _assert_rate_fuse_aborts_process_groups()
    print(f"run-smokes battery containment smoke: {checks} checks passed")


if __name__ == "__main__":
    main()
