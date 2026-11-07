"""Authenticate the current coordinator-role holder for assigned dispatches.

Original spawning identity is immutable provenance. Role handover grants the
verified holder resolution authority without requiring historical session data.
Prospective creation still requires the original spawning instance.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import HOLDER_KIND_SESSION

from .managed_dispatch_errors import DispatchError
from .role_binding_store import UNCLAIMED_SESSION_ID, RoleBindingVacantError, resolve_role_binding

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .managed_dispatch import DispatchActor

SUCCESSION_PAYLOAD_KEY = "_managed_dispatch_coordinator_succession"


def _identity(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value != UNCLAIMED_SESSION_ID


def _require_current_binding(state: StateManagementInterface, role: str, actor: DispatchActor) -> None:
    if not _identity(actor.agent_instance_id) or not _identity(actor.agent_session_id):
        raise DispatchError("coordinator_authority_denied", "Caller has no authenticated instance/session identity.")
    try:
        binding = resolve_role_binding(state, role)
    except RoleBindingVacantError as exc:
        raise DispatchError("coordinator_authority_denied", "The spawning coordinator role is vacant.") from exc
    if binding.holder_kind != HOLDER_KIND_SESSION or binding.agent_instance_id != actor.agent_instance_id or binding.agent_session_id != actor.agent_session_id:
        raise DispatchError("coordinator_authority_denied", "Caller does not hold the exact current coordinator binding.")


def coordinator_succession_proof(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    actor: DispatchActor,
) -> dict[str, str] | None:
    """Check authority before CAS or replay; derive immutable event evidence.

    Prospective specs have no assignment and cannot use the successor path.
    Genuine binding/state-operation failures propagate. Historical session
    data is not authority. There is no fallible lookup after projection CAS.
    """
    _require_current_binding(state, str(row["spawned_by_role"]), actor)
    original_instance = str(row["spawned_by_instance_id"])
    if actor.agent_instance_id == original_instance:
        return None
    if row.get("assignment") is None:
        raise DispatchError("coordinator_authority_denied", "Initial creation requires the original spawning instance.")
    return {"proof_kind": "current_role_holder", "original_instance_id": original_instance, "caller_instance_id": actor.agent_instance_id, "caller_session_id": actor.agent_session_id, "coordinator_role": str(row["spawned_by_role"])}
