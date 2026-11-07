#!/usr/bin/env python3
"""Real reserved restart worker: refusal, intact predecessor, durable outcomes.

In-memory boundary doubles only. No database, host process or worktree action.
A post-termination refusal mutant must violate the predecessor assertions.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "ananta/src", ROOT / "plugins/agent_messaging_plugin/src"):
    sys.path.insert(0, str(source))

from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402

from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import VerbError  # noqa: E402

CODE = "restart_successor_preflight_unavailable"


def _mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict), "fixture object shape"
    return cast(dict[str, object], value)


@dataclass
class FakeHost:
    live: bool = True
    role_holder: str = "agi-predecessor"
    effects: list[str] = field(default_factory=list)

    def terminate(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.effects.append("terminate")
        self.live = False
        return {"host_action": "terminated"}

    def spawn(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.effects.append("spawn")
        raise VerbError("fixture_spawn_refused", "base successor refusal")

    def clear(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        self.effects.append("clear")
        self.live = False
        return {}


class FakeState:
    def __init__(self) -> None:
        self.reservation: dict[str, object] | None = None
        self.releases = 0

    def upsert_state(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        del namespace
        inserted = self.reservation is None
        if inserted:
            self.reservation = dict(_mapping(data["record"]))
        return {"action_status": "completed", "data": {"result": {"inserted": inserted}}}

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        del namespace, query
        records = [] if self.reservation is None else [dict(self.reservation)]
        return {"action_status": "completed", "data": {"records": records}}

    def delete_records(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        del namespace
        owner = _mapping(query["filters"])["operation_id"]
        if self.reservation is not None and self.reservation["operation_id"] == owner:
            self.reservation = None
            self.releases += 1
        return {"action_status": "completed", "data": {"result": {"deleted": self.releases}}}


class FakeManager:
    def __init__(self, state: FakeState, role_class: str, committed: bool) -> None:
        self.state = state
        self.committed = committed
        self.status = "queued"
        self.error: dict[str, object] = {}
        self.attempted_error: dict[str, object] = {}
        self.request: dict[str, object] = {
            "agent_instance_id": "agi-predecessor", "role_name": "Fixture-Worker",
            "role_class": role_class, "grace_seconds": 0,
        }

    def get_job(self, job_id: str) -> dict[str, object]:
        assert job_id == "job-fixture"
        return {"action_status": "completed", "data": {"job": {"status": self.status}}}

    def get_job_payload(self, job_id: str, name: str) -> dict[str, object]:
        assert job_id == "job-fixture" and name == "request"
        assert self.state.reservation is not None and self.status == "processing"
        return {"action_status": "completed", "data": {"payload": dict(self.request)}}

    def update_job(self, job_id: str, updates: dict[str, object]) -> dict[str, object]:
        assert job_id == "job-fixture"
        status = str(updates["status"])
        if status == "processing":
            self.status = status
            return {"action_status": "completed", "data": {}}
        assert self.state.reservation is not None, "terminal outcome attempted while owner held"
        assert self.state.reservation["job_id"] == job_id, "terminal outcome owns this job"
        assert self.state.reservation["operation_id"] == f"restart:{job_id}", "reserved restart executor"
        self.attempted_error = dict(_mapping(updates.get("error", {})))
        if not self.committed:
            return {"action_status": "error", "data": {"outcome_committed": False}}
        self.status = status
        self.error = dict(self.attempted_error)
        return {"action_status": "completed", "data": {"outcome_committed": True}}


def _case(role_class: str, committed: bool) -> None:
    host, state = FakeHost(), FakeState()
    manager = FakeManager(state, role_class, committed)
    plugin = AgentMessagingPlugin.__new__(AgentMessagingPlugin)
    row = {"agent_instance_id": "agi-predecessor", "lane_id": "fixture-lane",
           "host": "tmux", "agent_runtime": "codex", "lifecycle_state": "live"}
    with (
        patch.object(plugin, "_update_choreography_progress"),
        patch.object(plugin, "_build_session_lifecycle_policy_config", return_value=None),
        patch.object(plugin_module, "_apply_spawn_session_policy", return_value=None),
        patch.object(plugin_module, "lifecycle_session_status", return_value=row),
        patch.object(plugin_module, "lifecycle_terminate_session", side_effect=host.terminate),
        patch.object(plugin_module, "lifecycle_spawn_session", side_effect=host.spawn),
        patch.object(plugin_module, "lifecycle_clear_session", side_effect=host.clear),
    ):
        plugin._run_restart_session_job(  # noqa: SLF001 -- actual reserved worker boundary
            "job-fixture", cast(AsyncJobManager, manager), state,
        )
    assert host.effects == [] and host.live, "predecessor still reachable; zero terminate/spawn/clear"
    assert host.role_holder == "agi-predecessor", "role holder preserved"
    assert manager.attempted_error["code"] == CODE, "VerbError becomes typed job failure"
    if committed:
        assert manager.status == "error" and manager.error["code"] == CODE
        assert state.reservation is None and state.releases == 1, "release after committed error"
    else:
        assert manager.status == "processing" and manager.error == {}
        assert state.reservation is not None and state.releases == 0, "retain uncommitted owner"


def _origin_prefixes(frozen: Path) -> set[str]:
    prefixes = {"ananta", "solet_setup_contracts", "solet_cli"}
    for source in (frozen / "plugins").glob("*/src/*"):
        if source.is_dir() or source.suffix == ".py":
            prefixes.add(source.stem)
    return prefixes


def _file_origin_witness(name: str, origin: object, frozen: Path) -> bool:
    if origin is None:
        return False
    assert isinstance(origin, str) and origin.strip(), f"malformed file origin: {name}"
    resolved = Path(origin).resolve(strict=True)
    assert resolved.is_file(), f"file origin is not a file: {name}"
    assert resolved.is_relative_to(frozen), f"outside frozen replay: {name} -> {resolved}"
    return True


def _package_origin_witness(name: str, module: ModuleType, frozen: Path) -> bool:
    if not hasattr(module, "__path__"):
        return False
    namespace_paths = getattr(module, "__path__", None)
    print(f"frozen-package {name}: __path__={namespace_paths!r}", flush=True)
    assert namespace_paths is not None, f"missing package path: {name}"
    assert not isinstance(namespace_paths, (str, bytes, dict)), f"malformed package path: {name}"
    observed: list[Path] = []
    for entry in namespace_paths:
        assert isinstance(entry, str) and entry.strip(), f"malformed package entry: {name}"
        resolved_entry = Path(entry).resolve(strict=True)
        assert resolved_entry.is_dir(), f"package entry is not a directory: {name}"
        assert resolved_entry.is_relative_to(frozen), f"outside package path: {name} -> {resolved_entry}"
        observed.append(resolved_entry)
    print(f"frozen-package-realpaths {name}: {observed!r}", flush=True)
    assert observed, f"empty package path: {name}"
    return True


def _assert_module_origin(name: str, module: ModuleType | None, frozen: Path) -> None:
    assert module is not None, f"unresolved frozen module: {name}"
    origin = getattr(module, "__file__", None)
    print(f"frozen-origin {name}: module.__file__={origin!r}", flush=True)
    file_witness = _file_origin_witness(name, origin, frozen)
    package_witness = _package_origin_witness(name, module, frozen)
    assert file_witness or package_witness, f"module origin unavailable: {name}"


def _assert_frozen_origins() -> None:
    """Require first-party imports to belong to this smoke's checkout."""
    frozen = ROOT.resolve(strict=True)
    prefixes = _origin_prefixes(frozen)
    for name, module in sorted(sys.modules.items()):
        if name.partition(".")[0] not in prefixes:
            continue
        _assert_module_origin(name, module, frozen)


def main() -> None:
    _assert_frozen_origins()
    for role_class in ("principal", "project"):
        for committed in (True, False):
            _case(role_class, committed)
    _assert_frozen_origins()
    print("PASS restart refusal: real reserved worker, intact Codex predecessor, committed/uncommitted outcomes")


if __name__ == "__main__":
    main()
