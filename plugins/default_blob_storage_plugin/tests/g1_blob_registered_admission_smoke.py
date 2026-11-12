"""Blob owner admission through the real registry, factory and poller preflight.

Private PostgreSQL supplies metadata, jobs, requests, outcomes and flow tokens.
An absent explicit private fixture is an advisory skip, never a profile lookup.
The poller parses, validates and reserializes input before owner admission;
only ActionProcessor preparation and the complete Blob service operation move.
"""

from __future__ import annotations

import asyncio
import base64
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
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/default_blob_storage_plugin/src", ROOT / "plugins/postgres_state_management_plugin/src"):
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
from ananta.services.blob_storage_service import BlobStorageService  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from ananta.utils.naming import build_external_id, parse_filename  # noqa: E402

from default_blob_storage_plugin import ingestion  # noqa: E402
from default_blob_storage_plugin.plugin import DefaultBlobStoragePlugin  # noqa: E402
from default_blob_storage_plugin.providers.filesystem_provider import FilesystemProvider  # noqa: E402
from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture, skip_fixture  # noqa: E402

SELECTED = frozenset({"store_blob", "store_blob_from_file", "retrieve_blob_by_name"})
PREFIX = "service_interface::blob_storage_service::"
CONTENT = b"owned binary blob\x00\xff\n" * 4096
MID_CONTENT = CONTENT * 16


class LiteralFactoryTemplates:
    """Factory template port for literal fixtures; dispatch preparation is real."""

    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del context
        return action_def


class Fixture:
    """Production components; poller storage/output ports are observed separately."""

    def __init__(self, state: StateService, home: Path) -> None:
        self.state, self.home = state, home
        self.plugin = DefaultBlobStoragePlugin()
        self.provider = FilesystemProvider(str(home), {})
        self.provider.set_state_service(state)
        self.plugin.provider = self.provider
        self.plugin.set_ready()
        plugins = PluginManager()
        plugins.plugins[self.plugin.name] = self.plugin
        self.service = BlobStorageService(plugins, self.plugin.name, str(home))
        self.graph = FlowRuntimeGraph(state)
        self.manager = AsyncJobManager(state, self.graph)
        self.reader = JobService(state, self.manager)
        self.contexts = ExecutionContextManager()
        self.registry: dict[str, Any] = {"processes": {}}
        scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
        for provider in ("blob_storage_service", "inference_service"):
            module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
            scanner._scan_module_classes(module, module.__name__, self.registry["processes"])
        self.factory = ActionFactory(process_registry=self.registry, state_service=cast(Any, state), template_engine=LiteralFactoryTemplates(), app_home=str(home))
        discovery = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.registry["processes"]
        orchestrator = SimpleNamespace(async_job_manager=self.manager, APP_HOME=str(home), get_service=lambda _name: None)
        self.processor = ActionProcessor(plugins, state_service=state, blob_storage_service=self.service, discovery_service=discovery, orchestrator=cast(Any, orchestrator), execution_context_manager=self.contexts, process_registry=self.registry, app_home=str(home))
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
        session = "owned-blob-session"
        flow = FlowManager(self.state).create_flow(session, "bridge_process_call", {})
        self.contexts.create_context(flow)
        runtime = self.factory.create_action({"process_key": PREFIX + verb, "arguments": arguments, "notes": "owned blob admission", "result_processor_kind": None, "error_processor_kind": None, "error_processor": None})
        action_id = str(runtime["name"])
        flow_token = self.graph.create_token(flow, TokenOwnerType.PROCESS, action_id) if token else None
        return self.poller._build_queued_action(
            {"id": action_id, "process_key": str(runtime["process_key"]), "parameters": json.dumps(runtime["parameters"], indent=1), "notes": str(runtime["notes"]), "sequence": "now", "core__sessions_id": session, "core__flows_id": flow, "flow_token_id": flow_token}, template_namespace=None
        )

    async def dispatch(self, action: QueuedAction) -> dict[str, Any]:
        poller = self.poller
        assert poller._get_canonical_arguments_schema(action.process_key), "real registered schema must reach poller preflight"
        original_parameters = action.parameters

        def completed(action_id: str, result: dict[str, Any], _token: str | None, *, on_status_written: Callable[[], None]) -> None:
            self.receipts[action_id] = result
            on_status_written()

        def failed(*args: object, **kwargs: object) -> None:
            raise AssertionError(f"registered dispatch failed: {args!r} {kwargs!r}")

        with ExitStack() as stack:
            stack.enter_context(patch.object(poller, "_is_terminated_flow_sibling", return_value=False))
            stack.enter_context(patch.object(poller, "_resolve_io_context", return_value=None))
            stack.enter_context(patch.object(poller, "_mark_action_completed", side_effect=completed))
            stack.enter_context(patch.object(poller, "_mark_action_failed", side_effect=failed))
            started = time.perf_counter()
            await poller._process_action(action)
            self.timings[action.id] = (time.perf_counter() - started) * 1000
        assert action.parameters != original_parameters, "real preflight canonicalizes the indented transport body before admission"
        return self.receipts[action.id]

    def terminal(self, receipt: dict[str, Any]) -> dict[str, Any]:
        job_id = receipt["data"]["job_id"]
        owner = self.manager._owners["blob_storage_service"]
        worker = owner._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "owned blob worker must finish"
        answer = self.reader.get_job(job_id)
        assert answer["action_status"] == "completed", answer
        job = cast(dict[str, Any], answer["data"]["result"]["job"])
        assert job["id"] == job_id and job["status"] in {"completed", "error"}, job
        return job


