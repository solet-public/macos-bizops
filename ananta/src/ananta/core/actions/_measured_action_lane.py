"""One selected task per lane per poller, using its dispatcher.

Each lane (``LANES``) runs one row at a time in FIFO order, off the serial
drain loop and independently of every other lane: a long seed run never delays
a session teardown, and neither delays ordinary actions.
Busy reads scan one bounded page per poll, skipping only the queued rows of a
lane whose task is running. Queue depth then counts dispatch-eligible page rows,
not total queue storage.
Mixed pages retain their cursor until ordinary rows drain; idle reads reset it.
Finite selected backlogs progress; infinite lower-key inserts and other classes
or pin-only saturation have no fairness bound here.
Uncertain effects retain row-level reaper protection, never execution ownership.
Shutdown drains the selected action through its terminal write. External task
cancellation still protects an uncertain row; it cannot stop an executor thread.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import Context, copy_context
from dataclasses import dataclass, field
from itertools import islice
from types import MappingProxyType
from typing import TYPE_CHECKING, Final, cast
from weakref import WeakKeyDictionary

from ananta.core.domain.types import ActionResult
from ananta.core.plugins.plugin_contracts import ActionStatus

if TYPE_CHECKING:
    from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction

logger = logging.getLogger(__name__)
# Main ruling agm-_d8377b3ecffe41fbbb9fc17ca7f14036. Measured in the
# 2026-10-30_profile.log on Nov 6: list_models 10.3 s (line 258967), seed
# verify status 11.9 s (259834). Both read independent state, as do the F4
# release status/list readers. Main's additional ruling
# agm-_f1813e508e43460abae369a7ddd705b5 explicitly assigns the synchronous
# seed handlers here (F4 census P14-P16: filesystem/Git/subprocess assembly,
# sealing and publishing; verification/release job dispatch and status DB).
# All nine seed entrypoints share this one barrier, so a later seed publish
# cannot overtake an earlier seal. Their own order remains FIFO, while ordinary
# independent actions can progress. report_alive is a victim, never selected.
# Bridge deliver_result remains serial: bridge event ordering has not been
# proved commutative with unselected deliveries. No ingest key is selected.
HEAVY_PROCESS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "service_interface::embedding_service::list_models",
        "plugin::seed_factory_plugin::assemble_seed",
        "plugin::seed_factory_plugin::validate_and_seal_seed_bundle",
        "plugin::seed_factory_plugin::publish_seed",
        "plugin::seed_factory_plugin::verify_seed_bundle",
        "plugin::seed_factory_plugin::verify_seed_bundle_status",
        "plugin::seed_factory_plugin::reproduce_release",
        "plugin::seed_factory_plugin::publish_release",
        "plugin::seed_factory_plugin::release_run_status",
        "plugin::seed_factory_plugin::list_release_runs",
    }
)
MEASURED_PROCESS_KEYS: Final[frozenset[str]] = (
    frozenset(
        {
            "service_interface::embedding_service::generate_embeddings",
            "service_interface::knowledge_service::search",
        }
    )
    | HEAVY_PROCESS_KEYS
)
# iss_bc364208: re-measured 2026-11-07 in profile/data/logs, retire_session
# held the serial loop 18.9-23.8 s on every 11-06 retire and 19.0 s on 11-07
# (48.9 s on 11-06 morning), and the spawn_session submission (admission plus
# assignment binding before its launch job) 10.1-16.1 s; the bridge long poll
# is 25 s. Session teardown and launch submission share their own FIFO lane, so
# two of them never overlap and keep the order the serial loop gave them, while
# ordinary actions (report_alive, peer_send, deliver_result) no longer wait.
# Lifecycle writes are predicated (transition_lifecycle_state from_state), so a
# concurrent ordinary action cannot be overwritten by a stale lane write.
SESSION_LIFECYCLE_PROCESS_KEYS: Final[frozenset[str]] = frozenset(
    {
        "plugin::agent_messaging_plugin::spawn_session",
        "plugin::agent_messaging_plugin::terminate_session",
        "plugin::agent_messaging_plugin::retire_session",
        "plugin::agent_messaging_plugin::retire_completed_lane_sessions",
    }
)
LANES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "measured": MEASURED_PROCESS_KEYS,
        "session_lifecycle": SESSION_LIFECYCLE_PROCESS_KEYS,
    }
)
_LANE_OF: Final[Mapping[str, str]] = MappingProxyType({key: name for name, keys in LANES.items() for key in keys})
if len(_LANE_OF) != sum(len(keys) for keys in LANES.values()):
    raise RuntimeError("A process key is assigned to more than one action lane")
PROCESS_KEYS: Final[frozenset[str]] = frozenset(_LANE_OF)
ADVISORY_INTERVAL: Final[float] = 120.0

# One process-owned F2 state/orchestration worker preserves the order of sync
# callbacks, including read-compute-write helpers. The executor admits at most
# 32 running+queued calls; further callers await admission without refusing work
# or occupying the loop/default executor. Python joins this worker at exit.
STATE_WORKERS: Final[int] = 1
STATE_PENDING: Final[int] = 32
_state_executor = ThreadPoolExecutor(max_workers=STATE_WORKERS, thread_name_prefix="poller-state")
_state_lock = threading.Lock()
_state_waiters: deque[asyncio.Future[None]] = deque()
_state_outstanding = 0
_state_worker = threading.local()
logger.info("POLLER_STATE_EXECUTOR: workers=%d pending=%d", STATE_WORKERS, STATE_PENDING)


def _grant_state_call(waiter: asyncio.Future[None]) -> None:
    if waiter.done():
        _release_state_call()
    else:
        waiter.set_result(None)


def _release_state_call() -> None:
    global _state_outstanding
    with _state_lock:
        while _state_waiters:
            waiter = _state_waiters.popleft()
            if waiter.done():
                continue
            try:
                waiter.get_loop().call_soon_threadsafe(_grant_state_call, waiter)
            except RuntimeError:  # The caller's loop closed before admission.
                continue
            return
        _state_outstanding -= 1


async def _admit_state_call() -> None:
    global _state_outstanding
    waiter = asyncio.get_running_loop().create_future()
    with _state_lock:
        if _state_outstanding < STATE_PENDING:
            _state_outstanding += 1
            waiter.set_result(None)
        else:
            _state_waiters.append(waiter)
    try:
        await waiter
    except asyncio.CancelledError:
        if waiter.done() and not waiter.cancelled():
            _release_state_call()
        raise


def _invoke_state_call[**P, R](context: Context, function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    _state_worker.active = True
    try:
        return context.run(function, *args, **kwargs)
    finally:
        _state_worker.active = False


async def run_blocking[**P, R](function: Callable[P, R], *args: P.args, **kwargs: P.kwargs) -> R:
    """Await an F2 sync operation with context and actual worker lifetime intact.

    A nested async facade called by a sync service already on this worker runs
    inline on its private loop, avoiding self-deadlock. Cancellation drains a
    submitted operation before propagating; it cannot abandon a status write.
    """
    if getattr(_state_worker, "active", False) is True:
        return function(*args, **kwargs)
    await _admit_state_call()
    context = copy_context()
    try:
        submitted = _state_executor.submit(_invoke_state_call, context, function, *args, **kwargs)
    except BaseException:
        _release_state_call()
        raise
    submitted.add_done_callback(lambda _future: _release_state_call())
    result = asyncio.wrap_future(submitted)
    try:
        return await asyncio.shield(result)
    except asyncio.CancelledError:
        while not result.done():
            try:
                await asyncio.shield(result)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        error = result.exception()
        if error is not None:
            logger.error("POLLER_STATE_CANCELLED: submitted operation failed while draining: %s", error)
        raise


@dataclass
class _Slot:
    """One lane's running row and its per-cycle FIFO barrier."""

    task: asyncio.Task[None] | None = None
    action: QueuedAction | None = None
    started: float = 0.0
    advised: float = 0.0
    barrier: bool = False


