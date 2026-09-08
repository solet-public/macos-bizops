#!/usr/bin/env python3
"""Pin host-serialized serial smokes without serializing the worker pool."""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SELF_ENTRY = "quality_gates/tests/run_smokes_host_lock_smoke.py"
_STUB_MODE = "RUN_SMOKES_HOST_LOCK_STUB_MODE"
_STUB_EVENTS = "RUN_SMOKES_HOST_LOCK_STUB_EVENTS"
_STUB_LABEL = "RUN_SMOKES_HOST_LOCK_STUB_LABEL"
_STUB_SLEEP = "RUN_SMOKES_HOST_LOCK_STUB_SLEEP"
_LOCK_PATH_ENV = "RUN_SMOKES_HOST_SERIAL_LOCK_PATH"
_NESTED_SELF_TEST_ENV = "RUN_SMOKES_NESTED_SELF_TEST"
_HOST_LOCK_PATH = Path.home() / ".ananta" / "locks" / "run_smokes_serial_only.lock"
_HOST_LOCK_WAIT_SECONDS = 0.5
_SKIP_EXIT_CODE = 77


def _record_stub() -> int:
    """Act as the subprocess fixture when invoked by the runner under test."""
    mode = os.environ.get(_STUB_MODE)
    if not mode:
        return -1
    events_path = Path(os.environ[_STUB_EVENTS])
    label = os.environ[_STUB_LABEL]
    started = time.monotonic()
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps([mode, label, "start", started]) + "\n")
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
    sleep_seconds: float,
    serial: bool,
    lock_path: Path | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env.update(
        {
            "SOLET_NAME": "host-lock-test",
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


def _host_lock_contention_skip() -> str | None:
    """Return a disclosed skip reason when another battery owns the host lock."""
    _HOST_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _HOST_LOCK_PATH.open("a+", encoding="utf-8") as handle:
        deadline = time.monotonic() + _HOST_LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(handle, fcntl.LOCK_UN)
                return None
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    handle.seek(0)
                    holder = handle.read().strip()
                    if holder:
                        return (
                            "SKIP host serial-only lock contention after "
                            f"{_HOST_LOCK_WAIT_SECONDS:.1f}s; holder {holder}"
                        )
                    return (
                        "SKIP host serial-only lock contention after "
                        f"{_HOST_LOCK_WAIT_SECONDS:.1f}s; lock {_HOST_LOCK_PATH}"
                    )
                time.sleep(0.05)


def main() -> None:
    stub_result = _record_stub()
    if stub_result >= 0:
        raise SystemExit(stub_result)
    skip_reason = _host_lock_contention_skip()
    if skip_reason is not None:
        print(skip_reason)
        raise SystemExit(_SKIP_EXIT_CODE)
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
        time.sleep(0.2)
        second = _run_runner(
            register,
            events,
            label="serial-second",
            sleep_seconds=0.1,
            serial=True,
            lock_path=lock_path,
        )
        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)
        _require(first.returncode == 0, f"first serial runner failed: {first_stdout}{first_stderr}")
        _require(second.returncode == 0, f"second serial runner failed: {second_stdout}{second_stderr}")
        serial = _intervals(events, "serial")
        first_interval = serial["serial-first"]
        second_interval = serial["serial-second"]
        _require(
            first_interval[1] <= second_interval[0],
            f"serial intervals overlap: {first_interval!r}, {second_interval!r}",
        )
        events.unlink()
        pooled_first = _run_runner(register, events, label="pooled-first", sleep_seconds=1.5, serial=False)
        time.sleep(0.2)
        pooled_second = _run_runner(register, events, label="pooled-second", sleep_seconds=1.5, serial=False)
        pooled_first_stdout, pooled_first_stderr = pooled_first.communicate(timeout=10)
        pooled_second_stdout, pooled_second_stderr = pooled_second.communicate(timeout=10)
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
            max(pooled_first_interval[0], pooled_second_interval[0])
            < min(pooled_first_interval[1], pooled_second_interval[1]),
            f"pooled intervals did not overlap: {pooled_first_interval!r}, {pooled_second_interval!r}",
        )


if __name__ == "__main__":
    main()
