#!/usr/bin/env python3
"""Regression coverage for the shared "battery ran under contention" signal.

Covers ``quality_gates/contention_signal.py`` directly, plus its integration
into ``run_smokes.py``'s battery receipt — the mechanism gates-cluster-A5
(part 1) adds for iss_35dbc06e/342, iss_dfca317c/357, iss_c35292ca/358,
iss_cf3a897d/756, and iss_0ecb55dc/760. See ``contention_signal.py``'s module
docstring for how each issue maps onto what this file checks; the
holder-liveness-specific coverage for 357/358 lives beside the mechanism it
tests, in ``run_smokes_host_lock_smoke.py``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from io import TextIOBase
from pathlib import Path
from typing import NoReturn, cast
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "quality_gates"))
import contention_signal  # noqa: E402


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# The longest measured smoke in the recent receipt corpus was 1171.5s
# (384be2fed4401a7e8643727eef71af0e2a493995cf76985a42b192d91a68761b.cd33699f18f35aa7.json).
# This is only an outer safety ceiling for a missing child terminal record; the
# receipt remains the source of completion and the expiry is advisory-labelled.
_CHILD_TERMINAL_WAIT_CEILING_SECONDS = 1800.0


def _read_terminal_receipt(receipt_path: Path) -> dict[str, object] | None:
    candidates = receipt_path.glob("*.json") if receipt_path.is_dir() else (receipt_path,)
    for candidate in candidates:
        try:
            receipt = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(receipt, dict) and "finished_at" in receipt and "result" in receipt:
            return receipt
    return None


def _wait_for_terminal_receipt(
    process: subprocess.Popen[str],
    receipt_path: Path,
    *,
    ceiling_seconds: float = _CHILD_TERMINAL_WAIT_CEILING_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    read_output: Callable[[], tuple[str, str]] | None = None,
) -> tuple[dict[str, object], str, str]:
    def fail_hang() -> NoReturn:
        process.kill()
        try:
            output = process.communicate(timeout=ceiling_seconds)
        except subprocess.TimeoutExpired:
            output = ("", "child did not exit after kill")
        stdout, stderr = read_output() if read_output is not None else output
        raise AssertionError(f"child hang after terminal receipt: elapsed={clock() - started:.3f}s ceiling={ceiling_seconds:.1f}s; receipt={receipt}; stdout={stdout}; stderr={stderr}")

    def finish_output() -> tuple[str, str]:
        remaining = ceiling_seconds - (clock() - started)
        if remaining <= 0:
            fail_hang()
        try:
            output = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            fail_hang()
        return read_output() if read_output is not None else output

    started = clock()
    last_progress = "no terminal receipt observed"
    while True:
        receipt = _read_terminal_receipt(receipt_path)
        if receipt is not None:
            stdout, stderr = finish_output()
            _require(process.returncode == 0, f"child exited {process.returncode} with terminal receipt {receipt}; stdout={stdout}; stderr={stderr}")
            return receipt, stdout, stderr
        running = process.poll()
        elapsed = clock() - started
        if running is not None:
            receipt = _read_terminal_receipt(receipt_path)
            if receipt is not None:
                stdout, stderr = finish_output()
                _require(process.returncode == 0, f"child exited {process.returncode} with terminal receipt {receipt}; stdout={stdout}; stderr={stderr}")
                return receipt, stdout, stderr
            stdout, stderr = finish_output()
            raise AssertionError(f"child exited {running} without terminal receipt after {elapsed:.3f}s; last progress: {last_progress}; stdout={stdout}; stderr={stderr}")
        if elapsed >= ceiling_seconds:
            process.kill()
            stdout, stderr = finish_output()
            raise AssertionError(f"ADVISORY: child terminal receipt wait ceiling expired; elapsed={elapsed:.3f}s ceiling={ceiling_seconds:.1f}s; last progress: {last_progress}; stdout={stdout}; stderr={stderr}")
        last_progress = f"child still running at {elapsed:.3f}s"
        sleeper(0.05)


def _assert_terminal_wait_replaces_fixed_timeout() -> int:
    """A delayed, progressing child may finish after the old 60s fuse."""

    class _Clock:
        now = 0.0

        def read(self) -> float:
            return self.now

        def sleep(self, seconds: float) -> None:
            self.now += seconds

    class _DelayedProcess:
        returncode: int | None = None

        def __init__(self, clock: _Clock, receipt_path: Path) -> None:
            self.clock = clock
            self.receipt_path = receipt_path
            self.killed = False

        def poll(self) -> int | None:
            if self.clock.now >= 65.0 and not self.receipt_path.exists():
                self.receipt_path.write_text(
                    json.dumps(
                        {
                            "started_at": "fixture-start",
                            "finished_at": "fixture-finish",
                            "result": "passed",
                            "smoke_results": [{"seconds": 65.0, "verdict": "passed"}],
                        }
                    ),
                    encoding="utf-8",
                )
                self.returncode = 0
            return self.returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if timeout is not None and self.clock.now < 65.0:
                raise subprocess.TimeoutExpired("fixture-child", timeout)
            return "child progress", ""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    with tempfile.TemporaryDirectory() as temp_dir:
        receipt_path = Path(temp_dir) / "receipt.json"
        clock = _Clock()
        process = cast(subprocess.Popen[str], _DelayedProcess(clock, receipt_path))
        try:
            process.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            old_timeout_fired = True
        else:
            old_timeout_fired = False
        _require(old_timeout_fired, "red-first fixture did not reproduce the old 60s TimeoutExpired")
        receipt, stdout, stderr = _wait_for_terminal_receipt(process, receipt_path, ceiling_seconds=120.0, clock=clock.read, sleeper=clock.sleep)
        _require(receipt["result"] == "passed", str(receipt))
        _require(receipt["finished_at"] == "fixture-finish", str(receipt))
        _require(stdout == "child progress" and stderr == "", str((stdout, stderr)))
    return 1


def _assert_passed_receipt_cannot_mask_failed_child() -> int:
    class _FailedChild:
        returncode = 7

        def poll(self) -> int:
            return self.returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "child stdout", "child stderr"

    with tempfile.TemporaryDirectory() as temp_dir:
        receipt_path = Path(temp_dir) / "receipt.json"
        receipt_path.write_text(json.dumps({"finished_at": "fixture-finish", "result": "passed"}), encoding="utf-8")
        try:
            _wait_for_terminal_receipt(cast(subprocess.Popen[str], _FailedChild()), receipt_path)
        except AssertionError as exc:
            _require("child exited 7" in str(exc) and "child stderr" in str(exc), f"failed child diagnostics missing: {exc}")
        else:
            raise AssertionError("passed receipt masked nonzero child exit")
    return 1


def _assert_terminal_receipt_cannot_mask_hung_child() -> int:
    class _HungChild:
        returncode: int | None = None
        killed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            if not self.killed:
                assert timeout is not None, "hung-child control needs a bounded wait"
                raise subprocess.TimeoutExpired("fixture-child", timeout)
            self.returncode = -9
            return "child stdout", "child stderr"

        def kill(self) -> None:
            self.killed = True

    with tempfile.TemporaryDirectory() as temp_dir:
        receipt_path = Path(temp_dir) / "receipt.json"
        receipt_path.write_text(json.dumps({"finished_at": "fixture-finish", "result": "passed"}), encoding="utf-8")
        process = _HungChild()
        try:
            _wait_for_terminal_receipt(cast(subprocess.Popen[str], process), receipt_path, ceiling_seconds=_CHILD_TERMINAL_WAIT_CEILING_SECONDS)
        except AssertionError as exc:
            _require("child hang after terminal receipt" in str(exc) and "child stderr" in str(exc), f"hung child diagnostics missing: {exc}")
        else:
            raise AssertionError("passed receipt masked hung child")
        _require(process.killed, "hung child was not killed")
    return 1


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _assert_liveness_probe() -> int:
    """``holder_pid_is_alive`` / ``parse_lock_holder_pid`` behave as the
    holder-liveness fix (iss_dfca317c/357) and the concurrent-count filter
    (iss_cf3a897d/756, iss_0ecb55dc/760) both depend on."""
    alive_pid = os.getpid()
    dead_pid = _dead_pid()
    _require(contention_signal.holder_pid_is_alive(alive_pid), "this process's own pid must read alive")
    _require(not contention_signal.holder_pid_is_alive(dead_pid), "an already-exited child's pid must read dead")
    _require(
        contention_signal.parse_lock_holder_pid(f"pid={alive_pid} cwd=/somewhere") == alive_pid,
        "well-formed holder text must parse the pid",
    )
    _require(
        contention_signal.parse_lock_holder_pid("cwd=/somewhere no-pid-token-here") is None,
        "text with no pid= token must parse to None, not a wrong guess",
    )
    return 4


def _assert_receipt_counting_excludes_dead_finished_and_self() -> int:
    """A stale ("running" but crashed), a finished, and self's own receipt
    must all be excluded — only a genuinely live OTHER battery counts."""
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        (directory / "live.json").write_text(json.dumps({"result": "running", "pid": os.getpid()}), encoding="utf-8")
        (directory / "dead.json").write_text(json.dumps({"result": "running", "pid": _dead_pid()}), encoding="utf-8")
        (directory / "finished.json").write_text(json.dumps({"result": "passed", "pid": os.getpid()}), encoding="utf-8")
        self_path = directory / "self.json"
        self_path.write_text(json.dumps({"result": "running", "pid": os.getpid()}), encoding="utf-8")

        count = contention_signal.count_concurrent_live_batteries(directory, exclude_path=self_path)
        _require(count == 1, f"expected exactly the live sibling counted, got {count}")
    return 1


def _assert_battery_receipt_carries_contention() -> int:
    """A matched-default-register run's own receipt gets a ``contention`` key
    shaped as the mechanism promises — the "surfaced on the run's own
    result/receipt" half of the brief, checked structurally rather than
    asserted-and-hoped."""
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        register = directory / "register.txt"
        register.write_text("quality_gates/tests/run_smokes_git_environment_smoke.py\n", encoding="utf-8")
        receipts = directory / "receipts"
        environment = {
            "SOLET_NAME": "contention-signal-fixture",
            "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipts),
        }
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file, tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                [
                    str(_REPO_ROOT / ".venv" / "bin" / "python3"),
                    "quality_gates/run_smokes.py",
                    "--register",
                    str(register),
                    "--jobs",
                    "1",
                    "--campaign",
                    "contention-signal-fixture",
                ],
                cwd=_REPO_ROOT,
                env={**os.environ, **environment, "RUN_SMOKES_DEFAULT_REGISTER_PATH": str(register)},
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
            )

            def read_output() -> tuple[str, str]:
                stdout_file.seek(0)
                stderr_file.seek(0)
                return stdout_file.read(), stderr_file.read()

            receipt, stdout, stderr = _wait_for_terminal_receipt(process, receipts, read_output=read_output)
            _require(process.returncode == 0, f"fixture battery child exited {process.returncode}: {stdout}{stderr}")
        _require(receipt["result"] == "passed", f"fixture battery did not pass: {stdout}{stderr}")
        receipt_files = list(receipts.glob("*.json"))
        _require(len(receipt_files) == 1, f"expected exactly one receipt, got {receipt_files}")
        receipt = json.loads(receipt_files[0].read_text(encoding="utf-8"))
        _require(isinstance(receipt.get("pid"), int), receipt)
        contention = receipt.get("contention")
        _require(isinstance(contention, dict), receipt)
        for key in (
            "measured_at",
            "concurrent_live_batteries_start",
            "concurrent_live_batteries_finish",
            "concurrent_live_batteries_peak",
            "host_lock_wait_seconds",
            "host_lock_contended",
            "host_load",
        ):
            _require(key in contention, f"receipt contention block missing {key!r}: {contention}")
        _require(contention["concurrent_live_batteries_start"] == 0, contention)
        _require(contention["host_lock_contended"] is False, contention)
        load = contention["host_load"]
        _require(isinstance(load, dict) and bool(load), contention)
        _require(load["sample_count"] >= 2 and load["logical_cpu_count"] > 0, str(load))
        _require(load["max_sampled_1m"] >= max(load["start_1m"], load["finish_1m"]), str(load))
        _require("overall host load1m" in stdout, stdout)
    return 12


def _assert_receipt_case_outlives_old_timeout() -> int:
    """Drive the actual receipt case with a child completing at virtual 65s."""

    def terminal_receipt(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fixture.json").write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "finished_at": "fixture-finish",
                    "result": "passed",
                    "contention": {
                        "measured_at": "fixture-time",
                        "concurrent_live_batteries_start": 0,
                        "concurrent_live_batteries_finish": 0,
                        "concurrent_live_batteries_peak": 0,
                        "host_lock_wait_seconds": 0.0,
                        "host_lock_contended": False,
                        "host_load": {
                            "sample_count": 2,
                            "logical_cpu_count": 1,
                            "start_1m": 1.0,
                            "finish_1m": 1.0,
                            "max_sampled_1m": 1.0,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

    class DelayedBattery:
        returncode: int | None = None

        def __init__(self, **kwargs: object) -> None:
            env = kwargs["env"]
            _require(isinstance(env, dict), "fixture launcher missing environment")
            self.receipts = Path(cast(dict[str, str], env)["RUN_SMOKES_BATTERY_RECEIPT_DIR"])
            self.stdout = kwargs.get("stdout")
            self.elapsed_seconds = 0

        def poll(self) -> int | None:
            self.elapsed_seconds += 5
            if self.elapsed_seconds == 65:
                terminal_receipt(self.receipts)
                if isinstance(self.stdout, TextIOBase):
                    self.stdout.write("overall host load1m fixture\n")
                    self.stdout.flush()
                self.returncode = 0
            return self.returncode

        def __enter__(self) -> DelayedBattery:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def communicate(self, _input: object = None, timeout: float | None = None) -> tuple[str, str]:
            if timeout == 60:
                self.elapsed_seconds = 60
                raise subprocess.TimeoutExpired("fixture-child", timeout)
            return "", ""

        def kill(self) -> None:
            # The pre-fix parent requests a kill at 60s; model delayed child
            # cleanup so its terminal receipt is observable at virtual 65s.
            _require(self.elapsed_seconds == 60, "fixture child was killed before its timeout")

        def wait(self) -> int:
            while self.poll() is None:
                pass
            _require(len(list(self.receipts.glob("*.json"))) == 1, "delayed child did not reach its terminal receipt")
            return cast(int, self.returncode)

    def launch(*_args: object, **kwargs: object) -> DelayedBattery:
        return DelayedBattery(**kwargs)

    with patch.object(subprocess, "Popen", side_effect=launch):
        _require(_assert_battery_receipt_carries_contention() == 12, "receipt case assertions were skipped")
    return 1


_CONCURRENT_BATTERY_RUNNER_SCRIPT = (
    "import sys\n"
    "sys.path.insert(0, {repo_root!r})\n"
    "from pathlib import Path\n"
    "from unittest.mock import patch\n"
    "from quality_gates import run_smokes\n"
    "root = Path({root!r})\n"
    "register = Path({register!r})\n"
    "with patch.object(run_smokes, '_REPO_ROOT', root), "
    "patch.object(run_smokes, '_DEFAULT_REGISTER', register), "
    "patch.object(run_smokes, '_venv_python', return_value=Path(sys.executable)), "
    "patch.object(sys, 'argv', ['run_smokes.py', '--register', str(register), "
    "'--jobs', '1', '--campaign', {campaign!r}]):\n"
    "    raise SystemExit(run_smokes.main())\n"
)


def _launch_candidate_battery(*, repo_root: Path, root: Path, register: Path, campaign: str, receipts: Path) -> subprocess.Popen[str]:
    script = _CONCURRENT_BATTERY_RUNNER_SCRIPT.format(repo_root=str(repo_root), root=str(root), register=str(register), campaign=campaign)
    environment = {
        **os.environ,
        "SOLET_NAME": f"contention-signal-{campaign}",
        "RUN_SMOKES_BATTERY_RECEIPT_DIR": str(receipts),
    }
    return subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=repo_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_running_receipt(receipts: Path, *, exclude: set[Path], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for candidate in receipts.glob("*.json"):
            if candidate in exclude:
                continue
            try:
                loaded = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if loaded.get("result") == "running":
                return
        time.sleep(0.05)
    raise AssertionError(f"no running receipt appeared under {receipts} within {timeout}s")


def _assert_concurrent_subprocess_batteries_are_detected() -> int:
    """Two REAL, independently-running full-battery OS processes: the second
    must see the first as live concurrent contention (iss_cf3a897d/756,
    iss_0ecb55dc/760 — telling "this red ran alongside real fleet load" apart
    from a clean, isolated run). Each battery gets its own disposable
    candidate root so neither reads nor writes the real repository tree.
    """
    with tempfile.TemporaryDirectory() as temp_dir:
        directory = Path(temp_dir)
        receipts = directory / "receipts"

        slow_root = directory / "slow_candidate"
        slow_root.mkdir()
        (slow_root / "fixture.py").write_text("import time\ntime.sleep(2.5)\n", encoding="utf-8")
        slow_register = slow_root / "register.txt"
        slow_register.write_text("fixture.py\n", encoding="utf-8")

        fast_root = directory / "fast_candidate"
        fast_root.mkdir()
        (fast_root / "fixture.py").write_text("pass\n", encoding="utf-8")
        fast_register = fast_root / "register.txt"
        fast_register.write_text("fixture.py\n", encoding="utf-8")

        slow = _launch_candidate_battery(repo_root=_REPO_ROOT, root=slow_root, register=slow_register, campaign="slow", receipts=receipts)
        try:
            _wait_for_running_receipt(receipts, exclude=set())
            fast = _launch_candidate_battery(repo_root=_REPO_ROOT, root=fast_root, register=fast_register, campaign="fast", receipts=receipts)
            fast_stdout, fast_stderr = fast.communicate(timeout=30)
            _require(fast.returncode == 0, f"fast battery did not pass: {fast_stdout}{fast_stderr}")
        finally:
            slow_stdout, slow_stderr = slow.communicate(timeout=30)
            _require(slow.returncode == 0, f"slow battery did not pass: {slow_stdout}{slow_stderr}")

        fast_receipt_path = next(candidate for candidate in receipts.glob("*.json") if json.loads(candidate.read_text(encoding="utf-8")).get("campaign") == "fast")
        fast_receipt = json.loads(fast_receipt_path.read_text(encoding="utf-8"))
        contention = fast_receipt["contention"]
        _require(
            contention["concurrent_live_batteries_start"] >= 1,
            f"the fast battery did not observe the still-sleeping slow battery as live contention: {contention}",
        )
    return 1


def main() -> None:
    checks = 0
    checks += _assert_terminal_wait_replaces_fixed_timeout()
    checks += _assert_passed_receipt_cannot_mask_failed_child()
    checks += _assert_terminal_receipt_cannot_mask_hung_child()
    checks += _assert_liveness_probe()
    checks += _assert_receipt_counting_excludes_dead_finished_and_self()
    checks += _assert_battery_receipt_carries_contention()
    checks += _assert_receipt_case_outlives_old_timeout()
    checks += _assert_concurrent_subprocess_batteries_are_detected()
    print(f"contention signal smoke: {checks} checks passed")


if __name__ == "__main__":
    main()