@dataclass
class _State:
    slots: dict[str, _Slot] = field(default_factory=dict)
    protected_advised: float = 0.0
    protected: dict[str, str] = field(default_factory=dict)
    cursor: tuple[object, str] | None = None
    #: Lanes whose queued rows the cursor has skipped; see ``_valid_cursor``.
    cursor_lanes: frozenset[str] = frozenset()


_states: WeakKeyDictionary[ActionQueuePoller, _State] = WeakKeyDictionary()


def color_gated_off(getter: Callable[[], bool] | None) -> bool:
    """Preserve the poller's existing mid-batch colour check and short circuit."""
    return getter is not None and not getter()


def _active(slot: _Slot) -> bool:
    return slot.task is not None and not slot.task.done()


def _active_lanes(state: _State | None) -> frozenset[str]:
    if state is None:
        return frozenset()
    return frozenset(name for name, slot in state.slots.items() if _active(slot))


def _page(result: ActionResult) -> list[dict[str, object]] | None:
    """Validate auxiliary page shape without changing the provider contract."""
    if result.get("action_status") not in (
        ActionStatus.COMPLETED,
        str(ActionStatus.COMPLETED),
        ActionStatus.COMPLETED.value,
    ):
        return None
    data = result.get("data")
    rows = data.get("records") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return cast("list[dict[str, object]]", rows)


