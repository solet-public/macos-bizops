#!/usr/bin/env python3
"""Real registered scheduler routes, owned receipts and complete terminal bodies.

The default run uses the actual memory schedule repository and transactional job
fixture. --private-pg uses the existing private-PG owner fixture for schedules,
jobs and requests; it never connects to the development database.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "ananta/tests/core/actions", Path(__file__).parent):
    sys.path.insert(0, str(source))
for name in ("default_scheduling_plugin", "platform_health_plugin", "agent_messaging_plugin", "postgres_state_management_plugin"):
    sys.path.insert(0, str(ROOT / "plugins" / name / "src"))

for required in ("default_scheduling_plugin", "platform_health_plugin"):
    if not (ROOT / "plugins" / required / "src" / required / "plugin.py").is_file():
        print(f"ADVISORY scheduler admission proof skipped: optional profile plugin {required} is absent", flush=True)
        raise SystemExit(0)

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.plugin_process_scanner import PluginProcessScanner  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from ananta.core.process_registry.service_interface_metadata_generator import ServiceInterfaceMetadataGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType  # noqa: E402
from ananta.core.state.job_completion_reach import COMPLETION_REACH_KEY, REACH_BRIDGE_DISPATCH_NO_RETURN_PATH  # noqa: E402
from ananta.interfaces.state_management_interface import StateTransaction  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.scheduling_service import SchedulingService  # noqa: E402
from async_job_atomic_completion_smoke import FixtureAction  # pyright: ignore[reportMissingImports]  # noqa: E402 - candidate-root core fixture path above
from async_job_fixture_support import JobState  # pyright: ignore[reportMissingImports]  # noqa: E402 - candidate-root core fixture path above
from default_scheduling_plugin.models import ScheduleData  # noqa: E402
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402
from default_scheduling_plugin.scheduling.scheduler_manager import SchedulerManager  # noqa: E402
from default_scheduling_plugin.storage.schedule_repository import ScheduleRepository  # noqa: E402
from platform_health_plugin.plugin import PlatformHealthPlugin  # noqa: E402

VERBS = ("execute_in_seconds", "recover_scheduled_action", "get_schedule_binding_history")
TEXT = "  preserved λ\nsecond line\r\n"


def schedule() -> ScheduleData:
    return ScheduleData.model_validate({"label": "owned admission fixture", "type": "recurring", "cron_expression": "0 * * * *", "tags": ["admission-fixture"], "actions": [{"name": "plugin::agent_messaging_plugin::peer_send_by_name", "parameters": {"name": "fixture-owner", "content": TEXT}}]})


class Templates:
    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del context
        return action_def


class Harness:
    """Real factory, registry, processor, poller and manager; owned persistence."""

    def __init__(self, plugin: SchedulingPlugin, backend: Any | None = None) -> None:
        memory_state = JobState()
        memory_state.rows["flows"] = [{"id": "flow-scheduler-fixture", "status": "active", "trigger_type": "bridge_process_call"}]
        self.graph = FlowRuntimeGraph(cast(Any, memory_state))
        self.state = backend if backend is not None else memory_state
        if backend is not None:
            flow = self.state.prepare_record("core", "flows", {"id": "flow-scheduler-fixture", "core__sessions_id": "sess-scheduler-fixture", "trigger_type": "bridge_process_call", "status": "active"})
            installed = self.state.upsert_state("core", {"table": "flows", "record": flow, "conflict_columns": ["id"], "on_conflict": "do_nothing"})
            assert installed["action_status"] == "completed", "owned_flow_fixture_installed"
        self.manager = AsyncJobManager(cast(Any, self.state), self.graph)
        self.plugin, self.health = plugin, PlatformHealthPlugin()
        self.health.name = "platform_health_plugin"
        plugins = PluginManager()
        plugins.plugins.update({plugin.name: plugin, self.health.name: self.health})
        plugin.set_ready()
        self.service = SchedulingService(plugins, app_home=str(ROOT))
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager = str(ROOT), plugins
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        orchestrator.service_manager = SimpleNamespace(scheduling_service=self.service)
        plugin.set_orchestrator_ref(orchestrator)
        self.health.set_orchestrator_ref(orchestrator)
        self.records: dict[str, FixtureAction] = {}
        self.routes: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.callers: list[int] = []
        registry = self._registry(plugins)
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = registry["processes"]
        contexts = ExecutionContextManager()
        contexts.create_context("flow-scheduler-fixture")
        # Schedule storage is already bound by startup/the explicit memory
        # profile. Keep the job-only fixture out of plugin dependency injection.
        self.processor = ActionProcessor(plugins, orchestrator=orchestrator, process_registry=registry, execution_context_manager=contexts, discovery_service=discovery)
        self.factory = ActionFactory(registry, Templates(), memory_state._id_service, self, str(ROOT))
        self.poller: Any = ActionQueuePoller(cast(Any, self.state), cast(Any, self.processor), self.graph, cast(Any, self.factory), execution_context_manager=contexts, blob_storage_service=cast(Any, SimpleNamespace()), app_home=str(ROOT))
        self.poller._retrieve_action_details = self._details
        self.poller._update_action_status_to_completed = lambda *_args: None
        self.poller._store_action_result = lambda *_args: None
        self.poller._extract_and_store_attachments = lambda *_args: []
        self.poller._record_work_products_after_success = lambda *_args: None
        self.poller._submit_returned_actions = lambda *_args: True
        self.poller._maybe_dispatch_result_processing = lambda **values: self.routes.append({"action_id": values["action_id"], "result": values["result"]})
        self.poller._mark_action_failed = lambda _action, message, _token, **values: self.errors.append({"message": message, **values})

    @staticmethod
    def _registry(plugins: PluginManager) -> dict[str, Any]:
        import ananta.services.scheduling_service.interfaces.public as public

        schema = InvocationSchemaGenerator()
        registry: dict[str, Any] = {"processes": {}}
        scanner = PluginProcessScanner(plugins, PluginRegistrationValidator(), ServiceInterfaceMetadataGenerator(), schema)
        plugin = plugins.plugins["default_scheduling_plugin"]
        for metadata in plugin.get_available_actions():
            scanner._register_plugin_process(plugin.name, metadata, registry)
        ServiceInterfaceScanner(schema)._scan_module_classes(public, public.__name__, registry["processes"])
        return registry

    def store_action_event(self, action: dict[str, object]) -> str:
        row = self.state.prepare_record(
            "core", "action_events", {"core__sessions_id": "sess-scheduler-fixture", "core__flows_id": "flow-scheduler-fixture", "sequence": len(self.records) + 1, "process_key": action["process_key"], "parameters": json.dumps(action["parameters"]), "notes": action["notes"], "status": "processing"}
        )
        action_id = str(row["id"])
        token = self.graph.create_token("flow-scheduler-fixture", TokenOwnerType.PROCESS, action_id)
        row["flow_token_id"] = token
        row["created_at"] = datetime.now(UTC).isoformat()
        with self.state.transactional() as transaction:
            assert transaction.write_state("core", {"table": "action_events", "record": row}) == action_id
        self.records[action_id] = FixtureAction(action_id, str(action["process_key"]), str(row["parameters"]), str(action["notes"]), str(row["created_at"]), session_id="sess-scheduler-fixture", flow_id="flow-scheduler-fixture", flow_token_id=token, source_plugin="platform_health_plugin")
        return action_id

    def action(self, route: str, verb: str, parameters: dict[str, Any]) -> FixtureAction:
        key = f"plugin::default_scheduling_plugin::{verb}" if route == "plugin" else f"service_interface::scheduling_service::{verb}"
        action_id = self.factory.submit_action_definition({"process_key": key, "arguments": parameters, "notes": "owned scheduler admission", "session_id": "sess-scheduler-fixture", "flow_id": "flow-scheduler-fixture", "error_processor_kind": "bridge_delivery"})
        return self.records[action_id]

    def _details(self, action_id: str, *, parse_parameters: bool = True) -> tuple[Any, ...]:
        action = self.records[action_id]
        return (action.process_key, action.notes, None, None, "sess-scheduler-fixture", "flow-scheduler-fixture", None, json.loads(action.parameters) if parse_parameters else None, None, None, None)

    def dispatch(self, action: FixtureAction) -> tuple[dict[str, Any], float]:
        self.callers.append(threading.get_ident())
        before = time.perf_counter()
        result = self.processor.execute_action(action)
        return result, (time.perf_counter() - before) * 1000

    def join(self) -> None:
        worker = self.health._operation_jobs._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "scheduler_owner_drained"

    def result(self, job_id: str) -> dict[str, Any]:
        self.join()
        readback = self.manager.get_job_payload(job_id, "result")
        assert readback["action_status"] == "completed", readback
        return cast(Any, readback)["data"]["payload"]


class ActivationCAS:
    """A real second connection wins once before the activation no-op CAS."""

    def __init__(self, transaction: StateTransaction, contender: ScheduleRepository, attempts: list[int], park: Callable[[], None]) -> None:
        self.transaction, self.contender, self.attempts, self.park = transaction, contender, attempts, park

    def __getattr__(self, name: str) -> Any:
        return getattr(self.transaction, name)

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> int:
        activation = namespace == "default_scheduling_plugin" and query["table"] == "schedules" and set(updates) == {"revision"}
        if activation:
            self.attempts[0] += 1
            if self.attempts[0] == 1:
                sid = query["filters"]["id"]
                current = self.contender.load_schedule(sid)
                assert current is not None
                self.contender.mutate(sid, current.revision, lambda row: row, operation="rebind", actor="owned-second-connection", reason="same-target correction before activation CAS")
        affected = self.transaction.update_state(namespace, query, updates)
        if activation and self.attempts[0] == 1:
            assert affected == 0, "real_independent_connection_causes_activation_cas_retry"
            self.park()
        return affected


@contextmanager
def parked_leaf(plugin: SchedulingPlugin, repo: ScheduleRepository, verb: str, entered: threading.Event, release: threading.Event, reached: list[int], contender: ScheduleRepository | None = None) -> Generator[None]:
    def park() -> None:
        reached.append(threading.get_ident())
        print(f"LEAF {verb} reached thread={reached[-1]}", flush=True)
        entered.set()
        assert release.wait(5), "owned_test_releases_leaf"

    if verb == "execute_in_seconds":

        def remember(**_values: Any) -> dict[str, Any]:
            park()
            return {"action_status": "completed", "data": {"memory_id": "owned-memory"}}

        with patch.object(plugin, "_memory_service", SimpleNamespace(remember=remember)):
            yield
    elif verb == "get_schedule_binding_history":
        history = plugin._memory_repository._history_rows

        def walk(*args: Any) -> list[dict[str, Any]]:
            park()
            return history(*args)

        with patch.object(plugin._memory_repository, "_history_rows", walk):
            yield
    elif contender is not None:
        state = repo._state()
        transactional = state.transactional
        attempts = [0]

        @contextmanager
        def contended() -> Generator[StateTransaction]:
            with transactional() as transaction:
                yield cast(StateTransaction, ActivationCAS(transaction, contender, attempts, park))

        with patch.object(state, "transactional", contended):
            yield
        assert attempts[0] >= 2, "entire_persistent_cas_retry_and_activation_ran_on_worker"
    else:
        dispatch = repo.dispatch_row

        @contextmanager
        def activation(schedule_id: str) -> Generator[Any]:
            park()
            with dispatch(schedule_id) as current:
                yield current

        with patch.object(repo, "dispatch_row", activation):
            yield


def family_setup(verb: str, plugin: SchedulingPlugin | None, repo: ScheduleRepository | None) -> tuple[SchedulingPlugin, ScheduleRepository, str, str, dict[str, Any]]:
    if plugin is None:
        plugin = SchedulingPlugin()
        plugin.configure_memory_store()
        repo = plugin._memory_repository
    assert repo is not None
    plugin.logger.disabled = True
    plugin._scheduler_manager = SchedulerManager()
    plugin._scheduler_manager.initialize()
    sid, ordinary_id = repo.save_schedule(schedule()), repo.save_schedule(schedule())
    for _index in range(80):
        plugin._memory_repository.save_schedule(schedule())
    if verb == "recover_scheduled_action":
        repo.update_schedule_status(sid, "error", "owned failure")
        parameters: dict[str, Any] = {"schedule_id": sid, "expected_revision": 1, "reason": "explicit recovery"}
    elif verb == "get_schedule_binding_history":
        parameters = {"schedule_id": sid}
    else:
        parameters = {"seconds": 86400, "memory_tag": "admission-fixture", "content": TEXT, "tags": ["admission-fixture"]}
    return plugin, repo, sid, ordinary_id, parameters


def assert_receipt(h: Harness, action: FixtureAction, receipt: dict[str, Any], elapsed: float, reached: list[int]) -> str:
    assert receipt["success"] and receipt["data"]["status"] == "queued", receipt
    assert elapsed < 50, f"registered_admission_under_50ms: {elapsed:.3f}"
    assert reached[-1] != h.callers[-1], "actual_leaf_off_dispatch_caller"
    job_id = receipt["data"]["job_id"]
    job = cast(Any, h.manager.get_job(job_id))["data"]["job"]
    request = cast(Any, h.manager.get_job_payload(job_id))["data"]["payload"]
    assert job["provider_name"] == f"{h.plugin.name}.{action.process_key.split('::')[-1]}" and job["group_id"] == f"registered:{action.id}", "canonical_job_and_server_owned_lineage"
    assert job["flow_token_id"] == action.flow_token_id, "actual_owned_token"
    assert len(json.dumps(request)) < 512 and TEXT not in json.dumps(request), "bounded_descriptor_without_operational_payload"
    h.poller._mark_action_completed(action.id, receipt, action.flow_token_id)
    assert h.routes == [], "receipt_does_not_route_terminal_completion"
    return str(job_id)


def family_case(route: str, verb: str, *, plugin: SchedulingPlugin | None = None, repo: ScheduleRepository | None = None, backend: Any | None = None, contender: ScheduleRepository | None = None) -> None:
    plugin, repo, sid, ordinary_id, parameters = family_setup(verb, plugin, repo)
    h = Harness(plugin, backend)
    action = h.action(route, verb, parameters)
    entered, release = threading.Event(), threading.Event()
    reached: list[int] = []
    with parked_leaf(plugin, repo, verb, entered, release, reached, contender), ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(h.dispatch, action)
        try:
            assert entered.wait(3), f"valid_setup_reaches_{verb}_leaf: {future.result(timeout=1) if future.done() else 'not reached'}"
            try:
                receipt, elapsed = future.result(timeout=0.15)
            except TimeoutError:
                raise AssertionError(f"registered_{route}_{verb}_must_return_admission_while_leaf_parked; leaf_reached=true caller_thread={h.callers[-1]} leaf_thread={reached[-1]}") from None
            job_id = assert_receipt(h, action, receipt, elapsed, reached)
            ordinary = h.action("plugin", "rebind_scheduled_action", {"schedule_id": ordinary_id, "action_index": 0, "target_kind": "role", "target_value": "corrected", "expected_revision": 0, "reason": "ordinary action"})
            assert h.dispatch(ordinary)[0]["success"] and not release.is_set(), "ordinary_action_dispatches_during_parked_activity"
            queued_action = h.action("service", "get_schedule_binding_history", {"schedule_id": sid})
            queued, queued_elapsed = h.dispatch(queued_action)
            assert queued["success"] and queued_elapsed < 50, "successor_admitted_without_waiting_for_owner"
            queued_job = cast(Any, h.manager.get_job(queued["data"]["job_id"]))["data"]["job"]
            assert queued_job["status"] == "queued" and not release.is_set(), "one_serial_owner_keeps_successor_out_of_body"
            h.poller._mark_action_completed(queued_action.id, queued, queued_action.flow_token_id)
            print(f"ADMISSION {route} {verb} {elapsed:.3f}ms private_pg={backend is not None}", flush=True)
        finally:
            release.set()
            returned = future.result(timeout=5)[0]
            h.join()
            assert returned["success"], f"valid_released_body_completes: {returned}"
    result = assert_terminal_lineage(h, action, queued_action, job_id, queued["data"]["job_id"])
    assert_family_outcome(verb, repo, result)
    direct = plugin.get_schedule_binding_history({"schedule_id": sid}, {})
    assert direct["action_status"] == "completed" and "events" in direct["data"] and "job_id" not in direct["data"], "internal_python_history_stays_synchronous"
    print(f"PASS {route} {verb}: real_factory_registry_processor_poller, bounded_receipt, full_terminal_result", flush=True)


def assert_terminal_lineage(h: Harness, action: FixtureAction, successor: FixtureAction, job_id: str, successor_job: str) -> dict[str, Any]:
    result = h.result(job_id)
    assert result["action_status"] == "completed" and [row["action_id"] for row in h.routes] == [action.id, successor.id] and not h.errors, "one_full_terminal_completion_per_outer_action_in_order"
    terminal_job = cast(Any, h.manager.get_job(job_id))["data"]["job"]
    assert json.loads(terminal_job["metadata"])[COMPLETION_REACH_KEY] == REACH_BRIDGE_DISPATCH_NO_RETURN_PATH, "channel_less_completion_reach_measured_and_readback_only"
    assert h.result(successor_job)["data"]["events"], "queued_history_observes_prior_operation"
    return result


def assert_family_outcome(verb: str, repo: ScheduleRepository, result: dict[str, Any]) -> None:
    if verb == "execute_in_seconds":
        assert result["data"]["delay_seconds"] == 86400 and repo.load_schedule(result["data"]["schedule_id"]) is not None, "full_delayed_schedule_result"
    elif verb == "get_schedule_binding_history":
        assert result["data"]["events"][0]["operation"] == "create", "complete_history_after_memory_scan_and_persistent_fallback"
    else:
        assert result["data"]["outcome"] == "recovered" and result["data"]["effective"], "entire_recovery_and_activation_result"


def invoke(h: Harness, route: str, verb: str, parameters: dict[str, Any]) -> tuple[FixtureAction, str, dict[str, Any]]:
    action = h.action(route, verb, parameters)
    receipt, _elapsed = h.dispatch(action)
    assert receipt["success"] and receipt["data"]["status"] == "queued", receipt
    h.poller._mark_action_completed(action.id, receipt, action.flow_token_id)
    job_id = str(receipt["data"]["job_id"])
    return action, job_id, h.result(job_id)


def argument_transfer_and_internal_composition() -> None:
    plugin = SchedulingPlugin()
    plugin.configure_memory_store()
    repo = plugin._memory_repository
    sid = repo.save_schedule(schedule())
    h = Harness(plugin)
    content = "owned text λ\n" * 150000
    seen: list[dict[str, Any]] = []

    def remember(**values: Any) -> dict[str, Any]:
        seen.append(values)
        assert cast(Any, h.service.get_schedule_binding_history(sid))["data"]["events"], "worker_internal_dependency_keeps_final_data_without_self_deadlock"
        return {"action_status": "completed", "data": {"memory_id": "owned-memory"}}

    plugin._memory_service = SimpleNamespace(remember=remember)
    action = h.action("service", "execute_in_seconds", {"seconds": 86400, "memory_tag": "owned-transfer", "content": content, "state": {"session_id": "spoof", "flow_id": "spoof", "call_context": {"calling_plugin": "spoof"}}})
    entered, release = threading.Event(), threading.Event()
    reached: list[int] = []
    original = h.processor._prepare_arguments

    def prepare(owned_action: Any) -> dict[str, object] | None:
        reached.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "argument_preparation_released"
        return original(owned_action)

    with patch.object(h.processor, "_prepare_arguments", prepare), ThreadPoolExecutor(max_workers=1) as caller:
        future = caller.submit(h.dispatch, action)
        try:
            receipt, elapsed = future.result(timeout=1)
            assert entered.wait(2), "owned_argument_preparation_reached"
            job_id = assert_receipt(h, action, receipt, elapsed, reached)
            assert seen == [], "no_preparation_or_embedding_before_persisted_receipt"
        finally:
            release.set()
            h.join()
    result = h.result(job_id)
    saved = repo.load_schedule(result["data"]["schedule_id"])
    assert saved is not None and saved.session_id == "sess-scheduler-fixture" and saved.flow_id == "flow-scheduler-fixture", "server_context_overwrites_request_state"
    assert saved.tags == ["owned-transfer"] and seen == [{"content": content, "tags": ["owned-transfer"], "session_id": "sess-scheduler-fixture"}], "actual_service_normalization_and_exact_large_content"
    assert len(h.state.rows["job"]) == 1 and len(h.routes) == 1, "one_job_and_completion_despite_internal_composition"
    print("PASS exclusive_argument_transfer, worker_preparation, trusted_state, actual_service_tags, reentrant_full_result")


def lifecycle_and_typed_errors() -> None:
    plugin = SchedulingPlugin()
    plugin.configure_memory_store()
    repo = plugin._memory_repository
    sid = repo.save_schedule(schedule())
    h = Harness(plugin)
    repo.update_schedule_status(sid, "error", "owned error")
    _action, _job, recovered = invoke(h, "plugin", "recover_scheduled_action", {"schedule_id": sid, "expected_revision": 1, "reason": "explicit recover"})
    assert recovered["data"]["activation_status"] == "pending" and not recovered["data"]["effective"], "pending_activation_advisory_preserved"
    history = repo.history(sid)
    plugin._scheduler_manager = SchedulerManager()
    plugin._scheduler_manager.initialize()
    _action, _job, retry = invoke(h, "service", "recover_scheduled_action", {"schedule_id": sid, "expected_revision": 2, "reason": "explicit retry"})
    assert retry["data"]["outcome"] == "already_scheduled" and retry["data"]["effective"] and repo.history(sid) == history, "current_revision_retry_no_duplicate_history"
    _action, failed_job, failed = invoke(h, "service", "recover_scheduled_action", {"schedule_id": sid, "expected_revision": 1, "reason": "stale retry"})
    job = cast(Any, h.manager.get_job(failed_job))["data"]["job"]
    assert job["status"] == "error" and failed["error"]["code"] == "scheduling.stale_revision" and repo.history(sid) == history, "typed_stale_failure_and_no_writes"
    assert cast(Any, h.manager.get_job_payload(failed_job, "error"))["data"]["payload"] == failed["error"], "exact_typed_job_error_readback"
    correction_reversal_and_clear(h, repo, sid)
    assert len(h.state.rows["job"]) == 5 and len(h.routes) == 3 and len(h.errors) == 2, "one_job_and_one_terminal_route_per_invocation"
    print("PASS pending_recovery, already_scheduled_retry, stale_write_refusal, correction_reversal, clear_and_historical_read, terminal_no_reopen")


def correction_reversal_and_clear(h: Harness, repo: ScheduleRepository, sid: str) -> None:
    plugin = h.plugin
    for revision, target in ((2, "corrected"), (3, "fixture-owner")):
        assert plugin.rebind_scheduled_action({"schedule_id": sid, "action_index": 0, "target_kind": "role", "target_value": target, "expected_revision": revision, "reason": "correction/reversal"}, {})["action_status"] == "completed", "binding_correction_and_reversal"
    assert cast(Any, h.service.clear_scheduled_action(sid))["data"]["cancelled"], "direct_clear_keeps_full_result"
    _action, _job, after_clear = invoke(h, "service", "get_schedule_binding_history", {"schedule_id": sid})
    assert [event["operation"] for event in after_clear["data"]["events"]] == ["create", "status", "recover", "rebind", "rebind", "clear"], "immutable_history_after_correction_reversal_and_clear"
    assert after_clear["data"]["events"][-1]["snapshot"]["actions"][0]["parameters"]["content"] == TEXT, "historical_original_text"
    _action, _job, terminal = invoke(h, "plugin", "recover_scheduled_action", {"schedule_id": sid, "expected_revision": 4, "reason": "cannot reopen cleared row"})
    assert terminal["error"]["code"] == "scheduling.schedule_not_found" and repo.load_schedule(sid) is None, "terminal_clear_never_resurrects"


def worker_loss_and_uncertain_terminal_write() -> None:
    for failure in ("worker", "terminal_write", "notification"):
        uncertain_case(failure)


def uncertain_case(failure: str) -> None:
    plugin = SchedulingPlugin()
    plugin.configure_memory_store()
    h = Harness(plugin)
    effects: list[str] = []

    def remember(**_values: Any) -> dict[str, Any]:
        effects.append("remember")
        if failure == "worker":
            raise SystemExit("owned worker loss")
        if failure == "terminal_write":
            h.state.fail_update = "job"
        return {"action_status": "completed", "data": {"memory_id": "owned-memory"}}

    plugin._memory_service = SimpleNamespace(remember=remember)
    if failure == "notification":

        def notify(**_values: Any) -> None:
            raise RuntimeError("owned notification failure")

        h.poller._maybe_dispatch_result_processing = notify
    action = h.action("plugin", "execute_in_seconds", {"seconds": 86400, "memory_tag": "owned-failure", "content": TEXT})
    receipt, _elapsed = h.dispatch(action)
    assert receipt["success"] and receipt["data"]["status"] == "queued", receipt
    h.join()
    job_id = str(receipt["data"]["job_id"])
    job = cast(Any, h.manager.get_job(job_id))["data"]["job"]
    assert effects == ["remember"], "uncertain_effect_never_replayed"
    if failure == "worker":
        assert job["status"] == "error" and h.result(job_id)["error"]["code"] == "registered_owner.worker_lost", "worker_loss_typed_uncertain_error"
    elif failure == "terminal_write":
        assert job["status"] == "processing" and len(plugin._memory_repository.memory_schedules) == 1, "failed_terminal_write_retains_uncertainty_after_effect"
        h.state.fail_update = ""
    else:
        assert job["status"] == "completed" and h.result(job_id)["data"]["schedule_id"], "notification_failure_does_not_erase_committed_result"
    h.poller._maybe_dispatch_result_processing = lambda **values: h.routes.append(values["result"])
    _next, _job, successor = invoke(h, "service", "get_schedule_binding_history", {"schedule_id": "missing"})
    assert successor["data"]["events"] == [] and effects == ["remember"] and len(h.state.rows["job"]) == 2, "untouched_successor_progresses_without_replaying_lost_body"
    print(f"PASS {failure}: visible_outcome_or_reconciliation, no_replay, successor_progress")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-pg", action="store_true")
    args = parser.parse_args()
    failures: list[str] = []
    if args.private_pg:
        from schedule_target_rebind_pg_smoke import Fixture

        fixture = Fixture()
        try:
            fixture.open()
            for route in ("plugin", "service"):
                for verb in VERBS:
                    family_case(route, verb, plugin=fixture.plugins[0], repo=fixture.repos[0], backend=fixture.states[0], contender=fixture.repos[1])
        finally:
            fixture.close()
    else:
        for route in ("plugin", "service"):
            for verb in VERBS:
                try:
                    family_case(route, verb)
                except AssertionError as error:
                    failures.append(str(error))
                    print("FAIL", error, flush=True)
        argument_transfer_and_internal_composition()
        lifecycle_and_typed_errors()
        worker_loss_and_uncertain_terminal_write()
        print("ADVISORY private-PG matrix runs separately through provision_test_pg.py --exec", flush=True)
    assert not failures, "\n".join(failures)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
