#!/usr/bin/env python3
"""RIDER-1 — role-message terminal-clear: the limit-page STARVATION regression + role escalation.

Rev-A's RIDER-1: before this, a capped-unconsumed role IMPORTANT went DORMANT
(consumed=false, emit_count=cap) and NEVER left the ``consumed=false`` owed set —
so ≥cap dormant rows for ONE role filled the oldest LIMIT page of the drain and
STARVED genuinely-owed newer rows behind them (+ the sender got no terminal
signal). Direct rows were immune (escalation flips ``escalated`` → drops them
from the drain). The fix gives role rows the same terminal-clear.

  * **LIMIT-AWARE STARVATION regression (the whole point)** — cap capped-dormant
    role rows (older) fill a limit-``cap`` drain page + a NEWER genuinely-owed
    row sits behind them. RED (pre-fix state, rows not yet escalated): the drain
    returns NOTHING — the oldest page is all capped rows, the Python cap-filter
    drops them, and the newer row (position > limit) is never fetched → STARVED.
    GREEN (post-fix): the reconciler escalates the capped rows → they drop from
    the ``consumed=false AND escalated=false`` query → the drain returns the
    newer owed row. This is only exercisable because ``RealShapeState.query_ordered``
    now HONORS order_by+limit (the load-bearing fidelity fix) — a guard test pins
    that too, so the fake can't silently regress and hide the bug again.
  * **role escalation fires the sender terminal signal** — a capped role row is
    escalated (escalated=true, reason=cap_reached) and a ``post_message`` lands
    on the SENDER's live bridge, exactly once (the silent-fail fix).

Run:
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/role_wake_terminal_clear_smoke.py
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
from ananta.llm.agent_messaging.role_binding import TABLE_ROLE_BINDING  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
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


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()


def _service(state: RealShapeState) -> AgentMessagingService:
    return AgentMessagingService(
        repository=cast(Any, object()),
        state_service=cast(StateManagementInterface, state),
        backend_router=cast(Any, object()),
        flow_manager=cast(Any, object()),
        action_factory=cast(Any, object()),
        compilation_context_builder=cast(Any, object()),
        bridge_delivery=cast(Any, object()),
        config=cast(Any, _EnabledConfig()),
        clock=lambda: T0 + timedelta(hours=1),
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


def _seed_role_row(
    state: RealShapeState,
    *,
    row_id: str,
    message_id: str,
    created_at: datetime,
    emit_count: int,
    escalated: bool = False,
    recipient_key: str = "R",
    sender_instance: str = "agi-S",
    emitted_to: str = "agi-holder",
) -> None:
    state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE).append(
        {
            "id": row_id,
            "external_id": f"role:{recipient_key}:{message_id}",
            "recipient_kind": "role",
            "recipient_key": recipient_key,
            "message_id": message_id,
            "sender_agent_id": "claude_code",
            "sender_agent_instance_id": sender_instance,
            "sender_session_label": "Sender",
            "important": True,
            "delivered": emit_count > 0,
            "consumed": False,
            "escalated": escalated,
            "emit_count": emit_count,
            "emitted_to_agent_instance_id": emitted_to,
            "last_emitted_at": created_at.isoformat() if emit_count else "",
            "content": [{"type": "text", "text": "IMPORTANT: hi"}],
            "created_at": created_at.isoformat(),
            "is_deleted": 0,
        },
    )


def _seed_role_binding(state: RealShapeState, *, role: str, agi: str) -> None:
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


def _reconciler(
    svc: AgentMessagingService,
    mgr: BridgeSessionManager,
    reg: PeerRegistry,
) -> DirectWakeReconciler:
    return DirectWakeReconciler(
        service=svc,
        bridge_manager=mgr,
        peer_registry=reg,
        cap=3,
        re_emit_window_s=300.0,
        clock=lambda: T0 + timedelta(hours=1),
    )


def _is_factual_watcher_escalation(content: str) -> bool:
    """Truthfulness contract without inflating the parent smoke's complexity."""
    return all(
        (
            "watcher delivery acknowledgement" in content,
            "does not prove the recipient failed to see or act" in content,
            "never entered a turn" not in content,
        ),
    )


# ---------------------------------------------------------------------------
# The load-bearing fidelity guard: the fake must HONOR order_by + limit
# ---------------------------------------------------------------------------


def test_fake_query_ordered_honors_order_and_limit() -> None:
    state = RealShapeState()
    for i in range(5):
        _seed_role_row(
            state, row_id=f"arm-{i}", message_id=f"agm-{i}",
            created_at=T0 + timedelta(seconds=i), emit_count=0,
        )
    result = state.query_ordered(
        NAMESPACE,
        {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "filters": {"recipient_key": "R"},
            "order_by": [("created_at", "asc"), ("id", "asc")],
            "limit": 2,
        },
    )
    records = result["data"]["records"]
    _check(
        len(records) == 2 and [r["id"] for r in records] == ["arm-0", "arm-1"],
        "GUARD: RealShapeState.query_ordered HONORS order_by + limit (oldest 2) — "
        "the fidelity the starvation regression depends on",
    )


