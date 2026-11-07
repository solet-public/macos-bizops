#!/usr/bin/env python3
"""Focused regression for managed-dispatch CLI caller attribution."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import RegisterUnitDouble  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from managed_dispatch_smoke import T0, _assignment_identity, _coordinator_actor, _seed_binding, _spec, _state  # noqa: E402

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DispatchActor,
    DispatchError,
    dispatch_managed_work,
    read_managed_dispatch,
    report_managed_dispatch,
    resolve_managed_dispatch,
    validate_resolution_admission,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MANAGED_DISPATCH_EVENT, TABLE_MANAGED_SESSION  # noqa: E402

_passed = 0
_failed: list[str] = []


class _Registry:
    def __init__(self, bindings: dict[str, str]) -> None:
        self.bindings = bindings

    def agent_session_id_for_instance(self, instance: str) -> str:
        return self.bindings.get(instance, "")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _plugin(bindings: dict[str, str]) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._peer_registry = cast(Any, _Registry(bindings))  # noqa: SLF001
    return plugin


def _actor(plugin: AgentMessagingPlugin, state: dict[str, Any]) -> DispatchActor | str:
    try:
        return plugin._dispatch_actor_from_state(state)  # noqa: SLF001
    except DispatchError as exc:
        return exc.code


def _report_actor(plugin: AgentMessagingPlugin) -> DispatchActor:
    captured: list[DispatchActor] = []

    def _report(_: object, **kwargs: Any) -> dict[str, str]:
        captured.append(cast(DispatchActor, kwargs["actor"]))
        return {"projection": "accepted"}

    cast(Any, plugin)._get_state_service = lambda: object()  # noqa: SLF001
    original = plugin_module.lifecycle_report_managed_dispatch
    plugin_module.lifecycle_report_managed_dispatch = _report
    try:
        response = plugin.report_managed_dispatch(
            {
                "dispatch_id": "mdp-cli",
                "event_id": "evt-cli",
                "event_kind": "progress",
                "attempt_agent_instance_id": "agi-cli",
                "prior_version": 1,
                "payload": {},
            },
            {"caller_attribution_instance_id": "agi-cli"},
        )
    finally:
        plugin_module.lifecycle_report_managed_dispatch = original
    if response.get("action_status") != "completed" or len(captured) != 1:
        raise AssertionError(f"report path did not receive one attributed dispatch actor: response={response!r} captured={captured!r}")
    return captured[0]


_SUCCESSION_KEY = "_managed_dispatch_coordinator_succession"


def _successor_fixture(root: Path, session: str = "ases-coordinator", *, bound: bool = True, original_session: str | None = "ases-coordinator") -> tuple[StateManagementInterface, dict[str, Any], DispatchActor, RegisterUnitDouble]:
    # Only the existence marker used by lane_root: no real Git or host operation.
    (root / ".git").mkdir(exist_ok=True)
    state, register = _state(), RegisterUnitDouble()
    register.add_unit()
    launches: list[Mapping[str, Any]] = []

    def launch(row: Mapping[str, Any]) -> Mapping[str, Any]:
        launches.append(row)
        return {"agent_instance_id": "agi-recorded-worker", "host_ref": "recorded-only"}

    row = dispatch_managed_work(state, _spec(root, repository_root=str(root)), register=register, actor=_coordinator_actor(), spawn_attempt=launch, now=T0)["dispatch"]
    assert len(launches) == 1 and row["assignment"]
    if original_session is not None:
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_SESSION, "record": {"external_id": "session:original", "agent_instance_id": "agi-coordinator", "agent_session_id": original_session, "role_name": "Coordinator-Main", "lifecycle_state": "live"}})
    actor = DispatchActor("agi-successor", session, "live_peer_binding")
    _seed_binding(state, role="Coordinator-Main", instance=actor.agent_instance_id if bound else "agi-unrelated", session=session)
    return state, row, actor, register


def _successor_cancel(state: StateManagementInterface, row: Mapping[str, Any], actor: DispatchActor, register: RegisterUnitDouble, *, event: str = "successor-cancel", **overrides: Any) -> dict[str, Any]:
    arguments = {"dispatch_id": row["dispatch_id"], "event_id": event, "action": "cancel", "actor": actor, "prior_version": row["version"], "payload": {"reason": "typed continuity fixture"}, "observed_at": T0, "register": register, **overrides}
    return resolve_managed_dispatch(state, **arguments)


def _successor_events(state: StateManagementInterface) -> list[dict[str, Any]]:
    return cast(RealShapeState, state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH_EVENT)


def _assert_verified_successor_resolution() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state, row, actor, register = _successor_fixture(Path(tmp), "ases-different")
        result = None
        try:
            result = _successor_cancel(state, row, actor, register)
        except DispatchError as exc:
            print(f"  successor cancel rejected: {exc.code}: {exc.message}; session={actor.agent_session_id}")
        _check(result is not None and result["state"] == "cancelled", "COORDINATOR_SUCCESSOR_RESOLVES_PREDECESSOR_DISPATCH")
        if result is None or result["state"] != "cancelled":
            return
        events = list(_successor_events(state))
        accepted = next((event for event in events if event["event_id"] == "successor-cancel"), {})
        proof = accepted.get("payload", {}).get(_SUCCESSION_KEY, {})
        expected = {"proof_kind": "current_role_holder", "coordinator_role": row["spawned_by_role"], "original_instance_id": row["spawned_by_instance_id"], "caller_instance_id": actor.agent_instance_id, "caller_session_id": actor.agent_session_id}
        _check(
            all(
                (
                    _successor_cancel(state, row, actor, register) == result,
                    _successor_events(state) == events,
                    result["version"] == row["version"] + 1,
                    all(result[key] == row[key] for key in ("spawned_by_instance_id", "spawned_by_role", "assignment", "request_sha256")),
                    sum(event["event_id"] == "successor-cancel" for event in events) == 1,
                    accepted.get("accepted") is True,
                    accepted.get("actor_instance_id") == actor.agent_instance_id,
                    proof == expected,
                )
            ),
            "SUCCESSOR_REPLAY_PRESERVES_PROVENANCE",
        )


def _successor_denied(*, session: str = "ases-coordinator", bound: bool = True, notice: bool = False, original_session: str | None = "ases-coordinator") -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        state, row, actor, register = _successor_fixture(Path(tmp), session, bound=bound, original_session=original_session)
        if notice:
            # Genuine-shaped negative data, deliberately outside all authority reads.
            state.write_state("core", {"table": "agent_message", "record": {"external_id": "handover-notice", "sender_instance_id": "agi-coordinator", "message_kind": "role_displaced", "content": {"role_name": "Coordinator-Main", "new_holder_instance_id": actor.agent_instance_id}}})
        with patch.object(state, "query_state", wraps=state.query_state) as queries:
            try:
                _successor_cancel(state, row, actor, register)
            except DispatchError as exc:
                return exc.code == "coordinator_authority_denied" and read_managed_dispatch(state, row["dispatch_id"]) == row and all(call.args[0] != "core" or call.args[1]["table"] not in {"agent_thread", "agent_message"} for call in queries.call_args_list)
        return False


def _assert_disconnected_successor_denials() -> None:
    _check(_successor_denied(bound=False, original_session=None), "SUCCESSOR_UNRELATED_DENIED")
    _check(_successor_denied(session="ases-different", bound=False, notice=True), "SUCCESSOR_NOTICE_HAS_NO_AUTHORITY")
    _check(_successor_denied(bound=False), "SUCCESSOR_SESSION_WITHOUT_BINDING_DENIED")


def _completion_fixture(state: StateManagementInterface, row: Mapping[str, Any]) -> dict[str, Any]:
    worker = DispatchActor(str(row["current_agent_instance_id"]), "ases-worker", "live_peer_binding")
    _seed_binding(state, role=str(row["role_name"]), instance=worker.agent_instance_id, session=worker.agent_session_id)
    active = report_managed_dispatch(
        state,
        dispatch_id=row["dispatch_id"],
        event_id="worker-ack",
        event_kind="ack",
        attempt_agent_instance_id=worker.agent_instance_id,
        actor=worker,
        prior_version=row["version"],
        payload={**_assignment_identity(row), "role_binding": row["role_name"], "scope_readback_sha256": "a" * 64, "plan_sha256": "b" * 64},
        observed_at=T0,
    )
    artifact = Path(row["expected_path"])
    artifact.write_text("exact successor acceptance artifact\n", encoding="utf-8")
    return report_managed_dispatch(
        state,
        dispatch_id=row["dispatch_id"],
        event_id="worker-completion",
        event_kind="completion",
        attempt_agent_instance_id=worker.agent_instance_id,
        actor=worker,
        prior_version=active["version"],
        payload={
            **_assignment_identity(row),
            "artifact_path": str(artifact),
            "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            "completion_contract_sha256": row["completion_contract_sha256"],
            "evidence": {"focused": {"status": "pass"}, "registered": {"status": "pass"}},
            "verdict": "READY-FOR-REVIEW",
        },
        observed_at=T0,
    )


def _assert_successor_completion() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        state, row, actor, register = _successor_fixture(Path(tmp), "ases-different")
        reported = _completion_fixture(state, row)
        assert reported["state"] == "completion_reported"
        payload = {
            "artifact_sha256": reported["reported_artifact_sha256"],
            "acceptance_evidence": {
                "evidence": {"focused": {"status": "pass"}, "registered": {"status": "pass"}},
                "verdict": "READY-FOR-REVIEW",
                "worker_evidence_sha256": hashlib.sha256(
                    json.dumps(
                        reported["reported_completion_evidence"],
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest(),
            },
        }
        try:
            validate_resolution_admission(state, dispatch_id=row["dispatch_id"], event_id="successor-accept", action="accept_completion", actor=actor)
            accepted = resolve_managed_dispatch(
                state,
                dispatch_id=row["dispatch_id"],
                event_id="successor-accept",
                action="accept_completion",
                actor=actor,
                prior_version=reported["version"],
                payload=payload,
                observed_at=T0,
                register=register,
            )
        except DispatchError as exc:
            print(f"  successor completion rejected: {exc.code}: {exc.message}")
            _check(False, "DIFFERENT_SESSION_COMPLETION_ACCEPTANCE")
            return
        _check(accepted["state"] == "completed" and all(accepted[key] == row[key] for key in ("assignment", "request_sha256", "spawned_by_instance_id", "spawned_by_role")), "DIFFERENT_SESSION_COMPLETION_ACCEPTANCE")
        events = list(_successor_events(state))
        replay = resolve_managed_dispatch(
            state,
            dispatch_id=row["dispatch_id"],
            event_id="successor-accept",
            action="accept_completion",
            actor=actor,
            prior_version=reported["version"],
            payload=payload,
            observed_at=T0,
            register=register,
        )
        _check(replay == accepted and _successor_events(state) == events, "COMPLETION_EXACT_REPLAY")
        _seed_binding(state, role=row["spawned_by_role"], instance="agi-next", session="ases-next")
        try:
            resolve_managed_dispatch(
                state,
                dispatch_id=row["dispatch_id"],
                event_id="successor-accept",
                action="accept_completion",
                actor=actor,
                prior_version=reported["version"],
                payload=payload,
                observed_at=T0,
                register=register,
            )
        except DispatchError as exc:
            _check(exc.code == "coordinator_authority_denied" and _successor_events(state) == events and read_managed_dispatch(state, row["dispatch_id"]) == accepted, "DISPLACED_HOLDER_REPLAY_DENIED")
        else:
            _check(False, "DISPLACED_HOLDER_REPLAY_DENIED")


def main() -> int:
    _assert_verified_successor_resolution()
    _assert_successor_completion()
    _assert_disconnected_successor_denials()
    plugin = _plugin({"agi-registered": "ases-registered", "agi-cli": "ases-cli"})
    attributed = _actor(plugin, {"caller_attribution_instance_id": "agi-cli"})
    precedence = _actor(
        plugin,
        {
            "inference_vertex_session_id": "agi-registered",
            "caller_attribution_instance_id": "agi-cli",
        },
    )
    _check(
        attributed == DispatchActor("agi-cli", "ases-cli", "live_peer_binding"),
        "attributed CLI call resolves its current peer binding",
    )
    _check(
        precedence == DispatchActor("agi-registered", "ases-registered", "live_peer_binding"),
        "registered inference identity takes precedence over caller attribution",
    )
    _check(
        _report_actor(plugin) == DispatchActor("agi-cli", "ases-cli", "live_peer_binding"),
        "normal managed-dispatch report path receives the attributed actor",
    )
    _check(
        _actor(_plugin({}), {"caller_attribution_instance_id": "agi-stale"}) == "dispatch_identity_unregistered" and _actor(plugin, {}) == "dispatch_authentication_required",
        "missing or stale identity does not fabricate another session",
    )
    print(f"\nmanaged dispatch CLI actor smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