def _selected(row: dict[str, object], active: frozenset[str]) -> bool:
    """Whether a row belongs to a lane that is running now (an idle lane's row is eligible)."""
    key = row.get("process_key")
    return isinstance(key, str) and _LANE_OF.get(key) in active


def _advance(state: _State, rows: list[dict[str, object]]) -> None:
    """Page past running lanes' rows; missing cursor data is advisory, never invented."""
    state.cursor_lanes |= frozenset(_LANE_OF[key] for row in rows if isinstance(key := row.get("process_key"), str) and key in _LANE_OF)
    sequence, action_id = rows[-1].get("sequence"), rows[-1].get("id")
    if sequence is None or not isinstance(action_id, str) or not action_id:
        logger.warning("MEASURED_SCAN: missing cursor; retaining %r", state.cursor)
        return
    state.cursor = (sequence, action_id)


def _valid_cursor(state: _State, active: frozenset[str]) -> tuple[object, str] | None:
    """The scan cursor, dropped once a lane whose rows it skipped is no longer running.

    The skipped rows of that lane are eligible again, and its oldest row must be read
    before any newer row of the same lane, or a newer row could dispatch first.
    """
    if state.cursor is not None and not state.cursor_lanes <= active:
        logger.info("MEASURED_SCAN: lane(s) %s idle; rescanning from the oldest queued row", sorted(state.cursor_lanes - active))
        state.cursor = None
    if state.cursor is None:
        state.cursor_lanes = frozenset()
    return state.cursor


async def query_ordered(
    poller: ActionQueuePoller,
    namespace: str,
    data: dict[str, object],
) -> ActionResult:
    """Read once per poll; only actual task ownership enables selected paging.

    Protected ids alone are idle. Copy successful envelopes before filtering,
    preserve failures/raises, and retain mixed-page boundaries across batch10.
    One page at the shipped poll interval supplies bounded forward progress.
    """
    state = _states.get(poller)
    active = _active_lanes(state)
    if state is None or not active:
        if state is not None:
            state.cursor, state.cursor_lanes = None, frozenset()
        return await run_blocking(poller.state_service.query_ordered, namespace, data)
    request = dict(data)
    cursor = _valid_cursor(state, active)
    if cursor is not None:
        request["after"] = list(cursor)
    result = await run_blocking(poller.state_service.query_ordered, namespace, request)
    rows = _page(result)
    if rows is None:
        logger.warning("MEASURED_SCAN: failed/malformed page; retaining %r", state.cursor)
        return result
    eligible = [row for row in rows if not _selected(row, active)]
    if not rows:
        state.cursor = None
    elif not eligible:
        _advance(state, rows)
    logger.info("MEASURED_SCAN: raw=%d eligible=%d after=%r next=%r", len(rows), len(eligible), request.get("after"), state.cursor)
    return {**result, "data": {**cast("dict[str, object]", result.get("data")), "records": eligible}}


