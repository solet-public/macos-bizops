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
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
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
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    retire_session,
    terminate_session,
)
from agent_messaging_plugin.session_sweep import (  # noqa: E402
    SessionRoleClaimPruner,
    sweep_deadline_dependencies,
    sweep_lane_closed_dependencies,
    sweep_overdue_sessions,
)

T0 = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)

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
    bridge_id = mgr.open(homunculus_name="", parent_pid=1).bridge_id
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
    bridge_id = mgr.open(homunculus_name="", parent_pid=1).bridge_id
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
    bridge_id = mgr.open(homunculus_name="", parent_pid=1).bridge_id
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

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