def arguments(home: Path, name: str = "owned.txt", *, content: bytes = CONTENT) -> dict[str, dict[str, Any]]:
    source = home / name
    source.write_bytes(content)
    # Existing by-name lookup uses the normalized stem, whereas automatic
    # ingestion IDs include the extension. Explicit metadata is a supported
    # caller shape and makes this fixture's name resolvable on unchanged base.
    external_id = build_external_id(parse_filename(name)[0])
    return {
        "store_blob": {"namespace": "blob-admission", "content": base64.b64encode(content).decode(), "metadata": {"filename": name, "original_name": name, "mime_type": "text/plain", "external_id": external_id}},
        "store_blob_from_file": {"namespace": "blob-admission", "file_path": str(source), "filename": name, "mime_type": "text/plain", "metadata": {"external_id": external_id}},
        "retrieve_blob_by_name": {"filename": name},
    }


def assert_lineage(fixture: Fixture, action: QueuedAction, receipt: dict[str, Any]) -> None:
    assert {"action_status", "data", "actions", "error", "timestamp"} <= receipt.keys()
    job_id = receipt["data"]["job_id"]
    job = cast(Any, fixture.manager.get_job(job_id))["data"]["job"]
    owned = cast(Any, fixture.state.read_state("core", {"table": "job", "filters": {"group_id": f"registered:{action.id}"}}))["data"]["records"]
    assert [row["id"] for row in owned] == [job_id], "exactly one persisted job for this outer invocation"
    assert job["flow_token_id"] == action.flow_token_id
    assert job["group_id"] == f"registered:{action.id}"
    metadata = json.loads(job["metadata"])
    assert metadata["registered_action_id"] == action.id and metadata["flow_id"] == action.flow_id and metadata["session_id"] == action.session_id
    request = cast(Any, fixture.manager.get_job_payload(job_id))["data"]["payload"]
    assert len(json.dumps(request)) < 512, "admission persists a bounded descriptor, never content or metadata"


