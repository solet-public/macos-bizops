#!/usr/bin/env python3
"""Regression coverage for full-battery receipts, the fork-rate fuse and the
runner's deadline-free use of the owned-process collector."""

from __future__ import annotations

import argparse
import contextlib
import inspect
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import call, patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates import full_smoke_campaign, run_smokes, smoke_process  # noqa: E402
from quality_gates.smoke_budgets import load_budgets  # noqa: E402
from quality_gates.tests import _run_smokes_head_receipt_fixture, _smoke_process_fixture  # noqa: E402
from quality_gates.tests._run_smokes_scratch_fixture import _group_alive  # noqa: E402

_RunSuiteResult = tuple[list[str], list[str], list[str], list[str], float]
_RunTimedResult = tuple[str, str, float, int | None]
_run_suite_for_test = cast(
    Callable[[Path, list[str], int, frozenset[str]], _RunSuiteResult],
    vars(run_smokes)["_run_suite"],
)
# Attention cadence is set through the collector's documented setting, and
# every wait polls at the collector's own configured interval: the slow
# child is released by real repeated attention, never by a fixed sleep.
_ATTENTION_FIXTURE_ENVIRONMENT = {"SMOKE_PROCESS_ATTENTION_SECONDS": "0.05"}
_FIXTURE_COLLECTOR_CONFIG = smoke_process.CollectorConfig.from_environment(_ATTENTION_FIXTURE_ENVIRONMENT)
# Any well-formed former value is refused: the option no longer exists.
_REMOVED_TIMEOUT_ARGUMENT = str(int(smoke_process.DEFAULT_ATTENTION_SECONDS))
_candidate_paths_for_digest = cast(
    Callable[[Path], list[Path]],
    vars(run_smokes)["_candidate_paths_for_digest"],
)
_candidate_digest = cast(Callable[[Path], str], vars(run_smokes)["_candidate_digest"])


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
        environment = _run_smokes_head_receipt_fixture._runner_environment(receipt_directory)
        with (
            _runner_root(root),
            patch.object(run_smokes, "_DEFAULT_REGISTER", register),
            patch.object(run_smokes, "_venv_python", return_value=Path(sys.executable)),
            patch.dict(os.environ, environment, clear=True),
        ):
            first, first_stderr = _call_main(base_arguments)
            second, second_stderr = _call_main(base_arguments)
            third, third_stderr = _call_main([*base_arguments, "--reauthorize"])

        assert first == 0, first_stderr
        assert "ADVISORY: Git HEAD commit unavailable" in first_stderr, first_stderr
        assert second == 2, second_stderr
        assert "already ran" in second_stderr, second_stderr
        assert "prior result=passed" in second_stderr, second_stderr
        assert third == 0, third_stderr
        receipts = list(receipt_directory.glob("*.json"))
        assert len(receipts) == 1, receipts
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
        assert receipt["result"] == "passed", receipt
        assert receipt["reauthorized_from"]["result"] == "passed", receipt
    return 8


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
    except (PermissionError, ProcessLookupError):
        return


def _run_rate_fixture(root: Path, entries: list[str]) -> tuple[list[str], list[str], list[str]]:
    with (
        _runner_root(root),
        patch.object(run_smokes, "_MAX_SMOKE_STARTS_PER_WINDOW", 2),
    ):
        skipped, failures, missing, _harness, _host_lock_wait_seconds = _run_suite_for_test(
            Path(sys.executable),
            entries,
            2,
            frozenset(),
        )
        return skipped, failures, missing


def _recorded_groups(records: Path) -> list[tuple[int, int, int, int]]:
    rows: list[tuple[int, int, int, int]] = []
    for row in records.read_text(encoding="utf-8").splitlines():
        fields = row.split()
        assert len(fields) == 4, row
        rows.append((int(fields[0]), int(fields[1]), int(fields[2]), int(fields[3])))
    return rows


