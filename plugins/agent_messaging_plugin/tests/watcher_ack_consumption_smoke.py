#!/usr/bin/env python3
"""Watcher delivery labelling + events-ack consumption (no DB).

An MCP-free ``<name> watch`` recipient is a PULL consumer: the queued IMPORTANT
event streams into its watch output and surfaces when the session next looks —
no model turn starts, so the REL-05 model-activity consumption signal never
fires for it. Pre-fix, every IMPORTANT to a watcher-held recipient escalated
``recipient_gone`` forever even though the bytes were surfaced and read, and
senders got ``queued_wake`` (reads as a live wake). This smoke pins the fix:

  * **L1 labelling (direct)** — an IMPORTANT direct send resolving to a
    watcher binding (``agi-watch-*``) reports ``queued_watcher`` on BOTH
    transports (native adapter / channel event); a non-watcher recipient keeps
    ``queued_wake`` / ``queued_notification`` (positive control).
  * **L2 labelling (role)** — same split for ``dispatch_role_send``.
  * **A1 events-ack consumption (route-level)** — GET /events returning a
    wake event does NOT consume (in-flight, not yet surfaced); the NEXT poll
    whose cursor acks it DOES stamp the direct row consumed and the role row
    consumed+delivered. RED anchor: an un-acked sibling row stays owed and the
    reconciler still escalates it (a dead watcher must keep escalating).
  * **A2 watcher-only fence** — the same two-poll cycle on a NON-watcher
    binding consumes nothing (an MCP forwarder drains events without the model
    reading them; its consumption authority stays /peer/drain).
  * **A3 inbox catch-up consumption** — a watcher's arm-time
    ``peer_inbox`` default catch-up read stamps the returned role rows
    consumed via the real route; the helper also consumes instance-section
    (direct) entries; a non-watcher inbox read consumes nothing.
  * **A4 stdio role-send parity** — the bridge HTTP route behind stdio
    ``peer_send_by_name`` resolves the role and stamps the sender bridge
    identity onto the durable role row.

Run:
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/watcher_ack_consumption_smoke.py
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
from ananta.llm.agent_messaging.models import (  # noqa: E402
    AgentMessageRow,
    MessageKind,
    MessageRole,
    PeerInboxEntry,
    RoleMessagePersisted,
    TextPart,
)
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    META_KEY_DELIVERY_EXTERNAL_ID,
    NAMESPACE,
    RECIPIENT_KIND_ROLE,
    ROLE_THREAD_PREFIX,
    TABLE_AGENT_DIRECT_WAKE,
    TABLE_AGENT_MESSAGE,
    TABLE_AGENT_ROLE_MESSAGE,
    TABLE_AGENT_THREAD,
)
from ananta.llm.agent_messaging.service import (  # noqa: E402
    AgentMessagingService,
    role_message_external_id,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    normalize_sort_value,
)
from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.direct_wake_reconcile import (  # noqa: E402
    DirectWakeReconciler,
)
from agent_messaging_plugin.http_routes import (  # noqa: E402
    _consume_watcher_inbox_page,
    register_routes,
)
from agent_messaging_plugin.models import (  # noqa: E402
    WATCH_AGENT_INSTANCE_PREFIX,
    BridgeBinding,
)
from agent_messaging_plugin.peer_dispatch import (  # noqa: E402
    DELIVERY_QUEUED_NOTIFICATION,
    DELIVERY_QUEUED_WAKE,
    DELIVERY_QUEUED_WATCHER,
    EVENT_POST_MESSAGE,
    dispatch_peer_send,
    dispatch_role_send,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.peer_role_management import ResolvedRole  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

T0 = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)
WATCHER_AGI = f"{WATCH_AGENT_INSTANCE_PREFIX}0123456789abcdef01234567"
LIVE_AGI = "agi-live-instance"
_ROLE_BINDING_NS = "agent_messaging_plugin"

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
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()
    max_message_bytes = 65_536


def _service(state: RealShapeState, clock: _Clock) -> AgentMessagingService:
    # Deterministic standardizer stamps: inserts carry the SAME controlled
    # clock the service uses, so window/deadline arithmetic is time-decoupled.
    state.now_iso = lambda: clock.now.isoformat()
    return AgentMessagingService(
        repository=AgentMessagingRepository(
            cast(StateManagementInterface, state),
        ),
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


class _FakeWakeAdapter:
    """The claude_code adapter's queue behaviour: append onto a fixed bridge."""

    def __init__(self, manager: BridgeSessionManager, bridge_id: str) -> None:
        self._manager = manager
        self._bridge_id = bridge_id

    def wake(self, **kwargs: Any) -> str:
        message_id = str(kwargs["message_id"])
        meta: dict[str, object] = {
            "flow_id": f"peer-wake-{message_id}",
            "thread_id": str(kwargs["thread_id"]),
            "message_id": message_id,
        }
        delivery_meta = kwargs.get("delivery_meta")
        if delivery_meta:
            meta.update(delivery_meta)
        self._manager.append_event(
            self._bridge_id,
            EVENT_POST_MESSAGE,
            str(kwargs["delivered_prose"]),
            meta,
        )
        return self._bridge_id


