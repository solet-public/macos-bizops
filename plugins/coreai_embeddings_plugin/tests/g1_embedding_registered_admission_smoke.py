"""Real registered CoreAI admission, with owned runtime doubles and private PG.

The registered binding is the CoreAI plugin, not the internal EmbeddingService
facade. Its real runtime encodes every input before any native embedding.
Poller transport validation remains outside this family admission measurement.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/coreai_embeddings_plugin/src", ROOT / "plugins/coreai_embeddings_plugin/tests", ROOT / "plugins/postgres_state_management_plugin/src"):
    sys.path.insert(0, str(source))

from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.orchestration.service_bindings import ServiceBindings  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.execution_token_context import get_current_flow_token_id  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.embedding_service import EmbeddingService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from coreai_embeddings_plugin import runtime as runtime_module  # noqa: E402
from coreai_embeddings_plugin.contracts import DIMENSION, MODEL_ID, EmbeddingError, ErrorCode  # noqa: E402
from coreai_embeddings_plugin.plugin import CoreAIEmbeddingsPlugin  # noqa: E402
from timeout_recovery_smoke import FakeNative, FakeTokenizer, FixtureRuntime, _scaled_result, _wait_for_abandoned_workers  # noqa: E402

from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture, skip_fixture  # noqa: E402

PREFIX = "service_interface::embedding_service::"
OWNER = "embedding_service"


class LiteralFactoryTemplates:
    """Only the action factory's literal input port is a double."""

    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del context
        return action_def


class Fixture:
    """Real binding, registry, factory, poller, runtime, job and token ledgers."""

    def __init__(self, state: StateService, home: Path) -> None:
        self.state, self.home = state, home
        self.plugin = CoreAIEmbeddingsPlugin({"asset_root": str(home / "owned-assets")})
        self.plugin.prepare_for_readiness()
        assert self.plugin.is_ready(), self.plugin.readiness_error
        plugins = PluginManager()
        plugins.plugins[self.plugin.name] = self.plugin
        config = home / "config"
        config.mkdir(exist_ok=True)
        (config / ServiceBindings.CONFIG_FILENAME).write_text(json.dumps({OWNER: self.plugin.name}), encoding="utf-8")
        bindings = ServiceBindings(home)
        bindings.load()
        self.graph = FlowRuntimeGraph(state)
        self.manager = AsyncJobManager(state, self.graph)
        self.reader = JobService(state, self.manager)
        self.facade = EmbeddingService(plugins, self.plugin.name)
        self.contexts = ExecutionContextManager()
        self.registry: dict[str, Any] = {"processes": {}}
        scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
        for provider in (OWNER, "inference_service"):
            module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
            scanner._scan_module_classes(module, module.__name__, self.registry["processes"])
        self.factory = ActionFactory(process_registry=self.registry, state_service=cast(Any, state), template_engine=LiteralFactoryTemplates(), app_home=str(home))
        orchestrator = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME = str(home)
        orchestrator.plugin_manager = plugins
        orchestrator.service_bindings = bindings
        orchestrator.async_job_manager = self.manager
        orchestrator.action_recorder = None
        discovery = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.registry["processes"]
        self.processor = ActionProcessor(plugins, state_service=state, orchestrator=cast(Any, orchestrator), discovery_service=discovery, execution_context_manager=self.contexts, process_registry=self.registry, app_home=str(home))
        assert self.processor._resolve_service(OWNER) is self.plugin, "fixture must use the shipped plugin binding, not the facade"
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

    def action(self, arguments: dict[str, Any], verb: str = "generate_embeddings", *, token: bool = True) -> QueuedAction:
        session = "owned-embedding-session"
        flow = FlowManager(self.state).create_flow(session, "bridge_process_call", {})
        self.contexts.create_context(flow)
        runtime = self.factory.create_action({"process_key": PREFIX + verb, "arguments": arguments, "notes": "owned embedding admission", "result_processor_kind": None, "error_processor_kind": None, "error_processor": None})
        action_id = str(runtime["name"])
        flow_token = self.graph.create_token(flow, TokenOwnerType.PROCESS, action_id) if token else None
        return self.poller._build_queued_action(
            {"id": action_id, "process_key": str(runtime["process_key"]), "parameters": json.dumps(runtime["parameters"], indent=1), "notes": str(runtime["notes"]), "sequence": "now", "core__sessions_id": session, "core__flows_id": flow, "flow_token_id": flow_token}, template_namespace=None
        )

    async def dispatch(self, action: QueuedAction) -> dict[str, Any]:
        assert self.poller._get_canonical_arguments_schema(action.process_key), "real registered schema must reach preflight"

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
        owner = self.manager._owners.get(OWNER)
        worker = owner._thread if owner is not None else None
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "owned job worker must retire before PG teardown"

    def terminal(self, receipt: dict[str, Any]) -> dict[str, Any]:
        self.join()
        job_id = receipt["data"]["job_id"]
        answer = self.reader.get_job(job_id)
        assert answer["action_status"] == "completed", answer
        job = cast(dict[str, Any], answer["data"]["result"]["job"])
        assert job["id"] == job_id and job["status"] in {"completed", "error"}, job
        return job