def parked_case(fixture: Fixture, verb: str, *, content: bytes = CONTENT, case: str = "ordinary") -> None:
    values = arguments(fixture.home, f"{verb}-{case}.txt", content=content)
    if verb == "retrieve_blob_by_name":
        seeded = fixture.service.store_blob("blob-admission", content, values["store_blob"]["metadata"])
        assert seeded.get("action_status") == "completed", seeded
        assert fixture.service.get_by_name(values[verb]["filename"]), "unchanged-base valid name lookup"
    target, method = {"store_blob": (fixture.provider, "_write_blob_file"), "store_blob_from_file": (ingestion, "read_content"), "retrieve_blob_by_name": (fixture.service, "get_by_name")}[verb]
    original = getattr(target, method)
    entered, release = threading.Event(), threading.Event()
    leaf_threads: list[int] = []
    parse_threads: list[int] = []
    original_parse = fixture.processor._parse_action_parameters

    def leaf(*args: Any, **kwargs: Any) -> Any:
        leaf_threads.append(threading.get_ident())
        assert get_current_flow_token_id() == action.flow_token_id
        entered.set()
        assert release.wait(5), "owned leaf release"
        return original(*args, **kwargs)

    def parse(queued: Any) -> dict[str, object]:
        if queued.id == action.id:
            parse_threads.append(threading.get_ident())
        return original_parse(queued)

    action = fixture.action(verb, values[verb])
    cheap = fixture.action("search_blobs", {"namespace": "blob-admission", "metadata_filters": {}}, token=False)
    timer = threading.Timer(0.3, release.set)
    timer.start()
    with patch.object(target, method, side_effect=leaf), patch.object(fixture.processor, "_parse_action_parameters", side_effect=parse):
        try:
            receipt = asyncio.run(fixture.dispatch(action))
            assert entered.wait(2), "real operational leaf reached with valid setup"
            print(json.dumps({"verb": verb, "case": case, "content_bytes": len(content), "transport_bytes": len(action.parameters.encode()), "elapsed_ms": fixture.timings[action.id], "real_poller_preflight": True, "leaf_reached": True, "receipt_keys": sorted(receipt["data"])}), flush=True)
            assert_parked_admission(fixture, action, receipt, parse_threads, leaf_threads)
            control = asyncio.run(fixture.dispatch(cheap))
            assert "job_id" not in control["data"] and not release.is_set(), "ordinary bounded action dispatches while Blob leaf remains parked"
        finally:
            release.set()
            timer.cancel()
            timer.join()
            join_owner(fixture)
    assert_blob_outcome(fixture, action, receipt, verb, content)


def assert_parked_admission(fixture: Fixture, action: QueuedAction, receipt: dict[str, Any], parse_threads: list[int], leaf_threads: list[int]) -> None:
    assert "job_id" in receipt["data"], "whole_blob_operation_returns_job_before_parked_leaf"
    assert fixture.timings[action.id] < 50, "registered Blob admission must return under the census 50 ms line"
    assert_lineage(fixture, action, receipt)
    worker = fixture.manager._owners["blob_storage_service"]._thread
    assert worker is not None
    assert parse_threads == leaf_threads == [worker.ident], "ActionProcessor argument preparation and service effects share the owner worker; poller transport preflight precedes admission"


def assert_blob_outcome(fixture: Fixture, action: QueuedAction, receipt: dict[str, Any], verb: str, content: bytes) -> None:
    job = fixture.terminal(receipt)
    assert job["status"] == "completed" and job["error"] is None, job
    result = job["result"]
    assert {"action_status", "data", "actions", "error", "timestamp"} <= result.keys(), result
    blob_id = result["data"]["blob_id"]
    assert (fixture.provider.blobs_dir / blob_id).read_bytes() == content
    if verb == "retrieve_blob_by_name":
        assert bytes.fromhex(result["data"]["content"]) == content
    assert_one_completion(fixture, action, job, result)


def assert_one_completion(fixture: Fixture, action: QueuedAction, job: dict[str, Any], result: dict[str, Any]) -> None:
    routes = [route for route in fixture.routes if route[0] == action.id]
    assert len(routes) == 1 and routes[0][2] == result
    assert fixture.manager.update_job(job["id"], {"status": "completed", "result": {"wrong": True}})["action_status"] == "error", "stale terminal rewrite refused"
    assert fixture.reader.get_job(job["id"])["data"]["result"]["job"]["result"] == result


