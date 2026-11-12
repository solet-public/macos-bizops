#!/usr/bin/env python3
"""Guard the serial ledger owner, replacing the synchronous-flag tripwire.

Concurrent producers enter the real serial poller and ActionProcessor. With the
first PostgreSQL session upsert parked, persisted admissions and an ordinary
read must return; the owner must preserve claim order and one writer. Run the
private body through quality_gates/provision_test_pg.py --exec. Without its
manifest declaration checks still run and the private body exits 77.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "postgres_state_management_plugin" / "src"))

from _stub_state_service import StubBlobStorageService  # noqa: E402
from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.core.actions.action_processor import _REGISTERED_JOB_OPERATIONS, ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.runtime.loop_executors import close_loop_executors  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import NAMESPACE, TABLE_EVENT, TABLE_SESSION, get_session_ledger_schema  # noqa: E402
from ananta.llm.session_ledger.types import ImportBatchStatus, IngestSourceKind, SourceVendor  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: E402

from quality_gates.private_test_pg import MANIFEST_ENV, LedgerFixture, ledger_fixture, skip_fixture  # noqa: E402

PARSER_ROOT = ROOT / "plugins" / "claude_code_pushed_session_source_plugin" / "src" / "claude_code_pushed_session_source_plugin"
INGEST = "service_interface::session_ledger_service::ingest_raw_chunk"
STATUS = "service_interface::session_ledger_service::get_import_status"
SESSION = "serial-owner-session"
TIMES = ("2026-11-09T11:00:00", "2026-11-09T13:00:00", "2026-11-09T10:00:00", "2026-11-09T14:00:00")
SNAPSHOT = {
    "originator_session_label": "first-originator",
    "originator_agent_instance_id": "first-originator-id",
    "recipient_session_label": "first-recipient",
    "recipient_agent_instance_id": "first-recipient-id",
    "summary_text": "first-summary",
}


@dataclass
class Action(QueuedAction):
    source_plugin: str | None = None


def processes() -> dict[str, Any]:
    result: dict[str, Any] = {}
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    for provider in ("session_ledger_service", "job_service"):
        module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
        scanner._scan_module_classes(module, module.__name__, result)
    return result


def declaration() -> None:
    entry = processes()[INGEST]
    assert "ingest_raw_chunk" in _REGISTERED_JOB_OPERATIONS["session_ledger_service"]
    assert entry["is_async"] is True and entry["action_blueprint"]["metadata"]["is_async"] is True
    schema = entry["output"]
    schema_dict = schema.to_dict() if hasattr(schema, "to_dict") else schema
    assert set(schema_dict["properties"]) == {"job_id", "status"}
    print("PASS truthful queued admission declaration and ledger owner registration")


def chunk(index: int, session: str = SESSION) -> str:
    return json.dumps({"type": "user", "uuid": f"u1b-{session}-{index}", "sessionId": session, "timestamp": f"{TIMES[index]}Z", "message": {"role": "user", "content": f"chunk {index}"}})


class Harness:
    """Real registry, orchestrator, dispatcher, job manager, ledger and parser.

    Only action-queue transport/status storage and the unused inline-blob seam
    are replaced. Job requests/outcomes and session writes use the fixture's
    production StateManagementInterface.
    """

    def __init__(self, fixture: LedgerFixture) -> None:
        sys.path.insert(0, str(PARSER_ROOT.parent))
        from claude_code_pushed_session_source_plugin.plugin import ClaudeCodePushedSessionSourcePlugin

        self.state = fixture.state
        schemas = SchemaManager(self.state, fixture.plugin)
        schemas.initialize_schemas([get_session_ledger_schema(), CoreSchemaDefinitions.get_job_schema(), CoreSchemaDefinitions.get_job_payload_schema(), CoreSchemaDefinitions.get_flows_schema(), CoreSchemaDefinitions.get_flow_tokens_schema()])
        self.state.set_schema_manager(schemas)
        plugins = PluginManager()
        source = ClaudeCodePushedSessionSourcePlugin()
        plugins.plugins[source.name] = source
        self.service = SessionLedgerService(state_service=cast(Any, self.state), blob_storage_service=cast(Any, StubBlobStorageService()), plugin_manager=plugins)
        self.graph = FlowRuntimeGraph(self.state)
        self.manager = AsyncJobManager(self.state, self.graph)
        self.jobs = JobService(self.state, self.manager)
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager = str(fixture.app_home), plugins
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        orchestrator.session_ledger_service = self.service
        orchestrator.service_manager = SimpleNamespace(job_service=self.jobs)
        registry = processes()
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = registry
        contexts = ExecutionContextManager()
        contexts.create_context("u1b-flow")
        self.processor = ActionProcessor(plugins, state_service=self.state, orchestrator=orchestrator, process_registry={"processes": registry}, execution_context_manager=contexts, discovery_service=discovery)
        self.source_id = self.service._register_source_internal(source_kind="claude_code_pushed", root_uri="pushed:u1b")["source_id"]
        self.session_id = self.seed()
        self.queue: list[Action] = []
        self.claimed: list[str] = []
        self.responses: dict[str, dict[str, Any]] = {}
        self.seconds: dict[str, float] = {}
        self.failures: list[str] = []

    def seed(self) -> str:
        return self.service._repository.upsert_session(
            source_id=self.source_id,
            external_session_id=SESSION,
            vendor=SourceVendor.CLAUDE_CODE,
            source_kind=IngestSourceKind.CLAUDE_CODE_PUSHED,
            vendor_session_label="first-label",
            project_path="/first/path",
            first_event_at=datetime(2026, 11, 9, 12),
            last_event_at=datetime(2026, 11, 9, 12),
            summary_text_seed=SNAPSHOT["summary_text"],
            **{key: value for key, value in SNAPSHOT.items() if key != "summary_text"},
        )

    def action(self, name: str, index: int, session: str = SESSION) -> Action:
        token = self.graph.create_token("u1b-flow", TokenOwnerType.PROCESS, name, process_key=INGEST)
        return Action(name, INGEST, json.dumps({"source_kind": "claude_code_pushed", "source_id": self.source_id, "chunk_text": chunk(index, session)}), "serial owner proof", "now", flow_id="u1b-flow", flow_token_id=token)

    def dispatch(self, action: QueuedAction) -> dict[str, object]:
        started = time.perf_counter()
        result = self.processor.execute_action(cast(Any, action))
        self.seconds[action.id] = time.perf_counter() - started
        return result

    def complete(self, action_id: str, result: dict[str, Any], _token: str | None = None, *, on_status_written: Any = None) -> None:
        self.responses[action_id] = result
        if on_status_written is not None:
            on_status_written()

    def fail(self, action_id: str, message: str, *_args: object, **_kwargs: object) -> None:
        self.failures.append(f"{action_id}: {message}")

    def poller(self) -> Any:
        poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)

        async def queued() -> list[Action]:
            return list(self.queue)

        def claim(action_id: str) -> bool:
            self.claimed.append(action_id)
            self.queue[:] = [action for action in self.queue if action.id != action_id]
            return True

        poller._get_queued_actions, poller._mark_action_processing = queued, claim
        poller._is_terminated_flow_sibling = lambda _action: False
        poller._prepare_action_for_execution = lambda _action: True
        poller._resolve_io_context = lambda _action: None
        poller._mark_action_completed, poller._mark_action_failed = self.complete, self.fail
        poller.action_processor = SimpleNamespace(execute_action=self.dispatch)
        poller.total_actions_processed = poller.total_poll_cycles = 0
        poller._last_observed_queue_depth = len(self.queue)
        return poller

    def job(self, job_id: str) -> dict[str, Any]:
        response = self.jobs.get_job(job_id)
        assert response["action_status"] == "completed", response
        return response["data"]["result"]["job"]

    def rows(self, table: str, filters: dict[str, object]) -> list[dict[str, Any]]:
        response = self.state.query_state(NAMESPACE, {"table": table, "filters": filters})
        assert response.get("action_status") == "completed", response
        data = response.get("data")
        assert isinstance(data, dict) and isinstance(data.get("records"), list), response
        return cast(list[dict[str, Any]], data["records"])

    def join(self) -> None:
        thread = self.manager._owners["session_ledger_service"]._thread
        if thread is not None:
            thread.join(10)
            assert not thread.is_alive(), "ledger owner did not drain"


class ParkedUpsert:
    def __init__(self) -> None:
        self.original = SessionLedgerRepository.upsert_session
        self.entered, self.release = threading.Event(), threading.Event()
        self.lock = threading.Lock()
        self.order: list[datetime] = []
        self.active = 0
        self.maximum = 0

    def invoke(self, repository: SessionLedgerRepository, **values: Any) -> str:
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.order.append(values["first_event_at"])
            first = len(self.order) == 1
        try:
            if first:
                self.entered.set()
                assert self.release.wait(10), "fixture must release its parked upsert"
            return self.original(repository, **values)
        finally:
            with self.lock:
                self.active -= 1


def enqueue_concurrently(harness: Harness) -> list[int]:
    barrier, lock = threading.Barrier(len(TIMES)), threading.Lock()
    order: list[int] = []

    def producer(index: int) -> None:
        action = harness.action(f"push-{index}", index)
        barrier.wait()
        with lock:
            order.append(index)
            harness.queue.append(action)

    with ThreadPoolExecutor(max_workers=len(TIMES)) as producers:
        list(producers.map(producer, range(len(TIMES))))
    return order


async def admissions(harness: Harness, parked: ParkedUpsert) -> list[int]:
    order = enqueue_concurrently(harness)
    control_batch = harness.service._repository.start_batch(harness.source_id, polling_lease_token="control")
    harness.service._repository.finish_batch(control_batch, polling_lease_token="control", status=ImportBatchStatus.COMPLETED)
    harness.queue.append(Action("ordinary", STATUS, json.dumps({"batch_id": control_batch}), "ordinary point read", "now", flow_id="u1b-flow"))
    poll = asyncio.create_task(harness.poller()._poll_once())
    returned = False
    try:
        assert await asyncio.to_thread(parked.entered.wait, 5), "actual PostgreSQL persist leaf reached"
        try:
            await asyncio.wait_for(asyncio.shield(poll), 0.5)
            returned = True
        except TimeoutError:
            pass
        before_release = list(parked.order)
        ordinary = "ordinary" in harness.responses
        if returned:
            durable_requests(harness, order)
    finally:
        parked.release.set()
        await asyncio.wait_for(poll, 10)
        await close_loop_executors()
        harness.join()
    admission_outcome(harness, parked, order, returned, ordinary, before_release)
    return order


def durable_requests(harness: Harness, order: list[int]) -> None:
    for index in order:
        receipt = harness.responses[f"push-{index}"]["data"]
        job_id = receipt["job_id"]
        assert receipt["status"] == "queued" and harness.job(job_id)["status"] in {"queued", "processing"}
        request = harness.manager.get_job_payload(job_id)["data"]
        assert isinstance(request, dict) and request["payload"]["parameters"]["chunk_text"] == chunk(index), "request must be durable while effects are parked"


def admission_outcome(harness: Harness, parked: ParkedUpsert, order: list[int], returned: bool, ordinary: bool, before_release: list[datetime]) -> None:
    print("ADMISSION_SECONDS", json.dumps(harness.seconds, sort_keys=True))
    print("PERSIST_ORDER", [value.isoformat() for value in parked.order], "ADMISSION_ORDER", order)
    assert returned, "persisted admissions must return while the first persist is parked"
    assert ordinary, "an ordinary action must dispatch while the ledger worker is parked"
    assert len(before_release) == 1, "second push cannot enter a parked first write"
    assert before_release[0] == datetime.fromisoformat(f"{TIMES[order[0]]}+00:00"), "first admitted push must own the parked write"
    assert not harness.failures, harness.failures
    assert all(value < 0.05 for value in harness.seconds.values()), "each admission and ordinary action must stay below the census 50 ms line"
    assert parked.maximum == 1, "one ledger writer only"
    assert parked.order == [datetime.fromisoformat(f"{TIMES[index]}+00:00") for index in order], "persist order must equal poller admission order"


def persisted_results(harness: Harness, order: list[int]) -> None:
    for index in order:
        response = harness.responses[f"push-{index}"]
        assert response["data"]["status"] == "queued" and response["timestamp"]
        job = harness.job(response["data"]["job_id"])
        assert job["status"] == "completed" and job["error"] is None, job
        result = job["result"]
        assert result["events_persisted"] == 1 and result["batch_id"], result
        assert harness.service.get_import_status(result["batch_id"])["status"] == "completed"
    rows = harness.rows(TABLE_SESSION, {"id": harness.session_id})
    row = rows[0]
    assert row["first_event_at"] == "2026-11-09T10:00:00" and row["last_event_at"] == "2026-11-09T14:00:00", row
    assert {key: row[key] for key in SNAPSHOT} == SNAPSHOT, row
    assert row["vendor_session_label"] == "first-label" and row["project_path"] == "/first/path"
    events = harness.rows(TABLE_EVENT, {"session_id": harness.session_id})
    assert len(events) == len(TIMES), events
    print("PASS concurrent persisted admissions under 50ms, ordinary dispatch, FIFO single writer, bounds and COALESCE snapshots")


def worker_loss(harness: Harness) -> None:
    original = SessionLedgerService.ingest_raw_chunk

    def lost(service: SessionLedgerService, *args: Any, **kwargs: Any) -> dict[str, Any]:
        original(service, *args, **kwargs)
        raise SystemExit("worker lost after committed ledger effects")

    with patch.object(SessionLedgerService, "ingest_raw_chunk", lost):
        receipt = harness.dispatch(harness.action("lost", 0, "uncertain-session"))
        harness.join()
    job_id = cast(dict[str, Any], receipt["data"])["job_id"]
    job = harness.job(job_id)
    assert job["status"] == "error" and job["error"]["code"] == "registered_owner.worker_lost", job
    assert job["error"]["details"]["uncertain"] is True and job["status_reason"] == "owner_worker_lost_uncertain", job
    assert harness.manager.update_job(job_id, {"status": "processing"})["action_status"] == "error", "a terminal uncertain job must not be replayed"
    restarted = AsyncJobManager(harness.state)
    assert restarted._owners == {} and harness.job(job_id)["status"] == "error", "new manager must not replay persisted requests"
    row = harness.rows(TABLE_SESSION, {"external_session_id": "uncertain-session"})[0]
    events = harness.rows(TABLE_EVENT, {"session_id": row["id"]})
    assert len(events) == 1, "effects committed once despite uncertain worker completion"
    with patch("threading.Thread.start", side_effect=FrameworkError("owner unavailable", error_code="ledger.owner_unavailable")):
        refused = harness.dispatch(harness.action("unavailable", 1, "never-written"))
    failed = harness.job(cast(dict[str, Any], refused["data"])["job_id"])
    assert failed["status"] == "error" and failed["error"]["code"] == "ledger.owner_unavailable", failed
    print("PASS typed owner loss after effects, terminal stale-write refusal, no replay, durable worker-start error")


def main() -> int:
    declaration()
    if not PARSER_ROOT.is_dir():
        print("ADVISORY private real-parser fixture skipped: claude_code_pushed_session_source_plugin is absent from this seed")
        return 77
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    with ledger_fixture("ledger_ingest") as fixture:
        harness, parked = Harness(fixture), ParkedUpsert()

        def upsert(repository: SessionLedgerRepository, **values: Any) -> str:
            return parked.invoke(repository, **values)

        with patch.object(SessionLedgerRepository, "upsert_session", upsert):
            order = asyncio.run(admissions(harness, parked))
        persisted_results(harness, order)
        worker_loss(harness)
        direct = harness.service.ingest_raw_chunk("claude_code_pushed", chunk(2, "internal-session"), source_id=harness.source_id)
        assert direct["events_persisted"] == 1 and direct["batch_id"], "internal callers keep the complete synchronous result"
        print("PASS synchronous internal body and real pushed parser unchanged")
    print("PRIVATE_PG_BODY ledger_ingest serial owner admission proof")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
