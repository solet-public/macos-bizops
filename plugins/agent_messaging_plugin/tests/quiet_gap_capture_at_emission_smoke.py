#!/usr/bin/env python3
"""QUIET-GAP capture-at-emission (H2 fix) — the four ruled fixture legs.

Architect's ruling `workbench/2026-08-01_architect_deaf_wake_h1_h2_fix_shape_ruling.md`
§Ruling 1(f). The defect: guard 2 was evaluated at DRAIN time against the bridge's
LIVE ``prev_model_activity_at``, which ``stamp_model_activity`` slides forward on
every model call. The consumption predicate was therefore

    prev_model_activity_at + 45s <= last_emitted_at < last_model_activity_at

with ``prev`` only ever moving FORWARD — so an emission was consumable only in the
single drain window between the recipient's FIRST and SECOND post-emission model
call. A session issuing two tool calls inside one 15s repair tick missed it, and a
recipient who provably read and acted on the message was recorded as deaf
(measured false-positive rate ~1 in 3 against peers replying within ~2 minutes).

The fix freezes the datum per-emission: ``activity_at_emission`` is captured on the
row at EVERY emission, and guard 2 reads that instead. Guard 1 still reads the live
stamp — a condition that only becomes MORE true, so once the window opens it never
closes.

LEGS (the ruling's (f)1-4):

  A  the defect leg, inverted — drain after TWO post-emission model calls CONSUMES.
     Route-driven end to end, so it exercises the real middleware + drain.
  B  the WRITE path — ``/peer/delivered`` stamps the recipient's current
     ``last_model_activity_at`` onto the row. Without this, A could pass off a
     column nothing populates.
  C  the PRESERVED NEGATIVE, and the one that matters most: an emission landing
     MID-TURN (captured stamp < 45s before it) still does NOT consume. Guard 2
     exists to stop busy-session silent loss; a fix that merely disabled it would
     pass every other leg here.
  D  a LEGACY row (NULL capture) consumes on first post-emission activity —
     binding (c)'s deliberate over-consume of the pre-migration population.
  E  RE-EMIT REFRESH (binding (a)) — a row busy at emit 1 stays owed, and once a
     later emission is captured in a genuine idle gap it consumes. Capture-at-
     creation-only would strand it forever, which would be a NEW false-alarm class
     replacing the old one.

Legs A/B drive the real FastAPI routes. Legs C/D/E set the captured column
directly: they are testing the READ predicate against specific captured values, and
B is what proves the writer populates it. Deliberately NOT using
``re_emit_window_seconds=0``: at a zero window every re-emit is confirmed
microseconds after a burst of model calls, which would make the busy case true by
construction rather than by measurement.

Run:
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/quiet_gap_capture_at_emission_smoke.py
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
from ananta.llm.agent_messaging.repository import (  # noqa: E402
    AgentMessagingRepository,
)
from ananta.llm.agent_messaging.role_binding import TABLE_ROLE_BINDING  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    COL_ACTIVITY_AT_EMISSION,
    NAMESPACE,
    TABLE_AGENT_DIRECT_WAKE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.service import (  # noqa: E402
    TURN_BOUNDARY_QUIET_S,
    AgentMessagingService,
)
from ananta.services.store import Store, open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import (  # noqa: E402
    API_PREFIX,
    register_routes,
)
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.route_activity import (  # noqa: E402
    make_model_activity_middleware,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_ROLE_BINDING_NS = "agent_messaging_plugin"
ROLE = "Coordinator-Test"
SESSION_ID = "ases-quiet-gap"
AGI = "agi-quiet-gap-recipient"

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


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()
    max_message_bytes = 65_536


class _RouteConfig:
    long_poll_timeout_seconds = 1
    re_emit_cap = 3
    re_emit_window_seconds = 300.0


def _service(state: RealShapeState, clock: _Clock) -> AgentMessagingService:
    state.now_iso = lambda: clock.now.isoformat()
    return AgentMessagingService(
        repository=AgentMessagingRepository(cast(StateManagementInterface, state)),
        state_service=cast(StateManagementInterface, state),
        backend_router=cast(Any, object()),
        flow_manager=cast(Any, object()),
        action_factory=cast(Any, object()),
        compilation_context_builder=cast(Any, object()),
        bridge_delivery=cast(Any, object()),
        config=cast(Any, _EnabledConfig()),
        clock=clock,
    )


class _Fixture:
    """A registered recipient holding ROLE, with one owed role + direct row."""

    def __init__(self, *, activity_at_emission: datetime | None) -> None:
        self.state = RealShapeState()
        self.real_now = datetime.now(UTC)
        self.clock = _Clock(self.real_now)
        self.service = _service(self.state, self.clock)
        self.manager = BridgeSessionManager(
            session_id_factory=lambda _n: "ags-http",
            idle_timeout_s=3600,
            max_pending_events=50,
            long_poll_timeout_s=1,
        )
        store: Store = open_store(
            get_peer_binding_schema(),
            namespace=PEER_BINDING_NAMESPACE,
            backend="in_memory",
        )
        self.registry = PeerRegistry(bindings_store=store)
        app = FastAPI()
        app.middleware("http")(
            make_model_activity_middleware(self.manager, self.registry),
        )
        register_routes(
            app,
            bridge_manager=self.manager,
            peer_registry=self.registry,
            platform_surface=cast(Any, object()),
            agent_messaging_service=self.service,
            config=_RouteConfig(),
            state_service=cast(StateManagementInterface, self.state),
        )
        self.client = TestClient(app)
        self.bridge_id = self.manager.open(
            homunculus_name="", parent_pid=4242,
        ).bridge_id
        response = self.client.post(
            f"{API_PREFIX}/{self.bridge_id}/peer/register",
            json={
                "agent_id": "claude_code",
                "agent_instance_id": AGI,
                "session_label": ROLE,
                "agent_session_id": SESSION_ID,
            },
        )
        if response.status_code != 200:
            msg = f"register returned {response.status_code}"
            raise AssertionError(msg)
        self._seed_role_binding()
        # The session's activity history BEFORE the wake arrived. Only the
        # pre-emission history is hand-set; every stamp after it is produced by
        # the production stamp_model_activity via real model-route calls.
        bridge = self.manager.get(self.bridge_id)
        if bridge is None:
            msg = "bridge vanished"
            raise AssertionError(msg)
        self.bridge = bridge
        bridge.prev_model_activity_at = (
            self.real_now - timedelta(seconds=600)
        ).isoformat()
        bridge.last_model_activity_at = (
            self.real_now - timedelta(seconds=60)
        ).isoformat()
        self.emitted_at = self.real_now - timedelta(seconds=1)
        self._seed_role_row(activity_at_emission)
        self._persist_direct_row(activity_at_emission)

    def _seed_role_binding(self) -> None:
        self.state.rows(_ROLE_BINDING_NS, TABLE_ROLE_BINDING).append(
            {
                "id": f"rbn-{AGI}",
                "external_id": f"role:{ROLE}",
                "role": ROLE,
                "holder_kind": "session",
                "agent_instance_id": AGI,
                "agent_session_id": SESSION_ID,
                "holder_identity": {
                    "agent_id": "claude_code", "session_label": ROLE,
                },
                "claim_epoch": 1,
                "claimed_at": self.real_now.isoformat(),
                "is_deleted": 0,
            },
        )

    def _seed_role_row(self, activity_at_emission: datetime | None) -> None:
        self.state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE).append(
            {
                "id": "arm-quiet-gap",
                "external_id": f"role:{ROLE}:agm-role",
                "recipient_kind": "role",
                "recipient_key": ROLE,
                "message_id": "agm-role",
                "sender_agent_id": "claude_code",
                "sender_agent_instance_id": "agi-sender",
                "sender_bridge_id": "agc-sender",
                "important": True,
                "delivered": True,
                "consumed": False,
                "escalated": False,
                "emit_count": 1,
                "emitted_to_agent_instance_id": AGI,
                "last_emitted_at": self.emitted_at.isoformat(),
                COL_ACTIVITY_AT_EMISSION: (
                    activity_at_emission.isoformat()
                    if activity_at_emission is not None
                    else None
                ),
                "content": [{"type": "text", "text": "IMPORTANT: act"}],
                "created_at": self.emitted_at.isoformat(),
                "is_deleted": 0,
            },
        )

    def _persist_direct_row(self, activity_at_emission: datetime | None) -> None:
        restore = self.clock.now
        self.clock.now = self.emitted_at
        self.service.persist_direct_wake(
            message_id="agm-direct",
            thread_id="agt-quiet-gap",
            recipient_agent_id="claude_code",
            recipient_agent_instance_id=AGI,
            recipient_agent_session_id=SESSION_ID,
            sender_agent_id="claude_code",
            sender_agent_instance_id="agi-sender",
            sender_session_label="Sender",
            sender_bridge_id="agc-sender",
            content=[TextPart(type="text", text="IMPORTANT: act")],
            activity_at_emission=(
                activity_at_emission.isoformat()
                if activity_at_emission is not None
                else None
            ),
        )
        self.clock.now = restore

    def model_call(self) -> None:
        response = self.client.get(f"{API_PREFIX}/{self.bridge_id}/peer/list")
        if response.status_code != 200:
            msg = f"peer/list returned {response.status_code}"
            raise AssertionError(msg)

    def drain(self) -> None:
        response = self.client.post(
            f"{API_PREFIX}/{self.bridge_id}/peer/drain", json={"limit": 50},
        )
        if response.status_code != 200:
            msg = f"drain returned {response.status_code}"
            raise AssertionError(msg)

    def row(self, table: str, message_id: str) -> dict[str, Any]:
        matches = [
            r
            for r in self.state.rows(NAMESPACE, table)
            if r.get("message_id") == message_id and not r.get("is_deleted")
        ]
        if len(matches) != 1:
            msg = f"{len(matches)} rows for {message_id} in {table}"
            raise AssertionError(msg)
        return matches[0]

    def consumed(self) -> tuple[bool, bool]:
        return (
            bool(self.row(TABLE_AGENT_ROLE_MESSAGE, "agm-role").get("consumed")),
            bool(self.row(TABLE_AGENT_DIRECT_WAKE, "agm-direct").get("consumed")),
        )


def test_a_two_calls_now_consume() -> None:
    """(f)(1) The defect leg, inverted: TWO post-emission calls now consume."""
    print("\nA — drain after TWO post-emission model calls")
    fx = _Fixture(activity_at_emission=datetime.now(UTC) - timedelta(seconds=61))
    fx.model_call()
    fx.model_call()
    fx.drain()
    role_consumed, direct_consumed = fx.consumed()
    _check(
        role_consumed and direct_consumed,
        "A: both rows consume after two post-emission model calls — the "
        "sliding-pair race is closed (this leg was the measured defect)",
    )


def test_b_delivered_route_writes_the_capture() -> None:
    """The WRITE path: /peer/delivered stamps the recipient's current activity."""
    print("\nB — /peer/delivered captures the recipient's activity stamp")
    fx = _Fixture(activity_at_emission=None)
    fx.model_call()  # produces a real stamp via the production middleware
    expected = fx.bridge.last_model_activity_at
    response = fx.client.post(
        f"{API_PREFIX}/{fx.bridge_id}/peer/delivered",
        json={"external_id": f"role:{ROLE}:agm-role", "recipient_key": ROLE},
    )
    _check(response.status_code == 200, "B: /peer/delivered accepted the confirm")
    _check(
        str(fx.row(TABLE_AGENT_ROLE_MESSAGE, "agm-role").get(
            COL_ACTIVITY_AT_EMISSION,
        ) or "") == expected,
        "B: the confirm froze the recipient's CURRENT last_model_activity_at "
        "onto the row (without this, leg A could pass off an unpopulated column)",
    )


