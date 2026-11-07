"""FSEvents watcher: trigger ``BridgeTracker.restart_each`` on bridge-port change.

macOS-native FSEvents via the pyobjc ``FSEvents`` framework. Watches
the runtime directory the bridge port file lives in and, on every
event for ``<solet>.bridge.port`` or its coalesced watched directory,
calls into the supplied tracker to restart every tracked bridge.

The watcher runs on a dedicated thread spawned by the plugin's
``prepare_for_readiness``. The thread owns a CFRunLoop that the
FSEventStream schedules into; ``stop`` flips a stop event and signals
the runloop to exit so plugin shutdown stays bounded.

Per D-W-SLICE-5-3 Architect recommends pyobjc direct — the plugin is
explicitly macOS-scope so the dependency is fine and the FSEvents API
fidelity beats third-party abstractions on macOS editor-write quirks.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from macos_coding_agent_session_plugin import bridge_tracker
from macos_coding_agent_session_plugin.constants import DEFAULT_FSEVENTS_LATENCY_SECONDS

RestartCallback = Callable[[], None]


def _read_bridge_port_content(port_file: Path) -> str | None:
    """Return the stripped bridge-port file content, or ``None`` on empty/unreadable."""
    try:
        content = port_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def bridge_port_changed(
    port_file: Path,
    last_seen: str | None,
) -> tuple[bool, str | None]:
    """Decide whether a bridge-port filesystem event reflects a real value change.

    The watcher must act on CONTENT change, not on raw filesystem events:
    the router's watchdog rewrites ``<solet>.bridge.port`` every ~5s
    with the SAME value (idempotent re-materialisation — load-bearing, do
    not remove). The pre-fix watcher restarted ALL tracked bridges on every
    such write; once the tracked set grows under multi-session load, each
    5s tick kills+respawns the whole set and emits a per-bridge log line —
    restart amplification that saturates the log and storms MCP. (Observed
    steady churn ~12/min == exactly one per 5s == the watchdog cadence,
    confirming the same-value write is the trigger, not an event-rate
    runaway.) Restarting only on an actual value change honours the
    watcher's contract (react to a router blue-green swap) and makes the
    same-value rewrite a no-op. Any other no-content-change touch (e.g. a
    read-open atime tick) is likewise gated to a cheap no-op.

    Returns ``(changed, current)``. ``changed`` is ``False`` when the file
    is unreadable/empty (a transient partial write — treated as a no-op so
    a partial write never storms; the next settled write is caught against
    the retained ``last_seen``) or when the content equals ``last_seen``.
    When ``changed`` is ``False`` the returned content is ``last_seen``
    unchanged, so the caller's retained value is never clobbered by a
    transient read.
    """
    current = _read_bridge_port_content(port_file)
    if current is None or current == last_seen:
        return (False, last_seen)
    return (True, current)


def _relevant_event(path: object, target_name: str, directory: str) -> bool:
    if isinstance(path, bytes | bytearray):
        path = os.fsdecode(bytes(path))
    # Native delivery can coalesce a fresh file write into its watched root.
    # Re-read the target on that directory hint; content gating still suppresses
    # same-value writes and unrelated filesystem churn.
    return isinstance(path, str) and (path.endswith(target_name) or path.rstrip("/") == directory)


class FSEventsWatcher:
    """Single-threaded FSEventStream watcher around the bridge port file."""

    def __init__(
        self,
        *,
        watch_path: Path,
        target_filename: str,
        on_change: RestartCallback,
        logger: logging.Logger,
        latency_seconds: float = DEFAULT_FSEVENTS_LATENCY_SECONDS,
    ) -> None:
        self._watch_path = watch_path
        self._target_filename = target_filename
        self._on_change = on_change
        self._logger = logger
        self._latency_seconds = latency_seconds
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._startup_event = threading.Event()
        self._ready = False
        self._stream_lock = threading.Lock()
        self._stream_ref: object | None = None
        self._runloop_ref: object | None = None  # CFRunLoopRef proxy

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the watcher thread; idempotent."""
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._startup_event.clear()
        self._ready = False
        self._thread = threading.Thread(
            target=self._run,
            name="macos-coding-agent-session-fsevents",
            daemon=True,
        )
        self._thread.start()

    def wait_until_ready(self, *, timeout_seconds: float = 30.0) -> bool:
        """Wait for the native stream to start; failure/stop returns false."""
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError("readiness wait must be finite and nonnegative")
        return self._startup_event.wait(timeout_seconds) and self._ready and not self._stop_event.is_set()

    def flush_pending_events(self) -> None:
        """Request native delivery of queued events without blocking for callbacks.

        Useful when a caller needs to observe a completed write promptly. The
        event still travels through the real native stream and content gate.
        Startup must have succeeded; a stopped/failed stream refuses.
        """
        from FSEvents import FSEventStreamFlushAsync  # noqa: PLC0415

        with self._stream_lock:
            if not self._ready or self._stop_event.is_set() or self._stream_ref is None:
                raise RuntimeError("cannot flush an unarmed FSEvents stream")
            FSEventStreamFlushAsync(self._stream_ref)

    def stop(self, *, join_timeout_seconds: float = 2.0) -> None:
        """Signal the runloop to exit and join the watcher thread."""
        self._stop_event.set()
        self._ready = False
        self._startup_event.set()
        runloop = self._runloop_ref
        if runloop is not None:
            try:  # pragma: no cover — exercised only when pyobjc is present
                from CoreFoundation import CFRunLoopStop  # noqa: PLC0415

                CFRunLoopStop(runloop)
            except ImportError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout_seconds)
        self._thread = None
        self._runloop_ref = None

    # ------------------------------------------------------------------
    # Thread target
    # ------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._run_inner()
        except Exception:  # noqa: BLE001 — watcher thread must never crash silent
            self._logger.exception("FSEvents watcher thread crashed")
        finally:
            self._ready = False
            self._startup_event.set()

    def _run_inner(self) -> None:
        try:
            from CoreFoundation import (  # noqa: PLC0415
                CFRunLoopGetCurrent,
                CFRunLoopRun,
                kCFRunLoopDefaultMode,
            )
            from FSEvents import (  # noqa: PLC0415
                FSEventsGetCurrentEventId,
                FSEventStreamCreate,
                FSEventStreamInvalidate,
                FSEventStreamRelease,
                FSEventStreamScheduleWithRunLoop,
                FSEventStreamStart,
                FSEventStreamStop,
                kFSEventStreamCreateFlagFileEvents,
                kFSEventStreamCreateFlagNoDefer,
            )
        except ImportError:
            self._logger.error(
                "FSEvents watcher disabled: pyobjc-framework-FSEvents missing. MCP bridges will NOT auto-reconnect on router blue-green swap.",
            )
            return
        target = str(self._watch_path)
        # Capture a concrete native cursor before the content baseline. SinceNow
        # lost an immediate post-start mutation in measured fresh-directory
        # runs; replay from this cursor retains that startup window.
        since = FSEventsGetCurrentEventId()
        callback = self._build_callback()
        stream = FSEventStreamCreate(
            None,
            callback,
            None,
            [target],
            since,
            self._latency_seconds,
            kFSEventStreamCreateFlagNoDefer | kFSEventStreamCreateFlagFileEvents,
        )
        if stream is None:
            self._logger.error(
                "FSEventStreamCreate returned None for path=%s",
                target,
            )
            return
        runloop = CFRunLoopGetCurrent()
        self._runloop_ref = runloop
        started = False
        try:
            FSEventStreamScheduleWithRunLoop(stream, runloop, kCFRunLoopDefaultMode)
            started = bool(FSEventStreamStart(stream))
            if not started:
                raise RuntimeError(f"FSEventStreamStart failed for path={target}")
            if self._stop_event.is_set():
                return
            with self._stream_lock:
                self._stream_ref = stream
                self._ready = True
            self._startup_event.set()
            self._logger.info(
                "FSEvents watcher armed on %s (target=%s)",
                target,
                self._target_filename,
            )
            CFRunLoopRun()
        finally:
            with self._stream_lock:
                self._ready = False
                self._stream_ref = None
                if started:
                    FSEventStreamStop(stream)
                FSEventStreamInvalidate(stream)
                FSEventStreamRelease(stream)

    def _build_callback(self) -> Callable[..., None]:
        target_name = self._target_filename
        on_change = self._on_change
        stop_event = self._stop_event
        logger = self._logger
        port_file = self._watch_path / self._target_filename
        watched_directory = str(self._watch_path.resolve())
        # Baseline read at arm time so the first same-value rewrite after the
        # stream starts is correctly a no-op.
        state: dict[str, str | None] = {"last": _read_bridge_port_content(port_file)}

        def _callback(
            stream: object,
            info: object,
            num_events: int,
            event_paths: object,
            event_flags: object,
            event_ids: object,
        ) -> None:
            del stream, info, event_flags, event_ids
            if stop_event.is_set():
                return
            try:
                paths: list[object] = list(cast("list[object]", event_paths))
            except TypeError:
                paths = []
            logger.debug(
                "FSEvents callback fired n=%d paths=%r",
                num_events,
                paths,
            )
            relevant = any(_relevant_event(path, target_name, watched_directory) for path in paths)
            if not relevant:
                return
            changed, current = bridge_port_changed(port_file, state["last"])
            if not changed:
                # Same-value rewrite (router 5s watchdog) or any other
                # no-content-change touch — NOT a real swap. Skip the restart;
                # restarting on these is the reconnect-storm amplification.
                logger.debug(
                    "FSEvents touch on %s with unchanged content (%r) — skipping restart",
                    target_name,
                    current,
                )
                return
            logger.info(
                "FSEvents detected bridge-port change on %s (%r -> %r, n=%d) — restarting tracked bridges",
                target_name,
                state["last"],
                current,
                num_events,
            )
            state["last"] = current
            try:
                on_change()
            except Exception:  # noqa: BLE001 — callback must keep firing
                logger.exception("FSEvents on_change callback raised")

        return _callback


def build_restart_callback(
    tracker: bridge_tracker.BridgeTracker,
    logger: logging.Logger,
) -> RestartCallback:
    """Bind a ``restart_each`` over the tracker's current id set."""

    def _restart() -> None:
        ids = tracker.tracked_ids()
        if not ids:
            return
        bridge_tracker.restart_each(tracker, ids, logger)

    return _restart