def _assert_groups_terminated(rows: list[tuple[int, int, int, int]], cleanup_groups: set[int]) -> None:
    assert 1 <= len(rows) <= 2, rows
    for parent_pid, parent_group, child_pid, child_group in rows:
        assert parent_pid == parent_group, rows
        assert child_group == parent_group, (
            "fixture descendant escaped the runner-created process group",
            rows,
        )
        try:
            os.killpg(parent_group, 0)
        except (PermissionError, ProcessLookupError):
            continue
        cleanup_groups.add(parent_group)
        raise AssertionError(f"start-rate abort left a fixture process group alive: parent={parent_pid} child={child_pid} group={parent_group}")


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

        cleanup_groups: set[int] = set()
        try:
            skipped, failures, missing = _run_rate_fixture(root, entries)
            assert not skipped, skipped
            assert not missing, missing
            assert failures, "the start-rate fuse did not fail the battery"
            _assert_groups_terminated(_recorded_groups(records), cleanup_groups)
        finally:
            for process_group in cleanup_groups:
                _kill_group_if_present(process_group)
    return 9


def _assert_recorded_group_probe_ignores_eperm_esrch() -> int:
    """Treat EPERM/ESRCH as gone without signalling an inaccessible group."""
    row = (101, 101, 102, 101)
    for error in (PermissionError(1, "fixture EPERM"), ProcessLookupError(3, "fixture ESRCH")):
        cleanup_groups: set[int] = set()
        with patch.object(os, "killpg", side_effect=error) as killpg:
            _assert_groups_terminated([row], cleanup_groups)
        assert not cleanup_groups, cleanup_groups
        killpg.assert_called_once_with(row[1], 0)
    return 2


def _assert_live_recorded_group_fails_and_is_cleaned_up() -> int:
    """Retain the live-group failure and clean up its recorded group."""
    row = (101, 101, 102, 101)
    cleanup_groups: set[int] = set()
    with patch.object(os, "killpg") as killpg:
        try:
            _assert_groups_terminated([row], cleanup_groups)
        except AssertionError as error:
            assert "start-rate abort left a fixture process group alive" in str(error), error
        else:
            raise AssertionError("a genuinely live fixture group was accepted as terminated")
        for process_group in cleanup_groups:
            _kill_group_if_present(process_group)

    assert cleanup_groups == {row[1]}, cleanup_groups
    assert killpg.call_args_list == [
        call(row[1], 0),
        call(row[1], signal.SIGKILL),
    ], killpg.call_args_list
    return 2


def _assert_recorded_group_probe_handles_eperm_esrch_and_live_control() -> int:
    """Treat inaccessible groups as gone while retaining live-group detection."""
    return _assert_recorded_group_probe_ignores_eperm_esrch() + _assert_live_recorded_group_fails_and_is_cleaned_up()


def _assert_group_cleanup_ignores_eperm_and_esrch() -> int:
    """A cleanup race or reused inaccessible group must not escape the smoke."""
    for error in (PermissionError(1, "fixture EPERM"), ProcessLookupError(3, "fixture ESRCH")):
        with patch.object(os, "killpg", side_effect=error) as killpg:
            _kill_group_if_present(101)
        killpg.assert_called_once_with(101, signal.SIGKILL)
    return 2


def _assert_deleted_index_path_is_not_candidate_source() -> int:
    """An unstaged deletion must not make receipt hashing unreadable."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        deleted = root / "deleted.py"
        deleted.write_text("print('removed')\n", encoding="utf-8")
        initialized = subprocess.run(
            ["git", "init", "-q"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert initialized.returncode == 0, initialized.stderr
        indexed = subprocess.run(
            ["git", "add", "deleted.py"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert indexed.returncode == 0, indexed.stderr
        deleted.unlink()
        paths = _candidate_paths_for_digest(root)
        assert deleted not in paths, paths
        assert len(_candidate_digest(root)) == 64
    return 2


def _pid_recording_source(records: Path, exit_code: int) -> str:
    return f"import os\nwith open({str(records)!r}, 'a', encoding='utf-8') as handle:\n    handle.write(f'{{os.getpid()}}\\n')\nraise SystemExit({exit_code})\n"


def _assert_cleanup_permission_error_contains_one_smoke_failure() -> int:
    """An EPERM session cleanup must not prevent later smoke verdicts from reporting."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        records = root / "pids.txt"
        failing_entry = "fails.py"
        succeeding_entry = "after-failure.py"
        (root / failing_entry).write_text(_pid_recording_source(records, 1), encoding="utf-8")
        (root / succeeding_entry).write_text(_pid_recording_source(records, 0), encoding="utf-8")
        permission_denied_calls: list[tuple[int, signal.Signals]] = []

        def _permission_denied(process_group: int, signum: signal.Signals) -> None:
            permission_denied_calls.append((process_group, signum))
            raise PermissionError(1, "fixture EPERM")

        with (
            _runner_root(root),
            patch.object(run_smokes.os, "killpg", side_effect=_permission_denied),
        ):
            skipped, failures, missing, harness, _host_lock_wait_seconds = _run_suite_for_test(
                Path(sys.executable),
                [failing_entry, succeeding_entry],
                run_smokes._DEFAULT_JOBS,
                frozenset(),
            )
        pids = sorted(int(line) for line in records.read_text(encoding="utf-8").splitlines())

    observed = (len(pids), skipped, failures, missing, harness, sorted(permission_denied_calls))
    assert observed == (2, [], [failing_entry], [], [], [(pid, signal.SIGTERM) for pid in pids]), observed
    return 4


