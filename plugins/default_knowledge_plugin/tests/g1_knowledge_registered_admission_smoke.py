#!/usr/bin/env python3
"""Real Knowledge admission, provider serialization and lifecycle proof.

The registry, factory, recorder, processor, poller, provider, job manager and
PostgreSQL state are production implementations. Only queue transport and the
embedding-backed memory collaborator use owned fixtures. Missing private PG is
an advisory skip; it never selects an application's database. Existing poller
transport validation remains a separately disclosed bounded residual.
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
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT, ROOT / "ananta/src", ROOT / "plugins/default_knowledge_plugin/src", ROOT / "plugins/postgres_state_management_plugin/src"):
    sys.path.insert(0, str(directory))

# Shipped profiles may omit either optional provider source tree.
for source in ("plugins/default_knowledge_plugin/src/default_knowledge_plugin", "plugins/postgres_state_management_plugin/src/postgres_state_management_plugin"):
    if not (ROOT / source).is_dir():
        print(f"ADVISORY optional source absent: {source}; private Knowledge proof skipped")
        raise SystemExit(77)

from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.orchestration.managers.action_event_recorder import ActionEventRecorder  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.runtime.loop_executors import close_loop_executors  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from ananta.services.knowledge_service import KnowledgeService  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from default_knowledge_plugin.constants import PLUGIN_NAME, TABLE_KNOWLEDGE_INSTALL, TAG_DOMAIN_OFFICIAL, kb_id_tag  # noqa: E402
from default_knowledge_plugin.kb_indexing import parse_memory_ids  # noqa: E402
from default_knowledge_plugin.plugin import DefaultKnowledgePlugin  # noqa: E402

from quality_gates.private_test_pg import MANIFEST_ENV, LedgerFixture, ledger_fixture, skip_fixture  # noqa: E402

OPERATIONS = frozenset({"archive_file", "browse", "create_file", "delete_file", "edit_file", "ingest", "install", "purge_orphaned_chunks", "read_file", "refresh_plugin_process", "refresh_plugin_processes", "search", "search_planning_references", "test_retrieval", "uninstall", "update"})
REFRESHED_KEY = "service_interface::knowledge_service::read_file"


def registry() -> dict[str, Any]:
    result: dict[str, Any] = {}
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    for provider in ("knowledge_service", "job_service"):
        module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
        scanner._scan_module_classes(module, module.__name__, result)
    return result


def declarations() -> None:
    processes = registry()
    for name in OPERATIONS:
        entry = processes[f"service_interface::knowledge_service::{name}"]
        assert entry["is_async"] is True and entry["action_blueprint"]["metadata"]["is_async"] is True, name
        output = entry["output"]
        assert set(output["properties"]) == {"job_id", "status"}, name
    for name in ("list_installed", "audit_retrieval_corpus"):
        assert processes[f"service_interface::knowledge_service::{name}"]["is_async"] is False, name
    print("PASS all16 actual registrations describe receipts; bounded controls unchanged")


class Memory:
    """Owned deterministic chunk collaborator; no native/vector/model calls."""

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.next_id = 0
        self.park = False
        self.entered, self.release = threading.Event(), threading.Event()

    def remember(self, content: str, tags: list[str]) -> dict[str, Any]:
        self.next_id += 1
        identity = f"owned-chunk-{self.next_id}"
        self.store[identity] = {"id": identity, "content": content, "tags": list(tags), "status": "active", "similarity": 1.0}
        return {"memory_id": identity}

    def get_memories_by_tag(self, tag: str, include_archived: bool = False) -> dict[str, Any]:
        return {"memories": [row for row in self.store.values() if tag in row["tags"] and (include_archived or row["status"] == "active")]}

    def delete_memories_by_ids(self, ids: list[str]) -> None:
        if self.park:
            self.park = False
            self.entered.set()
            assert self.release.wait(10), "owned deletion leaf must be released"
        for identity in ids:
            self.store.pop(identity, None)

    def delete_memories_by_tag(self, tag: str) -> None:
        self.delete_memories_by_ids([key for key, row in self.store.items() if tag in row["tags"]])

    def recall(self, query: str, top_k: int = 8, tags: list[str] | None = None, exclude_ids: list[str] | None = None, score_by_similarity: bool = False) -> dict[str, Any]:
        del query, score_by_similarity
        return {"memories": [row for key, row in self.store.items() if key not in (exclude_ids or []) and all(tag in row["tags"] for tag in tags or [])][:top_k]}


class NoFactoryTemplates:
    def resolve_templates(self, action_def: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        del action_def, context
        raise AssertionError("owned factory inputs contain no templates")


def response_data(response: object) -> dict[str, Any]:
    assert isinstance(response, dict) and response.get("action_status") == "completed", response
    data = response.get("data")
    assert isinstance(data, dict), response
    return cast(dict[str, Any], data)


@dataclass
class Submission:
    contexts: ExecutionContextManager
    factory: ActionFactory
    flows: FlowManager
    session: str


@dataclass
class Observed:
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    seconds: dict[str, float] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    notifications: list[tuple[str, str]] = field(default_factory=list)


class Harness:
    def __init__(self, fixture: LedgerFixture) -> None:
        self.state = fixture.state
        self.plugin = DefaultKnowledgePlugin()
        schemas = SchemaManager(self.state, fixture.plugin)
        definitions = [
            CoreSchemaDefinitions.get_sessions_schema(),
            CoreSchemaDefinitions.get_flows_schema(),
            CoreSchemaDefinitions.get_flow_tokens_schema(),
            CoreSchemaDefinitions.get_action_events_schema(),
            CoreSchemaDefinitions.get_job_schema(),
            CoreSchemaDefinitions.get_job_payload_schema(),
            *self.plugin.get_schema_definitions(),
        ]
        schemas.initialize_schemas(definitions)
        self.state.set_schema_manager(schemas)
        self.root = fixture.app_home / "knowledge_bases"
        self.root.mkdir()
        self.memory = Memory()
        self.plugin._kb_root, self.plugin._state_service = self.root, self.state
        self.plugin._memory_service = self.memory
        self.plugin._embedding_service = SimpleNamespace(input_token_budget=lambda: None)
        self.plugin.set_ready()
        plugins = PluginManager()
        plugins.plugins[PLUGIN_NAME] = self.plugin
        self.service = KnowledgeService(plugins, PLUGIN_NAME)
        graph = FlowRuntimeGraph(self.state)
        self.manager = AsyncJobManager(self.state, graph)
        self.jobs = JobService(self.state, self.manager)
        processes = registry()
        self.refresh_registry: dict[str, Any] = {"processes": {REFRESHED_KEY: dict(processes[REFRESHED_KEY])}}
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager = str(fixture.app_home), plugins
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        orchestrator.knowledge_service = self.service
        orchestrator.service_manager = SimpleNamespace(job_service=self.jobs)
        orchestrator.get_process_registry = lambda: self.refresh_registry
        orchestrator.apply_knowledge_base_updates = self.refresh
        self.plugin.set_orchestrator_ref(orchestrator)
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = processes
        contexts = ExecutionContextManager()
        self.processor = ActionProcessor(plugins, state_service=self.state, orchestrator=orchestrator, process_registry={"processes": processes}, execution_context_manager=contexts, discovery_service=discovery)
        flows = FlowManager(self.state)
        recorder = ActionEventRecorder(self.state, flows, graph)
        factory = ActionFactory({"processes": processes}, NoFactoryTemplates(), cast(Any, self.state), recorder, str(fixture.app_home))
        session = response_data(self.state.write_state("core", {"table": "sessions", "record": {"context_type": "owned_knowledge_fixture", "status": "active"}}))["result"]["generated_id"]
        self.submission = Submission(contexts, factory, flows, session)
        self.queue: list[QueuedAction] = []
        self.observed = Observed()
        self.manager.set_registered_completion_handler(lambda action, status, _result, _error: self.observed.notifications.append((action, status)))
        self.calls: list[tuple[str, str]] = []

    def refresh(self, updates: dict[str, dict[str, object]]) -> dict[str, object]:
        for key, update in updates.items():
            assert key == REFRESHED_KEY, "refresh fixture uses actual registered keys"
            self.refresh_registry["processes"][key].update(update)
        return {"updated_count": len(updates), "process_keys": list(updates), "errors": []}

    def action(self, verb: str, values: dict[str, object]) -> QueuedAction:
        flow = self.submission.flows.create_flow(self.submission.session, "owned_fixture", {})
        self.submission.contexts.create_context(flow)
        key = f"service_interface::knowledge_service::{verb}"
        identity = self.submission.factory.submit_action_definition({"name": f"owned_{verb}", "process_key": key, "arguments": values, "notes": "owned knowledge admission proof", "flow_id": flow, "session_id": self.submission.session, "result_processor_kind": None, "error_processor_kind": None})
        rows = response_data(self.state.read_state("core", {"table": "action_events", "filters": {"id": identity}, "limit": 1}))["records"]
        assert len(rows) == 1 and rows[0]["flow_token_id"], "real factory/recorder must persist action and token"
        row = rows[0]
        return QueuedAction(identity, key, row["parameters"], row["notes"], str(row["sequence"]), session_id=self.submission.session, flow_id=flow, flow_token_id=row["flow_token_id"])

    def dispatch(self, action: QueuedAction) -> dict[str, Any]:
        before = time.perf_counter()
        result = self.processor.execute_action(cast(Any, action))
        self.observed.seconds[action.id] = time.perf_counter() - before
        return cast(dict[str, Any], result)

    def completed(self, identity: str, result: dict[str, Any], _token: str | None = None, *, on_status_written: Any = None) -> None:
        self.observed.responses[identity] = result
        if on_status_written is not None:
            on_status_written()

    def failed(self, identity: str, message: str, *_args: object, **_kwargs: object) -> None:
        self.observed.errors.append(f"{identity}: {message}")

    def poller(self) -> Any:
        poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)

        async def queued() -> list[QueuedAction]:
            return list(self.queue)

        def claim(identity: str) -> bool:
            self.queue[:] = [action for action in self.queue if action.id != identity]
            return True

        poller._get_queued_actions, poller._mark_action_processing = queued, claim
        poller._is_terminated_flow_sibling = lambda _action: False
        poller._prepare_action_for_execution = lambda _action: True
        poller._resolve_io_context = lambda _action: None
        poller._mark_action_completed, poller._mark_action_failed = self.completed, self.failed
        poller.action_processor = SimpleNamespace(execute_action=self.dispatch)
        poller.total_actions_processed = poller.total_poll_cycles = 0
        poller._last_observed_queue_depth = len(self.queue)
        return poller

    def job_count(self) -> int:
        return len(response_data(self.state.read_state("core", {"table": "job", "limit": 100}))["records"])

    def join(self) -> None:
        for owner in list(self.manager._owners.values()):
            thread = owner._thread
            if thread is not None:
                thread.join(10)
                assert not thread.is_alive(), "owned Knowledge worker must drain"

    def final(self, verb: str, values: dict[str, object], *, status: str = "completed") -> dict[str, Any]:
        action = self.action(verb, values)
        response = self.dispatch(action)
        assert response["success"] is True and {"timestamp", "actions", "error"} <= response.keys(), response
        identity = response["data"]["job_id"]
        self.join()
        job = self.jobs.get_job(identity)["data"]["result"]["job"]
        assert job["status"] == status, job
        assert_job_lineage(self, job, action)
        assert self.observed.notifications.count((action.id, status)) == 1
        request = response_data(self.manager.get_job_payload(identity, "request"))["payload"]
        assert len(json.dumps(request)) < 1024 and "content" not in request.get("parameters", {}), request
        self.calls.append((verb, identity))
        return job


def make_kb(harness: Harness, name: str) -> Path:
    directory = harness.root / name
    directory.mkdir()
    (directory / "manifest.yaml").write_text(f"name: {name}\nwrite_posture: full\narchive_subdir: archive\ncontent:\n  chunking:\n    strategy: headers\n    max_chars: 3000\n", encoding="utf-8")
    (directory / "a.md").write_text("# A\n\nOriginal A.\n", encoding="utf-8")
    return directory


async def ordered_admission(harness: Harness, directory: Path) -> None:
    """First deletion parks; public successors and a bounded control progress."""
    (directory / "a.md").write_text("# A\n\nChanged before update.\n", encoding="utf-8")
    os.utime(directory / "a.md", (time.time() + 1, time.time() + 1))
    before_jobs = harness.job_count()
    harness.memory.park = True
    first = harness.action("update", {"name": "owned"})
    control = harness.action("list_installed", {})
    successors = [harness.action("edit_file", {"name": "owned", "path": "a.md", "content": "# A\n\nFinal A.\n"}), harness.action("create_file", {"name": "owned", "path": "public.md", "content": "# Public\n\n" + "Owned public content.\n" * 16384})]
    harness.queue[:] = [first, *successors, control]
    polling = asyncio.create_task(harness.poller()._poll_once())
    entered = await asyncio.to_thread(harness.memory.entered.wait, 5)
    print("WITNESS actual default Knowledge update reached its chunk-deletion leaf:", entered)
    direct_result: dict[str, Any] | None = None
    try:
        assert entered, (harness.observed.responses, harness.observed.errors)
        await asyncio.sleep(0.08)
        returned_while_parked = first.id in harness.observed.responses and control.id in harness.observed.responses
        with ThreadPoolExecutor(max_workers=1) as executor:
            direct = executor.submit(harness.service.create_file, "owned", "direct.md", "# Direct\n\nSynchronous create.\n")
            try:
                await asyncio.sleep(0.03)
                direct_waited = not direct.done() and not (directory / "direct.md").exists()
            finally:
                harness.memory.release.set()
                direct_result = direct.result(5)
        await polling
    finally:
        harness.memory.release.set()
        await polling
        harness.join()
        await close_loop_executors()
    assert returned_while_parked, "persisted Knowledge admission must return while the update deletion leaf is parked"
    assert not harness.observed.errors, harness.observed.errors
    assert direct_waited and direct_result is not None and direct_result["action"] == "created", "internal full body must share whole-operation serialization"
    assert "job_id" not in harness.observed.responses[control.id].get("data", {}), "bounded control cannot create a job"
    assert_public_jobs(harness, [first, *successors])
    assert harness.job_count() == before_jobs + 3, "three public actions, one job each; direct/control calls create none"
    assert_consistent(harness)
    print("PASS ordinary admissions <50ms, unrelated control progress, serialized internal result and complete chunks", harness.observed.seconds)


def assert_public_jobs(harness: Harness, actions: list[QueuedAction]) -> None:
    for action in actions:
        response = harness.observed.responses[action.id]
        assert response["data"]["status"] == "queued", response
        assert harness.observed.seconds[action.id] < 0.05, (action.process_key, harness.observed.seconds[action.id])
        identity = response["data"]["job_id"]
        job = harness.jobs.get_job(identity)["data"]["result"]["job"]
        assert job["status"] == "completed", job
        assert_job_lineage(harness, job, action)
        request = response_data(harness.manager.get_job_payload(identity, "request"))["payload"]
        assert len(json.dumps(request)) < 1024, "large public content must stay out of the durable descriptor"
        assert harness.observed.notifications.count((action.id, "completed")) == 1


def assert_job_lineage(harness: Harness, job: dict[str, Any], action: QueuedAction) -> None:
    metadata = json.loads(job["metadata"]) if isinstance(job["metadata"], str) else job["metadata"]
    assert job["flow_token_id"] == action.flow_token_id and job["group_id"] == f"registered:{action.id}", job
    assert metadata["registered_action_id"] == action.id and metadata["flow_id"] == action.flow_id and metadata["session_id"] == harness.submission.session, metadata


def assert_consistent(harness: Harness) -> None:
    record = response_data(harness.state.read_state(PLUGIN_NAME, {"table": TABLE_KNOWLEDGE_INSTALL, "filters": {"name": "owned"}, "limit": 1}))["records"][0]
    expected = {identity for identity, row in harness.memory.store.items() if kb_id_tag("owned") in row["tags"]}
    assert set(parse_memory_ids(record["memory_ids"])) == expected and record["chunk_count"] == len(expected), (record, expected)


def file_lifecycle(harness: Harness, directory: Path) -> None:
    read = harness.final("read_file", {"name": "owned", "path": "a.md"})["result"]
    assert "Final A." in read["content"] and read["content_sha256"]
    assert harness.service.read_file("owned", "a.md") == read, "in-process reader returns content/hash, never a job"
    prior_snapshots = set((directory / ".doc_history/a.md").glob("*.md"))
    harness.final("edit_file", {"name": "owned", "path": "a.md", "content": "# A\n\nCorrected.\n", "expected_content_hash": read["content_sha256"]})
    snapshots = sorted(set((directory / ".doc_history/a.md").glob("*.md")) - prior_snapshots)
    assert len(snapshots) == 1 and snapshots[0].read_text() == read["content"]
    current = harness.service.read_file("owned", "a.md")
    stale = harness.final("edit_file", {"name": "owned", "path": "a.md", "content": "stale must not write", "expected_content_hash": read["content_sha256"]}, status="error")
    assert "precondition failed" in json.dumps(stale["error"]) and harness.service.read_file("owned", "a.md") == current
    historical = harness.final("read_file", {"name": "owned", "path": str(snapshots[0].relative_to(directory))})["result"]
    assert historical["content"] == read["content"]
    harness.final("edit_file", {"name": "owned", "path": "a.md", "content": historical["content"], "expected_content_hash": current["content_sha256"]})
    assert snapshots[0].read_text() == read["content"], "reversal cannot rewrite immutable history"
    harness.final("browse", {"name": "owned"})
    archived = harness.final("archive_file", {"name": "owned", "path": "a.md", "superseded_by": "public.md"})["result"]
    assert not (directory / "a.md").exists() and "Superseded_by: public.md" in (directory / archived["archived_path"]).read_text()
    harness.final("delete_file", {"name": "owned", "path": "public.md"})
    assert not (directory / "public.md").exists()
    assert_consistent(harness)


def retrieval_lifecycle(harness: Harness) -> None:
    for name in ("search", "search_planning_references"):
        result = harness.final(name, {"query": "Direct", "top_k": 8})["result"]
        assert result["data"]["results"], result
    report = harness.final("test_retrieval", {"article_path": "knowledge_bases/owned/direct.md", "target_queries": ["Direct"], "forbidden_queries": []})["result"]
    assert report["data"]["passed"], report
    count = harness.job_count()
    harness.service.deactivate("owned")
    assert harness.service.search("Direct")["data"]["count"] == 0
    harness.service.activate("owned")
    assert harness.job_count() == count, "bounded activation controls and direct search admit no jobs"


def installation_lifecycle(harness: Harness) -> None:
    orphan = harness.memory.remember("orphan", [TAG_DOMAIN_OFFICIAL])["memory_id"]
    dry = harness.final("purge_orphaned_chunks", {})["result"]
    assert dry["orphan_count"] == 1 and orphan in harness.memory.store
    harness.final("purge_orphaned_chunks", {"confirm": True, "batch_size": 1, "max_batches": 1})
    assert orphan not in harness.memory.store
    fresh = make_kb(harness, "fresh")
    harness.final("install", {"name": "fresh"})
    harness.final("ingest", {"name": "all"})
    harness.final("uninstall", {"name": "fresh"})
    assert fresh.exists(), "local uninstall must preserve the source"
    harness.final("install", {"name": "fresh"})
    print("PASS file/CAS/stale/history/reversal/archive/delete, retrieval aliases, install/ingest/uninstall/reinstall and orphan confirmation")


def refresh_and_controls(harness: Harness) -> None:
    directory = harness.root.parent / "ananta/knowledge_base/processes/knowledge_service"
    directory.mkdir(parents=True)
    (directory / "read_file.json").write_text(json.dumps({"process_key": REFRESHED_KEY, "description": "Owned refresh"}))
    singular = harness.final("refresh_plugin_process", {"plugin_name": "ananta", "process_key": REFRESHED_KEY})["result"]
    assert singular["refreshed_process_key"] == REFRESHED_KEY and singular["updated"] is True
    plural = harness.final("refresh_plugin_processes", {"plugin_name": "ananta"})["result"]
    assert plural["updated_count"] == 1
    before = harness.job_count()
    with patch.object(harness.plugin._kb_audit_executor, "submit", return_value=True):
        action = harness.action("audit_retrieval_corpus", {})
        result = harness.dispatch(action)
        assert result["data"]["audit"] == "started" and "job_id" not in result["data"]
    assert harness.job_count() == before
    print("PASS singular/all refresh and no-new-job audit control")


def terminal_failures(harness: Harness) -> None:
    from default_knowledge_plugin import _file_ops_plugin_mixin as mixin

    with patch.object(mixin, "read_file_kb", side_effect=FrameworkError("owned typed failure", error_code="knowledge.owned_fixture_failure")):
        job = harness.final("read_file", {"name": "owned", "path": "direct.md"}, status="error")
    assert job["error"]["code"] == "knowledge.owned_fixture_failure" and job["result"]["error"] == job["error"]
    with patch.object(mixin, "read_file_kb", side_effect=SystemExit("owned worker loss")):
        lost = harness.final("read_file", {"name": "owned", "path": "direct.md"}, status="error")
    assert lost["error"]["code"] == "registered_owner.worker_lost"
    harness.final("read_file", {"name": "owned", "path": "direct.md"})
    calls: list[str] = []
    original = harness.manager.update_job

    def refuse_terminal(identity: str, updates: dict[str, object]) -> dict[str, object]:
        if updates.get("status") == "completed":
            calls.append(identity)
            return {"action_status": "error", "error": {"code": "owned.terminal_refusal"}}
        return original(identity, updates)

    action = harness.action("create_file", {"name": "owned", "path": "uncertain.md", "content": "# Once\n\nOne effect.\n"})
    with patch.object(harness.manager, "update_job", side_effect=refuse_terminal):
        receipt = harness.dispatch(action)
        harness.join()
    identity = receipt["data"]["job_id"]
    uncertain = harness.jobs.get_job(identity)["data"]["result"]["job"]
    assert calls == [identity] and uncertain["status"] == "processing" and uncertain["result"] is None
    assert (harness.root / "owned/uncertain.md").read_text() == "# Once\n\nOne effect.\n"
    assert not any(row[0] == action.id for row in harness.observed.notifications)
    print("PASS typed terminal errors, worker loss/replacement and visible uncertain terminal write without replay")


def main() -> int:
    if not os.environ.get(MANIFEST_ENV):
        declarations()
        return skip_fixture()
    with ledger_fixture("fast_return_census", schema="knowledge_admission_fixture") as fixture:
        harness = Harness(fixture)
        fixture_spec = importlib.util.spec_from_file_location("secrets_output_boundaries_smoke", ROOT / "ananta/tests/secrets_output_boundaries_smoke.py")
        assert fixture_spec is not None and fixture_spec.loader is not None
        fixture_module = importlib.util.module_from_spec(fixture_spec)
        fixture_spec.loader.exec_module(fixture_module)
        fixture_module.registered_git_failure(harness)
        directory = make_kb(harness, "owned")
        installed = harness.service.install("owned")
        assert installed["chunk_count"] == 1, installed
        asyncio.run(ordered_admission(harness, directory))
        file_lifecycle(harness, directory)
        retrieval_lifecycle(harness)
        installation_lifecycle(harness)
        refresh_and_controls(harness)
        terminal_failures(harness)
        declarations()
        assert OPERATIONS <= {verb for verb, _identity in harness.calls} | {"create_file", "edit_file", "update"}
        print("ADVISORY optional git/source credentials and native embeddings absent from this owned fixture; existing git and retrieval fixtures retain their separate coverage")
    print("PASS Knowledge registered admission and serialized activity lifecycle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