def lineage(fixture: Fixture, action: QueuedAction, receipt: dict[str, Any]) -> None:
    assert {"action_status", "data", "actions", "error", "timestamp"} <= receipt.keys()
    job_id = receipt["data"]["job_id"]
    rows = cast(Any, fixture.state.read_state("core", {"table": "job", "filters": {"group_id": f"registered:{action.id}"}}))["data"]["records"]
    assert [row["id"] for row in rows] == [job_id], "one job per outer invocation"
    job = rows[0]
    assert job["flow_token_id"] == action.flow_token_id
    metadata = json.loads(job["metadata"])
    assert (metadata["registered_action_id"], metadata["flow_id"], metadata["session_id"]) == (action.id, action.flow_id, action.session_id)
    payload = cast(Any, fixture.manager.get_job_payload(job_id))["data"]["payload"]
    assert len(json.dumps(payload)) < 512 and "inputs" not in payload["parameters"], "only a bounded non-replayable descriptor is persisted"


def parked_case(fixture: Fixture, leaf_name: str) -> None:
    runtime = fixture.plugin._runtime
    assert isinstance(runtime, FixtureRuntime)
    action = fixture.action({"inputs": ["alpha", "beta"]})
    entered, release = threading.Event(), threading.Event()
    parse_threads: list[int] = []
    parse_tokens: list[str | None] = []
    leaf_threads: list[int] = []
    original = getattr(runtime, leaf_name)
    parse = fixture.processor._parse_action_parameters

    def parked(*args: Any, **kwargs: Any) -> Any:
        leaf_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "owned runtime barrier"
        return original(*args, **kwargs)

    def observed_parse(queued: Any) -> dict[str, object]:
        if queued.id == action.id:
            parse_threads.append(threading.get_ident())
            parse_tokens.append(get_current_flow_token_id())
        return parse(queued)

    timer = threading.Timer(0.4, release.set)
    timer.start()
    with patch.object(runtime, leaf_name, side_effect=parked), patch.object(fixture.processor, "_parse_action_parameters", side_effect=observed_parse):
        try:
            receipt = asyncio.run(fixture.dispatch(action))
            assert entered.wait(2), "real runtime leaf reached after valid binding/setup"
            print(json.dumps({"leaf": leaf_name, "elapsed_ms": fixture.timings[action.id], "leaf_reached": True, "receipt_data_keys": sorted(receipt["data"])}), flush=True)
            assert_parked(fixture, action, receipt, release, parse_threads, parse_tokens, leaf_threads)
        finally:
            release.set()
            timer.cancel()
            timer.join()
            fixture.join()
    job = fixture.terminal(receipt)
    assert job["status"] == "completed" and job["error"] is None
    result = job["result"]
    assert result["data"]["result"]["model"] == MODEL_ID
    assert len(result["data"]["result"]["embeddings"]) == 2
    assert {"action_status", "data", "actions", "error", "timestamp"} <= result.keys()
    assert sum(route[0] == action.id for route in fixture.routes) == 1


