"""Vector admission through production registry, factory, poller and private PG.

The fixture owns its database, vectors, jobs and tokens. No application profile
or credential is consulted. Poller transport preflight remains a known residual;
ActionProcessor preparation and the whole vector body belong to the owner.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/pgvector_service_plugin/src", ROOT / "plugins/postgres_state_management_plugin/src"):
    sys.path.insert(0, str(source))

from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.execution_token_context import get_current_flow_token_id  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from ananta.services.vector_service import VectorService  # noqa: E402
from pgvector_service_plugin.plugin import PGVectorServicePlugin  # noqa: E402
from pgvector_service_plugin.postgres_backend.vector.config import PGVectorConfig  # noqa: E402
from pgvector_service_plugin.postgres_backend.vector.provider import PGVectorProvider, make_local_pool  # noqa: E402

from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture, load_fixture, skip_fixture  # noqa: E402

SELECTED = frozenset({"delete_vectors", "get_namespace_stats", "search_similar", "store_vectors"})
PREFIX = "service_interface::vector_service::"
NAMESPACE = "pgvector_service_plugin"
SCHEMA = "vector_registered_admission"


class LiteralFactoryTemplates:
    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del context
        return action_def


class Fixture:
    """Real components, with observed poller storage and completion output ports."""

    def __init__(self, state: StateService, home: Path, plugin: PGVectorServicePlugin) -> None:
        self.state, self.plugin = state, plugin
        self.provider = plugin._get_provider()
        plugins = PluginManager()
        plugins.plugins[plugin.name] = plugin
        self.service = VectorService(plugins, plugin.name, state)
        self.graph = FlowRuntimeGraph(state)
        self.manager = AsyncJobManager(state, self.graph)
        self.reader = JobService(state, self.manager)
        self.contexts = ExecutionContextManager()
        self.registry: dict[str, Any] = {"processes": {}}
        scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
        for provider in ("vector_service", "inference_service"):
            module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
            scanner._scan_module_classes(module, module.__name__, self.registry["processes"])
        self.factory = ActionFactory(process_registry=self.registry, state_service=cast(Any, state), template_engine=LiteralFactoryTemplates(), app_home=str(home))
        discovery = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.registry["processes"]
        orchestrator = SimpleNamespace(async_job_manager=self.manager, APP_HOME=str(home), get_service=lambda name: self.service if name == "vector_service" else None)
        self.processor = ActionProcessor(plugins, state_service=state, discovery_service=discovery, orchestrator=cast(Any, orchestrator), execution_context_manager=self.contexts, process_registry=self.registry, app_home=str(home))
        self.poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        self.poller.action_processor = self.processor
        self.poller._discovery_service = discovery
        self.poller._flow_runtime_graph = self.graph
        self.poller.execution_context_manager = self.contexts
        self.receipts: dict[str, dict[str, Any]] = {}
        self.routes: list[tuple[str, str, object, object]] = []
        self.timings: dict[str, float] = {}
        self.manager.set_registered_completion_handler(self.route)

    def route(self, action_id: str, status: str, result: dict[str, object] | None, error: dict[str, object] | None) -> None:
        self.routes.append((action_id, status, result, error))

    def action(self, verb: str, arguments: dict[str, Any], *, token: bool = True) -> QueuedAction:
        session = "owned-vector-session"
        flow = FlowManager(self.state).create_flow(session, "bridge_process_call", {})
        self.contexts.create_context(flow)
        runtime = self.factory.create_action({"process_key": PREFIX + verb, "arguments": arguments, "notes": "owned vector admission", "result_processor_kind": None, "error_processor_kind": None, "error_processor": None})
        action_id = str(runtime["name"])
        flow_token = self.graph.create_token(flow, TokenOwnerType.PROCESS, action_id) if token else None
        return self.poller._build_queued_action(
            {"id": action_id, "process_key": str(runtime["process_key"]), "parameters": json.dumps(runtime["parameters"], indent=1), "notes": str(runtime["notes"]), "sequence": "now", "core__sessions_id": session, "core__flows_id": flow, "flow_token_id": flow_token}, template_namespace=None
        )

    async def dispatch(self, action: QueuedAction) -> dict[str, Any]:
        assert self.poller._get_canonical_arguments_schema(action.process_key), "real registry schema reaches poller preflight"

        def completed(action_id: str, result: dict[str, Any], _token: str | None, *, on_status_written: Callable[[], None]) -> None:
            self.receipts[action_id] = result
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
        owner = self.manager._owners.get("vector_service")
        worker = owner._thread if owner is not None else None
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "owner must finish before private PG teardown"

    def terminal(self, receipt: dict[str, Any]) -> dict[str, Any]:
        self.join()
        job_id = receipt["data"]["job_id"]
        answer = self.reader.get_job(job_id)
        assert answer["action_status"] == "completed", answer
        job = cast(dict[str, Any], answer["data"]["result"]["job"])
        assert job["id"] == job_id and job["status"] in {"completed", "error"}, job
        return job


def vectors(label: str, count: int = 2) -> list[dict[str, Any]]:
    return [{"external_id": f"{label}-{index}", "vector": [1.0, 0.1 * index, 0.0], "dimension": 3, "metadata": {"source": label}} for index in range(count)]


def seed(fixture: Fixture, label: str) -> list[str]:
    result = cast(dict[str, Any], fixture.service.store_vectors(NAMESPACE, vectors(label)))
    assert result["action_status"] == "completed", result
    return cast(list[str], result["data"]["result"]["inserted_ids"])


def values(verb: str, ids: list[str]) -> dict[str, Any]:
    return {
        "store_vectors": {"namespace": NAMESPACE, "vectors": vectors("parked-store")},
        "search_similar": {"namespace": NAMESPACE, "query_vector": [1.0, 0.0, 0.0], "top_k": 2},
        "get_namespace_stats": {"namespace": NAMESPACE},
        "delete_vectors": {"namespace": NAMESPACE, "vector_ids": ids},
    }[verb]


def lineage(fixture: Fixture, action: QueuedAction, receipt: dict[str, Any]) -> None:
    assert {"action_status", "data", "actions", "error", "timestamp"} <= receipt.keys()
    job_id = receipt["data"]["job_id"]
    job = cast(Any, fixture.manager.get_job(job_id))["data"]["job"]
    owned = cast(Any, fixture.state.read_state("core", {"table": "job", "filters": {"group_id": f"registered:{action.id}"}}))["data"]["records"]
    assert [row["id"] for row in owned] == [job_id], "one persisted job per outer invocation"
    assert job["flow_token_id"] == action.flow_token_id and job["group_id"] == f"registered:{action.id}"
    metadata = json.loads(job["metadata"])
    assert metadata["registered_action_id"] == action.id and metadata["session_id"] == action.session_id and metadata["flow_id"] == action.flow_id
    request = cast(Any, fixture.manager.get_job_payload(job_id))["data"]["payload"]
    assert len(json.dumps(request)) < 512, "persist a bounded descriptor, not the vector batch"


def completion(fixture: Fixture, action: QueuedAction, job: dict[str, Any]) -> None:
    routes = [route for route in fixture.routes if route[0] == action.id]
    assert len(routes) == 1 and routes[0][2] == job["result"]
    assert fixture.manager.update_job(job["id"], {"status": "completed", "result": {"wrong": True}})["action_status"] == "error", "stale terminal overwrite refused"
    assert fixture.reader.get_job(job["id"])["data"]["result"]["job"]["result"] == job["result"]


def parked_case(fixture: Fixture, verb: str) -> None:
    ids = seed(fixture, "seed-" + verb)
    action = fixture.action(verb, values(verb, ids))
    cheap = fixture.action("list_namespaces", {}, token=False)
    method = {"store_vectors": "_validate_batch_dimensions", "search_similar": "search_similar", "get_namespace_stats": "_get_connection", "delete_vectors": "delete_vectors"}[verb]
    original = getattr(fixture.provider, method)
    entered, release = threading.Event(), threading.Event()
    leaf_threads: list[int] = []
    parse_threads: list[int] = []
    original_parse = fixture.processor._parse_action_parameters

    def leaf(*args: Any, **kwargs: Any) -> Any:
        leaf_threads.append(threading.get_ident())
        assert get_current_flow_token_id() == action.flow_token_id
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    def parse(queued: Any) -> dict[str, object]:
        if queued.id == action.id:
            parse_threads.append(threading.get_ident())
        return original_parse(queued)

    timer = threading.Timer(0.3, release.set)
    timer.start()
    with patch.object(fixture.provider, method, side_effect=leaf), patch.object(fixture.processor, "_parse_action_parameters", side_effect=parse):
        try:
            receipt = asyncio.run(fixture.dispatch(action))
            assert entered.wait(2), "actual vector leaf reached with valid setup"
            print(json.dumps({"verb": verb, "leaf_reached": True, "elapsed_ms": fixture.timings[action.id], "receipt_keys": sorted(receipt["data"])}), flush=True)
            assert "job_id" in receipt["data"], "whole_vector_operation_returns_job_before_parked_leaf"
            assert fixture.timings[action.id] < 50, "registered vector receipt under census 50 ms line"
            lineage(fixture, action, receipt)
            worker = fixture.manager._owners["vector_service"]._thread
            assert worker is not None and parse_threads == leaf_threads == [worker.ident], "preparation and actual effect share the owner worker"
            control = asyncio.run(fixture.dispatch(cheap))
            assert "job_id" not in control["data"] and not release.is_set(), "ordinary action dispatches while vector leaf remains parked"
        finally:
            release.set()
            timer.cancel()
            timer.join()
            fixture.join()
    job = fixture.terminal(receipt)
    assert job["status"] == "completed" and job["error"] is None, job
    assert {"action_status", "data", "actions", "error", "timestamp"} <= job["result"].keys()
    vector_outcome(fixture, verb, job["result"]["data"]["result"])
    completion(fixture, action, job)


def vector_outcome(fixture: Fixture, verb: str, data: dict[str, Any]) -> None:
    """Pin each family's full successful result rather than only job completion."""
    if verb == "store_vectors":
        stored_outcome(fixture, data)
    elif verb == "search_similar":
        search_outcome(data)
    elif verb == "get_namespace_stats":
        assert data["vector_count"] > 0 and data["dimensions"] == [3]
    else:
        assert data == {"deleted_count": 2}


