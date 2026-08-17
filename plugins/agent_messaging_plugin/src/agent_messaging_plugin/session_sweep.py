"""D1 platform sweep (§3.4 / §6 rule 3 edge ownership) — an ``on_tick`` rider
that piggybacks the existing :class:`BridgeLifecycleSweeper` cadence (same
pattern as the INF-02 serve-timeout sweep / INF-06 forwarded-vertex re-drive /
REL-05 deaf-wake escalation composed in ``plugin.py``'s ``_on_sweep_tick``),
rather than growing a new thread.

Three responsibilities, all owned by "the platform sweep" per the design's own
edge-ownership table:

1. :func:`sweep_overdue_sessions` — mark ``live``/``idle`` rows ``overdue``
   once their ``report_by`` deadline has passed (the report-or-die contract),
   then best-effort notify each row's steward (``spawned_by_instance_id`` —
   D2-lane-tail follow-up #3, fixing a measured gap where the transition
   fired with no notification of any kind). A row with no ``report_by`` (an
   operator-hosted session with no such contract) is never swept — absence
   means no contract, not an expired one. Also sweeps ``spawning`` rows past
   their ``report_by`` deadline (a ``spawn_session`` call whose host process
   never registered) — invisible to earlier versions of this sweep, which
   scanned only ``live``/``idle`` and left such a row stuck in ``spawning``
   forever. ``LIFECYCLE_TRANSITIONS`` has no legal ``spawning -> overdue``
   edge (a spawn that never registered has no live session to recover via a
   late ``report_alive``, unlike an overdue ``live``/``idle`` row), so this
   leg calls :func:`session_lifecycle_verbs.terminate_session` directly —
   reusing its host-driver kill so an OS process still alive but never
   registered is actually reaped, not just marked dead in the ledger — then
   best-effort notifies the steward with a distinct event.
2. :func:`sweep_deadline_dependencies` — fire + deliver armed
   ``session_dependency`` rows whose ``condition_kind == 'deadline'`` and
   ``condition_ref`` (an ISO timestamp) has passed.
3. :func:`sweep_lane_closed_dependencies` — fire + deliver armed rows whose
   ``condition_kind == 'lane_closed'`` once every ``managed_session`` row
   carrying that ``condition_ref`` (a ``lane_id``) is terminal
   (``terminated``/``retired``). A lane with NO managed_session rows yet is
   NOT closed (no vacuous truth on an empty set — a lane that never spawned
   is unstarted, not finished).

Scope boundary (deliberate, not an oversight):

* ``condition_kind == 'session_terminal'`` is NOT handled here.
  ``terminate_session`` (``session_lifecycle_verbs.py``) fires + best-effort
  delivers those edges (guarded by ``fired_at IS NULL``); ``retire_session``
  composes ``terminate_session`` and no longer fires them itself. Two
  writers racing to fire the SAME guarded condition is the
  second-guard-makes-the-first-vacuous shape; owning it in exactly one place
  keeps that unambiguous. FULLY RESOLVED 2026-08-04 (acceptance Test C +
  fix-slice completion, coordinator-seat ruling) — this section used to carry TWO
  successive debt claims, both now closed: (1) firing itself never worked
  live — ``_fire_session_terminal_dependencies``' guard filters carried a
  bare ``None`` for ``fired_at`` instead of ``{"op": "is_null"}``, which the
  postgres provider compiles to an always-false ``col = NULL`` comparison,
  matching zero rows, always; fixed (both the query and the predicated
  update). (2) firing carried no delivery — fixed same slice:
  ``_fire_session_terminal_dependencies`` now calls
  :func:`session_lifecycle_verbs.drive_on_delivery` per fired edge,
  best-effort, same containment contract as ``_deliver_dependency_wake``
  above. Both debts closed; no ``session_terminal`` tracked debt remains.
* ``lane_closed`` replaced the original spec's ``lane_landed`` (Dawn ruling
  2026-08-03, arm-124065ee): no canonical "lane landed" observable exists
  platform-side (an ancestor check would bind the platform to a dev git
  checkout — seed instances are not checkouts). ``lane_closed`` is what the
  registry can actually observe; true work-product landing needs a lane
  ENTITY with an audited declaration, deferred to Phase C. No verb in this
  codebase arms a ``session_dependency`` row yet (there is no "create
  dependency" verb) — the evaluator exists so the FIRST caller that does
  isn't waiting on the sweep too.
* A ``deadline``/``lane_closed`` edge with an empty ``waiter_instance_id``
  (lane-scoped) is fired (state) but its wake delivery is a logged no-op —
  resolving "the session currently working lane L" has no defined mapping in
  this slice.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    require_deleted,
    require_records,
    require_updated,
)

from . import rotation_thresholds
from .peer_registry import PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError
from .schema import (
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_ROLE_CLAIM,
)
from .session_context_status_store import read_session_context_status
from .session_hosts import OPERATOR_HOST
from .session_lifecycle_store import (
    DEFAULT_REPORT_BY_SECONDS,
    StaleLifecycleStateError,
    list_managed_sessions,
    mark_registration_overdue,
    transition_lifecycle_state,
)
from .session_lifecycle_verbs import (
    VerbError,
    _rearm_report_by,
    _resolve_termination_driver,
    drive_on_delivery,
    terminate_session,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .models import BridgeBinding
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)

# Longer than the binding-liveness window (90s, bridge_sessions.py's
# DEFAULT_BINDING_LIVENESS_WINDOW_S — 3x the long-poll cadence) and comfortably
# longer than a blue-green cutover's fleet-wide re-registration burst (the
# measured trap Architect ratification #3 names): a row absent for less than
# this is "mid-bounce", not stale.
DEFAULT_PRUNE_GRACE_WINDOW_S: float = 300.0

# Channel event type the deadline wake rides — mirrors REL-05's
# ``ESCALATION_EVENT_TYPE`` naming convention (a distinct type per concern,
# never the generic peer_message type, so a receiver can tell wake classes apart).
EVENT_SESSION_DEPENDENCY_WAKE = "session_dependency_wake"

# Same convention, the D2-lane-tail overdue-steward-notice fix (follow-up
# #3): the report-or-die contract's own steward notification, distinct from
# the session_dependency wake above even though both ride append_event.
EVENT_SESSION_OVERDUE_NOTICE = "session_overdue_notice"

# Same convention again, for the two L4 sweep legs (2026-08-17).
#
# WHY THESE LIVE IN THE SWEEP AND NOT IN A HOOK. The reporting hook binds
# PostToolUse, which fires AFTER a tool call, which follows a model call. So
# during an idle gap it does not tick at all, and by its first tick after
# resuming the full-price prefix rewrite has already been paid. It cannot warn
# before the cost, by construction. The sweep walks sessions on its OWN
# schedule without any session having to act first, which makes it the only
# surface that can observe "this session has been idle a while, its context is
# large, its cache is about to lapse" BEFORE the next call pays for it.
EVENT_ROTATION_DUE_NOTICE = "rotation_due_notice"

# The gauge-coverage leg. Signature it detects: the platform believes a session
# is LIVE (its lifecycle row says so, i.e. report_alive is landing) while NO
# session_context_status row exists for it -- hooks are running, the gauge
# write is not.
#
# This is a MISSING-row detector, not a stale-row detector: a session whose
# gauge froze while keeping an old row is a different case and is deliberately
# out of scope here rather than half-covered.
#
# It is state-based on purpose. The obvious alternative -- compare the hook's
# throttle-marker mtime against the absent row -- needs AGENT_HEARTBEAT_MARKER_DIR,
# which the spawn adapters export to Claude Code sessions and which the platform
# process running this sweep has no reason to hold. A detector that silently
# no-ops when its input is missing is the exact fail-open shape this leg exists
# to catch, so building it that way would put the bug inside its own alarm.
EVENT_GAUGE_COVERAGE_NOTICE = "gauge_coverage_notice"

# Distinct from EVENT_SESSION_OVERDUE_NOTICE so a receiver can tell the two
# classes apart: "went quiet" (overdue, may self-heal) vs "never came up"
# (spawning row past its deadline, terminated outright — see sweep_overdue_
# sessions' spawning leg).
EVENT_SESSION_SPAWN_ORPHANED_NOTICE = "session_spawn_orphaned_notice"

# Third spawn-notice class: "alive but never registered" — the spawning row is
# past its report_by, but the HOST DRIVER OBSERVES the spawned process alive
# (live-measured 2026-08-13: a tmux worker, productive for hours and reporting
# over a side channel, was reaped mid-programme because ledger liveness keyed
# on peer registration alone). Instead of the reaper, the row's deadline is
# re-armed and its steward told — bounded, because the OTHER live-measured
# shape (a hung claude process that sat 'spawning' 8+ hours doing nothing,
# the red-first case the reap itself fixed) is ALSO "alive": a liveness probe
# cannot distinguish productive from hung, so patience runs out.
EVENT_SESSION_SPAWN_UNREGISTERED_NOTICE = "session_spawn_unregistered_notice"

# Total patience for an observed-alive, never-registered spawning row: this
# many of the row's OWN spawn windows (report_by_seconds), measured from the
# row's spawn timestamp (last_transition_at — unchanged while the row stays
# 'spawning'). The first window is the original deadline; the remainder are
# announced re-arms. Past the bound, the reap proceeds even though the
# process is alive — every extension was announced, so the termination is
# never the steward's first news.
SPAWN_ALIVE_PATIENCE_WINDOWS: int = 4

# W4A (#8 §43.1): fourth spawn-notice class, and the one that ATTRIBUTES.
# "This row has not registered and by construction therefore has not run its
# registration hook" — a statement about the seam, not about any policy blob.
EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE = "session_registration_overdue_notice"

# The registration bound, deliberately NOT report_by. Registration is a
# machine-speed event — a worker that is going to register does it seconds
# after its process comes up — so a bound measured in a couple of minutes is
# already generous, and it is SHORTER than DEFAULT_REPORT_BY_SECONDS (300) on
# purpose: the attribution has to land BEFORE the report-or-die machinery
# starts telling a different story about the same row.
DEFAULT_REGISTRATION_BOUND_S: float = 120.0


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp cell to an aware (UTC) datetime.

    Mirrors ``ananta.llm.agent_messaging.service._parse_iso`` exactly (state
    ``DATETIME`` columns read back offset-naive; live clocks are aware UTC —
    coerce once at this boundary so every comparison here is aware-vs-aware).
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _managed_sessions_in_state(
    state: StateManagementInterface, lifecycle_state: str,
) -> list[dict[str, Any]]:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": {"lifecycle_state": lifecycle_state}},
    )
    return require_records(result)


def _managed_session_agent_id(
    state: StateManagementInterface, agent_instance_id: str,
) -> str:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
    )
    rows = require_records(result)
    return str(rows[0].get("agent_id") or "") if rows else ""


def _resolve_steward_via_managed_session(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    spawner_instance_id: str,
) -> BridgeBinding | None:
    """Fallback path for :func:`_notify_steward_of_overdue` — the ORIGINAL
    (pre-fix) resolution route: look up the spawner's ``agent_id`` via its
    own ``managed_session`` row, then resolve ``(agent_id, instance_id)``.
    Kept for a direct registry-by-instance-id miss; no longer the primary
    path since it silently fails for any spawner with no managed_session row
    of its own (the operator-launched-seat case this fix addresses)."""
    spawner_agent_id = _managed_session_agent_id(state, spawner_instance_id)
    if not spawner_agent_id:
        return None
    try:
        return peer_registry.resolve(spawner_agent_id, spawner_instance_id)
    except (PeerUnreachableError, PeerAmbiguousError):
        return None


def _resolve_steward_binding(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    spawner_instance_id: str,
) -> BridgeBinding | None:
    """Shared resolution step for both steward-notify paths below: resolve
    straight from the peer registry by instance id, falling back to the
    managed_session detour on a miss. See :func:`_notify_steward_of_overdue`
    for why the direct lookup is primary."""
    binding = peer_registry.resolve_by_agent_instance_id(spawner_instance_id)
    if binding is None:
        binding = _resolve_steward_via_managed_session(
            state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
        )
    return binding


def _notify_steward_of_overdue(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
) -> None:
    """Best-effort steward notice for one just-marked-overdue row (D2-lane-
    tail follow-up #3 — the report-or-die contract's own promise: "the
    platform sweep marks overdue rows overdue and notifies the steward
    (spawner) through normal messaging"). Mirrors
    :func:`_deliver_dependency_wake`'s exact resolve-then-append pattern —
    the row is already transitioned (state), so a delivery fault here must
    never raise back into the sweep loop and must never block marking the
    OTHER overdue rows in this tick.

    Absent ``spawned_by_instance_id`` (an operator-launched row, or a row
    with no recorded spawner) is silently skipped, not warned — that is a
    session with no steward to notify by construction, not a fault.

    Resolves the steward straight from the peer registry by instance id
    (``PeerRegistry.resolve_by_agent_instance_id`` — a direct lookup, no
    ``agent_id`` needed up front): the ``managed_session``-row detour this
    used to require as its ONLY path fails for the dominant case, an
    operator-launched seat (e.g. the primary seat) that spawned the overdue
    session directly — that seat has no ``managed_session`` row of its own,
    so the notice silently never fired (measured live, 2026-08-04 13:13:01Z,
    session_sweep.py:175). The old managed_session-based resolution stays as
    a fallback for a registry-lookup miss, not the primary path."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = _resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s overdue: spawner %s not resolvable to a live binding "
            "(checked the peer registry directly and via its managed_session "
            "row) — marked overdue, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_overdue_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}) missed its report_by deadline and was "
        "marked overdue. This is the report-or-die contract firing — "
        "silence is detectable by construction. Check the session's "
        "status; direct a recovery report, park, or terminate it."
    )
    meta: dict[str, object] = {"flow_id": f"session-overdue-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_OVERDUE_NOTICE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the row is already marked overdue
        logger.warning(
            "session %s overdue notice append failed", agent_instance_id, exc_info=True,
        )
    # Drive-on-delivery (2026-08-04, slice 2): ALONGSIDE the append_event
    # above, never instead of it. The steward is usually an operator-
    # launched, UNMANAGED session (the seat) — drive_on_delivery's own
    # SessionNotFoundError no-op covers that path byte-unchanged; a managed
    # steward (a spawned session that itself spawned the overdue worker)
    # gets the extra nudge.
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label="session_overdue_notice",
    )