def assert_parked(fixture: Fixture, action: QueuedAction, receipt: dict[str, Any], release: threading.Event, parse_threads: list[int], parse_tokens: list[str | None], leaf_threads: list[int]) -> None:
    """Admission, lineage and unchanged controls while the real leaf is parked."""
    assert "job_id" in receipt["data"], "whole_embedding_returns_job_before_parked_runtime_leaf"
    assert fixture.timings[action.id] < 50, "registered embedding admission exceeds census 50 ms line"
    assert not release.is_set(), "receipt must arrive while real runtime leaf remains parked"
    lineage(fixture, action, receipt)
    worker = fixture.manager._owners[OWNER]._thread
    assert worker is not None and parse_threads == [worker.ident]
    assert parse_tokens == [action.flow_token_id]
    assert leaf_threads and all(thread != worker.ident for thread in leaf_threads), "existing CoreAI single native worker is reused"
    for verb in ("get_embedding_dimension", "list_models"):
        control = asyncio.run(fixture.dispatch(fixture.action({}, verb, token=False)))
        assert "job_id" not in control["data"] and not release.is_set(), "bounded metadata dispatch progresses meanwhile"


def serial_order(fixture: Fixture) -> None:
    """A second registered generation cannot overtake the first token preflight."""
    runtime = fixture.plugin._runtime
    assert runtime is not None
    original = runtime._encode
    entered, release = threading.Event(), threading.Event()
    seen: list[list[str]] = []
    first, second = fixture.action({"inputs": ["first order"]}), fixture.action({"inputs": ["second order"]})

    def encode(texts: list[str]) -> Any:
        seen.append(texts)
        entered.set()
        assert release.wait(5), "serial fixture release"
        return original(texts)

    with patch.object(runtime, "_encode", side_effect=encode):
        try:
            one = asyncio.run(fixture.dispatch(first))
            assert entered.wait(2)
            two = asyncio.run(fixture.dispatch(second))
            lineage(fixture, first, one)
            lineage(fixture, second, two)
            assert seen == [["first order"]], "only the first operation may enter the runtime"
        finally:
            release.set()
            fixture.join()
    assert fixture.terminal(one)["status"] == fixture.terminal(two)["status"] == "completed"
    assert seen == [["first order"], ["second order"]]
    assert [route[0] for route in fixture.routes if route[0] in {first.id, second.id}] == [first.id, second.id]
    print("PASS registered FIFO order, one original-action completion each and no extra runtime resources")


def success_and_errors(fixture: Fixture) -> None:
    inputs = [f"batch {index}" for index in range(128)]
    before = FakeNative.embeds
    job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action({"inputs": inputs}))))
    vectors = job["result"]["data"]["result"]["embeddings"]
    assert job["status"] == "completed" and len(vectors) == 128 and all(len(vector) == DIMENSION for vector in vectors)
    assert FakeNative.embeds - before == 128
    for arguments, code in (({"inputs": ["one"], "model": "foreign"}, ErrorCode.UNSUPPORTED_MODEL), ({"inputs": ["fine", "Long last"]}, ErrorCode.INPUT_TOO_LONG), ({"inputs": ["one"] * 129}, ErrorCode.INVALID_INPUT)):
        before = FakeNative.embeds
        failure = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action(arguments))))
        assert failure["status"] == "error" and failure["error"]["code"] == code, failure
        assert failure["result"]["error"]["code"] == code and failure["result"]["data"] == {}
        assert FakeNative.embeds == before, "invalid input never produces a partial batch"
    FakeNative.raises = EmbeddingError(ErrorCode.INVALID_OUTPUT, "owned invalid output")
    try:
        failure = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action({"inputs": ["one"]}))))
        assert failure["error"]["code"] == ErrorCode.INVALID_OUTPUT and not fixture.plugin.is_ready()
    finally:
        FakeNative.raises = None
        fixture.plugin.prepare_for_readiness()
    print("PASS full128 batch and unchanged unsupported/oversize/invalid-output terminal errors")


def timeout_and_internal_control(fixture: Fixture) -> None:
    prepared = FixtureRuntime.prepared
    before = FakeNative.embeds
    FakeNative.hangs_left = 1
    with patch.object(Future, "result", _scaled_result):
        failed = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action({"inputs": ["hang"]}))))
    assert failed["status"] == "error" and failed["error"]["code"] == ErrorCode.TIMEOUT
    assert FixtureRuntime.prepared == prepared + 1 and FakeNative.embeds == before + 2, "one failed generation plus one recovery readiness probe, never a retry"
    assert fixture.plugin.is_ready()
    direct = cast(Any, fixture.facade.generate_embeddings(["direct synchronous"]))
    assert direct["data"]["result"]["embeddings"] and "job_id" not in direct["data"]
    assert fixture.facade.input_token_budget() is not None
    refused = cast(Any, fixture.facade.generate_embeddings(["word " * 2049]))
    assert refused["error"]["code"] == "embedding_input_too_long", "facade budget remains its own synchronous contract"
    print("PASS one timeout recovery/no retry and synchronous facade budget control")


