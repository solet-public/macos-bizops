#!/usr/bin/env python3
"""REL-05 direct-wake outbox + consumption-gated re-emit + escalation + F2 (no DB).

Drives the SERVICE + the escalation reconciler against the REAL-shape state fake
(``RealShapeState`` — the actual provider ActionResult envelopes, NOT a
convenient stub), so a green here exercises the production state-extraction path.

  * **S1 outbox lifecycle** — an IMPORTANT direct send persists one owed row
    (emit_count=1, consumed=False); model activity AFTER the emission stamps it
    consumed; a consumed row is excluded from the drain. RED-FIRST: activity
    BEFORE the emission does NOT consume (the row stays owed forever).
  * **S2 re-emit window + cap + escalation** — a row inside the window is not
    re-emitted; past the window it is owed; at the cap it stops; the reconciler
    escalates the past-cap row EXACTLY once and notifies the sender's bridge.
  * **S3 loop-prevention** — a silent direct send creates NO outbox row (IMPORTANT
    does); the escalation is a ``post_message`` channel event, and the reconciler
    module cannot reach the peer-send path (structural pin).
  * **F3 displacement** — role-row consumption requires activity from the
    emitted-to instance; a DIFFERENT instance's activity does not consume;
    ``mark_delivered_for_instance`` records the emitted-to instance.
  * **S7 F2 migration grandfather** — the migration flips delivered history to
    consumed so the new drain predicate cannot flood-re-emit it; a post-migration
    unconsumed row is still owed. RED-FIRST: before the migration the delivered
    history IS re-owed.

Run:
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/direct_wake_outbox_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.models import TextPart  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    COL_LAST_EMITTED_AT,
    NAMESPACE,
    TABLE_AGENT_DIRECT_WAKE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.direct_wake_reconcile import (  # noqa: E402
    DirectWakeReconciler,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.role_message_consumed_backfill import (  # noqa: E402
    STATUS_COMPLETED,
    backfill_role_message_consumed,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_ROLE_BINDING_NS = "agent_messaging_plugin"
T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)

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


class _Clock:
    """A controllable monotonic clock for deterministic window/cap tests."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()


def _service(state: RealShapeState, clock: _Clock) -> AgentMessagingService:
    return AgentMessagingService(
        repository=cast(Any, object()),
        state_service=cast(StateManagementInterface, state),
        backend_router=cast(Any, object()),
        flow_manager=cast(Any, object()),
        action_factory=cast(Any, object()),
        compilation_context_builder=cast(Any, object()),
        bridge_delivery=cast(Any, object()),
        config=cast(Any, _EnabledConfig()),
        clock=clock,
    )


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


def _persist_direct(
    svc: AgentMessagingService,
    *,
    message_id: str = "agm-1",
    recipient_instance: str = "agi-R",
    recipient_session: str = "sess-R",
    sender_instance: str = "agi-S",
    sender_bridge_id: str = "agc-s",
) -> None:
    svc.persist_direct_wake(
        message_id=message_id,
        thread_id="agt-1",
        recipient_agent_id="claude_code",
        recipient_agent_instance_id=recipient_instance,
        recipient_agent_session_id=recipient_session,
        sender_agent_id="claude_code",
        sender_agent_instance_id=sender_instance,
        sender_session_label="Sender",
        sender_bridge_id=sender_bridge_id,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
    )


def _seed_role_row(
    state: RealShapeState,
    *,
    row_id: str,
    message_id: str,
    recipient_key: str = "R",
    delivered: bool,
    consumed: bool,
    emit_count: int = 0,
    emitted_to: str = "",
    last_emitted_at: str = "",
    created_at: str,
    escalated: bool = False,
) -> None:
    state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE).append(
        {
            "id": row_id,
            "external_id": f"role:{recipient_key}:{message_id}",
            "recipient_kind": "role",
            "recipient_key": recipient_key,
            "message_id": message_id,
            "sender_agent_id": "claude_code",
            "sender_agent_instance_id": "agi-sender",
            "important": True,
            "delivered": delivered,
            "consumed": consumed,
            "escalated": escalated,
            "emit_count": emit_count,
            "emitted_to_agent_instance_id": emitted_to,
            "last_emitted_at": last_emitted_at,
            "content": [{"type": "text", "text": "IMPORTANT: hi"}],
            "created_at": created_at,
            "is_deleted": 0,
        },
    )


def _seed_role_binding(state: RealShapeState, *, role: str, agi: str) -> None:
    from ananta.llm.agent_messaging.role_binding import TABLE_ROLE_BINDING

    state.rows(_ROLE_BINDING_NS, TABLE_ROLE_BINDING).append(
        {
            "id": f"rbn-{agi}",
            "external_id": f"role:{role}",
            "role": role,
            "holder_kind": "session",
            "agent_instance_id": agi,
            "agent_session_id": f"sess-{agi}",
            "holder_identity": {"agent_id": "claude_code", "session_label": role},
            "claim_epoch": 1,
            "claimed_at": T0.isoformat(),
            "is_deleted": 0,
        },
    )


