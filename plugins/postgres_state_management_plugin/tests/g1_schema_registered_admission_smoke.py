"""Real registered schema lifecycle admission and private PostgreSQL proof.

Uses the shipped binding, scanner, factory/recorder, poller preflight, processor,
SchemaManager and provider. Only queue transport/output ports are observed.
An absent private fixture is an advisory skip, never a profile database lookup.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/postgres_state_management_plugin/src"):
    sys.path.insert(0, str(source))

from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.orchestration.managers.action_event_recorder import ActionEventRecorder  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.orchestration.service_bindings import BindingSource, ServiceBinding, ServiceBindings, ServiceName  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.execution_token_context import get_current_flow_token_id  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.plugin_schema_service.serialization import to_json  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema  # noqa: E402
from postgres_state_management_plugin.plugin import PostgresStatePlugin  # noqa: E402

from quality_gates.private_test_pg import LedgerFixture, MissingFixtureError, ledger_fixture, skip_fixture  # noqa: E402

SELECTED = ("install_plugin_schema", "update_plugin_schema", "uninstall_plugin_schema", "purge_plugin_schema")
OWNER = "plugin_schema_service"
NAMESPACE = "owned_schema_admission"
PREFIX = f"service_interface::{OWNER}::"


def declaration(*, expanded: bool = False) -> SchemaDefinition:
    columns = {
        "kind": ColumnDefinition(type=ColumnType.TEXT, check="kind IN ('a', 'b', 'c')" if expanded else "kind IN ('a', 'b')"),
        "label": ColumnDefinition(type=ColumnType.TEXT),
    }
    if expanded:
        columns["note"] = ColumnDefinition(type=ColumnType.TEXT)
    return SchemaDefinition(namespace=NAMESPACE, tables={"thing": TableSchema(table_name="thing", id_prefix="ost", columns=columns)})


def values(verb: str, *, expanded: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"plugin_namespace": NAMESPACE}
    if verb in SELECTED[:2]:
        result["declared_schema_json"] = to_json(declaration(expanded=expanded))
    return result


def data(response: Any) -> dict[str, Any]:
    assert response.get("action_status") == "completed", response
    return cast(dict[str, Any], response["data"])


class LiteralTemplates:
    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del context
        return action_def


class Harness:
    def __init__(self, pg: LedgerFixture) -> None:
        self.state, self.home = pg.state, pg.app_home
        self.plugin = cast(PostgresStatePlugin, pg.plugin)
        self.schemas = SchemaManager(self.state, self.plugin)
        self.schemas.initialize_schemas(CoreSchemaDefinitions.get_all_core_schemas())
        self.state.set_schema_manager(self.schemas)
        self.graph = FlowRuntimeGraph(self.state)
        self.manager = AsyncJobManager(self.state, self.graph)
        self.jobs = JobService(self.state, self.manager)
        plugins = PluginManager()
        plugins.plugins[self.plugin.name] = self.plugin
        bindings = ServiceBindings(str(self.home))
        bindings._bindings = {ServiceName.PLUGIN_SCHEMA_SERVICE: ServiceBinding(ServiceName.PLUGIN_SCHEMA_SERVICE, self.plugin.name, BindingSource.CONFIG)}
        bindings._loaded = True
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager, orchestrator.service_bindings = str(self.home), plugins, bindings
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        orchestrator.service_manager = SimpleNamespace(job_service=self.jobs)
        assert orchestrator.get_service(OWNER) is self.plugin, "actual shipped schema binding must resolve the provider"
        self.plugin.set_orchestrator_ref(orchestrator)
        self.processes: dict[str, Any] = {}
        scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
        for provider in (OWNER, "job_service", "inference_service"):
            module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
            scanner._scan_module_classes(module, module.__name__, self.processes)
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.processes
        self.contexts = ExecutionContextManager()
        self.flows = FlowManager(self.state)
        recorder = ActionEventRecorder(self.state, self.flows, self.graph)
        self.factory = ActionFactory({"processes": self.processes}, LiteralTemplates(), cast(Any, self.state), recorder, str(self.home))
        self.processor = ActionProcessor(plugins, state_service=self.state, orchestrator=orchestrator, process_registry={"processes": self.processes}, execution_context_manager=self.contexts, discovery_service=discovery)
        self.session = str(data(self.state.write_state("core", {"table": "sessions", "record": {"context_type": "owned_schema_fixture", "status": "active"}}))["result"]["generated_id"])
        self.poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        self.poller.action_processor, self.poller._discovery_service = self.processor, discovery
        self.poller._flow_runtime_graph, self.poller.execution_context_manager = self.graph, self.contexts
        self.receipts: dict[str, dict[str, Any]] = {}
        self.timings: dict[str, float] = {}
        self.routes: list[tuple[str, str, object, object]] = []
        self.manager.set_registered_completion_handler(lambda action, status, result, error: self.routes.append((action, status, result, error)))

    def action(self, verb: str, arguments: dict[str, Any], *, provider: str = OWNER) -> QueuedAction:
        flow = self.flows.create_flow(self.session, "owned_schema_fixture", {})
        self.contexts.create_context(flow)
        key = f"service_interface::{provider}::{verb}"
        identity = self.factory.submit_action_definition({"name": f"owned_{verb}", "process_key": key, "arguments": arguments, "notes": "owned schema admission proof", "flow_id": flow, "session_id": self.session, "result_processor_kind": None, "error_processor_kind": None})
        rows = data(self.state.read_state("core", {"table": "action_events", "filters": {"id": identity}, "limit": 1}))["records"]
        assert len(rows) == 1 and rows[0]["flow_token_id"], "real factory/recorder persisted action and token"
        row = rows[0]
        return QueuedAction(identity, key, row["parameters"], row["notes"], str(row["sequence"]), session_id=self.session, flow_id=flow, flow_token_id=row["flow_token_id"])

    async def dispatch(self, action: QueuedAction) -> dict[str, Any]:
        assert self.poller._get_canonical_arguments_schema(action.process_key), "actual registered schema reaches transport preflight"

        def completed(identity: str, result: dict[str, Any], _token: str | None, *, on_status_written: Callable[[], None]) -> None:
            self.receipts[identity] = result
            on_status_written()

        def failed(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"registered dispatch failed: {args!r} {kwargs!r}")

        with ExitStack() as stack:
            stack.enter_context(patch.object(self.poller, "_is_terminated_flow_sibling", return_value=False))
            stack.enter_context(patch.object(self.poller, "_resolve_io_context", return_value=None))
            stack.enter_context(patch.object(self.poller, "_mark_action_completed", side_effect=completed))
            stack.enter_context(patch.object(self.poller, "_mark_action_failed", side_effect=failed))
            started = time.perf_counter()
            await self.poller._process_action(action)
            self.timings[action.id] = (time.perf_counter() - started) * 1000
        return self.receipts[action.id]

    def join(self) -> None:
        for owner in list(self.manager._owners.values()):
            worker = owner._thread
            if worker is not None:
                worker.join(5)
                assert not worker.is_alive(), "owned schema worker must drain before fixture retirement"

    def terminal(self, receipt: dict[str, Any], *, status: str = "completed") -> dict[str, Any]:
        self.join()
        identity = receipt["data"]["job_id"]
        job = data(self.jobs.get_job(identity))["result"]["job"]
        assert job["id"] == identity and job["status"] == status, job
        return cast(dict[str, Any], job)

    def final(self, verb: str, arguments: dict[str, Any], *, status: str = "completed") -> dict[str, Any]:
        action = self.action(verb, arguments)
        receipt = asyncio.run(self.dispatch(action))
        assert_lineage(self, action, receipt)
        job = self.terminal(receipt, status=status)
        assert len([route for route in self.routes if route[0] == action.id]) == 1
        return job


def assert_lineage(harness: Harness, action: QueuedAction, receipt: dict[str, Any]) -> None:
    assert {"action_status", "data", "actions", "error", "timestamp"} <= receipt.keys()
    identity = receipt["data"]["job_id"]
    job = data(harness.manager.get_job(identity))["job"]
    rows = data(harness.state.read_state("core", {"table": "job", "filters": {"group_id": f"registered:{action.id}"}}))["records"]
    assert [row["id"] for row in rows] == [identity], "one canonical job per registered invocation"
    assert job["flow_token_id"] == action.flow_token_id
    metadata = json.loads(job["metadata"])
    assert metadata["registered_action_id"] == action.id and metadata["flow_id"] == action.flow_id and metadata["session_id"] == action.session_id
    request = data(harness.manager.get_job_payload(identity))["payload"]
    assert len(json.dumps(request)) < 512 and "declared_schema_json" not in request["parameters"], "only bounded public descriptor persists"


def parked_case(harness: Harness, verb: str) -> None:
    lifecycle = harness.plugin._get_lifecycle()
    leaf_name = "_hydrate_and_standardize" if verb in SELECTED[:2] else "_read_ownership"
    original = getattr(lifecycle, leaf_name)
    entered, release = threading.Event(), threading.Event()
    leaf_threads: list[int] = []
    parse_threads: list[int] = []
    action = harness.action(verb, values(verb, expanded=verb == "update_plugin_schema"))
    control = harness.action("get_job", {"job_id": "owned-absent-job"}, provider="job_service")
    parse = harness.processor._parse_action_parameters

    def leaf(*args: Any, **kwargs: Any) -> Any:
        leaf_threads.append(threading.get_ident())
        assert get_current_flow_token_id() == action.flow_token_id
        entered.set()
        assert release.wait(5), "owned schema leaf must be released"
        return original(*args, **kwargs)

    def observed_parse(queued: Any) -> dict[str, object]:
        if queued.id == action.id:
            parse_threads.append(threading.get_ident())
        return parse(queued)

    timer = threading.Timer(1, release.set)
    timer.start()
    with patch.object(lifecycle, leaf_name, side_effect=leaf), patch.object(harness.processor, "_parse_action_parameters", side_effect=observed_parse):
        try:
            receipt = asyncio.run(harness.dispatch(action))
            assert entered.wait(2), "valid declaration reached actual lifecycle preflight/effect"
            print(json.dumps({"verb": verb, "elapsed_ms": harness.timings[action.id], "leaf_reached": True, "receipt_keys": sorted(receipt.get("data", {}))}), flush=True)
            assert "job_id" in receipt.get("data", {}), "whole_schema_lifecycle_returns_job_before_parked_leaf"
            assert harness.timings[action.id] < 50, "schema admission under census 50 ms line"
            assert_lineage(harness, action, receipt)
            assert not release.is_set(), "durable receipt precedes lifecycle effect completion"
            worker = harness.manager._owners[OWNER]._thread
            assert worker is not None and parse_threads == leaf_threads == [worker.ident], "whole preparation/effect runs on one owner"
            ordinary = asyncio.run(harness.dispatch(control))
            assert "job_id" not in ordinary.get("data", {}) and not release.is_set(), "another ordinary registered action dispatches while schema work is parked"
        finally:
            release.set()
            timer.cancel()
            timer.join()
            harness.join()
    job = harness.terminal(receipt)
    assert job["error"] is None
    assert len([route for route in harness.routes if route[0] == action.id]) == 1
    print(f"PASS {verb}: persisted receipt, real leaf, ordinary dispatch, one completion")


def lifecycle_cases(harness: Harness) -> None:
    startup_and_reentrant_calls(harness)
    corrected = schema_corrections(harness)
    schema_reversal_and_terminal(harness, corrected)
    print("PASS startup/reentrant sync, install/read/update, stale-check refusal, inactive/reactivate and force purge, retained terminal history")


def startup_and_reentrant_calls(harness: Harness) -> None:
    assert harness.plugin.get_installed_schema(NAMESPACE)["tables"] == {}, "purge removed ownership"
    before = len(data(harness.state.read_state("core", {"table": "job", "limit": 100}))["records"])
    lifecycle = harness.plugin._get_lifecycle()
    original = lifecycle._table_exists_on_disk

    def reentrant(namespace: str, table: str) -> bool:
        assert harness.plugin.get_installed_schema(namespace)["tables"] == {}, "same-thread nested lifecycle read retains full result"
        return bool(original(namespace, table))

    with patch.object(lifecycle, "_table_exists_on_disk", side_effect=reentrant):
        SchemaManager(harness.state, harness.plugin).initialize_schemas([declaration()])
    assert harness.plugin.install_plugin_schema(NAMESPACE, to_json(declaration()))["status"] == "no_op"
    assert len(data(harness.state.read_state("core", {"table": "job", "limit": 100}))["records"]) == before, "startup/direct calls never create a bookkeeping job"
    installed = harness.plugin.get_installed_schema(NAMESPACE)["tables"]["thing"]
    assert installed["status"] == "active" and installed["installed_at"]
    assert data(harness.state.read_state(NAMESPACE, {"table": "thing", "limit": 1}))["records"] == [], "purge removed the old data as well as ownership"


def schema_corrections(harness: Harness) -> dict[str, Any]:
    data(harness.state.write_state(NAMESPACE, {"table": "thing", "record": {"kind": "a", "label": "owned surviving data"}}))
    corrected = harness.final("update_plugin_schema", values("update_plugin_schema", expanded=True))
    assert corrected["result"]["status"] == "updated"
    expanded = harness.plugin.get_installed_schema(NAMESPACE)
    assert "note" in expanded["tables"]["thing"]["schema_snapshot"]["columns"]
    stale = values("update_plugin_schema", expanded=True)
    stale["declared_schema_json"]["tables"]["thing"]["columns"]["kind"]["check"] = "kind IN ('a', 'b')"
    refused = harness.final("update_plugin_schema", stale, status="error")
    assert refused["error"] and "column" in str(refused["result"]).lower()
    assert harness.plugin.get_installed_schema(NAMESPACE) == expanded, "stale shrinking declaration changes neither ownership nor data"
    protected = values("update_plugin_schema", expanded=True)
    protected["declared_schema_json"]["tables"]["thing"]["columns"]["id"] = {"type": "TEXT", "primary_key": True}
    harness.final("update_plugin_schema", protected, status="error")
    return corrected


def schema_reversal_and_terminal(harness: Harness, corrected: dict[str, Any]) -> None:
    active_purge = harness.final("purge_plugin_schema", values("purge_plugin_schema"), status="error")
    assert "active" in str(active_purge["error"])
    inactive = harness.final("uninstall_plugin_schema", values("uninstall_plugin_schema"))
    assert inactive["result"]["data_preserved"] is True
    snapshot = harness.plugin.get_installed_schema(NAMESPACE)
    assert snapshot["tables"]["thing"]["status"] == "inactive"
    harness.final("update_plugin_schema", values("update_plugin_schema", expanded=True), status="error")
    restored = harness.final("install_plugin_schema", values("install_plugin_schema", expanded=True))
    assert restored["result"]["status"] == "reactivated" and restored["result"]["data_preserved"] is True
    rows = data(harness.state.read_state(NAMESPACE, {"table": "thing", "limit": 1}))["records"]
    assert rows[0]["label"] == "owned surviving data", "uninstall/reactivate preserves real data"
    assert snapshot["tables"]["thing"]["status"] == "inactive", "earlier readback is unchanged"
    old = dict(corrected["result"])
    assert harness.manager.update_job(corrected["id"], {"status": "completed", "result": {"wrong": True}})["action_status"] == "error"
    assert data(harness.jobs.get_job(corrected["id"]))["result"]["job"]["result"] == old, "terminal history cannot be overwritten"
    forced = harness.final("purge_plugin_schema", {**values("purge_plugin_schema"), "force": True})
    assert forced["result"]["force"] is True and harness.plugin.get_installed_schema(NAMESPACE)["tables"] == {}


def ordering_and_direct_calls(harness: Harness) -> None:
    harness.plugin.install_plugin_schema(NAMESPACE, to_json(declaration()))
    lifecycle = harness.plugin._get_lifecycle()
    entered, release = threading.Event(), threading.Event()
    original = lifecycle._hydrate_and_standardize

    def parked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    with patch.object(lifecycle, "_hydrate_and_standardize", side_effect=parked), patch.object(lifecycle, "uninstall_plugin_schema", wraps=lifecycle.uninstall_plugin_schema) as uninstall:
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                first = asyncio.run(harness.dispatch(harness.action("install_plugin_schema", values("install_plugin_schema"))))
                assert entered.wait(2)
                direct = executor.submit(harness.plugin.get_installed_schema, NAMESPACE)
                second = asyncio.run(harness.dispatch(harness.action("uninstall_plugin_schema", values("uninstall_plugin_schema"))))
                time.sleep(0.03)
                assert not direct.done() and uninstall.call_count == 0, "one whole-body guard and registered FIFO preserve ownership order"
            finally:
                release.set()
                harness.join()
            assert direct.result(timeout=5)["tables"]["thing"]["status"] in {"active", "inactive"}
    assert harness.terminal(first)["result"]["status"] == "no_op"
    assert harness.terminal(second)["result"]["status"] == "uninstalled"
    print("PASS registered FIFO and full synchronous direct-call serialization")


def failure_cases(harness: Harness) -> None:
    lifecycle = harness.plugin._get_lifecycle()
    with patch.object(lifecycle, "_hydrate_and_standardize", side_effect=FrameworkError("owned typed schema failure", error_code="owned.schema_failure")) as leaf:
        failed = harness.final("install_plugin_schema", values("install_plugin_schema"), status="error")
        assert failed["error"]["code"] == failed["result"]["error"]["code"] == "owned.schema_failure" and leaf.call_count == 1
    with patch.object(lifecycle, "_hydrate_and_standardize", side_effect=SystemExit("owned worker loss")) as leaf:
        lost = harness.final("install_plugin_schema", values("install_plugin_schema"), status="error")
        assert lost["error"]["code"] == "registered_owner.worker_lost" and leaf.call_count == 1
    resumed = harness.final("install_plugin_schema", values("install_plugin_schema"))
    assert resumed["result"]["status"] == "reactivated", "successor executes once without replaying lost body"
    print("PASS typed error and uncertain worker loss with no effect replay")


def terminal_uncertainty(harness: Harness) -> None:
    update = harness.manager.update_job

    def fail_terminal(identity: str, changes: dict[str, object]) -> dict[str, object]:
        if changes.get("status") == "completed":
            return {"action_status": "error", "error": {"code": "owned_terminal_write"}}
        return update(identity, changes)

    lifecycle = harness.plugin._get_lifecycle()
    with patch.object(harness.manager, "update_job", side_effect=fail_terminal), patch.object(lifecycle, "_touch_updated_at", wraps=lifecycle._touch_updated_at) as leaf, patch("ananta.core.state.registered_job_operations.logger.exception") as logged:
        action = harness.action("install_plugin_schema", values("install_plugin_schema"))
        receipt = asyncio.run(harness.dispatch(action))
        harness.join()
        identity = receipt["data"]["job_id"]
        job = data(harness.jobs.get_job(identity))["result"]["job"]
        assert job["status"] == "processing" and job["result"] is None and leaf.call_count == 1
        assert logged.called, "terminal persistence uncertainty remains visible"
    assert update(identity, {"status": "error", "error": {"code": "owned_reconciliation"}})["action_status"] == "completed"
    with patch.object(harness.manager, "_registered_completion", side_effect=RuntimeError("owned notification loss")), patch.object(lifecycle, "_touch_updated_at", wraps=lifecycle._touch_updated_at) as leaf:
        receipt = asyncio.run(harness.dispatch(harness.action("install_plugin_schema", values("install_plugin_schema"))))
        job = harness.terminal(receipt)
        assert job["result"]["status"] == "no_op" and leaf.call_count == 1, "notification loss preserves committed operation outcome"
    print("PASS terminal-write/notification uncertainty is visible and never replays effects")


def declarations(harness: Harness) -> None:
    for verb in SELECTED:
        entry = harness.processes[PREFIX + verb]
        assert entry["is_async"] is True and entry["action_blueprint"]["metadata"]["is_async"] is True
        assert set(entry["output"]["properties"]) == {"job_id", "status"}
    assert harness.processes[PREFIX + "get_installed_schema"]["is_async"] is False
    print("PASS four real registered receipt schemas; advisory read unchanged")


def main() -> int:
    try:
        with ledger_fixture("fast_return_census", schema="schema_registered_admission") as pg:
            harness = Harness(pg)
            try:
                for verb in SELECTED:
                    parked_case(harness, verb)
                    if verb == "install_plugin_schema":
                        harness.schemas.initialize_schemas([declaration()])
                        data(harness.state.write_state(NAMESPACE, {"table": "thing", "record": {"kind": "b", "label": "purged fixture row"}}))
                lifecycle_cases(harness)
                ordering_and_direct_calls(harness)
                failure_cases(harness)
                terminal_uncertainty(harness)
                declarations(harness)
            finally:
                harness.join()
    except MissingFixtureError:
        return skip_fixture()
    print("g1_schema_registered_admission_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