def test_c_mid_turn_emission_still_does_not_consume() -> None:
    """(f)(2) THE PRESERVED NEGATIVE — the leg a guard-disabling fix would fail."""
    print("\nC — an emission landing MID-TURN must still NOT consume")
    # Captured stamp only 10s before the emission: the recipient was mid-turn, so
    # the emission cannot be proven to have STARTED a turn.
    fx = _Fixture(activity_at_emission=datetime.now(UTC) - timedelta(seconds=11))
    fx.model_call()
    fx.drain()
    role_consumed, direct_consumed = fx.consumed()
    _check(
        not role_consumed and not direct_consumed,
        "C: neither row consumes — guard 2 still refuses a mid-turn emission, "
        "so busy-session silent loss stays prevented",
    )


def test_d_legacy_null_capture_consumes() -> None:
    """(f)(4) Binding (c): pre-migration rows read NULL and over-consume."""
    print("\nD — a legacy row (NULL capture) consumes on first activity")
    fx = _Fixture(activity_at_emission=None)
    fx.model_call()
    fx.drain()
    role_consumed, direct_consumed = fx.consumed()
    _check(
        role_consumed and direct_consumed,
        "D: a NULL capture keeps the None-rule (whole preceding session is the "
        "quiet gap) and clears the legacy population instead of flooding",
    )


