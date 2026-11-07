"""Bounded, advisory loop-lag telemetry for every asyncio loop in the solet.

Install before plugin startup. The stdlib loop run hook covers every creation
path, including loops created in one thread and run in another.
Sampling never changes handler placement, executor choice or HTTP readiness.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import math
import os
import struct
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from types import FrameType
from typing import TypedDict, cast

from ananta.core.runtime.loop_invariant import handler_violation_summary

logger = logging.getLogger(__name__)

CURRENT_PROCESS_KEY: ContextVar[str | None] = ContextVar("loop_lag_process_key", default=None)
MAX_SAMPLES = 10_000
STACK_FRAME_LIMIT = 8
DEFAULT_BUDGET_MS = 50.0
DEFAULT_INTERVAL_MS = 10.0
DEFAULT_WINDOW_S = 60.0
DEFAULT_SUMMARY_S = 60.0
MIN_INTERVAL_MS = 1.0
MIN_SUMMARY_S = 1.0
MAX_CPU_THREADS = 4096
TOP_CPU_THREADS = 8
CPU_HISTORY_CAPACITY = 62
_RUN_FOREVER = asyncio.BaseEventLoop.run_forever


def _positive_env(key: str, default: float, minimum: float = 0.0) -> float:
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        parsed = float(value)
        if math.isfinite(parsed) and parsed > 0:
            if parsed < minimum:
                logger.warning("LOOP_LAG_CONFIG %s=%r is below %s; using %s", key, value, minimum, minimum)
                return minimum
            return parsed
    except ValueError:
        pass
    logger.warning("LOOP_LAG_CONFIG %s=%r is not positive and finite; using default %s", key, value, default)
    return default


@dataclass(frozen=True)
class LoopLagConfig:
    """Startup settings; a hard ring cap bounds memory even for a long window."""

    budget_ms: float = DEFAULT_BUDGET_MS
    interval_ms: float = DEFAULT_INTERVAL_MS
    window_s: float = DEFAULT_WINDOW_S
    summary_s: float = DEFAULT_SUMMARY_S

    @classmethod
    def from_environment(cls) -> LoopLagConfig:
        return cls(
            budget_ms=_positive_env("ANANTA_LOOP_LAG_BUDGET_MS", DEFAULT_BUDGET_MS),
            interval_ms=_positive_env("ANANTA_LOOP_LAG_INTERVAL_MS", DEFAULT_INTERVAL_MS, MIN_INTERVAL_MS),
            window_s=_positive_env("ANANTA_LOOP_LAG_WINDOW_S", DEFAULT_WINDOW_S),
            summary_s=_positive_env("ANANTA_LOOP_LAG_SUMMARY_S", DEFAULT_SUMMARY_S, MIN_SUMMARY_S),
        )


class ThreadCpuSnapshot(TypedDict):
    name: str
    id: int
    cpu_s: float


class LoopLagSnapshot(TypedDict):
    p50_ms: float | None
    p99_ms: float | None
    max_ms: float | None
    over_budget_count: int
    window_s: float
    samples: int
    budget_ms: float
    interval_ms: float
    sample_capacity: int
    threads: list[ThreadCpuSnapshot]
    thread_cpu_status: str
    thread_cpu_window_s: float
    py_switch_interval_ms: float


class _DarwinThreadCpu:
    """Read kernel counters, including native threads, without dependencies.

    Darwin sys/proc_info_private.h defines PROC_PIDLISTTHREADIDS=28 (native
    IDs); sys/proc_info.h defines PROC_PIDTHREADID64INFO=15 and the 112-byte
    proc_threadinfo structure.
    IDs are opaque kernel lookup keys, never dereferenced pthread pointers.
    PyDLL keeps the GIL across these short syscalls, avoiding one competing
    Python thread's switch interval on every counter read.
    """

    def __init__(self) -> None:
        if sys.platform != "darwin":
            raise OSError("per-thread CPU counters are supported on macOS")
        function = ctypes.PyDLL("/usr/lib/libproc.dylib").proc_pidinfo
        function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        function.restype = ctypes.c_int
        self._pidinfo = cast(Callable[[int, int, int, object, int], int], function)
        self._ids = (ctypes.c_uint64 * MAX_CPU_THREADS)()
        self._info = ctypes.create_string_buffer(112)
        self.status = "pending"

    def read(self) -> dict[int, tuple[str, float]]:
        count = self._pidinfo(os.getpid(), 28, 0, self._ids, ctypes.sizeof(self._ids))
        if count <= 0:
            raise OSError("PROC_PIDLISTTHREADIDS returned no counters")
        self.status = "truncated" if count >= ctypes.sizeof(self._ids) else "ok"
        names = {thread.native_id: thread.name for thread in threading.enumerate()}
        result: dict[int, tuple[str, float]] = {}
        for index in range(min(count // 8, MAX_CPU_THREADS)):
            ident = int(self._ids[index])
            if self._pidinfo(os.getpid(), 15, ident, self._info, 112) != 112:
                # A thread can exit between enumeration and its counter read.
                continue
            user, system = struct.unpack_from("=QQ", self._info.raw)
            native_name = self._info.raw[48:].split(b"\0", 1)[0].decode("utf-8", errors="replace")
            result[ident] = (names.get(ident, native_name or "native"), (user + system) / 1_000_000_000)
        return result


def _stack_text(frame: FrameType | None) -> str:
    """Only filenames, line numbers and functions: no source or local values."""
    frames: list[str] = []
    while frame is not None and len(frames) < STACK_FRAME_LIMIT:
        frames.append(f"{frame.f_code.co_filename}:{frame.f_lineno}:{frame.f_code.co_name}")
        frame = frame.f_back
    return " <- ".join(frames) or "unavailable"


class LoopLagMonitor:
    """One loop's sampler and heartbeat, discarded when that loop stops."""

    def __init__(self, loop: asyncio.AbstractEventLoop, name: str, config: LoopLagConfig) -> None:
        self.loop = loop
        self.name = name
        self.config = config
        self.thread_id = threading.get_ident()
        self.next_wake = time.monotonic() + config.interval_ms / 1000
        self.warned_wake: float | None = None
        self._last_capture: tuple[float, str, str, str] | None = None
        self.samples: deque[tuple[float, float]] = deque(maxlen=MAX_SAMPLES)
        self._samples_lock = threading.Lock()
        self._sample_handle: asyncio.TimerHandle | None = None

    def record(self, now: float, lag_ms: float) -> None:
        """Append one observation; expired samples are excluded on readback."""
        with self._samples_lock:
            self.samples.append((now, lag_ms))

    def snapshot(self, now: float | None = None) -> LoopLagSnapshot:
        cutoff = (time.monotonic() if now is None else now) - self.config.window_s
        with self._samples_lock:
            retained = tuple(self.samples)
        values = sorted(lag for stamp, lag in retained if stamp >= cutoff)
        count = len(values)
        return {
            "p50_ms": values[math.ceil(count * 0.50) - 1] if count else None,
            "p99_ms": values[math.ceil(count * 0.99) - 1] if count else None,
            "max_ms": values[-1] if count else None,
            "over_budget_count": sum(value > self.config.budget_ms for value in values),
            "window_s": self.config.window_s,
            "samples": count,
            "budget_ms": self.config.budget_ms,
            "interval_ms": self.config.interval_ms,
            "sample_capacity": MAX_SAMPLES,
            "threads": [],
            "thread_cpu_status": "pending",
            "thread_cpu_window_s": 0.0,
            "py_switch_interval_ms": sys.getswitchinterval() * 1000,
        }

    def start_sampling(self) -> None:
        """Schedule one timer; cancellation never needs to run the loop."""
        self.next_wake = time.monotonic() + self.config.interval_ms / 1000
        self._sample_handle = self.loop.call_later(self.config.interval_ms / 1000, self.sample)

    def stop_sampling(self) -> None:
        """Cancel our timer without executing deferred application callbacks."""
        if self._sample_handle is not None:
            self._sample_handle.cancel()
            self._sample_handle = None

    def sample(self) -> None:
        scheduled = self.next_wake
        now = time.monotonic()
        lag_ms = max(0.0, (now - scheduled) * 1000)
        self.record(now, lag_ms)
        # A thread cannot inspect Python while native code holds the GIL.
        # Keep that missed episode visible after recovery, without inventing
        # a blocking owner or stack that can no longer be captured.
        if lag_ms > self.config.budget_ms:
            captured = self._last_capture
            if captured is not None and captured[0] == scheduled:
                _, task_name, process_key, stack = captured
                logger.warning("LOOP_LAG loop=%s lag_ms=%.3f phase=recovered task=%s process_key=%s capture=during_stall stack=%s", self.name, lag_ms, task_name, process_key, stack)
            else:
                logger.warning("LOOP_LAG loop=%s lag_ms=%.3f phase=recovered task=unknown process_key=unknown capture=unavailable_gil_or_scheduling_delay stack=unavailable_after_recovery", self.name, lag_ms)
        # Rebase after a delay: never run a burst of catch-up samples.
        self.next_wake = now + self.config.interval_ms / 1000
        self._sample_handle = self.loop.call_later(self.config.interval_ms / 1000, self.sample)

    def inspect_stall(self, now: float) -> None:
        scheduled = self.next_wake
        lag_ms = (now - scheduled) * 1000
        if lag_ms <= self.config.budget_ms or self.warned_wake == scheduled:
            return
        task = asyncio.current_task(loop=self.loop)
        process_key = task.get_context().get(CURRENT_PROCESS_KEY) if task is not None else None
        task_name = task.get_name() if task is not None else "callback"
        stack = _stack_text(sys._current_frames().get(self.thread_id))
        if self.next_wake != scheduled:
            return
        self._last_capture = (scheduled, task_name, process_key or "unknown", stack)
        self.warned_wake = scheduled
        logger.warning("LOOP_LAG loop=%s lag_ms=%.3f phase=stalled task=%s process_key=%s stack=%s", self.name, lag_ms, task_name, process_key or "unknown", stack)


