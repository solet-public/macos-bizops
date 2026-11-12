#!/usr/bin/env python3
"""Regression guard: the two async-job failure modes a monitor must distinguish.

Slice 2 of the durable-job-completion work. Both verbs exist because a job can
go wrong in two ways that need OPPOSITE handling, and one mechanism cannot
serve both:

  * NEVER FINISHED — a worker died mid-run, the row sits in ``processing``
    forever, the flow never closes. ``sweep_stale_jobs`` terminates it through
    ``AsyncJobManager.update_job``, so the error continuation fires and the FRG
    token resolves.
  * FINISHED, CONTINUATION FAILED — ``update_job`` writes the terminal status
    BEFORE the completion handler runs and before the token resolves, so a
    raising handler leaves a row reading ``completed`` while nothing was
    submitted and the token stayed open. That row does not look stuck, so the
    sweep cannot find it. ``detect_unresolved_completion_tokens`` reports it and
    changes NOTHING — resolving here would fabricate a completion.

Cases, each naming the mutation that reds it:

  1. The sweep terminates only rows past the window, through the manager's own
     update path. (Mutation: call ``_update_ledger``-style direct write, or
     drop the ``updated_at`` cutoff filter -> PASS to FAIL.)
  2. The sweep refuses ``queued`` rows — a queued job is a backlog, not a
     death. (Mutation: add ``queued`` to the sweepable statuses -> PASS to FAIL.)
  3. The recorded status_reason NAMES the sweep, so a swept job is never
     readable as a worker-reported failure. (Mutation: drop the prefix from the
     reason string -> PASS to FAIL.)
  4. The sweep REFUSES to run with no job manager rather than writing a
     terminal status directly. (Mutation: fall back to a direct state write
     -> PASS to FAIL.)
  5. max_age_minutes is required and validated; limit is bounded. (Mutation:
     default the window or clamp the limit -> PASS to FAIL.)
  6. The detector reports a terminal row whose token is non-terminal. (Mutation:
     drop the ``_token_is_unresolved`` predicate -> PASS to FAIL.)
  7. The detector does NOT report a terminal row whose token resolved, and does
     NOT report one whose token row is missing (absent evidence is not evidence
     of the defect). (Mutation: treat a missing token row as unresolved ->
     PASS to FAIL.)
  8. The detector mutates nothing. (Mutation: have it resolve tokens -> PASS to
     FAIL.)

Rail: the fakes track StateServiceProtocol's and AsyncJobManager's real
signatures, so drift reds this smoke rather than vacating it.

Project policy: no pytest. Exits 0 on success, 1 on first failure.

Run:

    .venv/bin/python3 ananta/tests/core/services/job_service_lifecycle_smoke.py
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT = REPO_ROOT
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT))
for plugin_name in ("postgres_state_management_plugin", "agent_messaging_plugin"):
    sys.path.insert(0, str(REPO_ROOT / "plugins" / plugin_name / "src"))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.execution_token_context import action_execution_context  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from ananta.interfaces.state_service_protocol import StateServiceProtocol  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402

_failures: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _failures.append(message)


def _ok(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {"action_status": "completed", "data": {"records": records}, "actions": []}


class _FakeStateService:
    """Serves job / job_payload / flow_tokens rows with real filter semantics.

    Implements the subset of the filter grammar these verbs actually use —
    scalar equality, list (``= ANY``), and the ``{"op": "lt"}`` range — because
    a fake that ignored the cutoff would let a sweep-everything bug pass.
    """

    def __init__(
        self,
        jobs: list[dict[str, Any]] | None = None,
        tokens: list[dict[str, Any]] | None = None,
    ) -> None:
        self.jobs = jobs or []
        self.tokens = tokens or []
        self.payloads: list[dict[str, Any]] = []
        self.update_state_calls: list[dict[str, Any]] = []

    @staticmethod
    def _matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for column, expected in filters.items():
            actual = row.get(column)
            if isinstance(expected, dict) and expected.get("op") == "lt":
                if actual is None or not actual < expected["value"]:
                    return False
            elif isinstance(expected, list):
                if actual not in expected:
                    return False
            elif actual != expected:
                return False
        return True

    def _rows_for(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        table = str(query.get("table"))
        source = {
            "job": self.jobs,
            "job_payload": self.payloads,
            "flow_tokens": self.tokens,
        }.get(table)
        if source is None:
            raise AssertionError(f"unexpected table: {table!r}")
        filters = query.get("filters") or {}
        if not isinstance(filters, dict):
            filters = {}
        return [row for row in source if self._matches(row, filters)]

    def read_state(self, namespace: str, query: dict[str, object]) -> dict[str, object]:
        return _ok(self._rows_for(dict(query)))

    def query_state(self, namespace: str, filters: dict[str, object]) -> dict[str, object]:
        return _ok(self._rows_for(dict(filters)))

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, object]:
        from ananta.services.state_service.ordered_query import apply_ordered_query_in_memory, parse_ordered_query

        del namespace
        spec = parse_ordered_query(data)
        source = {"job": self.jobs, "job_payload": self.payloads, "flow_tokens": self.tokens}[spec.table]
        return _ok(apply_ordered_query_in_memory(source, spec))

    def write_state(
        self,
        namespace: str,
        data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> dict[str, object]:
        return {"action_status": "completed"}

    def update_state(self, namespace: str, query: dict[str, object], updates: dict[str, object]) -> dict[str, object]:
        self.update_state_calls.append({"query": dict(query), "updates": dict(updates)})
        filters = query.get("filters") or {}
        if isinstance(filters, dict):
            for row in self._rows_for({"table": query.get("table"), "filters": filters}):
                row.update(updates)
        return {"action_status": "completed"}


class _RecordingJobManager:
    """AsyncJobManager stand-in recording terminal transitions."""

    def __init__(self, state: _FakeStateService) -> None:
        self._state = state
        self.update_job_calls: list[tuple[str, dict[str, object]]] = []

    def update_job(self, job_id: str, updates: dict[str, object]) -> dict[str, object]:
        self.update_job_calls.append((job_id, dict(updates)))
        self._state.update_state("core", {"table": "job", "filters": {"id": job_id}}, dict(updates))
        return {"action_status": "completed", "data": {"job_id": job_id, "updated": True}}


def _stamp(minutes_ago: int) -> datetime:
    return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes_ago)


def _job(
    job_id: str,
    *,
    status: str = "processing",
    updated_minutes_ago: int = 60,
    provider: str = "g_suite_plugin.sheets_create_from_files",
    token: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": job_id,
        "provider_name": provider,
        "status": status,
        "created_at": _stamp(updated_minutes_ago + 5),
        "updated_at": _stamp(updated_minutes_ago),
        "metadata": json.dumps({"flow_id": "flow-1"}),
    }
    if token is not None:
        row["flow_token_id"] = token
    return row


def _result(envelope: dict[str, Any], key: str) -> Any:
    data = envelope.get("data")
    assert isinstance(data, dict)
    result = data.get("result")
    assert isinstance(result, dict)
    return result.get(key)


def _service(
    jobs: list[dict[str, Any]],
    tokens: list[dict[str, Any]] | None = None,
    *,
    with_manager: bool = True,
) -> tuple[JobService, _FakeStateService, _RecordingJobManager | None]:
    state = _FakeStateService(jobs=jobs, tokens=tokens)
    manager = _RecordingJobManager(state) if with_manager else None
    svc = JobService(state_service=state, async_job_manager=manager)  # type: ignore[arg-type]
    return svc, state, manager


def test_sweep_terminates_only_stale_processing_rows() -> None:
    """Case 1: past the window is swept; inside the window is left alone."""
    svc, _state, manager = _service(
        [
            _job("job-dead", updated_minutes_ago=90),
            _job("job-live", updated_minutes_ago=2),
        ]
    )
    envelope = svc.sweep_stale_jobs(max_age_minutes=30)
    swept = _result(envelope, "swept")
    ids = [s["job_id"] for s in swept] if isinstance(swept, list) else []
    _check(ids == ["job-dead"], f"only the stale processing job is swept (got {ids})")
    assert manager is not None
    _check(
        [c[0] for c in manager.update_job_calls] == ["job-dead"],
        "the sweep goes through AsyncJobManager.update_job, not a direct ledger write",
    )
    _check(
        bool(manager.update_job_calls) and manager.update_job_calls[0][1].get("status") == "error",
        "the swept job is transitioned to a terminal error status",
    )


def test_sweep_ignores_queued_rows() -> None:
    """Case 2: a queued job is a backlog, not a death."""
    svc, _state, manager = _service([_job("job-queued", status="queued", updated_minutes_ago=999)])
    envelope = svc.sweep_stale_jobs(max_age_minutes=1)
    _check(_result(envelope, "count") == 0, "an ancient QUEUED job is never swept")
    assert manager is not None
    _check(not manager.update_job_calls, "no terminal transition was attempted for it")


def test_swept_reason_names_the_sweep() -> None:
    """Case 3: a swept job must not read as a worker-reported failure."""
    svc, _state, manager = _service([_job("job-dead", updated_minutes_ago=90)])
    svc.sweep_stale_jobs(max_age_minutes=30)
    assert manager is not None
    reason = str(manager.update_job_calls[0][1].get("status_reason", ""))
    _check(
        reason.startswith("swept_stale"),
        f"the recorded reason marks it as swept, not worker-reported (got {reason!r})",
    )
    _check(
        "NOT reported by the worker" in reason,
        "the reason says outright that the worker did not report this failure",
    )


def test_sweep_refuses_without_a_job_manager() -> None:
    """Case 4: refuse rather than write a terminal status behind the manager."""
    svc, state, _ = _service([_job("job-dead", updated_minutes_ago=90)], with_manager=False)
    envelope = svc.sweep_stale_jobs(max_age_minutes=30)
    _check(
        envelope.get("action_status") == "error",
        "the sweep returns an error envelope when no job manager is wired",
    )
    _check(
        not state.update_state_calls,
        "and it writes NOTHING — no direct terminal write behind the manager's back",
    )


def test_sweep_validates_its_bounds() -> None:
    """Case 5: the window is required and the page is bounded."""
    svc, _state, _m = _service([_job("job-dead", updated_minutes_ago=90)])
    for bad in (0, -5):
        _check(
            svc.sweep_stale_jobs(max_age_minutes=bad).get("action_status") == "error",
            f"max_age_minutes={bad} is refused",
        )
    _check(
        svc.sweep_stale_jobs(max_age_minutes=30, limit=101).get("action_status") == "error",
        "limit=101 is refused rather than clamped",
    )


def test_detector_reports_terminal_row_with_open_token() -> None:
    """Case 6: the row that does not look stuck is found."""
    svc, _state, _m = _service(
        [_job("job-ghost", status="completed", token="tok-open")],
        [{"id": "tok-open", "state": "waiting_job"}],
    )
    envelope = svc.detect_unresolved_completion_tokens()
    jobs = _result(envelope, "jobs")
    ids = [j["id"] for j in jobs] if isinstance(jobs, list) else []
    _check(ids == ["job-ghost"], f"the completed-looking, token-open job is reported (got {ids})")


def test_detector_excludes_resolved_and_missing_tokens() -> None:
    """Case 7: resolved tokens and ABSENT token rows are both excluded."""
    svc, _state, _m = _service(
        [
            _job("job-fine", status="completed", token="tok-done"),
            _job("job-no-token-row", status="completed", token="tok-missing"),
        ],
        [{"id": "tok-done", "state": "completed"}],
    )
    envelope = svc.detect_unresolved_completion_tokens()
    _check(
        _result(envelope, "count") == 0,
        "a resolved token is not reported, and a MISSING token row is not either (absent evidence is not evidence of the defect)",
    )


def test_detector_mutates_nothing() -> None:
    """Case 8: read-only — it must never fabricate a completion."""
    svc, state, manager = _service(
        [_job("job-ghost", status="completed", token="tok-open")],
        [{"id": "tok-open", "state": "waiting_job"}],
    )
    svc.detect_unresolved_completion_tokens()
    _check(not state.update_state_calls, "the detector performed no state writes")
    assert manager is not None
    _check(not manager.update_job_calls, "the detector performed no job transitions")


def test_fakes_track_real_signatures() -> None:
    """Rail: protocol/implementation drift must red this smoke."""
    for method_name in ("read_state", "write_state", "update_state", "query_state"):
        real_params = list(inspect.signature(getattr(StateServiceProtocol, method_name)).parameters)
        fake_params = list(inspect.signature(getattr(_FakeStateService, method_name)).parameters)
        _check(
            real_params == fake_params,
            f"_FakeStateService.{method_name} params match StateServiceProtocol's",
        )
    real_update = list(inspect.signature(AsyncJobManager.update_job).parameters)
    fake_update = list(inspect.signature(_RecordingJobManager.update_job).parameters)
    _check(
        real_update == fake_update,
        f"_RecordingJobManager.update_job params {fake_update} match AsyncJobManager's {real_update}",
    )


def native_runtime(fixture: Any) -> tuple[Any, ...]:
    """Build the actual registered runtime on an owned PostgreSQL fixture."""
    from ananta.config.core_schemas import CoreSchemaDefinitions
    from ananta.core.actions.action_factory import ActionFactory
    from ananta.core.event_orchestrator import EventOrchestrator
    from ananta.core.orchestration.execution_context import ExecutionContextManager
    from ananta.core.orchestration.managers.action_event_recorder import ActionEventRecorder
    from ananta.core.process_registry.plugin_process_scanner import PluginProcessScanner
    from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator
    from ananta.core.process_registry.service_interface_metadata_generator import ServiceInterfaceMetadataGenerator
    from ananta.core.services.compilation_context_builder import CompilationContextBuilder
    from ananta.platform.new_template_engine import NewTemplateEngine
    from ananta.platform.unified_metadata_registry import UnifiedMetadataRegistry
    from ananta.services.discovery_service.service import DiscoveryService
    from ananta.services.inference_service.interfaces.public import InferenceServiceAPI
    from ananta.services.job_service.interfaces.public import JobServiceAPI
    from ananta.services.job_service.service import JobService
    from ananta.services.lifecycle_management_service.interfaces.public import LifecycleManagementAPI
    from ananta.services.schema_manager import SchemaManager
    from ananta.services.state_service.interfaces.public import StateManagementAPI

    from agent_messaging_plugin.models import BridgeSessionState
    from agent_messaging_plugin.platform_surface import PlatformSurface

    state = fixture.state
    SchemaManager(state, fixture.plugin).initialize_schemas(CoreSchemaDefinitions.get_all_core_schemas())
    processes: dict[str, object] = {}
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    for api in (StateManagementAPI, JobServiceAPI, InferenceServiceAPI, LifecycleManagementAPI):
        scanner._scan_class_methods(api, processes)
    ledger_public = importlib.import_module("ananta.services.session_ledger_service.interfaces.public")
    scanner._scan_module_classes(ledger_public, ledger_public.__name__, processes)
    local_public = importlib.import_module("ananta.services.local_self_deployment_service.interfaces.public")
    scanner._scan_module_classes(local_public, local_public.__name__, processes)
    registry: dict[str, object] = {"processes": processes}
    plugins = PluginManager()
    plugins.plugins[fixture.plugin.name] = fixture.plugin
    plugin_scanner = PluginProcessScanner(plugins, PluginRegistrationValidator(), ServiceInterfaceMetadataGenerator(), InvocationSchemaGenerator())
    sql_metadata = next(action for action in fixture.plugin.get_available_actions() if action.name == "execute_sql")
    plugin_scanner._register_plugin_process(fixture.plugin.name, sql_metadata, registry)
    graph = FlowRuntimeGraph(state)
    manager = AsyncJobManager(state, graph)
    flow_manager = FlowManager(state)
    recorder = ActionEventRecorder(state, flow_manager, graph)
    metadata = UnifiedMetadataRegistry(str(ROOT / "ananta/src/ananta/platform/metadata"), str(ROOT / "plugins"), str(fixture.app_home))
    engine = NewTemplateEngine(metadata, state_service=state)
    factory = ActionFactory(registry, template_engine=engine, state_service=state, action_event_recorder=recorder, app_home=str(fixture.app_home))
    manager.set_action_factory(factory)
    control: Any = manager.create_job("boundary_fixture", "point_read", {"notes": "existing exact-id reader control"})
    # Use the production resolver without starting any application lifecycle.
    orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
    orchestrator.APP_HOME = str(fixture.app_home)
    orchestrator.state_service = state
    orchestrator.async_job_manager = manager
    orchestrator._registered_control_job_id = control["data"]["job_id"]
    orchestrator.action_recorder = recorder
    orchestrator.service_manager = SimpleNamespace(job_service=JobService(state, manager))
    fixture.plugin.set_orchestrator_ref(orchestrator)
    discovery: Any = DiscoveryService.__new__(DiscoveryService)
    discovery.processes = processes
    contexts = ExecutionContextManager()
    processor = ActionProcessor(plugins, state_service=state, action_factory=factory, orchestrator=orchestrator, process_registry=registry, execution_context_manager=contexts, discovery_service=discovery)
    poller = ActionQueuePoller(state, cast(Any, processor), graph, cast(Any, factory), blob_storage_service=cast(Any, object()), app_home=str(fixture.app_home), execution_context_manager=contexts)
    surface = PlatformSurface(action_factory=factory, flow_manager=flow_manager, compilation_context_builder=CompilationContextBuilder(), bridge_manager=cast(Any, None), process_registry=registry, state_service=state)
    session = state.write_state("core", {"table": "sessions", "record": {"context_type": "registered-boundary-fixture"}})
    session_id = session["data"]["result"]["generated_id"]
    bridge = BridgeSessionState("registered-boundary-fixture", session_id)
    return state, plugins, manager, orchestrator, poller, surface, bridge


async def registered_count_boundary(fixture: Any) -> None:
    """Drive the real bridge/factory/poller against an owned PostgreSQL leaf."""
    state, plugins, manager, orchestrator, poller, surface, bridge = native_runtime(fixture)
    first = surface._submit_process_call(bridge=bridge, process_key="service_interface::state_service::count", arguments={"namespace": "core", "data": {"table": "job", "filters": {}}}, reason="park the actual private aggregate", deliver_to_bridge=False)
    second = surface._submit_process_call(bridge=bridge, process_key="service_interface::job_service::get_job", arguments={"job_id": orchestrator._registered_control_job_id}, reason="ordinary exact-id action while the effect is parked", deliver_to_bridge=False)
    entered, release = threading.Event(), threading.Event()
    at_leaf: list[dict[str, Any]] = []
    provider = fixture.plugin._get_provider()
    aggregate = provider.aggregate

    def parked(namespace: str, table: str, *args: Any, **kwargs: Any) -> Any:
        if table != "job":
            return aggregate(namespace, table, *args, **kwargs)
        snapshot = state.query_state("core", {"table": "job", "filters": {"provider_name": "state_service.count"}, "limit": 1})
        at_leaf.extend(snapshot["data"]["records"])
        entered.set()
        assert release.wait(10), "fixture must release the parked aggregate"
        return aggregate(namespace, table, *args, **kwargs)

    task: asyncio.Task[None] | None = None
    with patch.object(provider, "aggregate", side_effect=parked):
        try:
            task = asyncio.create_task(poller._poll_once())
            assert await asyncio.to_thread(entered.wait, 5), "actual aggregate leaf must be reached"
            assert len(at_leaf) == 1, "registered count must persist its owned job before entering the aggregate leaf"
            await asyncio.wait_for(asyncio.shield(task), 3)
            dispatched = state.query_state("core", {"table": "action_events", "filters": {"id": second["action_id"]}, "limit": 1})
            assert dispatched["data"]["records"][0]["status"] == "completed", "another ordinary action must dispatch while the effect leaf is parked"
            assert first["action_id"] != second["action_id"]
        finally:
            release.set()
            if task is not None:
                await task
    assert len(at_leaf) == 1
    terminal = await terminal_job(orchestrator.service_manager.job_service, str(at_leaf[0]["id"]))
    assert terminal["result"]["data"]["result"]["value"] >= 1
    assert json.loads(terminal["metadata"])["registered_action_id"] == first["action_id"]
    assert terminal["flow_token_id"], "the admitted owner retains the actual action token"

    await native_latest_lookup(manager, state, orchestrator, surface, bridge, poller, terminal["id"], first["action_id"])
    await native_sql_boundaries(provider, state, orchestrator, surface, bridge, poller)
    await native_specialized_bodies(fixture, orchestrator, plugins, surface, bridge, poller)
    await drained_registered_workers(manager)
    print("PASS real_bridge_factory_poller, persisted_receipt_before_leaf, ordinary_dispatch_while_parked")


async def drained_registered_workers(manager: AsyncJobManager) -> None:
    """Fixture teardown follows all durable outcome and notification writes."""
    for owner in manager._owners.values():
        thread = owner._thread
        if thread is not None:
            await asyncio.to_thread(thread.join, 5)
            assert not thread.is_alive(), "the owned worker must finish before closing its private pool"


async def native_latest_lookup(manager: AsyncJobManager, state: Any, orchestrator: Any, surface: Any, bridge: Any, poller: ActionQueuePoller, previous_job_id: str, first_action_id: str) -> None:
    """A native lookup fixes its answer before its bookkeeping admission."""
    # A concurrent newer job and this lookup's own job cannot replace the
    # pre-admission answer. Use the actual bridge and original reader.
    create_job = manager.create_job

    def concurrent_create(*args: Any, **kwargs: Any) -> dict[str, object]:
        receipt = create_job(*args, **kwargs)
        from ananta.core.state.async_job_manager import registered_action_context

        with registered_action_context(None), action_execution_context(None):
            create_job("concurrent", "newer", {"notes": "frontier control"})
        return receipt

    with patch.object(manager, "create_job", side_effect=concurrent_create):
        latest = surface._submit_process_call(bridge=bridge, process_key="service_interface::job_service::get_latest_job", arguments={}, reason="lookup excludes its own admission and concurrent newer jobs", deliver_to_bridge=False)
        await poller._poll_once()
    lookup = state.query_state("core", {"table": "job", "filters": {"provider_name": "job_service.get_latest_job"}, "limit": 1})["data"]["records"][0]
    answer = await terminal_job(orchestrator.service_manager.job_service, str(lookup["id"]))
    assert answer["result"]["data"]["result"]["job"]["id"] == previous_job_id, "latest lookup must use its pre-admission frontier"
    assert latest["action_id"] != first_action_id


async def native_sql_boundaries(provider: Any, state: Any, orchestrator: Any, surface: Any, bridge: Any, poller: ActionQueuePoller) -> None:
    """Both native SQL registrations park the same owned PostgreSQL leaf."""
    for key in ("service_interface::state_service::execute_sql", "plugin::postgres_state_management_plugin::execute_sql"):  # wint:negative-fixture
        entered, release = threading.Event(), threading.Event()
        observed: list[dict[str, Any]] = []
        execute_query = provider.execute_query

        def parked_sql(*args: Any, rows: list[dict[str, Any]] = observed, reached: threading.Event = entered, resume: threading.Event = release, execute: Any = execute_query, **kwargs: Any) -> Any:
            result = state.query_state("core", {"table": "job", "filters": {"provider_name": "postgres_state_management_plugin.execute_sql", "status": "processing"}, "limit": 1})
            rows.extend(result["data"]["records"])
            reached.set()
            assert resume.wait(10)
            return execute(*args, **kwargs)

        surface._submit_process_call(bridge=bridge, process_key=key, arguments={"sql_query": "SELECT 7 AS owned_value"}, reason="both SQL endpoints use the same owner", deliver_to_bridge=False)
        ordinary = surface._submit_process_call(bridge=bridge, process_key="service_interface::job_service::get_job", arguments={"job_id": orchestrator._registered_control_job_id}, reason="ordinary action during SQL leaf", deliver_to_bridge=False)
        with patch.object(provider, "execute_query", side_effect=parked_sql):
            task = asyncio.create_task(poller._poll_once())
            try:
                assert await asyncio.to_thread(entered.wait, 5)
                assert len(observed) == 1, "each SQL endpoint must persist the same owner's job before SQL"
                await asyncio.wait_for(asyncio.shield(task), 3)
                row = state.query_state("core", {"table": "action_events", "filters": {"id": ordinary["action_id"]}, "limit": 1})["data"]["records"][0]
                assert row["status"] == "completed"
            finally:
                release.set()
                await task
        result = await terminal_job(orchestrator.service_manager.job_service, str(observed[0]["id"]))
        assert result["result"]["data"]["records"] == [[7]], "retain the original provider row-list shape"


async def native_specialized_bodies(fixture: Any, orchestrator: Any, plugins: PluginManager, surface: Any, bridge: Any, poller: ActionQueuePoller) -> None:
    """Actual VERTEX, poll-executor, repair and manifest bodies, owned effect leaves."""
    import ananta.services.lifecycle_management_service.service as lifecycle_module
    from ananta.services.inference_service import InferenceService
    from ananta.services.inference_service.vertex_resolver import InferenceProviderResolver
    from ananta.services.lifecycle_management_service.service import LifecycleManagementService
    from ananta.services.session_ledger_service.service import SessionLedgerService, _LedgerPollExecutor

    from agent_messaging_plugin.session_inference_provider import SessionInferenceProvider

    state = fixture.state

    async def drive(key: str, arguments: dict[str, object], target: Any, method: str, effect: Any, while_parked: Any = None) -> dict[str, Any]:
        entered, release = threading.Event(), threading.Event()
        observed: list[dict[str, Any]] = []
        owner_name = key.removeprefix("service_interface::").replace("::", ".")

        def parked(*args: Any, **kwargs: Any) -> Any:
            rows = state.query_state("core", {"table": "job", "filters": {"provider_name": owner_name, "status": "processing"}, "limit": 1})["data"]["records"]
            observed.extend(rows)
            entered.set()
            assert release.wait(10)
            return effect(*args, **kwargs)

        surface._submit_process_call(bridge=bridge, process_key=key, arguments=arguments, reason="native specialized body", deliver_to_bridge=False)
        ordinary = surface._submit_process_call(bridge=bridge, process_key="service_interface::job_service::get_job", arguments={"job_id": orchestrator._registered_control_job_id}, reason="ordinary progress during specialized effect", deliver_to_bridge=False)
        with patch.object(target, method, side_effect=parked):
            task = asyncio.create_task(poller._poll_once())
            try:
                assert await asyncio.to_thread(entered.wait, 5), (key, "effect leaf not reached")
                assert len(observed) == 1, (key, "receipt did not precede effect leaf")
                await asyncio.wait_for(asyncio.shield(task), 3)
                row = state.query_state("core", {"table": "action_events", "filters": {"id": ordinary["action_id"]}, "limit": 1})["data"]["records"][0]
                assert row["status"] == "completed"
                if while_parked is not None:
                    await while_parked()
            finally:
                release.set()
                await task
        return await terminal_job(orchestrator.service_manager.job_service, str(observed[0]["id"]))

    delivered: list[dict[str, Any]] = []

    def append(_bridge: str, _kind: str, content: str, _metadata: object) -> None:
        delivered.append(json.loads(content))

    events = SimpleNamespace(append_event=append)
    vertex = SessionInferenceProvider(bridge_id="owned-vertex", agent_instance_id="owned-vertex-instance", agent_id="fixture", session_label="owned vertex", bridge_manager=cast(Any, events))
    messaging = SimpleNamespace(resolve_role_to_instance=lambda _role: "owned-vertex-instance", get_inference_provider=lambda _instance: vertex, was_inference_provider_bound=lambda _instance: True, get_autonomic_provider=lambda: vertex)
    plugins.plugins["agent_messaging_plugin"] = cast(Any, messaging)
    inference: Any = InferenceService.__new__(InferenceService)
    inference._state_service = state
    inference._vertex_resolver = InferenceProviderResolver(plugin_manager=plugins, state_service=state)
    orchestrator.service_manager.inference_service = inference
    bridge.agent_instance_id = "owned-vertex-instance"
    for name in ("process_error", "process_results"):
        result = await drive(f"service_interface::inference_service::{name}", {"params": {"model": {}, "prompt": {}}, "state": {"flow_id": "spoof"}}, events, "append_event", append)
        assert result["status"] == "completed", result
        assert delivered[-1]["state"]["session_id"] == bridge.session_id
        assert delivered[-1]["state"]["flow_id"] != "spoof"
    bridge.agent_instance_id = None

    importer = SimpleNamespace(poll_source=lambda _source: SimpleNamespace(sources_polled=1, sessions_seen=2, events_persisted=3, batches_failed=0))
    repository = SimpleNamespace(backfill_event_external_ids=lambda **_kwargs: {"sessions_scanned": 1, "events_stamped": 2, "collisions_skipped": 0})
    ledger: Any = SessionLedgerService.__new__(SessionLedgerService)
    ledger._poll_executor, ledger._importer, ledger._repository = _LedgerPollExecutor(), importer, repository
    ledger._blob_adapter = SimpleNamespace(fetch_event_text=lambda _blob: "owned fixture")
    orchestrator.session_ledger_service = ledger
    polled = await drive("service_interface::session_ledger_service::poll_source", {"source_id": "owned-source"}, importer, "poll_source", importer.poll_source)
    assert polled["result"]["events_persisted"] == 3
    repaired = await drive("service_interface::session_ledger_service::backfill_event_external_ids", {}, repository, "backfill_event_external_ids", repository.backfill_event_external_ids)
    assert repaired["result"]["events_stamped"] == 2

    lifecycle: Any = LifecycleManagementService.__new__(LifecycleManagementService)
    lifecycle._orchestrator = orchestrator
    orchestrator.service_manager.lifecycle_management_service = lifecycle
    from ananta.core.orchestration.service_bindings import BindingSource, ServiceBinding, ServiceBindings, ServiceName

    later_effects: list[str] = []
    later_actions: list[str] = []

    def admit(action_name: str, parameters: dict[str, Any], _context: dict[str, Any]) -> dict[str, object] | None:
        if action_name != "rollback_release":
            return None

        def body(_values: dict[str, object]) -> dict[str, object]:
            first = state.query_state("core", {"table": "job", "filters": {"provider_name": "lifecycle_management_service.apply_manifest"}, "limit": 1})["data"]["records"][0]
            assert first["status"] in {"completed", "error"}, "a later deploy effect cannot overtake the first terminal write"
            later_effects.append(action_name)
            return {"action_status": "completed", "data": {"ordered": True}}

        return orchestrator.async_job_manager.submit_registered_operation("owned-deploy-contract", action_name, parameters, body)

    plugins.plugins["owned-deploy-contract"] = cast(Any, SimpleNamespace(admit_registered_operation=admit))
    orchestrator.plugin_manager = plugins
    bindings = ServiceBindings(fixture.app_home)
    bindings._bindings = {ServiceName.LOCAL_SELF_DEPLOYMENT_SERVICE: ServiceBinding(ServiceName.LOCAL_SELF_DEPLOYMENT_SERVICE, "owned-deploy-contract", BindingSource.CONFIG)}
    bindings._loaded = True
    orchestrator.service_bindings = bindings

    async def later_deploy() -> None:
        # Arguments come from this exact registered declaration; the owner
        # contract double performs no release/router/host activity.
        entry = surface._process_registry["processes"]["service_interface::local_self_deployment_service::rollback_release"]
        arguments: dict[str, object] = {}
        for name, parameter in entry["parameters"].items():
            if parameter.get("required"):
                arguments[name] = {} if parameter["type"] == "object" else "owned-release"
        submitted = surface._submit_process_call(bridge=bridge, process_key="service_interface::local_self_deployment_service::rollback_release", arguments=arguments, reason="cross-key deploy ordering", deliver_to_bridge=False)
        later_actions.append(submitted["action_id"])
        ordinary = surface._submit_process_call(bridge=bridge, process_key="service_interface::job_service::get_job", arguments={"job_id": orchestrator._registered_control_job_id}, reason="ordinary progress while deploy slot held", deliver_to_bridge=False)
        await poller._poll_once()
        row = state.query_state("core", {"table": "action_events", "filters": {"id": submitted["action_id"]}, "limit": 1})["data"]["records"][0]
        assert row["status"] == "queued" and later_effects == [], "a later deploy stays unclaimed through the owner outcome"
        ordinary_row = state.query_state("core", {"table": "action_events", "filters": {"id": ordinary["action_id"]}, "limit": 1})["data"]["records"][0]
        assert ordinary_row["status"] == "completed"

    await drive("service_interface::lifecycle_management_service::apply_manifest", {"new_manifest": {"plugins": []}, "reason": "owned dry-run preflight", "dry_run": True}, lifecycle_module, "read_current_manifest_state", lifecycle_module.read_current_manifest_state, later_deploy)
    assert poller._detached_deploy is not None
    await asyncio.wait_for(asyncio.shield(poller._detached_deploy.task), 3)
    await poller._poll_once()
    assert poller._detached_deploy is not None
    await asyncio.wait_for(asyncio.shield(poller._detached_deploy.task), 3)
    assert later_actions and later_effects == ["rollback_release"]
    print("PASS actual_VERTEX_forward, ledger_poll_executor_and_repair, manifest_read_preflight_before_return, no_live_effects")


async def terminal_job(service: Any, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = service.get_job(job_id)
        assert result["action_status"] == "completed", result
        job = result["data"]["result"]["job"]
        assert job is not None
        if job["status"] in {"completed", "error", "cancelled"}:
            return job
        await asyncio.sleep(0.005)
    raise AssertionError(f"fixture job {job_id} did not reach a durable outcome")


def real_payload_histories(fixture: Any) -> None:
    """A long physical history never becomes a truncated or absent answer."""
    from ananta.services.job_service.service import JobService

    state = fixture.state
    manager = AsyncJobManager(state, FlowRuntimeGraph(state))
    receipt: Any = manager.create_job("history", "readback", {"notes": "long result and NULL error history"})
    job_id = receipt["data"]["job_id"]
    for sequence in range(1, 202):
        result = state.write_state("core", {"table": "job_payload", "record": {"job_id": job_id, "payload_type": "result", "sequence": sequence, "payload_data": json.dumps({"sequence": sequence})}})
        assert result["action_status"] == "completed", result
    for kind, sequence, payload in (("result", None, {"sequence": "null"}), ("result", 201, {"sequence": "tied"}), ("error", None, {"detail": "null sequence retained"})):
        result = state.write_state("core", {"table": "job_payload", "record": {"job_id": job_id, "payload_type": kind, "sequence": sequence, "payload_data": json.dumps(payload)}})
        assert result["action_status"] == "completed", result
    service = JobService(state, manager)
    ordered = state.query_ordered
    queries: list[dict[str, object]] = []

    def observed(namespace: str, data: dict[str, object]) -> Any:
        queries.append(data)
        return ordered(namespace, data)

    with patch.object(state, "query_ordered", side_effect=observed), patch.object(manager, "create_job", side_effect=AssertionError("exact-id read must not admit another job")):
        result = service.get_job(job_id)
        assert result["action_status"] == "completed", result
        job = result["data"]["result"]["job"]
        assert job["result"]["sequence"] in {201, "tied"}, "numeric maximum wins over NULL, including an equal-sequence tie"
        assert job["error"] == {"detail": "null sequence retained"}
        assert queries and all(query["limit"] == 1 for query in queries)
    missing_and_failed_payloads(state, manager, service, job_id, ordered)


def missing_and_failed_payloads(state: Any, manager: AsyncJobManager, service: Any, job_id: str, ordered: Any) -> None:
    """A missing payload and a failed read remain different outcomes."""
    absent: Any = manager.create_job("history", "absent", {"notes": "no result or error exists"})
    empty = service.get_job(absent["data"]["job_id"])["data"]["result"]["job"]
    assert empty["result"] is None and empty["error"] is None

    def failed(namespace: str, data: dict[str, object]) -> Any:
        if data.get("table") == "job_payload":
            return {"action_status": "error", "error": {"code": "fixture.payload_read_failed"}}
        return ordered(namespace, data)

    with patch.object(state, "query_ordered", side_effect=failed):
        result = service.get_job(job_id)
        assert result["action_status"] == "error", "a failed payload read must not look like an absent payload"
    print("PASS real_PG_long_numeric_null_tied_missing_failed_payloads, store_limit1, exact_id_non_recursion")


def main() -> int:
    from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture, load_fixture, skip_fixture

    try:
        load_fixture("fast_return_census")
    except MissingFixtureError:
        return skip_fixture()
    with ledger_fixture("fast_return_census", schema="registered_boundary") as fixture:
        asyncio.run(registered_count_boundary(fixture))
        real_payload_histories(fixture)
    print("JobService lifecycle smoke — staleness sweep + unresolved-token detector")
    print("\nCase 1: the sweep terminates only stale processing rows")
    test_sweep_terminates_only_stale_processing_rows()
    print("\nCase 2: the sweep ignores queued rows")
    test_sweep_ignores_queued_rows()
    print("\nCase 3: the swept reason names the sweep")
    test_swept_reason_names_the_sweep()
    print("\nCase 4: the sweep refuses without a job manager")
    test_sweep_refuses_without_a_job_manager()
    print("\nCase 5: the sweep validates its bounds")
    test_sweep_validates_its_bounds()
    print("\nCase 6: the detector reports a terminal row with an open token")
    test_detector_reports_terminal_row_with_open_token()
    print("\nCase 7: the detector excludes resolved AND missing tokens")
    test_detector_excludes_resolved_and_missing_tokens()
    print("\nCase 8: the detector mutates nothing")
    test_detector_mutates_nothing()
    print("\nRail: fakes track the real signatures")
    test_fakes_track_real_signatures()

    print()
    if _failures:
        print(f"FAIL: {len(_failures)} check(s) failed")
        for message in _failures:
            print(f"  - {message}")
        return 1
    print("PASS: the two failure modes stay distinguishable, and only one of them mutates")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
