#!/usr/bin/env python3
"""A raw send naming a bound role refuses with a pointer to peer_send_by_name.

Covers iss_f6fea921. A raw send whose peer id or instance id is a bound role
label fails before anything persists, and the error names peer_send_by_name.
An unbound label keeps its original error, a live explicit id still delivers,
a role-addressed send still delivers, and a fault in the role lookup keeps the
original unreachable error instead of escaping.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.state_results import StateOperationError  # noqa: E402
from role_dispatch_smoke import _dispatch  # noqa: E402
from sender_session_reply_fallback_smoke import _Harness  # noqa: E402

from agent_messaging_plugin import peer_dispatch  # noqa: E402
from agent_messaging_plugin.http_routes import PeerSendBody, _peer_send_impl  # noqa: E402
from agent_messaging_plugin.peer_dispatch import DELIVERY_QUEUED_NOTIFICATION  # noqa: E402
from agent_messaging_plugin.role_binding_store import HolderClaim, claim_role_binding_v4  # noqa: E402

_ROLE = "Git-Controller"


def _bound_state() -> RealShapeState:
    state = RealShapeState()
    claim_role_binding_v4(
        cast(Any, state),
        name=_ROLE,
        claim=HolderClaim(
            holder_kind="session",
            holder_identity={"agent_id": "claude_code", "session_label": "Controller"},
            agent_instance_id="agi-current",
            agent_session_id="ases-current",
            session_label="Controller",
        ),
    )
    return state


def _send(
    h: _Harness,
    state: Any,
    *,
    peer_id: str,
    instance_id: str | None,
) -> tuple[int, dict[str, Any]]:
    response = _peer_send_impl(
        bridge_id=h.sender_bridge_id,
        body=PeerSendBody(
            peer_id=peer_id,
            peer_agent_instance_id=instance_id,
            content=[{"type": "text", "text": "raw send"}],
        ),
        bridge_manager=h.manager,
        peer_registry=h.registry,
        agent_messaging_service=h.service,
        state_service=state,
    )
    payload = json.loads(bytes(response.body))
    assert isinstance(payload, dict)
    return response.status_code, payload


class _RoleLookupFault:
    """A bound-role state whose role_binding reads fail; other reads are served."""

    def __init__(self, inner: RealShapeState) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        if query.get("table") == "role_binding":
            return {"action_status": "failed"}
        return self._inner.query_state(namespace, query)


class RawTargetTests(unittest.TestCase):
    def test_origin_is_this_tree(self) -> None:
        origin = Path(peer_dispatch.__file__).resolve()
        self.assertTrue(origin.is_relative_to(REPO_ROOT), str(origin))

    def test_role_label_in_peer_id_points(self) -> None:
        h = _Harness()
        status, payload = _send(h, _bound_state(), peer_id=_ROLE, instance_id=None)
        self.assertEqual(status, 404)
        self.assertIn(
            "peer_role_target",
            payload["message"],
            "ROLE_TARGET_POINTS_TO_BY_NAME",
        )
        self.assertIn(f"peer_send_by_name(name='{_ROLE}')", payload["message"])
        self.assertEqual(payload["code"], "peer_unreachable")
        self.assertEqual(h.service.requests, [])

    def test_role_label_in_instance_slot_points(self) -> None:
        h = _Harness()
        status, payload = _send(
            h,
            _bound_state(),
            peer_id="claude_code",
            instance_id=_ROLE,
        )
        self.assertEqual(status, 404)
        self.assertIn("peer_role_target", payload["message"])
        self.assertIn(f"peer_send_by_name(name='{_ROLE}')", payload["message"])
        self.assertEqual(payload["code"], "peer_unreachable")
        self.assertEqual(h.service.requests, [])

    def test_unbound_label_keeps_original(self) -> None:
        h = _Harness()
        state = _bound_state()
        status, payload = _send(
            h,
            state,
            peer_id="claude_code",
            instance_id="agi-never-registered",
        )
        self.assertEqual(status, 404)
        self.assertEqual(
            payload["message"],
            "peer_unreachable: no binding for 'claude_code'/'agi-never-registered'",
        )
        self.assertNotIn("peer_role_target", payload["message"])
        status, payload = _send(h, state, peer_id="Nope", instance_id=None)
        self.assertEqual(status, 404)
        self.assertTrue(
            payload["message"].startswith(
                "peer agent_id 'Nope' is not currently registered",
            ),
        )
        self.assertNotIn("peer_role_target", payload["message"])
        self.assertEqual(h.service.requests, [])

    def test_live_explicit_id_still_delivers(self) -> None:
        h = _Harness()
        h.add_target(instance_id="agi-live", session_id="ases-live", label="roleless")
        status, _payload = _send(
            h,
            _bound_state(),
            peer_id="claude_code",
            instance_id="agi-live",
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(h.service.requests), 1)

    def test_role_send_by_name_delivers(self) -> None:
        outcome, service, _manager = _dispatch(
            text="IMPORTANT: ping",
            online=True,
            adapter=None,
        )
        self.assertEqual(outcome.delivery, DELIVERY_QUEUED_NOTIFICATION)
        self.assertEqual(len(service.persisted), 1)

    def test_lookup_fault_keeps_original(self) -> None:
        h = _Harness()
        state = _RoleLookupFault(_bound_state())
        try:
            status, payload = _send(h, state, peer_id=_ROLE, instance_id=None)
        except StateOperationError:
            self.fail("LOOKUP_FAULT_ESCAPED")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "peer_unreachable")
        self.assertTrue(
            payload["message"].startswith(
                f"peer agent_id '{_ROLE}' is not currently registered",
            ),
        )
        self.assertNotIn("peer_role_target", payload["message"])
        self.assertEqual(h.service.requests, [])


if __name__ == "__main__":
    unittest.main()
