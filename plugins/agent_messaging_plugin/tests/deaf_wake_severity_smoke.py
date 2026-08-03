#!/usr/bin/env python3
"""Deaf-wake Guard-1 severity fix — heartbeat gates the NOTIFICATION, not the row.

Architect-ruled shape (workbench/2026-08-01_deaf_wake_alarm_findings_claude_b.md,
"ANSWERED FROM THE ROWS" §6): ``cap_reached`` is measured by Guard 1 — a LATER
model-initiated platform call from the recipient. Local-only tool work never
stamps that signal, so a session at maximum effort looks identical to a dead
one. The registry binding, refreshed by an always-on ~200s heartbeat
independent of model activity, is the one signal that CAN see the difference.
Heartbeat-as-CONSUMPTION was explicitly REJECTED (it would silently retire
rows never confirmed read); what survives is heartbeat gating the
NOTIFICATION's severity only.

The trap this suite is built to avoid (per its own design review): a test that
only asserts "no alarm fired" cannot tell a working severity gate from Guard 1
having short-circuited for an unrelated reason — two independent guards, and a
green on one proves nothing about the other. Every test here instead asserts
BOTH which severity fired (info vs alarm, by an explicit prose tag) AND that
the row's terminal state (escalated=True, escalation_reason) is IDENTICAL
regardless of severity — proving the heartbeat check gates the notification's
tone only, never the drain/escalation logic RIDER-1 depends on.

  * **INFO when the recipient is alive by heartbeat** — a capped role row whose
    recipient has a live registry binding (heartbeat, no model activity
    required) gets an ``info``-tagged notice; the row still escalates
    (terminal, dropped from the owed drain) exactly as an alarm-severity row
    would.
  * **ALARM when the recipient is NOT registered** — same capped row, same
    reason, no registry binding for the recipient's session id: the
    notification is alarm-tagged. Row's terminal state is byte-identical to
    the INFO case above — the negative control that proves severity is
    notification-only.
  * **``recipient_gone`` stays alarm-class even if the recipient is somehow
    registered** — the ruling is unconditional for this reason; heartbeat
    liveness must not be consulted at all when the reason isn't ``cap_reached``.

Run:
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/deaf_wake_severity_smoke.py
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

T0 = datetime(2026, 8, 2, 12, 0, 0, tzinfo=UTC)

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


def _service(state: RealShapeState, *, now: datetime) -> AgentMessagingService:
    return AgentMessagingService(
        repository=cast(Any, object()),
        state_service=cast(StateManagementInterface, state),
        backend_router=cast(Any, object()),
        flow_manager=cast(Any, object()),
        action_factory=cast(Any, object()),
        compilation_context_builder=cast(Any, object()),
        bridge_delivery=cast(Any, object()),
        config=cast(Any, _EnabledConfig()),
        clock=lambda: now,
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


def _register_sender(mgr: BridgeSessionManager, reg: PeerRegistry) -> str:
    bridge_id = mgr.open(homunculus_name="", parent_pid=1).bridge_id
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id, agent_id="claude_code",
            agent_instance_id="agi-S", session_label="Sender", parent_pid=1,
        ),
    )
    return bridge_id


def _seed_role_row(
    state: RealShapeState,
    *,
    row_id: str,
    message_id: str,
    created_at: datetime,
    emit_count: int,
    emitted_to: str = "agi-holder",
    emitted_to_session: str = "",
    recipient_key: str = "R",
) -> None:
    state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE).append(
        {
            "id": row_id,
            "external_id": f"role:{recipient_key}:{message_id}",
            "recipient_kind": "role",
            "recipient_key": recipient_key,
            "message_id": message_id,
            "sender_agent_id": "claude_code",
            "sender_agent_instance_id": "agi-S",
            "sender_session_label": "Sender",
            "important": True,
            "delivered": emit_count > 0,
            "consumed": False,
            "escalated": False,
            "emit_count": emit_count,
            "emitted_to_agent_instance_id": emitted_to,
            "emitted_to_agent_session_id": emitted_to_session,
            "last_emitted_at": created_at.isoformat() if emit_count else "",
            "content": [{"type": "text", "text": "IMPORTANT: hi"}],
            "created_at": created_at.isoformat(),
            "is_deleted": 0,
        },
    )


def _role_row(state: RealShapeState, message_id: str) -> dict[str, Any]:
    return next(
        r for r in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
        if r["message_id"] == message_id
    )


# ---------------------------------------------------------------------------
# INFO when the recipient is alive by heartbeat (registered, no model call)
# ---------------------------------------------------------------------------


def test_cap_reached_is_info_when_recipient_alive_by_heartbeat() -> None:
    state = RealShapeState()
    now = T0 + timedelta(hours=1)
    svc = _service(state, now=now)
    mgr = _bridge_manager()
    reg = _peer_registry()
    sender_bridge = _register_sender(mgr, reg)
    # The recipient has a LIVE registry binding — simulating an always-on
    # heartbeat from a session doing local-only work (no bridge call, so
    # Guard 1 in _stamp_consumed_rows never advanced; irrelevant here since
    # this suite never calls reconcile_role_consumption at all — the row is
    # capped and escalated on emit_count alone, exactly as production does).
    reg.register(
        BridgeBinding(
            bridge_id="agc-holder", agent_id="claude_code",
            agent_instance_id="agi-holder", session_label="Holder",
            parent_pid=2, agent_session_id="sess-holder",
        ),
    )
    _seed_role_row(
        state, row_id="arm-info", message_id="agm-info",
        created_at=T0, emit_count=3, emitted_to="agi-holder",
        emitted_to_session="sess-holder",
    )
    reconciler = DirectWakeReconciler(
        service=svc, bridge_manager=mgr, peer_registry=reg,
        cap=3, re_emit_window_s=300.0, clock=lambda: now,
    )
    n = reconciler.reconcile()
    row = _role_row(state, "agm-info")
    _check(
        n == 1 and row["escalated"] is True and row["escalation_reason"] == "cap_reached",
        "INFO case: row still escalates terminal (cap_reached) — heartbeat "
        "does not touch the drop",
    )
    _, events = mgr.get(sender_bridge).events_after(-1)  # type: ignore[union-attr]
    content = events[0].content
    _check(
        "severity=info" in content and "severity=alarm" not in content,
        "INFO case: notification is tagged severity=info, not alarm",
    )
    _check(
        "no model-initiated platform call from this session since emission"
        in content,
        "INFO case: prose names exactly what Guard 1 measures (H2 narrowing)",
    )
    _check(
        "no recorded consumption acknowledgement" not in content,
        "INFO case: the narrowed MCP-route claim never uses the overclaiming "
        "'no consumption acknowledgement' phrasing",
    )


# ---------------------------------------------------------------------------
# ALARM when the recipient is NOT registered — the negative control
# ---------------------------------------------------------------------------


def test_cap_reached_is_alarm_when_recipient_not_registered() -> None:
    state = RealShapeState()
    now = T0 + timedelta(hours=1)
    svc = _service(state, now=now)
    mgr = _bridge_manager()
    reg = _peer_registry()
    sender_bridge = _register_sender(mgr, reg)
    # No registration for the recipient at all — heartbeat is gone.
    _seed_role_row(
        state, row_id="arm-alarm", message_id="agm-alarm",
        created_at=T0, emit_count=3, emitted_to="agi-holder",
        emitted_to_session="sess-holder-gone",
    )
    reconciler = DirectWakeReconciler(
        service=svc, bridge_manager=mgr, peer_registry=reg,
        cap=3, re_emit_window_s=300.0, clock=lambda: now,
    )
    n = reconciler.reconcile()
    row = _role_row(state, "agm-alarm")
    _check(
        n == 1 and row["escalated"] is True and row["escalation_reason"] == "cap_reached",
        "ALARM case: row's terminal state is BYTE-IDENTICAL to the INFO case "
        "above (same reason, same escalated flag) — the negative control "
        "proving severity is notification-only",
    )
    _, events = mgr.get(sender_bridge).events_after(-1)  # type: ignore[union-attr]
    content = events[0].content
    _check(
        "severity=alarm" in content and "severity=info" not in content,
        "ALARM case: notification is tagged severity=alarm, not info",
    )


# ---------------------------------------------------------------------------
# recipient_gone stays alarm-class UNCONDITIONALLY — heartbeat never consulted
# ---------------------------------------------------------------------------


def test_recipient_gone_is_always_alarm_even_if_registered() -> None:
    state = RealShapeState()
    now = T0 + timedelta(minutes=20)  # > cap(3) * window(300s) = 900s = 15min
    svc = _service(state, now=now)
    mgr = _bridge_manager()
    reg = _peer_registry()
    sender_bridge = _register_sender(mgr, reg)
    # The recipient IS registered/alive — if severity consulted heartbeat for
    # this reason, it would wrongly downgrade to info. It must not.
    reg.register(
        BridgeBinding(
            bridge_id="agc-holder2", agent_id="claude_code",
            agent_instance_id="agi-holder2", session_label="Holder2",
            parent_pid=3, agent_session_id="sess-holder2",
        ),
    )
    _seed_role_row(
        state, row_id="arm-gone", message_id="agm-gone",
        created_at=T0, emit_count=0, emitted_to="agi-holder2",
        emitted_to_session="sess-holder2",
    )
    reconciler = DirectWakeReconciler(
        service=svc, bridge_manager=mgr, peer_registry=reg,
        cap=3, re_emit_window_s=300.0, clock=lambda: now,
    )
    n = reconciler.reconcile()
    row = _role_row(state, "agm-gone")
    _check(
        n == 1 and row["escalated"] is True and row["escalation_reason"] == "recipient_gone",
        "recipient_gone: row escalates with reason=recipient_gone (time-based, "
        "not emit_count-based)",
    )
    _, events = mgr.get(sender_bridge).events_after(-1)  # type: ignore[union-attr]
    content = events[0].content
    _check(
        "severity=alarm" in content and "severity=info" not in content,
        "recipient_gone: ALWAYS alarm-class, even though the recipient has a "
        "live registry binding — heartbeat liveness must not be consulted "
        "for this reason at all",
    )


def main() -> None:
    print("=== Deaf-wake Guard-1 severity fix (heartbeat gates notification) smoke ===")
    test_cap_reached_is_info_when_recipient_alive_by_heartbeat()
    test_cap_reached_is_alarm_when_recipient_not_registered()
    test_recipient_gone_is_always_alarm_even_if_registered()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