def _direct_rows(state: RealShapeState) -> list[dict[str, Any]]:
    return [
        r
        for r in state.rows(NAMESPACE, TABLE_AGENT_DIRECT_WAKE)
        if not r.get("is_deleted")
    ]


# ---------------------------------------------------------------------------
# S1 — outbox lifecycle + consumption stamp (with red-first)
# ---------------------------------------------------------------------------


def test_s1_outbox_lifecycle() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist_direct(svc)
    rows = _direct_rows(state)
    _check(
        len(rows) == 1
        and rows[0]["emit_count"] == 1
        and rows[0]["consumed"] is False,
        "S1: IMPORTANT direct send persists one owed row (emit_count=1, consumed=False)",
    )
    clock.advance(301)
    owed = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R", limit=50, now=clock.now,
    )
    _check(len(owed) == 1, "S1: past-window unconsumed row is owed by the drain")
    stamped = svc.reconcile_direct_consumption(
        agent_instance_id="agi-R", activity_at=T0 + timedelta(seconds=5),
    )
    _check(stamped == ["agm-1"], "S1: model activity AFTER emission stamps consumed")
    owed2 = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R", limit=50, now=clock.now,
    )
    _check(owed2 == [], "S1: a consumed row is excluded from the drain")


def test_s1_red_first_activity_before_emission() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist_direct(svc)  # last_emitted_at = T0
    stamped = svc.reconcile_direct_consumption(
        agent_instance_id="agi-R", activity_at=T0 - timedelta(seconds=5),
    )
    clock.advance(301)
    owed = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R", limit=50, now=clock.now,
    )
    _check(
        stamped == [] and len(owed) == 1,
        "S1 red-first: activity BEFORE the emission does NOT consume (row stays owed)",
    )


# ---------------------------------------------------------------------------
# QUIET-GAP — an emission only consumes when it landed on a turn BOUNDARY
# ---------------------------------------------------------------------------


def test_quiet_gap_midturn_arrival_is_not_consumed() -> None:
    """A wake landing mid-turn stays owed (the silent-loss class this closes).

    Reproduces the live 2026-07-26 loss: the wake was queued at T0 while the
    recipient was already working; its next model-initiated call came seconds
    later and — under the old activity-only rule — retired the row on its FIRST
    emit, unseen and never re-emitted.
    """
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist_direct(svc)  # last_emitted_at = T0
    stamped = svc.reconcile_direct_consumption(
        agent_instance_id="agi-R",
        activity_at=T0 + timedelta(seconds=40),
        # The session called the homunculus 6s BEFORE the wake landed → it was mid-turn.
        prev_activity_at=T0 - timedelta(seconds=6),
    )
    clock.advance(301)
    owed = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R", limit=50, now=clock.now,
    )
    _check(
        stamped == [] and len(owed) == 1,
        "QUIET-GAP: a wake arriving MID-TURN is not consumed and stays owed",
    )


def test_quiet_gap_idle_arrival_is_consumed() -> None:
    """Positive control: a wake landing in a real idle gap still consumes."""
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist_direct(svc)  # last_emitted_at = T0
    stamped = svc.reconcile_direct_consumption(
        agent_instance_id="agi-R",
        activity_at=T0 + timedelta(seconds=5),
        # Last call was long before the wake → the session was quiet; this wake
        # is what started the next turn.
        prev_activity_at=T0 - timedelta(seconds=600),
    )
    _check(
        stamped == ["agm-1"],
        "QUIET-GAP: a wake arriving after a genuine idle gap DOES consume",
    )


# ---------------------------------------------------------------------------
# S2 — re-emit window + cap + escalation
# ---------------------------------------------------------------------------


