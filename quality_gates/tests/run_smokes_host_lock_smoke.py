#!/usr/bin/env python3
"""Pin host-serialized serial smokes without serializing the worker pool.

Also covers this smoke's own contention check (iss_dfca317c/357,
iss_c35292ca/358): a holder pid that is verifiably dead must turn the smoke
FAIL, not another indistinguishable SKIP, and a legitimate live-holder skip
must leave a durable record behind — see ``contention_signal.py``'s module
docstring for the full rationale shared with ``run_smokes.py``.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "quality_gates"))
import contention_signal  # noqa: E402

_SELF_ENTRY = "quality_gates/tests/run_smokes_host_lock_smoke.py"
_STUB_MODE = "RUN_SMOKES_HOST_LOCK_STUB_MODE"
_STUB_EVENTS = "RUN_SMOKES_HOST_LOCK_STUB_EVENTS"
_STUB_LABEL = "RUN_SMOKES_HOST_LOCK_STUB_LABEL"
_STUB_SLEEP = "RUN_SMOKES_HOST_LOCK_STUB_SLEEP"
_LOCK_PATH_ENV = "RUN_SMOKES_HOST_SERIAL_LOCK_PATH"
_NESTED_SELF_TEST_ENV = "RUN_SMOKES_NESTED_SELF_TEST"
_HOST_LOCK_PATH = Path.home() / ".ananta" / "locks" / "run_smokes_serial_only.lock"
_HOST_LOCK_WAIT_SECONDS = 0.5
# A hang guard for the paired runners, not a timing assertion: ordering is
# asserted from the recorded intervals. Each runner sleeps at most 1.5 s, yet a
# 10 s guard expired at host load1 ~19 during a full born-clone run (unit
# unt_587279e4 event 17). 60 s bounds a genuine hang while tolerating load.
# It is the one guard for every wait in this smoke, so a result never depends
# on how fast a busy host starts a process.
_RUNNER_PAIR_TIMEOUT_SECONDS = 60
# The nested runner hands its stub only the exact RUN_SMOKES_* control variables
# it allowlists, so a pooled stub finds its peer by label instead of by a new
# variable. Each pooled stub holds until the other has also started.
_POOLED_PEER = {"pooled-first": "pooled-second", "pooled-second": "pooled-first"}
_SKIP_EXIT_CODE = 77
# Fixture pacing only: serial runners report attention while the stub holds
# the host lock, so non-overlap proves attention never releases it.
_ATTENTION_FIXTURE_ENVIRONMENT = {"SMOKE_PROCESS_ATTENTION_SECONDS": "0.2"}


def _has_start_row(events: Path, label: str) -> bool:
    """True once ``label`` has recorded its start row in ``events``."""
    if not events.exists():
        return False
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    return any(row[1] == label and row[2] == "start" for row in rows)


def _wait_for(condition: Callable[[], bool], process: subprocess.Popen[str] | None = None) -> bool:
    """Poll ``condition`` until it holds: False at the hang guard, or at once if ``process`` has exited."""
    deadline = time.monotonic() + _RUNNER_PAIR_TIMEOUT_SECONDS
    while not condition():
        if time.monotonic() >= deadline or (process is not None and process.poll() is not None):
            return condition()
        time.sleep(0.02)
    return True


def _fail_with_output(process: subprocess.Popen[str], what: str) -> NoReturn:
    """Stop ``process`` if it still runs, then fail with its return code and output."""
    hung = process.poll() is None
    if hung:
        process.terminate()
    stdout, stderr = process.communicate(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)
    state = f"still running after {_RUNNER_PAIR_TIMEOUT_SECONDS}s" if hung else "exited"
    raise AssertionError(f"{what}: process {state}, return code {process.returncode}: {stdout}{stderr}")


def _record_stub() -> int:
    """Act as the subprocess fixture when invoked by the runner under test.

    A serial stub holds for its sleep. A pooled stub holds until its peer has
    also started, which proves the two overlap without sleeping.
    """
    mode = os.environ.get(_STUB_MODE)
    if not mode:
        return -1
    events_path = Path(os.environ[_STUB_EVENTS])
    label = os.environ[_STUB_LABEL]
    started = time.monotonic()
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps([mode, label, "start", started]) + "\n")
    if mode == "pooled":
        peer = _POOLED_PEER[label]
        if not _wait_for(lambda: _has_start_row(events_path, peer)):
            return 1
    else:
        time.sleep(float(os.environ[_STUB_SLEEP]))
    ended = time.monotonic()
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps([mode, label, "end", ended]) + "\n")
    return 0


def _run_runner(
    register: Path,
    events: Path,
    *,
    label: str,
    serial: bool,
    sleep_seconds: float = 0.0,
    lock_path: Path | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "SOLET_NAME": os.environ["SOLET_NAME"],
            _STUB_MODE: "serial" if serial else "pooled",
            _STUB_EVENTS: str(events),
            _STUB_LABEL: label,
            _STUB_SLEEP: str(sleep_seconds),
            _NESTED_SELF_TEST_ENV: "1",
        }
    )
    if lock_path is not None:
        env[_LOCK_PATH_ENV] = str(lock_path)
    command = [
        str(_REPO_ROOT / ".venv" / "bin" / "python3"),
        "quality_gates/run_smokes.py",
        "--register",
        str(register),
        "--jobs",
        "1",
    ]
    if serial:
        command.extend(["--serial-only-override", _SELF_ENTRY])
        env.update(_ATTENTION_FIXTURE_ENVIRONMENT)
    return subprocess.Popen(
        command,
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _intervals(events: Path, mode: str) -> dict[str, tuple[float, float]]:
    rows = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
    result: dict[str, dict[str, float]] = {}
    for row_mode, label, event, timestamp in rows:
        if row_mode == mode:
            result.setdefault(label, {})[event] = timestamp
    return {label: (times["start"], times["end"]) for label, times in result.items()}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _host_lock_contention_outcome(lock_path: Path = _HOST_LOCK_PATH, *, skip_log_path: Path | None = None) -> tuple[str, str] | None:
    """Decide the smoke's outcome when the host lock cannot be acquired promptly.

    Returns ``None`` when there is no contention at all. Otherwise returns
    ``(status, message)``: ``"skip"`` when the recorded holder pid is
    verifiably alive (legitimate contention with a live sibling battery —
    also records a durable skip event, iss_c35292ca/358), or ``"fail"`` when
    the recorded pid is verifiably dead (a leaked lock, not contention —
    iss_dfca317c/357: "the runner already writes pid+cwd into the lock file,
    so a dead holder is detectable and should turn the smoke RED instead of
    skipping"). A holder pid this smoke cannot parse is treated as "skip",
    the historical behavior — an unparseable holder is "can't tell", not
    "known dead".
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + _HOST_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                return None
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    holder_text = handle.read().strip()
                    holder_pid = contention_signal.parse_lock_holder_pid(holder_text)
                    if holder_pid is not None and not contention_signal.holder_pid_is_alive(holder_pid):
                        return (
                            "fail",
                            f"FAIL host serial-only lock held by dead pid {holder_pid} after {_HOST_LOCK_WAIT_SECONDS:.1f}s (leaked lock, not live contention): {holder_text or lock_path}",
                        )
                    contention_signal.record_host_lock_skip_event(
                        holder_pid=holder_pid,
                        holder_alive=True if holder_pid is not None else None,
                        waited_seconds=_HOST_LOCK_WAIT_SECONDS,
                        log_path=skip_log_path,
                    )
                    detail = holder_text or str(lock_path)
                    return (
                        "skip",
                        f"SKIP host serial-only lock contention after {_HOST_LOCK_WAIT_SECONDS:.1f}s; holder {detail}",
                    )
                time.sleep(0.05)


def _lock_is_held(lock_path: Path) -> bool:
    """True when some OTHER process holds ``lock_path``'s exclusive flock."""
    with lock_path.open("a+", encoding="utf-8") as probe:
        try:
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
        except BlockingIOError:
            return True
    return False


def _wait_until_locked(lock_path: Path, holder: subprocess.Popen[str]) -> None:
    """Block until ``holder`` holds ``lock_path``'s exclusive flock; fail with its output if it dies first."""
    if not _wait_for(lambda: _lock_is_held(lock_path), holder):
        _fail_with_output(holder, f"lock never became held: {lock_path}")


_HOLD_LOCK_SCRIPT = "import fcntl, time\nhandle = open({path!r}, 'a+')\nfcntl.flock(handle, fcntl.LOCK_EX)\ntime.sleep(5)\n"


def _start_lock_holder(lock_path: Path) -> subprocess.Popen[str]:
    """Start a process that takes ``lock_path``'s exclusive flock and holds it."""
    command = [sys.executable, "-c", _HOLD_LOCK_SCRIPT.format(path=str(lock_path))]
    return subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _dead_pid() -> int:
    """Return a pid guaranteed to be dead: a just-exited child's own pid."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _assert_dead_holder_pid_fails_not_skips() -> None:
    """iss_dfca317c/357: a leaked lock (dead recorded pid) must FAIL, not skip."""
    with tempfile.TemporaryDirectory() as temp_dir:
        lock_path = Path(temp_dir) / "serial-only.lock"
        dead_pid = _dead_pid()
        lock_path.write_text(f"pid={dead_pid} cwd=/dead\n", encoding="utf-8")
        holder = _start_lock_holder(lock_path)
        try:
            _wait_until_locked(lock_path, holder)
            outcome = _host_lock_contention_outcome(lock_path=lock_path)
            _require(
                outcome is not None and outcome[0] == "fail",
                f"expected fail for a dead recorded holder pid, got {outcome!r}",
            )
            assert outcome is not None
            _require(str(dead_pid) in outcome[1], outcome[1])
        finally:
            holder.terminate()
            holder.wait(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)


def _assert_live_holder_pid_skips_and_logs() -> None:
    """iss_c35292ca/358: a legitimate skip must leave a durable, countable record."""
    with tempfile.TemporaryDirectory() as temp_dir:
        lock_path = Path(temp_dir) / "serial-only.lock"
        skip_log = Path(temp_dir) / "skips.jsonl"
        lock_path.touch()
        holder = _start_lock_holder(lock_path)
        try:
            _wait_until_locked(lock_path, holder)
            lock_path.write_text(f"pid={holder.pid} cwd=/live\n", encoding="utf-8")
            outcome = _host_lock_contention_outcome(lock_path=lock_path, skip_log_path=skip_log)
            _require(outcome is not None and outcome[0] == "skip", f"expected skip, got {outcome!r}")
            _require(skip_log.exists(), "a legitimate live-holder skip did not record a skip event")
            events = [json.loads(line) for line in skip_log.read_text(encoding="utf-8").splitlines()]
            _require(len(events) == 1, f"expected exactly one skip event, got {events!r}")
            _require(events[0]["holder_pid"] == holder.pid, events)  # pyright: ignore[reportArgumentType]
            _require(events[0]["holder_alive"] is True, events)  # pyright: ignore[reportArgumentType]
        finally:
            holder.terminate()
            holder.wait(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)


def _assert_runner_interrupt_cleanup(signum: int | None = signal.SIGINT, serial: bool | None = True) -> None:
    """Interrupts reap registered groups before removing their owned roots."""
    from _run_smokes_scratch_fixture import interrupt_cleanup

    interrupt_cleanup(signum, serial)


def _assert_stale_runner_scratch_is_swept() -> None:
    """Only old dead owners are swept, with live and malformed controls."""
    from _run_smokes_scratch_fixture import stale_scratch

    stale_scratch()


def _assert_runner_scratch_root_budget() -> None:
    """Measure actual resolved filesystem bytes before testing ownership."""
    from _run_smokes_scratch_fixture import _scratch_run

    _scratch_run("length")


def main() -> None:
    stub_result = _record_stub()
    if stub_result >= 0:
        raise SystemExit(stub_result)
    outcome = _host_lock_contention_outcome()
    if outcome is not None:
        status, message = outcome
        print(message)
        raise SystemExit(1 if status == "fail" else _SKIP_EXIT_CODE)
    _assert_runner_interrupt_cleanup(None, None)
    _assert_stale_runner_scratch_is_swept()
    _assert_runner_scratch_root_budget()
    _assert_dead_holder_pid_fails_not_skips()
    _assert_live_holder_pid_skips_and_logs()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        register = temp / "register.txt"
        register.write_text(_SELF_ENTRY + "\n", encoding="utf-8")
        events = temp / "events.jsonl"
        lock_path = temp / "serial-only.lock"

        first = _run_runner(
            register,
            events,
            label="serial-first",
            sleep_seconds=1.5,
            serial=True,
            lock_path=lock_path,
        )
        if not _wait_for(lambda: _has_start_row(events, "serial-first"), first):
            _fail_with_output(first, "first serial runner never recorded its start")
        second = _run_runner(
            register,
            events,
            label="serial-second",
            sleep_seconds=0.1,
            serial=True,
            lock_path=lock_path,
        )
        first_stdout, first_stderr = first.communicate(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)
        second_stdout, second_stderr = second.communicate(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)
        _require(first.returncode == 0, f"first serial runner failed: {first_stdout}{first_stderr}")
        _require(second.returncode == 0, f"second serial runner failed: {second_stdout}{second_stderr}")
        _require(f"ATTENTION {_REPO_ROOT / _SELF_ENTRY}: running " in first_stderr, f"held serial stub reported no attention: {first_stderr}")
        serial = _intervals(events, "serial")
        first_interval = serial["serial-first"]
        second_interval = serial["serial-second"]
        _require(
            first_interval[1] <= second_interval[0] or second_interval[1] <= first_interval[0],
            f"serial intervals overlap: {first_interval!r}, {second_interval!r}",
        )
        events.unlink()
        # Each pooled stub holds until the other has started, so overlap is proven
        # by rendezvous and a slow start only delays it; if the pool were
        # serialized the stubs would wait out the guard and fail below.
        pooled_first = _run_runner(register, events, label="pooled-first", serial=False)
        pooled_second = _run_runner(register, events, label="pooled-second", serial=False)
        pooled_first_stdout, pooled_first_stderr = pooled_first.communicate(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)
        pooled_second_stdout, pooled_second_stderr = pooled_second.communicate(timeout=_RUNNER_PAIR_TIMEOUT_SECONDS)
        _require(
            pooled_first.returncode == 0,
            f"first pooled runner failed: {pooled_first_stdout}{pooled_first_stderr}",
        )
        _require(
            pooled_second.returncode == 0,
            f"second pooled runner failed: {pooled_second_stdout}{pooled_second_stderr}",
        )
        pooled = _intervals(events, "pooled")
        pooled_first_interval = pooled["pooled-first"]
        pooled_second_interval = pooled["pooled-second"]
        _require(
            max(pooled_first_interval[0], pooled_second_interval[0]) < min(pooled_first_interval[1], pooled_second_interval[1]),
            f"pooled intervals did not overlap: {pooled_first_interval!r}, {pooled_second_interval!r}",
        )


if __name__ == "__main__":
    main()
