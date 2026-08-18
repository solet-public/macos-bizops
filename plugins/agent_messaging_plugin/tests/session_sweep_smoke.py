#!/usr/bin/env python3
"""Unit smoke for the D1 platform sweep (``session_sweep.py``) — the
``on_tick`` rider that marks overdue sessions, fires+delivers armed
``deadline`` dependency edges, and prunes stale ``session_role_claim`` rows
(Architect ratification #3). Also covers the ``retire_session`` crash-mid-
retire redrive leg (Coordinator-Dawn's explicit fold-in — both touch the
same states, so one file measures both).

``sweep_overdue_sessions`` / ``sweep_deadline_dependencies`` are pure
functions against ``RealShapeState``, with a controlled clock (no real time
in a sweep test). The dependency-delivery + pruner legs use REAL
``BridgeSessionManager``/``PeerRegistry`` instances (in-process, no server) —
the same technique ``direct_wake_outbox_smoke.py`` uses for REL-05 — so the
resolve-then-append delivery path is exercised for real, not stubbed.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_sweep_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    PEER_BINDING_NAMESPACE,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_ROLE_CLAIM,
    WORK_CLASS_READ_ONLY,
    get_peer_binding_schema,
    session_role_claim_external_id,
)
from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    upsert_session_context_status,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    backfill_registration,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    retire_session,
    terminate_session,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    DEFAULT_REGISTRATION_BOUND_S,
    EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE,
    GAUGE_COVERAGE_GRACE_S,
    NoticeLatch,
    SessionRoleClaimPruner,
    _notify_rotation_due,
    sweep_deadline_dependencies,
    sweep_gauge_coverage,
    sweep_lane_closed_dependencies,
    sweep_overdue_sessions,
    sweep_rotation_due_sessions,
    sweep_ttl_overdue_sessions,
    sweep_unregistered_spawning_sessions,
)

T0 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)


def _past_grace() -> datetime:
    """A clock far enough past the fixtures' spawn time that the gauge leg's
    startup grace no longer applies.

    The fixtures create rows at the REAL wall clock, so this is derived from
    ``datetime.now`` rather than from :data:`T0`. Every gauge-coverage test
    passes this explicitly: after the R4 lane added the grace, "this session is
    dark" is a claim about a session that has HAD TIME to report, and a test
    that does not say how old its row is no longer states its own precondition.
    """
    return datetime.now(UTC) + timedelta(seconds=GAUGE_COVERAGE_GRACE_S + 60)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _peer_registry() -> PeerRegistry:
    store: Store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _spawn_live(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lifecycle_state: str = LIFECYCLE_LIVE,
    report_by_seconds: int = 0,
    report_by_override: str | None = None,
    spawned_by_instance_id: str = "",
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-x", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host="operator",
            report_by_seconds=report_by_seconds,
            spawned_by_instance_id=spawned_by_instance_id,
        ),
    )
    if report_by_override is not None:
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
            {"report_by": report_by_override},
        )
    if lifecycle_state != LIFECYCLE_SPAWNING:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        if lifecycle_state == LIFECYCLE_IDLE:
            transition_lifecycle_state(
                state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_LIVE,
                to_state=LIFECYCLE_IDLE, directed_by="operator:none",
            )


# ---------------------------------------------------------------------------
# sweep_overdue_sessions
# ---------------------------------------------------------------------------


def test_overdue_no_report_by_never_swept() -> None:
    state = _state()
    _spawn_live(state, agent_instance_id="agi-no-contract")  # no report_by at all
    marked = sweep_overdue_sessions(state, now=T0 + timedelta(days=365))
    _check(marked == 0, "a row with no report_by is never swept (no contract, not expired)")
    _check(
        read_managed_session(state, "agi-no-contract")["lifecycle_state"] == LIFECYCLE_LIVE,
        "its lifecycle_state is untouched",
    )


def test_overdue_marks_past_deadline_live_and_idle() -> None:
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-live-late", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past,
    )
    _spawn_live(
        state, agent_instance_id="agi-idle-late", lifecycle_state=LIFECYCLE_IDLE,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(marked == 2, "both a late LIVE row and a late IDLE row are marked overdue")
    _check(
        read_managed_session(state, "agi-live-late")["lifecycle_state"] == LIFECYCLE_OVERDUE
        and read_managed_session(state, "agi-idle-late")["lifecycle_state"] == LIFECYCLE_OVERDUE,
        "both rows now read 'overdue'",
    )


def test_overdue_skips_future_deadline() -> None:
    state = _state()
    future = (T0 + timedelta(seconds=300)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-not-yet", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=future,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(marked == 0, "a report_by still in the future is not swept")


def _register_live_binding(
    reg: PeerRegistry, mgr: BridgeSessionManager, *, agent_instance_id: str,
) -> str:
    """Same pattern ``test_deadline_dependency_fires_and_delivers`` uses —
    a real bridge + a real peer registry binding, no server, so the
    resolve-then-append delivery path is exercised for real, not stubbed."""
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id=agent_instance_id,
            session_label=agent_instance_id, parent_pid=1,
        ),
    )
    return bridge_id


def test_overdue_notifies_steward() -> None:
    """D2-lane-tail follow-up #3: the fix. The MANAGED-spawner leg -- a row
    spawned WITH a recorded steward (spawned_by_instance_id) that ALSO has
    its own managed_session row goes overdue and delivers exactly one
    session_overdue_notice event to the steward's live bridge. See
    :func:`test_overdue_notifies_unmanaged_steward` for the dominant
    UNMANAGED-spawner leg (an operator-launched seat with no managed_session
    row of its own)."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the overdue row is still transitioned")
    _check(
        read_managed_session(state, "agi-worker")["lifecycle_state"] == LIFECYCLE_OVERDUE,
        "the row reads 'overdue'",
    )
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_overdue_notice"
        and "agi-worker" in events[0].content,
        f"RED-vs-GREEN: the steward's bridge gets exactly one delivered "
        f"overdue-notice event naming the overdue session (got {events!r}) "
        "-- before this fix, sweep_overdue_sessions sent NO notification "
        "of any kind",
    )


