#!/usr/bin/env python3
"""D0.3/Option-A regression guard: AsyncJobManager's completion-handler firing.

Not the live proof of the mechanic (see
`workbench/2026-08-09_sync_verb_d03_deferred_completion_doctrine_syncverb-doctrine.md`
§6 — comfyui_image_generation_plugin's production history is that: every
generated image that lands in conversation is a live firing of this exact
code path). This smoke's job is narrower and complementary: pin the real
`AsyncJobManager.update_job` code path against fakes so a future regression
here reds a smoke instead of only degrading production silently.

Traces (file:line current as of 2026-08-09):
`ananta/src/ananta/core/state/async_job_manager.py:446` (`update_job`) ->
`:842` (`_handle_completion_actions`) -> `:827`
(`self._action_factory.submit_action_definition`, via `_submit_completion_action`).

Four cases, each naming its failing mutation:

  1. Happy path: terminal status + `completion_handlers` + `flow_id` in job
     metadata -> exactly one action submitted, addressed at the configured
     process key, carrying the job's flow_id. (Mutation: remove the
     `_action_factory.submit_action_definition` call from
     `_submit_completion_action` -> this case goes from PASS to FAIL.)
  2. No `completion_handlers` in metadata -> silent, legitimate no-op (no
     submission, no error — a job with no configured continuation is valid,
     e.g. a job created outside action context). (Mutation: make
     `_get_completion_handler` return a handler even with no
     `completion_handlers` key -> this case's "no action submitted" check
     goes from PASS to FAIL.)
  3. Non-terminal status transition (`"processing"`) -> no submission at
     all; completion routing is gated on terminal status in `update_job`
     itself, before `_handle_completion_actions` is ever called. (Mutation:
     widen `update_job`'s `if new_status in (...)` guard to include
     `"processing"` -> this case's "no action submitted" check goes from
     PASS to FAIL.)
  4. `completion_handlers` present but `flow_id` missing from metadata ->
     loud, structured failure (not a silent success and not a swallowed
     exception): `_handle_completion_actions`'s fail-fast `ValueError`
     propagates into `update_job`'s own `except Exception` handler and comes
     back as `action_status="error"` naming the missing field. (Mutation:
     make the fail-fast check `if not flow_id: raise ValueError(...)` a
     silent `return` instead -> this case's error-surfaced check goes from
     PASS to FAIL, since `update_job` would then report a false
     "completed".) Note (observed, not asserted further): the job's ledger
     row and result payload ARE written before this failure — they are
     written earlier in `update_job`, before the terminal-status branch runs
     — so a caller sees "the job's own status/result persisted" alongside
     "the continuation-routing call failed." That combination is real
     `update_job` behavior, not a smoke artifact.

Rail (coordinator seat, arm-6363cec06def7aa902c2b10dee4ccb3b, 2026-08-09): fakes must
track the real protocols they stand in for, so a signature drift reds this
smoke rather than quietly vacating it — `test_fakes_track_real_signatures`
below.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 ananta/tests/core/actions/async_job_manager_completion_handler_smoke.py
"""

from __future__ import annotations

import importlib
import inspect
import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.result_processing.coordinator import DispatchOutcome  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.execution_token_context import action_execution_context  # noqa: E402
from ananta.core.state.job_completion_reach import (  # noqa: E402
    COMPLETION_REACH_KEY,
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
    REACH_CHANNEL_FLOW,
)
from ananta.core.state.registered_job_operations import RegisteredOperationOwner  # noqa: E402
from ananta.interfaces.state_service_protocol import StateServiceProtocol  # noqa: E402
from async_job_atomic_completion_smoke import FixtureAction, create, manager_fixture  # noqa: E402
from async_job_fixture_support import JobState  # noqa: E402


def selected_declarations_and_owner_aliases() -> None:
    """The finite selection and actual resolver use the agreed owner contract."""
    from ananta.core.actions.action_processor import _REGISTERED_JOB_OPERATIONS, _REGISTERED_OWNER_ALIASES

    processes: dict[str, Any] = {}
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    for provider in {*_REGISTERED_JOB_OPERATIONS, *_REGISTERED_OWNER_ALIASES}:
        module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
        scanner._scan_module_classes(module, module.__name__, processes)
    assert sum(map(len, _REGISTERED_JOB_OPERATIONS.values())) == 180
    assert sum(map(len, _REGISTERED_OWNER_ALIASES.values())) == 16
    for provider, names in {**_REGISTERED_JOB_OPERATIONS, **_REGISTERED_OWNER_ALIASES}.items():
        for name in names:
            entry = processes[f"service_interface::{provider}::{name}"]
            assert_selected_declaration(entry)
    assert processes["service_interface::job_service::get_job"]["is_async"] is False
    assert processes["service_interface::session_ledger_service::ingest_raw_chunk"]["is_async"] is True

    with TemporaryDirectory(prefix="registered-owner-contract-") as temporary:
        for provider, names in _REGISTERED_OWNER_ALIASES.items():
            for name in names:
                owner_alias_case(processes, provider, name, names, temporary)
    print("PASS 179_explicit_framework_entries, 16_bound_owner_alias_contracts, unchanged_point_reader_and_serial_ledger_owner")


def assert_selected_declaration(entry: Any) -> None:
    """Registration and blueprint both describe the canonical receipt."""
    assert entry["is_async"] is True and entry["action_blueprint"]["metadata"]["is_async"] is True
    schema = entry["output"]
    schema_dict = schema.to_dict() if hasattr(schema, "to_dict") else schema
    assert set(schema_dict["properties"]) == {"job_id", "status"}