def _hanging_fixture_source(records: Path) -> str:
    return (
        "import os\n"
        "import subprocess\n"
        "import sys\n"
        "import time\n"
        "child = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(30)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,\n"
        ")\n"
        f"with open({str(records)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write(f'{os.getpid()} {os.getpgrp()} {child.pid} {os.getpgid(child.pid)}\\n')\n"
        "    handle.flush()\n"
        "time.sleep(30)\n"
    )


def _assert_slow_child_survives_repeated_attention() -> int:
    """A child outliving repeated attention keeps running and keeps its real result.

    The child waits until the runner's attention hook has fired more than
    once, so only real repeated attention releases it. Only the collector's
    hook prints ATTENTION, which also proves the landed collector drove it.
    """
    report_attention = run_smokes._report_attention
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        smoke, release = root / "slow.py", root / "release"
        smoke.write_text(f"import time\nfrom pathlib import Path\nprint('slow start', flush=True)\nwhile not Path({str(release)!r}).exists():\n    time.sleep({_FIXTURE_COLLECTOR_CONFIG.poll_seconds})\nprint('slow done')\n", encoding="utf-8")
        events: list[smoke_process.Attention] = []

        def _release_after_repeated_attention(path: Path, event: smoke_process.Attention) -> None:
            report_attention(path, event)
            events.append(event)
            if len(events) > 1:
                release.write_text("repeated attention observed", encoding="utf-8")

        diagnostics = io.StringIO()
        with (
            _runner_root(root),
            patch.dict(os.environ, _ATTENTION_FIXTURE_ENVIRONMENT),
            patch.object(run_smokes, "_report_attention", side_effect=_release_after_repeated_attention),
            contextlib.redirect_stderr(diagnostics),
        ):
            observed = run_smokes._run_one(Path(sys.executable), smoke)
    attention = [line.endswith("observation only, the smoke is not stopped or retried") for line in diagnostics.getvalue().splitlines() if line.startswith(f"ATTENTION {smoke}: running ")]
    assert (observed, len(attention) > 1, all(attention)) == (("passed", "slow start\nslow done\n", 0), True, True), (observed, diagnostics.getvalue())
    return 3


def _assert_real_exit_codes_are_disclosed() -> int:
    """Nonzero and 77 keep real output and an honest verdict; a child's own 124
    is a disclosed harness defect, never a failed test verdict."""
    harness = f"HARNESS-DEFECT: exit {run_smokes._HARNESS_DEFECT_EXIT_CODE} is a harness defect to file, not a test verdict; not counted as failed and not retried"
    expected = {1: ("failed", ""), run_smokes._SKIP_EXIT_CODE: ("skipped", ""), run_smokes._HARNESS_DEFECT_EXIT_CODE: ("harness", harness)}
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        root.mkdir()
        for code, (verdict, disclosure) in expected.items():
            smoke = root / f"exit_{code}.py"
            smoke.write_text(f"import sys\nprint('child output', {code})\nsys.exit({code})\n", encoding="utf-8")
            with _runner_root(root):
                observed = run_smokes._run_one(Path(sys.executable), smoke)
            assert observed == (verdict, f"child output {code}\n{disclosure}", code), (code, observed)
    return len(expected)


