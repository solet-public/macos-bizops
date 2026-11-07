#!/usr/bin/env python3
"""Regression coverage for the full battery's retry-once-alone flaky-leg policy.

``run_smokes.py`` re-runs every first-time failure of the canonical registered
full battery ONCE, alone, under the host lock, and ends ``passed_with_retries``
(never ``passed``) when it is green only through retries. Each case below drives
the REAL runner as a subprocess against a disposable fixture register of tiny
scripts (never the registered smokes), with the battery receipt directory and
the host lock pointed at temporary paths.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

from quality_gates.run_smokes import _HARNESS_DEFECT_EXIT_CODE  # noqa: E402
from quality_gates.smoke_process import CollectorConfig  # noqa: E402

# Fails (50 numbered lines, then exit 1) while its state file is absent, and
# passes once it exists. Every attempt appends whether the runner's host lock
# was held at that moment: the pooled first attempt must not hold it, the
# retry must.
_FLAKY = (
    "import fcntl, sys\n"
    "from pathlib import Path\n"
    "state = Path({state!r})\n"
    "lock = Path({lock!r})\n"
    "locked = False\n"
    "if lock.exists():\n"
    "    with lock.open('a+') as handle:\n"
    "        try:\n"
    "            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "        except BlockingIOError:\n"
    "            locked = True\n"
    "with Path({attempts!r}).open('a') as log:\n"
    "    log.write(f'locked={{locked}}\\n')\n"
    "if state.exists():\n"
    "    print('second attempt ok')\n"
    "    sys.exit(0)\n"
    "state.write_text('seen')\n"
    "for number in range(1, 51):\n"
    "    print(f'first failure line {{number}}')\n"
    "sys.exit(1)\n"
)
_ALWAYS_FAIL = "from pathlib import Path\nwith Path({attempts!r}).open('a') as log:\n    log.write('x\\n')\nprint('always failing')\nraise SystemExit(3)\n"
_SKIPPED = "from pathlib import Path\nwith Path({attempts!r}).open('a') as log:\n    log.write('x\\n')\nprint('disclosed skip')\nraise SystemExit(77)\n"
_GREEN = "print('green')\n"
# Exits like a timeout wrapper giving up; every attempt is logged, so a retry
# of this harness defect would be visible.
_HARNESS = "from pathlib import Path\nwith Path({attempts!r}).open('a') as log:\n    log.write('x\\n')\nprint('own timeout wrapper gave up')\nraise SystemExit({harness_exit})\n"
# Fails genuinely first; its one retry then exits like a timeout wrapper.
_FAIL_THEN_HARNESS = "import sys\nfrom pathlib import Path\nwith Path({attempts!r}).open('a') as log:\n    log.write('x\\n')\nstate = Path({state!r})\nif state.exists():\n    print('retry timeout wrapper gave up')\n    sys.exit({harness_exit})\nstate.write_text('seen')\nprint('first attempt fails')\nsys.exit(1)\n"
# Each attempt waits until the nested runner has logged more than one new
# attention event for it, recording at the start and end of the attempt
# whether the battery receipt still reads running with its lock held, and
# whether the host lock is held. It fails once at its own end.
_SLOW_FLAKY = (
    "import fcntl, json, sys, time\n"
    "from pathlib import Path\n"
    "def held(path):\n"
    "    with path.open('a+') as handle:\n"
    "        try:\n"
    "            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
    "        except BlockingIOError:\n"
    "            return True\n"
    "        fcntl.flock(handle, fcntl.LOCK_UN)\n"
    "        return False\n"
    "def observe(moment):\n"
    "    receipts = Path({receipts!r})\n"
    "    lock = Path({lock!r})\n"
    "    state = dict(moment=moment, receipt=[json.loads(path.read_text())['result'] for path in receipts.glob('*.json')], receipt_lock=[held(path) for path in receipts.glob('*.lock')], host_lock=lock.exists() and held(lock))\n"
    "    with Path({attempts!r}).open('a') as log:\n"
    "        log.write(json.dumps(state) + '\\n')\n"
    "attention = Path({attention!r})\n"
    "def attention_count():\n"
    "    return len(attention.read_text().splitlines()) if attention.exists() else 0\n"
    "observe('start')\n"
    "first = attention_count()\n"
    "while attention_count() <= first + 1:\n"
    "    time.sleep({poll})\n"
    "observe('end')\n"
    "state = Path({state!r})\n"
    "if state.exists():\n"
    "    print('slow retry ok')\n"
    "    sys.exit(0)\n"
    "state.write_text('seen')\n"
    "print('slow first attempt reached its own end')\n"
    "sys.exit(1)\n"
)
# Attention cadence is set through the collector's documented setting, and
# the slow fixture polls at the collector's own configured interval: it is
# released by real repeated attention, never by a fixed sleep.
_ATTENTION_FIXTURE_ENVIRONMENT = {"SMOKE_PROCESS_ATTENTION_SECONDS": "0.05"}
_FIXTURE_POLL_SECONDS = CollectorConfig.from_environment(_ATTENTION_FIXTURE_ENVIRONMENT).poll_seconds
_FAIL_THEN_SKIP = "import sys\nfrom pathlib import Path\nstate = Path({state!r})\nif state.exists():\n    print('retry reports a skip')\n    sys.exit(77)\nstate.write_text('seen')\nprint('first attempt fails')\nsys.exit(1)\n"

_RUNNER_SCRIPT = (
    "import sys\n"
    "sys.path.insert(0, {repo_root!r})\n"
    "from pathlib import Path\n"
    "from unittest.mock import patch\n"
    "from quality_gates import run_smokes\n"
    "root = Path({root!r})\n"
    "register = Path({register!r})\n"
    "original_read = run_smokes._read_register\n"
    "def read_register(path):\n"
    "    entries = original_read(path)\n"
    "    for name in {missing!r}:\n"
    "        (root / name).unlink()\n"
    "    return entries\n"
    "original_attention = run_smokes._report_attention\n"
    "def report_attention(smoke, event):\n"
    "    original_attention(smoke, event)\n"
    "    with (Path({work!r}) / (smoke.stem + '.attention')).open('a') as log:\n"
    "        log.write('attention\\n')\n"
    "with patch.object(run_smokes, '_REPO_ROOT', root), "
    "patch.object(run_smokes, '_read_register', side_effect=read_register), "
    "patch.object(run_smokes, '_report_attention', side_effect=report_attention), "
    "patch.object(run_smokes, '_DEFAULT_REGISTER', Path({default_register!r})), "
    "patch.object(run_smokes, '_venv_python', return_value=Path(sys.executable)), "
    "patch.object(sys, 'argv', ['run_smokes.py', '--register', str(register), "
    "'--campaign', 'retry-fixture'] + sys.argv[1:]):\n"
    "    raise SystemExit(run_smokes.main())\n"
)


@dataclass(frozen=True)
class _Run:
    exit_code: int
    stdout: str
    receipts: Path
    work: Path
    argv: tuple[str, ...]
    env: dict[str, str]

    def receipt(self) -> dict[str, Any]:
        files = list(self.receipts.glob("*.json"))
        _require(len(files) == 1, f"expected exactly one receipt, got {files}")
        loaded = json.loads(files[0].read_text(encoding="utf-8"))
        _require(isinstance(loaded, dict), str(loaded))
        return loaded

    def results(self) -> dict[str, Any]:
        """The ``--results-output`` campaign file of a run started with ``results=True``."""
        loaded = json.loads((self.work / "results.json").read_text(encoding="utf-8"))
        _require(isinstance(loaded, dict), str(loaded))
        return loaded

    def attempts(self, name: str) -> list[str]:
        path = self.work / f"{name}.attempts"
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    def retry_lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if "(retry," in line]

    def flaky_lines(self) -> list[str]:
        return [line for line in self.stdout.splitlines() if line.startswith("FLAKY    ")]


_CHECKS = [0]


def _require(condition: bool, message: str) -> None:
    _CHECKS[0] += 1
    if not condition:
        raise AssertionError(message)


@contextlib.contextmanager
def _run_battery(fixtures: dict[str, str], entries: list[str], *, focused: bool = False, attention: bool = False, extra: tuple[str, ...] = (), interrupt: str | None = None, results: bool = False) -> Generator[_Run]:
    """Run the real runner over ``entries``; ``focused`` makes it a non-default register.

    ``interrupt`` names the runner function that raises KeyboardInterrupt
    instead of running; ``results`` also writes the ``--results-output`` file.
    """
    with tempfile.TemporaryDirectory() as temporary:
        base = Path(temporary)
        root = base / "candidate"
        work = base / "work"
        receipts = base / "receipts"
        root.mkdir()
        work.mkdir()
        for name, template in fixtures.items():
            state = work / f"{name}.state"
            attempts = work / f"{name}.attempts"
            source = template.format(state=str(state), attempts=str(attempts), lock=str(base / "host.lock"), receipts=str(receipts), attention=str(work / f"{name}.attention"), poll=_FIXTURE_POLL_SECONDS, harness_exit=_HARNESS_DEFECT_EXIT_CODE)
            (root / f"{name}.py").write_text(source, encoding="utf-8")
        register = root / "register.txt"
        register.write_text("".join(f"{entry}\n" for entry in entries), encoding="utf-8")
        missing = [entry for entry in entries if not (root / entry).exists()]
        for entry in missing:
            (root / entry).write_text(_GREEN, encoding="utf-8")
        default_register = root / "default_register.txt" if focused else register
        script = _RUNNER_SCRIPT.format(
            repo_root=str(_REPO_ROOT),
            root=str(root),
            register=str(register),
            default_register=str(default_register),
            missing=missing,
            work=str(work),
        )
        if interrupt is not None:
            script = script.replace(
                "    raise SystemExit(run_smokes.main())",
                f"    with patch.object(run_smokes, {interrupt!r}, side_effect=KeyboardInterrupt):\n        raise SystemExit(run_smokes.main())",
            )
        if results:
            extra = (*extra, "--results-output", str(work / "results.json"))
        argv = (sys.executable, "-c", script, *extra)
        env = {
            **os.environ,
            "SOLET_NAME": "run-smokes-retry-fixture",
            "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipts),
            "RUN_SMOKES_HOST_SERIAL_LOCK_PATH": str(base / "host.lock"),
            **(_ATTENTION_FIXTURE_ENVIRONMENT if attention else {}),
        }
        completed = subprocess.run(
            argv,
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        yield _Run(completed.returncode, completed.stdout + completed.stderr, receipts, work, argv, env)


def _assert_dead_running_receipt_reclaimed() -> None:
    sys.path.insert(0, str(_REPO_ROOT))
    from quality_gates import run_smokes as runner

    with tempfile.TemporaryDirectory() as temporary:
        receipt_dir = Path(temporary)
        digest = "dead-running-receipt-digest"
        campaign = "dead-running-receipt-campaign"
        path = runner._receipt_path(receipt_dir, digest, campaign)
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = child.pid
        child.wait(timeout=10)
        previous = {
            "schema_version": 1,
            "candidate_digest": digest,
            "campaign": campaign,
            "started_at": "2026-10-07T22:34:54Z",
            "result": "running",
            "pid": dead_pid,
            "history_marker": "preserve every prior field",
        }
        path.write_text(json.dumps(previous), encoding="utf-8")
        output = io.StringIO()
        outcome: object
        with patch.object(runner, "_receipt_directory", return_value=receipt_dir), contextlib.redirect_stdout(output):
            try:
                outcome = runner._acquire_battery_receipt(digest, campaign, reauthorize=False)
            except runner.BatteryReceiptError as exc:
                outcome = exc
        acquired = outcome if isinstance(outcome, runner._BatteryReceipt) else None
        _require(acquired is not None, "DEAD_RUNNING_RECEIPT_RECLAIMED")
        if acquired is None:
            return
        replacement = json.loads(path.read_text(encoding="utf-8"))
        _require(replacement.get("result") == "running", "DEAD_RUNNING_RECEIPT_REPLACEMENT_RUNNING")
        _require(replacement.get("reauthorized_from") == previous, "DEAD_RUNNING_RECEIPT_PROVENANCE_PRESERVED")
        _require("STALE RUNNING BATTERY RECEIPT REPLACED" in output.getvalue(), "DEAD_RUNNING_RECEIPT_REPLACEMENT_REPORTED")

        # The acquired receipt owns one lock file description while the next
        # acquisition opens a second description and must refuse contention.
        live_outcome: object
        with patch.object(runner, "_receipt_directory", return_value=receipt_dir):
            try:
                live_outcome = runner._acquire_battery_receipt(digest, campaign, reauthorize=False)
            except runner.BatteryReceiptError as exc:
                live_outcome = exc
        if isinstance(live_outcome, runner._BatteryReceipt):
            live_outcome.close()
        _require(isinstance(live_outcome, runner.BatteryReceiptError) and "already running" in str(live_outcome), "LIVE_HOLDER_STILL_REFUSED")

        acquired.finish("passed")
        acquired.close()
        finished_outcome: object
        with patch.object(runner, "_receipt_directory", return_value=receipt_dir):
            try:
                finished_outcome = runner._acquire_battery_receipt(digest, campaign, reauthorize=False)
            except runner.BatteryReceiptError as exc:
                finished_outcome = exc
        if isinstance(finished_outcome, runner._BatteryReceipt):
            finished_outcome.close()
        _require(isinstance(finished_outcome, runner.BatteryReceiptError) and "already ran in campaign" in str(finished_outcome), "FINISHED_RECEIPT_STILL_REFUSED")


def _repeat(run: _Run, extra: tuple[str, ...] = ()) -> _Run:
    completed = subprocess.run((*run.argv, *extra), cwd=_REPO_ROOT, env=run.env, capture_output=True, text=True, timeout=120, check=False)
    return _Run(completed.returncode, completed.stdout + completed.stderr, run.receipts, run.work, run.argv, run.env)


def _assert_receipt_per_smoke_results() -> None:
    controlled = "from pathlib import Path\nstate = Path({state!r})\nwith Path({attempts!r}).open('a') as log:\n    log.write('attempt\\n')\nprint('PASS_OUTPUT' if state.exists() else 'FAIL_OUTPUT')\nraise SystemExit(0 if state.exists() else 1)\n"
    with _run_battery({"green": _GREEN, "controlled": controlled}, ["green.py", "controlled.py"], extra=("--jobs", "2", "--serial-only-override", "green.py")) as run:
        _require(run.exit_code == 1, run.stdout)
        receipt = run.receipt()
        _require(isinstance(receipt.get("smoke_results"), list), "RECEIPT_PER_SMOKE_RESULTS")
        rows = receipt["smoke_results"]
        _require([row["smoke"] for row in rows] == ["green.py", "controlled.py"], str(rows))
        _require([row["verdict"] for row in rows] == ["passed", "failed"], str(rows))
        _require([row["retried"] for row in rows] == [False, True], str(rows))
        _require(all(isinstance(row["seconds"], (int, float)) and row["seconds"] >= 0 for row in rows), str(rows))
        _require("green" in rows[0]["output_tail"], str(rows))
        _require("FAIL_OUTPUT" in rows[1]["output_tail"], str(rows))
        for index, row in enumerate(rows):
            _require(row["output_ref"] == f"#/smoke_results/{index}/output_tail", str(row))
            _require(receipt[row["output_ref"].split("/")[1]][index]["output_tail"] == row["output_tail"], str(row))
        _require((receipt["schema_version"], receipt["result"]) == (1, "failed"), str(receipt))
        attempts = run.attempts("controlled")
        refused = _repeat(run)
        _require(refused.exit_code == 2, refused.stdout)
        _require("failed/missing smokes=1: controlled.py" in refused.stdout, refused.stdout)
        _require("green.py" not in refused.stdout, refused.stdout)
        _require(run.attempts("controlled") == attempts, refused.stdout)
        (run.work / "controlled.state").write_text("ready", encoding="utf-8")
        fresh = _repeat(run, ("--reauthorize",))
        _require(fresh.exit_code == 0, fresh.stdout)
        updated = fresh.receipt()
        _require((updated["result"], updated["reauthorized_from"]["result"]) == ("passed", "failed"), str(updated))
        _require([(row["verdict"], row["retried"]) for row in updated["smoke_results"]] == [("passed", False), ("passed", False)], str(updated))
        _require("PASS_OUTPUT" in updated["smoke_results"][1]["output_tail"], str(updated))
        _require(len(list(run.receipts.glob("*.json"))) == len(list(run.receipts.glob("*.lock"))) == 1, str(updated))


def _assert_receipt_retry_and_output_bounds() -> None:
    sys.path.insert(0, str(_REPO_ROOT))
    from quality_gates import run_smokes as runner

    long_pass = "print('é' * 2048 + 'FINAL_PASS_OUTPUT')\n"
    with _run_battery({"flaky": _FLAKY, "green": long_pass}, ["flaky.py", "green.py"]) as run:
        _require(run.exit_code == 0, run.stdout)
        receipt = run.receipt()
        rows = receipt["smoke_results"]
        _require((receipt["result"], rows[0]["verdict"], rows[0]["retried"]) == ("passed_with_retries", "passed", True), str(receipt))
        _require("second attempt ok" in rows[0]["output_tail"], str(rows))
        _require(all(isinstance(row["seconds"], (int, float)) and row["seconds"] >= 0 for row in rows), str(rows))
        _require(rows[1]["output_tail"].endswith("FINAL_PASS_OUTPUT\n") and rows[1]["output_truncated"], str(rows))
        _require(len(rows[1]["output_tail"].encode("utf-8")) <= 1024, str(rows))
        retry = receipt["retries"][0]
        _require(set(retry) == {"smoke", "first_exit_or_verdict", "first_failure_tail", "retry_verdict", "retry_seconds"}, str(retry))
        _require("first failure line 50" in retry["first_failure_tail"], str(retry))
        print(f"receipt bound measurement: {next(run.receipts.glob('*.json')).stat().st_size} serialized bytes; tail={len(rows[1]['output_tail'].encode('utf-8'))} UTF-8 bytes")
    for output in ("", "é" * 600 + "\x00\nEND", "control\x1b[0m"):
        tail, truncated = runner._output_tail(output)
        _require(len(tail.encode("utf-8")) <= 1024 and truncated == (len(output.encode("utf-8")) > 1024), repr(tail))
        _require(output.endswith(tail), repr(tail))
    initial = runner._initial_smoke_results(["missing.py", "not_started.py"])
    _require(all(row["verdict"] == "not_run" and row["seconds"] is None and row["output_tail"] == "" for row in initial.values()), str(initial))
    runner._record_smoke_result(initial, "missing.py", "missing", "", None)
    _require(initial["missing.py"]["verdict"] == "missing" and initial["missing.py"]["seconds"] is None, str(initial))


def _assert_prior_receipt_detail_compatibility() -> None:
    sys.path.insert(0, str(_REPO_ROOT))
    from quality_gates import run_smokes as runner

    old = {"schema_version": 1, "started_at": "then", "result": "failed"}
    before = dict(old)
    for optional in ({}, {"smoke_results": "bad"}, {"smoke_results": [{}]}):
        detail = runner._prior_receipt_detail({**old, **optional})
        _require("at then; prior result=failed" in detail and "per-smoke detail unavailable" in detail, detail)
    rows = [{"smoke": f"fail{index}.py", "verdict": "failed"} for index in range(6)]
    rows.append({"smoke": "flaky_pass.py", "verdict": "passed"})
    detail = runner._prior_receipt_detail({**old, "smoke_results": rows})
    _require("failed/missing smokes=6: fail0.py, fail1.py, fail2.py, fail3.py, fail4.py" in detail and "1 omitted" in detail, detail)
    _require("fail5.py" not in detail and "flaky_pass.py" not in detail and old == before, detail)


def _assert_flaky_leg_passes_with_retries() -> None:
    with _run_battery({"green": _GREEN, "flaky": _FLAKY}, ["green.py", "flaky.py"]) as run:
        _require(run.exit_code == 0, run.stdout)
        receipt = run.receipt()
        _require(receipt["result"] == "passed_with_retries", str(receipt))
        _require(receipt["schema_version"] == 1, str(receipt))
        retries = receipt["retries"]
        _require(isinstance(retries, list) and len(retries) == 1, str(receipt))
        record = retries[0]
        _require(set(record) == {"smoke", "first_exit_or_verdict", "first_failure_tail", "retry_verdict", "retry_seconds"}, str(record))
        _require(record["smoke"] == "flaky.py" and record["retry_verdict"] == "passed", str(record))
        _require(record["first_exit_or_verdict"] == "failed" and isinstance(record["retry_seconds"], float), str(record))
        tail = str(record["first_failure_tail"]).splitlines()
        _require(len(tail) == 40 and tail[0] == "first failure line 11" and tail[-1] == "first failure line 50", str(tail))
        _require(len(run.flaky_lines()) == 1, run.stdout)
        flaky_line = run.flaky_lines()[0]
        _require(flaky_line.startswith("FLAKY    flaky.py  failed first (failed), passed on retry ("), flaky_line)
        _require("retrying 1 failed smoke(s) once, alone, under the host lock: flaky.py" in run.stdout, run.stdout)
        _require(len(run.retry_lines()) == 1 and run.retry_lines()[0].startswith("  ok      flaky.py  (retry, "), run.stdout)
        _require("smokes: 2/2 passed, 0/2 skipped" in run.stdout, run.stdout)
        _require("flaky: 1 (flaky.py)" in run.stdout and "failed:" not in run.stdout, run.stdout)
        # Walk-time lines are untouched: the first failure's FAIL line and tail are already on the screen.
        _require("  FAIL    flaky.py  (" in run.stdout and "first failure line 50" in run.stdout, run.stdout)
        _require(run.attempts("flaky") == ["locked=False", "locked=True"], str(run.attempts("flaky")))


def _assert_failed_retry_is_red_with_both_attempts_named() -> None:
    with _run_battery({"always_fail": _ALWAYS_FAIL, "green": _GREEN}, ["always_fail.py", "green.py"]) as run:
        _require(run.exit_code == 1, run.stdout)
        _require(len(run.attempts("always_fail")) == 2, str(run.attempts("always_fail")))
        receipt = run.receipt()
        _require(receipt["result"] == "failed", str(receipt))
        retries = receipt["retries"]
        _require(isinstance(retries, list) and len(retries) == 1, str(receipt))
        _require(retries[0]["retry_verdict"] == "failed" and "always failing" in retries[0]["first_failure_tail"], str(retries))
        _require(not run.flaky_lines() and "flaky:" not in run.stdout, run.stdout)
        _require(len(run.retry_lines()) == 1 and run.retry_lines()[0].startswith("  FAIL    always_fail.py  (retry, "), run.stdout)
        _require("failed: 1 (always_fail.py)" in run.stdout and "smokes: 1/2 passed, 0/2 skipped" in run.stdout, run.stdout)


def _assert_cap_is_three_first_time_failures() -> None:
    names = ["fail_a", "fail_b", "fail_c", "fail_d"]
    with _run_battery(dict.fromkeys(names, _ALWAYS_FAIL), [f"{name}.py" for name in names]) as run:
        _require(run.exit_code == 1, run.stdout)
        _require(all(len(run.attempts(name)) == 1 for name in names), str([run.attempts(name) for name in names]))
        _require("retry skipped: 4 first-time failures exceed the cap of 3" in run.stdout, run.stdout)
        _require("retrying" not in run.stdout and not run.retry_lines() and not run.flaky_lines(), run.stdout)
        receipt = run.receipt()
        _require(receipt["result"] == "failed" and "retries" not in receipt, str(receipt))
        _require("failed: 4 (fail_a.py, fail_b.py, fail_c.py, fail_d.py)" in run.stdout, run.stdout)


def _assert_exactly_three_first_time_failures_are_all_retried() -> None:
    flaky = ["flake_a", "flake_b", "flake_c"]
    with _run_battery(dict.fromkeys(flaky, _FLAKY), [f"{name}.py" for name in flaky]) as run:
        _require(run.exit_code == 0, run.stdout)
        _require(len(run.flaky_lines()) == 3 and len(run.retry_lines()) == 3, run.stdout)
        _require(run.receipt()["result"] == "passed_with_retries", run.stdout)
        _require("flaky: 3 (flake_a.py, flake_b.py, flake_c.py)" in run.stdout, run.stdout)


def _assert_skipped_is_never_retried() -> None:
    with _run_battery({"skipped": _SKIPPED, "flaky": _FLAKY}, ["skipped.py", "flaky.py"]) as run:
        _require(run.exit_code == 0, run.stdout)
        _require(len(run.attempts("skipped")) == 1, str(run.attempts("skipped")))
        _require(len(run.retry_lines()) == 1 and "skipped.py" not in " ".join(run.retry_lines()), run.stdout)
        _require("skipped: 1 (skipped.py)" in run.stdout, run.stdout)
        _require(run.receipt()["result"] == "passed_with_retries", run.stdout)
    with _run_battery({"skipped": _SKIPPED}, ["skipped.py"]) as only_skip:
        _require(only_skip.exit_code == 0 and "retrying" not in only_skip.stdout, only_skip.stdout)
        _require(only_skip.receipt()["result"] == "passed" and "retries" not in only_skip.receipt(), only_skip.stdout)


def _assert_attention_never_retries_and_locks_outlive_it() -> None:
    """Attention neither stops nor retries; only the terminal failure is retried."""
    with _run_battery({"slow": _SLOW_FLAKY, "green": _GREEN}, ["slow.py", "green.py"], attention=True) as run:
        receipt = run.receipt()
        retries = [(retry["retry_verdict"], "slow first attempt reached its own end" in retry["first_failure_tail"]) for retry in receipt.get("retries", [])]
        attention = [line.endswith("observation only, the smoke is not stopped or retried") for line in run.stdout.splitlines() if line.startswith("ATTENTION ") and line.split(":", 1)[0].endswith("slow.py")]
        observed = (run.exit_code, receipt["result"], retries, len(run.retry_lines()), len(run.flaky_lines()), len(attention) > 3, all(attention), "TIMEOUT" in run.stdout or "CANCELLED" in run.stdout)
        _require(observed == (0, "passed_with_retries", [("passed", True)], 1, 1, True, True, False), f"{observed}\n{run.stdout}")
        # Each attempt ran to its own end with the receipt running and locked;
        # only the alone, host-locked retry held the host lock throughout.
        states = [(state["moment"], state["receipt"], state["receipt_lock"], state["host_lock"]) for state in map(json.loads, run.attempts("slow"))]
        held = (["running"], [True])
        _require(states == [("start", *held, False), ("end", *held, False), ("start", *held, True), ("end", *held, True)], str(states))


def _assert_harness_exit_is_never_a_verdict_or_retried() -> None:
    """A smoke's own exit 124 is a disclosed harness defect: never failed, never retried."""
    with _run_battery({"harness": _HARNESS, "green": _GREEN}, ["harness.py", "green.py"], results=True) as run:
        receipt = run.receipt()
        rows = {row["smoke"]: row for row in receipt["smoke_results"]}
        observed = (run.exit_code, receipt["result"], receipt.get("harness_defects"), "retries" in receipt, run.attempts("harness"), rows["harness.py"]["verdict"], run.retry_lines())
        _require(observed == (0, "passed", ["harness.py"], False, ["x"], "not_run", []), f"{observed}\n{run.stdout}")
        disclosed = (f"HARNESS-DEFECT: exit {_HARNESS_DEFECT_EXIT_CODE}" in rows["harness.py"]["output_tail"], "  HARNESS harness.py  (" in run.stdout, "harness: 1 (harness.py)" in run.stdout, "smokes: 1/2 passed, 0/2 skipped" in run.stdout)
        _require(disclosed == (True, True, True, True), f"{disclosed}\n{run.stdout}")
        # The campaign file carries the receipt's classification, not just its rows.
        results = run.results()
        for result_row in results["smoke_results"]:
            attempt_code = result_row["attempts"][-1]["exit_code"]
            _require(result_row["exit_code"] == attempt_code, str(result_row))
        campaign = (results.get("harness_defects"), results["result"], results["exit_code"], [row["verdict"] for row in results["smoke_results"]])
        _require(campaign == (["harness.py"], "passed", 0, ["not_run", "passed"]), f"CAMPAIGN_HARNESS_DEFECTS {campaign}")


