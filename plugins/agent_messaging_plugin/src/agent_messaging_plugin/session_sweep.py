"""D1 platform sweep (§3.4 / §6 rule 3 edge ownership) — an ``on_tick`` rider
that piggybacks the existing :class:`BridgeLifecycleSweeper` cadence (same
pattern as the INF-02 serve-timeout sweep / INF-06 forwarded-vertex re-drive /
REL-05 deaf-wake escalation composed in ``plugin.py``'s ``_on_sweep_tick``),
rather than growing a new thread.

One dependency responsibility, owned by "the platform sweep" per the
design's own edge-ownership table. Nothing here checks on a session's own
report: no row is marked ``overdue`` by a clock and no steward is told about a
session's silence (a worker declares itself with ``report_alive``, and a late
report from an ``overdue`` row still recovers it).

:func:`sweep_deadline_dependencies` — fire + deliver armed
``session_dependency`` rows whose ``condition_kind == 'deadline'`` and
``condition_ref`` (an ISO timestamp) has passed.

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
* A ``deadline`` edge with an empty ``waiter_instance_id`` is fired (state)
  but its wake delivery is a logged no-op — there is no session to wake.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.llm.agent_messaging.state_results import (
    require_deleted,
    require_records,
    require_updated,
)
from ananta.services.state_service.bounded_read import iter_table_rows

from .channel_support import channel_carries_message
from .peer_registry import PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError
from .schema import (
    CONDITION_DEADLINE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_TERMINATED,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_ROLE_CLAIM,
)
from .session_lifecycle_store import (
    list_managed_sessions,
)
from .session_lifecycle_verbs import (
    REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,
    drive_on_delivery,
)
from .steward_resolution import managed_session_agent_id

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

# The two gauge notice-type names are kept for ``gauge_notice_records`` and
# ``gauge_canary``, which still read durable records by these types. No sweep
# emits them any more; whether those readers stay is a16's check.
EVENT_GAUGE_COVERAGE_NOTICE = "gauge_coverage_notice"
EVENT_GAUGE_STALE_NOTICE = "gauge_stale_notice"

# The startup grace that ``rotation_self_notice`` reuses as its
# silent-registration grace (``GAUGE_SILENT_REGISTRATION_GRACE_S``).
GAUGE_COVERAGE_GRACE_S: float = 600.0


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
    state: StateManagementInterface,
    lifecycle_state: str,
) -> list[dict[str, Any]]:
    return list_managed_sessions(state, {"lifecycle_state": lifecycle_state})


def live_lifecycle_rows_by_instance(
    state: StateManagementInterface,
) -> dict[str, dict[str, Any]]:
    """Every ``live`` lifecycle row, indexed by ``agent_instance_id``.

    ONE QUERY PER SWEEP, not one per candidate. The L4c leg scans the gauge
    table -- which is never pruned and therefore carries the whole history of
    every session that ever reported -- so a per-row lifecycle lookup would put
    an unbounded query count on a leg whose entire eligibility problem is that
    its input is unbounded. Indexing once and looking up in memory keeps the
    leg's cost proportional to the LIVE fleet rather than to history.

    Rows with no ``agent_instance_id`` are dropped rather than keyed on the
    empty string: an empty key would collapse every such row onto one entry and
    hand the last one out as though it were a match, which is the confident-
    wrong-row failure the gauge store's own session-id join guards against.
    """
    rows: dict[str, dict[str, Any]] = {}
    for row in _managed_sessions_in_state(state, "live"):
        agent_instance_id = str(row.get("agent_instance_id") or "")
        if agent_instance_id:
            rows[agent_instance_id] = row
    return rows


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
    :func:`_resolve_steward_via_managed_session`'s identical fix for steward
    resolution."""
    binding = peer_registry.resolve_by_agent_instance_id(waiter_instance_id)
    if binding is not None:
        return binding
    agent_id = managed_session_agent_id(state, waiter_instance_id)
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
            "session_dependency %s has no waiter_instance_id — the edge is fired but nobody was notified",
            edge.get("id"),
        )
        return
    binding = _resolve_dependency_waiter(
        state=state,
        peer_registry=peer_registry,
        waiter_instance_id=waiter_instance_id,
    )
    if binding is None:
        logger.warning(
            "session_dependency %s: waiter %s not resolvable to a live binding (checked the peer registry directly and via its managed_session row) — condition fired, nobody woken",
            edge.get("id"),
            waiter_instance_id,
        )
        return
    prose = f"session_dependency fired: condition_kind={edge.get('condition_kind')} condition_ref={edge.get('condition_ref')!r} is now satisfied."
    meta: dict[str, object] = {"flow_id": f"session-dependency-{edge.get('id')}"}
    appended = False
    try:
        bridge_manager.append_event(
            binding.bridge_id,
            EVENT_SESSION_DEPENDENCY_WAKE,
            prose,
            meta,
        )
        appended = True
    except Exception:  # noqa: BLE001 — best-effort notify; the edge is already fired
        logger.warning(
            "session_dependency %s wake append failed",
            edge.get("id"),
            exc_info=True,
        )
    # Drive-on-delivery (2026-08-04, slice 2): ALONGSIDE the append_event
    # above, never instead of it — a managed waiter's driver channel gets an
    # extra best-effort nudge; drive_on_delivery no-ops silently for an
    # unmanaged waiter (the SessionNotFoundError path), so this is safe to
    # call unconditionally for every resolved binding. The nudge is skipped
    # (prb_57c118ad) only when the append succeeded AND the waiter's live
    # channel carries it, since the pane notice would then repeat the event.
    drive_on_delivery(
        state,
        recipient_agent_instance_id=waiter_instance_id,
        sender_label="session_dependency wake",
        channel_delivered=appended
        and channel_carries_message(
            binding,
            native_wake_adapter=peer_registry.wake_adapter_for(binding.agent_id) is not None,
        ),
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
    machinery behind :func:`sweep_deadline_dependencies`: the due-ness
    predicate is the only per-kind part.

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
            state=state,
            peer_registry=peer_registry,
            bridge_manager=bridge_manager,
            edge=edge,
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
        state,
        condition_kind=CONDITION_DEADLINE,
        is_due=_is_due,
        peer_registry=peer_registry,
        bridge_manager=bridge_manager,
        now=clock,
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
        self,
        state: StateManagementInterface,
        *,
        peer_registry: PeerRegistry,
    ) -> int:
        now = self._clock()
        pruned = 0
        seen: set[str] = set()
        for row in iter_table_rows(
            state,
            namespace=AGENT_ROLE_BINDING_NAMESPACE,
            table=TABLE_SESSION_ROLE_CLAIM,
            filters={},
            ceiling=1_000_000,
            reason=("one live role-claim row per session is required by the pruner; 135 rows measured on 2026-09-14"),
        ):
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
                LIFECYCLE_TERMINATED,
                LIFECYCLE_RETIRED,
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


