#!/usr/bin/env python3
"""Thinking registered admission, bounded receipt and synchronous composition.

Real registry, action factory validation, processor, job manager and poller;
only owned storage/model collaborators are doubles. The private-PG leg uses
the shipped provisioner when its manifest is present.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for directory in (ROOT, ROOT / "ananta" / "src", ROOT / "ananta" / "tests" / "core" / "actions", Path(__file__).parent):
    sys.path.insert(0, str(directory))
for plugin_name in ("default_thinking_plugin", "default_knowledge_plugin", "postgres_state_management_plugin"):
    sys.path.insert(0, str(ROOT / "plugins" / plugin_name / "src"))

importlib.import_module("ananta.core.config")

import authored_by_value_conversion_smoke as authored  # noqa: E402
import joseki_run_driver_smoke as runs  # noqa: E402
import plan_template_lifecycle_smoke as templates  # noqa: E402
import planning_completion_resume_smoke as completion  # noqa: E402
import pull_execution_smoke as pull  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory, validate_process_arguments  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor, QueuedActionProtocol  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.orchestration.execution_context import ExecutionContextManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager, registered_action_context  # noqa: E402
from ananta.core.state.execution_token_context import action_execution_context  # noqa: E402
from ananta.core.state.registered_job_operations import THINKING_REGISTERED_OPERATIONS  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.platform.new_template_engine import NewTemplateEngine  # noqa: E402
from ananta.platform.unified_metadata_registry import UnifiedMetadataRegistry  # noqa: E402
from ananta.services.discovery_service.service import DiscoveryService  # noqa: E402
from ananta.services.job_service import JobService  # noqa: E402
from ananta.services.job_service.interfaces import public as job_public  # noqa: E402
from ananta.services.knowledge_service.interfaces import search as knowledge_search  # noqa: E402
from ananta.services.thinking_service import ThinkingService  # noqa: E402
from ananta.services.thinking_service.interfaces import public  # noqa: E402
from default_thinking_plugin.plan_store import PlanStore  # noqa: E402
from default_thinking_plugin.plugin import DefaultThinkingPlugin, _PluginProcessSchemaLookup  # noqa: E402
from default_thinking_plugin.wbs_authoring import WbsAuthoringService  # noqa: E402

_job_fixture: Any = importlib.import_module("async_job_atomic_completion_smoke")
manager_fixture = _job_fixture.manager_fixture


def registered_processes() -> dict[str, Any]:
    """Use shipped declarations for every fixture's referenced process key."""
    processes: dict[str, Any] = {}
    scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
    for module in (public, job_public, knowledge_search):
        scanner._scan_module_classes(module, module.__name__, processes)
    return processes


def authored_card(key: str) -> str:
    """One owned, valid card with no checkout-only exemplar dependency."""
    return f"""# Owned {key}
JOSEKI_KEY: {key}
DESCRIPTION: Search for guidance and preserve the public outcome.
EMBEDDING_DESCRIPTION: A reusable one-step research pattern for finding guidance on an owned topic, preserving the result for the next planning operation.

## Input Contract
- Topic to search for.

## Output Contract
- Guidance for the topic.

## Sequence
[ ] 1. Search for guidance
    a) Search ({pull.SEARCH_KEY})
        Arguments:
        {{"query": "owned guidance"}}

## Expected Step Count
1 step.

## Next Joseki
None — terminal pattern.

## Repair Joseki
None — failed searches return to the caller.
"""


def authored_plugin() -> tuple[Any, Any]:
    """Real registrar/lifecycle and WBS service over shipped owned doubles."""
    plugin: Any = DefaultThinkingPlugin()
    _service, writer, state, focus = authored.build_service()
    plugin._state_service = runs._MemState()
    kb = templates.InMemoryKb()
    plugin._read_from_authored_joseki_kb = kb.read
    plugin._write_to_authored_joseki_kb = kb.write
    discovery: Any = DiscoveryService.__new__(DiscoveryService)
    discovery.processes = registered_processes()
    plugin.orchestrator_ref = SimpleNamespace(get_service=lambda name: discovery if name == "discovery_service" else None)
    service = WbsAuthoringService(writer, state, focus, process_schema_lookup=_PluginProcessSchemaLookup(plugin))
    plugin._wbs_authoring_service = lambda _session: service
    return plugin, writer