class _FakeDispatchService:
    """Minimal peer_send/persist collaborator for labelling-only dispatches."""

    def peer_send(self, request: Any) -> Any:  # noqa: ANN401 — mirrors the real facade
        class _R:
            thread_id = "agt-x"
            message_id = "agm-x"
            cursor = 0

        return _R()

    def persist_direct_wake(self, **_kwargs: Any) -> None:
        return None

    def persist_role_message(self, **kwargs: Any) -> RoleMessagePersisted:
        # Mirrors the real facade's widened return: the dispatcher reads
        # ``.created_at`` off this to stamp the role-delivery wire meta.
        return RoleMessagePersisted(
            message_id=str(kwargs.get("message_id", "")),
            created_at="2026-08-01T00:00:00.000001+00:00",
        )


def _register_binding(
    reg: PeerRegistry,
    *,
    bridge_id: str,
    agent_instance_id: str,
    session_label: str,
    parent_pid: int,
) -> None:
    reg.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id=agent_instance_id,
            session_label=session_label,
            parent_pid=parent_pid,
            agent_session_id=f"ases-{agent_instance_id}",
        ),
    )


def _dispatch_direct(
    mgr: BridgeSessionManager,
    reg: PeerRegistry,
    *,
    recipient_agi: str,
) -> str:
    outcome = dispatch_peer_send(
        bridge_manager=mgr,
        peer_registry=reg,
        agent_messaging_service=cast(Any, _FakeDispatchService()),
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_parent_pid=1,
        peer_id="claude_code",
        peer_agent_instance_id=recipient_agi,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
    )
    return outcome.delivery


def _dispatch_role(
    mgr: BridgeSessionManager,
    reg: PeerRegistry,
    *,
    recipient_agi: str,
) -> str:
    outcome = dispatch_role_send(
        bridge_manager=mgr,
        peer_registry=reg,
        agent_messaging_service=cast(Any, _FakeDispatchService()),
        role_name="R",
        role=ResolvedRole(
            name="R",
            agent_id="claude_code",
            agent_instance_id=recipient_agi,
            session_label="R",
        ),
        sender_bridge_id="agc-sender",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_parent_pid=1,
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        message_id="agm-role-1",
        reply_to_role="Sender-Role",
    )
    return outcome.delivery


# ---------------------------------------------------------------------------
# L1 / L2 — delivery labelling
# ---------------------------------------------------------------------------