def held(poller: ActionQueuePoller, key: str) -> bool:
    """Hold a lane's successors before CAS, including earlier skips this cycle."""
    lane = _LANE_OF.get(key)
    state = _states.get(poller)
    slot = None if lane is None or state is None else state.slots.get(lane)
    if slot is None:
        return False
    slot.barrier = slot.barrier or _active(slot)
    return slot.barrier


def start(poller: ActionQueuePoller, action: QueuedAction) -> bool:
    """Start a selected row in its lane only after the existing queued-status CAS succeeded."""
    lane = _LANE_OF.get(action.process_key)
    if lane is None:
        return False
    state = _states.setdefault(poller, _State())
    slot = state.slots.setdefault(lane, _Slot())
    slot.action, slot.started, slot.advised = action, time.monotonic(), 0.0
    slot.task = asyncio.create_task(_run(poller, action, state, slot))
    return True


def _protect(state: _State, action: QueuedAction, reason: str, traceback: bool) -> None:
    if action.id not in state.protected:
        state.protected[action.id] = action.process_key
        logger.error("MEASURED_UNCERTAIN: action %s (%s): %s; row remains protected, later selected rows may run", action.id, action.process_key, reason, exc_info=traceback)


async def _run(poller: ActionQueuePoller, action: QueuedAction, state: _State, slot: _Slot) -> None:
    try:
        await poller._dispatch_claimed(action)
    except asyncio.CancelledError:
        _protect(state, action, "cancelled; executor threads may continue running", True)
        raise
    except Exception:
        _protect(state, action, "terminal recording raised", True)
    else:
        state.protected.pop(action.id, None)
    finally:
        logger.info("MEASURED_ACTION: action %s (%s) ended after %.1fs off the serial loop", action.id, action.process_key, time.monotonic() - slot.started)


def live_ids(poller: ActionQueuePoller) -> frozenset[str]:
    """Active and uncertain ids are protected; only the task owns admission."""
    state = _states.get(poller)
    if state is None:
        return frozenset()
    active = {slot.action.id for slot in state.slots.values() if _active(slot) and slot.action is not None}
    return frozenset(state.protected) | active


def _report_active(slot: _Slot, now: float) -> None:
    action = slot.action
    age = now - slot.started
    if action is None or age < ADVISORY_INTERVAL:
        return
    if slot.advised and now - slot.advised < ADVISORY_INTERVAL:
        return
    slot.advised = now
    logger.error("MEASURED_STALLED: action %s (%s) active %.1fs", action.id, action.process_key, age)


def report(poller: ActionQueuePoller) -> None:
    """Begin a new cycle, clearing every lane's FIFO barrier even after uncertainty."""
    state = _states.get(poller)
    if state is None:
        return
    now = time.monotonic()
    for name, slot in list(state.slots.items()):
        if not _active(slot):
            del state.slots[name]
            continue
        slot.barrier = False
        _report_active(slot, now)
    if state.protected and now - state.protected_advised >= ADVISORY_INTERVAL:
        state.protected_advised = now
        logger.error("MEASURED_PROTECTED: %d uncertain rows; sample=%s", len(state.protected), tuple(islice(state.protected.items(), 5)))
    if not state.slots and not state.protected:
        del _states[poller]


async def stop(poller: ActionQueuePoller) -> None:
    """Drain every lane's selected work through completion; keep uncertain rows protected."""
    state = _states.get(poller)
    if state is None:
        return
    for slot in list(state.slots.values()):
        task, action = slot.task, slot.action
        if task is None:
            continue
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if action is not None:
                _protect(state, action, "cancelled; executor threads may continue running", True)
            if not task.cancelled():
                raise
    state.slots.clear()
    if not state.protected:
        del _states[poller]