def _assert_harness_retry_keeps_first_failure_and_is_disclosed() -> None:
    """A retry exiting 124 is an unmeasured, disclosed harness defect; the genuine first failure stays red."""
    with _run_battery({"fail_then_harness": _FAIL_THEN_HARNESS}, ["fail_then_harness.py"], results=True) as run:
        receipt = run.receipt()
        row, retry = receipt["smoke_results"][0], receipt["retries"][0]
        observed = (run.exit_code, receipt["result"], receipt.get("harness_defects"), row["verdict"], row["retried"], receipt["summary"]["not_run"], retry["retry_verdict"], run.attempts("fail_then_harness"))
        _require(observed == (1, "failed", ["fail_then_harness.py"], "not_run", True, 1, "harness", ["x", "x"]), f"{observed}\n{run.stdout}")
        kept = ("first attempt fails" in retry["first_failure_tail"], f"HARNESS-DEFECT: exit {_HARNESS_DEFECT_EXIT_CODE}" in row["output_tail"], "failed: 1 (fail_then_harness.py)" in run.stdout, "harness: 1 (fail_then_harness.py)" in run.stdout)
        _require(kept == (True, True, True, True), f"{kept}\n{run.stdout}")
        retry_lines = run.retry_lines()
        _require(len(retry_lines) == 1 and retry_lines[0].startswith("  HARNESS fail_then_harness.py  (retry, "), f"RETRY_LINE_HARNESS {retry_lines}")
        results = run.results()
        for result_row in results["smoke_results"]:
            attempt_code = result_row["attempts"][-1]["exit_code"]
            _require(result_row["exit_code"] == attempt_code, str(result_row))
        campaign = (results.get("harness_defects"), results["result"], results["exit_code"])
        _require(campaign == (["fail_then_harness.py"], "failed", 1), f"CAMPAIGN_RETRY_HARNESS_DEFECTS {campaign}")