def test_overdue_notifies_unmanaged_steward() -> None:
    """RED-FIRST (D3 slice-0c, coordinator-seat queue addition 2026-08-04 13:22Z):
    the dominant case in practice -- a steward with NO managed_session row
    of its own (the operator-launched-seat shape; today every worker is
    seat-spawned, and the seat itself is operator-launched, never spawned
    via spawn_session). Before this fix, steward resolution went ONLY
    through the spawner's managed_session row for its agent_id;
    an unmanaged spawner has no such row, so the lookup returned "" and the
    notice silently never fired (measured live, session_sweep.py:175,
    2026-08-04 13:13:01Z: "spawner ... has no managed_session row ... cannot
    resolve a live binding to notify"). The fix resolves the steward
    straight from the peer registry by instance id, with no managed_session
    detour required."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # The steward is registered in the peer registry directly -- no
    # _spawn_live() call for it at all, so it has NO managed_session row.
    steward_bridge_id = _register_live_binding(
        reg, mgr, agent_instance_id="agi-unmanaged-steward",
    )
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-unmanaged-steward", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-unmanaged-steward",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the overdue row is still transitioned")
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_overdue_notice"
        and "agi-worker-unmanaged-steward" in events[0].content,
        f"RED-vs-GREEN: an UNMANAGED steward (no managed_session row of its "
        f"own) still gets the overdue notice delivered (got {events!r}) -- "
        "before this fix, resolution went only through the spawner's "
        "managed_session row and silently found nothing to notify",
    )


def test_overdue_no_spawner_is_silent_noop() -> None:
    """An operator-hosted row (or any row with no recorded spawner) has no
    steward to notify by construction -- marked overdue, zero notify
    attempts, no crash."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-orphan", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "a spawner-less row is still transitioned")
    _check(
        read_managed_session(state, "agi-orphan")["lifecycle_state"] == LIFECYCLE_OVERDUE,
        "the row reads 'overdue'",
    )


def test_overdue_unresolvable_spawner_is_best_effort() -> None:
    """A recorded spawner with no live binding (never registered, or
    already gone) is best-effort -- the row is still marked, no crash, no
    delivery."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-worker-ghost", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward-ghost",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "a row with an unresolvable spawner is still transitioned")


def test_overdue_marks_without_notify_when_registry_absent() -> None:
    """peer_registry/bridge_manager are OPTIONAL (unlike the sibling
    dependency sweeps) -- an early-boot tick with neither available must
    still mark overdue rows; it just cannot notify."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-early-boot", lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=past, spawned_by_instance_id="agi-steward-unreachable",
    )
    marked = sweep_overdue_sessions(state, now=T0)  # no peer_registry/bridge_manager at all
    _check(
        marked == 1,
        "the state transition still runs with no peer_registry/bridge_manager passed at all",
    )


def test_overdue_terminates_stuck_spawning_row() -> None:
    """RED-FIRST (the fix this test proves): a ``spawning`` row -- a
    ``spawn_session`` call whose host process never registered -- is
    invisible to today's sweep, which scans only LIFECYCLE_LIVE/IDLE. Its
    ``report_by`` deadline can pass by any amount with no transition, no
    steward notice, nothing (live-observed: a probe subject sat in
    ``spawning`` 8+ hours past deadline while its OS process stayed alive,
    doing nothing). ``LIFECYCLE_TRANSITIONS[LIFECYCLE_SPAWNING]`` (schema.py)
    has no legal ``overdue`` edge -- only ``live``/``terminated`` -- so a
    stuck spawning row has no live session to recover via a late
    ``report_alive`` and goes straight to ``terminated``, not ``overdue``."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-spawn-stuck", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(
        marked == 1,
        "RED-vs-GREEN: a stuck 'spawning' row past its report_by deadline IS "
        "swept (before this fix, sweep_overdue_sessions scanned only "
        "live/idle and this row sat in 'spawning' forever)",
    )
    _check(
        read_managed_session(state, "agi-spawn-stuck")["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "the row reaches 'terminated' directly -- 'overdue' is not a legal "
        "edge from 'spawning'",
    )


def test_overdue_skips_spawning_row_with_future_deadline() -> None:
    state = _state()
    future = (T0 + timedelta(seconds=300)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-spawn-not-yet", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=future,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(marked == 0, "a spawning row's report_by still in the future is not swept")
    _check(
        read_managed_session(state, "agi-spawn-not-yet")["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "its lifecycle_state is untouched",
    )


def test_overdue_spawning_alive_row_is_extended_not_reaped() -> None:
    """2026-08-13 (live-measured): a spawning row past its deadline whose host
    process is OBSERVED ALIVE is a live session whose registration never
    completed, not an orphaned spawn — a tmux worker productive for hours was
    reaped mid-programme by the deadline alone. Observed-alive earns a
    deadline re-arm + a distinct steward notice; nothing is terminated.

    RED MUTATION: drop the probe branch (always terminate) — this leg's
    lifecycle assertion goes red; or notify without re-arming — the deadline
    assertion goes red."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-alive-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-alive-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-alive-steward")
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-alive-unreg", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_seconds=3600, report_by_override=past,
        spawned_by_instance_id="agi-alive-steward",
    )
    marked = sweep_overdue_sessions(
        state, peer_registry=reg, bridge_manager=mgr, now=T0,
        host_alive_probe=lambda _row: True,
    )
    _check(marked == 0, "an observed-alive spawning row is NOT counted as swept")
    row = read_managed_session(state, "agi-alive-unreg")
    _check(
        row["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "observed-alive: the row stays 'spawning', never terminated",
    )
    new_report_by = str(row.get("report_by") or "")
    _check(
        new_report_by > T0.isoformat(),
        f"observed-alive: report_by was re-armed into the future (got {new_report_by!r})",
    )
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_spawn_unregistered_notice"
        and "agi-alive-unreg" in events[0].content
        and "OBSERVED ALIVE" in events[0].content,
        f"the steward gets exactly one spawn-UNREGISTERED notice (distinct "
        f"class from orphaned) naming the row (got {events!r})",
    )


def test_overdue_spawning_alive_past_patience_is_reaped() -> None:
    """The bound that keeps the alive-branch from regressing the hung-spawn
    fix (the OTHER live-measured shape: a hung process, alive, 'spawning'
    8+ hours, doing nothing): an observed-alive row whose spawn timestamp is
    older than SPAWN_ALIVE_PATIENCE_WINDOWS x its own window is reaped even
    though its process is alive — liveness cannot distinguish productive from
    hung, so patience is bounded.

    RED MUTATION: remove the patience bound — this leg's terminated
    assertion goes red (the row would be extended forever)."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-alive-exhausted", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_seconds=300, report_by_override=past,
    )
    # Age the spawn timestamp past the patience bound (4 windows x 300s).
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-alive-exhausted"}},
        {"last_transition_at": (T0 - timedelta(seconds=300 * 5)).isoformat()},
    )
    marked = sweep_overdue_sessions(state, now=T0, host_alive_probe=lambda _row: True)
    _check(marked == 1, "an observed-alive row PAST patience is swept")
    _check(
        read_managed_session(state, "agi-alive-exhausted")["lifecycle_state"]
        == LIFECYCLE_TERMINATED,
        "past patience the reap proceeds even though the process is alive",
    )


def test_overdue_spawning_operator_host_alive_is_not_evidence() -> None:
    """The operator host driver's ``alive()`` is an unconditional True by
    design (it observes via registration only) — a NON-observation. The
    production probe must not treat it as evidence, or every operator-hosted
    row would earn indefinite patience. This leg runs WITHOUT a probe
    override: the real probe sees host='operator' and declines to observe,
    so the established reap proceeds."""
    state = _state()
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-op-host", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=past,
    )
    marked = sweep_overdue_sessions(state, now=T0)
    _check(
        marked == 1,
        "an operator-hosted spawning row past deadline is reaped — the "
        "operator driver's vacuous alive() is never liveness evidence",
    )


def test_overdue_spawning_notifies_steward_of_orphan() -> None:
    """The steward (spawner) of an orphaned spawn is very likely still
    alive and would want to know its spawn never came up -- distinct event
    type from the live/idle overdue notice (a receiver must be able to
    tell the two classes apart: 'went quiet' vs 'never came up')."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-spawn-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-spawn-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-spawn-steward")
    past = (T0 - timedelta(seconds=10)).isoformat()
    _spawn_live(
        state, agent_instance_id="agi-spawn-orphan", lifecycle_state=LIFECYCLE_SPAWNING,
        report_by_override=past, spawned_by_instance_id="agi-spawn-steward",
    )
    marked = sweep_overdue_sessions(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(marked == 1, "the orphaned spawning row is still transitioned")
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_spawn_orphaned_notice"
        and "agi-spawn-orphan" in events[0].content,
        f"the steward's bridge gets exactly one delivered spawn-orphaned "
        f"notice naming the orphaned session (got {events!r})",
    )