def stored_outcome(fixture: Fixture, data: dict[str, Any]) -> None:
    """Read every returned batch id back through the real synchronous service."""
    assert data["count"] == len(data["inserted_ids"]) == 2
    for vector_id in data["inserted_ids"]:
        row = cast(Any, fixture.service.get_vector(NAMESPACE, vector_id))["data"]["result"]
        assert row["dimension"] == 3 and row["metadata"] == {"source": "parked-store"}


def search_outcome(data: dict[str, Any]) -> None:
    """Preserve result count, searched namespace and cosine-distance ordering."""
    results = data["results"]
    assert data["count"] == len(results) == 2 and data["namespaces_searched"] == [NAMESPACE]
    distances = [row["distance"] for row in results]
    assert distances == sorted(distances) and all(distance >= 0 for distance in distances)


def lifecycle_and_controls(fixture: Fixture) -> None:
    ids = seed(fixture, "lifecycle")
    revisions: tuple[dict[str, object], ...] = ({"corrected": True}, {"source": "lifecycle"}, {})
    for metadata in revisions:
        expected = fixture.service.update_metadata(NAMESPACE, ids[0], metadata)
        action = fixture.action("get_vector", {"namespace": NAMESPACE, "vector_id": ids[0]}, token=False)
        result = asyncio.run(fixture.dispatch(action))
        assert expected.get("action_status") == "completed" and result["data"]["result"]["metadata"] == metadata
        assert "job_id" not in result["data"], "point reads remain synchronous"
    control = fixture.action("update_metadata", {"namespace": NAMESPACE, "vector_id": ids[0], "metadata": {"cleared": None}}, token=False)
    assert asyncio.run(fixture.dispatch(control))["data"]["result"] == {"updated": True}
    deleted = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("delete_vectors", {"namespace": NAMESPACE, "vector_ids": ids}))))
    assert deleted["result"]["data"]["result"] == {"deleted_count": 2}
    assert fixture.service.get_vector(NAMESPACE, ids[0]).get("action_status") == "error"
    print("PASS vector create/read, metadata correct/reverse/clear, explicit-id terminal delete and synchronous controls")