def test_labelling() -> None:
    for adapter_registered, watcher_expect, live_expect in (
        (True, DELIVERY_QUEUED_WATCHER, DELIVERY_QUEUED_WAKE),
        (False, DELIVERY_QUEUED_WATCHER, DELIVERY_QUEUED_NOTIFICATION),
    ):
        mgr = _bridge_manager()
        reg = _peer_registry()
        watcher_bridge = mgr.open(homunculus_name="", parent_pid=77).bridge_id
        live_bridge = mgr.open(homunculus_name="", parent_pid=88).bridge_id
        _register_binding(
            reg,
            bridge_id=watcher_bridge,
            agent_instance_id=WATCHER_AGI,
            session_label="GC",
            parent_pid=77,
        )
        _register_binding(
            reg,
            bridge_id=live_bridge,
            agent_instance_id=LIVE_AGI,
            session_label="Live",
            parent_pid=88,
        )
        transport = "adapter" if adapter_registered else "channel-event"
        if adapter_registered:
            # One adapter per agent_id; point it at each recipient's bridge
            # right before dispatch (the real adapter resolves by parent_pid).
            reg.register_native_wake_adapter(
                "claude_code", _FakeWakeAdapter(mgr, watcher_bridge),
            )
        _check(
            _dispatch_direct(mgr, reg, recipient_agi=WATCHER_AGI)
            == watcher_expect,
            f"L1: direct IMPORTANT to a watcher ({transport}) → {watcher_expect}",
        )
        _check(
            _dispatch_role(mgr, reg, recipient_agi=WATCHER_AGI)
            == watcher_expect,
            f"L2: role IMPORTANT to a watcher holder ({transport}) → {watcher_expect}",
        )
        if adapter_registered:
            reg.register_native_wake_adapter(
                "claude_code", _FakeWakeAdapter(mgr, live_bridge),
            )
        _check(
            _dispatch_direct(mgr, reg, recipient_agi=LIVE_AGI) == live_expect,
            f"L1: direct IMPORTANT to a non-watcher ({transport}) → {live_expect}",
        )
        _check(
            _dispatch_role(mgr, reg, recipient_agi=LIVE_AGI) == live_expect,
            f"L2: role IMPORTANT to a non-watcher holder ({transport}) → {live_expect}",
        )


# ---------------------------------------------------------------------------
# A1 / A2 — events-ack consumption through the real route
# ---------------------------------------------------------------------------


def _routes_client(
    mgr: BridgeSessionManager,
    reg: PeerRegistry,
    svc: AgentMessagingService,
    state: RealShapeState,
) -> TestClient:
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=mgr,
        peer_registry=reg,
        platform_surface=cast(Any, object()),
        agent_messaging_service=svc,
        config={"long_poll_timeout_seconds": 1},
        state_service=cast(StateManagementInterface, state),
    )
    return TestClient(app)


def _persist_direct_row(svc: AgentMessagingService, *, message_id: str) -> None:
    svc.persist_direct_wake(
        message_id=message_id,
        thread_id="agt-1",
        recipient_agent_id="claude_code",
        recipient_agent_instance_id=WATCHER_AGI,
        recipient_agent_session_id=f"ases-{WATCHER_AGI}",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_bridge_id="agc-sender",
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        activity_at_emission=None,
    )


def _row(state: RealShapeState, table: str, message_id: str) -> dict[str, Any]:
    return next(
        r
        for r in state.rows(NAMESPACE, table)
        if r["message_id"] == message_id
    )


def test_events_ack_consumption() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    watcher_bridge = mgr.open(homunculus_name="", parent_pid=77).bridge_id
    _register_binding(
        reg,
        bridge_id=watcher_bridge,
        agent_instance_id=WATCHER_AGI,
        session_label="GC",
        parent_pid=77,
    )
    # An owed direct row + an owed role row, each with its wake event queued on
    # the watcher's bridge exactly as the dispatch transports append them.
    _persist_direct_row(svc, message_id="agm-d1")
    persisted_role = svc.persist_role_message(
        recipient_kind=RECIPIENT_KIND_ROLE,
        recipient_key="R",
        message_id="agm-r1",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        important=True,
        content=[TextPart(type="text", text="IMPORTANT: role ping")],
    )
    # (A) server half, against the REAL service rather than a fake: the
    # created_at handed back is the PERSISTED ROW's — the same quantity the
    # role-inbox section orders and pages on — which is the whole reason it is
    # safe for a watcher to advance role_high_water against it. A clock read
    # taken beside the write would be a different quantity and would fail here.
    _check(
        bool(persisted_role.created_at)
        and persisted_role.created_at
        == normalize_sort_value(
            _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r1")["created_at"],
        ),
        "A0: persist_role_message returns the persisted ROW's created_at",
    )
    role_external = role_message_external_id(RECIPIENT_KIND_ROLE, "R", "agm-r1")
    mgr.append_event(
        watcher_bridge, EVENT_POST_MESSAGE, "ping",
        {"message_id": "agm-d1", "thread_id": "agt-1"},
    )
    mgr.append_event(
        watcher_bridge, EVENT_POST_MESSAGE, "role ping",
        {"message_id": "agm-r1", META_KEY_DELIVERY_EXTERNAL_ID: role_external},
    )
    client = _routes_client(mgr, reg, svc, state)
    first = client.get(f"/api/v1/bridge/{watcher_bridge}/events?after=-1").json()
    _check(
        len(first["events"]) == 2
        and _row(state, TABLE_AGENT_DIRECT_WAKE, "agm-d1")["consumed"] is False
        and _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r1")["consumed"] is False,
        "A1: returning events does NOT consume (in-flight, not yet surfaced)",
    )
    ack_cursor = first["next_cursor"]
    client.get(f"/api/v1/bridge/{watcher_bridge}/events?after={ack_cursor}")
    direct_row = _row(state, TABLE_AGENT_DIRECT_WAKE, "agm-d1")
    role_row = _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r1")
    _check(
        direct_row["consumed"] is True and bool(direct_row["consumed_at"]),
        "A1: the cursor ack stamps the direct row consumed",
    )
    _check(
        role_row["consumed"] is True and role_row["delivered"] is True,
        "A1: the cursor ack stamps the role row consumed + delivered",
    )
    # Green: nothing escalatable far past the cap-equivalent time — an ARMED
    # watcher never produces recipient_gone spam. Red anchor: a NEW un-acked
    # row (dead/unread watcher) still escalates.
    reconciler = DirectWakeReconciler(
        service=svc,
        bridge_manager=mgr,
        peer_registry=reg,
        cap=3,
        re_emit_window_s=300,
        clock=lambda: T0 + timedelta(seconds=100_000),
    )
    _check(
        reconciler.reconcile() == 0,
        "A1: acked deliveries never escalate (recipient_gone spam is gone)",
    )
    _persist_direct_row(svc, message_id="agm-d2")
    _check(
        reconciler.reconcile() == 1,
        "A1 red anchor: an UN-acked owed row still escalates (dead watcher stays loud)",
    )