def owner_alias_case(processes: dict[str, Any], provider: str, name: str, names: frozenset[str], temporary: str) -> None:
    """Resolve one real framework alias to an owned hook-contract fixture."""
    from ananta.core.event_orchestrator import EventOrchestrator
    from ananta.core.orchestration.execution_context import ExecutionContextManager
    from ananta.core.orchestration.service_bindings import BindingSource, ServiceBinding, ServiceBindings, ServiceName
    from ananta.core.services.call_context import CallContext
    from ananta.services.discovery_service.service import DiscoveryService
    from ananta.services.scheduling_service import SchedulingService

    class OwnerHook:
        """Owned contract double; the real U3/U4 hooks are merged-tree evidence."""

        def __init__(self, manager: AsyncJobManager, owner: str, selected: frozenset[str]) -> None:
            self.manager, self.name, self.selected = manager, owner, selected
            self.calls: list[str] = []

        def is_ready(self) -> bool:
            return True

        def admit_registered_operation(self, action_name: str, parameters: dict[str, Any], state: dict[str, Any]) -> dict[str, object] | None:
            if action_name not in self.selected:
                return None
            assert state["session_id"] == "s" and state["flow_id"] == "flow"
            assert isinstance(state["call_context"], CallContext), "only the server constructs the owner context"

            def body(values: dict[str, object]) -> dict[str, object]:
                self.calls.append(action_name)
                return {"action_status": "completed", "data": {"owner": self.name, "operation": action_name, "parameters": values}}

            return self.manager.submit_registered_operation(self.name, action_name, parameters, body)

    state, manager, token_id, _completed = manager_fixture()
    owner = OwnerHook(manager, "scheduler-owner" if provider == "scheduling_service" else "deployment-owner", names)
    plugins = PluginManager()
    plugins.plugins[owner.name] = cast(Any, owner)
    bindings = ServiceBindings(temporary)
    bindings._bindings = {ServiceName(provider): ServiceBinding(ServiceName(provider), owner.name, BindingSource.CONFIG)}
    bindings._loaded = True
    orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
    orchestrator.APP_HOME, orchestrator.plugin_manager, orchestrator.service_bindings = temporary, plugins, bindings
    orchestrator.async_job_manager = manager
    orchestrator.action_recorder = None
    if provider == "scheduling_service":
        scheduler = SchedulingService(plugins, owner.name, temporary)
        scheduler._plugin = cast(Any, owner)
        orchestrator.service_manager = SimpleNamespace(scheduling_service=scheduler)
    discovery: Any = DiscoveryService.__new__(DiscoveryService)
    discovery.processes = processes
    contexts = ExecutionContextManager()
    contexts.create_context("flow")
    processor = ActionProcessor(plugins, state_service=cast(Any, state), orchestrator=orchestrator, process_registry={"processes": processes}, execution_context_manager=contexts, discovery_service=discovery)
    action = FixtureAction("action", f"service_interface::{provider}::{name}", json.dumps({"state": {"flow_id": "spoof"}}), "owner contract", "now", session_id="s", flow_id="flow", flow_token_id=token_id)
    response = processor.execute_action(action)
    assert response["success"] is True, (provider, name, response)
    data = response["data"]
    assert isinstance(data, dict)
    job_id = str(data["job_id"])
    thread = manager._owners[owner.name]._thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive()
    assert owner.calls == [name]
    result = manager.get_job_payload(job_id, "result")["data"]
    assert isinstance(result, dict) and isinstance(result["payload"], dict)
    assert result["payload"]["data"]["operation"] == name


def plugin_result(data: dict[str, object]) -> dict[str, object]:
    """A complete plugin envelope, including the dispatch-required timestamp."""
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": "2026-11-09T00:00:00+00:00"}