def _notify_steward_of_spawn_orphaned(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
) -> None:
    """Best-effort steward notice for a ``spawning`` row whose spawn process
    never registered before its ``report_by`` deadline — mirrors
    :func:`_notify_steward_of_overdue`'s resolve-then-append pattern, but the
    row has already been terminated outright (not marked overdue): a spawn
    that never registered has no live session to recover via a late
    ``report_alive``, so the notice tells the steward its spawn never came
    up, distinct from "went quiet" (``EVENT_SESSION_SPAWN_ORPHANED_NOTICE``,
    not ``EVENT_SESSION_OVERDUE_NOTICE``)."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = _resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s spawn-orphaned: spawner %s not resolvable to a live "
            "binding (checked the peer registry directly and via its "
            "managed_session row) — marked terminated, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_spawn_orphaned_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}) never registered before its report_by "
        "deadline and was marked terminated. Its spawn process may be hung, "
        "crashed, or otherwise never reached the platform — check for an "
        "orphaned OS process if the host was headless."
    )
    meta: dict[str, object] = {"flow_id": f"session-spawn-orphaned-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_SPAWN_ORPHANED_NOTICE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the row is already terminated
        logger.warning(
            "session %s spawn-orphaned notice append failed", agent_instance_id, exc_info=True,
        )
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label="session_spawn_orphaned_notice",
    )


def _spawning_host_observed_alive(row: dict[str, Any]) -> bool:
    """True ONLY on a genuine host-level observation that the spawned process
    is alive. False on every non-observation: an operator-hosted row (that
    driver's ``alive()`` is an unconditional ``True`` by design — it observes
    via registration only, so its answer is not evidence), a row with no
    ``host_ref``, an unresolvable driver, or a probe that raises. The
    fail-toward direction is deliberate: an unanswerable probe falls back to
    the established reap, never to indefinite patience."""
    host = str(row.get("host") or "")
    host_ref = str(row.get("host_ref") or "")
    if not host_ref or host == OPERATOR_HOST:
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    try:
        driver, _ = _resolve_termination_driver(row, agent_instance_id)
        return bool(driver.alive(host_ref))
    except VerbError:
        return False
    except Exception:  # noqa: BLE001 — a probe that raises is a non-observation, not a sweep failure
        logger.warning(
            "spawning row %s host-liveness probe raised; treating as unobserved",
            agent_instance_id, exc_info=True,
        )
        return False


def _spawn_alive_patience_exhausted(row: dict[str, Any], *, clock: datetime) -> bool:
    """Whether an observed-alive spawning row has used up its bounded patience:
    :data:`SPAWN_ALIVE_PATIENCE_WINDOWS` of its own spawn window, measured from
    the row's spawn timestamp. ``last_transition_at`` IS the spawn timestamp for
    a row still in ``spawning`` — the state has never transitioned. A row with
    no parseable spawn timestamp exhausts immediately (no basis for patience —
    fail toward the established reap)."""
    spawned_at = _parse_iso(row.get("last_transition_at"))
    if spawned_at is None:
        return True
    window = int(row.get("report_by_seconds") or 0) or DEFAULT_REPORT_BY_SECONDS
    return (clock - spawned_at).total_seconds() > window * SPAWN_ALIVE_PATIENCE_WINDOWS


def _notify_steward_of_spawn_unregistered(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
) -> None:
    """Steward notice for an observed-alive, never-registered spawning row
    whose deadline was just re-armed instead of reaped — mirrors
    :func:`_notify_steward_of_spawn_orphaned`'s resolve-then-append pattern.
    The ``flow_id`` is stable per row so repeated extensions thread rather
    than scatter."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = _resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s spawn-unregistered: spawner %s not resolvable to a live "
            "binding — deadline re-armed, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_spawn_unregistered_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}) is past its report_by deadline and has never "
        "registered, but its host process is OBSERVED ALIVE — its deadline was "
        "re-armed instead of reaping it. Patience is bounded: after "
        f"{SPAWN_ALIVE_PATIENCE_WINDOWS} spawn windows in this state the row is "
        "terminated even though the process is alive. If this session is doing "
        "real work, drive it to register; if it is hung, terminate_session it now."
    )
    meta: dict[str, object] = {"flow_id": f"session-spawn-unregistered-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_SPAWN_UNREGISTERED_NOTICE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the deadline is already re-armed
        logger.warning(
            "session %s spawn-unregistered notice append failed", agent_instance_id, exc_info=True,
        )
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label="session_spawn_unregistered_notice",
    )