class Harness:
    """Actual registered dispatch bound to an owned real Thinking provider."""

    def __init__(self, plugin: Any) -> None:
        self.state, self.manager, self.token, self.completed = manager_fixture()
        prepare = self.state.prepare_record

        def prepare_with_defaults(namespace: str, table: str, record: dict[str, object]) -> dict[str, object]:
            prepared = prepare(namespace, table, record)
            prepared.setdefault("created_at", datetime.now(UTC).isoformat())
            prepared.setdefault("updated_at", prepared["created_at"])
            return prepared

        self.state.prepare_record = prepare_with_defaults
        self.processes = registered_processes()
        self.plugins = PluginManager()
        plugin.name = "default_thinking_plugin"
        plugin.logger = logging.getLogger("thinking-admission-fixture")
        plugin.set_ready()
        self.plugins.plugins[plugin.name] = plugin
        self.service = ThinkingService(self.plugins, plugin.name)
        self.service._thinking_plugin = plugin
        self.services: dict[str, Any] = {"thinking_service": self.service, "job_service": JobService(self.state, self.manager)}
        previous = getattr(getattr(plugin, "orchestrator_ref", None), "get_service", lambda _name: None)
        self.orchestrator = SimpleNamespace(async_job_manager=self.manager, get_service=lambda name: self.services.get(name) or previous(name), APP_HOME=str(ROOT), action_recorder=None)
        plugin.orchestrator_ref = self.orchestrator
        contexts = ExecutionContextManager()
        contexts.create_context("flow")
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.processes
        self.processor = ActionProcessor(self.plugins, state_service=self.state, orchestrator=cast(Any, self.orchestrator), process_registry={"processes": self.processes}, execution_context_manager=contexts, discovery_service=discovery)
        metadata_root = ROOT / "ananta" / "src" / "ananta" / "platform" / "metadata"
        metadata = UnifiedMetadataRegistry(str(metadata_root), str(ROOT / "plugins"), str(Path(__file__).parent))
        engine = NewTemplateEngine(metadata)
        engine.template_patterns = json.loads((metadata_root / "schemas" / "template_patterns.json").read_text())
        engine._initialized = True
        self.factory = ActionFactory(process_registry={"processes": self.processes}, state_service=self.state._id_service, template_engine=engine)

    def action(self, name: str, arguments: dict[str, object], *, identity: str = "action", provider: str = "thinking_service") -> QueuedActionProtocol:
        key = f"service_interface::{provider}::{name}"
        validate_process_arguments(key, self.processes[key]["parameters"], arguments)
        built = self.factory.create_action({"process_key": key, "arguments": arguments, "notes": "owned Thinking fixture", "session_id": "s", "flow_id": "flow", "error_processor_kind": "bridge_delivery"})
        serialized = json.dumps(built["parameters"])
        self.state.rows.setdefault("action_events", []).append({"id": identity, "status": "processing", "process_key": key, "parameters": serialized, "flow_token_id": self.token, "core__sessions_id": "s", "core__flows_id": "flow"})
        return cast(QueuedActionProtocol, _job_fixture.FixtureAction(identity, key, serialized, str(built["notes"]), str(built["timestamp"]), session_id="s", flow_id="flow", flow_token_id=self.token, source_plugin="thinking-fixture"))

    def job(self, job_id: str) -> dict[str, Any]:
        """Use the public exact-id reader that attaches persisted payloads."""
        service = JobService(self.manager.state_service, self.manager)
        response = service.get_job(job_id)
        job = response["data"]["result"]["job"]
        assert isinstance(job, dict), response
        return job

    def join(self) -> None:
        for owner in self.manager._owners.values():
            thread = owner._thread
            if thread is not None:
                thread.join(5)
                assert not thread.is_alive(), "owned fixture worker must drain"


