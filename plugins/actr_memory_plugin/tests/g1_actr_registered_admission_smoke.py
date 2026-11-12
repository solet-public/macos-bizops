"""ACTR admission through the real registry, factory, poller and private state.

The private-PG leg uses the existing disposable census service graph and owned
embedding/vector doubles. Without its provisioned manifest, only declaration
checks run and the missing behavioral measurement is explicitly reported.
"""

from __future__ import annotations

import asyncio
import faulthandler
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "ananta/src"), str(ROOT / "solet_setup_contracts/src"), str(ROOT / "quality_gates/tests"), *(str(path) for path in sorted(ROOT.glob("plugins/*/src")))]

from actr_memory_plugin import backend as memory_backend  # noqa: E402
from actr_memory_plugin.async_jobs import REGISTERED_MEMORY_OPERATIONS as SELECTED  # noqa: E402
from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.orchestration.managers.action_event_recorder import ActionEventRecorder  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.services.compilation_context_builder import CompilationContextBuilder  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from ananta.platform.new_template_engine import NewTemplateEngine  # noqa: E402
from ananta.platform.unified_metadata_registry import UnifiedMetadataRegistry  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402

if TYPE_CHECKING:
    from quality_gates.tests.fast_return_census_context import CensusContext

SYNCHRONOUS = frozenset({"export_memories", "upsert_memory_by_tag"})


class Embeddings:
    """An owned effect leaf that can park or fail without external transport."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.inputs: list[str] = []

    def generate_embeddings(self, inputs: list[str], **_options: Any) -> dict[str, Any]:
        self.inputs.extend(inputs)
        self.entered.set()
        assert self.release.wait(5), "owned embedding leaf was not released"
        return {"action_status": "completed", "data": {"result": {"embeddings": [[1.0, 0.0] for _ in inputs]}}}

    def input_token_budget(self) -> None:
        return None


class Vectors:
    """Owned vector effects; memory rows and every job remain real PostgreSQL."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def store_vectors(self, namespace: str, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        del namespace
        for vector in vectors:
            self.rows[str(vector["external_id"])] = vector
        return {"action_status": "completed", "data": {"result": {"stored_count": len(vectors)}}}

    def search_similar(self, **_options: Any) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"result": {"results": [{"external_id": key, "distance": 0.0} for key in self.rows]}}}

    def delete_by_external_ids(self, namespace: str, external_ids: list[str]) -> dict[str, Any]:
        del namespace
        for key in external_ids:
            self.rows.pop(key, None)
        return {"action_status": "completed", "data": {"result": {"deleted_count": len(external_ids)}}}

    def find_missing_external_ids(self, namespace: str, candidate_external_ids: list[str]) -> dict[str, Any]:
        del namespace
        return {"action_status": "completed", "data": {"result": {"missing": [key for key in candidate_external_ids if key not in self.rows]}}}

    def get_namespace_stats(self, namespace: str) -> dict[str, Any]:
        del namespace
        return {"action_status": "completed", "data": {"result": {"vector_count": len(self.rows)}}}


