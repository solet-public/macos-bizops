#!/usr/bin/env python3
"""Unit smoke for REL-01 Fork 4 — two-way role-addressed delivery (no pytest, no DB).

Fork 4 makes ``peer_send_by_name`` sends TWO-WAY by stamping the sender from the
caller's DURABLE role (reconnect-surviving) instead of a system scheduler, and by
surfacing a ROLE reply-to on the recipient's envelope. This smoke drives the four
real seams end-to-end with stubs:

  * **item 1 — consume-side lift** (``ActionProcessor._lift_inference_vertex_identity``):
    the caller's ``inference_vertex_role`` + originating ``inference_vertex_session_id``
    are lifted from the flow's ``trigger_data`` into the plugin handler ``state``.
    Guards: no flow_id / roleless / absent trigger_data leave ``state`` untouched.
  * **item 2 — sender ladder** (``_resolve_role_send_sender``): role → originating
    instance → system scheduler sentinel; provenance degrades silently but the
    role reply-to is always set when a role is present.
  * **item 3 — envelope reply-to** (``_wake_reply_hint``): a role send surfaces a
    ``peer_send_by_name name=<role>`` reply-to; a direct send keeps the instance
    reply-to.
  * **item 4 — return-leg reconnect survival**: the reply leg (role → role) is
    persist-first, so an offline/reconnecting holder yields ``queued_for_replay``
    (durable; the repair drain re-delivers) — the Vector-A guarantee on the return.

Plus the operator's BINDING CONSTRAINT (role names are arbitrary/opaque): a
deliberately bizarre role string routes/stamps identically, and a genuine
scheduler-originated send (no caller identity) still stamps ``system:scheduler``.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_two_way_reply_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.llm.agent_messaging.models import (  # noqa: E402
    RoleMessagePersisted,
    TextPart,
)

# Stands in for the persisted ROW's created_at (see role_dispatch_smoke).
_ROW_CREATED_AT = "2026-08-01T00:00:00.000001+00:00"

from _real_state_fake import RealShapeState  # noqa: E402

from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    DELIVERY_QUEUED_FOR_REPLAY,
    DELIVERY_QUEUED_WAKE,
    build_wake_reply_hint,
    dispatch_role_send,
)
from agent_messaging_plugin.peer_registry import PeerUnreachableError  # noqa: E402
from agent_messaging_plugin.peer_role_management import ResolvedRole  # noqa: E402
from agent_messaging_plugin.plugin import _resolve_role_send_sender  # noqa: E402

# A deliberately arbitrary, operator-defined-shaped role string with punctuation,
# unicode and spaces — proves the code paths treat role names as OPAQUE (never
# enumerated, never special-cased), per the operator's binding constraint.
_ARBITRARY_ROLE = "zz-Ω arbitrary/role #7!"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ---------------------------------------------------------------------------
# item 1 — ActionProcessor._lift_inference_vertex_identity (real method)
# ---------------------------------------------------------------------------


class _FakeAction:
    """Minimal QueuedAction-shaped stub carrying only what the lift reads."""

    def __init__(self, flow_id: str) -> None:
        self.flow_id = flow_id


class _LiftProc(ActionProcessor):
    """ActionProcessor with a stubbed flow-lookup — exercises the REAL lift.

    Bypasses the heavy constructor (the lift depends only on
    ``_get_flow_trigger_data``), so this drives the exact method
    ``_execute_plugin_method`` calls, not a reimplementation.
    """

    def __init__(
        self,
        trigger_data: dict[str, Any] | None,
        *,
        raises: bool = False,
    ) -> None:
        self._trigger_data = trigger_data
        self._raises = raises

    def _get_flow_trigger_data(self, flow_id: str) -> dict[str, Any] | None:
        if self._raises:
            raise ValueError("malformed trigger_data json")
        return self._trigger_data


def test_lift_populates_role_and_origin() -> None:
    proc = _LiftProc(
        {
            "inference_vertex_role": _ARBITRARY_ROLE,
            "inference_vertex_session_id": "agi-origin",
            "authenticated_principal": {"client_id": "irrelevant"},
        },
    )
    state: dict[str, object] = {"session_id": "s", "flow_id": "f"}
    proc._lift_inference_vertex_identity(state, _FakeAction("f"))  # type: ignore[arg-type]
    _check(
        state.get("inference_vertex_role") == _ARBITRARY_ROLE,
        "lift: arbitrary role copied opaquely into state",
    )
    _check(
        state.get("inference_vertex_session_id") == "agi-origin",
        "lift: originating instance id (inference_vertex_session_id) copied into state",
    )


def test_lift_guards() -> None:
    # no flow_id → no lookup, no lift
    proc = _LiftProc({"inference_vertex_role": "R"})
    s1: dict[str, object] = {}
    proc._lift_inference_vertex_identity(s1, _FakeAction(""))  # type: ignore[arg-type]
    _check("inference_vertex_role" not in s1, "lift guard: no flow_id → no lift")
    # roleless trigger_data → nothing added
    proc2 = _LiftProc({"authenticated_principal": {"client_id": "x"}})
    s2: dict[str, object] = {}
    proc2._lift_inference_vertex_identity(s2, _FakeAction("f"))  # type: ignore[arg-type]
    _check(
        "inference_vertex_role" not in s2 and "inference_vertex_session_id" not in s2,
        "lift guard: roleless trigger_data → no keys added",
    )
    # absent trigger_data → nothing added
    proc3 = _LiftProc(None)
    s3: dict[str, object] = {}
    proc3._lift_inference_vertex_identity(s3, _FakeAction("f"))  # type: ignore[arg-type]
    _check(not s3, "lift guard: absent trigger_data → state untouched")


def test_lift_survives_malformed_trigger_data() -> None:
    """MAJOR (Codex): the lift runs for EVERY plugin flow, so a malformed / faulted
    trigger_data (json.loads raise, state read error) MUST degrade silently — never
    break a non-role flow."""
    proc = _LiftProc(None, raises=True)
    state: dict[str, object] = {"session_id": "s", "flow_id": "f"}
    raised = False
    try:
        proc._lift_inference_vertex_identity(state, _FakeAction("f"))  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — the whole point is it must NOT propagate
        raised = True
    _check(not raised, "lift: malformed/faulted trigger_data → does NOT raise (non-role flows safe)")
    _check(
        "inference_vertex_role" not in state,
        "lift: malformed trigger_data → state untouched (degrade-silent)",
    )


# ---------------------------------------------------------------------------
# item 2 — _resolve_role_send_sender (real ladder) + role-name agnosticism
# ---------------------------------------------------------------------------


class _RaisingStateService:
    """A state_service whose role resolution always raises (provenance degrade)."""


def _patch_resolve(monkey: object | None) -> None:
    """Set plugin_module.resolve_role_binding to a fake (or restore the real one)."""
    plugin_module.resolve_role_binding = monkey  # type: ignore[assignment]


_REAL_RESOLVE = plugin_module.resolve_role_binding


def test_sender_role_with_provenance() -> None:
    def _fake_resolve(_state: object, name: str) -> ResolvedRole:
        return ResolvedRole(
            name=name,
            agent_id="claude_code",
            agent_instance_id="agi-current-holder",
            session_label=name,
        )

    _patch_resolve(_fake_resolve)
    try:
        sender = _resolve_role_send_sender(
            {"inference_vertex_role": _ARBITRARY_ROLE, "inference_vertex_session_id": "agi-o"},
            _RaisingStateService(),
        )
    finally:
        _patch_resolve(_REAL_RESOLVE)
    _check(sender.reply_to_role == _ARBITRARY_ROLE, "sender(role): reply_to_role = the opaque role")
    _check(
        sender.agent_id == "claude_code" and sender.agent_instance_id == "agi-current-holder" and sender.session_label == _ARBITRARY_ROLE,
        "sender(role): provenance taken from the resolved current binding",
    )


def test_sender_role_provenance_degrades() -> None:
    def _raise(_state: object, _name: str) -> ResolvedRole:
        raise RuntimeError("state fault")

    _patch_resolve(_raise)
    try:
        sender = _resolve_role_send_sender(
            {"inference_vertex_role": _ARBITRARY_ROLE, "inference_vertex_session_id": "agi-o"},
            _RaisingStateService(),
        )
    finally:
        _patch_resolve(_REAL_RESOLVE)
    _check(
        sender.reply_to_role == _ARBITRARY_ROLE,
        "sender(role, resolve fails): reply_to_role STILL set (two-way survives provenance fault)",
    )
    _check(
        sender.agent_instance_id == "agi-o" and sender.session_label == _ARBITRARY_ROLE,
        "sender(role, resolve fails): degrades to originating instance + role label",
    )


def test_sender_instance_fallback() -> None:
    sender = _resolve_role_send_sender(
        {"inference_vertex_session_id": "agi-o"},
        _RaisingStateService(),
    )
    _check(
        sender.reply_to_role == "" and sender.agent_instance_id == "agi-o",
        "sender(no role, origin present): fire-and-forget by instance, no reply-to-role",
    )


def test_sender_scheduler_sentinel() -> None:
    sender = _resolve_role_send_sender({}, _RaisingStateService())
    _check(
        sender.agent_id == "system" and sender.agent_instance_id == "system:scheduler" and sender.reply_to_role == "",
        "sender(no identity): genuine scheduler send still stamps system:scheduler (no regression)",
    )


# ---------------------------------------------------------------------------
# item 3 — _wake_reply_hint (real envelope reply-to)
# ---------------------------------------------------------------------------


def test_reply_hint_role_form() -> None:
    hint = build_wake_reply_hint(
        reply_to_role=_ARBITRARY_ROLE,
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-x",
        thread_id="role:X",
        message_id="arm-1",
    )
    _check(
        f"peer_send_by_name with name={_ARBITRARY_ROLE}" in hint,
        "reply-hint(role): role reply-to surfaced verbatim (opaque)",
    )
    _check(
        "peer_agent_instance_id=" not in hint,
        "reply-hint(role): NO instance reply-to (return leg is role-addressed, reconnect-surviving)",
    )


def test_reply_hint_instance_form() -> None:
    hint = build_wake_reply_hint(
        reply_to_role="",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-x",
        thread_id="t",
        message_id="m",
    )
    _check(
        "peer_send with peer_id=claude_code" in hint and "peer_agent_instance_id=agi-x" in hint,
        "reply-hint(direct): keeps the same-connection instance reply-to",
    )
    _check(
        "peer_send_by_name" not in hint,
        "reply-hint(direct): no role reply-to for a direct instance send",
    )


def test_reply_hint_omitted_for_scheduler_sentinel() -> None:
    """T1: a hint that cannot be answered is worse than none.

    ``system:scheduler`` is the sender stamp of a send with no caller identity; it
    is not an address anyone holds, so a reply instruction naming it reaches no one.
    """
    hint = build_wake_reply_hint(
        reply_to_role="",
        sender_agent_id="system",
        sender_agent_instance_id="system:scheduler",
        thread_id="agt-x",
        message_id="agm-x",
    )
    _check(hint == "", "reply-hint(scheduler sentinel): NO reply instruction (system:scheduler is not a routable address)")


def test_reply_hint_kept_for_registered_instance_and_role() -> None:
    """T2: every other delivery keeps its hint byte for byte."""
    instance_hint = build_wake_reply_hint(
        reply_to_role="",
        sender_agent_id="codex",
        sender_agent_instance_id="agi-sender",
        thread_id="agt-1",
        message_id="agm-1",
    )
    _check(
        instance_hint == '(reply via peer_send with peer_id=codex, peer_agent_instance_id=agi-sender; prefix prose with "IMPORTANT: " only if you need a response. thread_id=agt-1, message_id=agm-1.)',
        "reply-hint(registered instance): exact instance hint unchanged byte for byte",
    )
    role_hint = build_wake_reply_hint(
        reply_to_role="R_A",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-x",
        thread_id="role:R_B",
        message_id="arm-1",
    )
    _check(
        role_hint == '(reply via peer_send_by_name with name=R_A; prefix prose with "IMPORTANT: " only if you need a response. thread_id=role:R_B, message_id=arm-1.)',
        "reply-hint(role): exact role hint unchanged byte for byte",
    )
    # Rung 2 of the sender ladder stamps agent id "system" with a REAL originating
    # instance. Only the sentinel instance is unroutable, not the agent id.
    sender = _resolve_role_send_sender({"inference_vertex_session_id": "agi-o"}, _RaisingStateService())
    rung2_hint = build_wake_reply_hint(
        reply_to_role=sender.reply_to_role,
        sender_agent_id=sender.agent_id,
        sender_agent_instance_id=sender.agent_instance_id,
        thread_id="agt-2",
        message_id="agm-2",
    )
    _check(
        sender.agent_id == "system" and rung2_hint == '(reply via peer_send with peer_id=system, peer_agent_instance_id=agi-o; prefix prose with "IMPORTANT: " only if you need a response. thread_id=agt-2, message_id=agm-2.)',
        "reply-hint(agent id 'system', real instance): still gets the instance hint",
    )


def test_scheduler_sender_flows_to_no_hint() -> None:
    """T3: the REAL rung-4 output of the sender ladder is what the builder omits."""
    sender = _resolve_role_send_sender({}, _RaisingStateService())
    _check(
        sender.agent_instance_id == plugin_module.SYSTEM_SCHEDULER_ID and sender.reply_to_role == "",
        "sender(no identity): rung 4 yields the scheduler sentinel instance and no reply_to_role",
    )
    hint = build_wake_reply_hint(
        reply_to_role=sender.reply_to_role,
        sender_agent_id=sender.agent_id,
        sender_agent_instance_id=sender.agent_instance_id,
        thread_id="agt-x",
        message_id="agm-x",
    )
    _check(hint == "", "sender(no identity) → builder: no reply instruction reaches the recipient")


# ---------------------------------------------------------------------------
# item 4 — two-way loop + return-leg reconnect survival (dispatch_role_send)
# ---------------------------------------------------------------------------


class _FakeService:
    def __init__(self) -> None:
        self.persisted: list[dict[str, Any]] = []
        self.delivered: list[str] = []

    def persist_role_message(self, **kwargs: Any) -> RoleMessagePersisted:
        self.persisted.append(kwargs)
        return RoleMessagePersisted(
            message_id=str(kwargs["message_id"]),
            created_at=_ROW_CREATED_AT,
        )

    def mark_delivered(self, *, external_id: str) -> None:
        self.delivered.append(external_id)


def _live_binding() -> BridgeBinding:
    # The REAL binding type, not a hand-rolled stub — dispatch reads binding
    # surface beyond raw fields (``is_watcher``), and a stub silently drifts.
    return BridgeBinding(
        bridge_id="agc-live",
        agent_id="claude_code",
        agent_instance_id="agi-holder",
        session_label="holder",
        parent_pid=4242,
    )


class _WakeAdapter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def wake(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "agc-woke"


class _FakePeerRegistry:
    def __init__(self, *, online: bool, adapter: _WakeAdapter | None) -> None:
        self._online = online
        self._adapter = adapter

    def resolve(self, agent_id: str, agent_instance_id: str | None) -> BridgeBinding:
        if not self._online:
            raise PeerUnreachableError(f"no live binding for {agent_id}/{agent_instance_id}")
        return _live_binding()

    def wake_adapter_for(self, agent_id: str) -> _WakeAdapter | None:
        return self._adapter


class _LiveBridge:
    """A4 Amendment 5: binding_is_live now reads ``closed``/``last_seen_at``
    off the bridge for EVERY recipient kind (was watcher-only)."""

    closed = False
    last_seen_at = datetime.now(UTC).isoformat()


class _FakeBridgeManager:
    # WS-2a W3: the dispatch liveness gate reads this off its bridge_manager
    # collaborator. A fake that omits it is not standing in for the real
    # manager — and a defensive getattr in the production path would hide
    # exactly that, so the CONTRACT is satisfied here instead.
    @property
    def binding_liveness_window_s(self) -> int:
        return DEFAULT_BINDING_LIVENESS_WINDOW_S

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict[str, object]]] = []

    def get(self, bridge_id: str) -> _LiveBridge:
        del bridge_id
        return _LiveBridge()

    def append_event(
        self,
        bridge_id: str,
        event: str,
        prose: str,
        meta: dict[str, object],
    ) -> None:
        self.events.append((bridge_id, event, prose, meta))


def _role(name: str) -> ResolvedRole:
    return ResolvedRole(
        name=name,
        agent_id="claude_code",
        agent_instance_id="agi-holder",
        session_label=name,
    )


def test_forward_leg_carries_role_reply_to() -> None:
    """R_A → R_B live: the wake carries reply_to_role = R_A (the durable return address)."""
    adapter = _WakeAdapter()
    dispatch_role_send(
        bridge_manager=_FakeBridgeManager(),  # type: ignore[arg-type]
        peer_registry=_FakePeerRegistry(online=True, adapter=adapter),  # type: ignore[arg-type]
        agent_messaging_service=_FakeService(),
        state_service=RealShapeState(),  # type: ignore[arg-type]
        role_name="R_B",
        role=_role("R_B"),
        sender_bridge_id="system:scheduler",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label=_ARBITRARY_ROLE,
        sender_parent_pid=None,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        message_id="arm-fwd",
        reply_to_role=_ARBITRARY_ROLE,
    )
    _check(len(adapter.calls) == 1, "forward leg: native wake invoked once")
    _check(
        adapter.calls[0].get("reply_to_role") == _ARBITRARY_ROLE,
        "forward leg: wake carries reply_to_role = caller's opaque role (two-way return address)",
    )


def test_no_adapter_role_send_prose_names_role() -> None:
    """BLOCKER (Codex): a role send to a NO-ADAPTER (Codex / streamable) recipient
    must still carry the role reply-to. The channel-event PROSE names the sender
    role so the return leg is role-addressed (reconnect-surviving) off-native too."""
    manager = _FakeBridgeManager()
    dispatch_role_send(
        bridge_manager=manager,  # type: ignore[arg-type]
        peer_registry=_FakePeerRegistry(online=True, adapter=None),  # type: ignore[arg-type]
        agent_messaging_service=_FakeService(),
        state_service=RealShapeState(),  # type: ignore[arg-type]
        role_name="R_B",
        role=_role("R_B"),
        sender_bridge_id="system:scheduler",
        sender_agent_id="codex",
        sender_agent_instance_id="agi-sender",
        sender_session_label=_ARBITRARY_ROLE,
        sender_parent_pid=None,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        message_id="arm-noad",
        reply_to_role=_ARBITRARY_ROLE,
    )
    _check(len(manager.events) == 1, "no-adapter role send → one channel event appended")
    _, _, prose, _meta = manager.events[0]
    _check(
        f"peer_send_by_name with name={_ARBITRARY_ROLE}" in prose,
        "no-adapter event PROSE names the sender role (two-way works for Codex/streamable)",
    )
    _check(
        "peer_agent_instance_id=" not in prose,
        "no-adapter role send: reply-to is role-addressed, not instance (reconnect-surviving)",
    )


def _no_adapter_prose(*, sender_agent_id: str, sender_agent_instance_id: str, message_id: str) -> tuple[str, dict[str, object]]:
    """Drive the no-adapter channel-event path (peer_dispatch's hinted-prose caller) and return (prose, meta)."""
    manager = _FakeBridgeManager()
    dispatch_role_send(
        bridge_manager=manager,  # type: ignore[arg-type]
        peer_registry=_FakePeerRegistry(online=True, adapter=None),  # type: ignore[arg-type]
        agent_messaging_service=_FakeService(),
        state_service=RealShapeState(),  # type: ignore[arg-type]
        role_name="R_B",
        role=_role("R_B"),
        sender_bridge_id="system:scheduler",
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label="sender",
        sender_parent_pid=None,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        message_id=message_id,
        reply_to_role="",
    )
    _check(len(manager.events) == 1, f"no-adapter send ({message_id}) → one channel event appended")
    _, _, prose, meta = manager.events[0]
    return prose, meta


def test_forward_prose_has_no_trailing_blank_lines_without_hint() -> None:
    """T4: with no reply instruction the delivered prose is the bare prose.

    The caller used to join ``"\\n\\n" + hint`` unconditionally, so an omitted hint
    would have left two trailing newlines on every scheduler-originated message.
    """
    prose, _meta = _no_adapter_prose(sender_agent_id="system", sender_agent_instance_id="system:scheduler", message_id="arm-sentinel")
    _check(prose == "ping", "no-adapter scheduler-sentinel send: prose is exactly the delivered prose (no hint, no trailing newline)")
    # Control: a registered instance still gets "\n\n" + its hint, byte for byte.
    prose, meta = _no_adapter_prose(sender_agent_id="codex", sender_agent_instance_id="agi-sender", message_id="arm-registered")
    _check(
        prose == f'ping\n\n(reply via peer_send with peer_id=codex, peer_agent_instance_id=agi-sender; prefix prose with "IMPORTANT: " only if you need a response. thread_id={meta["thread_id"]}, message_id=arm-registered.)',
        "no-adapter registered-instance send: prose still carries the instance hint after a blank line",
    )


class _WakeBridge:
    """Stands in for the recipient ``BridgeSessionState`` — ``wake`` reads only ``bridge_id``."""

    bridge_id = "agc-wake"


class _WakeBridgeManager(_FakeBridgeManager):
    """``wake`` also refreshes the bridge's liveness stamp after it appends the event."""

    def touch(self, bridge_id: str) -> None:
        del bridge_id


def _wake_envelope(*, sender_agent_id: str, sender_agent_instance_id: str, sender_session_label: str) -> str:
    """Drive the REAL ``AgentMessagingPlugin.wake`` (claude_code native wake) and return its envelope."""
    manager = _WakeBridgeManager()
    # ``Any`` handles: the fakes stand in for the real manager, registry and bridge finder.
    plugin: Any = plugin_module.AgentMessagingPlugin()
    plugin._bridge_manager = manager
    plugin._peer_registry = _FakePeerRegistry(online=True, adapter=None)
    patched_module: Any = plugin_module
    real_find = patched_module._find_claude_code_bridge_by_parent_pid
    patched_module._find_claude_code_bridge_by_parent_pid = lambda **_kwargs: _WakeBridge()
    try:
        plugin.wake(
            recipient_parent_pid=4242,
            delivered_prose="ping",
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_session_label=sender_session_label,
            thread_id="agt-x",
            message_id="agm-x",
        )
    finally:
        patched_module._find_claude_code_bridge_by_parent_pid = real_find
    _check(len(manager.events) == 1, "native wake → one post_message event appended")
    return manager.events[0][2]


def test_wake_envelope_has_no_hint_for_scheduler_sentinel() -> None:
    """T5 (beyond the plan's T1-T4): the native wake envelope is the builder's second caller.

    Same two-sided check as T4 on ``plugin.wake``, whose own ``"\\n\\n{reply_hint}"``
    join is changed by the same fix.
    """
    envelope = _wake_envelope(sender_agent_id="system", sender_agent_instance_id="system:scheduler", sender_session_label="System (Scheduler)")
    _check(
        envelope == '[peer:system "System (Scheduler)" instance=system:scheduler] ping',
        "wake envelope, scheduler sentinel: header and prose only (no reply instruction, no trailing newline)",
    )
    envelope = _wake_envelope(sender_agent_id="codex", sender_agent_instance_id="agi-sender", sender_session_label="worker")
    _check(
        envelope == '[peer:codex "worker" instance=agi-sender] ping\n\n(reply via peer_send with peer_id=codex, peer_agent_instance_id=agi-sender; prefix prose with "IMPORTANT: " only if you need a response. thread_id=agt-x, message_id=agm-x.)',
        "wake envelope, registered instance: still carries the instance hint after a blank line",
    )


def test_return_leg_survives_reconnect() -> None:
    """R_B → R_A while R_A is offline/reconnecting: persist-first → queued_for_replay durable."""
    service = _FakeService()
    outcome = dispatch_role_send(
        bridge_manager=_FakeBridgeManager(),  # type: ignore[arg-type]
        peer_registry=_FakePeerRegistry(online=False, adapter=None),  # type: ignore[arg-type]
        agent_messaging_service=service,
        state_service=RealShapeState(),  # type: ignore[arg-type]
        role_name=_ARBITRARY_ROLE,
        role=_role(_ARBITRARY_ROLE),
        sender_bridge_id="system:scheduler",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-rb",
        sender_session_label="R_B",
        sender_parent_pid=None,
        content=[TextPart(type="text", text="IMPORTANT: reply")],
        message_id="arm-ret",
        reply_to_role="R_B",
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_FOR_REPLAY,
        "return leg: offline holder → queued_for_replay (durable; repair drain re-delivers)",
    )
    _check(
        len(service.persisted) == 1 and service.persisted[0]["important"] is True,
        "return leg: persist-first — envelope durable BEFORE resolve (survives reconnect)",
    )
    _check(not service.delivered, "return leg: delivered NOT flipped at send")


def test_forward_leg_delivers_woke_native() -> None:
    """Sanity: the forward leg to a live native holder reports queued_wake."""
    outcome = dispatch_role_send(
        bridge_manager=_FakeBridgeManager(),  # type: ignore[arg-type]
        peer_registry=_FakePeerRegistry(online=True, adapter=_WakeAdapter()),  # type: ignore[arg-type]
        agent_messaging_service=_FakeService(),
        state_service=RealShapeState(),  # type: ignore[arg-type]
        role_name="R_B",
        role=_role("R_B"),
        sender_bridge_id="system:scheduler",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label=_ARBITRARY_ROLE,
        sender_parent_pid=None,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        message_id="arm-fwd2",
        reply_to_role=_ARBITRARY_ROLE,
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_WAKE,
        "forward leg: live native holder → queued_wake",
    )


def main() -> int:
    print("=== REL-01 Fork 4 two-way role reply smoke ===")
    test_lift_populates_role_and_origin()
    test_lift_guards()
    test_lift_survives_malformed_trigger_data()
    test_sender_role_with_provenance()
    test_sender_role_provenance_degrades()
    test_sender_instance_fallback()
    test_sender_scheduler_sentinel()
    test_reply_hint_role_form()
    test_reply_hint_instance_form()
    test_reply_hint_omitted_for_scheduler_sentinel()
    test_reply_hint_kept_for_registered_instance_and_role()
    test_scheduler_sender_flows_to_no_hint()
    test_forward_leg_carries_role_reply_to()
    test_no_adapter_role_send_prose_names_role()
    test_forward_prose_has_no_trailing_blank_lines_without_hint()
    test_wake_envelope_has_no_hint_for_scheduler_sentinel()
    test_return_leg_survives_reconnect()
    test_forward_leg_delivers_woke_native()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