# ---------------------------------------------------------------------------
# sweep_deadline_dependencies
# ---------------------------------------------------------------------------


def _seed_dependency(
    state: StateManagementInterface,
    *,
    row_id: str,
    condition_kind: str,
    condition_ref: str,
    waiter_instance_id: str = "",
    waiter_lane_id: str = "",
    fired_at: str | None = None,
) -> None:
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_DEPENDENCY,
            "record": {
                "id": row_id,
                "external_id": row_id,
                "condition_kind": condition_kind,
                "condition_ref": condition_ref,
                "waiter_instance_id": waiter_instance_id,
                "waiter_lane_id": waiter_lane_id,
                "fired_at": fired_at,
            },
        },
    )


def test_deadline_dependency_not_yet_due_skipped() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    future = (T0 + timedelta(seconds=60)).isoformat()
    _seed_dependency(
        state, row_id="sdp-future", condition_kind=CONDITION_DEADLINE, condition_ref=future,
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 0, "a deadline still in the future is not fired")


def test_deadline_dependency_fires_and_delivers() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-waiter")
    # The D1 registration hook that would normally backfill agent_id onto a
    # managed_session row does not exist yet (Reviewer-A's independent
    # finding, out of scope for this slice — headless-adapter work) — set it
    # directly here to exercise the delivery path AS IF that hook existed.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-waiter"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id="agi-waiter",
            session_label="Waiter", parent_pid=1,
        ),
    )
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-1", condition_kind=CONDITION_DEADLINE, condition_ref=past,
        waiter_instance_id="agi-waiter",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "a past-deadline armed edge is fired exactly once")
    rows = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_DEPENDENCY, "filters": {"id": "sdp-1"}},
    )["data"]["records"]
    _check(rows[0]["fired_at"] is not None, "fired_at is stamped on the edge")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_dependency_wake"
        and "deadline" in events[0].content,
        f"the waiter's bridge gets exactly one delivered wake event (got {events!r})",
    )
    again = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _, events2 = mgr.get(bridge_id).events_after(events[-1].cursor)
    _check(
        again == 0 and events2 == [],
        "a re-run does not re-fire or re-deliver an already-fired edge",
    )


