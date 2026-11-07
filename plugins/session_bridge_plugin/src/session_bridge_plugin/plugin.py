"""Session dispatch bridge plugin (W4 hook bridge, M1).

A background-plumbing ``ServicePlugin`` that drains the Claude Code dispatch hook
spool (``~/.ananta/_session/spool/dispatch/``) into this solet's
``memory_service`` on a periodic tick, so peers (coordinator, watchdog, Codex)
see live ``Task`` dispatch state via tag-scoped recall. Pure plumbing — it
surfaces NO inference-facing EDGE process.

Per design OQ-1 (``workbench/2026-05-27_w4_hook_bridge_design.md``) this plugin
owns the spool-line schema (``spool_schema``), the in-process drain task, and the
write-surface adapter (``drainer``). The cursor/janitor machinery and the cloud
fan-out drainer are M1.5, deliberately out of scope here.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from ananta.core.domain.types import ActionResult
from ananta.core.plugins.decorators import service_lifecycle
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition, EdgeProcessProvider
from ananta.interfaces.memory_service_interface import MemoryServiceInterface

from .drainer import SpoolDrainer
from .janitor import spool_setting

PLUGIN_NAME = "session_bridge_plugin"
DEFAULT_DRAIN_INTERVAL_SECONDS = 5.0
DRAIN_INTERVAL_SECONDS = spool_setting("SOLET_SESSION_BRIDGE_DRAIN_INTERVAL_SECONDS", DEFAULT_DRAIN_INTERVAL_SECONDS)
_SPOOL_SUBPATH = ("_session", "spool", "dispatch")
_CURSOR_SUBDIR = "cursors"  # design line 88: cursors/<solet>.cursor
_JANITOR_LOCK_NAME = ".janitor.lock"  # brief D4: sibling of dispatch/ under spool/
_SOLET_NAME_ENV = "SOLET_NAME"  # the drainer id (design D1; one drainer per solet)
_JOIN_TIMEOUT_SECONDS = 30.0


def _default_spool_dir() -> Path:
    """The host-level singleton spool home (design §3 D2)."""
    return Path.home().joinpath(".ananta", *_SPOOL_SUBPATH)


def _require_drainer_id() -> str:
    """The drainer id is this solet's name (design §3 D2.2 / D1).

    ``SOLET_NAME`` is a required runtime invariant (``launch.py`` sets it);
    a drainer with no stable id cannot own a cursor, so a missing value is a fatal
    misconfiguration — fail fast and loud, never invent a default (KB
    "Critical Development Guidelines v2")."""
    drainer_id = os.environ.get(_SOLET_NAME_ENV)
    if not drainer_id:
        raise RuntimeError(f"{PLUGIN_NAME}: {_SOLET_NAME_ENV} is unset; the drainer cannot own a cursor without a stable solet id (launch.py must set it).")
    return drainer_id


def _completed(message: str, started_at: str | None = None) -> ActionResult:
    data: dict[str, object] = {"message": message}
    if started_at is not None:
        data["started_at"] = started_at
    return ActionResult(
        action_status="completed",
        data=data,
        actions=[],
        error=None,
        timestamp=datetime.now(UTC).isoformat(),
    )


# A single running operation and FIFO async turn reservations preserve ordering.
# Waiting callers never occupy an executor worker or its submission queue.
# The process owns this pool across plugin stop/restart; Python retires it at exit.
_F5_WORKERS = 1
_F5_PENDING_BOUND = 1
_F5_EXECUTOR = ThreadPoolExecutor(max_workers=_F5_WORKERS, thread_name_prefix="session_bridge-f5")
_F5_SUBMISSION_LOCK = threading.RLock()
_f5_in_flight: Future[object] | None = None
_f5_turn: Future[None] | None = None
_f5_waiters: deque[Future[None]] = deque()
logging.getLogger(__name__).info("F5 executor session_bridge-f5: workers=%s pending=%s", _F5_WORKERS, _F5_PENDING_BOUND)


def _release_f5_turn(turn: Future[None]) -> None:
    global _f5_turn
    with _F5_SUBMISSION_LOCK:
        if _f5_turn is not turn:
            return
        if _f5_waiters:
            next_turn = _f5_waiters.popleft()
            _f5_turn = next_turn
            next_turn.set_result(None)
        else:
            _f5_turn = None


def _cancel_f5_waiter(turn: Future[None]) -> None:
    with _F5_SUBMISSION_LOCK:
        if _f5_turn is turn:
            _release_f5_turn(turn)
        elif turn in _f5_waiters:
            _f5_waiters.remove(turn)
            turn.cancel()


def _complete_f5_operation(turn: Future[None], done: Future[object]) -> None:
    global _f5_in_flight
    with _F5_SUBMISSION_LOCK:
        if _f5_in_flight is done:
            _f5_in_flight = None
        _release_f5_turn(turn)


def _report_f5_late_failure(done: Future[object]) -> None:
    error = done.exception()
    if error is not None:
        logging.getLogger(__name__).error("F5 operation failed after its waiter cancelled: %s", error)


async def _run_f5_blocking[T](operation: Callable[[], T]) -> T:
    """Reserve FIFO order before waiting; cancellation retains active ownership."""
    global _f5_in_flight, _f5_turn
    turn = Future[None]()
    with _F5_SUBMISSION_LOCK:
        if _f5_turn is None:
            _f5_turn = turn
            turn.set_result(None)
        else:
            _f5_waiters.append(turn)
    try:
        await asyncio.shield(asyncio.gather(asyncio.wrap_future(turn), return_exceptions=True))
    except asyncio.CancelledError:
        _cancel_f5_waiter(turn)
        raise
    try:
        with _F5_SUBMISSION_LOCK:
            future = _F5_EXECUTOR.submit(copy_context().run, operation)
            _f5_in_flight = cast(Future[object], future)
    except BaseException:
        _release_f5_turn(turn)
        raise
    future.add_done_callback(lambda done: _complete_f5_operation(turn, cast(Future[object], done)))
    try:
        await asyncio.shield(asyncio.gather(asyncio.wrap_future(future), return_exceptions=True))
    except asyncio.CancelledError:
        future.add_done_callback(lambda done: _report_f5_late_failure(cast(Future[object], done)))
        raise
    return future.result()


class SessionBridgePlugin(ServicePlugin, EdgeProcessProvider):
    """Drains the dispatch hook spool into ``memory_service`` on a periodic tick."""

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger = logging.getLogger(self.name)
        self._memory_service: MemoryServiceInterface | None = None
        spool_dir = _default_spool_dir()
        self._drainer = SpoolDrainer(
            drainer_id=_require_drainer_id(),
            spool_dir=spool_dir,
            cursor_dir=spool_dir / _CURSOR_SUBDIR,
            lock_path=spool_dir.parent / _JANITOR_LOCK_NAME,
            logger=self.logger,
        )
        self._stop_event = threading.Event()
        self._worker_thread: threading.Thread | None = None
        self._active: bool = True
        self._drain_lock = threading.Lock()
        self._drain_executor: ThreadPoolExecutor | None = None
        self._drain_owner = threading.local()

    # -- service injection -------------------------------------------------

    def set_memory_service(self, memory_service: MemoryServiceInterface) -> None:
        """Injected by ``startup_sequence._inject_memory_service`` for any plugin
        that exposes this setter."""
        self._memory_service = memory_service
        self.logger.debug("memory_service injected into %s", self.name)

    # -- ServicePlugin lifecycle ------------------------------------------

    @service_lifecycle(operation="start")
    async def start_services(self) -> ActionResult:
        """Start the periodic drain worker. Idempotent."""
        return await _run_f5_blocking(self._f5_start_services)

    def _f5_start_services(self) -> ActionResult:
        """Start the periodic drain worker. Idempotent."""
        if self._services_started:
            return _completed("session bridge drainer already running")
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name=f"{PLUGIN_NAME}-drainer",
            daemon=False,
        )
        self._worker_thread.start()
        self._services_started = True
        self._service_started_at = datetime.now(UTC).isoformat()
        self.logger.debug("session bridge drainer started")
        return _completed("session bridge drainer started", self._service_started_at)

    @service_lifecycle(operation="stop")
    async def stop_services(self) -> ActionResult:
        """Stop the drain worker gracefully. Idempotent."""
        return await _run_f5_blocking(self._f5_stop_services)

    def _f5_stop_services(self) -> ActionResult:
        """Stop the drain worker gracefully. Idempotent."""
        if not self._services_started:
            self._close_drain_executor()
            return _completed("session bridge drainer already stopped")
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
            if self._worker_thread.is_alive():
                self.logger.error("session bridge drainer did not stop within timeout")
        self._worker_thread = None
        self._close_drain_executor()
        self._services_started = False
        self._service_started_at = None
        self.logger.debug("session bridge drainer stopped")
        return _completed("session bridge drainer stopped")

    # -- worker ------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Drain on startup, then every ``DRAIN_INTERVAL_SECONDS`` (design §3 D2:
        startup + short interval, never only on startup)."""
        while not self._stop_event.is_set():
            self.drain_once()
            self._stop_event.wait(DRAIN_INTERVAL_SECONDS)
        self.logger.debug("session bridge drainer loop exited")

    def drain_once(self) -> int:
        """One-shot drain; returns files drained. Safe to call directly (used by
        smoke tests and the periodic worker). No-op until ``memory_service`` is
        injected, so start-vs-inject ordering is irrelevant. Returns 0 when the
        plugin is the inactive color (see ``set_active``)."""
        if not self._active:
            return 0
        if self._memory_service is None:
            return 0
        if getattr(self._drain_owner, "active", False):
            return self._drain_on_worker()
        # Admission is held through completion: this executor can have only
        # one submitted job, including direct calls racing the periodic tick.
        with self._drain_lock:
            if self._drain_executor is None:
                self._drain_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-bridge-drain")
            return self._drain_executor.submit(self._drain_on_worker).result()

    def _drain_on_worker(self) -> int:
        previous = getattr(self._drain_owner, "active", False)
        self._drain_owner.active = True
        try:
            if not self._active or self._memory_service is None:
                return 0
            return self._drainer.drain_once(self._memory_service)
        except Exception:
            self.logger.exception("session bridge drain tick failed")
            return 0
        finally:
            self._drain_owner.active = previous

    def _close_drain_executor(self) -> None:
        with self._drain_lock:
            if self._drain_executor is not None:
                self._drain_executor.shutdown(wait=True)
                self._drain_executor = None

    def set_active(self, active: bool) -> None:
        """Gate the drain tick on color-active state (per L3 blue-green slice D).

        When called with ``False`` the next ``drain_once`` returns 0 without
        touching the spool; ``True`` resumes drains. The worker thread keeps
        ticking either way — quiescence is at the tick boundary, not at the
        thread lifecycle, so resume is instant.
        """
        self._active = active

    # -- edge surface (none in M1) ----------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """No inference-facing EDGE process — this plugin is background plumbing."""
        return {}