def _observed_alive_within_patience(
    row: dict[str, Any],
    *,
    clock: datetime,
    host_alive_probe: Callable[[dict[str, Any]], bool] | None,
) -> bool:
    """The whole earn-an-extension predicate: a genuine host-liveness
    observation AND unexhausted patience. Split from
    :func:`_mark_one_spawning_orphaned` to keep it under the radon cc
    threshold; ``host_alive_probe`` overrides the real observation for tests
    only."""
    probe = host_alive_probe if host_alive_probe is not None else _spawning_host_observed_alive
    return probe(row) and not _spawn_alive_patience_exhausted(row, clock=clock)


def _extend_observed_alive_spawning_row(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    agent_instance_id: str,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> None:
    """The observed-alive branch of :func:`_mark_one_spawning_orphaned` —
    re-arm the row's deadline from its own window, then best-effort notify the
    steward. Split out to keep the caller under the radon cc threshold."""
    _rearm_report_by(
        state, agent_instance_id,
        report_by_seconds=int(row.get("report_by_seconds") or 0),
    )
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_spawn_unregistered(
            state=state, peer_registry=peer_registry,
            bridge_manager=bridge_manager, row=row,
        )


def _mark_one_spawning_orphaned(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    clock: datetime,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
    host_alive_probe: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Counterpart to :func:`_mark_one_overdue` for a ``spawning`` row past
    its ``report_by`` deadline. Not a bare :func:`transition_lifecycle_state`
    call: this reuses :func:`terminate_session` itself so a host process that
    is still alive but never registered (the live-observed shape) is
    actually reaped by the host driver, not just marked dead in the ledger —
    ``terminate_session`` already supports a ``spawning`` origin (its
    ``current`` state is read from the row, not assumed), and
    ``LIFECYCLE_TRANSITIONS[LIFECYCLE_SPAWNING]`` already permits
    ``terminated``. Any :class:`VerbError` (e.g. an undeclared host) is
    caught and skipped, not retried — same skip-not-retry contract as
    :func:`_mark_one_overdue`'s :class:`StaleLifecycleStateError` catch; the
    next tick re-evaluates the row."""
    report_by = _parse_iso(row.get("report_by"))
    if report_by is None or report_by >= clock:
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    # OBSERVE before reaping (2026-08-13, live-measured both ways): a spawning
    # row past its deadline whose host process is observed alive is not an
    # orphaned spawn — it is a live session whose registration never completed.
    # It earns a bounded, ANNOUNCED deadline re-arm instead of the reaper. An
    # unobservable or observed-dead host, or exhausted patience, falls through
    # to the established reap unchanged.
    if _observed_alive_within_patience(row, clock=clock, host_alive_probe=host_alive_probe):
        _extend_observed_alive_spawning_row(
            state, row, agent_instance_id=agent_instance_id,
            peer_registry=peer_registry, bridge_manager=bridge_manager,
        )
        return False
    try:
        terminate_session(state, agent_instance_id=agent_instance_id, directed_by="sweep:platform")
    except VerbError:
        logger.warning(
            "spawning row %s past its report_by deadline could not be "
            "terminated by the sweep", agent_instance_id, exc_info=True,
        )
        return False
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_spawn_orphaned(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager, row=row,
        )
    return True


def sweep_overdue_sessions(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    host_alive_probe: Callable[[dict[str, Any]], bool] | None = None,
) -> int:
    """Mark ``live``/``idle`` managed_session rows ``overdue`` past ``report_by``,
    then best-effort notify each row's steward (D2-lane-tail follow-up #3 —
    previously this function transitioned the row and returned a count with
    NO notification of any kind; a missing feature, not a delivery fault).
    Also terminates ``spawning`` rows past ``report_by`` — see
    :func:`_mark_one_spawning_orphaned`; a ``spawning`` row was invisible to
    an earlier version of this sweep, which scanned only ``live``/``idle``.
    Since 2026-08-13 that leg OBSERVES host liveness first: an observed-alive
    row gets a bounded, announced deadline re-arm instead of the reaper
    (``host_alive_probe`` overrides the observation for tests only —
    production callers omit it and get the real host-driver probe).

    Uncapped ``query_state`` per lifecycle_state (equality filter — no
    op-grammar dependency), never ``query_ordered``'s capped page: a fleet-wide
    sweep must never silently skip an owed row past a page boundary. Returns
    the count actually transitioned; a row that loses a race (e.g. a
    concurrent ``report_alive`` moved it first) is skipped, not retried —
    the NEXT tick re-evaluates it under its new state.

    ``peer_registry``/``bridge_manager`` are OPTIONAL (unlike the sibling
    ``sweep_deadline_dependencies``/``sweep_lane_closed_dependencies``,
    which require them): the STATE TRANSITION must always run regardless of
    whether notification is possible (e.g. an early-boot tick before the
    bridge service is up) — only the notify step is skipped, silently, when
    either is absent. The caller in ``plugin.py`` passes both whenever they
    are available.
    """
    clock = now or datetime.now(UTC)
    marked = 0
    for lifecycle_state in (LIFECYCLE_LIVE, LIFECYCLE_IDLE):
        for row in _managed_sessions_in_state(state, lifecycle_state):
            if _mark_one_overdue(
                state, row, from_state=lifecycle_state, clock=clock,
                peer_registry=peer_registry, bridge_manager=bridge_manager,
            ):
                marked += 1
    for row in _managed_sessions_in_state(state, LIFECYCLE_SPAWNING):
        if _mark_one_spawning_orphaned(
            state, row, clock=clock,
            peer_registry=peer_registry, bridge_manager=bridge_manager,
            host_alive_probe=host_alive_probe,
        ):
            marked += 1
    return marked


def sweep_unregistered_spawning_sessions(
    state: StateManagementInterface,
    *,
    now: datetime | None = None,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    registration_bound_s: float = DEFAULT_REGISTRATION_BOUND_S,
) -> int:
    """W4A registration watchdog — surface a worker that came up DEAF.

    An adopter running an org-managed Claude Code policy carrying
    ``strictPluginOnlyCustomization: ["hooks"]`` spawned a worker that answered
    its first turn while NONE of its injected hooks ran: no hook events, empty
    session-mapping spool, ``lifecycle_state`` stuck at ``spawning``. We
    accepted the reporter's framing — **the defect is the silence, not the
    unsupported environment** — so this leg exists to make the row SAY so.

    WHY THIS IS NOT THE EXISTING ``spawning`` LEG, and why the two must not be
    merged (the next reader will otherwise try):
    :func:`sweep_overdue_sessions`' spawning leg is bounded by ``report_by``
    (the WORK deadline) and its remedy is the REAPER — terminate the row, or,
    since 2026-08-13, re-arm it when the host driver observes the process
    alive. This leg is bounded by REGISTRATION latency and its remedy is
    ATTRIBUTION: it writes a field, logs loudly, and tells the steward what was
    observed. It transitions nothing and kills nothing.
    Concretely, the adopter's worker takes the observed-alive path over there:
    it answered its first turn, so it probes alive, so it is re-armed with a
    notice that announces a DEADLINE EXTENSION — true, and not the news that a
    worker's hooks never ran. That leg working exactly as designed is what
    produced the reported silence, which is why the fix is a separate
    observation rather than another branch inside it.

    MECHANISM-INDEPENDENT BY CONSTRUCTION. The trigger is "still ``spawning``
    past the bound" — the seam itself. A row in that condition has, by
    construction, not run ``capture_session_mapping``, whatever the cause:
    the policy shape in front of us, a policy shape nobody has reported yet, a
    crashed hook, a mis-wired plugin, a read-only spool. Nothing here reads a
    settings file or reasons about one; the managed-policy PREFLIGHT
    (``HeadlessHostDriver.verify_config``) is a separate, narrower thing that
    refuses a spawn it can prove is doomed. A check written from the one
    incident in front of us would miss the next one.

    Idempotent: a row already carrying ``registration_overdue_at`` is skipped,
    so the field records the FIRST observation and the steward is told once.
    Returns the number of rows newly marked.
    """
    clock = now or datetime.now(UTC)
    marked = 0
    for row in _managed_sessions_in_state(state, LIFECYCLE_SPAWNING):
        if _mark_one_registration_overdue(
            state, row, clock=clock, registration_bound_s=registration_bound_s,
            peer_registry=peer_registry, bridge_manager=bridge_manager,
        ):
            marked += 1
    return marked


def _mark_one_registration_overdue(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    clock: datetime,
    registration_bound_s: float,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> bool:
    """One row's worth of :func:`sweep_unregistered_spawning_sessions` — split
    out to keep the outer loop under the radon cc threshold, same shape as
    :func:`_mark_one_overdue`. Returns whether the row was newly marked."""
    if row.get("registration_overdue_at"):
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    # Age from last_transition_at: it is the row's spawn timestamp and stays
    # unchanged while the row remains 'spawning' (the same anchor the
    # observed-alive patience bound uses).
    spawned_at = _parse_iso(row.get("last_transition_at"))
    if spawned_at is None or (clock - spawned_at).total_seconds() < registration_bound_s:
        return False
    acknowledged = bool(row.get("degraded_hooks_acknowledged"))
    reason = (
        f"still 'spawning' {int((clock - spawned_at).total_seconds())}s after spawn "
        f"(registration bound {int(registration_bound_s)}s) — the spawned process has "
        "not registered, so its registration hook has not run. Its hooks may have been "
        "stripped by host policy, failed to load, or never fired."
    )
    if acknowledged:
        reason += " This spawn ACKNOWLEDGED degraded hooks, so this was an accepted risk."
    mark_registration_overdue(
        state, agent_instance_id=agent_instance_id, reason=reason, observed_at=clock,
    )
    logger.warning(
        "registration watchdog: session %s (lane_id=%r, host=%r) is %s. This row "
        "would otherwise sit in 'spawning' with nothing said about why.",
        agent_instance_id, row.get("lane_id"), row.get("host"), reason,
    )
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_registration_overdue(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            row=row, reason=reason,
        )
    return True


def _notify_steward_of_registration_overdue(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    reason: str,
) -> None:
    """Best-effort steward notice for a registration-overdue row — mirrors
    :func:`_notify_steward_of_spawn_orphaned`'s resolve-then-append pattern.
    Distinct event type from the other three spawn notices so a receiver can
    tell "came up deaf" from "never came up" and from "alive but late"."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return
    binding = _resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s registration-overdue: spawner %s not resolvable to a live "
            "binding — row marked, steward not notified",
            agent_instance_id, spawner_instance_id,
        )
        return
    prose = (
        f"session_registration_overdue_notice: {agent_instance_id} (lane_id="
        f"{row.get('lane_id')!r}, host={row.get('host')!r}) is {reason} The session "
        "may still be RUNNING and answering turns — a worker whose hooks did not "
        "run can look healthy from the outside while being invisible to the "
        "platform. Treat it as unmanaged until it registers."
    )
    meta: dict[str, object] = {"flow_id": f"session-registration-overdue-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE, prose, meta,
        )
    except (PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError):
        logger.warning(
            "session %s registration-overdue: steward notice could not be delivered",
            agent_instance_id, exc_info=True,
        )