def _assert_interrupted_battery_keeps_observed_harness_defects() -> None:
    """An interruption after a defect was classified still publishes it on the receipt and the campaign file."""
    with _run_battery({"harness": _HARNESS, "green": _GREEN}, ["harness.py", "green.py"], interrupt="_retry_failures", results=True) as run:
        receipt, results = run.receipt(), run.results()
        observed = (run.exit_code != 0, receipt["result"], receipt.get("harness_defects"), results["result"], results.get("harness_defects"), [row["verdict"] for row in results["smoke_results"]])
        _require(observed == (True, "interrupted", ["harness.py"], "interrupted", ["harness.py"], ["not_run", "passed"]), f"INTERRUPTED_HARNESS_DEFECTS {observed}\n{run.stdout}")


def _assert_retry_that_reports_skip_stays_red() -> None:
    with _run_battery({"fail_then_skip": _FAIL_THEN_SKIP}, ["fail_then_skip.py"]) as run:
        _require(run.exit_code == 1 and not run.flaky_lines(), run.stdout)
        receipt = run.receipt()
        _require(receipt["result"] == "failed" and receipt["retries"][0]["retry_verdict"] == "skipped", str(receipt))
        _require("failed: 1 (fail_then_skip.py)" in run.stdout, run.stdout)


