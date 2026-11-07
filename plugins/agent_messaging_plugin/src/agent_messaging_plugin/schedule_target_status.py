"""Owner-only schedule destination observations without lifecycle side effects."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ananta.interfaces.agent_messaging_service_interface import ScheduleTarget, ScheduleTargetObservation
from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_records

from .peer_registry import PeerRegistry
from .role_binding_store import RoleBindingVacantError, resolve_role_binding
from .schema import TABLE_MANAGED_SESSION
from .session_lifecycle_store import SessionNotFoundError, read_managed_session

RELOAD_SAFE = True


def _session_instance(state: StateManagementInterface, registry: PeerRegistry | None, session_id: str) -> str:
    binding = registry.resolve_by_agent_session_id(session_id) if registry is not None else None
    if binding is not None:
        return binding.agent_instance_id
    rows = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {"agent_session_id": session_id, "is_deleted": 0},
                "limit": 2,
            },
        )
    )
    if not rows:
        raise SessionNotFoundError(session_id)
    if len(rows) != 1:
        raise ValueError("ambiguous_session_generations")
    return str(rows[0]["agent_instance_id"])


def _resolve_instance(state: StateManagementInterface, registry: PeerRegistry | None, target: ScheduleTarget) -> str:
    kind, value = target["kind"], target["value"]
    if kind == "role":
        resolved = resolve_role_binding(state, value)
        if not resolved.agent_instance_id:
            raise ValueError("role_holder_has_no_instance")
        return resolved.agent_instance_id
    elif kind == "session":
        return _session_instance(state, registry, value)
    elif kind == "instance":
        return value
    else:
        raise ValueError("unsupported_target_kind")


def _observe(state: StateManagementInterface, registry: PeerRegistry | None, target: ScheduleTarget, result: ScheduleTargetObservation) -> None:
    instance = _resolve_instance(state, registry, target)
    result["resolved_instance"] = instance
    row: dict[str, Any] = read_managed_session(state, instance)
    lifecycle = str(row.get("lifecycle_state") or "")
    result["lifecycle_state"] = lifecycle or None
    if lifecycle == "retired":
        result.update(status="retired", delivery_route="terminal")
        return
    binding = registry.resolve_by_agent_instance_id(instance) if registry is not None else None
    result["delivery_route"] = "registered" if binding is not None else "unbound" if registry is not None else "unavailable"
    if lifecycle in ("live", "idle") and binding is not None:
        result["status"] = "live"
    elif lifecycle:
        result["status"] = "disconnected"
    else:
        result.update(status="unavailable", advisory="lifecycle_state_missing")


def observe_targets(state: StateManagementInterface | None, registry: PeerRegistry | None, targets: list[ScheduleTarget]) -> list[ScheduleTargetObservation]:
    """Cache distinct targets within one read; a failed read is never vacancy."""
    observed_at = datetime.now(UTC).isoformat()
    cached: dict[tuple[str, str], ScheduleTargetObservation] = {}
    for target in targets:
        key = (target["kind"], target["value"])
        if key in cached:
            continue
        result: ScheduleTargetObservation = {
            **target,
            "status": "unavailable",
            "observed_at": observed_at,
            "resolved_instance": None,
            "lifecycle_state": None,
            "delivery_route": "unavailable",
            "advisory": None,
        }
        try:
            if state is None:
                raise RuntimeError("state_service_unavailable")
            _observe(state, registry, target, result)
        except RoleBindingVacantError:
            result.update(status="vacant", delivery_route="unbound")
        except SessionNotFoundError:
            result.update(status="not_found", advisory="identity_not_in_lifecycle_ledger")
        except Exception as exc:
            result.update(status="unavailable", advisory=str(exc))
        cached[key] = result
    return [cached[(target["kind"], target["value"])].copy() for target in targets]