def test_events_ack_is_watcher_only() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    live_bridge = mgr.open(homunculus_name="", parent_pid=88).bridge_id
    _register_binding(
        reg,
        bridge_id=live_bridge,
        agent_instance_id=LIVE_AGI,
        session_label="Live",
        parent_pid=88,
    )
    svc.persist_direct_wake(
        message_id="agm-d3",
        thread_id="agt-1",
        recipient_agent_id="claude_code",
        recipient_agent_instance_id=LIVE_AGI,
        recipient_agent_session_id=f"ases-{LIVE_AGI}",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        sender_bridge_id="agc-sender",
        content=[TextPart(type="text", text="IMPORTANT: ping")],
        activity_at_emission=None,
    )
    mgr.append_event(
        live_bridge, EVENT_POST_MESSAGE, "ping",
        {"message_id": "agm-d3", "thread_id": "agt-1"},
    )
    client = _routes_client(mgr, reg, svc, state)
    first = client.get(f"/api/v1/bridge/{live_bridge}/events?after=-1").json()
    client.get(
        f"/api/v1/bridge/{live_bridge}/events?after={first['next_cursor']}",
    )
    _check(
        _row(state, TABLE_AGENT_DIRECT_WAKE, "agm-d3")["consumed"] is False,
        "A2: a NON-watcher bridge's events ack consumes nothing (forwarder"
        " drains are not reads)",
    )


# ---------------------------------------------------------------------------
# A3 — inbox catch-up consumption
# ---------------------------------------------------------------------------


def _seed_role_binding(state: RealShapeState, *, role: str, agi: str) -> None:
    from ananta.llm.agent_messaging.role_binding import TABLE_ROLE_BINDING

    state.rows(_ROLE_BINDING_NS, TABLE_ROLE_BINDING).append(
        {
            "id": f"rbn-{agi}",
            "external_id": f"role:{role}",
            "role": role,
            "holder_kind": "session",
            "agent_instance_id": agi,
            "agent_session_id": f"ases-{agi}",
            "holder_identity": {"agent_id": "claude_code", "session_label": role},
            "claim_epoch": 1,
            "claimed_at": T0.isoformat(),
            "is_deleted": 0,
        },
    )