def _assert_focused_register_never_retries() -> None:
    with _run_battery({"flaky": _FLAKY}, ["flaky.py"], focused=True) as run:
        _require(run.exit_code == 1, run.stdout)
        _require(len(run.attempts("flaky")) == 1, str(run.attempts("flaky")))
        _require("retry" not in run.stdout and not run.flaky_lines() and "flaky:" not in run.stdout, run.stdout)
        _require(not list(run.receipts.glob("*.json")), "a focused register must not claim a battery receipt")
        _require("failed: 1 (flaky.py)" in run.stdout, run.stdout)


def _assert_green_run_is_unchanged() -> None:
    with _run_battery({"green": _GREEN}, ["green.py"], results=True) as run:
        _require(run.exit_code == 0, run.stdout)
        receipt = run.receipt()
        _require(receipt["result"] == "passed" and "retries" not in receipt, str(receipt))
        # An empty list says no harness defect was observed; it is never left out.
        _require((receipt.get("harness_defects"), run.results().get("harness_defects")) == ([], []), "EMPTY_HARNESS_DEFECTS_PRESENT")
        _require("smokes: 1/1 passed, 0/1 skipped" in run.stdout, run.stdout)
        _require("retry" not in run.stdout and "FLAKY" not in run.stdout and "flaky" not in run.stdout, run.stdout)