def _real_poller(harness: Harness, action: QueuedActionProtocol) -> Any:
    """Keep preflight, dispatch and receipt storage on the real poller route."""
    poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.action_processor = harness.processor
    poller.state_service = harness.state
    poller._flow_runtime_graph = harness.manager._flow_runtime_graph
    poller.execution_context_manager = harness.processor.execution_context_manager
    poller._discovery_service = harness.processor.discovery_service
    poller._io_post_message_keys = frozenset()
    assert poller._get_canonical_arguments_schema(action.process_key), "real registry must expose the canonical transport schema"
    return poller


def _stored_poller_receipt(harness: Harness, action: QueuedActionProtocol, elapsed: float, callers: list[int]) -> dict[str, object]:
    """Check the bounded durable receipt while the provider leaf stays parked."""
    rows = harness.state.rows["action_results"]
    assert len(rows) == 1 and rows[0]["core__action_events_id"] == action.id
    response: dict[str, object] = json.loads(rows[0]["result_data"])
    assert_owned_receipt(harness, action, response, elapsed, callers)
    assert harness.completed == [], "the stored receipt must return before the parked job completes"
    assert harness.state.rows["flow_tokens"][0]["state"] == "waiting_job"
    return response


def template_admission() -> None:
    """Real poller validates ordinary arguments and returns with the KB parked."""
    plugin, kb = templates.build_plugin()
    harness = Harness(plugin)
    action = harness.action("get_plan_template", {"template_key": "plan_template::fixture::missing"})
    poller = _real_poller(harness, action)
    validate = poller._validate_action_arguments
    validated: list[str] = []
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    callers: list[int] = []
    response: dict[str, object] = {}

    def transport_preflight(queued: Any) -> Any:
        assert not harness.state.rows.get("job"), "existing transport validation precedes owner admission"
        result = validate(queued)
        assert result[0] is True, result
        validated.append(queued.process_key)
        return result

    def read(path: str) -> str:
        assert validated == [action.process_key], "real canonical validation must precede provider effects"
        callers.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "fixture must release the actual KB read"
        return kb.read(path)

    def dispatch() -> None:
        asyncio.run(poller._process_action(action))
        returned.set()

    with patch.object(poller, "_validate_action_arguments", side_effect=transport_preflight), patch.object(plugin, "_read_from_plan_templates_kb", side_effect=read), ThreadPoolExecutor(max_workers=1) as executor:
        start = time.perf_counter()
        future = executor.submit(dispatch)
        try:
            assert entered.wait(5), "valid setup must reach the actual template KB reader"
            prompt = returned.wait(0.2)
            elapsed = time.perf_counter() - start
            if prompt:
                response = _stored_poller_receipt(harness, action, elapsed, callers)
        finally:
            release.set()
        future.result(timeout=5)
        harness.join()
    assert prompt, f"get_plan_template registered admission waited for its parked KB read; reached={callers!r}, response={response!r}"
    assert elapsed < 0.05, f"canonical receipt exceeded 50 ms: {elapsed * 1000:.3f} ms"
    assert response["success"] is True and isinstance(response.get("data"), dict), response
    data: Any = response["data"]
    assert {"action_status", "data", "actions", "error", "timestamp"} <= response.keys()
    job = harness.job(str(data["job_id"]))
    assert job["status"] == "completed" and job["result"]["found"] is False
    assert len(harness.state.rows["job"]) == 1 and harness.completed == ["flow"]
    print(f"PASS template_real_poller_transport_preflight_then_bounded_receipt_while_KB_parked, receipt_ms={elapsed * 1000:.3f}, exact_id_terminal_result")