def test_inbox_catchup_consumption() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    watcher_bridge = mgr.open(homunculus_name="", parent_pid=77).bridge_id
    _register_binding(
        reg,
        bridge_id=watcher_bridge,
        agent_instance_id=WATCHER_AGI,
        session_label="R",
        parent_pid=77,
    )
    _seed_role_binding(state, role="R", agi=WATCHER_AGI)
    svc.persist_role_message(
        recipient_kind=RECIPIENT_KIND_ROLE,
        recipient_key="R",
        message_id="agm-r2",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        important=True,
        content=[TextPart(type="text", text="IMPORTANT: queued while away")],
    )
    client = _routes_client(mgr, reg, svc, state)
    silent_read = client.get(
        f"/api/v1/bridge/{watcher_bridge}/peer/inbox"
        "?include_important=false&limit=50",
    )
    _check(
        silent_read.status_code == 200
        and _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r2")["consumed"] is False,
        "A3: a silent-bucket inbox read does NOT consume (catch-up only)",
    )
    default_catchup = client.get(
        f"/api/v1/bridge/{watcher_bridge}/peer/inbox?limit=50",
    ).json()
    row = _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r2")
    _check(
        len(default_catchup["role_entries"]) == 1
        and row["consumed"] is True
        and row["delivered"] is True,
        "A3: the omitted include_important default is catch-up and consumes the"
        " surfaced role row",
    )
    catchup = client.get(
        f"/api/v1/bridge/{watcher_bridge}/peer/inbox"
        "?include_important=true&limit=50",
    ).json()
    row = _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r2")
    _check(
        len(catchup["role_entries"]) == 1
        and row["consumed"] is True
        and row["delivered"] is True,
        "A3: the watcher's include_important catch-up read consumes the"
        " surfaced role row",
    )


def test_inbox_default_catchup_returns_direct_important() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    watcher_bridge = mgr.open(homunculus_name="", parent_pid=77).bridge_id
    _register_binding(
        reg,
        bridge_id=watcher_bridge,
        agent_instance_id=WATCHER_AGI,
        session_label="R",
        parent_pid=77,
    )
    state.rows(NAMESPACE, TABLE_AGENT_THREAD).append(
        {
            "id": "agt-direct-catchup",
            "namespace": NAMESPACE,
            "originator_type": "mcp_bridge",
            "originator_id": None,
            "originator_session_id": "agc-sender",
            "originator_bridge_id": "agc-sender",
            "target_backend": "peer:claude_code",
            "target_plugin_name": "agent_messaging_plugin",
            "title": "peer: Sender -> R",
            "working_directory": None,
            "status": "idle",
            "last_message_cursor": 1,
            "metadata": {},
            "recipient_agent_instance_id": WATCHER_AGI,
            "recipient_agent_session_id": f"ases-{WATCHER_AGI}",
            "originator_session_label": "Sender",
            "originator_agent_instance_id": "agi-sender",
            "recipient_session_label": "R",
            "created_at": T0.isoformat(),
            "updated_at": T0.isoformat(),
            "is_deleted": 0,
        },
    )
    state.rows(NAMESPACE, TABLE_AGENT_MESSAGE).append(
        {
            "id": "agm-direct-catchup",
            "namespace": NAMESPACE,
            "thread_id": "agt-direct-catchup",
            "cursor": 1,
            "role": "originator",
            "kind": "message",
            "content": [{"type": "text", "text": "direct catch-up"}],
            "artifacts": [],
            "action_id": None,
            "backend_session_id": None,
            "error": None,
            "metadata": {
                "peer": True,
                "sender_agent_id": "claude_code",
                "sender_agent_instance_id": "agi-sender",
                "sender_session_label": "Sender",
                "peer_agent_id": "claude_code",
                "peer_agent_instance_id": WATCHER_AGI,
                "important": True,
            },
            "important": True,
            "created_at": T0.isoformat(),
            "updated_at": T0.isoformat(),
            "is_deleted": 0,
        },
    )
    client = _routes_client(mgr, reg, svc, state)
    silent_read = client.get(
        f"/api/v1/bridge/{watcher_bridge}/peer/inbox"
        "?include_important=false&limit=50",
    ).json()
    _check(
        silent_read["entries"] == [],
        "A3: explicit silent-only direct inbox excludes IMPORTANT rows",
    )
    default_catchup = client.get(
        f"/api/v1/bridge/{watcher_bridge}/peer/inbox?limit=50",
    ).json()
    entries = default_catchup["entries"]
    _check(
        len(entries) == 1
        and entries[0]["sender_agent_instance_id"] == "agi-sender"
        and entries[0]["message"]["metadata"]["important"] is True,
        "A3: omitted include_important default returns direct IMPORTANT rows",
    )