def test_e_re_emit_refresh_rescues_a_busy_first_emit() -> None:
    """(f)(3) Binding (a): the capture REFRESHES per emission.

    Busy at emit 1 ⇒ stays owed (leg C's behavior). Once a later emission is
    captured in a genuine idle gap, it consumes. Captured only at row creation,
    this row would be unconsumable forever — a NEW false-alarm class replacing
    the old one, which is precisely what binding (a) exists to prevent.
    """
    print("\nE — re-emit refresh rescues a row that was busy at emit 1")
    fx = _Fixture(activity_at_emission=datetime.now(UTC) - timedelta(seconds=11))
    fx.model_call()
    fx.drain()
    _check(
        not fx.consumed()[0],
        "E: emit 1 landed mid-turn, so the row is still owed",
    )
    # Emit 2, captured after a genuine idle gap (what a later drain records when
    # the recipient has since fallen quiet).
    now = datetime.now(UTC)
    fx.service.mark_delivered_for_instance(
        external_id=f"role:{ROLE}:agm-role",
        recipient_key=ROLE,
        agent_instance_id=AGI,
        agent_session_id=SESSION_ID,
        activity_at_emission=(
            now - timedelta(seconds=TURN_BOUNDARY_QUIET_S + 60)
        ).isoformat(),
    )
    fx.model_call()
    fx.drain()
    _check(
        fx.consumed()[0],
        "E: after the refreshed capture lands in an idle gap the row CONSUMES — "
        "capture-at-creation-only would have stranded it forever",
    )


def main() -> int:
    print("=== QUIET-GAP capture-at-emission (H2 fix) — ruled fixture legs ===")
    test_a_two_calls_now_consume()
    test_b_delivered_route_writes_the_capture()
    test_c_mid_turn_emission_still_does_not_consume()
    test_d_legacy_null_capture_consumes()
    test_e_re_emit_refresh_rescues_a_busy_first_emit()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