def _assert_typed_summary() -> None:
    with _run_battery({"green": _GREEN}, ["green.py"]) as run:
        _require(run.exit_code == 0, run.stdout)
        receipt = run.receipt()
        _require(isinstance(receipt.get("summary"), dict), "BATTERY_SUMMARY_PRESENT")
        _check_summary(run, [1, 0, 0, 0, 0], [], [], [])
        _require(receipt["repeat_flaky"]["state"] == "unavailable", "NO_PRIOR_DISCLOSED")
    cases = [
        ({"flaky": _FLAKY}, ["flaky.py"], (), [1, 0, 0, 0, 0], ["flaky.py"], ["flaky.py"], []),
        ({"load_sensitive": _ALWAYS_FAIL}, ["load_sensitive.py"], (), [0, 0, 1, 0, 0], ["load_sensitive.py"], [], ["load_sensitive.py"]),
        ({"flaky": _FLAKY, "red": _ALWAYS_FAIL}, ["flaky.py", "red.py"], (), [1, 0, 1, 0, 0], ["flaky.py", "red.py"], ["flaky.py"], ["red.py"]),
        ({}, ["missing.py"], (), [0, 0, 0, 1, 0], [], [], ["missing.py"]),
        ({"skip": _SKIPPED}, ["skip.py"], (), [0, 1, 0, 0, 0], [], [], []),
        ({"skip": _SKIPPED}, ["skip.py"], ("--fail-on-skip",), [0, 1, 0, 0, 0], [], [], []),
        ({"harness": _HARNESS}, ["harness.py"], (), [0, 0, 0, 0, 1], [], [], []),
    ]
    for fixtures, entries, extra, counts, retried, flaky, failing in cases:
        with _run_battery(fixtures, entries, extra=extra) as run:
            _check_summary(run, counts, retried, flaky, failing)
    with _run_battery({"green": _GREEN}, ["green.py"], interrupt="_run_suite_with_retry") as run:
        _require(run.exit_code != 0, run.stdout)
        summary = run.receipt()["summary"]
        _require(
            summary
            == {
                "exit_code": None,
                "result": "interrupted",
                "total": 1,
                "passed": 0,
                "skipped": 0,
                "failed": 0,
                "missing": 0,
                "not_run": 1,
                "retried_smokes": [],
                "retried_count": 0,
                "flaky_smokes": [],
                "flaky_count": 0,
                "failing_smokes": [],
            },
            "INTERRUPTED_SUMMARY_EXACT",
        )