def _mark_one_overdue(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    from_state: str,
    clock: datetime,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
) -> bool:
    """One row's worth of :func:`sweep_overdue_sessions` — split out to keep
    the outer loop under the radon cc threshold. Returns whether the row was
    actually transitioned (the sweep's own count)."""
    report_by = _parse_iso(row.get("report_by"))
    if report_by is None or report_by >= clock:
        return False
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=from_state,
            to_state=LIFECYCLE_OVERDUE,
            directed_by="sweep:platform",
            reason="report_by deadline passed",
        )
    except StaleLifecycleStateError:
        return False
    if peer_registry is not None and bridge_manager is not None:
        _notify_steward_of_overdue(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager, row=row,
        )
    return True


def _resolve_dependency_waiter(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    waiter_instance_id: str,
) -> BridgeBinding | None:
    """Resolve a dependency edge's waiter to a live binding.

    Primary path: a direct ``resolve_by_agent_instance_id`` reverse lookup —
    works for ANY registered waiter regardless of transport, including a
    watch-registered session with no ``managed_session`` row of its own.
    Falls back to the ``managed_session``-row detour (``agent_id`` lookup +
    ``PeerRegistry.resolve``) only on a direct-lookup miss, mirroring
    :func:`_resolve_steward_via_managed_session`'s identical fix for the
    overdue-notice path."""
    binding = peer_registry.resolve_by_agent_instance_id(waiter_instance_id)
    if binding is not None:
        return binding
    agent_id = _managed_session_agent_id(state, waiter_instance_id)
    if not agent_id:
        return None
    try:
        return peer_registry.resolve(agent_id, waiter_instance_id)
    except (PeerUnreachableError, PeerAmbiguousError):
        return None