class TimedProcessor(ActionProcessor):
    """Measure the actual handler invocation, excluding factory/claim setup."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.responses: dict[str, dict[str, object]] = {}
        self.elapsed: dict[str, float] = {}

    def execute_action(self, action: Any) -> dict[str, object]:
        started = time.perf_counter()
        result = super().execute_action(action)
        self.elapsed[action.id] = time.perf_counter() - started
        self.responses[action.id] = result
        return result


class Dispatch:
    """Real bridge/factory/poller components, separate from memory effects."""

    def __init__(self, context: CensusContext) -> None:
        from agent_messaging_plugin.models import BridgeSessionState
        from agent_messaging_plugin.platform_surface import PlatformSurface
        from quality_gates.tests.fast_return_census_context import _platform_registry

        self.state: Any = context.state
        self.graph = FlowRuntimeGraph(self.state)
        self.manager = AsyncJobManager(self.state, self.graph)
        context.services["async_job_manager"] = self.manager
        self.flow_manager = FlowManager(self.state)
        self.recorder = ActionEventRecorder(self.state, self.flow_manager, self.graph)
        self.registry = _platform_registry(context)
        metadata = UnifiedMetadataRegistry(str(ROOT / "ananta/src/ananta/platform/metadata"), str(ROOT / "plugins"), str(context.app_home))
        engine = NewTemplateEngine(metadata, state_service=self.state)
        self.factory = ActionFactory(self.registry, template_engine=engine, state_service=self.state, action_event_recorder=self.recorder, app_home=str(context.app_home))
        self.manager.set_action_factory(self.factory)
        self.reader = JobService(self.state, self.manager)
        context.services["job_service"] = self.reader
        context.wire(context.registry.plugins["actr_memory_plugin"])
        self.contexts = ExecutionContextManager()
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.registry["processes"]
        orchestrator: Any = SimpleNamespace(APP_HOME=str(context.app_home), async_job_manager=self.manager, action_recorder=self.recorder, get_service=context.get_service)
        self.processor = TimedProcessor(context.registry, state_service=self.state, orchestrator=orchestrator, process_registry=self.registry, execution_context_manager=self.contexts, discovery_service=discovery)
        self.poller = ActionQueuePoller(self.state, cast(Any, self.processor), self.graph, cast(Any, self.factory), execution_context_manager=self.contexts, blob_storage_service=context.services["blob_storage_service"], discovery_service=discovery, app_home=str(context.app_home), max_actions_per_poll=1)
        self.session = self._write_id("sessions", {"context_type": "owned-memory-smoke"})
        self.bridge = BridgeSessionState("owned-memory-smoke", self.session)
        self.surface = PlatformSurface(action_factory=self.factory, flow_manager=self.flow_manager, compilation_context_builder=CompilationContextBuilder(), bridge_manager=cast(Any, None), process_registry=self.registry, state_service=self.state)

    def _write_id(self, table: str, record: dict[str, Any]) -> str:
        result = self.state.write_state("core", {"table": table, "record": record})
        assert result["action_status"] == "completed", result
        return str(result["data"]["result"]["generated_id"])

    def dispatch(self, method: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
        submitted = self.surface._submit_process_call(bridge=self.bridge, process_key=f"service_interface::memory_service::{method}", arguments=arguments, reason="owned memory admission proof", deliver_to_bridge=False)
        action_id = submitted["action_id"]
        asyncio.run(self.poller._poll_once())
        response = self.processor.responses[action_id]
        assert response.get("success") is True, (method, response)
        return cast(dict[str, Any], response), self.processor.elapsed[action_id]

    def job(self, job_id: str) -> dict[str, Any]:
        result = self.reader.get_job(job_id)
        assert result["action_status"] == "completed", result
        return cast(dict[str, Any], result["data"]["result"]["job"])


class Harness:
    """Owned memory leaves over a production dispatch and private-state graph."""

    def __init__(self, context: CensusContext) -> None:
        self.context = context
        self.state: Any = context.state
        self.memory: Any = context.services["memory_service"]
        self.plugin: Any = context.registry.plugins["actr_memory_plugin"]
        self.backend: Any = self.plugin._backend
        self.embeddings, self.vectors = Embeddings(), Vectors()
        self.backend.embedding_service, self.backend.vector_service = self.embeddings, self.vectors
        with patch.object(self.plugin.orchestrator_ref, "async_job_manager", None):
            bootstrap = self.memory.remember(content="synchronous startup body", embed=False)
        assert bootstrap["memory_id"] and "job_id" not in bootstrap
        print("PASS full synchronous body before startup initializes AsyncJobManager")
        self.runtime = Dispatch(context)
        self.covered: set[str] = set()

    def dispatch(self, method: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], float]:
        response, elapsed = self.runtime.dispatch(method, arguments)
        if method in SELECTED:
            self.covered.add(method)
        return response, elapsed

    def terminal(self, response: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
        job_id = response["data"]["job_id"]
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            job = self.runtime.job(job_id)
            if job["status"] in {"completed", "error"}:
                assert job["status"] == ("error" if error else "completed"), job
                assert isinstance(job["result"], dict), job
                if error:
                    assert job["error"], job
                return job
            time.sleep(0.005)
        raise AssertionError(f"owned memory job did not terminate: {job_id}")

    def run(self, method: str, arguments: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
        response, elapsed = self.dispatch(method, arguments)
        assert elapsed < 0.050, (method, elapsed)
        assert response["data"]["status"] == "queued" and response["timestamp"], response
        return self.terminal(response, error=error)


def parked_admission(harness: Harness) -> str:
    """The baseline must reach the real embedding leaf before this RED."""
    embeddings = harness.embeddings
    embeddings.entered.clear()
    embeddings.release.clear()
    timer = threading.Timer(0.6, embeddings.release.set)
    timer.start()
    try:
        response, elapsed = harness.dispatch("remember", {"content": "first owned memory", "tags": ["owned-family"]})
        print(json.dumps({"remember_handler_ms": elapsed * 1000, "embedding_leaf_reached": embeddings.entered.is_set(), "response": response}), flush=True)
        assert elapsed < 0.050, "real remember handler waited on the parked embedding leaf"
        assert embeddings.entered.wait(1), "the actual bound memory embedding leaf was not reached"
        assert not embeddings.release.is_set(), "fixture released before the admission proof"
        job_id = response["data"]["job_id"]
        assert harness.runtime.job(job_id)["status"] == "processing"
        request = cast(dict[str, Any], harness.runtime.manager.get_job_payload(job_id, "request"))["data"]["payload"]
        assert len(json.dumps(request)) < 512 and "first owned memory" not in json.dumps(request), request
        ordinary, ordinary_elapsed = harness.dispatch("store_interaction", {"session_id": harness.runtime.session, "source_namespace": "owned", "event_type": "user_input", "content": "ordinary action during embedding"})
        assert ordinary.get("data", {}).get("event_id") and ordinary_elapsed < 0.050
        second, _ = harness.dispatch("remember", {"content": "second owned memory", "tags": ["owned-family"], "embed": False})
        assert harness.runtime.job(second["data"]["job_id"])["status"] == "queued", "second admission must retain FIFO order"
    finally:
        embeddings.release.set()
        timer.cancel()
        timer.join()
    first = harness.terminal(response)
    harness.terminal(second)
    assert first["group_id"].startswith("registered:") and first["flow_token_id"]
    assert first["result"]["memory_id"]
    print("PASS parked real remember, persisted bounded request, token lineage, ordinary dispatch and FIFO")
    return str(first["result"]["memory_id"])


def strength_race(harness: Harness, memory_id: str) -> None:
    """A recompute snapshot cannot undo a normal reinforcement of that row."""
    entered, release = threading.Event(), threading.Event()
    original = memory_backend.get_all_memories
    seeded = harness.state.update_state(memory_backend.NAMESPACE, {"table": "memory", "filters": {"id": memory_id}}, {"strength": 999.0})
    assert seeded["action_status"] == "completed"

    def parked(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        rows = original(*args, **kwargs)
        entered.set()
        assert release.wait(5), "owned recompute snapshot was not released"
        return rows

    with patch.object(memory_backend, "get_all_memories", parked):
        response, elapsed = harness.dispatch("recompute_strengths", {})
        assert elapsed < 0.050 and entered.wait(1)
        try:
            reinforced, _ = harness.dispatch("reinforce", {"memory_id": memory_id})
            focused, _ = harness.dispatch("focus", {"memory_id": memory_id})
            assert not focused.get("error"), focused
            interaction, _ = harness.dispatch("store_interaction", {"session_id": harness.runtime.session, "source_namespace": "owned", "event_type": "user_input", "content": "ordinary action during recompute"})
            assert interaction["data"]["event_id"]
            before = memory_backend.get_memory(harness.state, memory_id)
            assert before and before["retrieval_count"] == reinforced["retrieval_count"]
        finally:
            release.set()
        harness.terminal(response)
    after = memory_backend.get_memory(harness.state, memory_id)
    assert after and before
    assert after["retrieval_count"] == before["retrieval_count"] and after["retrieval_times"] == before["retrieval_times"], "strength recompute overwrote a concurrent ordinary reinforcement"
    assert after["tags"] == before["tags"] and after["status"] == before["status"]
    print("PASS strength-only update preserves concurrent retrieval history and unrelated fields")


def parked_recall_and_direct_order(harness: Harness) -> None:
    """A parked real recall admits both plugin jobs and full direct bodies in order."""
    from quality_gates.tests.fast_return_census_context import RecordingJobs

    embeddings = harness.embeddings
    embeddings.entered.clear()
    embeddings.release.clear()
    done = threading.Event()
    direct: list[dict[str, Any]] = []
    entries: list[str] = []
    remember = harness.backend.remember

    def observed(**arguments: Any) -> dict[str, Any]:
        entries.append(arguments["content"])
        return remember(**arguments)

    def synchronous() -> None:
        direct.append(harness.memory.remember(content="direct FIFO body", embed=False))
        done.set()

    thread = threading.Thread(target=synchronous)
    timer = threading.Timer(0.6, embeddings.release.set)
    timer.start()
    with patch.object(harness.backend, "remember", observed):
        try:
            response, elapsed = harness.dispatch("recall", {"query": "park real recall"})
            assert elapsed < 0.050 and embeddings.entered.wait(1) and not embeddings.release.is_set()
            recording = RecordingJobs(harness.runtime.manager)
            lineage = json.loads(harness.runtime.job(response["data"]["job_id"])["metadata"])
            with patch.object(harness.plugin.orchestrator_ref, "async_job_manager", recording):
                # Bound providers are deliberately omitted from plugin:: by
                # PluginProcessScanner._should_skip_plugin. This separate
                # internal-producer proof calls its existing real handler.
                plugin_job = harness.plugin.remember_action({"content": "plugin FIFO body"}, {"session_id": harness.runtime.session, "flow_id": lineage["flow_id"]})
            assert len(recording.jobs) == 1 and recording.jobs[0]["job_id"] == plugin_job["data"]["job_id"]
            thread.start()
            assert not done.wait(0.02), "direct writer escaped the shared memory owner"
            ordinary, ordinary_elapsed = harness.dispatch("store_interaction", {"session_id": harness.runtime.session, "source_namespace": "owned", "event_type": "user_input", "content": "ordinary action during recall"})
            assert ordinary["data"]["event_id"] and ordinary_elapsed < 0.050
        finally:
            embeddings.release.set()
            timer.cancel()
            timer.join()
            if thread.ident is not None:
                thread.join(5)
        recall = harness.terminal(response)
        harness.terminal(plugin_job)
    assert recall["result"]["memories"] and done.is_set() and direct[0]["memory_id"] and "job_id" not in direct[0]
    assert entries == ["plugin FIFO body", "direct FIFO body"], entries
    print("PASS parked actual recall, ordinary dispatch, shared plugin/direct FIFO and complete synchronous body")


def family(harness: Harness, scratch: Path, memory_id: str) -> None:
    """Use valid owned bodies, including nested memory calls and all cron twins."""
    file_path = scratch / "export.json"
    exported, _ = harness.dispatch("export_memories", {"file_path": str(file_path)})
    assert exported["memory_count"] >= 2 and file_path.is_file() and "job_id" not in exported
    replacement, _ = harness.dispatch("upsert_memory_by_tag", {"content": "replacement one", "tag": "owned-slot", "tags": ["owned-family"]})
    replacement_id = replacement["data"]["memory_id"]
    corrected, _ = harness.dispatch("upsert_memory_by_tag", {"content": "replacement corrected", "tag": "owned-slot", "tags": ["owned-family"]})
    assert corrected["data"]["memory_id"] != replacement_id and memory_backend.get_memory(harness.state, replacement_id) is None
    imported = json.loads(file_path.read_text())
    imported["memories"][0]["id"] = "owned-import-record"
    file_path.write_text(json.dumps(imported))
    learned = scratch / "learned.json"
    learned.write_text(json.dumps({"content": "owned learn body"}))
    target = harness.memory.remember(content="memorize target", tags=["owned-memorize"], embed=False)["memory_id"]
    doomed = harness.memory.remember(content="delete target", tags=["owned-delete"], embed=False)["memory_id"]
    cases: dict[str, dict[str, Any]] = {
        "audit_pinned_notes": {},
        "cleanup_orphaned_vectors": {"dry_run": True},
        "consolidate": {"dry_run": True},
        "consolidate_cron": {"dry_run": True},
        "get_memories_by_tag": {"tag": "owned-family"},
        "get_recent_memory": {"session_id": harness.runtime.session},
        "get_recent_memory_structured": {"session_id": harness.runtime.session},
        "import_memories": {"file_path": str(file_path), "regenerate_embeddings": False},
        "ingest_session": {"transcript": "User: owned transcript\nAssistant: owned response", "session_id": harness.runtime.session},
        "learn": {"path": str(learned)},
        "list_memories": {},
        "list_memorizing": {},
        "memorize": {"content": "owned nested remember"},
        "memorize_by_tag": {"tag": "owned-memorize"},
        "memory_stats": {},
        "process_memorization_queue": {},
        "process_memorization_queue_cron": {},
        "recall": {"query": "owned"},
        "recompute_strengths_cron": {},
        "reindex_orphaned_vectors": {},
        "review_blocked_intents": {},
        "store_compaction_summary": {"context_id": "owned-context", "summary": "owned compaction", "compacted_event_count": 2, "session_id": harness.runtime.session},
        "forget": {"memory_id": memory_id},
        "delete_memories_by_ids": {"ids": [doomed]},
        "delete_memories_by_tag": {"tag": "owned-slot"},
    }
    for method, arguments in cases.items():
        family_case(harness, method, arguments)
    refusal = harness.run("purge_memories", {"confirm": False}, error=True)
    assert refusal["error"] and memory_backend.get_memory(harness.state, target)
    assert memory_backend.get_memory(harness.state, doomed) is None
    archived = memory_backend.get_memory(harness.state, memory_id)
    assert archived and archived["status"] == "archived"
    assert harness.covered == SELECTED, (SELECTED - harness.covered, harness.covered - SELECTED)
    print("PASS all 28 registered methods, three cron pairs, synchronous export/upsert, nested bodies, tag correction and terminal deletion")


def family_case(harness: Harness, method: str, arguments: dict[str, Any]) -> None:
    job = harness.run(method, arguments)
    if method == "reindex_orphaned_vectors":
        assert job["result"]["data"]["reindex_count"] > 0 and not job["result"]["data"]["failed_records"]
    if method in {"learn", "ingest_session"}:
        assert job["result"]["memories_created"] > 0, (method, job)


def uncertain_outcomes(harness: Harness) -> None:
    """A worker loss or uncertain terminal write cannot replay a memory effect."""
    calls: list[str] = []
    original = harness.backend.remember

    def lost(**_arguments: Any) -> dict[str, Any]:
        calls.append("lost")
        raise SystemExit("owned worker loss")

    with patch.object(harness.backend, "remember", lost):
        failed = harness.run("remember", {"content": "owned lost request"}, error=True)
    assert failed["error"]["code"] == "registered_owner.worker_lost" and calls == ["lost"]
    followup = harness.run("remember", {"content": "after owned loss", "embed": False})
    assert followup["result"]["memory_id"] and calls == ["lost"]
    stale = harness.runtime.manager.update_job(failed["id"], {"status": "completed", "result": {"incorrect": True}})
    assert stale["action_status"] == "error" and harness.runtime.job(failed["id"])["error"] == failed["error"]
    terminal = harness.runtime.manager.update_job

    def uncertain(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        if updates.get("status") == "completed":
            return {"action_status": "error", "error": {"code": "owned-terminal-write-failure"}}
        return terminal(job_id, updates)

    def effect(**arguments: Any) -> dict[str, Any]:
        calls.append("uncertain")
        return original(**arguments)

    with patch.object(harness.runtime.manager, "update_job", uncertain), patch.object(harness.backend, "remember", effect):
        response, _ = harness.dispatch("remember", {"content": "owned uncertain effect", "embed": False})
        worker = harness.runtime.manager._owners["actr_memory_plugin"]._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()
        assert calls == ["lost", "uncertain"]
        assert harness.runtime.job(response["data"]["job_id"])["status"] == "processing"
    reconciled = terminal(response["data"]["job_id"], {"status": "error", "error": {"code": "owned-reconciliation", "message": "effect observed; never replay"}, "result": {"reconciliation_required": True}})
    # This owned graph has no inference recovery template. update_job reports
    # that notification error separately from its atomic outcome commit.
    assert cast(dict[str, Any], reconciled["data"])["outcome_committed"] and calls == ["lost", "uncertain"]
    durable = harness.runtime.job(response["data"]["job_id"])
    assert durable["status"] == "error" and durable["error"]["code"] == "owned-reconciliation"
    print(f"ADVISORY fixture recovery notification={reconciled['action_status']}; exact committed error retained; inference recovery delivery unmeasured")
    print("PASS worker loss, replacement, stale terminal refusal and uncertain persistence without replay")


def notification_failure(harness: Harness) -> None:
    """A failed completion callback cannot erase or repeat a committed effect."""
    entered: list[str] = []
    remember = harness.backend.remember

    def effect(**arguments: Any) -> dict[str, Any]:
        entered.append(arguments["content"])
        return remember(**arguments)

    with patch.object(harness.runtime.manager, "_registered_completion", side_effect=RuntimeError("owned notification failure")), patch.object(harness.backend, "remember", effect):
        job = harness.run("remember", {"content": "committed despite notification failure", "embed": False})
        worker = harness.runtime.manager._owners["actr_memory_plugin"]._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive()
    assert entered == ["committed despite notification failure"] and job["result"]["memory_id"]
    assert harness.runtime.job(job["id"])["result"] == job["result"]
    print("PASS notification failure preserves committed full outcome without replay")


def declarations() -> None:
    """The selected public contracts describe receipts and retain both exceptions."""
    from ananta.core.actions.action_processor import _REGISTERED_JOB_OPERATIONS
    from ananta.services.memory_service.interfaces.public import MemoryServiceAPI

    assert len(SELECTED) == 28 and _REGISTERED_JOB_OPERATIONS["memory_service"] == SELECTED
    for method in SELECTED | SYNCHRONOUS:
        metadata = getattr(MemoryServiceAPI, method)._service_interface_metadata
        definition = metadata.to_process_dict()
        document = json.loads((ROOT / "ananta/knowledge_base/processes/memory_service" / f"{method}.json").read_text())
        assert definition["is_async"] is (method in SELECTED), method
        if method in SELECTED:
            assert set(definition["return_value_schema"]["properties"]) == {"job_id", "status"}, method
            assert document["is_async"] is True, method
    print("PASS 28 exact declarations and two explicit synchronous consumer exceptions")


def fixture_import_paths() -> None:
    """Match the existing census fixture's physical source/library boundary."""
    from quality_gates.tests.fast_return_census_fixtures import library_roots

    allowed = (ROOT, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), *library_roots())
    sys.path[:] = [value for value in sys.path if any(Path(value).resolve().is_relative_to(base) for base in allowed)]