class LoopLagRegistry:
    """Only active loops, with one daemon watchdog for this process."""

    def __init__(self, config: LoopLagConfig | None = None) -> None:
        self.config = config or LoopLagConfig()
        self._monitors: dict[str, LoopLagMonitor] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sequence = 0
        self._cpu_history: deque[tuple[float, dict[int, tuple[str, float]]]] = deque(maxlen=CPU_HISTORY_CAPACITY)
        self._cpu_threads: list[ThreadCpuSnapshot] = []
        self._cpu_status = "pending"
        self._cpu_window_s = 0.0

    def start(self, loop: asyncio.AbstractEventLoop) -> LoopLagMonitor:
        thread_name = threading.current_thread().name
        label = "platform" if thread_name == "MainThread" else thread_name
        with self._lock:
            self._sequence += 1
            monitor = LoopLagMonitor(loop, f"{label}/loop-{self._sequence}", self.config)
            self._monitors[monitor.name] = monitor
            if self._thread is None:
                self._thread = threading.Thread(target=self._watch, name="loop-lag-watchdog", daemon=True)
                self._thread.start()
        return monitor

    def remove(self, name: str) -> None:
        with self._lock:
            self._monitors.pop(name, None)

    def _active(self) -> tuple[LoopLagMonitor, ...]:
        with self._lock:
            return tuple(self._monitors.values())

    def snapshot(self) -> dict[str, LoopLagSnapshot]:
        with self._lock:
            threads = [row.copy() for row in self._cpu_threads]
            status, window_s = self._cpu_status, self._cpu_window_s
        snapshots = {monitor.name: monitor.snapshot() for monitor in self._active()}
        for snapshot in snapshots.values():
            snapshot.update(threads=threads, thread_cpu_status=status, thread_cpu_window_s=window_s)
        return snapshots

    def _sample_thread_cpu(self, reader: _DarwinThreadCpu, now: float) -> None:
        """Called only by the watchdog; health consumes cached bounded rows."""
        current = reader.read()
        self._cpu_history.append((now, current))
        cutoff = now - self.config.window_s
        # Keep one boundary sample, so deltas cover the rolling window.
        while len(self._cpu_history) > 1 and self._cpu_history[1][0] <= cutoff:
            self._cpu_history.popleft()
        baseline_at, baseline = self._cpu_history[0]
        rows: list[ThreadCpuSnapshot] = [{"name": name, "id": ident, "cpu_s": max(0.0, cpu - baseline.get(ident, (name, 0.0))[1])} for ident, (name, cpu) in current.items()]
        rows.sort(key=lambda row: row["cpu_s"], reverse=True)
        with self._lock:
            self._cpu_threads = rows[:TOP_CPU_THREADS]
            self._cpu_status = reader.status
            self._cpu_window_s = now - baseline_at

    def shutdown(self) -> None:
        """Retire the watchdog after owned loops have stopped (also for tests)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def _watch(self) -> None:
        reader: _DarwinThreadCpu | None = None
        try:
            reader = _DarwinThreadCpu()
        except (OSError, AttributeError) as error:
            self._cpu_status = f"unavailable: {error}"
            logger.warning("LOOP_LAG_CONFIG thread_cpu=%s", self._cpu_status)
        census_at = 0.0
        census_interval = max(1.0, self.config.window_s / 60)
        summary_at = time.monotonic() + self.config.summary_s
        interval = max(MIN_INTERVAL_MS, min(self.config.interval_ms, self.config.budget_ms / 2)) / 1000
        while not self._stop.wait(interval):
            now = time.monotonic()
            if reader is not None and now >= census_at:
                try:
                    self._sample_thread_cpu(reader, now)
                except OSError as error:
                    with self._lock:
                        self._cpu_threads = []
                        self._cpu_status = f"unavailable: {error}"
                        self._cpu_window_s = 0.0
                    logger.warning("LOOP_LAG_CONFIG thread_cpu=%s", self._cpu_status)
                census_at = now + census_interval
            for monitor in self._active():
                if monitor.loop.is_running():
                    monitor.inspect_stall(now)
            if now >= summary_at:
                logger.info("LOOP_LAG_SUMMARY loops=%s violations=%s", self.snapshot(), handler_violation_summary())
                summary_at = now + self.config.summary_s


LOOP_LAG = LoopLagRegistry()
_SAMPLING_LOOPS: set[asyncio.BaseEventLoop] = set()
_SAMPLING_LOCK = threading.Lock()


def _run_monitored_loop(loop: asyncio.BaseEventLoop) -> None:
    """Bind a sampler to each stdlib loop's running lifetime.

    Raw run_forever users may stop without closing (the Vault async bridge).
    Cancel only our timer on stop, so those loops retain no active sampler or
    monitor. Cleanup never runs the loop or application callbacks deferred to
    a later restart. Later runs get a new, empty window.
    """

    with _SAMPLING_LOCK:
        reentry = loop in _SAMPLING_LOOPS
        if not reentry and not loop.is_running() and not loop.is_closed():
            _SAMPLING_LOOPS.add(loop)
        else:
            reentry = True
    if reentry:
        _RUN_FOREVER(loop)
        return
    registry = LOOP_LAG
    monitor = registry.start(loop)
    try:
        monitor.start_sampling()
        _RUN_FOREVER(loop)
    finally:
        monitor.stop_sampling()
        registry.remove(monitor.name)
        with _SAMPLING_LOCK:
            _SAMPLING_LOOPS.remove(loop)


def install_loop_lag_monitor() -> None:
    """Enable monitoring before any startup loop is created; idempotent."""
    if asyncio.BaseEventLoop.run_forever is _run_monitored_loop:
        return
    LOOP_LAG.config = LoopLagConfig.from_environment()
    interval_ms = _positive_env("ANANTA_PY_SWITCH_INTERVAL_MS", sys.getswitchinterval() * 1000)
    try:
        if "ANANTA_PY_SWITCH_INTERVAL_MS" in os.environ:
            sys.setswitchinterval(interval_ms / 1000)
    except (ValueError, OverflowError) as error:
        logger.warning("LOOP_LAG_CONFIG ANANTA_PY_SWITCH_INTERVAL_MS=%r unchanged: %s", interval_ms, error)
    # Every in-process stdlib loop reaches this method, including implicit
    # get_event_loop creation and factories imported before installation.
    asyncio.BaseEventLoop.run_forever = _run_monitored_loop
    logger.info("LOOP_LAG_ENABLED config=%s sample_capacity=%s py_switch_interval_ms=%s", LOOP_LAG.config, MAX_SAMPLES, sys.getswitchinterval() * 1000)