def _deliver_dependency_wake(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    edge: dict[str, Any],
) -> None:
    """Best-effort wake delivery for one already-fired edge.

    Mirrors ``direct_wake_reconcile._notify_sender``'s exact
    resolve-then-append pattern: the edge is already marked fired (state),
    so a delivery fault here must never raise back into the sweep loop and
    must never block firing/marking the OTHER armed edges in this tick.

    Delivery must reach ANY resolvable peer binding, not just a managed
    one (phase-2 scope ruling, the phase-1 unified finding): a
    watch-transport or otherwise unmanaged waiter is a legal arm target
    (``arm_session_dependency`` has no waiter-existence check) and must not
    be silently dropped just because it has no ``managed_session`` row.
    """
    waiter_instance_id = str(edge.get("waiter_instance_id") or "")
    if not waiter_instance_id:
        logger.warning(
            "session_dependency %s is lane-scoped (waiter_lane_id=%r): "
            "lane-scoped wake delivery has no defined mapping in this "
            "slice — the edge is fired but nobody was notified",
            edge.get("id"), edge.get("waiter_lane_id"),
        )
        return
    binding = _resolve_dependency_waiter(
        state=state, peer_registry=peer_registry, waiter_instance_id=waiter_instance_id,
    )
    if binding is None:
        logger.warning(
            "session_dependency %s: waiter %s not resolvable to a live "
            "binding (checked the peer registry directly and via its "
            "managed_session row) — condition fired, nobody woken",
            edge.get("id"), waiter_instance_id,
        )
        return
    prose = (
        f"session_dependency fired: condition_kind={edge.get('condition_kind')} "
        f"condition_ref={edge.get('condition_ref')!r} is now satisfied."
    )
    meta: dict[str, object] = {"flow_id": f"session-dependency-{edge.get('id')}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_SESSION_DEPENDENCY_WAKE, prose, meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify; the edge is already fired
        logger.warning(
            "session_dependency %s wake append failed", edge.get("id"), exc_info=True,
        )
    # Drive-on-delivery (2026-08-04, slice 2): ALONGSIDE the append_event
    # above, never instead of it — a managed waiter's driver channel gets an
    # extra best-effort nudge; drive_on_delivery no-ops silently for an
    # unmanaged waiter (the SessionNotFoundError path), so this is safe to
    # call unconditionally for every resolved binding.
    drive_on_delivery(
        state, recipient_agent_instance_id=waiter_instance_id,
        sender_label="session_dependency wake",
    )


def _fire_armed_dependencies(
    state: StateManagementInterface,
    *,
    condition_kind: str,
    is_due: Callable[[dict[str, Any]], bool],
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime,
) -> int:
    """Shared fire-then-deliver loop for one ``condition_kind`` — the common
    machinery behind :func:`sweep_deadline_dependencies` and
    :func:`sweep_lane_closed_dependencies`: only the due-ness predicate
    differs per kind.

    ``fired_at`` is set via a predicated ``update_state`` (guarded on
    ``fired_at IS NULL``) BEFORE delivery is attempted — Architect ratification
    #1's no-fall-through discipline: a lost predicate here means another
    tick/process already claimed this row, so the loop moves on rather than
    delivering a second wake for a row it no longer owns.
    """
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_DEPENDENCY,
            # {"op": "is_null"} -- NEVER a bare None. A bare None filter
            # value compiles to SQL `col = NULL`, which the postgres
            # provider's own placeholder binding renders as a literal NULL
            # comparison -- always UNKNOWN/false in SQL, so it matches ZERO
            # rows, silently, forever (measured live 2026-08-04: an armed,
            # past-due deadline edge never fired -- the query above found
            # nothing because "fired_at": None matched no row, not because
            # none were due).
            "filters": {"condition_kind": condition_kind, "fired_at": {"op": "is_null"}},
        },
    )
    fired = 0
    for edge in require_records(result):
        if not is_due(edge):
            continue
        update_result = state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_DEPENDENCY,
                "filters": {"id": edge["id"], "fired_at": {"op": "is_null"}},
            },
            {"fired_at": now.isoformat()},
        )
        if require_updated(update_result) == 0:
            continue
        fired += 1
        _deliver_dependency_wake(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager, edge=edge,
        )
    return fired