def uncertain_outcomes(fixture: Fixture) -> None:
    runtime = fixture.plugin._runtime
    assert runtime is not None
    with patch.object(runtime, "generate", side_effect=SystemExit("owned worker loss")) as leaf:
        failed = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action({"inputs": ["worker loss"]}))))
        assert failed["status"] == "error" and failed["error"]["code"] == "registered_owner.worker_lost"
        assert leaf.call_count == 1, "lost volatile body never replays"
    update = fixture.manager.update_job

    def refuse_terminal(job_id: str, updates: dict[str, object]) -> dict[str, object]:
        if updates.get("status") == "completed":
            return {"action_status": "error", "error": {"code": "owned_terminal_write_uncertain"}}
        return update(job_id, updates)

    with patch.object(fixture.manager, "update_job", side_effect=refuse_terminal), patch.object(runtime, "generate", wraps=runtime.generate) as leaf, patch("ananta.core.state.registered_job_operations.logger.exception") as logged:
        receipt = asyncio.run(fixture.dispatch(fixture.action({"inputs": ["uncertain"]})))
        fixture.join()
        job = cast(Any, fixture.manager.get_job(receipt["data"]["job_id"]))["data"]["job"]
        assert job["status"] == "processing" and leaf.call_count == 1
        logged.assert_called_once()
    assert update(job["id"], {"status": "error", "error": {"code": "owned_reconciled_without_replay"}})["action_status"] == "completed"
    with patch.object(fixture.manager, "_registered_completion", side_effect=RuntimeError("owned notification failure")), patch.object(runtime, "generate", wraps=runtime.generate) as leaf:
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action({"inputs": ["notification failure"]}))))
        assert job["status"] == "completed" and job["result"]["data"]["result"]["embeddings"] and leaf.call_count == 1
    print("PASS uncertain worker/terminal outcomes, explicit reconciliation and durable result after notification failure")


def declarations(fixture: Fixture) -> None:
    entry = fixture.registry["processes"][PREFIX + "generate_embeddings"]
    assert entry["is_async"] is True
    output = entry["output"]
    output = output.to_dict() if hasattr(output, "to_dict") else output
    assert set(output["properties"]) == {"job_id", "status"}
    for verb in ("get_embedding_dimension", "list_models"):
        assert fixture.registry["processes"][PREFIX + verb]["is_async"] is False


def main() -> int:
    with ExitStack() as stack:
        stack.enter_context(patch.object(runtime_module, "NativeModel", FakeNative))
        stack.enter_context(patch.object(runtime_module, "NomicTokenizer", FakeTokenizer))
        stack.enter_context(patch("coreai_embeddings_plugin.plugin.EmbeddingRuntime", FixtureRuntime))
        stack.enter_context(patch("timeout_recovery_smoke.EMBED_SECONDS", 0.0))
        stack.enter_context(patch("timeout_recovery_smoke.HANG_SECONDS", 1.2))
        FakeNative.hangs_left = 0
        FakeNative.raises = None
        try:
            with ledger_fixture("fast_return_census", schema="embedding_registered_admission") as pg:
                schemas = SchemaManager(pg.state, pg.plugin)
                schemas.initialize_schemas(CoreSchemaDefinitions.get_all_core_schemas())
                pg.state.set_schema_manager(schemas)
                fixture = Fixture(pg.state, pg.app_home)
                try:
                    parked_case(fixture, "_encode")
                    parked_case(fixture, "_embed")
                    serial_order(fixture)
                    success_and_errors(fixture)
                    timeout_and_internal_control(fixture)
                    uncertain_outcomes(fixture)
                    declarations(fixture)
                finally:
                    fixture.join()
                    asyncio.run(fixture.plugin.cleanup())
                    _wait_for_abandoned_workers()
        except MissingFixtureError:
            return skip_fixture()
    print("g1_embedding_registered_admission_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