class InlinePlugin:
    """The same synchronous body supports both no-hook and unselected controls.

    Its recorded entry distinguishes an actual dispatch from a setup failure.
    A selected hook must return before this body is called; an unselected hook
    must leave the complete body result intact, including request parameters.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    def operation(self, params: dict[str, object], state: dict[str, object]) -> dict[str, object]:
        self.calls.append("operation")
        assert state["flow_id"] == "flow", "body state must come from the queued action"
        return plugin_result({"inline": True, "parameters": params})

    unselected = operation


class AdmittingPlugin(InlinePlugin):
    """Retained-route-style owner: persist an admission before any body entry.

    Only the owner is a double. The real processor, execution context, job
    manager and transaction fixture carry the registered action and token.
    No worker is needed to distinguish admission from synchronous dispatch.
    """

    def __init__(self, manager: AsyncJobManager) -> None:
        super().__init__()
        self.manager = manager
        self.hook_calls: list[str] = []
        self.state: dict[str, object] = {}

    def admit_registered_operation(self, action_name: str, parameters: dict[str, object], state: dict[str, object]) -> dict[str, object] | None:
        self.hook_calls.append(action_name)
        self.state = state
        if action_name != "operation":
            return None
        receipt = self.manager.create_job("retained-route-fixture", action_name, {"notes": "ordinary plugin admission", "parameters": parameters}, job_metadata={"session_id": state["session_id"], "flow_id": state["flow_id"]})
        assert receipt["action_status"] == "completed", receipt
        data = receipt["data"]
        assert isinstance(data, dict)
        return plugin_result(data)


def ordinary_plugin_processor(plugins: PluginManager, state: JobState, process_key: str) -> ActionProcessor:
    """Build the real outer dispatch route, with no replaced processor methods.

    Discovery supplies only the fixture's output schema. Placeholder resolution
    and result storage use the real execution context, just as owner aliases do.
    A server source_plugin also exercises trusted principal construction without
    borrowing any live bridge identity or permitting request-state injection.
    """
    from ananta.core.orchestration.execution_context import ExecutionContextManager
    from ananta.services.discovery_service.service import DiscoveryService

    discovery: Any = DiscoveryService.__new__(DiscoveryService)
    discovery.processes = {process_key: {"return_value_schema": {"type": "object"}}}
    contexts = ExecutionContextManager()
    contexts.create_context("flow")
    return ActionProcessor(plugins, state_service=cast(Any, state), execution_context_manager=contexts, discovery_service=discovery)


def ordinary_plugin_admission() -> None:
    """Selected, unselected and absent hooks run through execute_action.

    The negative controls run first, so the red-first failure specifically
    identifies the missing selected admission after a valid ordinary dispatch.
    The persisted request, token and registered metadata are read back from
    the real job manager. Caller-supplied state remains ordinary request data.
    This also covers a retained service-provider-style route without touching
    any plugin's registration policy or executing host effects.
    """
    for mode in ("no_hook", "unselected", "selected"):
        ordinary_plugin_case(mode)


def ordinary_plugin_case(mode: str) -> None:
    """Dispatch one fixture action through the unchanged public processor entry.

    Each mode gets fresh state and a fresh token, so a selected receipt cannot
    borrow a job from a control. A body-entry failure includes its actual entry
    list, preserving a decisive red witness rather than a missing-key error.
    """
    state, manager, token_id, _completed = manager_fixture()
    plugin = InlinePlugin() if mode == "no_hook" else AdmittingPlugin(manager)
    verb = "unselected" if mode == "unselected" else "operation"
    key = f"plugin::retained-route-fixture::{verb}"
    plugins = PluginManager()
    plugins.plugins["retained-route-fixture"] = cast(Any, plugin)
    processor = ordinary_plugin_processor(plugins, state, key)
    parameters: dict[str, object] = {"state": {"session_id": "spoof", "flow_id": "spoof", "call_context": "spoof"}}
    action = FixtureAction("action", key, json.dumps(parameters), "ordinary plugin", "now", session_id="s", flow_id="flow", context_id="context", flow_token_id=token_id, source_plugin="trusted-caller")
    response = processor.execute_action(action)
    assert response["success"] is True, response
    if mode != "selected":
        assert response == {"success": True, **plugin_result({"inline": True, "parameters": parameters})}
        assert (plugin.calls, state.rows.get("job", [])) == (["operation"], [])
        print(f"PASS ordinary_plugin_{mode}_body_unchanged")
        return
    assert plugin.calls == [], f"selected admission must bypass the plugin body; body entries={plugin.calls!r}"
    assert isinstance(plugin, AdmittingPlugin)
    assert_selected_plugin_admission(plugin, response, state, manager, token_id, action, parameters)
    print("PASS ordinary_plugin_selected_persisted_admission, server_context_and_token, retained_route_body_not_entered")


def assert_selected_plugin_admission(plugin: AdmittingPlugin, response: dict[str, object], state: JobState, manager: AsyncJobManager, token_id: str, action: FixtureAction, parameters: dict[str, object]) -> None:
    """Read back the durable request and distinguish trusted state from inputs.

    The receipt identifies the sole job, whose registered action and token
    metadata came from the processor's outer execution scopes. The hook sees
    exactly the same server context as a required service-owner admission.
    Request state is preserved as data and cannot replace that context.
    """
    from ananta.core.services.call_context import CallContext

    assert plugin.hook_calls == ["operation"]
    assert plugin.state == {"session_id": "s", "flow_id": "flow", "context_id": "context", "call_context": CallContext.for_plugin("trusted-caller")}
    assert len(state.rows["job"]) == 1 and state.rows["job"][0]["flow_token_id"] == token_id
    job = state.rows["job"][0]
    assert response["data"] == {"job_id": job["id"], "status": "queued"}
    assert {"action_status", "data", "actions", "error", "timestamp"} <= response.keys()
    assert json.loads(job["metadata"])["registered_action_id"] == action.id
    request: Any = manager.get_job_payload(job["id"], "request")
    assert request["data"]["payload"]["parameters"] == parameters


def scheduler_plugin_single_admission() -> None:
    """Keep the three existing PH/DS plugin wrappers at exactly one admission.

    These are real plugin instances and real decorated wrappers. Only their
    synchronous effect body is parked. The owner hook, AsyncJobs worker and
    AsyncJobManager ledger are unchanged, so a second admission would create
    a second persisted job and fail here. The worker is always released and
    joined, including if an assertion fails, and its terminal result is read
    back after completion. No real scheduler or host operation is performed.
    """
    for name in ("platform_health_plugin", "default_scheduling_plugin"):
        sys.path.insert(0, str(REPO_ROOT / "plugins" / name / "src"))
    from default_scheduling_plugin.plugin import SchedulingPlugin
    from platform_health_plugin.plugin import PlatformHealthPlugin

    for provider, verb in (("platform_health_plugin", "ensure_model_capability_guard_schedule"), ("default_scheduling_plugin", "get_schedules_by_tag"), ("default_scheduling_plugin", "list_schedules")):
        scheduler_plugin_case(provider, verb, PlatformHealthPlugin(), SchedulingPlugin())


def scheduler_plugin_case(provider: str, verb: str, health: Any, scheduler: Any) -> None:
    """Exercise one actual scheduler-family owner without cross-case closures.

    Assign the health plugin's runtime name as PluginManager does at startup;
    PluginBase construction clears its class-level default. Both dependencies
    share the real owner queue and the same persistent manager for this case.
    """
    state, manager, token_id, _completed = manager_fixture()
    health.name = "platform_health_plugin"
    plugins = PluginManager()
    plugins.plugins.update({"platform_health_plugin": health, "default_scheduling_plugin": scheduler})
    orchestrator: Any = SimpleNamespace(async_job_manager=manager, plugin_manager=plugins)
    health.set_orchestrator_ref(orchestrator)
    scheduler.set_orchestrator_ref(orchestrator)
    plugin = health if provider == health.name else scheduler
    entered, release = threading.Event(), threading.Event()
    callers: list[int] = []

    def body(_params: dict[str, Any], _state: dict[str, Any]) -> dict[str, object]:
        callers.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "test must release the owned worker"
        return plugin_result({"final": verb})

    key = f"plugin::{provider}::{verb}"
    processor = ordinary_plugin_processor(plugins, state, key)
    action = FixtureAction("action", key, json.dumps({"tag": "fixture"}), "single admission", "now", session_id="s", flow_id="flow", flow_token_id=token_id)
    with patch.object(plugin, verb, side_effect=body), patch.object(plugin, "admit_registered_operation", wraps=plugin.admit_registered_operation) as hook:
        try:
            response = processor.execute_action(action)
            assert response["success"] is True, response
            assert (hook.call_count, len(state.rows["job"])) == (1, 1), "registered wrapper must admit exactly one job"
            assert entered.wait(5) and threading.get_ident() not in callers
        finally:
            release.set()
            worker = health._operation_jobs._thread
            if worker is not None:
                worker.join(5)
                assert not worker.is_alive(), "the scheduler-family worker must drain"
    job = state.rows["job"][0]
    assert (job["status"], job["flow_token_id"]) == ("completed", token_id)
    payload: Any = manager.get_job_payload(job["id"], "result")
    assert payload["data"]["payload"]["data"] == {"final": verb}
    print(f"PASS {provider}.{verb}: exactly_one_admission, owned_worker, persisted_terminal_result")


def conditional_response_routing() -> None:
    """The same async declaration routes bounded results and defers owned jobs."""
    key = "plugin::agent_messaging_plugin::report_managed_dispatch"
    for has_job in (False, True):
        state, manager, token_id, _completed = manager_fixture()
        state.rows["action_events"] = [{"id": "action", "flow_token_id": token_id}]
        poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        poller.state_service, poller._flow_runtime_graph = state, manager._flow_runtime_graph
        poller._async_process_cache = {key: True}
        routes: list[dict[str, object]] = []
        poller._retrieve_action_details = lambda _id, **_kwargs: (key, "conditional", "{}", None, "s", "flow", None, {}, "inference", None, None)
        for method in ("_update_action_status_to_completed", "_store_action_result", "_record_work_products_after_success"):
            setattr(poller, method, lambda *_args: None)
        poller._extract_and_store_attachments = lambda *_args: []
        poller._submit_returned_actions = lambda *_args: True
        poller._maybe_dispatch_result_processing = lambda _routes=routes, **kwargs: _routes.append(kwargs["result"])
        manager.set_registered_completion_handler(poller._complete_registered_job)
        if has_job:
            from ananta.core.state.async_job_manager import registered_action_context

            with registered_action_context("action", "flow", "s"):
                job_id = create(manager, token_id)
            poller._mark_action_completed("action", {"data": {"job_id": job_id}}, token_id)
            assert routes == [], "an actual owned receipt must defer the original response"
            assert manager.update_job(job_id, {"status": "completed", "result": {"data": {"final": True}}})["action_status"] == "completed"
            assert len(routes) == 1 and routes[0]["data"] == {"final": True}
        else:
            poller._mark_action_completed("action", {"data": {"ack": True}}, token_id)
            assert len(routes) == 1 and routes[0]["data"] == {"ack": True}
        assert poller._should_skip_result_processor(key) is False, "metadata alone cannot drop a bounded response"
    print("PASS bounded_conditional_response_once, owned_final_response_once, no_global_async_offload")


def test_continuation_guards_preserve_owned_token_routing() -> None:
    """Real manager -> registered callback -> poller -> FRG preserves both guards."""
    for guard in ("none", "blocks_continuation", "contract_violation"):
        for has_job in (False, True):
            continuation_guard_case(guard, has_job)


def continuation_guard_case(guard: str, has_job: bool) -> None:
    state, manager, token_id, completed = manager_fixture()
    state.rows["action_events"] = [{"id": "action", "flow_token_id": token_id}]
    poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.state_service, poller._flow_runtime_graph = state, manager._flow_runtime_graph
    poller._retrieve_action_details = lambda _id, **_kwargs: ("service_interface::state_service::count", "guard fixture", "{}", None, "s", "flow", None, {}, "inference", None, None)
    for method in ("_update_action_status_to_completed", "_store_action_result", "_record_work_products_after_success"):
        setattr(poller, method, lambda *_args: None)
    poller._extract_and_store_attachments = lambda *_args: []
    poller._submit_returned_actions = lambda *_args: True
    routes: list[dict[str, object]] = []

    def dispatch(**kwargs: Any) -> DispatchOutcome | None:
        routes.append(kwargs["result"])
        return DispatchOutcome.CONTRACT_VIOLATION_DISPATCHED if guard == "contract_violation" else None

    poller._maybe_dispatch_result_processing = dispatch
    manager.set_registered_completion_handler(poller._complete_registered_job)
    outcome: dict[str, object] = {"action_status": "completed", "data": {"final": True}, "blocks_continuation": guard == "blocks_continuation"}
    if has_job:
        from ananta.core.state.async_job_manager import registered_action_context

        with registered_action_context("action", "flow", "s"):
            job_id = create(manager, token_id)
        poller._mark_action_completed("action", {"data": {"job_id": job_id}}, token_id)
        assert routes == [] and completed == [], "receipt cannot route or resolve before the owner outcome"
        assert manager.update_job(job_id, {"status": "completed", "result": outcome})["action_status"] == "completed"
        assert state.rows["job"][0]["status"] == "completed", "a routing guard must preserve the committed operation outcome"
    else:
        poller._mark_action_completed("action", outcome, token_id)
    expected = "completed" if guard == "none" else "failed"
    actual = state.rows["flow_tokens"][0]["state"]
    label = f"{guard}, owned={has_job}"
    _check(actual == expected, f"{label}: token={actual}, expected={expected}")
    _check(len(routes) == 1 and completed == ["flow"], f"{label}: one result route and one flow completion")


def test_registered_route_and_explicit_handler_precedence() -> None:
    metadata: dict[str, object] = {"registered_action_id": "original-action", "flow_id": "flow-smoke-1", "session_id": "session-smoke-1"}
    manager, state, factory, _graph = _make_manager(metadata)
    routed: list[tuple[str, str, dict[str, object] | None]] = []

    def route(action_id: str, status: str, result: dict[str, object] | None, error: dict[str, object] | None) -> None:
        assert state.rows["job"][0]["status"] == "completed", "route must follow durable terminal state"
        assert error is None
        routed.append((action_id, status, result))

    manager.set_registered_completion_handler(route)
    outcome = {"action_status": "completed", "data": {"original": 23}, "actions": []}
    assert manager.update_job("job-smoke-1", {"status": "completed", "result": outcome})["action_status"] == "completed"
    assert routed == [("original-action", "completed", outcome)]
    assert manager.update_job("job-smoke-1", {"status": "completed", "result": {"wrong": True}})["action_status"] == "error"
    assert len(routed) == 1, "a refused second outcome cannot duplicate the registered response"
    state.rows["job"][1]["metadata"] = {**_completion_handlers_metadata(), "registered_action_id": "original-action"}
    assert manager.update_job("job-smoke-2", {"status": "completed", "result": outcome})["action_status"] == "completed"
    assert len(routed) == 1 and len(factory.calls) == 1, "explicit legacy completion routing keeps precedence"


def test_owned_worker_context_errors_and_uncertain_terminal() -> None:

    for terminal_failure in (False, True):
        owned_worker_failure_case(terminal_failure)


def owned_worker_failure_case(terminal_failure: bool) -> None:
    from ananta.core.state.async_job_manager import registered_action_context
    from ananta.error_handling import FrameworkError

    manager, state, _factory, _graph = _make_manager(None)
    calls: list[str] = []
    responses: list[str] = []
    manager.set_registered_completion_handler(lambda action, _status, _result, _error, _responses=responses: _responses.append(action))

    def operation(parameters: dict[str, object], *, captured: list[str] = calls, fail_terminal: bool = terminal_failure, persistence: JobState = state) -> dict[str, object]:
        captured.append(str(parameters["value"]))
        if fail_terminal:
            persistence.fail_commit = True
            return {"action_status": "completed", "data": {"effect": "finished once"}}
        raise FrameworkError(message="typed worker failure", error_code="fixture.worker_typed_failure")

    with registered_action_context("worker-action", "flow-smoke-1", "session-smoke-1"), action_execution_context("worker-token"):
        receipt = manager.submit_registered_operation("worker-control", "once", {"value": "original"}, operation)
    data = receipt["data"]
    assert isinstance(data, dict)
    job_id = str(data["job_id"])
    thread = manager._owners["worker-control"]._thread
    if thread is not None:
        thread.join(5)
        assert not thread.is_alive(), "owned fixture worker must drain"
    assert calls == ["original"], "terminal uncertainty must never replay the effect"
    assert_owned_worker_failure(manager, job_id, terminal_failure, responses)


def assert_owned_worker_failure(manager: AsyncJobManager, job_id: str, terminal_failure: bool, responses: list[str]) -> None:
    job = manager.get_job(job_id)["data"]
    assert isinstance(job, dict) and isinstance(job["job"], dict)
    if terminal_failure:
        assert job["job"]["status"] == "processing" and responses == []
    else:
        assert job["job"]["status"] == "error" and responses == ["worker-action"]
        payload = manager.get_job_payload(job_id, "error")["data"]
        assert isinstance(payload, dict) and isinstance(payload["payload"], dict)
        assert payload["payload"]["code"] == "fixture.worker_typed_failure"


def test_synchronous_residual_shares_the_owner_fifo() -> None:
    manager, state, _factory, _graph = _make_manager(None)
    entered, release, second_entered = threading.Event(), threading.Event(), threading.Event()

    def first(_parameters: dict[str, object]) -> dict[str, object]:
        entered.set()
        assert release.wait(5)
        return {"first": "durable before the next body"}

    def second(_parameters: dict[str, object]) -> dict[str, object]:
        second_entered.set()
        return {"full": "synchronous result"}

    manager.submit_registered_operation("ledger-order-control", "first", {}, first)
    assert entered.wait(5)
    count = len(state.rows["job"])
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(manager.run_registered_serial_body, "ledger-order-control", {}, second)
        try:
            assert not second_entered.wait(0.03), "a parked first body must block the second writer"
        finally:
            release.set()
        assert future.result(timeout=5) == {"full": "synchronous result"}
    assert len(state.rows["job"]) == count, "the unchanged synchronous residual must not acquire a job"


def test_owner_loss_marks_uncertain_and_drains_fifo() -> None:
    """SystemExit after an effect must not strand any registered owner."""
    for owner_name in ("session_ledger_service", "context_service"):
        owner_loss_case(owner_name)
    print("PASS owner_thread_loss_uncertain_error, FIFO_replacement, new_admission, no_replay, synchronous_failure_transport")


def owner_loss_case(owner_name: str) -> None:
    state, manager, _token, _completed = manager_fixture()
    entered, release = threading.Event(), threading.Event()
    effects: list[str] = []

    def lost(_parameters: dict[str, object]) -> dict[str, object]:
        effects.append("first-effect")
        entered.set()
        assert release.wait(5)
        raise SystemExit("owner lost after one effect")

    def next_body(parameters: dict[str, object]) -> dict[str, object]:
        effects.append(str(parameters["index"]))
        return {"index": parameters["index"]}

    first: Any = manager.submit_registered_operation(owner_name, "lost", {}, lost)
    owner: RegisteredOperationOwner = manager._owners[owner_name]
    first_thread = owner._thread
    assert first_thread is not None and entered.wait(5)
    pending: list[Any] = []
    try:
        pending = [manager.submit_registered_operation(owner_name, "next", {"index": index}, next_body) for index in (1, 2)]
    finally:
        release.set()
    first_thread.join(5)
    assert not first_thread.is_alive(), "lost worker must retire"
    replacement = owner._thread
    assert replacement is not first_thread, "dead thread reference must be cleared"
    if replacement is not None:
        replacement.join(5)
        assert not replacement.is_alive(), "replacement must drain the queued FIFO"
    lost_id = str(first["data"]["job_id"])
    assert_owner_loss_outcomes(manager, lost_id, pending, effects)
    admission_after_owner_loss(manager, owner_name, effects)
    synchronous_loss_control(manager, owner_name, state)


def admission_after_owner_loss(manager: AsyncJobManager, owner_name: str, effects: list[str]) -> None:
    owner = manager._owners[owner_name]

    def next_body(parameters: dict[str, object]) -> dict[str, object]:
        effects.append(str(parameters["index"]))
        return {"index": parameters["index"]}

    last: Any = manager.submit_registered_operation(owner_name, "after-loss", {"index": 3}, next_body)
    fresh_thread = owner._thread
    if fresh_thread is not None:
        fresh_thread.join(5)
        assert not fresh_thread.is_alive()
    job: Any = manager.get_job(str(last["data"]["job_id"]))["data"]
    assert owner._thread is None and job["job"]["status"] == "completed"
    assert effects == ["first-effect", "1", "2", "3"], "lost effect must never replay"


def assert_owner_loss_outcomes(manager: AsyncJobManager, lost_id: str, pending: list[Any], effects: list[str]) -> None:
    job: Any = manager.get_job(lost_id)["data"]
    assert job["job"]["status"] == "error" and job["job"]["status_reason"] == "owner_worker_lost_uncertain"
    error: Any = manager.get_job_payload(lost_id, "error")["data"]
    assert error["payload"]["code"] == "registered_owner.worker_lost"
    assert error["payload"]["details"]["uncertain"] is True and error["payload"]["details"]["exception_type"] == "SystemExit"
    assert effects == ["first-effect", "1", "2"], "queued successors must drain in original FIFO order"
    for receipt in pending:
        current: Any = manager.get_job(str(receipt["data"]["job_id"]))["data"]
        assert current["job"]["status"] == "completed", current


def synchronous_loss_control(manager: AsyncJobManager, owner_name: str, state: JobState) -> None:
    """The synchronous seam transports BaseException through its waiting Future."""
    count = len(state.rows["job"])

    def lost(_parameters: dict[str, object]) -> dict[str, object]:
        raise SystemExit("synchronous loss")

    try:
        manager.run_registered_serial_body(owner_name, {}, lost)
    except SystemExit as error:
        assert str(error) == "synchronous loss"
    else:
        raise AssertionError("synchronous loss must reach the caller")
    assert manager.run_registered_serial_body(owner_name, {}, lambda _parameters: {"sync": "unchanged"}) == {"sync": "unchanged"}
    assert len(state.rows["job"]) == count, "synchronous calls must never create jobs"


_RESULT_HANDLER_KEY = "plugin::_smoke_only::noop_result"  # wint:negative-fixture
_ERROR_HANDLER_KEY = "plugin::_smoke_only::noop_error"  # wint:negative-fixture

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


class _FakeActionFactory:
    """Stand-in tracking ActionFactory's real submit_action_definition surface."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def submit_action_definition(self, action_definition: dict[str, object], context: dict[str, object] | None = None) -> str:
        self.calls.append(action_definition)
        return "ae-smoke-fake-action-id"


