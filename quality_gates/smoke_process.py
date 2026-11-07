"""Collect an already-started, caller-owned smoke without an execution deadline.

The caller starts/registers the process, owns signals, start-rate policy, locks
and receipts, and supplies cleanup for *all* its owned descendants (including
detached writers when applicable). This module never signals or starts a process.
It requires distinct, unread stdout/stderr PIPEs; text-mode Popen is supported
by reading the underlying descriptors as bytes. No other reader may consume them.
Tails are per-stream raw bytes, with exact byte totals; decoding belongs to the
caller and does not change those counts. Cross-stream ordering is unspecified.

Configuration is loaded by CollectorConfig.from_environment, also used when
collect_owned_process receives no config. Real environment keys and defaults:
SMOKE_PROCESS_ATTENTION_SECONDS=900, SMOKE_PROCESS_POLL_SECONDS=0.1,
SMOKE_PROCESS_TAIL_BYTES=16000 (per stream; zero retains no bytes), and
SMOKE_PROCESS_READ_BYTES=65536 (per ready descriptor per observation).
Durations must be finite and positive; byte limits are integers. Intervals only
schedule observations, never cancellation, a retry, or a synthetic exit code.

Cleanup is attempted exactly once: on observed natural root exit *before*
waiting for pipe EOF, explicit cancellation, or any collection/callback error.
On cancellation/error the hook must stop the owned root and release all owned
pipe writers; the collector then reaps it without a deadline. On natural exit
the hook releases remaining owned writers even though the root is already reaped.
A successful hook may signal and return: reaping belongs to the collector.
Finalization closes both read pipes, then calls on_reaped only when the root is
reaped. That hook can unregister it. If cleanup raises, no fallback signal or
blocking wait is attempted; an unreaped root remains the caller's responsibility
and on_reaped is not called for it. Callback errors are raised, never converted
to process failures; multiple errors are retained in a BaseExceptionGroup.

Each call is one terminal collection, not a mutable database record. Reuse,
registration, owner corrections and historical receipt lifecycle belong to the
caller. Runner integration and its registered lifecycle proof are separate.
"""

from __future__ import annotations

import math
import os
import selectors
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

DEFAULT_ATTENTION_SECONDS = 900.0
DEFAULT_POLL_SECONDS = 0.1
DEFAULT_TAIL_BYTES = 16_000
DEFAULT_READ_BYTES = 65_536

type OwnedProcess = subprocess.Popen[str] | subprocess.Popen[bytes]


class CleanupReason(StrEnum):
    """Why the caller must release its owned process tree and pipe writers."""

    ROOT_EXIT = "root_exit"
    CANCELLED = "cancelled"
    ERROR = "error"


@dataclass(frozen=True)
class CollectorConfig:
    """Observation and storage limits, with no process execution limit."""

    attention_seconds: float = DEFAULT_ATTENTION_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    tail_bytes: int = DEFAULT_TAIL_BYTES
    read_bytes: int = DEFAULT_READ_BYTES

    def __post_init__(self) -> None:
        for name, value in (("attention_seconds", self.attention_seconds), ("poll_seconds", self.poll_seconds)):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name, value, minimum in (("tail_bytes", self.tail_bytes, 0), ("read_bytes", self.read_bytes, 1)):
            if type(value) is not int or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str] | None = None) -> CollectorConfig:
        """Validate actual environment settings; malformed values fail visibly."""
        values = os.environ if environment is None else environment
        return cls(
            attention_seconds=float(values.get("SMOKE_PROCESS_ATTENTION_SECONDS", str(DEFAULT_ATTENTION_SECONDS))),
            poll_seconds=float(values.get("SMOKE_PROCESS_POLL_SECONDS", str(DEFAULT_POLL_SECONDS))),
            tail_bytes=int(values.get("SMOKE_PROCESS_TAIL_BYTES", str(DEFAULT_TAIL_BYTES))),
            read_bytes=int(values.get("SMOKE_PROCESS_READ_BYTES", str(DEFAULT_READ_BYTES))),
        )


@dataclass(frozen=True)
class OutputTail:
    """Retained trailing bytes and the true total before truncation."""

    tail: bytes
    total_bytes: int

    @property
    def truncated(self) -> bool:
        return self.total_bytes > len(self.tail)


@dataclass(frozen=True)
class Attention:
    """An observation, never an instruction to terminate the process."""

    elapsed_seconds: float
    since_output_seconds: float
    host_load: tuple[float, float, float] | None
    tests_done: int | None
    stdout_bytes: int
    stderr_bytes: int


@dataclass(frozen=True)
class CollectionResult:
    """The real root result; owner cancellation is an independent fact."""

    returncode: int
    cancelled: bool
    stdout: OutputTail
    stderr: OutputTail
    elapsed_seconds: float


@dataclass
class _Tail:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    total: int = 0

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.limit:
            self.data.extend(chunk[-self.limit :])
            del self.data[: max(0, len(self.data) - self.limit)]

    def snapshot(self) -> OutputTail:
        return OutputTail(bytes(self.data), self.total)


def _host_load() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return None


def _tests_done(provider: Callable[[], int | None] | None) -> int | None:
    value = provider() if provider is not None else None
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("tests_done must return a nonnegative integer or None")
    return value