def test_deadline_dependency_unmanaged_waiter_still_delivers() -> None:
    """RED-FIRST (phase-2 slice 3, the phase-1 unified finding — seat
    log-proven 2026-08-05 00:13:27Z on edge sdp-2nm84y6h8k21s): a
    watch-transport waiter registered directly in the peer registry (no
    ``managed_session`` row of its own -- the dominant shape for a watch-arm
    subject or any other non-spawn_session-managed session) must still get
    the wake delivered. Before this fix, ``_deliver_dependency_wake``
    resolved the waiter's ``agent_id`` ONLY via its ``managed_session`` row
    (``_managed_session_agent_id``) and returned on an empty result BEFORE
    ever consulting the peer registry -- so an unmanaged, watch-registered
    waiter got a 'no managed_session row' WARNING and no delivery, for all
    three condition kinds. Mirrors the identical fix already landed for
    ``_notify_steward_of_overdue`` (see
    ``test_overdue_notifies_unmanaged_steward`` above) and the observed live
    identity shape (``agi-watch-...``)."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # The waiter is registered in the peer registry directly -- no
    # _spawn_live() call for it at all, so it has NO managed_session row.
    bridge_id = _register_live_binding(
        reg, mgr, agent_instance_id="agi-watch-92a6ae0e3e134e5e11774007",
    )
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-unmanaged-waiter", condition_kind=CONDITION_DEADLINE,
        condition_ref=past, waiter_instance_id="agi-watch-92a6ae0e3e134e5e11774007",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "the deadline edge fires")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "session_dependency_wake"
        and "deadline" in events[0].content,
        f"RED-vs-GREEN: an UNMANAGED (watch-registered) waiter still gets the "
        f"wake delivered (got {events!r}) -- before this fix, resolution went "
        "only through the waiter's managed_session row and silently found "
        "nothing to notify",
    )


def test_deadline_dependency_unresolvable_waiter_is_best_effort() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # No managed_session row at all for this waiter — agent_id is unknowable.
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-2", condition_kind=CONDITION_DEADLINE, condition_ref=past,
        waiter_instance_id="agi-ghost",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 1,
        "the edge still fires (state) even though delivery cannot be resolved",
    )


def test_deadline_dependency_lane_scoped_is_logged_noop() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state, row_id="sdp-3", condition_kind=CONDITION_DEADLINE, condition_ref=past,
        waiter_lane_id="lane-only",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 1,
        "a lane-scoped edge (no waiter_instance_id) still fires; delivery is a no-op, "
        "not a crash",
    )


# ---------------------------------------------------------------------------
# sweep_lane_closed_dependencies (Dawn ruling 2026-08-03, arm-124065ee —
# 'lane_closed' replaced the unbuildable 'lane_landed' spec kind)
# ---------------------------------------------------------------------------


def test_lane_closed_empty_lane_is_not_closed() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _seed_dependency(
        state, row_id="sdp-lane-empty", condition_kind=CONDITION_LANE_CLOSED,
        condition_ref="lane-never-spawned",
    )
    fired = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 0,
        "a lane_id with ZERO managed_session rows is NOT closed — no vacuous "
        "truth on an empty set",
    )


def test_lane_closed_open_while_any_session_non_terminal() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-lane-a", lifecycle_state=LIFECYCLE_LIVE)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-lane-a"}},
        {"lane_id": "lane-mixed"},
    )
    terminate_session(state, agent_instance_id="agi-lane-a", directed_by="operator:none")
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-lane-b", lane_id="lane-mixed", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host="operator",
        ),
    )  # left 'spawning' — non-terminal
    _seed_dependency(
        state, row_id="sdp-lane-mixed", condition_kind=CONDITION_LANE_CLOSED,
        condition_ref="lane-mixed",
    )
    fired = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 0,
        "one terminal + one non-terminal managed_session row for the lane -> "
        "NOT closed yet",
    )


def test_lane_closed_fires_when_every_session_terminal() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    for agi in ("agi-lane-x", "agi-lane-y"):
        _spawn_live(state, agent_instance_id=agi, lifecycle_state=LIFECYCLE_LIVE)
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": agi}},
            {"lane_id": "lane-done"},
        )
        terminate_session(state, agent_instance_id=agi, directed_by="operator:none")
    _seed_dependency(
        state, row_id="sdp-lane-done", condition_kind=CONDITION_LANE_CLOSED,
        condition_ref="lane-done",
    )
    fired = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "every managed_session row for the lane is terminal -> fires")
    again = sweep_lane_closed_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(again == 0, "a re-run does not re-fire an already-fired lane_closed edge")


# ---------------------------------------------------------------------------
# SessionRoleClaimPruner
# ---------------------------------------------------------------------------


def _seed_claim(
    state: StateManagementInterface, *, agent_session_id: str, held_role: str,
) -> None:
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "record": {
                "id": f"src-{agent_session_id}",
                "external_id": session_role_claim_external_id(agent_session_id),
                "agent_session_id": agent_session_id,
                "held_role": held_role,
                "agent_instance_id": f"agi-{agent_session_id}",
                "claimed_at": T0.isoformat(),
            },
        },
    )


def _claim_rows(state: StateManagementInterface) -> list[dict[str, Any]]:
    return [
        r for r in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_SESSION_ROLE_CLAIM)
        if not r.get("is_deleted")
    ]


def test_pruner_terminal_managed_session_pruned_immediately() -> None:
    state = _state()
    reg = _peer_registry()
    _spawn_live(state, agent_instance_id="agi-term")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-term"}},
        {"agent_session_id": "sess-term"},
    )
    terminate_session(state, agent_instance_id="agi-term", directed_by="operator:none")
    _seed_claim(state, agent_session_id="sess-term", held_role="Some-Lane")
    pruner = SessionRoleClaimPruner(clock=lambda: T0)
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 1 and _claim_rows(state) == [],
        "a claim whose managed_session is terminal is pruned with NO grace wait",
    )


def test_pruner_live_managed_session_never_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    _spawn_live(state, agent_instance_id="agi-alive")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-alive"}},
        {"agent_session_id": "sess-alive"},
    )
    _seed_claim(state, agent_session_id="sess-alive", held_role="Some-Lane")
    pruner = SessionRoleClaimPruner(clock=lambda: T0 + timedelta(days=365))
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 0 and len(_claim_rows(state)) == 1,
        "a claim whose managed_session is non-terminal is NEVER pruned (ledger-authoritative "
        "alive), regardless of elapsed time",
    )


def test_pruner_live_registered_session_never_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code", agent_instance_id="agi-reg",
            session_label="Reg", parent_pid=1, agent_session_id="sess-reg",
        ),
    )
    _seed_claim(state, agent_session_id="sess-reg", held_role="Some-Lane")
    pruner = SessionRoleClaimPruner(clock=lambda: T0 + timedelta(days=365))
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 0 and len(_claim_rows(state)) == 1,
        "no managed_session row, but a LIVE registry binding for the session -> never pruned",
    )


def test_pruner_absence_within_grace_window_not_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    _seed_claim(state, agent_session_id="sess-ghost", held_role="Some-Lane")
    clock_value = {"now": T0}
    pruner = SessionRoleClaimPruner(grace_window_s=300, clock=lambda: clock_value["now"])
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 0 and len(_claim_rows(state)) == 1,
        "genuine absence (no managed_session, no live binding) within the grace window "
        "is NOT pruned — the blue-green-bounce guard",
    )
    clock_value["now"] = T0 + timedelta(seconds=100)
    pruned2 = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned2 == 0 and len(_claim_rows(state)) == 1,
        "still within the window on the next tick -> still not pruned",
    )


def test_pruner_absence_past_grace_window_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    _seed_claim(state, agent_session_id="sess-stale", held_role="Some-Lane")
    clock_value = {"now": T0}
    pruner = SessionRoleClaimPruner(grace_window_s=300, clock=lambda: clock_value["now"])
    pruner.sweep(state, peer_registry=reg)  # first-observed-absent stamped at T0
    clock_value["now"] = T0 + timedelta(seconds=301)
    pruned = pruner.sweep(state, peer_registry=reg)
    _check(
        pruned == 1 and _claim_rows(state) == [],
        "absence past the grace window IS pruned",
    )


# ---------------------------------------------------------------------------
# retire_session crash-mid-retire redrivability (Coordinator-Dawn fold-in)
# ---------------------------------------------------------------------------


def test_retire_session_crash_mid_retire_is_redrivable() -> None:
    """Simulates a crash BETWEEN retire_session's steps: the row is already
    'terminated' (step 1 done, by a prior crashed attempt or a plain
    terminate_session call) and an armed session_terminal dependency edge is
    still un-fired (step 3 not done) — re-running retire_session must finish
    the job: fire the pending edge and complete terminated -> retired.
    """
    state = _state()
    _spawn_live(state, agent_instance_id="agi-crash")
    terminate_session(state, agent_instance_id="agi-crash", directed_by="operator:none")
    _seed_dependency(
        state, row_id="sdp-crash", condition_kind=CONDITION_SESSION_TERMINAL,
        condition_ref="agi-crash", waiter_instance_id="agi-waiter-crash",
    )
    _check(
        read_managed_session(state, "agi-crash")["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "setup: the row is 'terminated' but NOT yet 'retired' (simulating the crash point)",
    )
    result = retire_session(state, agent_instance_id="agi-crash", directed_by="operator:none")
    _check(
        result == {"already_retired": False, "dependencies_fired": 1},
        f"re-running retire_session finishes the job: fires the pending edge and "
        f"completes the transition (got {result!r})",
    )
    _check(
        read_managed_session(state, "agi-crash")["lifecycle_state"] == LIFECYCLE_RETIRED,
        "the row reaches 'retired' despite the simulated mid-retire crash",
    )


# ---------------------------------------------------------------------------
# W4A registration watchdog (sweep_unregistered_spawning_sessions)
# ---------------------------------------------------------------------------


def _spawn_unregistered(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    spawned_at: datetime,
    host: str = "headless",
    spawned_by_instance_id: str = "",
    degraded_hooks_acknowledged: bool = False,
) -> None:
    """A ``spawning`` row whose spawn timestamp we CONTROL, so the watchdog
    tests advance a clock across the bound instead of asserting on a static
    row. ``last_transition_at`` is the anchor the bound is measured from."""
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id="lane-z", brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line="b1", host=host,
            spawned_by_instance_id=spawned_by_instance_id,
            degraded_hooks_acknowledged=degraded_hooks_acknowledged,
        ),
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {"last_transition_at": spawned_at.isoformat()},
    )


def test_registration_within_bound_is_not_marked() -> None:
    """The advancing half #1: the SAME row, read before the bound, is clean."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-fresh", spawned_at=T0)
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S - 1),
    )
    _check(marked == 0, "a spawning row inside the registration bound is not marked")
    _check(
        not read_managed_session(state, "agi-fresh").get("registration_overdue_at"),
        "and carries no registration_overdue_at",
    )


