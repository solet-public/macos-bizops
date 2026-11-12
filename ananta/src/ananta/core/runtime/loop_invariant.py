"""Advisory evidence of actual handler entry on registered asyncio threads.

Records are process-local: append/read, bounded FIFO retirement, then retirement
on process exit. Counters last for the process lifetime. Recording never changes
execution placement or refuses an invocation. Dispatch-only declarations are
reviewed source, with a reason; remove a declaration to clear the exception.

Handler wall time is advisory evidence too: per process key, the call count and
the total, maximum and last elapsed seconds of timed handler calls. The table
tracks at most ``entry_capacity`` keys; recording a new key beyond that evicts
the least-recently-recorded key and counts the eviction.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import traceback
from collections import OrderedDict, deque
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypedDict

logger = logging.getLogger(__name__)
DEFAULT_ENTRY_CAPACITY = 128
DEFAULT_STACK_FRAME_LIMIT = 12
DEFAULT_STACK_CHAR_LIMIT = 8192
MIN_ENTRY_CAPACITY = 1
MIN_STACK_FRAME_LIMIT = 1
MIN_STACK_CHAR_LIMIT = 128

# Exact endpoint identities, not prefixes or plugin-wide exemptions. These
# bodies only dispatch work to workers or serve in-process transport telemetry.
# The AST census attached to the author Unit records the reviewed bodies.
DISPATCH_ONLY_ROUTES: Mapping[str, str] = MappingProxyType(
    {
        "health": "In-process telemetry snapshots and response construction only.",
        "open_bridge": "Bridge open and sender attribution run on bridge workers; response construction stays here.",
        "close_bridge": "Cleanup body is dispatched with asyncio.to_thread.",
        "events_bridge": "Touch is offloaded; event polling awaits the in-memory bridge queue.",
        "process_search_route": "Dispatches _process_search_blocking with asyncio.to_thread.",
        "process_schema_route": "Dispatches _process_schema_blocking with asyncio.to_thread.",
        "process_call_route": "Dispatches _process_call_blocking with asyncio.to_thread.",
        "process_result_route": "Dispatches _process_result_blocking with asyncio.to_thread.",
        "download_route": "Dispatches _download_blocking with asyncio.to_thread.",
        "peer_holds_role_route": "In-memory bridge policy check; role binding read is offloaded.",
        "peer_inbox_for_session_route": "In-memory bridge policy check; inbox read is offloaded.",
        "peer_register_route": "Dispatches registration, probing and lock ownership to a worker.",
        "peer_list_route": "Binding-table read is offloaded; response serialization stays here.",
        "current_identity_route": "Dispatches _current_identity_blocking with asyncio.to_thread.",
        "peer_send_route": "Dispatches _peer_send_impl with asyncio.to_thread.",
        "peer_claim_role_route": "Dispatches _peer_claim_role_impl with asyncio.to_thread.",
        "peer_send_by_name_route": "Dispatches _peer_send_by_name_impl with asyncio.to_thread.",
        "peer_inbox_route": "Dispatches _peer_inbox_blocking with asyncio.to_thread.",
        "peer_drain_route": "Dispatches _peer_drain_blocking with asyncio.to_thread.",
        "peer_delivered_route": "Dispatches _peer_delivered_blocking with asyncio.to_thread.",
    }
)


def _positive_int_env(key: str, default: int, minimum: int) -> int:
    value = os.environ.get(key)
    if value is None:
        return default
    try:
        parsed = int(value)
        if 0 < parsed <= sys.maxsize:
            if parsed < minimum:
                logger.warning("LOOP_INVARIANT_CONFIG %s=%r is below %s; using %s", key, value, minimum, minimum)
                return minimum
            return parsed
    except ValueError:
        pass
    logger.warning("LOOP_INVARIANT_CONFIG %s=%r is not a supported positive integer; using default %s", key, value, default)
    return default


@dataclass(frozen=True)
class HandlerEntryConfig:
    """Startup limits, read when a recorder is constructed."""

    entry_capacity: int = DEFAULT_ENTRY_CAPACITY
    stack_frame_limit: int = DEFAULT_STACK_FRAME_LIMIT
    stack_char_limit: int = DEFAULT_STACK_CHAR_LIMIT

    @classmethod
    def from_environment(cls) -> HandlerEntryConfig:
        return cls(
            entry_capacity=_positive_int_env("ANANTA_LOOP_INVARIANT_ENTRY_CAPACITY", DEFAULT_ENTRY_CAPACITY, MIN_ENTRY_CAPACITY),
            stack_frame_limit=_positive_int_env("ANANTA_LOOP_INVARIANT_STACK_FRAME_LIMIT", DEFAULT_STACK_FRAME_LIMIT, MIN_STACK_FRAME_LIMIT),
            stack_char_limit=_positive_int_env("ANANTA_LOOP_INVARIANT_STACK_CHAR_LIMIT", DEFAULT_STACK_CHAR_LIMIT, MIN_STACK_CHAR_LIMIT),
        )


class EntrySnapshot(TypedDict):
    sequence: int
    process_key: str
    kind: str
    thread_id: int
    on_loop: bool
    recorded_at: float
    dispatch_only_reason: str | None
    stack: str | None


class ViolationSummary(TypedDict):
    count: int
    last_process_key: str | None
    last_stack: str | None


class ViolationSnapshot(ViolationSummary):
    recent: list[EntrySnapshot]


class WallTimeSnapshot(TypedDict):
    count: int
    total_s: float
    max_s: float
    last_s: float


class HandlerWallTimeSnapshot(TypedDict):
    evicted_keys: int
    by_process_key: dict[str, WallTimeSnapshot]


class InvariantSnapshot(TypedDict):
    invocations: int
    capacity: int
    recent_entries: list[EntrySnapshot]
    violations: ViolationSnapshot
    handler_wall_time: HandlerWallTimeSnapshot


@dataclass
class _WallTime:
    count: int = 0
    total_s: float = 0.0
    max_s: float = 0.0
    last_s: float = 0.0

    def add(self, elapsed_s: float) -> None:
        self.count += 1
        self.total_s += elapsed_s
        self.max_s = max(self.max_s, elapsed_s)
        self.last_s = elapsed_s

    def snapshot(self) -> WallTimeSnapshot:
        return {"count": self.count, "total_s": self.total_s, "max_s": self.max_s, "last_s": self.last_s}


@dataclass(frozen=True)
class _Entry:
    sequence: int
    process_key: str
    kind: str
    thread_id: int
    on_loop: bool
    recorded_at: float
    dispatch_only_reason: str | None
    stack: str | None

    def snapshot(self) -> EntrySnapshot:
        return {
            "sequence": self.sequence,
            "process_key": self.process_key,
            "kind": self.kind,
            "thread_id": self.thread_id,
            "on_loop": self.on_loop,
            "recorded_at": self.recorded_at,
            "dispatch_only_reason": self.dispatch_only_reason,
            "stack": self.stack,
        }


class HandlerEntryRecorder:
    """Thread-safe bounded history; no exceptions are enabled by default."""

    def __init__(self, dispatch_only: Mapping[tuple[str, str], str] | None = None, config: HandlerEntryConfig | None = None) -> None:
        self.config = config or HandlerEntryConfig.from_environment()
        self._dispatch_only = MappingProxyType(dict(dispatch_only or {}))
        self._lock = threading.Lock()
        self._entries: deque[_Entry] = deque(maxlen=self.config.entry_capacity)
        self._violations: deque[_Entry] = deque(maxlen=self.config.entry_capacity)
        self._invocations = 0
        self._violation_count = 0
        self._wall_time: OrderedDict[str, _WallTime] = OrderedDict()
        self._wall_time_evicted_keys = 0

    def record_wall_time(self, process_key: str, elapsed_s: float) -> None:
        """Add one timed call; most recent key last, oldest evicted past capacity."""
        with self._lock:
            row = self._wall_time.get(process_key)
            if row is None:
                row = self._wall_time[process_key] = _WallTime()
                if len(self._wall_time) > self.config.entry_capacity:
                    self._wall_time.popitem(last=False)
                    self._wall_time_evicted_keys += 1
            else:
                self._wall_time.move_to_end(process_key)
            row.add(elapsed_s)

    def record(self, process_key: str, kind: str) -> None:
        # Import lazily: loop_lag's watchdog also reads our counters. The
        # registry owns loop registration and removal; do not duplicate it.
        from ananta.core.runtime import loop_lag

        thread_id = threading.get_ident()
        loop_ids = {monitor.thread_id for monitor in loop_lag.LOOP_LAG._active()}
        on_loop = thread_id in loop_ids
        reason = self._dispatch_only.get((process_key, kind))
        violation = on_loop and not reason
        stack = "".join(traceback.format_stack(limit=self.config.stack_frame_limit))[-self.config.stack_char_limit :] if violation else None
        with self._lock:
            self._invocations += 1
            entry = _Entry(self._invocations, process_key, kind, thread_id, on_loop, time.time(), reason, stack)
            self._entries.append(entry)
            if violation:
                self._violation_count += 1
                self._violations.append(entry)
        if violation:
            logger.error("LOOP_INVARIANT_VIOLATION process_key=%s kind=%s thread_id=%s stack=%s", process_key, kind, thread_id, stack)

    def snapshot(self) -> InvariantSnapshot:
        with self._lock:
            entries = tuple(self._entries)
            violations = tuple(self._violations)
            invocations = self._invocations
            count = self._violation_count
            wall_time = {key: row.snapshot() for key, row in self._wall_time.items()}
            evicted_keys = self._wall_time_evicted_keys
        last = violations[-1] if violations else None
        return {
            "invocations": invocations,
            "capacity": self.config.entry_capacity,
            "recent_entries": [entry.snapshot() for entry in entries],
            "violations": {
                "count": count,
                "last_process_key": last.process_key if last else None,
                "last_stack": last.stack if last else None,
                "recent": [entry.snapshot() for entry in violations],
            },
            "handler_wall_time": {"evicted_keys": evicted_keys, "by_process_key": wall_time},
        }

    def violation_summary(self) -> ViolationSummary:
        with self._lock:
            last = self._violations[-1] if self._violations else None
            return {
                "count": self._violation_count,
                "last_process_key": last.process_key if last else None,
                "last_stack": last.stack if last else None,
            }


HANDLER_ENTRIES = HandlerEntryRecorder({(f"route::agent_messaging_plugin::{name}", "route"): reason for name, reason in DISPATCH_ONLY_ROUTES.items()})


def record_handler_entry(process_key: str, kind: str) -> None:
    """Call immediately before the body, in its executing thread context."""
    HANDLER_ENTRIES.record(process_key, kind)


def record_handler_wall_time(process_key: str, elapsed_s: float) -> None:
    """Record one handler call's elapsed seconds; advisory, never refuses."""
    HANDLER_ENTRIES.record_wall_time(process_key, elapsed_s)


def handler_entry_snapshot() -> InvariantSnapshot:
    """Read current counters and copies of bounded history without I/O."""
    return HANDLER_ENTRIES.snapshot()


def handler_violation_summary() -> ViolationSummary:
    """Read only counters and the last stack for periodic summary logging."""
    return HANDLER_ENTRIES.violation_summary()
