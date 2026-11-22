"""Isolated launch boundaries for the serial-dispatch regression proofs."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Generator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from ananta.core.actions.action_processor import ActionProcessor
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph
from ananta.interfaces.state_service_protocol import StateServiceProtocol

# The caller inserts this core fixture directory before importing this module.
from async_job_fixture_support import JobState

import agent_messaging_plugin.plugin as plugin_module
from agent_messaging_plugin.plugin import AgentMessagingPlugin


@dataclass
class FixtureAction(QueuedAction):
    source_plugin: str | None = None


def action(verb: str) -> FixtureAction:
    return FixtureAction(
        id=f"action-{verb}",
        process_key=f"plugin::agent_messaging_plugin::{verb}",
        parameters="{}",
        notes="launch fixture",
        created_at="2026-10-03T00:00:00Z",
        session_id="session-fixture",
        flow_id="flow-fixture",
    )


def parameters(verb: str) -> dict[str, Any]:
    """The generic arguments of one launch verb: qualify_fleet takes none."""
    if verb == "qualify_fleet":
        return {}
    return {
        "agent_runtime": "codex",
        "model": "fixture-model",
        "effort": "medium",
        "workspace": str(Path(__file__).resolve().parents[3]),
        "first_message": "fixture first message",
        "host": "qualification",
        "visibility": "headless",
        "local_name": "fixture-worker",
        "permission_mode": "fixture",
        "transport": "mcp",
    }


def plugin_fixture(state: JobState) -> Any:
    plugin: Any = AgentMessagingPlugin()
    plugin._get_state_service = lambda: state
    plugin._try_acquire_async_job_manager = lambda: AsyncJobManager(cast(Any, state), FlowRuntimeGraph(cast(Any, state)))
    state.rows.setdefault("flows", [{"id": "flow-fixture", "trigger_type": "bridge_process_call", "status": "active"}])
    return plugin


def finish_fixture(plugin: Any) -> None:
    worker = getattr(plugin, "_launch_jobs", None)
    if worker is not None:
        if worker.future is not None:
            worker.future.result(timeout=5)
        worker.stop()


@contextmanager
def blocked_restart_owner(
    plugin: Any,
    manager: Any,
    state: JobState,
    selection: dict[str, Any],
    request: dict[str, object],
    result: dict[str, object],
) -> Generator[list[str]]:
    """Hold a real choreography selection in its effect while a second color runs."""
    entered, release = threading.Event(), threading.Event()
    effects: list[str] = []

    def restart(_job: str, payload: dict[str, object], _manager: object, _state: object) -> dict[str, object]:
        assert payload == request
        effects.append("rightful")
        entered.set()
        assert release.wait(5), "rightful_restart_release"
        return result

    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(plugin, "_run_restart_session_operation", side_effect=restart):
        future = pool.submit(plugin._process_choreography_job, selection, manager, state)
        try:
            assert entered.wait(5), "rightful_restart_entered"
            yield effects
        finally:
            release.set()
            future.result(timeout=5)
            finish_fixture(plugin)


def restart_request_failure(failure: str) -> dict[str, object]:
    if failure == "exception":
        raise RuntimeError("injected restart request read exception")
    if failure == "transient":
        return {"action_status": "error", "error": {"message": "injected transient request read failure"}}
    assert failure == "malformed"
    return {"action_status": "completed", "data": {"payload": ["not an object"]}}


def terminal_statuses(state: JobState) -> list[str]:
    return [str(update["status"]) for update in state.update_state_calls if update.get("status") in {"completed", "error"}]


@contextmanager
def delayed_restart_rejection(
    plugin: Any,
    manager: Any,
    state: JobState,
    selection: dict[str, Any],
) -> Generator[tuple[threading.Event, Future[None]]]:
    """Pause after observing a busy owner, before the losing selector handles it."""
    import agent_messaging_plugin.session_launch_jobs as launch_jobs_module
    from agent_messaging_plugin.session_launch_store import LaunchBusyError

    observed, resume = threading.Event(), threading.Event()
    original_reserve = launch_jobs_module.reserve_launch

    def reserve(service: StateServiceProtocol, operation_id: str, job_id: str = "") -> None:
        try:
            original_reserve(service, operation_id, job_id)
        except LaunchBusyError:
            observed.set()
            assert resume.wait(5), "release_observed_restart_rejection"
            raise

    with ThreadPoolExecutor(max_workers=1) as pool, patch.object(launch_jobs_module, "reserve_launch", side_effect=reserve), patch.object(plugin, "_run_restart_session_operation", side_effect=AssertionError("rejected_restart_effect")):
        future = pool.submit(plugin._process_choreography_job, selection, manager, state)
        try:
            assert observed.wait(5), "restart_selection_observed_unrelated_owner"
            yield resume, future
        finally:
            resume.set()
            future.result(timeout=5)
            finish_fixture(plugin)


def restart_completion_snapshot(state: JobState, manager: Any, job_id: str) -> dict[str, object]:
    return {
        "status": state.rows["job"][0]["status"],
        "outcomes": terminal_statuses(state),
        "result": manager.get_job_payload(job_id, "result").get("data", {}).get("payload"),
    }


def restart_busy_handoff() -> dict[str, Any]:
    """Observe unrelated-owner release, admission, and a stale rejected selection."""
    from agent_messaging_plugin.session_launch_store import release_launch, reserve_launch

    state = JobState()
    loser, owner = plugin_fixture(state), plugin_fixture(state)
    loser_manager, manager = loser._try_acquire_async_job_manager(), owner._try_acquire_async_job_manager()
    request: dict[str, object] = {"notes": "restart busy handoff fixture"}
    result: dict[str, object] = {"new_agent_instance_id": "agi-rightful-busy-handoff"}
    job_id = manager.create_job("agent_messaging_plugin", "restart_session", request)["data"]["job_id"]
    selection = deepcopy(state.rows["job"][0])
    reserve_launch(cast(Any, state), "unrelated-owner", "unrelated-job")
    releases: list[dict[str, object]] = []
    original_delete = state.delete_records

    def record_release(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        releases.append(restart_completion_snapshot(state, manager, job_id))
        return original_delete(namespace, query)

    with delayed_restart_rejection(loser, loser_manager, state, selection) as (resume, losing_selection):
        release_launch(cast(Any, state), "unrelated-owner", os.getpid())
        with patch.object(state, "delete_records", side_effect=record_release), blocked_restart_owner(owner, manager, state, selection, request, result) as effects:
            before = deepcopy(state.rows)
            with patch.object(loser_manager, "update_job", wraps=loser_manager.update_job) as losing_updates:
                resume.set()
                losing_selection.result(timeout=5)
                observation = {
                    "status_after_rejection": state.rows["job"][0]["status"],
                    "owner_unchanged": state.rows == before,
                    "losing_writes_and_releases": (losing_updates.call_count, len(releases)),
                }
    observation.update(
        effects=effects,
        final=restart_completion_snapshot(state, manager, job_id),
        expected_result=result,
        releases=releases,
        reservations=state.rows["session_launch_reservation"],
    )
    return observation


def owned_restart_request_failure(failure: str) -> None:
    state = JobState()
    plugin = plugin_fixture(state)
    manager = plugin._try_acquire_async_job_manager()
    job_id = manager.create_job("agent_messaging_plugin", "restart_session", {"notes": "owned request failure"})["data"]["job_id"]

    def read_request(*_args: object) -> dict[str, object]:
        assert state.rows["session_launch_reservation"][0]["job_id"] == job_id, "restart_request_read_requires_reservation"
        assert state.rows["job"][0]["status"] == "processing"
        return restart_request_failure(failure)

    with patch.object(manager, "get_job_payload", side_effect=read_request), patch.object(plugin, "_run_restart_session_operation") as effect:
        plugin._process_choreography_job(deepcopy(state.rows["job"][0]), manager, state)
        effect.assert_not_called()
    expected = {"transient": "request_payload_missing", "malformed": "request_payload_invalid", "exception": "internal_error"}
    assert manager.get_job(job_id)["data"]["job"]["status"] == "error"
    assert manager.get_job_payload(job_id, "error")["data"]["payload"]["code"] == expected[failure]
    assert state.rows["session_launch_reservation"] == []
    assert terminal_statuses(state) == ["error"]
    finish_fixture(plugin)
    print(f"PASS owned_restart_{failure}_commits_one_error_before_release")


def dispatch(verb: str, plugin: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
    processor = ActionProcessor(PluginManager())
    return cast(dict[str, Any], processor._execute_plugin_method(plugin, verb, params or parameters(verb), action(verb)))


def effect_patch(verb: str, plugin: Any, operation: Callable[..., dict[str, Any]]) -> Any:
    if verb == "qualify_fleet":
        return patch.object(plugin, "qualify_fleet", lambda *args, **kwargs: plugin_module._success_result(data=operation(*args, **kwargs)))
    target = {"spawn_session": "lifecycle_spawn_session"}[verb]
    return patch.object(plugin_module, target, operation)


async def serial_pair(verb: str, plugin: Any, cheap: threading.Event, results: list[dict[str, Any]]) -> None:
    def execute(queued: QueuedAction) -> dict[str, Any]:
        result = dispatch(queued.process_key.rsplit("::", 1)[1], plugin)
        result["success"] = result.get("action_status") == "completed"
        results.append(result)
        return result

    def completed(action_id: str, _result: object, _token: object, **_kwargs: object) -> None:
        if action_id == "action-list_sessions":
            cheap.set()

    def failed(*args: object, **kwargs: object) -> None:
        raise AssertionError(f"dispatch failed: {args!r} {kwargs!r}")

    poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.action_processor = SimpleNamespace(execute_action=execute)
    poller._is_terminated_flow_sibling = lambda _action: False
    poller._prepare_action_for_execution = lambda _action: True
    poller._resolve_io_context = lambda _action: None
    poller._mark_action_completed = completed
    poller._mark_action_failed = failed
    await poller._process_action(action(verb))
    await poller._process_action(action("list_sessions"))


def authority_patches() -> Any:
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch.object(plugin_module, "holds_role", return_value=True))
    return stack