def _registered_identity_smoke_entry() -> str:
    """The identity smoke's register entry, or the seed register's ``# not shipped:`` line for it.

    A born seed excludes that smoke, and its assembled register keeps only the
    disclosure comment, so both forms name the same path.
    """

    def identity_entries(entries: list[str]) -> tuple[str, ...]:
        return tuple(entry for entry in entries if entry.startswith("quality_gates/tests/") and entry.endswith("_identity_gate_smoke.py"))

    matches = identity_entries(run_smokes._read_register(run_smokes._DEFAULT_REGISTER))
    if not matches:
        lines = run_smokes._DEFAULT_REGISTER.read_text(encoding="utf-8").splitlines()
        matches = identity_entries([line.removeprefix("# not shipped: ").split(" ", 1)[0] for line in lines if line.startswith("# not shipped: ")])
    assert len(matches) == 1, matches
    assert matches[0].startswith("quality_gates/tests/"), matches
    return matches[0]


def _assert_runner_imports_and_lists_without_identity_smoke() -> int:
    """Import and list a born-seed runner that lacks the excluded identity smoke."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "candidate"
        quality_gates = root / "quality_gates"
        quality_gates.mkdir(parents=True)
        # The runner's module-level sibling imports. gate_smoke_register.py is
        # one of them: the runner resolves the effective register (gate_smokes.txt
        # plus its gate_smokes.d/ fragments) through it (prb_ae6d81ea), so a
        # born-seed tree that omitted it could not import the runner at all.
        for filename in ("run_smokes.py", "battery_admission.py", "battery_summary.py", "contention_signal.py", "host_load.py", "gate_smoke_register.py"):
            shutil.copyfile(
                _REPO_ROOT / "quality_gates" / filename,
                quality_gates / filename,
            )
        (root / "fixture_smoke.py").write_text("# listed fixture\n", encoding="utf-8")
        register = quality_gates / "gate_smokes.txt"
        register.write_text("fixture_smoke.py\n", encoding="utf-8")
        (quality_gates / "serial_only_smokes.txt").write_text("", encoding="utf-8")

        identity_entry = _registered_identity_smoke_entry()
        assert not (root / identity_entry).exists()
        runner_path = quality_gates / "run_smokes.py"
        child_source = "\n".join(
            (
                "import importlib.util",
                "import sys",
                "spec = importlib.util.spec_from_file_location(",
                f"    'isolated_run_smokes', {str(runner_path)!r}",
                ")",
                "assert spec is not None and spec.loader is not None",
                "runner = importlib.util.module_from_spec(spec)",
                "sys.modules[spec.name] = runner",
                "spec.loader.exec_module(runner)",
                "sys.argv = ['run_smokes.py', '--list']",
                "raise SystemExit(runner.main())",
            )
        )
        listed = subprocess.run(
            [sys.executable, "-c", child_source],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        assert listed.returncode == 0, listed.stderr
        assert listed.stdout == "fixture_smoke.py\n", listed.stdout
    return 3


def _assert_timeout_api_and_flag_are_removed() -> int:
    """The timeout CLI/API is gone: no ignored flag, keyword, alias or default survives."""
    leftovers = [name for name in ("_DEFAULT_TIMEOUT", "_ENTRY_TIMEOUTS_SECONDS") if hasattr(run_smokes, name)]
    accepting = [name for name in ("_run_one", "_wait_for_smoke", "_run_timed", "_run_serial_phase", "_run_suite") if "timeout" in inspect.signature(vars(run_smokes)[name]).parameters]
    keyword = inspect.signature(run_smokes._run_one).bind_partial
    with contextlib.suppress(TypeError):
        accepting.append(f"_run_one(timeout=...) bound {keyword(timeout=_REMOVED_TIMEOUT_ARGUMENT)}")
    refused = subprocess.run([sys.executable, "quality_gates/run_smokes.py", "--timeout", _REMOVED_TIMEOUT_ARGUMENT, "--list"], cwd=_REPO_ROOT, capture_output=True, text=True, check=False)
    observed = (leftovers, accepting, refused.returncode, refused.stdout, f"unrecognized arguments: --timeout {_REMOVED_TIMEOUT_ARGUMENT}" in refused.stderr)
    assert observed == ([], [], 2, "", True), (observed, refused.stderr)
    return 5


def _assert_single_failure_fails_battery() -> int:
    args = argparse.Namespace(jobs=4)
    observed = ([], ["red.py"], [], [], 0.0)
    with patch.object(run_smokes, "_run_suite", return_value=observed) as run:
        selected = run_smokes._run_selected_suite(Path(sys.executable), ["red.py"], args, frozenset())
    assert selected == observed
    run.assert_called_once()
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        verdict = run_smokes._summarize(1, selected[0], selected[1], selected[2], fail_on_skip=False, harness=selected[3])
    assert verdict == 1 and output.getvalue().count("red.py") == 1, (verdict, output.getvalue())
    return 1


def _process_group_is_live(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # An owned fixture group retains our uid; this number was reused.
        return False
    return True


def _assert_process_group_stopped(
    process_group: int,
    parent_pid: int,
    child_pid: int,
    cleanup_groups: set[int],
) -> None:
    deadline = time.monotonic() + 5.0
    while _process_group_is_live(process_group):
        if time.monotonic() >= deadline:
            cleanup_groups.add(process_group)
            raise AssertionError(f"session cleanup left a fixture process group alive: parent={parent_pid} child={child_pid} group={process_group}")
        time.sleep(0.05)


def _recorded_line(records: Path) -> bool:
    return records.exists() and records.read_text(encoding="utf-8").endswith("\n")


def _run_recorded_fixture(source: Callable[[Path], str], *, cancel: bool) -> tuple[str, str, int]:
    """Run one fixture whose descendant shares its session; optionally cancel it.

    Either way the runner must leave no member of the recorded session alive.
    """
    with tempfile.TemporaryDirectory() as temporary:
        records = Path(temporary) / "groups.txt"
        smoke = Path(temporary) / "owner.py"
        smoke.write_text(source(records), encoding="utf-8")
        supervisor = run_smokes._SmokeProcessSupervisor(run_smokes._MAX_SMOKE_STARTS_PER_WINDOW, run_smokes._SMOKE_START_WINDOW_SECONDS)
        observed: list[tuple[str, str, int]] = []
        cleanup_groups: set[int] = set()
        try:
            with _runner_root(Path(temporary)):
                worker = threading.Thread(target=lambda: observed.append(run_smokes._run_one(Path(sys.executable), smoke, supervisor)))
                worker.start()
                while cancel and worker.is_alive() and not _recorded_line(records):
                    time.sleep(_FIXTURE_COLLECTOR_CONFIG.poll_seconds)
                if cancel:
                    # The owner's explicit cancellation, observed by the collector; nothing is signalled here.
                    supervisor.cancel_snapshot()
                worker.join()
            [(parent_pid, parent_group, child_pid, child_group)] = _recorded_groups(records)
            assert (parent_pid, child_group) == (parent_group, parent_group), (parent_pid, parent_group, child_group)
            _assert_process_group_stopped(parent_group, parent_pid, child_pid, cleanup_groups)
        finally:
            if records.exists():
                cleanup_groups.update(row[1] for row in _recorded_groups(records))
            for process_group in cleanup_groups:
                _kill_group_if_present(process_group)
    [result] = observed
    return result


def _harness_on_cancel_fixture_source(records: Path) -> str:
    """The hanging fixture behind a wrapper that answers termination with exit 124."""
    return f"import signal\nimport sys\nsignal.signal(signal.SIGTERM, lambda *_: sys.exit({run_smokes._HARNESS_DEFECT_EXIT_CODE}))\n" + _hanging_fixture_source(records)


def _assert_natural_exit_and_cancellation_clean_owned_sessions() -> int:
    """Natural exit and explicit owner cancellation both stop the smoke's session;
    a cancelled smoke's own exit 124 is still a disclosed harness defect."""
    natural = _run_recorded_fixture(_fixture_source, cancel=False)
    cancelled = _run_recorded_fixture(_hanging_fixture_source, cancel=True)
    harness = _run_recorded_fixture(_harness_on_cancel_fixture_source, cancel=True)
    code = run_smokes._HARNESS_DEFECT_EXIT_CODE
    expected = (
        ("passed", "", 0),
        ("failed", f"CANCELLED: battery interrupted; real exit code {-signal.SIGTERM}", -signal.SIGTERM),
        ("harness", f"CANCELLED: battery interrupted; real exit code {code}\nHARNESS-DEFECT: exit {code} is a harness defect to file, not a test verdict; not counted as failed and not retried", code),
    )
    assert (natural, cancelled, harness) == expected, (natural, cancelled, harness)
    return 9


