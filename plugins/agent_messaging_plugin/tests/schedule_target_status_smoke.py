#!/usr/bin/env python3
"""Fake-only proof that owner evidence distinguishes absent, retired and vacant."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "ananta/src", ROOT / "plugins/agent_messaging_plugin/src"):
    sys.path.insert(0, str(source))

from ananta.interfaces.agent_messaging_service_interface import ScheduleTarget  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE, role_binding_external_id  # noqa: E402

import agent_messaging_plugin  # noqa: E402
import ananta  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schedule_target_status import observe_targets  # noqa: E402


def check(value: object, label: str) -> None:
    assert value, label
    print("PASS", label, flush=True)


class OwnerState:
    def __init__(self) -> None:
        self.fail = False
        self.reads = 0
        self.namespaces: list[str] = []

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.reads += 1
        self.namespaces.append(namespace)
        assert namespace == AGENT_ROLE_BINDING_NAMESPACE, "reads_owner_only"
        if self.fail:
            return {"action_status": "error", "error": "injected_owner_unavailable"}
        filters = query.get("filters", {})
        rows: list[dict[str, Any]] = []
        if query["table"] == "role_binding" and filters.get("external_id") == role_binding_external_id("held"):
            rows = [{"holder_kind": "session", "holder_identity": {"agent_id": "fixture", "session_label": "fixture"}, "agent_instance_id": "live", "agent_session_id": "session-live"}]
        elif query["table"] == "managed_session":
            instance = filters.get("agent_instance_id")
            if instance in ("live", "retired", "disconnected"):
                rows = [{"agent_instance_id": instance, "lifecycle_state": "retired" if instance == "retired" else "live"}]
            elif filters.get("agent_session_id") == "session-retired":
                rows = [{"agent_instance_id": "retired", "lifecycle_state": "retired"}]
        return {"action_status": "completed", "data": {"records": rows}}


class Registry:
    def resolve_by_agent_instance_id(self, instance: str) -> Any:
        return SimpleNamespace(agent_instance_id="live", bridge_id="fixture") if instance == "live" else None

    def resolve_by_agent_session_id(self, session: str) -> Any:
        return self.resolve_by_agent_instance_id("live") if session == "session-live" else None


def check_status() -> None:
    state = OwnerState()
    registry = cast(PeerRegistry, Registry())
    targets: list[ScheduleTarget] = [{"kind": "instance", "value": value} for value in ("missing", "retired", "live", "disconnected")]
    targets += [{"kind": "role", "value": "vacant"}, {"kind": "role", "value": "held"}, {"kind": "session", "value": "session-retired"}]
    result = observe_targets(cast(StateManagementInterface, state), registry, targets)
    check(result[0]["status"] == "not_found", "absent_is_not_retired")
    check(result[1]["status"] == "retired" and result[1]["lifecycle_state"] == "retired", "retired_evidence_preserved")
    check(result[2]["status"] == "live" and result[2]["delivery_route"] == "registered", "owner_liveness_pass")
    check(result[3]["status"] == "disconnected", "disconnected_visible")
    check(result[4]["status"] == "vacant", "vacancy_not_unavailable")
    check(result[5]["resolved_instance"] == "live" and result[6]["status"] == "retired", "role_and_session_owner_resolution")
    check(set(state.namespaces) == {AGENT_ROLE_BINDING_NAMESPACE}, "reads_owner_only")
    state.reads = 0
    repeated = observe_targets(cast(StateManagementInterface, state), registry, [targets[0], targets[0]])
    check(state.reads == 1 and repeated[0] == repeated[1], "distinct_target_cache_per_call")
    state.fail = True
    check(observe_targets(cast(StateManagementInterface, state), registry, [targets[4]])[0]["status"] == "unavailable", "owner_failure_not_vacancy")
    check(observe_targets(None, registry, [targets[1]])[0]["status"] == "unavailable", "unavailable_not_retired")
    # Real plugin's public typed accessor delegates to the same owner helper.
    owner = AgentMessagingPlugin()
    owner._get_state_service = lambda: cast(StateManagementInterface, state)  # type: ignore[method-assign]
    owner._peer_registry = registry
    check(owner.get_schedule_target_status([targets[4]])[0]["status"] == "unavailable", "public_owner_accessor")


def main() -> int:
    for module in (ananta, agent_messaging_plugin):
        check(Path(str(module.__file__)).resolve().is_relative_to(ROOT), "source_provenance_" + module.__name__)
    check_status()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