class _FakeFlowRuntimeGraph:
    """Stand-in for FlowRuntimeGraph — none of these test jobs carry a
    flow_token_id, so complete_token must never fire; tracked, not assumed."""

    def __init__(self) -> None:
        self.complete_token_calls: list[str] = []

    def complete_token(self, token_id: str, success: bool = True, result_summary: dict[str, object] | None = None) -> None:
        self.complete_token_calls.append(token_id)


class _FakeStateService(JobState):
    """Persist real job rows and use the shared typed transaction fixture."""

    def __init__(
        self,
        metadata: dict[str, object] | None,
        trigger_type: str | None = "operator_message",
    ) -> None:
        super().__init__()
        self.rows["job"] = [{"id": f"job-smoke-{index}", "status": "queued", "metadata": metadata} for index in range(1, 9)]
        if trigger_type is not None:
            self.rows["flows"] = [{"id": "flow-smoke-1", "trigger_type": trigger_type}]


def _completion_handlers_metadata(*, flow_id: str | None = "flow-smoke-1", session_id: str = "session-smoke-1") -> dict[str, object]:
    metadata: dict[str, object] = {
        "session_id": session_id,
        "completion_handlers": {
            "result": {"process_key": _RESULT_HANDLER_KEY, "template": {}, "notes": "smoke"},
            "error": {"process_key": _ERROR_HANDLER_KEY, "template": {}, "notes": "smoke"},
        },
    }
    if flow_id is not None:
        metadata["flow_id"] = flow_id
    return metadata