def _assert_calibration_rows_feed_only_the_advisory_estimate() -> int:
    """Retained calibration rows shape the campaign estimate and never a deadline."""
    calibrated = {**run_smokes._HISTORICAL_ENTRY_BUDGETS_SECONDS, **load_budgets(_REPO_ROOT / "quality_gates" / "smoke_budgets.json")}
    default = cast(int, inspect.signature(full_smoke_campaign.campaign_budget).parameters["per_smoke_seconds"].default)
    with tempfile.TemporaryDirectory() as temporary:
        unmeasured = full_smoke_campaign.campaign_budget(Path(temporary), registered_count=1, entries=["unmeasured_fixture_smoke.py"])
        observed = {entry: full_smoke_campaign.campaign_budget(Path(temporary), registered_count=1, entries=[entry])["seconds"] for entry in calibrated}
    headroom = unmeasured["seconds"] / default
    expected = {entry: max(default, seconds) * headroom for entry, seconds in calibrated.items()}
    assert (observed, unmeasured["evidence"], max(calibrated.values()) > default) == (expected, None, True), (observed, expected, unmeasured)
    return len(calibrated) + 2


def _assert_landed_collector_cases_and_origin() -> int:
    """Run the predecessor collector's own real-process cases from this registered entry."""
    passed: list[str] = []

    def check(condition: object, label: str) -> None:
        if not condition:
            raise AssertionError(f"collector case failed: {label}")
        passed.append(label)

    _smoke_process_fixture.run_cases(check)
    assert Path(smoke_process.__file__).resolve() == _REPO_ROOT / "quality_gates" / "smoke_process.py", smoke_process.__file__
    return len(passed) + 1