def _check_summary(run: _Run, counts: list[int], retried: list[str], flaky: list[str], failing: list[str]) -> None:
    passed, skipped, failed, missing, not_run = counts
    exit_code = int(bool(failed or missing or (skipped and "--fail-on-skip" in run.argv)))
    result = "failed" if exit_code else "passed_with_retries" if flaky else "passed"
    expected = {
        "exit_code": exit_code,
        "result": result,
        "total": sum(counts),
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "missing": missing,
        "not_run": not_run,
        "retried_smokes": retried,
        "retried_count": len(retried),
        "flaky_smokes": flaky,
        "flaky_count": len(flaky),
        "failing_smokes": failing,
    }
    _require(run.exit_code == exit_code, "BATTERY_EXIT_EXACT " + run.stdout)
    receipt = run.receipt()
    _require(receipt["result"] == result, "BATTERY_RESULT_EXACT")
    _require(receipt["summary"] == expected, "BATTERY_SUMMARY_EXACT " + str(receipt["summary"]))


def _assert_repeat_flaky() -> None:
    with _run_battery({"flaky": _FLAKY}, ["flaky.py"]) as run:
        prior = run.receipt()
        original = json.dumps(prior, sort_keys=True)
        (run.work / "flaky.state").unlink()
        repeated = _repeat(run, ("--reauthorize",))
        _require(repeated.exit_code == 0, repeated.stdout)
        receipt = repeated.receipt()
        _require(
            receipt["repeat_flaky"]
            == {
                "state": "available",
                "scope": "current_and_immediate_prior_receipts",
                "prior_candidate_digest": prior["candidate_digest"],
                "prior_campaign": prior["campaign"],
                "smokes": ["flaky.py"],
                "count": 1,
                "reason": None,
            },
            "REPEAT_FLAKY_EXACT",
        )
        _require(json.dumps(receipt["reauthorized_from"], sort_keys=True) == original, "PRIOR_BYTES_UNCHANGED")
        path = next(run.receipts.glob("*.json"))
        disjoint = {**prior, "reauthorized_from": prior, "smoke_results": [{"smoke": "other.py", "verdict": "passed", "retried": True}]}
        path.write_text(json.dumps(disjoint), encoding="utf-8")
        (run.work / "flaky.state").unlink()
        fresh = _repeat(run, ("--reauthorize",))
        advisory = fresh.receipt()["repeat_flaky"]
        _require(fresh.exit_code == 0 and advisory["state"] == "available" and advisory["smokes"] == [] and advisory["count"] == 0, "DISJOINT_HISTORY_EMPTY")
        for detail in (None, "bad", [{}], [{"smoke": "flaky.py", "verdict": "passed", "retried": "bad"}]):
            historical = {**prior, "smoke_results": detail}
            path.write_text(json.dumps(historical), encoding="utf-8")
            (run.work / "flaky.state").unlink()
            unknown = _repeat(run, ("--reauthorize",))
            _require(unknown.exit_code == 0, "UNKNOWN_HISTORY_NONBLOCKING " + unknown.stdout)
            receipt = unknown.receipt()
            advisory = receipt["repeat_flaky"]
            _require(advisory["state"] == "unavailable" and advisory["reason"] and advisory["smokes"] == [] and advisory["count"] == 0, "UNKNOWN_HISTORY_DISCLOSED")
            _require(receipt["reauthorized_from"] == historical, "MALFORMED_PRIOR_UNCHANGED")