def _make_manager(
    metadata: dict[str, object] | None,
    trigger_type: str | None = "operator_message",
) -> tuple[AsyncJobManager, _FakeStateService, _FakeActionFactory, _FakeFlowRuntimeGraph]:
    state = _FakeStateService(metadata=metadata, trigger_type=trigger_type)
    factory = _FakeActionFactory()
    frg = _FakeFlowRuntimeGraph()
    manager = AsyncJobManager(state_service=state, flow_runtime_graph=frg)  # type: ignore[arg-type]
    manager.set_action_factory(factory)  # type: ignore[arg-type]
    return manager, state, factory, frg


def test_happy_path_fires_completion_handler() -> None:
    """Case 1: terminal status + completion_handlers + flow_id -> submission fires."""
    manager, _state, factory, frg = _make_manager(_completion_handlers_metadata())
    result = manager.update_job("job-smoke-1", {"status": "completed", "result": {"ok": True}})
    _check(result.get("action_status") == "completed", "update_job itself reports completed")
    _check(len(factory.calls) == 1, "exactly one action submitted on terminal completion")
    if factory.calls:
        submitted = factory.calls[0]
        _check(
            submitted.get("process_key") == _RESULT_HANDLER_KEY,
            "submitted action targets the configured result completion_handler",
        )
        _check(
            submitted.get("flow_id") == "flow-smoke-1",
            "submitted action carries the job's flow_id",
        )
    _check(
        len(frg.complete_token_calls) == 0,
        "FlowRuntimeGraph.complete_token not called (job carries no flow_token_id)",
    )