def ordering_and_controls(fixture: Fixture) -> None:
    values = arguments(fixture.home, "ordered.txt")
    write = fixture.action("store_blob_from_file", values["store_blob_from_file"])
    read = fixture.action("retrieve_blob_by_name", values["retrieve_blob_by_name"])

    stored, retrieved = ordered_receipts(fixture, write, read)
    stored_job, read_job = fixture.terminal(stored), fixture.terminal(retrieved)
    assert stored_job["result"]["data"]["blob_id"] == read_job["result"]["data"]["blob_id"]
    assert bytes.fromhex(read_job["result"]["data"]["content"]) == CONTENT
    direct = fixture.service.retrieve_blob_by_name("ordered.txt")
    assert direct.get("data") == read_job["result"]["data"], "internal synchronous composition retains full results without nested jobs"
    for verb, params in (("search_blobs", {"namespace": "blob-admission", "metadata_filters": {}}), ("file_command", {"command": "ls --count"})):
        expected = getattr(fixture.service, verb)(**params)
        actual = asyncio.run(fixture.dispatch(fixture.action(verb, params, token=False)))
        assert actual["data"] == expected["data"] and "job_id" not in actual["data"]
    print("PASS serial store/read, synchronous internal composition, unchanged bounded search/file controls")


def ordered_receipts(fixture: Fixture, write: QueuedAction, read: QueuedAction) -> tuple[dict[str, Any], dict[str, Any]]:
    entered, release = threading.Event(), threading.Event()
    original = fixture.provider._write_blob_file

    def parked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    async def pair() -> tuple[dict[str, Any], dict[str, Any]]:
        stored = await fixture.dispatch(write)
        assert entered.wait(2)
        retrieved = await fixture.dispatch(read)
        return stored, retrieved

    with patch.object(fixture.provider, "_write_blob_file", side_effect=parked), patch.object(fixture.service, "get_by_name", wraps=fixture.service.get_by_name) as lookup:
        try:
            receipts = asyncio.run(pair())
            assert lookup.call_count == 0, "registered read cannot overtake the prior whole store operation"
        finally:
            release.set()
            worker = fixture.manager._owners["blob_storage_service"]._thread
            if worker is not None:
                worker.join(5)
    return receipts


def join_owner(fixture: Fixture) -> None:
    owner = fixture.manager._owners.get("blob_storage_service")
    worker = owner._thread if owner is not None else None
    if worker is not None:
        worker.join(5)
        assert not worker.is_alive(), "owned worker must retire before private PG teardown"


def failure_cases(fixture: Fixture) -> None:
    for verb, params, code in (
        ("retrieve_blob_by_name", {"filename": "missing.txt"}, "blob_storage_service.blob_not_found"),
        ("store_blob_from_file", {"namespace": "blob-admission", "file_path": "relative.txt"}, "blob_storage.file_path_not_absolute"),
    ):
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action(verb, params))))
        assert job["status"] == "error" and job["error"]["code"] == code, job
        assert job["result"]["error"]["code"] == code
    rollback_and_conflict(fixture)


def rollback_and_conflict(fixture: Fixture) -> None:
    values = arguments(fixture.home, "rollback.txt")
    with patch.object(fixture.provider, "_write_blob_file", return_value=(fixture.home / "never-written", OSError("owned failed write"))):
        failed = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_blob", values["store_blob"]))))
    assert failed["status"] == "error", failed
    rolled = cast(Any, fixture.service.get_by_name("rollback.txt"))
    assert rolled["metadata"]["is_deleted"] == 1 and rolled["blob_id"] is None, "existing rollback retains soft-deleted metadata, not a successful blob"
    assert not (fixture.provider.blobs_dir / rolled["metadata"]["id"]).exists()
    values = arguments(fixture.home, "rollback-recovered.txt")
    recovered = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_blob", values["store_blob"]))))
    assert recovered["status"] == "completed", recovered
    conflict = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_blob", values["store_blob"]))))
    assert conflict["status"] == "error" and conflict["error"]["code"] == "blob_storage.external_id_conflict", conflict
    assert (fixture.provider.blobs_dir / recovered["result"]["data"]["blob_id"]).read_bytes() == CONTENT
    assert fixture.provider.resolve_blob_path("../outside") is None
    print("PASS exact typed failures, existing soft-delete rollback, successor recovery, unique name conflict, containment control")