def test_registration_past_bound_marks_field_not_state() -> None:
    """The advancing half #2 AND the design call itself: past the bound the
    row is MARKED but its lifecycle_state is untouched. A new lifecycle state
    would have destroyed the fact that the row is still spawning; the field
    keeps both facts."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-deaf", spawned_at=T0)
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(marked == 1, "the same row, past the bound, is marked")
    row = read_managed_session(state, "agi-deaf")
    _check(bool(row.get("registration_overdue_at")), "registration_overdue_at is stamped")
    _check(
        row["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "FIELD-NOT-STATE: lifecycle_state is still 'spawning' -- the watchdog "
        "attributes, it does not transition",
    )
    reason = str(row.get("registration_overdue_reason") or "")
    _check(
        "has not registered" in reason and "registration hook has not run" in reason,
        f"the reason states what was OBSERVED at the seam (got {reason!r})",
    )


def test_registration_watchdog_never_reaps() -> None:
    """The other half of the leg-separation contract: unlike the report_by
    spawning leg, this one kills nothing, even long past the bound."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-alive", spawned_at=T0)
    sweep_unregistered_spawning_sessions(state, now=T0 + timedelta(days=365))
    _check(
        read_managed_session(state, "agi-alive")["lifecycle_state"] == LIFECYCLE_SPAWNING,
        "a year past the bound the row is STILL 'spawning' -- attribution, never the reaper",
    )


def test_registration_fires_without_any_report_by() -> None:
    """Independence from the report-or-die contract: an operator-host row is
    given no report_by by insert_managed_session, and the report_by spawning
    leg skips such a row by design. The watchdog must not inherit that blind
    spot -- its bound is registration, not the work deadline."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-nocontract", spawned_at=T0, host="operator")
    _check(
        not read_managed_session(state, "agi-nocontract").get("report_by"),
        "setup: the row genuinely has no report_by",
    )
    _check(
        sweep_overdue_sessions(state, now=T0 + timedelta(days=365)) == 0,
        "setup: the report_by spawning leg cannot see it (no contract)",
    )
    marked = sweep_unregistered_spawning_sessions(state, now=T0 + timedelta(days=365))
    _check(marked == 1, "the registration watchdog marks it anyway")
    _check(
        bool(read_managed_session(state, "agi-nocontract").get("registration_overdue_at")),
        "and the mark is actually ON THE ROW, not merely counted by the sweep",
    )


def test_registration_mark_is_idempotent_and_keeps_first_observation() -> None:
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-once", spawned_at=T0)
    first_clock = T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1)
    sweep_unregistered_spawning_sessions(state, now=first_clock)
    stamped = read_managed_session(state, "agi-once")["registration_overdue_at"]
    again = sweep_unregistered_spawning_sessions(state, now=first_clock + timedelta(hours=5))
    _check(again == 0, "a second sweep does not re-mark an already-marked row")
    _check(
        read_managed_session(state, "agi-once")["registration_overdue_at"] == stamped,
        "the field records the FIRST observation ('since when'), not the last tick",
    )


def test_registration_late_registration_clears_the_mark() -> None:
    """A worker that registers LATE is a different story from one that never
    did, so the mark clears rather than leaving the row permanently deaf."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-late", spawned_at=T0)
    sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(
        bool(read_managed_session(state, "agi-late").get("registration_overdue_at")),
        "setup: the row is marked registration-overdue",
    )
    backfill_registration(
        state, agent_instance_id="agi-late", agent_id="claude_code",
        agent_session_id="ases-agi-late",
    )
    row = read_managed_session(state, "agi-late")
    _check(not row.get("registration_overdue_at"), "a late registration clears the mark")
    _check(row["lifecycle_state"] == LIFECYCLE_LIVE, "and the row completes spawning->live")


def test_registration_non_spawning_rows_are_never_marked() -> None:
    state = _state()
    _spawn_live(state, agent_instance_id="agi-running", lifecycle_state=LIFECYCLE_LIVE)
    marked = sweep_unregistered_spawning_sessions(state, now=T0 + timedelta(days=365))
    _check(marked == 0, "a row that already registered (live) is never marked")


def test_registration_acknowledged_degraded_is_marked_but_says_so() -> None:
    """Item 3's half of the story: an acknowledged degraded spawn is still
    observed and still recorded -- honesty about what happened -- but the
    reason says the risk was accepted, so it does not read as a surprise."""
    state = _state()
    _spawn_unregistered(
        state, agent_instance_id="agi-degraded", spawned_at=T0,
        degraded_hooks_acknowledged=True,
    )
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(marked == 1, "an acknowledged-degraded row is still marked (the fact is still true)")
    _check(
        "ACKNOWLEDGED" in str(
            read_managed_session(state, "agi-degraded").get("registration_overdue_reason") or "",
        ),
        "but its reason records that this was an accepted risk",
    )


def test_registration_notifies_steward_with_distinct_event() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward-w4a")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward-w4a"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward-w4a")
    _spawn_unregistered(
        state, agent_instance_id="agi-deaf-child", spawned_at=T0,
        spawned_by_instance_id="agi-steward-w4a",
    )
    sweep_unregistered_spawning_sessions(
        state, peer_registry=reg, bridge_manager=mgr,
        now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _, events = mgr.get(steward_bridge_id).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == EVENT_SESSION_REGISTRATION_OVERDUE_NOTICE
        and "agi-deaf-child" in events[0].content,
        f"the steward gets exactly one registration-overdue notice, under an "
        f"event type distinct from the other three spawn notices (got {events!r})",
    )


def test_registration_marks_without_notify_when_registry_absent() -> None:
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-noreg", spawned_at=T0)
    marked = sweep_unregistered_spawning_sessions(
        state, now=T0 + timedelta(seconds=DEFAULT_REGISTRATION_BOUND_S + 1),
    )
    _check(marked == 1, "an early-boot tick with no bridge still MARKS the row")


# ---------------------------------------------------------------------------
# L4a: sweep_rotation_due_sessions / sweep_gauge_coverage
# ---------------------------------------------------------------------------


def _gauge(state: StateManagementInterface, agent_instance_id: str, **over: object) -> None:
    """Write a gauge row the way report_context_status would."""
    kwargs: dict[str, object] = {
        "agent_instance_id": agent_instance_id, "claude_session_id": "s1",
        "model": "claude-sonnet-5", "current_tokens": 900_000, "ceiling": 1_000_000,
        "measured_at": T0.isoformat(), "cache_cold": False,
        "reporter_surface": "checkout", "reporter_generation": 2,
    }
    kwargs.update(over)
    upsert_session_context_status(state, **kwargs)  # type: ignore[arg-type]