# ---------------------------------------------------------------------------
# The limit-aware STARVATION regression (red-first: pre-escalation → starved)
# ---------------------------------------------------------------------------


def test_capped_dormant_role_rows_do_not_starve_newer_owed() -> None:
    state = RealShapeState()
    svc = _service(state)
    mgr = _bridge_manager()
    reg = _peer_registry()
    _seed_role_binding(state, role="R", agi="agi-holder")
    # A live SENDER so escalation can resolve its bridge (and land a signal).
    sender_bridge = mgr.open(homunculus_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=sender_bridge, agent_id="claude_code",
            agent_instance_id="agi-S", session_label="Sender", parent_pid=1,
        ),
    )
    # cap (=3) capped-dormant rows, OLDER — they fill a limit-3 drain page.
    for i in range(3):
        _seed_role_row(
            state, row_id=f"arm-capped-{i}", message_id=f"agm-capped-{i}",
            created_at=T0 + timedelta(seconds=i), emit_count=3,
        )
    # One NEWER genuinely-owed row, BEHIND the capped page (emit_count=0). RECENT
    # (5 min before the reconcile clock, well within the cap-equivalent 15-min
    # window) so it is NOT itself time-escalated — it represents a live holder's
    # freshly-owed row that is merely starved behind the capped page.
    _seed_role_row(
        state, row_id="arm-newer", message_id="agm-newer",
        created_at=T0 + timedelta(minutes=55), emit_count=0,
    )

    def _drain() -> list[dict[str, Any]]:
        return svc.list_undelivered_for_instance(
            agent_instance_id="agi-holder", limit=3,
            now=T0 + timedelta(hours=1), re_emit_window_s=300.0, cap=3,
        )

    # RED-FIRST (pre-escalation = the pre-fix DORMANT state): the oldest limit-3
    # page is all capped rows → the cap-filter drops them → the newer owed row is
    # NEVER fetched → STARVED.
    starved = _drain()
    _check(
        starved == [],
        "RED-FIRST: pre-escalation, capped-dormant rows fill the limit page → "
        "newer owed role row is STARVED (drain returns nothing)",
    )
    # The fix: the reconciler escalates the capped rows (terminal-clear).
    escalated = _reconciler(svc, mgr, reg).reconcile()
    _check(escalated == 3, "reconciler escalates the 3 capped-dormant role rows")
    # GREEN: capped rows now escalated=true → drop from the drain query → the
    # newer owed row is returned (no longer starved).
    owed = _drain()
    _check(
        [r.get("message_id") for r in owed] == ["agm-newer"],
        "GREEN: post-escalation, the newer owed role row IS re-emitted (starvation cleared)",
    )


# ---------------------------------------------------------------------------
# Role escalation fires the sender terminal signal (once)
# ---------------------------------------------------------------------------


def test_role_escalation_fires_sender_terminal_signal_once() -> None:
    state = RealShapeState()
    svc = _service(state)
    mgr = _bridge_manager()
    reg = _peer_registry()
    sender_bridge = mgr.open(homunculus_name="", parent_pid=2).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=sender_bridge, agent_id="claude_code",
            agent_instance_id="agi-S", session_label="Sender", parent_pid=2,
        ),
    )
    _seed_role_row(
        state, row_id="arm-cap", message_id="agm-cap",
        created_at=T0, emit_count=3,
        emitted_to="agi-watch-holder",
    )
    reconciler = _reconciler(svc, mgr, reg)
    n = reconciler.reconcile()
    row = next(
        r for r in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
        if r["message_id"] == "agm-cap"
    )
    _check(
        n == 1 and row["escalated"] is True and row["escalation_reason"] == "cap_reached",
        "role escalation: capped role row stamped escalated=true / cap_reached",
    )
    _, events = mgr.get(sender_bridge).events_after(-1)
    _check(
        len(events) == 1
        and events[0].event_type == "post_message"
        and "deaf_wake_escalation" in events[0].content
        and "role R" in events[0].content,
        "role escalation: ONE post_message terminal signal on the sender's bridge "
        "(names 'role R')",
    )
    _check(
        _is_factual_watcher_escalation(events[0].content),
        "role escalation: watcher route names its actual missing ack and makes no "
        "unproved non-turn claim",
    )
    last_cursor = events[0].cursor
    again = reconciler.reconcile()
    _, new_events = mgr.get(sender_bridge).events_after(last_cursor)
    _check(
        again == 0 and new_events == [],
        "role escalation is once-only (an escalated role row is not re-escalated)",
    )


def main() -> None:
    print("=== RIDER-1 role-message terminal-clear (starvation regression) smoke ===")
    test_fake_query_ordered_honors_order_and_limit()
    test_capped_dormant_role_rows_do_not_starve_newer_owed()
    test_role_escalation_fires_sender_terminal_signal_once()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