def test_missing_completion_handlers_is_silent_noop() -> None:
    """Case 2: no completion_handlers in metadata -> legitimate silent no-op."""
    metadata: dict[str, object] = {"session_id": "s", "flow_id": "f"}  # no completion_handlers
    manager, _state, factory, _frg = _make_manager(metadata)
    result = manager.update_job("job-smoke-2", {"status": "completed", "result": {"ok": True}})
    _check(
        result.get("action_status") == "completed",
        "update_job still reports completed (the ledger write itself succeeds)",
    )
    _check(
        len(factory.calls) == 0,
        "no action submitted when metadata carries no completion_handlers",
    )


def test_non_terminal_status_does_not_fire() -> None:
    """Case 3: non-terminal status transition -> completion routing never runs."""
    manager, _state, factory, _frg = _make_manager(_completion_handlers_metadata())
    result = manager.update_job("job-smoke-3", {"status": "processing", "progress_percent": 50})
    _check(
        result.get("action_status") == "completed",
        "update_job reports completed for the ledger write itself",
    )
    _check(
        len(factory.calls) == 0,
        "no action submitted on a non-terminal ('processing') status transition",
    )


def test_missing_flow_id_fails_loud_not_silent() -> None:
    """Case 4: completion_handlers present, flow_id missing -> loud structured failure."""
    manager, _state, factory, _frg = _make_manager(_completion_handlers_metadata(flow_id=None))
    result = manager.update_job("job-smoke-4", {"status": "completed", "result": {"ok": True}})
    _check(
        result.get("action_status") == "error",
        "update_job surfaces a loud error, not a silent success, when flow_id is missing",
    )
    error = result.get("error")
    error_message = str(error.get("message", "")) if isinstance(error, dict) else ""
    _check(
        "flow_id" in error_message,
        f"the surfaced error names the missing flow_id, not a generic swallow (got: {error_message!r})",
    )
    _check(
        len(factory.calls) == 0,
        "no action submitted when the fail-fast flow_id check trips",
    )


