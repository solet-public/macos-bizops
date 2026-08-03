#!/usr/bin/env python3
"""Unit smoke for v10 Control #4 — persist-first role dispatch (no pytest, no DB).

Drives ``dispatch_role_send`` with stubs to prove the branching contract:

  * **persist-first** — the authoritative envelope upsert happens BEFORE any
    holder resolution, for EVERY branch (durability does not depend on a live
    holder);
  * **silent** role message → ``persisted_silent``, never auto-emitted, no
    resolve / wake / flag;
  * IMPORTANT + **offline holder** (resolve raises / wake fails / queue full)
    → ``queued_for_replay`` (success), ``delivered`` left false (NOT flipped);
  * IMPORTANT + **live holder, no native adapter** → ``queued_notification``
    (channel event carrying the Control #5 role-delivery meta keys);
    ``delivered`` is NOT flipped at send (v10 Q3 split — the holder's forwarder
    is the sole flip authority for the queued ``queued_notification`` path, via
    POST /peer/delivered);
  * IMPORTANT + **live holder, native adapter** → ``queued_wake``;
    ``delivered`` is NOT flipped at send (v10 Q3 REVISED — Codex B3: native wake
    is the SAME append_event bridge queue, not a direct push, so the forwarder
    is the sole flip authority for native too) — and the wake carries the role
    keys via ``delivery_meta``.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_dispatch_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.llm.agent_messaging.models import (  # noqa: E402
    RoleMessagePersisted,
    TextPart,
)

from agent_messaging_plugin.bridge_sessions import (  # noqa: E402
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    DELIVERY_PERSISTED_SILENT,
    DELIVERY_QUEUED_FOR_REPLAY,
    DELIVERY_QUEUED_NOTIFICATION,
    DELIVERY_QUEUED_WAKE,
    dispatch_role_send,
)
from agent_messaging_plugin.peer_registry import PeerUnreachableError  # noqa: E402
from agent_messaging_plugin.peer_role_management import ResolvedRole  # noqa: E402

# A fake stands in for the persisted ROW's created_at. Distinctive on purpose:
# a test asserting the wire meta carries THIS value cannot accidentally pass on
# a clock reading taken inside the code under test.
_ROW_CREATED_AT = "2026-08-01T00:00:00.000001+00:00"

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


def _binding() -> BridgeBinding:
    # The REAL binding type, not a hand-rolled stub — dispatch reads binding
    # surface beyond raw fields (``is_watcher``), and a stub silently drifts.
    return BridgeBinding(
        bridge_id="agc-live",
        agent_id="claude_code",
        agent_instance_id="agi-holder",
        session_label="Architect",
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
            raise PeerUnreachableError(
                f"no live binding for {agent_id}/{agent_instance_id}",
            )
        return _binding()

    def wake_adapter_for(self, agent_id: str) -> _WakeAdapter | None:
        return self._adapter


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

    def append_event(
        self, bridge_id: str, event: str, prose: str, meta: dict[str, object],
    ) -> None:
        self.events.append((bridge_id, event, prose, meta))


_ROLE = ResolvedRole(
    name="Architect",
    agent_id="claude_code",
    agent_instance_id="agi-holder",
    session_label="Architect",
)


def _content(text: str) -> list[TextPart]:
    return [TextPart(type="text", text=text)]


def _dispatch(
    *, text: str, online: bool, adapter: _WakeAdapter | None,
) -> tuple[Any, _FakeService, _FakeBridgeManager]:
    service = _FakeService()
    manager = _FakeBridgeManager()
    registry = _FakePeerRegistry(online=online, adapter=adapter)
    outcome = dispatch_role_send(
        bridge_manager=manager,  # type: ignore[arg-type]
        peer_registry=registry,  # type: ignore[arg-type]
        agent_messaging_service=service,
        role_name="Architect",
        role=_ROLE,
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Coordinator",
        sender_parent_pid=None,
        content=_content(text),
        message_id="arm-test",
    )
    return outcome, service, manager


def test_silent_inbox_only() -> None:
    outcome, service, manager = _dispatch(text="fyi", online=True, adapter=None)
    _check(outcome.delivery == DELIVERY_PERSISTED_SILENT, "silent → persisted_silent")
    _check(
        len(service.persisted) == 1 and service.persisted[0]["important"] is False,
        "silent → persisted once (important=False)",
    )
    _check(
        not service.delivered and not manager.events,
        "silent → never auto-emitted, no delivered-flag",
    )


def test_important_offline_queues() -> None:
    outcome, service, manager = _dispatch(
        text="IMPORTANT: ping", online=False, adapter=None,
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_FOR_REPLAY,
        "IMPORTANT + offline holder → queued_for_replay (success)",
    )
    _check(
        len(service.persisted) == 1 and service.persisted[0]["important"] is True,
        "persist-first: envelope persisted (important=True) BEFORE resolve",
    )
    _check(
        not service.delivered and not manager.events,
        "offline → delivered NOT flipped, nothing emitted",
    )


def test_important_live_channel_event() -> None:
    outcome, service, manager = _dispatch(
        text="IMPORTANT: ping", online=True, adapter=None,
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_NOTIFICATION,
        "IMPORTANT + live, no adapter → queued_notification",
    )
    _check(len(manager.events) == 1, "live → one peer_message channel event appended")
    # Q3 split: the queued_notification path must NOT flip delivered at send —
    # the holder's forwarder confirms emission via POST /peer/delivered. So a
    # reconnect before the forwarder drains /events leaves delivered=false and
    # the repair drain re-delivers (no queued_notification-path strand).
    _check(
        service.delivered == [],
        "queued_notification does NOT flip delivered at send (forwarder is sole authority)",
    )
    # The queued channel event MUST carry the Control #5 role-delivery meta keys
    # so the holder's forwarder can recognise the role delivery and POST the
    # /peer/delivered confirmation with the right external_id.
    _, _, _, meta = manager.events[0]
    _check(
        meta.get("recipient_kind") == "role"
        and meta.get("recipient_key") == "Architect"
        and meta.get("delivery_external_id") == "role:Architect:arm-test",
        "queued_notification event carries recipient_kind/recipient_key/delivery_external_id",
    )
    # (A) server half: the PERSISTED ROW's created_at rides the wire, so the
    # holder's watcher can advance role_high_water from a live delivery using
    # the same quantity the role-inbox section pages on. Asserted as the exact
    # value the persistence layer returned — a clock read taken inside dispatch
    # would be a different quantity and would fail here.
    _check(
        meta.get("role_created_at") == _ROW_CREATED_AT,
        "queued_notification event carries the persisted row's created_at",
    )


def test_important_native_wake() -> None:
    adapter = _WakeAdapter()
    outcome, service, manager = _dispatch(
        text="IMPORTANT: ping", online=True, adapter=adapter,
    )
    _check(
        outcome.delivery == DELIVERY_QUEUED_WAKE,
        "IMPORTANT + native adapter → queued_wake",
    )
    _check(len(adapter.calls) == 1, "native adapter.wake invoked once")
    # v10 Q3 REVISED (Codex B3): native wake is NOT a direct push — it's the
    # same append_event bridge queue — so it does NOT flip at send either; the
    # holder's forwarder /peer/delivered is the SOLE authority for both paths.
    _check(
        not service.delivered and not manager.events,
        "native wake does NOT flip at send (forwarder is sole authority); no channel event",
    )
    # The wake MUST carry the Control #5 role keys (delivery_meta) so the
    # holder's forwarder recognises the role delivery on /events and confirms it.
    wake_meta = adapter.calls[0].get("delivery_meta") or {}
    _check(
        wake_meta.get("recipient_kind") == "role"
        and wake_meta.get("recipient_key") == "Architect"
        and wake_meta.get("delivery_external_id") == "role:Architect:arm-test",
        "native wake carries the role keys (delivery_meta) for forwarder confirmation",
    )
    # (A) server half, BOTH transports. The native wake is the same bridge
    # queue, so a watcher woken this way must be able to advance its mark too —
    # stamping only the channel-event path would leave wake-delivered role mail
    # replaying forever on the arm after it.
    _check(
        wake_meta.get("role_created_at") == _ROW_CREATED_AT,
        "native wake carries the persisted row's created_at (delivery_meta)",
    )


def main() -> int:
    print("=== v10 Control #4 persist-first role dispatch smoke ===")
    test_silent_inbox_only()
    test_important_offline_queues()
    test_important_live_channel_event()
    test_important_native_wake()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