class ExitCodeResultsTest(unittest.TestCase):
    def test_results_file_keeps_child_codes_and_retry_history(self) -> None:
        fixtures = {
            "green": _GREEN,
            "nonzero": _ALWAYS_FAIL,
            "terminated": "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n",
            "flaky": _FLAKY,
        }
        entries = ["green.py", "nonzero.py", "terminated.py", "flaky.py", "missing.py"]
        with _run_battery(fixtures, entries, results=True) as run:
            self.assertEqual(run.exit_code, 1, run.stdout)
            rows = {row["smoke"]: row for row in run.results()["smoke_results"]}
            expected = {"green.py": [0], "nonzero.py": [3, 3], "terminated.py": [-signal.SIGTERM, -signal.SIGTERM], "flaky.py": [1, 0], "missing.py": [None]}
            for entry, codes in expected.items():
                with self.subTest(entry=entry):
                    self.assertEqual(rows[entry]["exit_code"], codes[-1])
                    self.assertEqual([attempt["exit_code"] for attempt in rows[entry]["attempts"]], codes)
                    for attempt, code in zip(rows[entry]["attempts"], codes, strict=True):
                        retained = json.loads(Path(attempt["raw_attempt_path"]).read_text(encoding="utf-8"))
                        self.assertEqual(retained["exit_code"], code)
            self.assertEqual(rows["missing.py"]["verdict"], "missing")
            self.assertEqual(rows["flaky.py"]["verdict"], "passed")