def sweep_deadline_dependencies(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime | None = None,
) -> int:
    """Fire + deliver armed ``deadline`` dependency edges past their timestamp."""
    clock = now or datetime.now(UTC)

    def _is_due(edge: dict[str, Any]) -> bool:
        deadline = _parse_iso(edge.get("condition_ref"))
        return deadline is not None and deadline < clock

    return _fire_armed_dependencies(
        state, condition_kind=CONDITION_DEADLINE, is_due=_is_due,
        peer_registry=peer_registry, bridge_manager=bridge_manager, now=clock,
    )


def _lane_is_closed(state: StateManagementInterface, lane_id: str) -> bool:
    """True iff at least one ``managed_session`` row carries ``lane_id`` AND
    every such row is terminal (``terminated``/``retired``). No vacuous truth
    on an empty set — a lane with zero spawned sessions is unstarted, not
    closed."""
    rows = list_managed_sessions(state, {"lane_id": lane_id})
    if not rows:
        return False
    return all(
        row.get("lifecycle_state") in (LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED)
        for row in rows
    )


def sweep_lane_closed_dependencies(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    now: datetime | None = None,
) -> int:
    """Fire + deliver armed ``lane_closed`` dependency edges (Dawn ruling
    2026-08-03, arm-124065ee) — ``condition_ref`` is a ``lane_id``; the
    condition is due once every ``managed_session`` row carrying that lane_id
    is terminal. See the module docstring for why this replaced the
    unbuildable ``lane_landed`` spec kind."""
    clock = now or datetime.now(UTC)

    def _is_due(edge: dict[str, Any]) -> bool:
        return _lane_is_closed(state, str(edge.get("condition_ref") or ""))

    return _fire_armed_dependencies(
        state, condition_kind=CONDITION_LANE_CLOSED, is_due=_is_due,
        peer_registry=peer_registry, bridge_manager=bridge_manager, now=clock,
    )


class SessionRoleClaimPruner:
    """Architect ratification #3 — prunes ``session_role_claim`` rows whose
    session maps to no live/managed session, WITHOUT the one-sided-guard flaw
    the ratification named ("session id maps to no managed or registered
    session" pruned on instantaneous absence — but a blue-green cutover
    bounces bridges into a fleet-wide re-registration burst, so a genuinely
    LIVE session can transiently map to nothing; the recorded 156-phantom-
    orphans shape).

    A row is pruned ONLY when:

    1. a ``managed_session`` row exists for its ``agent_session_id`` and is
       terminal (``terminated``/``retired``) — ledger-authoritative, no grace
       window needed; or
    2. NO ``managed_session`` row and NO live registry binding exist for it,
       AND that absence has persisted past :data:`DEFAULT_PRUNE_GRACE_WINDOW_S`
       — never on one missed lookup.

    Absence duration is tracked in-memory (first-observed-absent timestamp
    per ``agent_session_id``) rather than a new schema column: a process
    restart resets tracking, which conservatively RESTARTS the grace window
    instead of risking a premature prune from stale duration data recovered
    from disk. Stateful (unlike the two module-level sweep functions above)
    because the grace-window clock needs to survive across ticks within one
    process lifetime — mirrored the same config+clock-holding shape the
    now-retired ``DirectWakeReconciler`` (A4, 2026-08-04) used.
    """

    def __init__(
        self,
        *,
        grace_window_s: float = DEFAULT_PRUNE_GRACE_WINDOW_S,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._grace_window_s = grace_window_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._first_absent_at: dict[str, datetime] = {}

    def sweep(
        self, state: StateManagementInterface, *, peer_registry: PeerRegistry,
    ) -> int:
        now = self._clock()
        result = state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_SESSION_ROLE_CLAIM, "filters": {"is_deleted": 0}},
        )
        pruned = 0
        seen: set[str] = set()
        for row in require_records(result):
            agent_session_id = str(row.get("agent_session_id") or "")
            if not agent_session_id:
                continue
            seen.add(agent_session_id)
            if self._should_prune(state, peer_registry, agent_session_id, now):
                deleted = state.delete_records(
                    AGENT_ROLE_BINDING_NAMESPACE,
                    {
                        "table": TABLE_SESSION_ROLE_CLAIM,
                        "filters": {"agent_session_id": agent_session_id},
                        "soft_delete": False,
                    },
                )
                if require_deleted(deleted):
                    pruned += 1
                self._first_absent_at.pop(agent_session_id, None)
        # Forget grace-window tracking for rows no longer present at all
        # (already pruned, or the row vanished by some other path) so the
        # dict does not grow unboundedly across a long process lifetime.
        for stale_key in set(self._first_absent_at) - seen:
            self._first_absent_at.pop(stale_key, None)
        return pruned

    def _should_prune(
        self,
        state: StateManagementInterface,
        peer_registry: PeerRegistry,
        agent_session_id: str,
        now: datetime,
    ) -> bool:
        managed_rows = list_managed_sessions(state, {"agent_session_id": agent_session_id})
        if managed_rows:
            if managed_rows[0].get("lifecycle_state") in (
                LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED,
            ):
                return True
            # A non-terminal managed_session row is ledger-authoritative
            # "alive" — never absent, regardless of registry state.
            self._first_absent_at.pop(agent_session_id, None)
            return False
        try:
            binding = peer_registry.resolve_by_agent_session_id(agent_session_id)
        except PeerSessionAmbiguousError:
            # Ambiguous is "more than one live binding", i.e. present — not absence.
            self._first_absent_at.pop(agent_session_id, None)
            return False
        if binding is not None:
            self._first_absent_at.pop(agent_session_id, None)
            return False
        first_seen = self._first_absent_at.setdefault(agent_session_id, now)
        return (now - first_seen).total_seconds() > self._grace_window_s