def ordering_and_composition(fixture: Fixture) -> None:
    entered, release = threading.Event(), threading.Event()
    original = fixture.provider._execute_vector_insert
    before = cast(Any, fixture.service.get_namespace_stats(NAMESPACE))["data"]["result"]["vector_count"]

    def parked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    with patch.object(fixture.provider, "_execute_vector_insert", side_effect=parked), patch.object(fixture.provider, "get_namespace_stats", wraps=fixture.provider.get_namespace_stats) as stats:
        try:
            stored = asyncio.run(fixture.dispatch(fixture.action("store_vectors", {"namespace": NAMESPACE, "vectors": vectors("ordered")})))
            assert entered.wait(2)
            read = asyncio.run(fixture.dispatch(fixture.action("get_namespace_stats", {"namespace": NAMESPACE})))
            assert stats.call_count == 0, "namespace aggregate cannot overtake an earlier writer"
        finally:
            release.set()
            fixture.join()
    assert fixture.terminal(stored)["result"]["data"]["result"]["count"] == 2
    assert fixture.terminal(read)["result"]["data"]["result"]["vector_count"] == before + 2
    original_dimensions = fixture.provider._validate_batch_dimensions

    def reentrant(batch: list[dict[str, Any]]) -> int:
        assert cast(Any, fixture.service.get_namespace_stats(NAMESPACE))["data"]["result"]["vector_count"] == before + 2
        return original_dimensions(batch)

    with patch.object(fixture.provider, "_validate_batch_dimensions", side_effect=reentrant):
        result = fixture.service.store_vectors(NAMESPACE, vectors("reentrant"))
    assert cast(Any, result)["data"]["result"]["count"] == 2 and "job_id" not in cast(Any, result)["data"], "internal reentrant composition returns full data without another job"
    print("PASS one FIFO, complete batches, synchronous reentrant provider composition")