def test_s2_window_cap_escalation() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    sender_bridge = mgr.open(homunculus_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=sender_bridge,
            agent_id="claude_code",
            agent_instance_id="agi-S",
            session_label="Sender",
            parent_pid=1,
        ),
    )
    _persist_direct(svc, sender_bridge_id=sender_bridge)  # emit_count=1, last=T0
    owed_in = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R", limit=50, now=T0, re_emit_window_s=300, cap=3,
    )
    _check(owed_in == [], "S2: within the re-emit window → not re-emitted")
    clock.advance(301)
    owed_past = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R", limit=50, now=clock.now, re_emit_window_s=300, cap=3,
    )
    _check(len(owed_past) == 1, "S2: past the window → owed for re-emit")
    # Two confirmed re-emits take emit_count 1 → 3 (the cap).
    svc.mark_direct_emitted_for_instance(message_id="agm-1", agent_instance_id="agi-R")
    svc.mark_direct_emitted_for_instance(message_id="agm-1", agent_instance_id="agi-R")
    owed_cap = svc.list_owed_direct_for_instance(
        agent_instance_id="agi-R",
        limit=50,
        now=clock.now + timedelta(seconds=1000),
        re_emit_window_s=300,
        cap=3,
    )
    _check(owed_cap == [], "S2: at the cap → no further re-emit")
    reconciler = DirectWakeReconciler(
        service=svc,
        bridge_manager=mgr,
        peer_registry=reg,
        cap=3,
        re_emit_window_s=300,
        clock=lambda: clock.now + timedelta(seconds=1000),
    )
    escalated = reconciler.reconcile()
    row = _direct_rows(state)[0]
    _check(
        escalated == 1
        and row["escalated"] is True
        and row["escalation_reason"] == "cap_reached",
        "S2: reconciler escalates the past-cap row (escalated=True, cap_reached)",
    )
    _, events = mgr.get(sender_bridge).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "post_message"
        and "deaf_wake_escalation" in events[0].content
        and "qualifying model-activity consumption acknowledgement"
        in events[0].content
        and "does not prove the recipient failed to see or act" in events[0].content
        and "never entered a turn" not in events[0].content,
        "S2: sender gets ONE factual MCP escalation (no unproved non-turn claim)",
    )
    last_cursor = events[-1].cursor
    again = reconciler.reconcile()
    _, new_events = mgr.get(sender_bridge).events_after(last_cursor)
    _check(
        again == 0 and new_events == [],
        "S2: escalation is once-only (an escalated row is not re-escalated)",
    )


# ---------------------------------------------------------------------------
# S3 — loop prevention (silent = no row; escalation is post_message, not peer_send)
# ---------------------------------------------------------------------------


class _FakeDispatchService:
    """Records persist_direct_wake calls; a minimal peer_send (dispatch collaborator)."""

    def __init__(self) -> None:
        self.direct_persists = 0

    def peer_send(self, request: Any) -> Any:  # noqa: ANN401 — mirrors the real facade
        class _R:
            thread_id = "agt-x"
            message_id = "agm-x"
            cursor = 0

        return _R()

    def persist_direct_wake(self, **_kwargs: Any) -> None:
        self.direct_persists += 1


def _dispatch(*, text: str) -> _FakeDispatchService:
    from agent_messaging_plugin.peer_dispatch import dispatch_peer_send

    svc = _FakeDispatchService()
    mgr = _bridge_manager()
    reg = _peer_registry()
    recipient_bridge = mgr.open(homunculus_name="", parent_pid=2).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=recipient_bridge,
            agent_id="claude_code",
            agent_instance_id="agi-R",
            session_label="Recipient",
            parent_pid=2,
        ),
    )
    dispatch_peer_send(
        bridge_manager=mgr,
        peer_registry=reg,
        agent_messaging_service=cast(Any, svc),
        sender_bridge_id="agc-s",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-S",
        sender_session_label="Sender",
        sender_parent_pid=1,
        peer_id="claude_code",
        peer_agent_instance_id="agi-R",
        content=[TextPart(type="text", text=text)],
    )
    return svc


def test_s3_silent_creates_no_outbox_row() -> None:
    silent = _dispatch(text="fyi, no marker")
    important = _dispatch(text="IMPORTANT: ping")
    _check(silent.direct_persists == 0, "S3: a SILENT direct send creates NO outbox row")
    _check(
        important.direct_persists == 1,
        "S3: an IMPORTANT direct send creates exactly one outbox row (positive control)",
    )


def test_s3_escalation_cannot_reenter_peer_send() -> None:
    src = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "agent_messaging_plugin"
        / "direct_wake_reconcile.py"
    ).read_text()
    # Structural pin (the S3 analog of GTE-06's "no mutating verb reachable"):
    # the reconciler must NOT import or CALL the peer-send path — it escalates via
    # bridge_manager.append_event (a post_message channel event) only. A prose
    # mention of ``peer_send`` in the loop-prevention docstring is fine; a call /
    # import is not.
    _check(
        "dispatch_peer_send" not in src
        and "peer_dispatch" not in src
        and "append_event" in src
        and "post_message" in src,
        "S3: the escalation reconciler cannot reach the peer-send path (no send→wake→send cycle)",
    )


# ---------------------------------------------------------------------------
# F3 — displacement correctness (role consumption fenced to emitted-to instance)
# ---------------------------------------------------------------------------