def test_inbox_helper_consumes_direct_entries() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist_direct_row(svc, message_id="agm-d4")
    entry = PeerInboxEntry(
        thread_id="agt-1",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        message=AgentMessageRow(
            id="agm-d4",
            thread_id="agt-1",
            cursor=0,
            role=MessageRole.ORIGINATOR,
            kind=MessageKind.MESSAGE,
            content=[TextPart(type="text", text="ping")],
            created_at=T0,
        ),
    )

    class _Page:
        entries = (entry,)
        role_entries: tuple[PeerInboxEntry, ...] = ()

    binding = BridgeBinding(
        bridge_id="agc-w",
        agent_id="claude_code",
        agent_instance_id=WATCHER_AGI,
        session_label="GC",
        parent_pid=77,
    )
    _consume_watcher_inbox_page(svc, binding, _Page())
    _check(
        _row(state, TABLE_AGENT_DIRECT_WAKE, "agm-d4")["consumed"] is True,
        "A3: the inbox helper consumes instance-section (direct) entries",
    )
    _check(
        ROLE_THREAD_PREFIX == "role:",
        "A3: role thread prefix matches the persisted synthetic handle",
    )


def test_inbox_is_watcher_only() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    live_bridge = mgr.open(homunculus_name="", parent_pid=88).bridge_id
    _register_binding(
        reg,
        bridge_id=live_bridge,
        agent_instance_id=LIVE_AGI,
        session_label="R",
        parent_pid=88,
    )
    _seed_role_binding(state, role="R", agi=LIVE_AGI)
    svc.persist_role_message(
        recipient_kind=RECIPIENT_KIND_ROLE,
        recipient_key="R",
        message_id="agm-r3",
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-sender",
        sender_session_label="Sender",
        important=True,
        content=[TextPart(type="text", text="IMPORTANT: mcp catch-up")],
    )
    client = _routes_client(mgr, reg, svc, state)
    client.get(
        f"/api/v1/bridge/{live_bridge}/peer/inbox"
        "?include_important=true&limit=50",
    )
    _check(
        _row(state, TABLE_AGENT_ROLE_MESSAGE, "agm-r3")["consumed"] is False,
        "A3: a NON-watcher inbox read consumes nothing (drain reconcile stays"
        " the MCP authority)",
    )


def test_peer_send_by_name_route_dispatches_from_bridge_identity() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    mgr = _bridge_manager()
    reg = _peer_registry()
    sender_bridge = mgr.open(homunculus_name="", parent_pid=66).bridge_id
    watcher_bridge = mgr.open(homunculus_name="", parent_pid=77).bridge_id
    _register_binding(
        reg,
        bridge_id=sender_bridge,
        agent_instance_id="agi-route-sender",
        session_label="Sender",
        parent_pid=66,
    )
    _register_binding(
        reg,
        bridge_id=watcher_bridge,
        agent_instance_id=WATCHER_AGI,
        session_label="R",
        parent_pid=77,
    )
    _seed_role_binding(state, role="R", agi=WATCHER_AGI)
    client = _routes_client(mgr, reg, svc, state)
    response = client.post(
        f"/api/v1/bridge/{sender_bridge}/peer/send_by_name",
        json={"name": "R", "content": "IMPORTANT: route role"},
    )
    payload = response.json()
    row = state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)[0]
    _check(
        response.status_code == 200
        and payload["delivery"] == DELIVERY_QUEUED_WATCHER
        and payload["resolved_agent_instance_id"] == WATCHER_AGI,
        "A4: peer_send_by_name HTTP route resolves the role holder",
    )
    _check(
        row["sender_agent_instance_id"] == "agi-route-sender"
        and row["sender_session_label"] == "Sender"
        and row["important"] is True,
        "A4: peer_send_by_name HTTP route stamps sender bridge identity",
    )


def main() -> None:
    print("=== watcher labelling + events-ack consumption smoke ===")
    test_labelling()
    test_events_ack_consumption()
    test_events_ack_is_watcher_only()
    test_inbox_catchup_consumption()
    test_inbox_default_catchup_returns_direct_important()
    test_inbox_helper_consumes_direct_entries()
    test_inbox_is_watcher_only()
    test_peer_send_by_name_route_dispatches_from_bridge_identity()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