def metadata_lifecycle(fixture: Fixture) -> None:
    stored = fixture.service.get_by_name("ordered.txt")
    assert stored is not None
    blob_id = str(stored["blob_id"])
    revisions: tuple[dict[str, object], ...] = ({"filename": "corrected.txt", "description": "owned correction"}, {"filename": "ordered.txt", "description": ""})
    for metadata in revisions:
        corrected = fixture.service.update_blob_metadata("blob-admission", blob_id, metadata)
        assert corrected.get("action_status") == "completed", corrected
        current = fixture.service.get_blob_metadata("blob-admission", blob_id)
        actual = cast(Any, current)["data"]["metadata"]
        assert all(actual[key] == value for key, value in metadata.items())
    removed = fixture.service.delete_blob("blob-admission", blob_id)
    assert removed.get("action_status") == "completed", removed
    assert fixture.provider.resolve_blob_path(blob_id) is None
    print("PASS existing metadata correct/reverse/clear-value and physical delete lifecycle")


def delete_failure_ordering(fixture: Fixture) -> None:
    namespace = "delete-ordering"
    stored = fixture.provider.store_blob(namespace, CONTENT, {})
    blob_id = str(cast(Any, stored)["data"]["blob_id"])
    blob_path = fixture.provider.blobs_dir / blob_id
    for failure in ({"action_status": "error", "error": {"message": "planted metadata error"}}, {"action_status": "completed", "data": {"result": {"deleted": 0}}}):
        with patch.object(fixture.state, "delete_records", return_value=failure):
            result = fixture.provider.delete_blob(namespace, blob_id)
        assert result.get("action_status") == "error", "metadata retirement failure must not report successful deletion"
        assert cast(Any, result)["error"]["code"] == "blob_storage.metadata_storage_error", result
        assert blob_path.read_bytes() == CONTENT, "metadata failure must preserve readable bytes"
        row = cast(Any, fixture.state.read_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": blob_id}, "limit": 1}))["data"]["records"][0]
        assert row["is_deleted"] == 0, row
    with patch.object(fixture.state, "delete_records", side_effect=RuntimeError("planted raised metadata error")):
        result = fixture.provider.delete_blob(namespace, blob_id)
    assert result.get("action_status") == "error" and cast(Any, result)["error"]["code"] == "blob_storage.metadata_storage_error", result
    assert blob_path.read_bytes() == CONTENT
    with patch.object(Path, "unlink", side_effect=PermissionError("planted unlink failure")):
        result = fixture.provider.delete_blob(namespace, blob_id)
    assert result.get("action_status") == "error" and cast(Any, result)["error"]["code"] == "blob_storage.storage_error", result
    row = cast(Any, fixture.state.read_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": blob_id}, "limit": 1}))["data"]["records"][0]
    assert row["is_deleted"] == 1 and blob_path.read_bytes() == CONTENT, "unlink failure must leave only retired metadata plus leftover bytes"
    print("PASS provider metadata errors/zero affected/raised errors preserve bytes; unlink error follows durable retirement and is typed failure")


def worker_loss(fixture: Fixture) -> None:
    values = arguments(fixture.home, "worker-lost.txt")["store_blob"]
    with patch.object(fixture.provider, "_write_blob_file", side_effect=SystemExit("owned worker loss")) as leaf:
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_blob", values))))
        assert job["status"] == "error" and job["error"]["code"] == "registered_owner.worker_lost", job
        assert leaf.call_count == 1, "lost volatile body never replays its effect"
    successor = arguments(fixture.home, "worker-successor.txt")["store_blob"]
    job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_blob", successor))))
    assert job["status"] == "completed", job
    print("PASS worker loss retains uncertain typed error and successor never replays lost body")


def terminal_uncertainty(fixture: Fixture) -> None:
    values = arguments(fixture.home, "terminal-uncertain.txt")["store_blob"]
    update = fixture.manager.update_job

    def refuse_terminal(job_id: str, updates: dict[str, object]) -> dict[str, object]:
        if updates.get("status") == "completed":
            return {"action_status": "error", "error": {"code": "owned_terminal_write_uncertain"}}
        return update(job_id, updates)

    with patch.object(fixture.manager, "update_job", side_effect=refuse_terminal), patch.object(fixture.provider, "_write_blob_file", wraps=fixture.provider._write_blob_file) as leaf, patch("ananta.core.state.registered_job_operations.logger.exception") as logged:
        receipt = asyncio.run(fixture.dispatch(fixture.action("store_blob", values)))
        owner = fixture.manager._owners["blob_storage_service"]
        worker = owner._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()
        job = cast(Any, fixture.manager.get_job(receipt["data"]["job_id"]))["data"]["job"]
        assert job["status"] == "processing" and leaf.call_count == 1, job
        logged.assert_called_once()
    assert fixture.service.get_by_name("terminal-uncertain.txt") is not None, "effects survive the uncertain outcome write"
    assert update(job["id"], {"status": "error", "error": {"code": "owned_reconciliation"}})["action_status"] == "completed"
    print("PASS uncertain terminal write remains visible for reconciliation and never replays effects")


def notification_failure(fixture: Fixture) -> None:
    values = arguments(fixture.home, "notification-failed.txt")["store_blob"]
    with patch.object(fixture.manager, "_registered_completion", side_effect=RuntimeError("owned route failure")), patch.object(fixture.provider, "_write_blob_file", wraps=fixture.provider._write_blob_file) as leaf:
        job = fixture.terminal(asyncio.run(fixture.dispatch(fixture.action("store_blob", values))))
        assert job["status"] == "completed" and job["result"]["data"]["blob_id"] and leaf.call_count == 1, job
    print("PASS notification failure preserves committed full outcome and one effect")


def declarations(fixture: Fixture) -> None:
    for verb in SELECTED:
        entry = fixture.registry["processes"][PREFIX + verb]
        assert entry["is_async"] is True
        schema = entry["output"]
        schema = schema.to_dict() if hasattr(schema, "to_dict") else schema
        assert set(schema["properties"]) == {"job_id", "status"}
    for verb in ("search_blobs", "file_command", "get_blob_metadata", "delete_blob"):
        assert fixture.registry["processes"][PREFIX + verb]["is_async"] is False


def main() -> int:
    try:
        with ledger_fixture("fast_return_census", schema="blob_registered_admission") as pg:
            plugin = DefaultBlobStoragePlugin()
            schemas = SchemaManager(pg.state, pg.plugin)
            schemas.initialize_schemas([*CoreSchemaDefinitions.get_all_core_schemas(), *plugin.get_schema_definitions()])
            pg.state.set_schema_manager(schemas)
            fixture = Fixture(pg.state, pg.app_home)
            for verb in sorted(SELECTED):
                parked_case(fixture, verb)
            parked_case(fixture, "store_blob", content=MID_CONTENT, case="mid_size")
            ordering_and_controls(fixture)
            failure_cases(fixture)
            metadata_lifecycle(fixture)
            delete_failure_ordering(fixture)
            worker_loss(fixture)
            terminal_uncertainty(fixture)
            notification_failure(fixture)
            declarations(fixture)
    except MissingFixtureError:
        return skip_fixture()
    print("g1_blob_registered_admission_smoke PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