def typed_failures(fixture: Fixture) -> None:
    invalid = vectors("invalid")
    invalid[0]["dimension"] = 2
    cases = (("store_vectors", {"namespace": NAMESPACE, "vectors": invalid}, "Inconsistent dimensions"), ("search_similar", {"namespace": NAMESPACE, "query_vector": [1.0], "top_k": 2}, "dimension"))
    for verb, arguments, message in cases:
        typed_failure(fixture, verb, arguments, message)
    print("PASS dimension/search errors preserve exact operational type/code/message")


def typed_failure(fixture: Fixture, verb: str, arguments: dict[str, Any], message: str) -> None:
    """Compare the stored envelope to the exact operation that produced it."""
    direct = getattr(fixture.service, verb)(**arguments)
    envelopes: list[dict[str, Any]] = []
    original = fixture.plugin._create_error_result

    def capture(error: str) -> dict[str, Any]:
        envelope = cast(dict[str, Any], original(error))
        envelopes.append(envelope)
        return envelope

    with patch.object(fixture.plugin, "_create_error_result", side_effect=capture):
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action(verb, arguments))))
    assert job["status"] == "error" and job["error"]["code"] == direct["error"]["code"] == "pgvector.operation_failed", job
    assert message.lower() in job["error"]["message"].lower() and message.lower() in direct["error"]["message"].lower(), job
    assert len(envelopes) == 1 and job["result"] == envelopes[0] and job["result"]["error"] == job["error"], "retain the exact original typed envelope, including its own timestamp"