def _stamped_reach(state: _FakeStateService) -> object | None:
    """The completion_reach value written to the job's metadata, if any."""
    for updates in state.update_state_calls:
        raw = updates.get("metadata")
        if not isinstance(raw, str):
            continue
        decoded = json.loads(raw)
        if isinstance(decoded, dict) and COMPLETION_REACH_KEY in decoded:
            return decoded[COMPLETION_REACH_KEY]
    return None


def test_bridge_dispatch_is_stamped_unreached() -> None:
    """Case 5: a job dispatched by a bridge is stamped as having no return path.

    Measured 2026-08-14: a completion never returns over the dispatching
    bridge — the continuation posts into an IO channel a bridge flow does not
    have — so the job row is the only place its result can be found. The
    stamp is what makes that row findable.

    (Mutation: drop the `self._record_completion_reach(job_id)` call from
    `update_job`'s terminal branch -> this case goes from PASS to FAIL.)
    """
    manager, state, _factory, _frg = _make_manager(_completion_handlers_metadata(), trigger_type="bridge_process_call")
    manager.update_job("job-smoke-5", {"status": "completed", "result": {"ok": True}})
    _check(
        _stamped_reach(state) == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
        f"a bridge_process_call flow stamps completion_reach={REACH_BRIDGE_DISPATCH_NO_RETURN_PATH}",
    )