def _assert_runner_collection_lifecycle() -> int:
    """Exercise the runner's actual collector path with short hermetic children."""
    checks = _assert_landed_collector_cases_and_origin()
    checks += _assert_timeout_api_and_flag_are_removed()
    checks += _assert_slow_child_survives_repeated_attention()
    checks += _assert_real_exit_codes_are_disclosed()
    checks += _assert_natural_exit_and_cancellation_clean_owned_sessions()
    return checks + _assert_calibration_rows_feed_only_the_advisory_estimate()


def _assert_heavy_classification() -> int:
    classify = cast(Callable[[tuple[str, ...]], bool], vars(run_smokes)["_is_heavy_unit"])
    for prefix in (
        "update_",
        "real_style_",
        "create_origin_",
        "plain_clone_",
        "hook_interpreter_pin_",
        "cold_host_matrix",
        "existing_install_router_failure_sweep_",
    ):
        assert classify((f"solet_cli/tests/{prefix}fixture_smoke.py",)), prefix
    assert not classify(("quality_gates/tests/update_fixture_smoke.py",))
    assert not classify(("solet_cli/tests/light_fixture_smoke.py",))
    assert classify(("light.py", "solet_cli/tests/update_fixture_smoke.py"))
    return 10


class _PoolProbe:
    """Record actual scheduler occupancy without launching fixture builders."""

    def __init__(self, heavy: list[str], jobs: int) -> None:
        self.heavy = heavy
        self.jobs = jobs
        self.completed: list[str] = []
        self.observed = {"active": 0, "heavy": 0, "peak": 0, "heavy_peak": 0}
        self.lock = threading.Lock()
        self.light_started = threading.Event()
        self.heavy_full = threading.Event()
        self.serial_finished = threading.Event()

    def _assert_chain_order(self, entry: str) -> None:
        if entry == self.heavy[1]:
            assert self.heavy[0] in self.completed, "exclusive chain overlapped or reversed"

    def run_entry(self, _python: Path, entry: str, _supervisor: object) -> _RunTimedResult:
        if entry == "serial.py":
            assert self.observed["active"] == 0
            self.serial_finished.set()
            return "passed", "", 0.0, 0
        assert self.serial_finished.is_set(), "pooled work started before serial phase finished"
        self._assert_chain_order(entry)
        is_heavy = entry in self.heavy
        with self.lock:
            self.observed["active"] += 1
            self.observed["heavy"] += int(is_heavy)
            self.observed["peak"] = max(self.observed["peak"], self.observed["active"])
            self.observed["heavy_peak"] = max(self.observed["heavy_peak"], self.observed["heavy"])
            if self.observed["heavy"] >= min(4, self.jobs):
                self.heavy_full.set()
        if self.jobs > 4:
            if is_heavy:
                assert self.light_started.wait(3), "heavy queue starved light work"
            else:
                assert self.heavy_full.wait(3), "heavy pool did not fill"
                self.light_started.set()
        time.sleep(0.02)
        with self.lock:
            self.observed["active"] -= 1
            self.observed["heavy"] -= int(is_heavy)
            self.completed.append(entry)
        return "passed", "", 0.02, 0