def _wired() -> tuple[StateManagementInterface, PeerRegistry, BridgeSessionManager, str]:
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    return state, reg, mgr, bridge_id


def test_rotation_due_notice_carries_the_measured_number() -> None:
    """The charter: never a bare 'you should rotate'."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _check(n == 1, "a session past the rotation threshold produces one notice")
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check(events and events[0].event_type == "rotation_due_notice",
           "the event is typed rotation_due_notice, distinct from the overdue notice")
    _check("900000" in body, "the notice carries the MEASURED token count, not a bare verdict")
    _check("claude-sonnet-5" in body,
           "the notice names the MODEL beside the band -- the bands are model-blind")


def test_rotation_due_is_silent_below_the_threshold() -> None:
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker", current_tokens=1_000)
    n = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _check(n == 0, "a session well under the threshold produces no notice")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and nothing is delivered -- a notice that always fires is ignored")


def test_rotation_due_flags_an_unattributable_reporter() -> None:
    """A stale-copy row sends no cache state, so its band is the WARM DEFAULT
    rather than a measurement. Presenting that as an urgent verdict is false
    precision, so the notice says the reporter cannot be attributed."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker", reporter_surface=None, reporter_generation=None)
    sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content if events else ""
    _check("UNATTRIBUTABLE" in body,
           "a row from a pre-attribution reporter is flagged, not silently trusted")
    _check("provisional" in body,
           "and the band is marked provisional rather than presented as measured")


def test_gauge_coverage_catches_a_live_session_with_no_row() -> None:
    """The signature measured 2026-08-16: hooks running, gauge write silently
    failing. Neither the hook (it must swallow its own faults) nor the session
    (it does not know) can report this; the sweep sees both facts."""
    state, reg, mgr, bridge_id = _wired()  # agi-worker is LIVE with NO gauge row
    n = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "a live session with no gauge row is detected")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(events and events[0].event_type == "gauge_coverage_notice"
           and "agi-worker" in events[0].content,
           "the steward is told which session is dark")


def test_gauge_coverage_is_silent_when_the_row_exists() -> None:
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    n = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 0, "a session that IS reporting produces no coverage notice")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and nothing is delivered")


# ---------------------------------------------------------------------------
# R4 change 1: the gauge leg's STARTUP GRACE
# ---------------------------------------------------------------------------


def test_gauge_coverage_grants_a_newly_live_session_its_startup_grace() -> None:
    """The false alarm this fixes, measured live 2026-08-17T16:33:11Z.

    Four lanes ~2 minutes old were reported as "the reporting path is failing
    SILENTLY". All four were merely NEW — they had not completed a first
    reporting tick — and every one reported normally minutes later. A newly LIVE
    session is dark by construction until its first tick, so without this
    predicate every spawn wave manufactures one false alarm per lane.

    The latch cannot substitute for it: each wave is a fresh episode with fresh
    keys, so suppression of a REPEAT does nothing about a fresh false POSITIVE.
    """
    state, reg, mgr, bridge_id = _wired()  # agi-worker LIVE, no gauge row, born now
    n = sweep_gauge_coverage(
        state, now=datetime.now(UTC), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 0, "a just-born live session is NOT called dark")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and its steward is not woken about it")


def test_gauge_coverage_still_fires_once_the_grace_expires() -> None:
    """The other half, and the one that keeps the grace from being a mute
    button: the SAME row, still dark, is reported once it has had time."""
    state, reg, mgr, bridge_id = _wired()
    early = sweep_gauge_coverage(
        state, now=datetime.now(UTC), peer_registry=reg, bridge_manager=mgr,
    )
    late = sweep_gauge_coverage(
        state, now=_past_grace(), peer_registry=reg, bridge_manager=mgr,
    )
    _check((early, late) == (0, 1), "silent while young, reported once aged")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly one notice, delivered on the later tick")
    _check(
        "startup grace" in events[0].content,
        "and the prose says it is past the grace, so the reader knows this is "
        "not simply a session that has not reported yet",
    )


def test_gauge_coverage_does_not_grant_grace_on_an_unreadable_timestamp() -> None:
    """The fail-toward direction, stated because it is the opposite of
    _spawn_alive_patience_exhausted's and a reader will expect that one.

    The grace is an EXCEPTION to an alarm, so it may only apply on positive
    evidence that the row is young. A row whose transition timestamp cannot be
    read is still reported — suppressing an alarm on a timestamp nobody could
    parse is how a detector goes quiet for a reason nobody chose.
    """
    state, reg, mgr, _bridge_id = _wired()
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-worker"}},
        {"last_transition_at": ""},
    )
    n = sweep_gauge_coverage(
        state, now=datetime.now(UTC), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "an unreadable age does NOT buy silence")


# ---------------------------------------------------------------------------
# L4b composition: NoticeLatch — what makes the two legs SAFE to put on a tick
# ---------------------------------------------------------------------------