def poller_receipt_decode() -> None:
    """An admitted job skips body decode; its terminal completion retains it."""
    state, manager, token, _completed = manager_fixture()
    original_parameters = json.dumps({"content": "owned body " * 1024})
    state.rows["action_events"] = [{"id": "action", "status": "processing", "flow_token_id": token, "process_key": "service_interface::thinking_service::upsert_plan", "parameters": original_parameters, "core__sessions_id": "s", "core__flows_id": "flow"}]
    with action_execution_context(token), registered_action_context("action", "flow", "s"):
        receipt = manager.create_job("thinking_service", "upsert_plan", {"notes": "bounded receipt", "parameters": {"input_custody": "volatile_action"}})
    assert receipt["action_status"] == "completed", receipt
    poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.state_service, poller._flow_runtime_graph = state, manager._flow_runtime_graph
    original = json.loads
    decoded: list[str] = []

    def loads(value: Any, *args: Any, **kwargs: Any) -> Any:
        if value == original_parameters:
            decoded.append("body")
        return original(value, *args, **kwargs)

    with patch("ananta.core.actions.action_queue_poller.json.loads", side_effect=loads):
        poller._mark_action_completed("action", receipt, token)
        assert decoded == [], "owned receipt must bypass original action-body JSON decoding"
        assert len(state.rows["action_results"]) == 1
        poller._retrieve_action_details("action")
        assert decoded == ["body"], "terminal completion must retain original parameter decoding"
    print("PASS common_poller_owned_receipt_without_body_decode, terminal_decode_retained")


@dataclass
class Case:
    """A real provider entry and the owned I/O collaborator it reaches."""

    name: str
    plugin: Any
    arguments: dict[str, object]
    leaf: Any
    attribute: str
    services: dict[str, Any] = field(default_factory=dict)
    temporary: Any = None


def artifact_cases() -> list[Case]:
    cases: list[Case] = []
    arguments: dict[str, dict[str, object]] = {
        "create_resolved_intake_state": {"intake_id": authored.INTAKE_ID, "content": authored.AUTHORED_RIS},
        "create_work_manifest": {"content": authored.AUTHORED_MANIFEST},
        "patch_work_manifest": {"manifest_id": "wmf-fixture-001", "content": authored.AUTHORED_MANIFEST},
        "create_authored_artifact": {"artifact_type": "brief", "content": authored.AUTHORED_BRIEF},
        "patch_authored_artifact": {"artifact_type": "brief", "artifact_id": "brf-fixture", "content": authored.AUTHORED_BRIEF},
        "create_movement_design": {"manifest_id": "wmf-fixture-001", "movement_type": "toccata", "packet_content": authored.AUTHORED_PACKET, "ledger_content": authored.AUTHORED_LEDGER},
    }
    for name, values in arguments.items():
        service, writer, _state, _focus = authored.build_service(authored.FOCUSED_BRIEF)
        plugin: Any = DefaultThinkingPlugin()
        plugin._artifact_authoring_service = lambda _session, bound=service: bound
        cases.append(Case(name, plugin, values, writer, "write"))
    return cases