def last_report_alive(row: dict[str, Any]) -> datetime | None:
    """The moment ``report_alive`` last landed for this lifecycle row, or
    ``None`` when the row carries no evidence either way.

    ★ THE ONE COPY OF THE IDENTITY, and the reason it is a module-level public
    name rather than three private re-derivations. ``_rearm_report_by`` writes
    ``report_by = <the moment of the call> + report_by_seconds`` on EVERY
    report_alive, so ``report_by - report_by_seconds`` IS the last call's
    timestamp -- an identity, not an estimate. Verified live 2026-08-18 against
    three running lanes, where the derived value matched each row's
    ``updated_at`` to sub-millisecond (22:20:31.681054 derived vs .681320
    stored, and two more).

    SEVERAL READERS WANT IT (``fleet_status``, ``gauge_series`` and the L4c
    self-notice leg's eligibility bound), which is exactly when a shared rule
    has to stop being copied: two copies of a liveness rule drift apart, and
    the drift is silent -- each copy keeps returning a plausible answer.

    TRI-STATE PRESERVED AT THE SOURCE. ``None`` is not "never reported": a row
    whose ``report_by_seconds`` is 0 -- which a spawn may legitimately request,
    and which ``_spawn_live`` DEFAULTS to -- carries no window, and absence of
    the WINDOW is not evidence of absence of a TICK. Returning a datetime there
    would let a missing column speak as though it were a measurement, which is
    the failure mode this whole entry is about. Callers must keep the
    distinction; none of them may treat ``None`` as "did not tick".
    """
    if row.get("report_by_source") != REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT:
        return None
    report_by = _parse_iso(row.get("report_by"))
    window_s = row.get("report_by_seconds")
    if report_by is None or not isinstance(window_s, (int, float)):
        return None
    if window_s <= 0:
        return None
    return report_by - timedelta(seconds=float(window_s))


# ---------------------------------------------------------------------------


__all__ = [
    "DEFAULT_PRUNE_GRACE_WINDOW_S",
    "GAUGE_COVERAGE_GRACE_S",
    "EVENT_GAUGE_COVERAGE_NOTICE",
    "EVENT_GAUGE_STALE_NOTICE",
    "EVENT_SESSION_DEPENDENCY_WAKE",
    "SessionRoleClaimPruner",
    "sweep_deadline_dependencies",
]