def test_rotation_due_notifies_once_per_episode() -> None:
    """The composition guard. Unlike the overdue notice, rotation-due rides no
    state edge: the gauge stays over the threshold until the session rotates,
    so on a 300s tick an unlatched leg delivers the same notice every 5 minutes
    forever. Repetition is not a smaller version of the warning -- it destroys
    the channel the warning arrives on."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    latch = NoticeLatch()
    first = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    second = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    third = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _check((first, second, third) == (1, 0, 0), "the condition persists; the notice does not")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly ONE event reached the steward across three ticks")


def test_rotation_due_latch_rearms_when_the_session_rotates() -> None:
    """One notice per EPISODE, not one per lifetime. A session that rotates and
    later climbs back over the threshold is a NEW fact about the world, and
    suppressing it would make the latch a mute button."""
    state, reg, mgr, bridge_id = _wired()
    _gauge(state, "agi-worker")
    latch = NoticeLatch()
    sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _gauge(state, "agi-worker", current_tokens=1_000)  # rotated: back under the threshold
    cleared = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _gauge(state, "agi-worker", current_tokens=950_000)  # climbed again: a second episode
    again = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _check(cleared == 0, "no notice while the condition is clear")
    _check(again == 1, "a SECOND episode notifies again -- the latch released on the clear")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 2, "two episodes, two events")


def test_rotation_due_latch_does_not_swallow_an_undelivered_notice() -> None:
    """Latch on DELIVERY, never on detection. If the notice could not be
    delivered (no live steward binding this tick), latching it would let the
    delivery failure silence the whole episode -- the failure mode where the
    louder the outage, the quieter the alarm."""
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    _gauge(state, "agi-worker")
    latch = NoticeLatch()
    undelivered = sweep_rotation_due_sessions(
        state, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(undelivered == 0, "no live steward binding -- nothing delivered")
    bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    retried = sweep_rotation_due_sessions(state, peer_registry=reg, bridge_manager=mgr, latch=latch)
    _check(retried == 1, "the next tick RETRIES -- an undelivered notice was never latched")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "and the steward gets it once, not never and not twice")


def test_gauge_coverage_notifies_once_and_releases_on_recovery() -> None:
    """Same discipline on the darkness notice. A dark session stays dark until
    a person fixes it, so unlatched this repeats for the whole outage.

    The re-arm is asserted through the latch's own state rather than by
    staging a second outage: the ONLY way a gauge row goes missing again once
    it exists is a deletion, and manufacturing one here would be testing a
    fixture rather than the leg. What is genuinely reachable -- and what this
    asserts -- is that recovery RELEASES the key, so a later outage is a fresh
    notice instead of a permanent silence."""
    state, reg, mgr, bridge_id = _wired()  # agi-worker LIVE, no gauge row
    latch = NoticeLatch()
    aged = _past_grace()
    first = sweep_gauge_coverage(
        state, now=aged, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    second = sweep_gauge_coverage(
        state, now=aged, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check((first, second) == (1, 0), "one notice for one outage, not one per tick")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly ONE event across the outage's ticks")
    _check(latch.suppressed("agi-worker"), "the key is latched while the outage holds")
    _gauge(state, "agi-worker")  # reporting recovered
    recovered = sweep_gauge_coverage(
        state, now=aged, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(recovered == 0, "nothing to say while it reports")
    _check(
        not latch.suppressed("agi-worker"),
        "and recovery RELEASED the key -- a later outage notifies rather than being "
        "suppressed by the first one",
    )


# ---------------------------------------------------------------------------
# R4 change 3: a notice must not be able to swallow its own message bug
# ---------------------------------------------------------------------------


def test_a_broken_notice_message_surfaces_instead_of_being_swallowed() -> None:
    """Found by M5's blast radius, not by a separate investigation.

    Both notice legs composed their prose as an ARGUMENT INSIDE the try that
    guards ``append_event``. That guard exists for DELIVERY faults, but a broad
    ``except Exception`` around the prose too means a bug in the message itself
    is caught, logged as "append failed", and the notice silently vanishes while
    the log names the wrong cause. In a notice family whose entire purpose is to
    be the thing that speaks up, that is the fail-open shape these legs exist to
    catch, living inside the alarm.

    ``_rotation_prose`` formats ``fraction`` with ``:.3f``, so an enriched row
    without it raises. With the prose composed outside the try, that surfaces.
    Swallowing it would return False and report zero — indistinguishable from an
    unreachable steward.
    """
    state, reg, mgr, _bridge_id = _wired()
    raised = False
    try:
        _notify_rotation_due(
            state=state, peer_registry=reg, bridge_manager=mgr,
            row={},  # no 'fraction' -> _rotation_prose raises
            agent_instance_id="agi-worker", spawner_instance_id="agi-steward",
        )
    except (TypeError, ValueError):
        raised = True
    _check(raised, "a broken notice MESSAGE surfaces rather than being reported "
                   "as a delivery failure")


# ---------------------------------------------------------------------------
# R4 change 2: the TTL leg — expires_at was declared and NEVER READ
# ---------------------------------------------------------------------------


def _expire(state: StateManagementInterface, agent_instance_id: str, when: datetime) -> None:
    """Set a row's expires_at, which insert_managed_session only writes when the
    spawn requested ttl_seconds."""
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {"expires_at": when.isoformat()},
    )


def test_ttl_overdue_notifies_the_steward() -> None:
    """R4's whole point: expires_at had three touch points in the plugin (the
    column, one write at spawn, one output-schema entry) and ZERO readers. A
    knob that is never enforced is decoration, and the decoration cost a lane
    ~4h40m of nobody being told."""
    state, reg, mgr, bridge_id = _wired()
    _expire(state, "agi-worker", T0 - timedelta(hours=2))
    n = sweep_ttl_overdue_sessions(
        state, now=T0, peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "a past-TTL live session is detected")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(
        events and events[0].event_type == "ttl_overdue_notice",
        "delivered under its own event type, distinguishable from the other notices",
    )
    _check("agi-worker" in events[0].content, "and it names the session")


def test_ttl_notice_names_both_clocks_and_the_measured_overdue() -> None:
    """The two-clock confusion is the failure mode this text exists to prevent.

    A row can be past expires_at while its report_by sits HOURS LATER, because
    report_by is re-armed on every report and expires_at is frozen at spawn. A
    reader who sees only "past TTL" on such a row concludes the notice is buggy.
    """
    state, reg, mgr, bridge_id = _wired()
    _expire(state, "agi-worker", T0 - timedelta(hours=2, minutes=30))
    sweep_ttl_overdue_sessions(state, now=T0, peer_registry=reg, bridge_manager=mgr)
    _, events = mgr.get(bridge_id).events_after(-1)
    body = events[0].content
    _check("2h30m" in body, "the MEASURED overdue duration, not a bare 'past TTL'")
    _check("expires_at" in body and "report_by" in body, "BOTH clocks are named")
    _check(
        "NOTHING HAS BEEN DONE" in body,
        "and it says plainly that nothing was reaped — the platform notices, "
        "the steward decides",
    )


def test_ttl_silent_for_a_row_that_never_requested_a_ttl() -> None:
    """The load-bearing skip. expires_at is written ONLY when the spawn asked
    for ttl_seconds, so an absent value means 'unbounded by request' — never
    'expired at the epoch'. Read the other way, this leg would have fired on
    every operator-launched and ad-hoc row in the ledger on its first tick."""
    state, reg, mgr, bridge_id = _wired()  # agi-worker has NO expires_at
    n = sweep_ttl_overdue_sessions(
        state, now=T0 + timedelta(days=3650), peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 0, "no TTL requested is not an expiry, even ten years on")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(not events, "and nothing is delivered")


def test_ttl_silent_before_the_deadline() -> None:
    state, reg, mgr, _bridge_id = _wired()
    _expire(state, "agi-worker", T0 + timedelta(hours=1))
    n = sweep_ttl_overdue_sessions(
        state, now=T0, peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 0, "a session inside its TTL is not notified about")


def test_ttl_reads_expires_at_and_not_report_by() -> None:
    """The clock choice, asserted rather than described.

    A lane that keeps reporting re-arms report_by forever. If this leg read
    report_by, TTL would be structurally unreachable for exactly the sessions it
    exists to catch — inert in precisely the case it was built for. So: a row
    whose report_by is far in the FUTURE and whose expires_at is in the PAST
    must still fire.
    """
    state, reg, mgr, _bridge_id = _wired()
    _expire(state, "agi-worker", T0 - timedelta(hours=6))
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-worker"}},
        {"report_by": (T0 + timedelta(hours=6)).isoformat()},
    )
    n = sweep_ttl_overdue_sessions(
        state, now=T0, peer_registry=reg, bridge_manager=mgr,
    )
    _check(n == 1, "a healthy, chatty, past-TTL lane still expires")


def test_ttl_notifies_once_per_episode() -> None:
    """Latched, and the case is stronger here than for the L4 legs: TTL-overdue
    can NEVER clear on its own, because expires_at is frozen and the clock only
    advances. Unlatched, this notifies every tick forever."""
    state, reg, mgr, bridge_id = _wired()
    _expire(state, "agi-worker", T0 - timedelta(hours=2))
    latch = NoticeLatch()
    first = sweep_ttl_overdue_sessions(
        state, now=T0, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    second = sweep_ttl_overdue_sessions(
        state, now=T0 + timedelta(minutes=5), peer_registry=reg,
        bridge_manager=mgr, latch=latch,
    )
    _check((first, second) == (1, 0), "one notice per episode, not one per tick")
    _, events = mgr.get(bridge_id).events_after(-1)
    _check(len(events) == 1, "exactly ONE event across both ticks")
    _check(latch.suppressed("agi-worker"), "the key stays latched while it holds")


def test_ttl_latch_does_not_swallow_an_undelivered_notice() -> None:
    """Latched on DELIVERY, never on detection — an outage must not silence its
    own alarm. With no resolvable steward the notice cannot be delivered, so the
    key must stay un-latched and retry on the next tick."""
    state, reg, mgr, _bridge_id = _wired()
    _spawn_live(state, agent_instance_id="agi-orphan", spawned_by_instance_id="agi-nobody")
    _expire(state, "agi-orphan", T0 - timedelta(hours=1))
    latch = NoticeLatch()
    n = sweep_ttl_overdue_sessions(
        state, now=T0, peer_registry=reg, bridge_manager=mgr, latch=latch,
    )
    _check(n == 0, "an unresolvable steward means nothing was delivered")
    _check(
        not latch.suppressed("agi-orphan"),
        "and an undelivered notice is NOT latched — the next tick retries",
    )


def test_ttl_leg_no_ops_without_a_bridge() -> None:
    """Early-boot posture, matching every sibling: no registry/manager means
    return 0, never raise."""
    state = _state()
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    _expire(state, "agi-worker", T0 - timedelta(hours=1))
    _check(sweep_ttl_overdue_sessions(state, now=T0) == 0, "TTL leg no-ops with no bridge")


def test_latches_are_independent_per_notice_kind() -> None:
    """Why the rider holds TWO latches rather than one shared set: the same
    agent_instance_id can be both rotation-due and dark, and a shared latch
    would let whichever notice fired first suppress the other kind entirely."""
    latch = NoticeLatch()
    _check(not latch.suppressed("agi-x"), "an unseen key is not suppressed")
    latch.record_sent("agi-x")
    _check(latch.suppressed("agi-x"), "a recorded key suppresses its repeat")
    latch.retain_active({"agi-x"})
    _check(latch.suppressed("agi-x"), "a still-active key stays latched")
    latch.retain_active(set())
    _check(not latch.suppressed("agi-x"), "a cleared condition releases the key")


def test_l4a_legs_no_op_without_a_bridge() -> None:
    """Same posture as sweep_overdue_sessions: an early-boot tick with no
    bridge must not raise. Unlike the overdue sweep there is no state
    transition to preserve here, so both legs simply return 0."""
    state = _state()
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    _check(sweep_rotation_due_sessions(state) == 0, "rotation-due leg no-ops with no bridge")
    _check(sweep_gauge_coverage(state) == 0, "gauge-coverage leg no-ops with no bridge")


def main() -> int:
    test_overdue_no_report_by_never_swept()
    test_overdue_marks_past_deadline_live_and_idle()
    test_overdue_skips_future_deadline()
    test_overdue_notifies_steward()
    test_overdue_notifies_unmanaged_steward()
    test_overdue_no_spawner_is_silent_noop()
    test_overdue_unresolvable_spawner_is_best_effort()
    test_overdue_marks_without_notify_when_registry_absent()
    test_overdue_terminates_stuck_spawning_row()
    test_overdue_skips_spawning_row_with_future_deadline()
    test_overdue_spawning_alive_row_is_extended_not_reaped()
    test_overdue_spawning_alive_past_patience_is_reaped()
    test_overdue_spawning_operator_host_alive_is_not_evidence()
    test_overdue_spawning_notifies_steward_of_orphan()
    test_deadline_dependency_not_yet_due_skipped()
    test_deadline_dependency_fires_and_delivers()
    test_deadline_dependency_unmanaged_waiter_still_delivers()
    test_deadline_dependency_unresolvable_waiter_is_best_effort()
    test_deadline_dependency_lane_scoped_is_logged_noop()
    test_lane_closed_empty_lane_is_not_closed()
    test_lane_closed_open_while_any_session_non_terminal()
    test_lane_closed_fires_when_every_session_terminal()
    test_pruner_terminal_managed_session_pruned_immediately()
    test_pruner_live_managed_session_never_pruned()
    test_pruner_live_registered_session_never_pruned()
    test_pruner_absence_within_grace_window_not_pruned()
    test_pruner_absence_past_grace_window_pruned()
    test_retire_session_crash_mid_retire_is_redrivable()
    test_registration_within_bound_is_not_marked()
    test_registration_past_bound_marks_field_not_state()
    test_registration_watchdog_never_reaps()
    test_registration_fires_without_any_report_by()
    test_registration_mark_is_idempotent_and_keeps_first_observation()
    test_registration_late_registration_clears_the_mark()
    test_registration_non_spawning_rows_are_never_marked()
    test_registration_acknowledged_degraded_is_marked_but_says_so()
    test_registration_notifies_steward_with_distinct_event()
    test_registration_marks_without_notify_when_registry_absent()

    test_rotation_due_notice_carries_the_measured_number()
    test_rotation_due_is_silent_below_the_threshold()
    test_rotation_due_flags_an_unattributable_reporter()
    test_gauge_coverage_catches_a_live_session_with_no_row()
    test_gauge_coverage_is_silent_when_the_row_exists()
    test_l4a_legs_no_op_without_a_bridge()

    test_gauge_coverage_grants_a_newly_live_session_its_startup_grace()
    test_gauge_coverage_still_fires_once_the_grace_expires()
    test_gauge_coverage_does_not_grant_grace_on_an_unreadable_timestamp()

    test_a_broken_notice_message_surfaces_instead_of_being_swallowed()

    test_ttl_overdue_notifies_the_steward()
    test_ttl_notice_names_both_clocks_and_the_measured_overdue()
    test_ttl_silent_for_a_row_that_never_requested_a_ttl()
    test_ttl_silent_before_the_deadline()
    test_ttl_reads_expires_at_and_not_report_by()
    test_ttl_notifies_once_per_episode()
    test_ttl_latch_does_not_swallow_an_undelivered_notice()
    test_ttl_leg_no_ops_without_a_bridge()

    test_rotation_due_notifies_once_per_episode()
    test_rotation_due_latch_rearms_when_the_session_rotates()
    test_rotation_due_latch_does_not_swallow_an_undelivered_notice()
    test_gauge_coverage_notifies_once_and_releases_on_recovery()
    test_latches_are_independent_per_notice_kind()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
