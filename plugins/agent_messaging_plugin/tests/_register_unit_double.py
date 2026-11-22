"""Shared role-binding fixtures for messaging smokes."""

from __future__ import annotations

from typing import Any

from _real_state_fake import RealShapeState
from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_ROLE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)

from agent_messaging_plugin.session_lifecycle_verbs import DispatchActor

FIXTURE_UNIT_ID = "unt_fixture-dispatch"


def seed_binding(state: Any, *, role: str, instance: str, session: str) -> None:
    """Bind ``role`` to a live session in the fixture's role-binding table."""
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                COL_ROLE: role,
                COL_HOLDER_KIND: HOLDER_KIND_SESSION,
                COL_AGENT_INSTANCE_ID: instance,
                COL_AGENT_SESSION_ID: session,
                COL_HOLDER_IDENTITY: {"agent_id": "codex", "session_label": role},
                COL_CLAIM_EPOCH: 1,
            },
            "conflict_columns": ["external_id"],
        },
    )


def coordinator_state() -> Any:
    """A real-shaped state holding a worker binding and the coordinator binding."""
    state = RealShapeState()
    seed_binding(state, role="Codex-Fixture-Builder", instance="agi-worker-1", session="ases-worker-1")
    seed_binding(state, role="Coordinator-Main", instance="agi-coordinator", session="ases-coordinator")
    return state


def coordinator_actor() -> DispatchActor:
    return DispatchActor(
        agent_instance_id="agi-coordinator",
        agent_session_id="ases-coordinator",
        authority_source="live_peer_binding",
    )