def main() -> None:
    result = unittest.TextTestRunner().run(unittest.defaultTestLoader.loadTestsFromTestCase(ExitCodeResultsTest))
    if not result.wasSuccessful():
        raise SystemExit(1)
    _assert_typed_summary()
    _assert_repeat_flaky()
    _assert_dead_running_receipt_reclaimed()
    _assert_receipt_per_smoke_results()
    _assert_receipt_retry_and_output_bounds()
    _assert_prior_receipt_detail_compatibility()
    _assert_flaky_leg_passes_with_retries()
    _assert_failed_retry_is_red_with_both_attempts_named()
    _assert_cap_is_three_first_time_failures()
    _assert_exactly_three_first_time_failures_are_all_retried()
    _assert_skipped_is_never_retried()
    _assert_attention_never_retries_and_locks_outlive_it()
    _assert_harness_exit_is_never_a_verdict_or_retried()
    _assert_harness_retry_keeps_first_failure_and_is_disclosed()
    _assert_interrupted_battery_keeps_observed_harness_defects()
    _assert_retry_that_reports_skip_stays_red()
    _assert_focused_register_never_retries()
    _assert_green_run_is_unchanged()
    print(f"run-smokes battery retry smoke: {_CHECKS[0]} checks passed")


if __name__ == "__main__":
    main()