def wbs_cases() -> list[Case]:
    cases: list[Case] = []
    for name in ("start_wbs_execution", "get_next_wbs_step", "advance_wbs_execution", "record_wbs_step_observation"):
        kb, state = pull.fresh_stores()
        service = pull.build_session(kb, state)
        plugin: Any = DefaultThinkingPlugin()
        plugin._pull_execution_service = lambda _session, bound=service: bound
        arguments: dict[str, object] = {"wbs_id": pull.WBS_ID}
        if name == "record_wbs_step_observation":
            arguments.update(step_number=1, process_key=pull.SEARCH_KEY, result={"action_status": "completed", "data": {"query": "release guidance"}})
        cases.append(Case(name, plugin, arguments, kb, "read"))
    plugin, writer = authored_plugin()
    cases.append(Case("register_authored_work_breakdown_structure", plugin, {"content": pull.WBS_DOC, "wbs_id": pull.WBS_ID, "manifest_id": "wmf-fixture", "phase_number": 2, "phase_name": "Fixture"}, writer, "write"))
    for name in ("patch_work_breakdown_structure", "record_work_breakdown_structure_step_state", "record_work_manifest_phase_state"):
        plugin = DefaultThinkingPlugin()
        _kb, state = pull.fresh_stores()
        plugin._state_service = state
        arguments = {"wbs_id": pull.WBS_ID, "content": pull.WBS_DOC}
        attribute = "_write_to_thinking_plans_kb"
        if name == "record_work_breakdown_structure_step_state":
            arguments = {"wbs_id": pull.WBS_ID, "step_number": 1, "status": "completed"}
            attribute = "_read_from_thinking_plans_kb"
        elif name == "record_work_manifest_phase_state":
            arguments = {"manifest_id": "wmf-fixture", "phase_number": 2, "status": "completed", "outcome_summary": "owned fixture"}
            attribute = "_read_from_thinking_plans_kb"
        cases.append(Case(name, plugin, arguments, plugin, attribute, {"memory_service": SimpleNamespace()}))
    temporary = TemporaryDirectory(prefix="thinking-schema-fixture-")
    root = Path(temporary.name)
    (root / "03_templates").mkdir()
    (root / "03_templates" / "pipeline_schema.json").write_text("{}")
    plugin = DefaultThinkingPlugin()
    plugin._get_knowledge_base_roots = lambda: [("fixture", root)]
    cases.append(Case("generate_section_stem_wbs", plugin, {"wbs_id": "wbs-fixture-phase2", "manifest_id": "wmf-fixture", "phase_number": 2, "phase_name": "Fixture", "style_family": "fixture", "artifact_prefix": "fixture", "pipeline_spec": {}}, Path, "read_text", temporary=temporary))
    return cases


def plan_cases() -> list[Case]:
    cases: list[Case] = []
    for name in ("upsert_plan", "graft_work_breakdown_structure_segment"):
        plugin: Any = DefaultThinkingPlugin()
        memory = SimpleNamespace(get_focused=lambda **_kwargs: {"memories": []})
        plugin._plan_store = PlanStore(get_memory_service=lambda bound=memory: bound, get_knowledge_service=lambda: None, state_service=authored.RecordingStateStore(), cursor_holder=plugin)
        values: dict[str, object] = {"content": "[ ] 1. Owned fixture plan"} if name == "upsert_plan" else {"wbs_id": pull.WBS_ID, "anchor_step_number": 1}
        cases.append(Case(name, plugin, values, memory, "get_focused", {"memory_service": memory}))
    for name in ("create_extended_plan", "update_plan"):
        plugin = DefaultThinkingPlugin()
        memory = SimpleNamespace()
        inference = SimpleNamespace(generate_completion=lambda _request: {"action_status": "completed", "data": {"completion": "owned plan"}})
        plugin._assemble_plan_prompt = lambda *_args, **_kwargs: [{"role": "user", "content": "owned goal"}]
        plugin._require_context_services = lambda: (SimpleNamespace(registry=SimpleNamespace(create_context=lambda **_kwargs: "ctx")), None)
        plugin._append_context_event = lambda *_args, **_kwargs: None
        plugin._load_context_messages = lambda _context: []
        plugin._state_service = SimpleNamespace(read_state=lambda **_kwargs: {"data": {"records": [{"id": "thk-fixture", "task_type": "plan", "context_id": "ctx", "memory_id": ""}]}})
        values = {"goal": "owned goal"} if name == "create_extended_plan" else {"task_id": "thk-fixture", "status_update": "continue owned plan"}
        cases.append(Case(name, plugin, values, inference, "generate_completion", {"memory_service": memory, "inference_service": inference}))
    return cases