def test_channel_flow_is_not_stamped_unreached() -> None:
    """Case 6: a job from a channel flow is stamped as reachable, not unreached.

    The negative control for case 5: without it, a stamp hardcoded to the
    unreached value would pass case 5 while making the drain verb list every
    finished job in the system.

    (Mutation: make `_record_completion_reach` always stamp
    REACH_BRIDGE_DISPATCH_NO_RETURN_PATH -> this case goes from PASS to FAIL.)
    """
    manager, state, _factory, _frg = _make_manager(_completion_handlers_metadata(), trigger_type="operator_message")
    manager.update_job("job-smoke-6", {"status": "completed", "result": {"ok": True}})
    _check(
        _stamped_reach(state) == REACH_CHANNEL_FLOW,
        f"a non-bridge flow stamps completion_reach={REACH_CHANNEL_FLOW}",
    )


def test_unreadable_flow_leaves_stamp_absent() -> None:
    """Case 7: an unreadable flow row leaves the stamp ABSENT, never guessed.

    Absent means unmeasured. Writing a default here would let the drain verb
    report a measurement nobody took.

    (Mutation: make `_record_completion_reach` fall back to
    REACH_CHANNEL_FLOW when `_read_flow_trigger_type` returns None -> this
    case goes from PASS to FAIL.)
    """
    manager, state, _factory, _frg = _make_manager(_completion_handlers_metadata(), trigger_type=None)
    result = manager.update_job("job-smoke-7", {"status": "completed", "result": {"ok": True}})
    _check(
        _stamped_reach(state) is None,
        "an unreadable flow row writes no completion_reach value at all",
    )
    _check(
        result.get("action_status") == "completed",
        "a stamp that cannot be written does not fail the completion it describes",
    )


def test_stamp_survives_a_raising_completion_handler() -> None:
    """Case 8: the stamp lands even when the continuation then fails loudly.

    This is the doctrine's second ledger gap (a terminal row whose handler
    raised, so nothing was submitted and no token resolved — a row that does
    not LOOK stuck). Stamping BEFORE `_handle_completion_actions` is what
    keeps such a row findable.

    The fixture raises INSIDE the handler while flow_id is present: a
    malformed process_key trips `_parse_process_key`'s ValueError during
    `_build_action_definition`. Forcing the raise by removing flow_id instead
    would prove nothing — with no flow to classify, the stamp is legitimately
    skipped, so that fixture measures the wrong thing.

    (Mutation: move the `record_completion_reach(...)` call to AFTER the
    `_handle_completion_actions` call -> this case goes from PASS to FAIL.)
    """
    metadata = _completion_handlers_metadata()
    handlers = metadata["completion_handlers"]
    assert isinstance(handlers, dict)
    result_handler = handlers["result"]
    assert isinstance(result_handler, dict)
    result_handler["process_key"] = "not_a_three_part_key"  # wint:negative-fixture
    manager, state, factory, _frg = _make_manager(metadata, trigger_type="bridge_process_call")
    result = manager.update_job("job-smoke-8", {"status": "completed", "result": {"ok": True}})
    _check(
        result.get("action_status") == "error",
        "the malformed-process_key failure surfaces loudly, not as a false completion",
    )
    _check(
        len(factory.calls) == 0,
        "no continuation was submitted for this job",
    )
    _check(
        _stamped_reach(state) == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH,
        "the job is still stamped, so a row whose continuation never ran stays findable",
    )


def test_fakes_track_real_signatures() -> None:
    """Rail (coordinator seat, arm-6363cec0...): protocol drift must red this smoke."""
    real_submit = list(inspect.signature(ActionFactory.submit_action_definition).parameters)
    fake_submit = list(inspect.signature(_FakeActionFactory.submit_action_definition).parameters)
    _check(
        real_submit == fake_submit,
        f"_FakeActionFactory.submit_action_definition params {fake_submit} match the real ActionFactory's {real_submit}",
    )

    for method_name in ("read_state", "write_state", "update_state"):
        real_params = list(inspect.signature(getattr(StateServiceProtocol, method_name)).parameters)
        fake_params = list(inspect.signature(getattr(_FakeStateService, method_name)).parameters)
        _check(
            real_params == fake_params,
            f"_FakeStateService.{method_name} params {fake_params} match StateServiceProtocol's {real_params}",
        )


def main() -> int:
    scheduler_plugin_single_admission()
    ordinary_plugin_admission()
    selected_declarations_and_owner_aliases()
    conditional_response_routing()
    test_continuation_guards_preserve_owned_token_routing()
    test_registered_route_and_explicit_handler_precedence()
    test_owned_worker_context_errors_and_uncertain_terminal()
    test_synchronous_residual_shares_the_owner_fifo()
    test_owner_loss_marks_uncertain_and_drains_fifo()
    print("D0.3/Option-A AsyncJobManager completion-handler smoke")
    print("\nCase 1: happy path fires the configured completion handler")
    test_happy_path_fires_completion_handler()
    print("\nCase 2: no completion_handlers -> silent, legitimate no-op")
    test_missing_completion_handlers_is_silent_noop()
    print("\nCase 3: non-terminal status -> completion routing never runs")
    test_non_terminal_status_does_not_fire()
    print("\nCase 4: missing flow_id -> loud structured failure, not swallowed")
    test_missing_flow_id_fails_loud_not_silent()
    print("\nCase 5: a bridge dispatch is stamped as having no return path")
    test_bridge_dispatch_is_stamped_unreached()
    print("\nCase 6: a channel flow is stamped reachable (negative control)")
    test_channel_flow_is_not_stamped_unreached()
    print("\nCase 7: an unreadable flow leaves the stamp absent, never guessed")
    test_unreadable_flow_leaves_stamp_absent()
    print("\nCase 8: the stamp lands even when the continuation then fails")
    test_stamp_survives_a_raising_completion_handler()
    print("\nRail: fakes track the real protocol/implementation signatures")
    test_fakes_track_real_signatures()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("PASS: AsyncJobManager completion-handler firing behaves as traced in D0.3")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
