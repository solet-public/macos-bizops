"""Durable first-GO enqueue for native spawn ownership release."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol

from ananta.llm.agent_messaging.models import PeerSendRequest, PeerSendResult, TextPart
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import StateOperationError, require_updated

from .register_unit_client import RegisterActor, VerbError
from .schema import TABLE_MANAGED_SESSION
from .session_lifecycle_store import read_managed_session

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


FIRST_GO_RECEIPT_KEY = "native_spawn_first_go"
FIRST_GO_RECEIPT_SCHEMA = "native-spawn-first-go/v1"


class SpawnFirstGoRequest(Protocol):
    @property
    def first_go(self) -> str: ...

    @property
    def agent_runtime(self) -> str: ...


class SpawnFirstGoMessagingPort(Protocol):
    """Existing native messaging boundary used for pre-release spawn mail."""

    def peer_send(self, request: PeerSendRequest) -> PeerSendResult: ...


def _persist_receipt(state: StateManagementInterface, instance: str, receipt: Mapping[str, Any]) -> None:
    row = read_managed_session(state, instance)
    report = dict(row.get("capability_report") or {})
    report[FIRST_GO_RECEIPT_KEY] = dict(receipt)
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {"agent_instance_id": instance, "is_deleted": 0},
            },
            {"capability_report": report},
        )
    )
    if updated != 1:
        raise StateOperationError(f"state update did not persist first_go receipt for {instance!r} (updated={updated})")


def _stored_receipt(row: Mapping[str, Any]) -> dict[str, Any] | None:
    report = row.get("capability_report")
    receipt = report.get(FIRST_GO_RECEIPT_KEY) if isinstance(report, Mapping) else None
    return dict(receipt) if isinstance(receipt, Mapping) else None


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reuse_receipt(req: SpawnFirstGoRequest, row: Mapping[str, Any], receipt: dict[str, Any]) -> dict[str, Any]:
    if req.first_go and receipt.get("sha256") != _hash(req.first_go):
        raise VerbError(
            "first_go_payload_conflict",
            "A retained spawn already accepted different first_go bytes; the accepted payload is immutable.",
        )
    if receipt.get("recipient_agent_instance_id") != row.get("agent_instance_id"):
        raise VerbError(
            "first_go_receipt_mismatch",
            "Retained first_go receipt names a different recipient instance.",
        )
    return receipt


def _send_and_record(
    state: StateManagementInterface,
    req: SpawnFirstGoRequest,
    row: Mapping[str, Any],
    actor: RegisterActor,
    messaging: SpawnFirstGoMessagingPort | None,
) -> tuple[dict[str, Any] | None, str]:
    if messaging is None:
        return None, "first_go_messaging_port_unavailable"
    try:
        result = messaging.peer_send(
            PeerSendRequest(
                sender_bridge_id="system:scheduler",
                sender_agent_id="system",
                sender_agent_instance_id=actor.agent_instance_id,
                sender_session_label=actor.role_name or actor.lane_id,
                peer_agent_id=req.agent_runtime,
                peer_agent_instance_id=str(row["agent_instance_id"]),
                content=[TextPart(type="text", text=req.first_go)],
                important=True,
            ),
        )
        message_id = str(getattr(result, "message_id", "") or "")
        if not message_id:
            return None, "first_go_enqueue_failed: native messaging port returned no message_id"
        receipt = {
            "schema": FIRST_GO_RECEIPT_SCHEMA,
            "message_id": message_id,
            "sha256": _hash(req.first_go),
            "recipient_agent_id": req.agent_runtime,
            "recipient_agent_instance_id": str(row["agent_instance_id"]),
        }
        _persist_receipt(state, str(row["agent_instance_id"]), receipt)
    except Exception as exc:  # noqa: BLE001 — unconfirmed enqueue retains the host
        return None, f"first_go_enqueue_failed: {exc}"
    return receipt, ""


def enqueue_first_go(
    state: StateManagementInterface,
    req: SpawnFirstGoRequest,
    row: Mapping[str, Any],
    actor: RegisterActor,
    messaging: SpawnFirstGoMessagingPort | None,
) -> tuple[dict[str, Any] | None, str]:
    """Enqueue exact bytes once, persist the receipt, and report failures."""
    stored = _stored_receipt(row)
    if stored is not None:
        return _reuse_receipt(req, row, stored), ""
    if not req.first_go:
        return None, ""
    return _send_and_record(state, req, row, actor, messaging)


def first_go_failure_result(
    req: SpawnFirstGoRequest,
    row: Mapping[str, Any],
    host: str,
    host_ref: str,
    ownership: Mapping[str, Any],
    error: str,
) -> dict[str, Any]:
    return {
        "agent_instance_id": str(row["agent_instance_id"]),
        "agent_runtime": req.agent_runtime,
        "host": host,
        "host_ref": host_ref,
        "dispatch_id": getattr(req, "dispatch_id", ""),
        "lifecycle_state": str(row.get("lifecycle_state") or ""),
        "first_go_status": "incomplete",
        "first_go_receipt": None,
        "first_go_error": error,
        "first_turn_source": "",
        "first_turn_delivered": False,
        "first_turn_error": "first_go was not durably confirmed; retained host requires recovery",
        "ownership_receipt": dict(ownership),
    }