def test_f3_consumption_fenced_to_emitted_to_instance() -> None:
    state = RealShapeState()
    svc = _service(state, _Clock(T0))
    _seed_role_row(
        state,
        row_id="arm-1",
        message_id="agm-r1",
        delivered=True,
        consumed=False,
        emit_count=1,
        emitted_to="agi-A",
        last_emitted_at=T0.isoformat(),
        created_at=T0.isoformat(),
    )
    other = svc.reconcile_role_consumption(
        agent_instance_id="agi-B", activity_at=T0 + timedelta(seconds=5),
    )
    _check(
        other == [],
        "F3: activity from a DIFFERENT instance does NOT consume a role row emitted elsewhere",
    )
    same = svc.reconcile_role_consumption(
        agent_instance_id="agi-A", activity_at=T0 + timedelta(seconds=5),
    )
    _check(
        same == ["role:R:agm-r1"],
        "F3: activity from the emitted-to instance DOES consume the role row",
    )


def test_f3_mark_delivered_records_emitted_to() -> None:
    state = RealShapeState()
    svc = _service(state, _Clock(T0))
    _seed_role_binding(state, role="R", agi="agi-A")
    _seed_role_row(
        state,
        row_id="arm-2",
        message_id="agm-r2",
        delivered=False,
        consumed=False,
        emit_count=0,
        created_at=T0.isoformat(),
    )
    flagged = svc.mark_delivered_for_instance(
        external_id="role:R:agm-r2", recipient_key="R", agent_instance_id="agi-A",
    )
    row = next(
        r for r in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
        if r["message_id"] == "agm-r2"
    )
    _check(
        flagged is True
        and row["emitted_to_agent_instance_id"] == "agi-A"
        and row["emit_count"] == 1
        and row[COL_LAST_EMITTED_AT] == T0.isoformat()
        and row["delivered"] is True,
        "F3: mark_delivered_for_instance records emitted_to + emit_count + "
        "last_emitted_at + delivered",
    )
    denied = svc.mark_delivered_for_instance(
        external_id="role:R:agm-r2", recipient_key="R", agent_instance_id="agi-Z",
    )
    _check(
        denied is False,
        "F3: a non-holder instance cannot confirm (ownership fence rejects)",
    )


# ---------------------------------------------------------------------------
# S7 — F2 migration grandfather (with red-first)
# ---------------------------------------------------------------------------


def test_s7_f2_migration_grandfather() -> None:
    state = RealShapeState()
    svc = _service(state, _Clock(T0))
    _seed_role_row(
        state,
        row_id="arm-old",
        message_id="agm-old",
        delivered=True,
        consumed=False,
        emit_count=0,
        created_at=(T0 - timedelta(days=1)).isoformat(),
    )
    # RED-FIRST: BEFORE the migration, delivered history is re-owed by the new
    # consumed predicate — the exact flood the grandfather prevents.
    owed_before = svc.list_undelivered_for(
        recipient_kind="role", recipient_key="R", limit=50,
    )
    _check(
        len(owed_before) == 1,
        "S7 red-first: pre-migration delivered history IS re-owed (would flood)",
    )
    result = backfill_role_message_consumed(state)
    old = next(
        r for r in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
        if r["message_id"] == "agm-old"
    )
    _check(
        result["status"] == STATUS_COMPLETED
        and result["updated"] == ["arm-old"]
        and old["consumed"] is True
        and old["emit_count"] == 1
        and bool(old["consumed_at"]),
        "S7: migration grandfathers delivered history (consumed=True, emit_count=1)",
    )
    owed_after = svc.list_undelivered_for(
        recipient_kind="role", recipient_key="R", limit=50,
    )
    _check(owed_after == [], "S7: migration → delivered history NOT re-owed")
    _seed_role_row(
        state,
        row_id="arm-new",
        message_id="agm-new",
        delivered=False,
        consumed=False,
        emit_count=0,
        created_at=T0.isoformat(),
    )
    owed_new = svc.list_undelivered_for(
        recipient_kind="role", recipient_key="R", limit=50,
    )
    _check(
        len(owed_new) == 1 and owed_new[0]["message_id"] == "agm-new",
        "S7: a post-migration unconsumed row IS still owed (positive control)",
    )
    # Idempotent second run is a no-op (durable marker).
    again = backfill_role_message_consumed(state)
    _check(again["updated"] == [], "S7: the migration is one-shot (marker-gated no-op re-run)")


def main() -> None:
    print("=== REL-05 direct-wake outbox / re-emit / escalation / F2 smoke ===")
    test_s1_outbox_lifecycle()
    test_s1_red_first_activity_before_emission()
    test_quiet_gap_midturn_arrival_is_not_consumed()
    test_quiet_gap_idle_arrival_is_consumed()
    test_s2_window_cap_escalation()
    test_s3_silent_creates_no_outbox_row()
    test_s3_escalation_cannot_reenter_peer_send()
    test_f3_consumption_fenced_to_emitted_to_instance()
    test_f3_mark_delivered_records_emitted_to()
    test_s7_f2_migration_grandfather()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