def lifecycle_cases() -> list[Case]:
    cases: list[Case] = []
    plugin, _writer = authored_plugin()
    cases.append(Case("register_authored_joseki", plugin, {"content": authored_card("owned_registration")}, plugin, "_write_to_authored_joseki_kb"))
    plugin, _writer = authored_plugin()
    plugin.register_authored_joseki(authored_card("alpha_pattern"))
    cases.append(Case("transition_authored_joseki", plugin, {"joseki_key": "alpha_pattern", "target_state": "candidate"}, plugin, "_read_from_authored_joseki_kb"))
    for name in ("get_plan_template", "transition_plan_template"):
        plugin, kb = templates.build_plugin()
        card = templates._template("plan_template::fixture::owned")
        templates._seed(kb, card)
        values: dict[str, object] = {"template_key": card.template_key}
        if name == "transition_plan_template":
            values["target_state"] = "active"
        cases.append(Case(name, plugin, values, plugin, "_read_from_plan_templates_kb"))
    for name in ("run_joseki", "reconcile_joseki_runs"):
        rig = runs._Rig()
        plugin = DefaultThinkingPlugin()
        plugin._joseki_run_engine = lambda bound=rig.engine: bound
        values = {"joseki_key": "smoke_gate_run", "bindings": {"g": "ruff"}} if name == "run_joseki" else {}
        leaf, attribute = (rig.engine.plugin.cards, "read") if name == "run_joseki" else (rig.engine.plugin.run_store._state, "read_state")
        cases.append(Case(name, plugin, values, leaf, attribute))
    return cases


def completion_cases() -> list[Case]:
    cases: list[Case] = []
    for name in ("process_planning_results", "resume_thinking_completion"):
        inference = completion._RecordingCompletionService({"routing": "deferred", "request_id": "request"})
        plugin: Any = completion._build_plugin(inference)
        plugin._state_service = SimpleNamespace(read_state=lambda **_kwargs: {"data": {"records": [{"id": "pb-1"}]}})
        plugin._assemble_planning_prompt = lambda _context: completion.MESSAGES
        values: dict[str, object] = {"request_id": "request"} if name == "resume_thinking_completion" else {"params": {"prompt": {"observation": {"action_result": {"data": {"planning_context_id": "ctx-1"}}}}}}
        attribute = "get_completion_request" if name == "resume_thinking_completion" else "submit_completion_request"
        cases.append(Case(name, plugin, values, inference, attribute, {"inference_service": inference}))
    return cases


def assert_owned_receipt(harness: Harness, action: QueuedActionProtocol, response: dict[str, object], elapsed: float, entries: list[int]) -> str:
    """A bounded canonical receipt permits unrelated registered work."""
    assert response.get("success") is True and isinstance(response.get("data"), dict), response
    assert elapsed < 0.05, elapsed
    data: Any = response["data"]
    job_id = str(data["job_id"])
    assert len(harness.state.rows["job"]) == 1
    request: Any = harness.manager.get_job_payload(job_id, "request")["data"]
    assert request["payload"]["parameters"] == {"input_custody": "volatile_action", "action_id": action.id}
    assert len(json.dumps(request)) < 1024, "admission must never copy caller-sized content"
    ordinary = harness.action("get_job", {"job_id": job_id}, identity="ordinary", provider="job_service")
    assert harness.processor.execute_action(ordinary)["success"] is True, "ordinary dispatch must progress while Thinking is parked"
    assert entries == [harness.manager._owners["thinking_service"]._thread.ident]
    return job_id


def assert_typed_terminal(harness: Harness, action: QueuedActionProtocol, job_id: str, terminal: list[tuple[str, str]]) -> None:
    """Preserve the full failure envelope and one original-action completion."""
    job = harness.job(job_id)
    assert job["status"] == "error" and job["error"]["code"] == "thinking.fixture_leaf_failure", job
    assert job["result"]["error"]["code"] == "thinking.fixture_leaf_failure"
    assert terminal == [(action.id, "error")] and harness.completed == ["flow"]