def _assert_pool_observation(
    probe: _PoolProbe,
    jobs: int,
    output: io.StringIO,
    entries: list[str],
    skipped: list[str],
    failures: list[str],
    missing: list[str],
) -> None:
    assert not skipped and not failures and not missing
    assert probe.observed["heavy_peak"] == min(4, jobs), probe.observed
    assert probe.observed["peak"] <= jobs, probe.observed
    assert probe.observed["active"] == probe.observed["heavy"] == 0, probe.observed
    reported = [line.split()[1] for line in output.getvalue().splitlines() if line.startswith("  ok ")]
    assert reported == entries, reported
    if jobs > 4:
        assert probe.light_started.is_set()


def _assert_pool_limits(jobs: int) -> int:
    """Exercise the production scheduler: heavy queue first, light progress independent."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        heavy = [f"solet_cli/tests/update_fixture_{index}_smoke.py" for index in range(9)]
        light = [f"light_{index}.py" for index in range(9)]
        entries = ["serial.py", *heavy, *light]
        for entry in entries:
            target = root / entry
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# fixture\n", encoding="utf-8")
        probe = _PoolProbe(heavy, jobs)
        output = io.StringIO()
        with (
            _runner_root(root),
            patch.object(run_smokes, "_HOST_SERIAL_LOCK_PATH", root / "serial.lock"),
            patch.object(run_smokes, "_run_timed", side_effect=probe.run_entry),
            patch.object(run_smokes, "_EXCLUSIVE_GROUPS", ((heavy[0], heavy[1]),)),
            contextlib.redirect_stdout(output),
        ):
            skipped, failures, missing, _harness, _wait = _run_suite_for_test(
                Path(sys.executable),
                entries,
                jobs,
                frozenset({"serial.py"}),
            )
        _assert_pool_observation(probe, jobs, output, entries, skipped, failures, missing)
    return 6


def _assert_heavy_pool_and_jobs_bound() -> int:
    return _assert_heavy_classification() + sum(_assert_pool_limits(jobs) for jobs in (1, 2, 4, 5, 12))


def _assert_changed_head_gets_distinct_receipt() -> int:
    return _run_smokes_head_receipt_fixture.assert_changed_head_gets_distinct_receipt()


def main() -> None:
    checks = _assert_heavy_pool_and_jobs_bound()
    with patch.object(os, "killpg", side_effect=PermissionError):
        # EPERM means the group still exists; permission denial cannot prove it is gone.
        assert _group_alive(12345), "inaccessible process group remains present"
        assert not _process_group_is_live(12345), "inaccessible reused process group is not an owned fixture"
    checks += 2
    checks += _assert_duplicate_candidate_receipt_refuses()
    checks += _assert_changed_head_gets_distinct_receipt()
    checks += _assert_rate_fuse_aborts_process_groups()
    checks += _assert_recorded_group_probe_handles_eperm_esrch_and_live_control()
    checks += _assert_group_cleanup_ignores_eperm_and_esrch()
    checks += _assert_deleted_index_path_is_not_candidate_source()
    checks += _assert_cleanup_permission_error_contains_one_smoke_failure()
    checks += _assert_runner_collection_lifecycle()
    checks += _assert_single_failure_fails_battery()
    checks += _assert_runner_imports_and_lists_without_identity_smoke()
    print(f"run-smokes battery containment smoke: {checks} checks passed")


if __name__ == "__main__":
    main()