def worker_loss(fixture: Fixture) -> None:
    original = fixture.provider._execute_vector_insert

    def partial(namespace: str, records: list[dict[str, Any]]) -> dict[str, Any]:
        original(namespace, records[:1])
        raise SystemExit("owned loss after one vector")

    with patch.object(fixture.provider, "_execute_vector_insert", side_effect=partial) as leaf:
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_vectors", {"namespace": NAMESPACE, "vectors": vectors("lost")}))))
        assert job["status"] == "error" and job["error"]["code"] == "registered_owner.worker_lost", job
        assert leaf.call_count == 1
    missing = fixture.plugin.find_missing_external_ids(NAMESPACE, ["lost-0", "lost-1"])
    assert cast(Any, missing)["data"]["result"]["missing"] == ["lost-1"], "partial batch persists without replaying the lost body"
    successor = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_vectors", {"namespace": NAMESPACE, "vectors": vectors("successor")}))))
    assert successor["status"] == "completed", successor
    print("PASS partial batch loss records uncertain error; successor never replays effects")


def terminal_uncertainty(fixture: Fixture) -> None:
    update = fixture.manager.update_job

    def refuse(job_id: str, updates: dict[str, object]) -> dict[str, object]:
        if updates.get("status") == "completed":
            return {"action_status": "error", "error": {"code": "owned_terminal_uncertainty"}}
        return update(job_id, updates)

    with patch.object(fixture.manager, "update_job", side_effect=refuse), patch.object(fixture.provider, "_execute_vector_insert", wraps=fixture.provider._execute_vector_insert) as leaf, patch("ananta.core.state.registered_job_operations.logger.exception") as logged:
        receipt = asyncio.run(fixture.dispatch(fixture.action("store_vectors", {"namespace": NAMESPACE, "vectors": vectors("uncertain")})))
        fixture.join()
        job = cast(Any, fixture.manager.get_job(receipt["data"]["job_id"]))["data"]["job"]
        assert job["status"] == "processing" and leaf.call_count == 1
        logged.assert_called_once()
    assert update(job["id"], {"status": "error", "error": {"code": "owned_reconciliation"}})["action_status"] == "completed"
    with patch.object(fixture.manager, "_registered_completion", side_effect=RuntimeError("owned notification failure")), patch.object(fixture.provider, "_execute_vector_insert", wraps=fixture.provider._execute_vector_insert) as leaf:
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_vectors", {"namespace": NAMESPACE, "vectors": vectors("notify")}))))
        assert job["status"] == "completed" and job["result"]["data"]["result"]["count"] == 2 and leaf.call_count == 1
    print("PASS terminal-write and notification failures remain visible without replay")


def declarations(fixture: Fixture) -> None:
    for verb in SELECTED:
        entry = fixture.registry["processes"][PREFIX + verb]
        schema = entry["output"]
        schema = schema.to_dict() if hasattr(schema, "to_dict") else schema
        assert entry["is_async"] is True and set(schema["properties"]) == {"job_id", "status"}
    for verb in ("get_vector", "list_namespaces", "update_metadata"):
        assert fixture.registry["processes"][PREFIX + verb]["is_async"] is False


def main() -> int:
    try:
        config = load_fixture("fast_return_census")
        with ledger_fixture("fast_return_census", schema=SCHEMA) as pg:
            plugin = PGVectorServicePlugin()
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(get_service=lambda _name: pg.state))
            plugin.prepare_for_readiness()
            schemas = SchemaManager(pg.state, pg.plugin)
            schemas.initialize_schemas([*CoreSchemaDefinitions.get_all_core_schemas(), *plugin.get_schema_definitions()])
            pg.state.set_schema_manager(schemas)
            provider = PGVectorProvider(PGVectorConfig(host=config.host, port=config.port, database=config.database, user=config.user, password="", db_schema=SCHEMA, pool_size=2), plugin.name, pool_builder=make_local_pool, state_service=pg.state)
            provider.initialize()
            plugin._provider = provider
            plugin.set_ready()
            fixture = Fixture(pg.state, pg.app_home, plugin)
            try:
                for verb in ("store_vectors", "search_similar", "get_namespace_stats", "delete_vectors"):
                    parked_case(fixture, verb)
                ordering_and_composition(fixture)
                lifecycle_and_controls(fixture)
                typed_failures(fixture)
                worker_loss(fixture)
                terminal_uncertainty(fixture)
                declarations(fixture)
            finally:
                fixture.join()
                provider.close()
    except MissingFixtureError:
        return skip_fixture()
    print("g1_vector_registered_admission_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