class NoticeLatch:
    """One-notice-per-EPISODE gate for a sweep notice with no state edge behind
    it.

    The overdue notice does not need this: it rides the ``live -> overdue``
    lifecycle transition, so the row itself remembers that the notice was sent
    and the edge cannot be taken twice. The L4 notices have no such edge — a
    gauge row stays above the rotation threshold until the session actually
    rotates, and a session with no gauge row stays dark. Composed onto a tick
    without a latch, both would re-deliver the identical notice every
    ``bridge_sweep_interval_seconds`` for as long as the condition holds, which
    trains the reader to ignore the channel. An ignored warning is worse than
    no warning, so the latch is part of making these notices live, not a
    refinement of it.

    Three properties, each deliberate:

    * **Latched on SUCCESSFUL delivery only** (:meth:`record_sent` is called by
      the caller after the append returns True). A failed append leaves the key
      un-latched so the next tick retries — an episode must never be silenced
      by its own delivery failure.
    * **Re-arms when the condition CLEARS** (:meth:`retain_active`). A second
      episode is a second notice; only repetition WITHIN an episode is
      suppressed.
    * **In-memory, so it is bounded by the process lifetime.** A solet restart
      re-arms every episode and each live episode gets one more notice. That is
      a stated bound rather than an oversight: the alternative is a durable
      suppression table, and one repeat per restart is a cheaper failure than a
      schema for it. It is also self-limiting in the direction that matters —
      restarts are rare, ticks are every 5 minutes.
    """

    def __init__(self) -> None:
        self._latched: set[str] = set()

    def suppressed(self, key: str) -> bool:
        """True when ``key``'s episode has already been notified."""
        return key in self._latched

    def record_sent(self, key: str) -> None:
        """Latch ``key`` — call only after delivery actually succeeded."""
        self._latched.add(key)

    def retain_active(self, active: set[str]) -> None:
        """Release every latched key whose condition no longer holds, so the
        next episode notifies again."""
        self._latched &= active


def _latch_or_transient(latch: NoticeLatch | None) -> NoticeLatch:
    """``latch``, or a throwaway one for a caller that passed none.

    A fresh latch is EXACTLY equivalent to no latch for a single sweep — each
    key is visited once per call, so nothing it records can suppress anything
    within that call — which is what lets every leg below read as if a latch is
    always present. The alternative (``if latch is not None`` at each of the
    three use sites) is the same behaviour spelled three times, and it is the
    spelling in which one forgotten branch is a silent repeat-every-tick.
    """
    return NoticeLatch() if latch is None else latch


def _rotation_due_sessions(
    state: StateManagementInterface,
) -> list[tuple[str, str, dict[str, Any]]]:
    """``(agent_instance_id, steward_instance_id, enriched gauge row)`` for
    every live/idle session that is currently rotation-due.

    Splitting the SCAN from the NOTIFY keeps the sweep's loop about delivery
    policy (latch, count) and leaves "what counts as due" where it already
    lives, in :func:`_rotation_due_row`.
    """
    found: list[tuple[str, str, dict[str, Any]]] = []
    for lifecycle_state in ("live", "idle"):
        for row in _managed_sessions_in_state(state, lifecycle_state):
            enriched = _rotation_due_row(state, row)
            if enriched is not None:
                found.append(
                    (str(row["agent_instance_id"]), str(row["spawned_by_instance_id"]), enriched),
                )
    return found


def _gauge_dark_session(
    state: StateManagementInterface, row: dict[str, Any],
) -> tuple[str, str] | None:
    """``(agent_instance_id, steward_instance_id)`` when this LIVE row has no
    gauge row at all, else ``None``.

    Same "every ``None`` is a deliberate skip" idiom as
    :func:`_rotation_due_row`: no identity, no steward to tell, or a session
    that is reporting perfectly well.
    """
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not agent_instance_id or not spawner_instance_id:
        return None
    if read_session_context_status(state, agent_instance_id) is not None:
        return None
    return agent_instance_id, spawner_instance_id


def _rotation_prose(agent_instance_id: str, row: dict[str, Any]) -> str:
    """The notice text: the MEASURED numbers, never a bare "you should rotate".

    Two qualifications are carried IN THE TEXT rather than assumed away,
    because both are live limitations of the data this reads:

    * The bands are MODEL-BLIND. ``rotation_band`` takes no model argument and
      its thresholds came from one tier's economics, so the model is named
      beside the band and the reader discounts it themselves. A tier-specific
      verdict presented as universal is how a hygiene-level number reads as an
      emergency.
    * A row whose reporter is unattributable cannot be trusted to the same
      degree. A reporter predating attribution sends no cache state, so the
      band it implies is the WARM default rather than a measurement -- and an
      urgent-sounding notice derived from a default is false precision. Such a
      row is reported AS unattributable instead of being silently upgraded.
    """
    band = row.get("rotation_band") or "unknown"
    guidance = row.get("rotation_guidance") or "no guidance derived"
    surface = row.get("reporter_surface")
    generation = row.get("reporter_generation")
    attribution = (
        f"reported by {surface}/gen{generation}"
        if surface is not None and generation is not None
        else "REPORTER UNATTRIBUTABLE (predates attribution) -- treat the band "
        "as provisional: an un-upgraded reporter sends no cache state, so this "
        "band is the warm default rather than a measurement"
    )
    return (
        f"rotation_due_notice: {agent_instance_id} is at "
        f"{row.get('current_tokens')} tokens on {row.get('model')!r} "
        f"({row.get('fraction'):.3f} of a {row.get('ceiling')} ceiling), "
        f"band={band} -- {guidance}. Measured at {row.get('measured_at')}. "
        f"{attribution}. NOTE the bands are model-blind: the thresholds derive "
        f"from one tier's economics, so weigh this against {row.get('model')!r}'s "
        f"own costs rather than reading the band as universal."
    )


def _notify_rotation_due(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    row: dict[str, Any],
    agent_instance_id: str,
    spawner_instance_id: str,
) -> bool:
    """Best-effort steward notice for one rotation-due session.

    Mirrors :func:`_notify_steward_of_overdue`'s resolve-then-append shape, and
    inherits its posture: a delivery fault must never raise back into the sweep
    loop or block the OTHER rows in this tick.

    ★ THIS DOES NOT REACH AN OPERATOR-PRESENT SEAT, and that is structural
    rather than a gap to fix here. ``append_event`` lands on the recipient's
    bridge and is read when that session next takes a turn; ``drive_on_delivery``
    no-ops for exactly this case (a session with no ``managed_session`` row --
    an ordinary operator-launched seat -- and again for the degenerate
    ``operator`` host driver). So this leg serves MANAGED WORKERS. The seat is
    served by a separate surface that can reach it at a decision point, and
    until that lands this notice fires into a void for seats.
    """
    binding = _resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "session %s is rotation-due: steward %s not resolvable to a live binding "
            "-- notice not delivered",
            agent_instance_id, spawner_instance_id,
        )
        return False
    meta: dict[str, object] = {"flow_id": f"rotation-due-{agent_instance_id}"}
    try:
        bridge_manager.append_event(
            binding.bridge_id,
            EVENT_ROTATION_DUE_NOTICE,
            _rotation_prose(agent_instance_id, row),
            meta,
        )
    except Exception:  # noqa: BLE001 — best-effort notify, never fails the sweep
        logger.warning(
            "session %s rotation-due notice append failed", agent_instance_id, exc_info=True,
        )
        return False
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label=EVENT_ROTATION_DUE_NOTICE,
    )
    return True