class _Collector:
    """Local read ownership; process and descendant authority stay in hooks."""

    def __init__(
        self,
        process: OwnedProcess,
        cleanup_owned: Callable[[CleanupReason], None],
        on_reaped: Callable[[], None],
    ) -> None:
        self.process = process
        self.cleanup_owned = cleanup_owned
        self.on_reaped = on_reaped
        self.cleanup_reason: CleanupReason | None = None
        self.cancelled = False
        self.close_selector: Callable[[], None] | None = None
        self.started = time.monotonic()
        self.last_output = self.started

    def _cleanup(self, reason: CleanupReason) -> None:
        self.cleanup_reason = reason
        self.cleanup_owned(reason)
        self.process.wait()

    def _pipes(self, stdout: _Tail, stderr: _Tail, selector: selectors.BaseSelector) -> dict[int, _Tail]:
        if self.process.stdout is None or self.process.stderr is None:
            raise ValueError("collector requires distinct unread stdout=PIPE and stderr=PIPE")
        buffers = {self.process.stdout.fileno(): stdout, self.process.stderr.fileno(): stderr}
        if len(buffers) != 2:
            raise ValueError("collector requires distinct output descriptors")
        for descriptor in buffers:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        return buffers

    def _read(self, buffers: dict[int, _Tail], delay: float, chunk_size: int, selector: selectors.BaseSelector) -> None:
        if not selector.get_map():
            time.sleep(delay)
            return
        for key, _events in selector.select(delay):
            try:
                chunk = os.read(key.fd, chunk_size)
            except BlockingIOError:
                continue
            if chunk:
                buffers[key.fd].append(chunk)
                self.last_output = time.monotonic()
            else:
                selector.unregister(key.fd)

    def _observe_root(self, cancellation_requested: Callable[[], bool] | None) -> int | None:
        returncode = self.process.poll()
        if self.cleanup_reason is None:
            if returncode is not None:
                self._cleanup(CleanupReason.ROOT_EXIT)
            elif cancellation_requested is not None and cancellation_requested():
                self.cancelled = True
                self._cleanup(CleanupReason.CANCELLED)
        return self.process.returncode

    def _loop(
        self,
        config: CollectorConfig,
        on_attention: Callable[[Attention], None] | None,
        cancellation_requested: Callable[[], bool] | None,
        tests_done: Callable[[], int | None] | None,
    ) -> CollectionResult:
        stdout, stderr = _Tail(config.tail_bytes), _Tail(config.tail_bytes)
        selector = selectors.DefaultSelector()
        self.close_selector = selector.close
        buffers = self._pipes(stdout, stderr, selector)
        next_attention = self.started + config.attention_seconds
        while True:
            returncode = self._observe_root(cancellation_requested)
            if returncode is not None and not selector.get_map():
                return CollectionResult(returncode, self.cancelled, stdout.snapshot(), stderr.snapshot(), time.monotonic() - self.started)
            now = time.monotonic()
            if now >= next_attention:
                if on_attention is not None:
                    on_attention(Attention(now - self.started, now - self.last_output, _host_load(), _tests_done(tests_done), stdout.total, stderr.total))
                next_attention = time.monotonic() + config.attention_seconds
            delay = min(config.poll_seconds, max(0.0, next_attention - time.monotonic()))
            self._read(buffers, delay, config.read_bytes, selector)

    def _cleanup_error(self, errors: list[BaseException]) -> None:
        if errors and self.cleanup_reason is None:
            try:
                self._cleanup(CleanupReason.ERROR)
            except BaseException as exc:
                errors.append(exc)

    def _finish(self, errors: list[BaseException]) -> None:
        self._cleanup_error(errors)
        closers = [pipe.close for pipe in (self.process.stdout, self.process.stderr) if pipe is not None]
        if self.close_selector is not None:
            closers.insert(0, self.close_selector)
        for closer in closers:
            try:
                closer()
            except BaseException as exc:
                errors.append(exc)
        if self.process.poll() is not None:
            try:
                self.on_reaped()
            except BaseException as exc:
                errors.append(exc)

    def collect(
        self,
        config: CollectorConfig | None,
        on_attention: Callable[[Attention], None] | None,
        cancellation_requested: Callable[[], bool] | None,
        tests_done: Callable[[], int | None] | None,
    ) -> CollectionResult:
        errors: list[BaseException] = []
        result: CollectionResult | None = None
        try:
            result = self._loop(config if config is not None else CollectorConfig.from_environment(), on_attention, cancellation_requested, tests_done)
        except BaseException as exc:
            errors.append(exc)
        self._finish(errors)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("smoke collection and finalization errors", errors)
        if result is None:
            raise RuntimeError("collection ended without a result")
        return result


def collect_owned_process(
    process: OwnedProcess,
    *,
    cleanup_owned: Callable[[CleanupReason], None],
    on_reaped: Callable[[], None],
    config: CollectorConfig | None = None,
    on_attention: Callable[[Attention], None] | None = None,
    cancellation_requested: Callable[[], bool] | None = None,
    tests_done: Callable[[], int | None] | None = None,
) -> CollectionResult:
    """Drain both streams, observe indefinitely, clean owned writers, then reap.

    Hooks execute in the collecting thread and must return promptly, except for
    owner cleanup's necessary shutdown work. Attention has no cancellation return
    value. Cancellation is only the explicit cancellation_requested predicate.
    An unknown tests_done/load stays None. This function closes both output pipes
    and must be called once per owned process. See the module's cleanup contract.
    """
    return _Collector(process, cleanup_owned, on_reaped).collect(config, on_attention, cancellation_requested, tests_done)
