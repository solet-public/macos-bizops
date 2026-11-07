"""Explicitly seeded register double; reads never invent or mint a Unit."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE

from agent_messaging_plugin.managed_dispatch import (
    read_managed_dispatch,
)
from agent_messaging_plugin.managed_dispatch_errors import DispatchError
from agent_messaging_plugin.register_unit_client import (
    PsoletRegisterUnitClient,
    RegisterActor,
    RegisteredRevision,
    RegisterUnitError,
    UnitAssignment,
    UnitReadback,
)
from agent_messaging_plugin.schema import TABLE_MANAGED_DISPATCH

FIXTURE_REPOSITORY_ID = "rep_fixture-solet"
FIXTURE_UNIT_ID = "unt_fixture-dispatch"


def fixture_assignment(unit_id: str = FIXTURE_UNIT_ID, text: str = "exact immutable brief\n") -> UnitAssignment:
    raw = text.encode("utf-8")
    return UnitAssignment(
        unit_id,
        "fixture-key",
        FIXTURE_REPOSITORY_ID,
        RegisteredRevision(
            "drv_fixture-revision",
            hashlib.sha256(raw).hexdigest(),
            len(raw),
            text,
        ),
    )


class RegisterUnitDouble:
    """Existing Units and revisions are explicitly registered by each test."""

    def __init__(self, repository_id: str = FIXTURE_REPOSITORY_ID) -> None:
        self.repository_id = repository_id
        self.units: dict[str, UnitReadback] = {}
        self.revisions: dict[str, RegisteredRevision] = {}
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.event_actors: list[RegisterActor] = []
        self.reads: list[str] = []
        self.owners: dict[str, str] = {}
        self.spawn_observations: dict[str, dict[str, Any]] = {}
        self.owner_changes: list[dict[str, Any]] = []
        self.transfer_error = ""
        self.history_version = 0
        self.unit_evidence: dict[str, Any] | None = None
        self.candidate_revisions: list[dict[str, Any]] = []
        self.unit_references: list[dict[str, Any]] = []
        self.custody_failures: dict[str, str] = {}

    def add_unit(self, unit_id: str = FIXTURE_UNIT_ID, *, kind: str = "infrastructure") -> UnitAssignment:
        assignment = fixture_assignment(unit_id)
        self.units[unit_id] = UnitReadback(
            unit_id,
            assignment.unit_key,
            kind,
            self.repository_id,
            "dispatched",
            False,
            assignment.revision.revision_id,
        )
        self.revisions[assignment.revision.revision_id] = assignment.revision
        return assignment

    def rebind(self, unit_id: str, revision: RegisteredRevision) -> None:
        self.revisions[revision.revision_id] = revision
        self.units[unit_id] = replace(self.units[unit_id], revision_id=revision.revision_id)

    def resolve_repository(self, root: Path) -> str:
        self.reads.append(f"repository:{root}")
        return self.repository_id

    def show_unit(self, unit_id: str) -> UnitReadback:
        self.reads.append(f"unit:{unit_id}")
        if unit_id not in self.units:
            raise RegisterUnitError("unit_not_found", f"Register double has no Unit {unit_id}.")
        return self.units[unit_id]

    def read_brief(self, unit: UnitReadback) -> RegisteredRevision:
        return self.read_revision(unit.revision_id)

    def read_revision(self, revision_id: str) -> RegisteredRevision:
        self.reads.append(f"revision:{revision_id}")
        if revision_id not in self.revisions:
            raise RegisterUnitError("unit_revision_missing", "Register double has no revision.")
        return self.revisions[revision_id]

    def record_unit_event(self, unit_id: str, detail: Mapping[str, Any], actor: RegisterActor) -> None:
        self.event_actors.append(actor)
        self.events.append((unit_id, dict(detail)))

    def read_unit_evidence(self, unit_id: str) -> dict[str, Any]:
        self._custody_read("evidence", unit_id)
        if self.unit_evidence is None:
            raise RegisterUnitError("unit_not_found", f"Register double has no evidence for {unit_id}.")
        return self.unit_evidence

    def list_candidate_revisions(self, unit_id: str) -> list[Mapping[str, Any]]:
        self._custody_read("candidates", unit_id)
        return list(self.candidate_revisions)

    def list_unit_references(self, unit_id: str) -> list[Mapping[str, Any]]:
        self._custody_read("references", unit_id)
        return list(self.unit_references)

    def _custody_read(self, verb: str, unit_id: str) -> None:
        self.reads.append(f"{verb}:{unit_id}")
        if verb in self.custody_failures:
            raise RegisterUnitError(self.custody_failures[verb], f"injected {verb} failure")

    def prepare_spawn_owner(self, detail: Mapping[str, Any]) -> dict[str, Any]:
        instance = str(detail["agent_instance_id"])
        if instance not in self.spawn_observations:
            self.history_version += 1
            self.spawn_observations[instance] = {
                **detail,
                "owner_actor_id": f"act-{instance}",
                "identity_event_id": f"uev-{instance}",
            }
        return {**self.spawn_observations[instance], "expected_history_version": self.history_version}

    def transfer_spawn_owner(self, receipt: Mapping[str, Any], actor: RegisterActor) -> dict[str, Any]:
        unit = str(receipt["unit_id"])
        owner = str(receipt["owner_actor_id"])
        if self.owners.get(unit) == owner:
            return {**receipt, "ownership_status": "already_assigned"}
        if self.transfer_error:
            raise RegisterUnitError(self.transfer_error, "injected owner transfer failure")
        if any(row.get("owner_actor_id") == owner for row in self.owner_changes):
            raise RegisterUnitError("spawn_owner_superseded", "released attempt cannot reclaim Unit")
        if receipt["expected_history_version"] != self.history_version:
            raise RegisterUnitError("unit_register_unavailable", "stale expected_history_version")
        self.owners[unit] = owner
        self.history_version += 1
        result = {**receipt, "ownership_status": "assigned", "history_version": self.history_version, "assigned_by_instance_id": actor.agent_instance_id}
        self.owner_changes.append(result)
        return result


def fixture_assignment_record(unit_id: str = FIXTURE_UNIT_ID) -> dict[str, Any]:
    assignment = fixture_assignment(unit_id)
    return {"unit_id": unit_id, "assignment": assignment.to_record(), "assignment_sha256": assignment.sha256}


class NativeOwnerCLIDouble(PsoletRegisterUnitClient):
    """Exercise production client argv/response boundaries; no native parser or PostgreSQL."""

    def __init__(self) -> None:
        super().__init__("fixture-psolet", solet_name="native-spawn-owner-test")
        self.native_events: list[dict[str, Any]] = []
        self.assignments: list[dict[str, Any]] = [{"actor_id": "act-agi-main", "assignment_kind": "owner", "released_at": None, "is_deleted": False}]
        self.commands: list[tuple[str, ...]] = []

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[bytes]:
        self.commands.append(arguments)
        payload = self._command(arguments)
        return subprocess.CompletedProcess(arguments, 0, json.dumps(payload).encode(), b"")

    def _command(self, arguments: tuple[str, ...]) -> dict[str, Any]:
        verb = arguments[2]
        if verb == "show":
            return {"unit_assignment": self.assignments}
        if verb == "events":
            return {"records": self.native_events, "frontier_ordinal": len(self.native_events), "has_more": False}
        if verb == "event-show":
            return next(row for row in self.native_events if row["event_id"] == arguments[3])
        if verb == "record-event":
            actor = "act-" + arguments[arguments.index("--agent-instance-id") + 1]
            event = {
                "event_id": f"uev-{len(self.native_events) + 1}",
                "ordinal": len(self.native_events) + 1,
                "unit_id": arguments[3],
                "observed_by_actor_id": actor,
                "detail": arguments[arguments.index("--detail") + 1],
                "event_kind": arguments[arguments.index("--event-kind") + 1],
                "target_event_id": arguments[arguments.index("--target-event") + 1] if "--target-event" in arguments else None,
            }
            self.native_events.append(event)
            return {"unit_event_id": event["event_id"]}
        if verb == "set-owner":
            return self._set_owner(arguments)
        if verb in {"read-back", "start"}:
            return self._child_uptake(arguments)
        raise AssertionError(f"unexpected native CLI command {arguments}")

    def _set_owner(self, arguments: tuple[str, ...]) -> dict[str, Any]:
        expected = int(arguments[arguments.index("--expected-history-version") + 1])
        if expected != len(self.native_events):
            raise RegisterUnitError("unit_register_unavailable", "stale expected_history_version")
        owner = arguments[arguments.index("--actor-id") + 1]
        for assignment in self.assignments:
            assignment["released_at"] = "released"
        self.assignments.append({"actor_id": owner, "assignment_kind": "owner", "released_at": None, "is_deleted": False})
        self.native_events.append({"event_id": f"uev-{len(self.native_events) + 1}", "detail": "ownership change", "observed_by_actor_id": "act-agi-main", "unit_id": arguments[3]})
        return {"owner_actor_id": owner, "history_version": len(self.native_events)}

    def _child_uptake(self, arguments: tuple[str, ...]) -> dict[str, Any]:
        actor = "act-" + arguments[arguments.index("--agent-instance-id") + 1]
        if actor != self.assignments[-1]["actor_id"]:
            raise RegisterUnitError("owner_mismatch", "child does not own existing Unit")
        return {"state_event_id": "use-child", "readback_id": "urb-child"}


def check_configured_plugin_sweep_causal_annotation() -> None:
    """Exercise the actual plugin entry and both wrappers with its configured client."""
    from types import SimpleNamespace

    from session_sweep_smoke import (
        _TEST_MANAGED_HOST,
        _check,
        _linked_live_attempt,
        _LivenessDriver,
        _state,
    )

    import agent_messaging_plugin.plugin as plugin_mod
    import agent_messaging_plugin.session_hosts as session_hosts
    from agent_messaging_plugin.managed_dispatch import DISPATCH_WORKER_LOST
    from agent_messaging_plugin.managed_dispatch_unit import capture_annotation_source
    from agent_messaging_plugin.register_unit_client import RegisterActor

    with tempfile.TemporaryDirectory() as raw:
        state = _state()
        _linked_live_attempt(state, Path(raw), dispatch_id="mdp-configured", agent_instance_id="agi-configured")
        row = read_managed_dispatch(state, "mdp-configured")
        actor = RegisterActor("agi-steward", "ases-steward", "Coordinator-Main", "mdp-configured")
        source = capture_annotation_source(row, actor)
        state.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": "mdp-configured"}}, {"attempt_receipt": {"annotation_source": source}})
        register = RegisterUnitDouble()
        plugin = SimpleNamespace(_get_state_service=lambda: state, _register_unit_client=lambda: register, _peer_registry=None, _bridge_manager=None)
        driver = _LivenessDriver(True)
        with (
            patch.dict(session_hosts._REGISTRY, {("codex", _TEST_MANAGED_HOST): driver}),  # noqa: SLF001
            patch.object(plugin_mod, "_run_ledger_tmux_rider"),
            patch.object(plugin_mod, "sweep_overdue_sessions", return_value=0),
            patch.object(plugin_mod, "sweep_unregistered_spawning_sessions", return_value=0),
            patch.object(plugin_mod, "_run_session_claude_mapping_riders"),
        ):
            for outcome in (True, RuntimeError("unknown probe")):
                driver.outcome = outcome
                plugin_mod.AgentMessagingPlugin._run_session_lifecycle_sweep(plugin)  # type: ignore[arg-type]  # noqa: SLF001
                _check(not register.events, "ANNOTATION_ALIVE_UNKNOWN_ZERO")
            driver.outcome = False
            plugin_mod.AgentMessagingPlugin._run_session_lifecycle_sweep(plugin)  # type: ignore[arg-type]  # noqa: SLF001
            plugin_mod.AgentMessagingPlugin._run_session_lifecycle_sweep(plugin)  # type: ignore[arg-type]  # noqa: SLF001
        observed = [(detail, register.event_actors[i]) for i, (_unit, detail) in enumerate(register.events) if detail["event"] == "worker_lost"]
        _check(len(observed) == 1, "ANNOTATION_PLUGIN_LOST_EXACTLY_ONE")
        detail, recorded_actor = observed[0]
        _check(recorded_actor == actor and detail["agent_instance_id"] == "agi-configured", "ANNOTATION_PLUGIN_EXACT_SOURCE_CLIENT")
        _check(detail["causal_event_id"] == f"host-dead:{row['version']}" and detail["prior_version"] == row["version"], "ANNOTATION_PLUGIN_CAUSAL_VERSION")
        _check(detail["assignment_sha256"] == row["assignment_sha256"] and detail["annotation_source"] == source, "ANNOTATION_PLUGIN_ASSIGNMENT_SOURCE")
        _check(read_managed_dispatch(state, "mdp-configured")["state"] == DISPATCH_WORKER_LOST, "ANNOTATION_PLUGIN_STATE_PROGRESSES")


def check_lost_annotation_cas_loser(test: Any) -> None:
    from managed_dispatch_smoke import _state

    test.state = _state()
    test.preparing()
    row = test.uncertain(test.register)
    before = len(test.register.events)
    original_update = test.state.update_state

    def lose(namespace: str, query: Any, updates: Any) -> Any:
        if query.get("table") == TABLE_MANAGED_DISPATCH and updates.get("state") == "worker_lost":
            original_update(namespace, query, {"version": row["version"] + 1})
        return original_update(namespace, query, updates)

    with patch.object(test.state, "update_state", side_effect=lose), test.assertRaises(DispatchError) as caught:
        test.lost(test.register)
    test.assertEqual(caught.exception.code, "stale_dispatch_version")
    test.assertEqual(len(test.register.events), before, "ANNOTATION_LOST_CAS_LOSER_ZERO")
