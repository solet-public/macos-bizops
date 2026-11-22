#!/usr/bin/env python3
"""Unit smoke for the D1 platform sweep (``session_sweep.py``) — the
``on_tick`` rider that fires+delivers armed
``deadline`` dependency edges, and prunes stale ``session_role_claim`` rows
(Architect ratification #3). Also covers the ``retire_session`` crash-mid-
retire redrive leg (Coordinator-Dawn's explicit fold-in — both touch the
same states, so one file measures both).

``sweep_deadline_dependencies`` is a pure function against
``RealShapeState``, with a controlled clock (no real time in a sweep test). The dependency-delivery + pruner legs use REAL
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
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import CapEnforcingState, RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding, BridgeSessionState  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_DEADLINE,
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    PEER_BINDING_NAMESPACE,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_ROLE_CLAIM,
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
    SessionRoleClaimPruner,
    last_report_alive,
    sweep_deadline_dependencies,
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
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _n: "ags-http",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )


def _bridge(manager: BridgeSessionManager, bridge_id: str) -> BridgeSessionState:
    """The open bridge session a test registered; a missing one is a test setup fault."""
    bridge = manager.get(bridge_id)
    if bridge is None:
        raise AssertionError(f"bridge {bridge_id} is not open on the manager")
    return bridge


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
            agent_instance_id=agent_instance_id,
            label="lane-x",
            host="operator",
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
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        if lifecycle_state == LIFECYCLE_IDLE:
            transition_lifecycle_state(
                state,
                agent_instance_id=agent_instance_id,
                from_state=LIFECYCLE_LIVE,
                to_state=LIFECYCLE_IDLE,
                directed_by="operator:none",
            )


def _register_live_binding(
    reg: PeerRegistry,
    mgr: BridgeSessionManager,
    *,
    agent_instance_id: str,
    agent_session_id: str = "",
) -> str:
    """Same pattern ``test_deadline_dependency_fires_and_delivers`` uses —
    a real bridge + a real peer registry binding, no server, so the
    resolve-then-append delivery path is exercised for real, not stubbed.

    ``agent_session_id`` is what a real registration carries (the launcher's
    exported ``$AGENT_SESSION_ID``); it defaults to empty so every pre-GAU-26
    caller keeps the binding shape it was written against."""
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id=agent_instance_id,
            session_label=agent_instance_id,
            parent_pid=1,
            agent_session_id=agent_session_id,
        ),
    )
    return bridge_id


# ---------------------------------------------------------------------------
# The sweep tick keeps no clock on a session's own report
# ---------------------------------------------------------------------------

# Event types the removed overdue legs used to append to a spawner's bridge.
# Named as strings because the constants that carried them no longer exist.
_REMOVED_CLOCK_NOTICE_TYPES = frozenset(
    {
        "session_overdue_notice",
        "session_overdue_quiet_notice",
        "session_registration_overdue_notice",
        "session_spawn_orphaned_notice",
        "session_spawn_unregistered_notice",
    },
)


def test_sweep_tick_never_marks_a_live_session_overdue_or_tells_its_spawner() -> None:
    """Nothing on a clock checks on a session or tells another session about
    its silence. Drives the REAL ``_run_session_lifecycle_sweep`` with a spawner
    whose bridge is live, a live row whose ``report_by`` passed an hour ago, and
    a ``spawning`` row that has not registered for an hour: the live row stays
    ``live``, the spawning row stays ``spawning`` and unmarked, and the
    spawner's bridge receives no overdue, registration-overdue or spawn notice."""

    import agent_messaging_plugin.plugin as plugin_mod

    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-clockless-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-clockless-steward"}},
        {"agent_id": "claude_code"},
    )
    steward_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-clockless-steward")
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    _spawn_live(
        state,
        agent_instance_id="agi-clockless-worker",
        lifecycle_state=LIFECYCLE_LIVE,
        report_by_override=an_hour_ago.isoformat(),
        spawned_by_instance_id="agi-clockless-steward",
    )
    _spawn_unregistered(
        state,
        agent_instance_id="agi-clockless-unregistered",
        spawned_at=an_hour_ago,
        spawned_by_instance_id="agi-clockless-steward",
    )
    fake_self = SimpleNamespace(
        _get_state_service=lambda: state,
        _peer_registry=reg,
        _bridge_manager=mgr,
        _session_role_claim_pruner=None,
    )
    with (
        patch.object(plugin_mod, "_run_session_claude_mapping_riders"),
    ):
        plugin_mod.AgentMessagingPlugin._run_session_lifecycle_sweep(cast("Any", fake_self))  # noqa: SLF001
    _check(
        read_managed_session(state, "agi-clockless-worker")["lifecycle_state"] == LIFECYCLE_LIVE,
        "a live row whose report_by passed stays live after the sweep tick (the clock does not mark it overdue)",
    )
    unregistered = read_managed_session(state, "agi-clockless-unregistered")
    _check(
        unregistered["lifecycle_state"] == LIFECYCLE_SPAWNING and not unregistered.get("registration_overdue_at"),
        "a spawning row that has not registered stays spawning and carries no registration_overdue_at",
    )
    _, events = _bridge(mgr, steward_bridge_id).events_after(-1)
    sent = sorted(event.event_type for event in events if event.event_type in _REMOVED_CLOCK_NOTICE_TYPES)
    _check(
        sent == [],
        f"the spawner's bridge receives no overdue, registration-overdue or spawn notice from a timer (got {sent!r})",
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
        state,
        row_id="sdp-future",
        condition_kind=CONDITION_DEADLINE,
        condition_ref=future,
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
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id="agi-waiter",
            session_label="Waiter",
            parent_pid=1,
        ),
    )
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state,
        row_id="sdp-1",
        condition_kind=CONDITION_DEADLINE,
        condition_ref=past,
        waiter_instance_id="agi-waiter",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "a past-deadline armed edge is fired exactly once")
    queried = cast(
        "dict[str, Any]",
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_SESSION_DEPENDENCY, "filters": {"id": "sdp-1"}},
        ),
    )
    rows = queried["data"]["records"]
    _check(rows[0]["fired_at"] is not None, "fired_at is stamped on the edge")
    _, events = _bridge(mgr, bridge_id).events_after(-1)
    _check(
        len(events) == 1 and events[0].event_type == "session_dependency_wake" and "deadline" in events[0].content,
        f"the waiter's bridge gets exactly one delivered wake event (got {events!r})",
    )
    again = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _, events2 = _bridge(mgr, bridge_id).events_after(events[-1].cursor)
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
    three condition kinds. Matches the observed live identity shape
    (``agi-watch-...``)."""
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # The waiter is registered in the peer registry directly -- no
    # _spawn_live() call for it at all, so it has NO managed_session row.
    bridge_id = _register_live_binding(
        reg,
        mgr,
        agent_instance_id="agi-watch-92a6ae0e3e134e5e11774007",
    )
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state,
        row_id="sdp-unmanaged-waiter",
        condition_kind=CONDITION_DEADLINE,
        condition_ref=past,
        waiter_instance_id="agi-watch-92a6ae0e3e134e5e11774007",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(fired == 1, "the deadline edge fires")
    _, events = _bridge(mgr, bridge_id).events_after(-1)
    _check(
        len(events) == 1 and events[0].event_type == "session_dependency_wake" and "deadline" in events[0].content,
        f"RED-vs-GREEN: an UNMANAGED (watch-registered) waiter still gets the wake delivered (got {events!r}) -- before this fix, resolution went only through the waiter's managed_session row and silently found nothing to notify",
    )


def test_deadline_dependency_unresolvable_waiter_is_best_effort() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    # No managed_session row at all for this waiter — agent_id is unknowable.
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state,
        row_id="sdp-2",
        condition_kind=CONDITION_DEADLINE,
        condition_ref=past,
        waiter_instance_id="agi-ghost",
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 1,
        "the edge still fires (state) even though delivery cannot be resolved",
    )


def test_deadline_dependency_without_a_waiter_is_logged_noop() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    past = (T0 - timedelta(seconds=5)).isoformat()
    _seed_dependency(
        state,
        row_id="sdp-3",
        condition_kind=CONDITION_DEADLINE,
        condition_ref=past,
    )
    fired = sweep_deadline_dependencies(state, peer_registry=reg, bridge_manager=mgr, now=T0)
    _check(
        fired == 1,
        "an edge with no waiter_instance_id still fires; delivery is a no-op, not a crash",
    )


# ---------------------------------------------------------------------------
# SessionRoleClaimPruner
# ---------------------------------------------------------------------------


def _seed_claim(
    state: StateManagementInterface,
    *,
    agent_session_id: str,
    held_role: str,
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
    return [r for r in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_SESSION_ROLE_CLAIM) if not r.get("is_deleted")]


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
        "a claim whose managed_session is non-terminal is NEVER pruned (ledger-authoritative alive), regardless of elapsed time",
    )


def test_pruner_live_registered_session_never_pruned() -> None:
    state = _state()
    reg = _peer_registry()
    mgr = _bridge_manager()
    bridge_id = mgr.open(solet_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id="agi-reg",
            session_label="Reg",
            parent_pid=1,
            agent_session_id="sess-reg",
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
        "genuine absence (no managed_session, no live binding) within the grace window is NOT pruned — the blue-green-bounce guard",
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


def test_pruner_pages_claims_without_a_target_query_state_read() -> None:
    """More than two provider pages preserve prune and grace-map semantics."""

    class _NoClaimQueryState(CapEnforcingState):
        def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
            if query.get("table") == TABLE_SESSION_ROLE_CLAIM:
                raise AssertionError("D1 must page session_role_claim, never query_state it")
            return super().query_state(namespace, query)

    real_inner = RealShapeState()
    inner = cast("StateManagementInterface", real_inner)
    state = cast("StateManagementInterface", _NoClaimQueryState(real_inner))
    for index in range(205):
        session_id = f"sess-terminal-{index:03d}"
        _seed_claim(state, agent_session_id=session_id, held_role="Some-Lane")
        inner.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": "managed_session",
                "record": {
                    "agent_session_id": session_id,
                    "agent_instance_id": f"agi-{session_id}",
                    "lifecycle_state": LIFECYCLE_TERMINATED,
                },
            },
        )
    _seed_claim(state, agent_session_id="sess-absent", held_role="Some-Lane")
    _spawn_live(inner, agent_instance_id="agi-live-page")
    inner.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-live-page"}},
        {"agent_session_id": "sess-live-page"},
    )
    _seed_claim(state, agent_session_id="sess-live-page", held_role="Some-Lane")

    pruner = SessionRoleClaimPruner(grace_window_s=300, clock=lambda: T0)
    first_pruned = pruner.sweep(state, peer_registry=_peer_registry())
    _check(
        first_pruned == 205 and {row["agent_session_id"] for row in _claim_rows(state)} == {"sess-absent", "sess-live-page"},
        "D1 pages >200 claims: terminal rows prune while absent-grace and live rows survive",
    )
    _check(
        "sess-absent" in pruner._first_absent_at,
        "the absent row is tracked for grace even when terminal rows span pages",
    )
    inner.delete_records(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_ROLE_CLAIM,
            "filters": {"agent_session_id": "sess-absent"},
            "soft_delete": False,
        },
    )
    pruner.sweep(state, peer_registry=_peer_registry())
    _check(
        "sess-absent" not in pruner._first_absent_at and {row["agent_session_id"] for row in _claim_rows(state)} == {"sess-live-page"},
        "grace-map cleanup forgets an absent claim that vanishes across paged sweeps",
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
        state,
        row_id="sdp-crash",
        condition_kind=CONDITION_SESSION_TERMINAL,
        condition_ref="agi-crash",
        waiter_instance_id="agi-waiter-crash",
    )
    _check(
        read_managed_session(state, "agi-crash")["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "setup: the row is 'terminated' but NOT yet 'retired' (simulating the crash point)",
    )
    result = retire_session(state, agent_instance_id="agi-crash", directed_by="operator:none")
    _check(
        result["already_retired"] is False and result["dependencies_fired"] == 1 and result["host_action"] == "not_attempted",
        f"re-running retire_session finishes the job: fires the pending edge and completes the transition without re-attempting the host (got {result!r})",
    )
    _check(
        read_managed_session(state, "agi-crash")["lifecycle_state"] == LIFECYCLE_RETIRED,
        "the row reaches 'retired' despite the simulated mid-retire crash",
    )


# ---------------------------------------------------------------------------
# Registration-overdue marks on existing rows
# ---------------------------------------------------------------------------


def _spawn_unregistered(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    spawned_at: datetime,
    spawned_by_instance_id: str = "",
) -> None:
    """A ``spawning`` row whose spawn timestamp we CONTROL.
    ``last_transition_at`` is the anchor a registration bound was measured from."""
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            label="lane-z",
            host="headless",
            spawned_by_instance_id=spawned_by_instance_id,
        ),
    )
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {"last_transition_at": spawned_at.isoformat()},
    )


def test_registration_late_registration_clears_the_mark() -> None:
    """No timer writes ``registration_overdue_at`` any more, but existing rows
    carry it. A worker that registers LATE is a different story from one that
    never did, so registration still clears the mark rather than leaving the
    row permanently flagged."""
    state = _state()
    _spawn_unregistered(state, agent_instance_id="agi-late", spawned_at=T0)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-late"}},
        {"registration_overdue_at": T0.isoformat(), "registration_overdue_reason": "marked before the sweep was removed"},
    )
    _check(
        bool(read_managed_session(state, "agi-late").get("registration_overdue_at")),
        "setup: the existing row carries a registration-overdue mark",
    )
    backfill_registration(
        state,
        agent_instance_id="agi-late",
        agent_id="claude_code",
        agent_session_id="ases-agi-late",
    )
    row = read_managed_session(state, "agi-late")
    _check(not row.get("registration_overdue_at"), "a late registration clears the mark")
    _check(row["lifecycle_state"] == LIFECYCLE_LIVE, "and the row completes spawning->live")


# ---------------------------------------------------------------------------
# The sweep tick tells no session about its context gauge.
# ---------------------------------------------------------------------------


def _gauge(state: StateManagementInterface, agent_instance_id: str, **over: object) -> None:
    """Write a gauge row the way report_context_status would."""
    kwargs: dict[str, object] = {
        "agent_instance_id": agent_instance_id,
        "claude_session_id": "s1",
        "model": "claude-sonnet-5",
        "current_tokens": 900_000,
        "ceiling": 1_000_000,
        "measured_at": T0.isoformat(),
        "cache_cold": False,
        "reporter_surface": "checkout",
        "reporter_generation": 2,
    }
    kwargs.update(over)
    upsert_session_context_status(state, **kwargs)  # type: ignore[arg-type]


def _wired(session: str = "") -> tuple[StateManagementInterface, PeerRegistry, BridgeSessionManager, str]:
    state, reg, mgr = _state(), _peer_registry(), _bridge_manager()
    _spawn_live(state, agent_instance_id="agi-steward")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-steward"}},
        {"agent_id": "claude_code"},
    )
    bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-steward", agent_session_id=session)
    _spawn_live(state, agent_instance_id="agi-worker", spawned_by_instance_id="agi-steward")
    return state, reg, mgr, bridge_id


def _ticking(
    state: StateManagementInterface,
    agent_instance_id: str,
    *,
    last_alive: datetime,
) -> None:
    """Arm the lifecycle row so the §3.3 identity derives to ``last_alive``.

    Writes the PAIR, never `report_by` alone: the derivation is
    ``report_by - report_by_seconds``, so a test that set only one of them
    would be pinning a value it did not choose.

    ALSO backdates ``last_transition_at`` to a day before ``last_alive``:
    ``_spawn_live`` stamps its OWN transition at real wall-clock "now", and a
    session that is TICKING has, by construction, been alive for a while.
    """
    window_s = 300
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": agent_instance_id}},
        {
            "report_by_seconds": window_s,
            "report_by": (last_alive + timedelta(seconds=window_s)).isoformat(),
            "report_by_source": "explicit_self_report",
            "last_transition_at": (last_alive - timedelta(days=1)).isoformat(),
        },
    )


def test_last_report_alive_derives_the_tick_moment() -> None:
    """The identity ``fleet_status`` reads, pinned on its
    own: report_by minus report_by_seconds IS the last report_alive."""
    moment = datetime(2026, 8, 18, 12, 0, 0, tzinfo=UTC)
    row = {
        "report_by": (moment + timedelta(seconds=300)).isoformat(),
        "report_by_seconds": 300,
        "report_by_source": "explicit_self_report",
    }
    _check(last_report_alive(row) == moment, "the derived tick moment is exact")
    for source in ("confirmed_drive", "observed_spawning", ""):
        _check(
            last_report_alive({**row, "report_by_source": source}) is None,
            f"report_by provenance {source!r} is not misidentified as report_alive",
        )
    _check(
        last_report_alive({"report_by": moment.isoformat(), "report_by_seconds": 0}) is None,
        "a zero window is NO EVIDENCE (None), never a datetime — absence of the WINDOW is not evidence of absence of a TICK",
    )


def test_the_sweep_tick_tells_no_session_about_its_own_gauge() -> None:
    """The sweep tick appends no context-band notice to anyone's inbox or bridge.

    Four live sessions: a worker past its rotation threshold with its own live
    binding, a sibling with no gauge row past the startup grace, one whose gauge
    row stopped while its report_alive kept ticking, and their spawner. The
    removed legs used to append a rotation_due_notice, a gauge_coverage_notice or
    a gauge_stale_notice to the spawner's bridge, and a rotation_self_notice
    (durable inbox row plus live bridge event) to the measured session itself,
    on every new episode. Driving the REAL ``_on_sweep_tick``, none may appear.
    A session reads its own gauge when it chooses, through
    ``session_context_status``.
    """
    import agent_messaging_plugin.plugin as plugin_mod

    removed_types = {
        "rotation_due_notice",
        "gauge_coverage_notice",
        "gauge_stale_notice",
        "rotation_self_notice",
    }
    state, reg, mgr, steward_bridge_id = _wired()
    worker_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-worker")
    now = datetime.now(UTC)
    _gauge(state, "agi-worker", measured_at=now.isoformat())  # past its rotation threshold
    _spawn_live(state, agent_instance_id="agi-dark", spawned_by_instance_id="agi-steward")
    dark_bridge_id = _register_live_binding(reg, mgr, agent_instance_id="agi-dark")
    state.update_state(  # live for an hour, no gauge row ever written
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-dark"}},
        {"last_transition_at": (now - timedelta(hours=1)).isoformat()},
    )
    _spawn_live(state, agent_instance_id="agi-frozen", spawned_by_instance_id="agi-steward")
    _gauge(state, "agi-frozen", current_tokens=1_000, measured_at=(now - timedelta(seconds=5400)).isoformat())
    _ticking(state, "agi-frozen", last_alive=now - timedelta(seconds=30))

    persisted: list[object] = []

    def _record_peer_send(request: object) -> object:
        persisted.append(request)
        return SimpleNamespace(thread_id="agt-recorded")

    plugin = plugin_mod.AgentMessagingPlugin()
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]  # noqa: SLF001
    plugin._peer_registry = reg  # noqa: SLF001
    plugin._bridge_manager = mgr  # noqa: SLF001
    with patch.object(plugin, "_require_service", return_value=SimpleNamespace(peer_send=_record_peer_send)):
        plugin._on_sweep_tick()  # noqa: SLF001
    _check(not persisted, f"no durable notice is written to any session's inbox (saw {len(persisted)})")
    for label, bridge_id in (
        ("the measured worker", worker_bridge_id),
        ("the dark sibling", dark_bridge_id),
        ("the spawner", steward_bridge_id),
    ):
        _, events = _bridge(mgr, bridge_id).events_after(-1)
        _check(
            not removed_types & {event.event_type for event in events},
            f"no band, coverage or staleness notice reaches {label}'s bridge (saw {[e.event_type for e in events]})",
        )


def main() -> int:
    test_deadline_dependency_not_yet_due_skipped()
    test_deadline_dependency_fires_and_delivers()
    test_deadline_dependency_unmanaged_waiter_still_delivers()
    test_deadline_dependency_unresolvable_waiter_is_best_effort()
    test_deadline_dependency_without_a_waiter_is_logged_noop()
    test_pruner_terminal_managed_session_pruned_immediately()
    test_pruner_live_managed_session_never_pruned()
    test_pruner_live_registered_session_never_pruned()
    test_pruner_absence_within_grace_window_not_pruned()
    test_pruner_absence_past_grace_window_pruned()
    test_pruner_pages_claims_without_a_target_query_state_read()
    test_retire_session_crash_mid_retire_is_redrivable()
    test_sweep_tick_never_marks_a_live_session_overdue_or_tells_its_spawner()
    test_registration_late_registration_clears_the_mark()

    test_last_report_alive_derives_the_tick_moment()
    test_the_sweep_tick_tells_no_session_about_its_own_gauge()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