def _rotation_due_row(
    state: StateManagementInterface, row: dict[str, Any],
) -> dict[str, Any] | None:
    """The gauge row for ``row``'s session, enriched with the derived band —
    or ``None`` when this session is not rotation-due.

    Every ``None`` here is a distinct, deliberate skip rather than a failure:
    no steward to notify, no gauge row yet, an unusable ceiling, or simply
    below the threshold. Split out of the sweep loop so the loop reads as
    "for each session, notify if due" and the decision of what counts as DUE
    lives in one place.
    """
    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not agent_instance_id or not spawner_instance_id:
        return None
    gauge = read_session_context_status(state, agent_instance_id)
    if gauge is None:
        return None
    current = int(gauge.get("current_tokens") or 0)
    ceiling = int(gauge.get("ceiling") or 0)
    if ceiling <= 0:
        return None
    fraction = current / ceiling
    if fraction < rotation_thresholds.ROTATION_THRESHOLD_FRACTION:
        return None
    band, guidance = rotation_thresholds.rotation_band(
        current, cache_cold=bool(gauge.get("cache_cold")),
    )
    enriched = dict(gauge)
    enriched.update(
        {"fraction": fraction, "rotation_band": band, "rotation_guidance": guidance},
    )
    return enriched


def sweep_rotation_due_sessions(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    latch: NoticeLatch | None = None,
) -> int:
    """Notify stewards of managed sessions whose gauge says rotation is due.

    THE PRE-COST SURFACE. A ``PostToolUse`` hook cannot warn before the cost —
    it does not tick during the idle gap in which the cache lapses, so its
    first tick after resuming is already downstream of the full-price rewrite.
    This sweep runs on its own schedule and needs no session to act first,
    which is what lets a warning precede the spend.

    Reads the snapshots ``report_context_status`` already writes rather than
    measuring anything itself, and carries the MEASURED number and band into
    the notice (see :func:`_rotation_prose` for the two qualifications that
    ride with it).

    Returns the count actually notified. Notification requires both a peer
    registry and a bridge manager; absent either, this returns 0 without
    raising, matching :func:`sweep_overdue_sessions`' posture that an
    early-boot tick must not fail.

    ``latch`` gates repetition (see :class:`NoticeLatch`). Passing NONE means
    "notify every call", which is right for a one-shot invocation or a test and
    WRONG for a repeating tick — the composed production caller
    (``plugin.py``'s rotation-surface rider) always supplies one, because a
    rotation-due condition persists across ticks and an unlatched notice would
    re-deliver every ``bridge_sweep_interval_seconds`` until the session
    rotates.
    """
    if peer_registry is None or bridge_manager is None:
        return 0
    gate = _latch_or_transient(latch)
    notified = 0
    due: set[str] = set()
    for agent_instance_id, spawner_instance_id, enriched in _rotation_due_sessions(state):
        due.add(agent_instance_id)
        if gate.suppressed(agent_instance_id):
            continue
        if _notify_rotation_due(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            row=enriched, agent_instance_id=agent_instance_id,
            spawner_instance_id=spawner_instance_id,
        ):
            notified += 1
            gate.record_sent(agent_instance_id)
    gate.retain_active(due)
    return notified


def _notify_gauge_coverage(
    *,
    state: StateManagementInterface,
    peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager,
    agent_instance_id: str,
    spawner_instance_id: str,
) -> bool:
    """Tell the steward one live session is producing no gauge row.

    Same best-effort posture as the sibling notices: a delivery fault warns and
    returns False rather than raising into the sweep loop, so one unreachable
    steward never costs the other rows their notice.
    """
    binding = _resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        return False
    prose = (
        f"gauge_coverage_notice: {agent_instance_id} is LIVE (report_alive is "
        "landing, so its hooks are running) but has NO session_context_status "
        "row at all — the reporting path between the hook's tick and the "
        "state write is failing SILENTLY. The reporting hook is non-fatal by "
        "design and warns only to stderr, so it cannot surface this itself. "
        "Check that session's solet invocation and its environment."
    )
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_GAUGE_COVERAGE_NOTICE, prose,
            {"flow_id": f"gauge-coverage-{agent_instance_id}"},
        )
    except Exception:  # noqa: BLE001 — best-effort notify, never fails the sweep
        logger.warning(
            "session %s gauge-coverage notice append failed",
            agent_instance_id, exc_info=True,
        )
        return False
    return True


def sweep_gauge_coverage(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None,
    latch: NoticeLatch | None = None,
) -> int:
    """Notify stewards of LIVE sessions that have no gauge row at all.

    The signature this catches was measured on 2026-08-16: the reporting hook's
    ``solet`` invocation could fail silently (a bare binary name, an
    ``OSError`` caught and warned to stderr nobody reads), while the throttle
    marker — written BEFORE the report — kept updating. From outside, a session
    reporting perfectly and a session whose every report was discarded looked
    identical. Four sessions were dark for an unknown period and nothing
    surfaced it; it was found by a person going looking.

    The hook cannot catch this: its non-fatal-by-design contract requires it to
    swallow its own failures, and a component cannot report the failure of its
    own reporting path. The session cannot: it has no idea the write failed.
    The sweep can, because it sees BOTH facts — a lifecycle row saying live and
    an absent gauge row — and neither side can see the other.

    Deliberately generic in what it keys on: any future silent failure between
    tick and write (a renamed process key, a permissions change, a verb
    rejecting an argument) produces the same signature. Keying on the mismatch
    rather than on any particular cause is what makes it outlive the bug that
    motivated it.

    ``latch`` gates repetition exactly as in :func:`sweep_rotation_due_sessions`
    — a dark session stays dark until someone fixes it, so an unlatched notice
    would repeat every tick for the whole outage. The latch releases the key
    when a gauge row appears, which makes a RELAPSE (reporting recovered, then
    broke again) a fresh notice rather than a silence.
    """
    if peer_registry is None or bridge_manager is None:
        return 0
    gate = _latch_or_transient(latch)
    notified = 0
    dark: set[str] = set()
    for row in _managed_sessions_in_state(state, "live"):
        pair = _gauge_dark_session(state, row)
        if pair is None:
            continue
        agent_instance_id, spawner_instance_id = pair
        dark.add(agent_instance_id)
        if gate.suppressed(agent_instance_id):
            continue
        if _notify_gauge_coverage(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            agent_instance_id=agent_instance_id, spawner_instance_id=spawner_instance_id,
        ):
            notified += 1
            gate.record_sent(agent_instance_id)
    gate.retain_active(dark)
    return notified


__all__ = [
    "DEFAULT_PRUNE_GRACE_WINDOW_S",
    "DEFAULT_REGISTRATION_BOUND_S",
    "EVENT_GAUGE_COVERAGE_NOTICE",
    "EVENT_ROTATION_DUE_NOTICE",
    "EVENT_SESSION_DEPENDENCY_WAKE",
    "EVENT_SESSION_OVERDUE_NOTICE",
    "EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE",
    "NoticeLatch",
    "SessionRoleClaimPruner",
    "sweep_deadline_dependencies",
    "sweep_gauge_coverage",
    "sweep_lane_closed_dependencies",
    "sweep_overdue_sessions",
    "sweep_rotation_due_sessions",
    "sweep_unregistered_spawning_sessions",
]