def matrix_case(case: Case) -> None:
    """Park the real provider's actual storage/model/preflight collaborator."""
    harness = Harness(case.plugin)
    harness.services.update(case.services)
    action = harness.action(case.name, case.arguments)
    entered, release = threading.Event(), threading.Event()
    entries: list[int] = []
    terminal: list[tuple[str, str]] = []
    harness.manager.set_registered_completion_handler(lambda action_id, status, _result, _error: terminal.append((action_id, status)))
    parser = harness.processor._parse_action_parameters

    def owned_parse(queued: Any) -> dict[str, object]:
        if queued.id == action.id:
            assert len(harness.state.rows["job"]) == 1, "persist the receipt before decoding any caller body"
            assert threading.current_thread() is harness.manager._owners["thinking_service"]._thread, "argument decoding belongs to the owner"
        return parser(queued)

    def leaf(*_args: object, **_kwargs: object) -> object:
        entries.append(threading.get_ident())
        entered.set()
        assert release.wait(5), "fixture must release its operation leaf"
        raise FrameworkError("owned collaborator failure", error_code="thinking.fixture_leaf_failure")

    try:
        with patch.object(case.leaf, case.attribute, side_effect=leaf), patch.object(harness.processor, "_parse_action_parameters", side_effect=owned_parse):
            start = time.perf_counter()
            response = harness.processor.execute_action(action)
            elapsed = time.perf_counter() - start
            try:
                assert entered.wait(5), f"{case.name}: valid fixture must reach {case.attribute}; jobs={harness.state.rows.get('job')}"
                job_id = assert_owned_receipt(harness, action, response, elapsed, entries)
            finally:
                release.set()
                harness.join()
        assert_typed_terminal(harness, action, job_id, terminal)
        print(f"PASS {case.name}: real_{case.attribute}_parked, receipt_ms={elapsed * 1000:.3f}, ordinary_dispatch, one_typed_terminal")
    finally:
        if case.temporary is not None:
            case.temporary.cleanup()


def all_selected_entries() -> None:
    cases = artifact_cases() + wbs_cases() + plan_cases() + lifecycle_cases() + completion_cases()
    assert len(cases) == 27 and {case.name for case in cases} == THINKING_REGISTERED_OPERATIONS
    failures: list[str] = []
    for case in cases:
        try:
            matrix_case(case)
        except Exception:
            failures.append(case.name)
            traceback.print_exc()
    assert not failures, failures


def synchronous_fifo_and_reentrancy() -> None:
    """A direct caller waits in order, and nested provider composition stays inline."""
    plugin, kb = templates.build_plugin()
    harness = Harness(plugin)
    entered, release, direct_entered = threading.Event(), threading.Event(), threading.Event()
    nested: list[dict[str, Any]] = []
    order: list[str] = []

    def read(path: str) -> str:
        if "outer" in path:
            order.append("outer")
            entered.set()
            assert release.wait(5)
            nested.append(plugin.get_plan_template("plan_template::fixture::inner"))
        elif "direct" in path:
            order.append("direct")
            direct_entered.set()
        else:
            order.append("inner")
        return kb.read(path)

    with patch.object(plugin, "_read_from_plan_templates_kb", side_effect=read), ThreadPoolExecutor(max_workers=1) as executor:
        receipt = harness.processor.execute_action(harness.action("get_plan_template", {"template_key": "plan_template::fixture::outer"}))
        assert receipt["success"] is True and entered.wait(5)
        direct = executor.submit(harness.service.get_plan_template, "plan_template::fixture::direct")
        try:
            assert not direct_entered.wait(0.03), "the direct writer must not overtake the registered owner"
        finally:
            release.set()
        result = direct.result(timeout=5)
        harness.join()
    assert result["found"] is False and "job_id" not in result
    assert nested[0]["found"] is False and order == ["outer", "inner", "direct"]
    assert len(harness.state.rows["job"]) == 1 and harness.completed == ["flow"]
    print("PASS direct_synchronous_FIFO, nested_reentrant_full_results, exactly_one_outer_job")