def main() -> int:
    if not (ROOT / "quality_gates/private_test_pg.py").is_file():
        declarations()
        print("SKIP private-PG real ACTR dispatch: optional checkout fixture helpers absent")
        return 0
    from quality_gates.private_test_pg import MissingFixtureError, load_fixture
    from quality_gates.tests.fast_return_census import Probe, Settings
    from quality_gates.tests.fast_return_census_context import plugin_context

    faulthandler.dump_traceback_later(20)

    fixture_import_paths()
    try:
        load_fixture("fast_return_census")
    except MissingFixtureError:
        declarations()
        print("SKIP private-PG real ACTR dispatch: provision_test_pg.py --exec manifest absent; declaration checks only")
        return 0
    optional = [name for name in ("openai_embeddings_plugin", "pgvector_service_plugin", "default_address_book_plugin", "default_blob_storage_plugin", "default_scheduling_plugin", "agent_messaging_plugin") if not (ROOT / "plugins" / name / "src" / name).is_dir()]
    if optional:
        declarations()
        print(f"SKIP private-PG owned service graph: optional fixture providers absent: {optional}")
        return 0
    # The existing census guard grants this exact F5 executor by module name.
    # Load it before guard_options() snapshots available executor names.
    from default_scheduling_plugin.plugin import SchedulingPlugin

    assert SchedulingPlugin.__module__ == "default_scheduling_plugin.plugin"
    with tempfile.TemporaryDirectory(prefix="g1-actr-admission-", dir=ROOT / "workbench") as directory:
        scratch = Path(directory).resolve()
        with plugin_context(ROOT, scratch, "actr_memory_plugin", ACTRMemoryPlugin, Probe(Settings()), []) as context:
            faulthandler.cancel_dump_traceback_later()
            harness = Harness(context)
            memory_id = parked_admission(harness)
            parked_recall_and_direct_order(harness)
            strength_race(harness, memory_id)
            from actr_memory_consolidation_kb_exclusion_smoke import verify_consolidation_effects

            verify_consolidation_effects(harness)
            family(harness, context.export_root, memory_id)
            uncertain_outcomes(harness)
            notification_failure(harness)
            declarations()
    assert not scratch.exists()
    print("PASS owned fixture cleanup; private-PG behavioral evidence remains distinct from native scoped check-in")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
