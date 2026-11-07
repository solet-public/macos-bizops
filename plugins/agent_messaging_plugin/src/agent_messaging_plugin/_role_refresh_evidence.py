"""Request-local reconnect observations, captured before registry replacement.

These observations are not authenticated launch credentials. Missing attribution
is advisory: the session-key rotation remains available and is explicitly logged.
Evidence is immutable, consumed for one refresh, then discarded; no durable
evidence lifecycle or ownership history is introduced.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
)
from ananta.services.state_service.bounded_read import iter_table_rows

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RoleRefreshEvidence:
    """Independent predecessor identity and observed parent continuity, if known."""

    expected_agent_instance_id: str | None
    predecessor_parent_pid: int | None
    registering_parent_pid: int | None
    reason: str = ""

    def contradicts(self, new_agent_instance_id: str) -> bool:
        """Same-instance rearm needs no subprocess-parent continuity assertion."""
        return (
            self.expected_agent_instance_id is not None
            and self.expected_agent_instance_id != new_agent_instance_id
            and self.predecessor_parent_pid is not None
            and self.registering_parent_pid is not None
            and self.predecessor_parent_pid != self.registering_parent_pid
        )

    def missing(self) -> str:
        """Name every absent observation without manufacturing an ownership fact."""
        fields = {
            "expected_agent_instance_id": self.expected_agent_instance_id,
            "predecessor_parent_pid": self.predecessor_parent_pid,
            "registering_parent_pid": self.registering_parent_pid,
        }
        return ",".join(name for name, value in fields.items() if value is None)


def capture_role_refresh_evidence(
    registry: PeerRegistry, *, agent_session_id: str, registering_parent_pid: int | None,
) -> RoleRefreshEvidence:
    """Read under PEER_REGISTRATION_LOCK, before register sweeps the old row."""
    predecessor = None
    reason = "no stable session key"
    if agent_session_id and agent_session_id != "__unclaimed__":
        try:
            predecessor = registry.resolve_by_agent_session_id(agent_session_id)
            reason = "no predecessor peer row" if predecessor is None else ""
        except Exception as exc:  # noqa: BLE001 — evidence faults must not refuse registration
            reason = f"predecessor lookup failed: {type(exc).__name__}: {exc}"
            logger.exception("role refresh evidence: session=%r %s", agent_session_id, reason)
    return RoleRefreshEvidence(
        expected_agent_instance_id=(predecessor.agent_instance_id or None) if predecessor else None,
        predecessor_parent_pid=predecessor.parent_pid if predecessor else None,
        registering_parent_pid=registering_parent_pid,
        reason=reason,
    )


def advise_missing_attribution(
    state: StateManagementInterface, *, agent_session_id: str,
    new_agent_instance_id: str, evidence: RoleRefreshEvidence,
) -> None:
    """Diagnostic enumeration alone is bounded; failure never suppresses rotation."""
    missing = evidence.missing()
    if not missing:
        return
    filters: dict[str, object] = {
        COL_AGENT_SESSION_ID: agent_session_id, COL_HOLDER_KIND: HOLDER_KIND_SESSION,
    }
    if evidence.expected_agent_instance_id is not None:
        filters[COL_AGENT_INSTANCE_ID] = evidence.expected_agent_instance_id
    role = "<none>"
    try:
        for row in iter_table_rows(
            state, namespace=AGENT_ROLE_BINDING_NAMESPACE, table=TABLE_ROLE_BINDING,
            filters=filters, ceiling=1000,
            reason="diagnostic-only role attribution enumeration; incomplete evidence must not block rotation",
        ):
            role = str(row.get(COL_ROLE) or "<unread>")
            _log_absence(role, agent_session_id, new_agent_instance_id, evidence, missing)
        if role == "<none>":
            _log_absence(role, agent_session_id, new_agent_instance_id, evidence, missing)
    except Exception as exc:  # noqa: BLE001 — diagnostic-only, never an availability gate
        _log_absence("<unread>", agent_session_id, new_agent_instance_id, evidence, missing)
        logger.exception("role attribution diagnostic evidence incomplete: %s: %s", type(exc).__name__, exc)


def _log_absence(
    role: str, session: str, new: str, evidence: RoleRefreshEvidence, missing: str,
) -> None:
    logger.error(
        "role attribution unverified: role=%s session=%s expected=%s new=%s missing=%s reason=%s; %s",
        role, session, evidence.expected_agent_instance_id, new, missing, evidence.reason,
        "session-key-based rotation" if evidence.expected_agent_instance_id is None else "observed-instance rotation",
    )