def first_action_and_completion_resume() -> None:
    """Keep complete run kickoff and served continuation envelopes in job.result."""
    rig = runs._Rig()
    plugin: Any = DefaultThinkingPlugin()
    plugin._joseki_run_engine = lambda: rig.engine
    harness = Harness(plugin)
    response: Any = harness.processor.execute_action(harness.action("run_joseki", {"joseki_key": "smoke_gate_run", "bindings": {"g": "ruff"}}))
    harness.join()
    job = harness.job(response["data"]["job_id"])
    assert job["status"] == "completed", job
    outcome = job["result"]
    assert len(outcome["actions"]) == 1 and outcome["actions"][0]["session_id"] == "sess-run"
    assert outcome["run_id"] and outcome["wbs_id"] and harness.completed == ["flow"]

    inference = completion._RecordingCompletionService({})
    text = '```json\n{"actions":[{"process":{"provider_type":"service_interface","provider":"thinking_service","function_name":"get_plan_template"},"arguments":{"template_key":"plan_template::fixture::owned"},"notes":"owned continuation"}]}\n```'
    inference.rows["served"] = completion._served_row("served", completion=text)
    plugin = completion._build_plugin(inference)
    harness = Harness(plugin)
    harness.services["inference_service"] = inference
    response = harness.processor.execute_action(harness.action("resume_thinking_completion", {"request_id": "served"}))
    harness.join()
    job = harness.job(response["data"]["job_id"])
    assert job["status"] == "completed", job
    assert job["result"]["data"]["planning_context_id"] == "ctx-1"
    assert job["result"]["actions"][0]["result_processor_target"] == "service_interface::thinking_service::process_planning_results"
    assert len(harness.state.rows["job"]) == 1 and harness.completed == ["flow"]
    print("PASS complete_first_action_run_envelope, trusted_served_resume_and_exact_continuation")


def private_pg_persistence() -> None:
    """Supplementary real StateManagementInterface job and artifact persistence."""
    from ananta.config.core_schemas import CoreSchemaDefinitions
    from ananta.services.schema_manager import SchemaManager
    from default_thinking_plugin.schema import NAMESPACE, get_thinking_schema

    from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture

    try:
        with ledger_fixture("fast_return_census", "thinking_registered_admission") as fixture:
            core = CoreSchemaDefinitions.get_job_schema()
            core.tables.update(CoreSchemaDefinitions.get_job_payload_schema().tables)
            core.tables.update(CoreSchemaDefinitions.get_flows_schema().tables)
            schemas = SchemaManager(fixture.state, fixture.plugin)
            schemas.initialize_schemas([core, get_thinking_schema()])
            fixture.state.set_schema_manager(schemas)
            service, _writer, _state, _focus = authored.build_service()
            service._state_store = fixture.state
            plugin: Any = DefaultThinkingPlugin()
            plugin._artifact_authoring_service = lambda _session: service
            harness = Harness(plugin)
            harness.manager = AsyncJobManager(fixture.state, harness.manager._flow_runtime_graph)
            harness.orchestrator.async_job_manager = harness.manager
            response: Any = harness.processor.execute_action(harness.action("create_resolved_intake_state", {"intake_id": "intk-private-admission", "content": authored.AUTHORED_RIS}))
            harness.join()
            assert response["success"] is True, response
            job = harness.job(response["data"]["job_id"])
            assert job["status"] == "completed" and job["result"]["intake_id"] == "intk-private-admission", job
            request_read: Any = harness.manager.get_job_payload(job["id"], "request")
            request: Any = request_read["data"]["payload"]
            assert request["parameters"] == {"input_custody": "volatile_action", "action_id": "action"}
            found: Any = fixture.state.read_state(NAMESPACE, {"table": "thinking_intake_state", "filters": {"id": "intk-private-admission"}})
            assert len(found["data"]["records"]) == 1, found
            assert harness.completed == ["flow"]
            print("PASS private_PG_atomic_canonical_job_request_terminal_artifact_and_lineage")
    except MissingFixtureError:
        print("ADVISORY SKIP private-PG persistence: run through quality_gates/provision_test_pg.py --exec")


def main() -> None:
    failures: list[str] = []
    for check in (template_admission, poller_receipt_decode, all_selected_entries, synchronous_fifo_and_reentrancy, first_action_and_completion_resume, private_pg_persistence):
        try:
            check()
        except Exception:
            failures.append(check.__name__)
            traceback.print_exc()
    assert not failures, failures


if __name__ == "__main__":
    main()
