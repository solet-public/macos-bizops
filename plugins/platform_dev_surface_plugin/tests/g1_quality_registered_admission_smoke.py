#!/usr/bin/env python3
"""Real quality provider admission, frozen proof lifecycle and terminal routing.

Only external operation leaves and the transactional state fixture are doubles.
Registry discovery, ActionFactory, ActionProcessor, owner, job reader and poller
completion run their production code. No gate or full battery is executed.
"""

# ruff: noqa: E402
from __future__ import annotations

import hashlib
import importlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "ananta/tests/core/actions", ROOT / "plugins/platform_dev_surface_plugin/src"):
    sys.path.insert(0, str(source))

from ananta.core.actions.action_factory import ActionFactory
from ananta.core.actions.action_processor import ActionProcessor
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction
from ananta.core.event_orchestrator import EventOrchestrator
from ananta.core.orchestration.execution_context import ExecutionContextManager
from ananta.core.orchestration.service_bindings import BindingSource, ServiceBinding, ServiceBindings, ServiceName
from ananta.core.plugins.plugin_manager import PluginManager
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner
from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType
from ananta.error_handling import FrameworkError
from ananta.services.discovery_service.service import DiscoveryService
from ananta.services.job_service.service import JobService
from async_job_atomic_completion_smoke import FixtureAction  # pyright: ignore[reportMissingImports] - retained core action fixture
from async_job_fixture_support import JobState  # pyright: ignore[reportMissingImports] - retained transactional state fixture
from full_smoke_schedule_doubles import CapturingScheduler, RegisterDouble
from platform_dev_surface_plugin.bounded_subprocess import SubprocessResult
from platform_dev_surface_plugin.plugin import PlatformDevSurfacePlugin
from platform_dev_surface_plugin.quality import full_battery, landing_verify, operations
from platform_dev_surface_plugin.quality.full_battery import FullBatteryProducer
from platform_dev_surface_plugin.quality.operations import QualityOperations

from quality_gates import smoke_proof
from quality_gates.full_smoke_campaign import CampaignStore
from quality_gates.smoke_identity import FrozenTree
from quality_gates.smoke_pass_store import SmokePassStore
from quality_gates.tests._focused_smoke_selector_fixture import FrozenProofFixture

SELECTED = frozenset(
    {
        "clear_smoke_proof",
        "correct_smoke_proof",
        "ensure_full_smoke_schedule",
        "get_smoke_proof",
        "get_smoke_proof_history",
        "list_gates",
        "list_smoke_proofs",
        "predict_gate_smokes_merge",
        "reinstate_smoke_proof",
        "retire_smoke_proof",
        "run_gate",
        "run_selected_smokes",
        "run_test",
        "select_smokes",
        "trigger_full_smoke_battery",
        "verify_hash_manifest",
    }
)


class QualityJobState(JobState):  # pyright: ignore[reportUntypedBaseClass] - retained fixture has an explicit runtime sys.path
    """The retained state double with the database's job timestamp default."""

    def prepare_record(self, namespace: str, table_name: str, record: dict[str, object]) -> dict[str, object]:
        prepared = super().prepare_record(namespace, table_name, record)
        if table_name == "job":
            prepared.setdefault("created_at", datetime.now(UTC).isoformat())
        return prepared


class LiteralTemplates:
    """Factory template transport for these literal, already bound inputs."""

    def resolve_templates(self, action: dict[str, object], _context: dict[str, object]) -> dict[str, object]:
        return action


class Harness:
    """Resolve the actual configured quality provider through the public route."""

    def __init__(self, root: Path) -> None:
        register = root / "quality_gates/gate_smokes.txt"
        register.parent.mkdir(parents=True, exist_ok=True)
        if not register.exists():
            register.write_text("# owned empty register\n", encoding="utf-8")
        self.state = QualityJobState()
        self.state.rows["flows"] = [{"id": "flow", "status": "active", "trigger_type": "bridge_process_call"}]
        graph = FlowRuntimeGraph(cast(Any, self.state))
        self.completed: list[str] = []
        graph.register_completion_callback(self.completed.append)
        self.token = graph.create_token("flow", TokenOwnerType.PROCESS, "action-0")
        self.manager = AsyncJobManager(cast(Any, self.state), graph)
        self.plugin = PlatformDevSurfacePlugin()
        self.plugin._quality = QualityOperations(ROOT, "quality-admission-fixture")
        # Production modules retain their verified checkout identity. Only
        # operation data (registers/proofs) is redirected to an owned fixture.
        self.plugin._quality._repo_root = root
        self.scheduler = CapturingScheduler()
        plugins = PluginManager()
        plugins.plugins[self.plugin.name] = self.plugin
        bindings = ServiceBindings(str(root))
        bindings._bindings = {ServiceName.QUALITY_SERVICE: ServiceBinding(ServiceName.QUALITY_SERVICE, self.plugin.name, BindingSource.CONFIG)}
        bindings._loaded = True
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager, orchestrator.service_bindings = str(root), plugins, bindings
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        orchestrator.service_manager = SimpleNamespace(scheduling_service=self.scheduler)
        self.plugin.set_orchestrator_ref(orchestrator)
        self.processes: dict[str, Any] = {}
        module = importlib.import_module("ananta.services.quality_service.interfaces.public")
        ServiceInterfaceScanner(InvocationSchemaGenerator())._scan_module_classes(module, module.__name__, self.processes)
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.processes
        contexts = ExecutionContextManager()
        contexts.create_context("flow")
        self.processor = ActionProcessor(plugins, state_service=cast(Any, self.state), orchestrator=orchestrator, process_registry={"processes": self.processes}, execution_context_manager=contexts, discovery_service=discovery)
        self.factory = ActionFactory(process_registry={"processes": self.processes}, state_service=self.state._id_service, template_engine=cast(Any, LiteralTemplates()))
        self.jobs = JobService(cast(Any, self.state), self.manager)
        self.routes: list[dict[str, Any]] = []
        self.failures: list[str] = []
        self.actions: dict[str, FixtureAction] = {}
        self.poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        self.poller.state_service, self.poller._flow_runtime_graph = self.state, self.manager._flow_runtime_graph
        self.poller._async_process_cache = {}
        self.poller._retrieve_action_details = lambda identity, **_kwargs: (self.actions[identity].process_key, "quality admission", "{}", None, "s", "flow", None, {}, "inference", None, None)
        for method in ("_update_action_status_to_completed", "_store_action_result", "_record_work_products_after_success"):
            setattr(self.poller, method, lambda *_args: None)
        self.poller._extract_and_store_attachments = lambda *_args: []
        self.poller._submit_returned_actions = lambda *_args: True
        self.poller._maybe_dispatch_result_processing = lambda **kwargs: self.routes.append(kwargs["result"])
        self.poller._retrieve_failed_action_details = lambda identity: (self.actions[identity].process_key, "{}", "quality admission", "captured error transport", "s", "flow", None, None)
        self.poller._process_error_processor_template = lambda _identity, message, *_args, **_kwargs: self.failures.append(message)
        self.manager.set_registered_completion_handler(self.poller._complete_registered_job)

    def action(self, verb: str, arguments: dict[str, Any], *, token: bool = True) -> FixtureAction:
        key = f"service_interface::quality_service::{verb}"
        # Exercise the real factory with the real discovered declaration.
        self.factory._validate_required_arguments(key, arguments)
        runtime = self.factory.create_action({"process_key": key, "arguments": arguments, "notes": "quality admission", "flow_id": "flow", "session_id": "s", "error_processor_kind": "bridge_delivery"})
        identity = f"action-{len(self.actions)}"
        graph = self.manager._flow_runtime_graph
        assert graph is not None
        token_id = (self.token if not self.actions else graph.create_token("flow", TokenOwnerType.PROCESS, identity)) if token else None
        action = FixtureAction(identity, key, json.dumps(runtime["parameters"]), "quality admission", "now", session_id="s", flow_id="flow", flow_token_id=token_id, source_plugin="trusted-quality-caller")
        self.actions[identity] = action
        self.state.rows.setdefault("action_events", []).append({"id": identity, "flow_token_id": action.flow_token_id})
        return action

    def dispatch(self, action: FixtureAction) -> dict[str, Any]:
        response = self.processor.execute_action(action)
        data = response.get("data")
        if isinstance(data, dict) and "job_id" in data:
            self.poller._mark_action_completed(action.id, response, action.flow_token_id)
        return response

    def finish(self, response: dict[str, Any], action: FixtureAction) -> dict[str, Any]:
        assert self.actions[action.id] is action
        owner = self.manager._owners["quality_service"]
        worker = owner._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "owned quality work must drain after release"
        result: Any = self.jobs.get_job(response["data"]["job_id"])
        return result["data"]["result"]["job"]

    def run(self, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
        action = self.action(verb, arguments)
        response = self.dispatch(action)
        assert response["success"] is True and "job_id" in response["data"], response
        return self.finish(response, action)


def clean_observation(key: str) -> dict[str, Any]:
    return {"key": key, "smoke": "suite/check_smoke.py", "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1}


def runner_result(entries: list[str]) -> dict[str, Any]:
    return {"exit_code": 0, "harness_defects": [], "import_environment": {"state": "verified"}, "smoke_results": {entry: {"verdict": "passed", "attempts": [{"output": "owned runner result", "verdict": "passed"}]} for entry in entries}}


def operation_arguments(verb: str, root: Path, request: dict[str, object], key: str) -> dict[str, Any]:
    arguments: dict[str, dict[str, Any]] = {
        "run_gate": {"gate": "service_interface_ast"},
        "run_test": {"smoke": "suite/check_smoke.py"},
        "verify_hash_manifest": {"unit_id": "owned-unit", "root_path": str(root), "manifest": {"data/input.csv": hashlib.sha256(b"one\n").hexdigest()}},
        "predict_gate_smokes_merge": {"base_ref": "a" * 40, "lane_root_path": str(root), "current_master": "b" * 40},
        "select_smokes": {"request": request},
        "run_selected_smokes": {"request": request},
        "get_smoke_proof": {"key": key},
        "get_smoke_proof_history": {"key": key, "as_of": 1},
        "correct_smoke_proof": {"key": key, "metadata": {"annotation": "corrected"}, "expected_version": 1, "reason": "owned fixture"},
        "clear_smoke_proof": {"key": key, "fields": ["annotation"], "clear_reuse": True, "expected_version": 1, "reason": "owned fixture"},
        "retire_smoke_proof": {"key": key, "expected_version": 1, "reason": "owned fixture"},
        "reinstate_smoke_proof": {"key": key, "request": request, "expected_version": 1, "reason": "owned fixture"},
        "trigger_full_smoke_battery": {"trigger": "manual"},
    }
    return arguments.get(verb, {})


def park_target(harness: Harness, verb: str) -> tuple[Any, str]:
    if verb == "run_gate":
        return operations, "run_bounded"
    if verb == "run_test":
        return harness.plugin._quality_ops(), "_read_smoke_register"
    if verb == "verify_hash_manifest":
        return landing_verify, "_hash_row"
    if verb == "predict_gate_smokes_merge":
        return landing_verify, "_merge_file"
    if verb in {"select_smokes", "run_selected_smokes", "reinstate_smoke_proof"}:
        return smoke_proof, "read_sources"
    if verb == "ensure_full_smoke_schedule":
        return harness.scheduler, "get_schedules_by_tag"
    if verb == "trigger_full_smoke_battery":
        return harness.plugin._quality_ops().full_battery, "_request"
    return SmokePassStore, "history"


class QualityAdmissionTests(unittest.TestCase):
    def test_registry_read_returns_before_parked_leaf_and_ordinary_dispatch(self) -> None:
        reason = "List the registered quality gates before selecting checks for this candidate."
        for case, caller_reason in (("realistic", reason), ("large_trigger", reason + " " + "x" * (1024 * 1024))):
            with self.subTest(case=case):
                self.check_flow_admission(case, caller_reason)

    def check_flow_admission(self, case: str, caller_reason: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            fixture_action = harness.action("list_gates", {})
            # The production poller does not stamp source_plugin. Use its
            # actual action type, so trusted context must read the flow.
            action = QueuedAction(fixture_action.id, fixture_action.process_key, fixture_action.parameters, fixture_action.notes, fixture_action.created_at, session_id="s", flow_id="flow", flow_token_id=fixture_action.flow_token_id)
            self.assertFalse(hasattr(action, "source_plugin"))
            trigger_data = json.dumps(
                {
                    "source_namespace": "agent_messaging_plugin",
                    "bridge_id": "agc-quality-caller",
                    "session_id": "s",
                    "process_key": action.process_key,
                    "reason": caller_reason,
                    "bridge_plugin_namespace": "agent_messaging_plugin",
                    "deliver_result_process_key": "plugin::agent_messaging_plugin::deliver_result",
                    "deliver_error_process_key": "plugin::agent_messaging_plugin::deliver_error",
                    "authenticated_principal": {"client_id": "quality-caller", "agent_instance_id": "quality-caller-instance", "bridge_id": "agc-quality-caller", "session_id": "s", "operator_equivalent": False},
                }
            )
            with harness.state.transactional() as transaction:
                self.assertEqual(transaction.update_state("core", {"table": "flows", "filters": {"id": "flow"}}, {"trigger_data": trigger_data}), 1)
            entered, release = threading.Event(), threading.Event()
            threads: list[int] = []
            caller_threads: list[int] = []
            flow_reads: list[tuple[int, dict[str, Any], bool]] = []
            original = harness.plugin._quality_ops()._read_smoke_register
            original_read = harness.state.read_state

            def tracked_read(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
                if query.get("table") == "flows":
                    flow_reads.append((threading.get_ident(), dict(query.get("filters", {})), bool(harness.state.rows.get("job"))))
                return cast(dict[str, Any], original_read(namespace, query))

            def parked() -> list[str]:
                threads.append(threading.get_ident())
                entered.set()
                assert release.wait(5), "fixture releases parked registry read"
                return original()

            def timed_dispatch() -> tuple[dict[str, Any], float]:
                caller_threads.append(threading.get_ident())
                # Full execute_action admission: includes CallContext's
                # real flow read/JSON decode and job/request persistence.
                started = time.perf_counter_ns()
                # The runtime poller type lacks the protocol-only plugin field.
                response = cast(dict[str, Any], harness.processor.execute_action(cast(Any, action)))
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                harness.poller._mark_action_completed(action.id, response, action.flow_token_id)
                return response, elapsed_ms

            with patch.object(harness.state, "read_state", side_effect=tracked_read), patch.object(harness.plugin._quality_ops(), "_read_smoke_register", side_effect=parked), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(timed_dispatch)
                try:
                    self.assertTrue(entered.wait(5), "real provider registry leaf reached")
                    self.assertTrue(wait([future], timeout=0.05).done, f"registered admission must return while registry read is parked; leaf threads={threads}")
                    response, elapsed_ms = future.result(timeout=0.05)
                    print("ADMISSION " + json.dumps({"case": case, "trigger_bytes": len(trigger_data.encode("utf-8")), "elapsed_ms": round(elapsed_ms, 3), "flow_lookup": "LOCAL_FAST transactional fixture primary-key read and JSON decode"}), flush=True)
                    self.assertTrue(any(thread == caller_threads[0] and filters == {"id": "flow"} and not had_job for thread, filters, had_job in flow_reads), "production CallContext must read the actual flow on the admitting thread before a job exists")
                    self.assertEqual(response["data"], {"job_id": harness.state.rows["job"][0]["id"], "status": "queued"})
                    self.assertEqual(harness.state.rows["job"][0]["status"], "processing")
                    ordinary = harness.dispatch(harness.action("detect_scope_regex_gaps", {"paths": ["quality_gates/run_smokes.py"]}, token=False))
                    self.assertTrue(ordinary["success"])
                    self.assertEqual(len(harness.state.rows["job"]), 1)
                    self.assertEqual(harness.routes, [])
                finally:
                    release.set()
                response, elapsed_ms = future.result(timeout=5)
            job = harness.finish(response, fixture_action)
            self.assertEqual(job["status"], "completed")
            self.assertEqual(len(harness.routes), 1)
            self.assertEqual(harness.routes[0]["gates"], job["result"]["gates"])
            self.assertEqual(harness.completed, ["flow"])
            self.assertLess(elapsed_ms, 50, f"{case}: full quality admission including flow context crosses the census 50 ms line")

    def test_other_selected_preflights_are_owned_and_keep_terminal_results(self) -> None:
        for verb in sorted(SELECTED - {"list_gates"}):
            with self.subTest(verb=verb), tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
                root = Path(directory)
                fixture = FrozenProofFixture(root)
                stack.enter_context(fixture.patches())
                stack.enter_context(patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(root / "receipts")}))
                store = SmokePassStore()
                key = smoke_proof.select_smokes(root, fixture.request(), store=store)["selected"][0]["key"]
                store.observe(key, clean_observation(key), expected_version=0, principal="owned-fixture")
                harness = Harness(root)
                producer = FullBatteryProducer(root, "quality-admission-fixture", store=CampaignStore(root / "campaigns"), register=RegisterDouble(), runner=lambda *_args: {"result": "passed", "exit_code": 0, "registered_count": 1, "summary": {"total": 1, "passed": 1}})
                harness.plugin._quality_ops().full_battery = producer
                stack.enter_context(patch.object(operations, "run_bounded", return_value=SubprocessResult(0, "owned gate result", False, 17, False)))
                stack.enter_context(patch("quality_gates.run_smokes.execute_selected", side_effect=runner_result))
                stack.enter_context(patch.object(landing_verify, "_git_show", return_value="suite/check_smoke.py\n"))
                stack.enter_context(patch.object(full_battery, "read_tree", return_value=FrozenTree("b" * 40, "d" * 40, {})))
                stack.enter_context(patch.object(full_battery, "battery_busy", return_value=False))
                self.check_parked(harness, verb, operation_arguments(verb, root, fixture.request(), key))
                with producer._slot:
                    pass  # The existing campaign worker must finish before fixture cleanup.

    def check_parked(self, harness: Harness, verb: str, arguments: dict[str, Any]) -> None:
        target, attribute = park_target(harness, verb)
        original = getattr(target, attribute)
        entered, release = threading.Event(), threading.Event()
        worker_threads: list[str] = []

        def parked(*args: Any, **kwargs: Any) -> Any:
            worker_threads.append(threading.current_thread().name)
            entered.set()
            assert release.wait(5), "test must release the operation leaf"
            return original(*args, **kwargs)

        action = harness.action(verb, arguments)
        with patch.object(target, attribute, autospec=True if target is SmokePassStore else None, side_effect=parked), ThreadPoolExecutor(max_workers=1) as executor:
            started = time.perf_counter()
            future = executor.submit(harness.dispatch, action)
            try:
                self.assertTrue(entered.wait(5), f"actual {verb} preflight reached")
                self.assertTrue(wait([future], timeout=0.05).done, f"{verb} receipt must precede leaf release")
                response = future.result()
                self.assertLess(time.perf_counter() - started, 0.05)
                self.assertTrue(response["success"], response)
                self.assertEqual(len(harness.state.rows["job"]), 1)
                self.assertEqual(response["data"], {"job_id": harness.state.rows["job"][0]["id"], "status": "queued"})
                self.assertEqual(harness.routes, [])
                self.assertTrue(all(name == "registered-quality_service" for name in worker_threads))
            finally:
                release.set()
            response = future.result(timeout=5)
            job = harness.finish(response, action)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(len(harness.routes), 1)
        self.assertTrue({"action_status", "data", "actions", "error", "timestamp"} <= response.keys())
        self.assertEqual(harness.state.rows["job"][0]["group_id"], f"registered:{action.id}")
        if verb == "run_gate":
            self.assertEqual((job["result"]["output"], job["result"]["passed"]), ("owned gate result", True))
        if verb == "run_test":
            self.assertEqual((job["result"]["output"], job["result"]["passed"]), ("owned runner result", True))
        print(f"PASS {verb}: parked complete preflight, one persisted owner, original terminal result")

    def test_registered_frozen_proof_full_lifecycle_and_principal(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            fixture = FrozenProofFixture(root)
            stack.enter_context(fixture.patches())
            stack.enter_context(patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": str(root / "receipts")}))
            stack.enter_context(patch("quality_gates.run_smokes.execute_selected", side_effect=runner_result))
            harness = Harness(root)
            selected = harness.run("select_smokes", {"request": fixture.request()})["result"]
            key = selected["selected"][0]["key"]
            run = harness.run("run_selected_smokes", {"request": fixture.request()})["result"]
            self.assertEqual((run["executed_count"], run["proofs"][0]["proof"]["reusable"]), (1, True))
            before = harness.run("get_smoke_proof_history", {"key": key})["result"]["events"]
            version = before[-1]["version"]
            common = {"key": key, "expected_version": version, "reason": "owned lifecycle"}
            corrected = harness.run("correct_smoke_proof", {**common, "metadata": {"annotation": "first", "unit_id": "owned-unit"}})["result"]
            corrected_again = harness.run("correct_smoke_proof", {**common, "expected_version": corrected["version"], "metadata": {"annotation": "second"}})["result"]
            cleared = harness.run("clear_smoke_proof", {**common, "expected_version": corrected_again["version"], "fields": ["annotation"], "clear_reuse": True})["result"]
            self.assertEqual(cleared["metadata"], {"unit_id": "owned-unit"})
            self.assertFalse(cleared["reusable"])
            retired = harness.run("retire_smoke_proof", {**common, "expected_version": cleared["version"]})["result"]
            restored = harness.run("reinstate_smoke_proof", {**common, "expected_version": retired["version"], "request": fixture.request()})["result"]
            self.assertFalse(restored["retired"])
            self.assertTrue(restored["reusable"])
            historical = harness.run("get_smoke_proof", {"key": key, "as_of": version})["result"]
            self.assertEqual((historical["metadata"], historical["version"]), ({}, version))
            after = harness.run("get_smoke_proof_history", {"key": key})["result"]["events"]
            self.assertEqual(after[: len(before)], before)
            self.assertTrue(all(row["principal"] == "plugin:trusted-quality-caller" for row in after))
            stale = harness.run("correct_smoke_proof", {**common, "metadata": {"annotation": "stale"}})
            self.assertEqual(stale["status"], "error")
            self.assertIn("stale proof version", stale["result"]["error"])
            invalid = harness.run("correct_smoke_proof", {**common, "expected_version": restored["version"], "metadata": {"key": "cannot-change-identity"}})
            self.assertEqual(invalid["status"], "error")
            wrong = dict(fixture.request(), candidate_tree="e" * 40)
            obsolete = harness.run("reinstate_smoke_proof", {**common, "expected_version": restored["version"], "request": wrong})
            self.assertEqual(obsolete["status"], "error")
            self.assertEqual(harness.run("get_smoke_proof_history", {"key": key})["result"]["events"], after)
            self.assertEqual(harness.run("list_smoke_proofs", {})["result"]["proofs"][0]["key"], key)
            with self.assertLogs("platform_dev_surface_plugin", level="INFO") as logged:
                harness.run("get_smoke_proof", {"key": key})
            self.assertIn("plugin:trusted-quality-caller", "\n".join(logged.output))
            # Missing server authorization still fails synchronously internally.
            with self.assertRaises(PermissionError):
                harness.plugin.correct_smoke_proof(key, {}, restored["version"], "no principal")
            print("PASS create/read/as-of/history, set/correct/clear, retire/reinstate, immutable history and server principal")

    def test_typed_failure_owner_loss_and_uncertain_terminal_never_replay(self) -> None:
        for mode in ("typed", "worker_loss", "terminal_write", "notification"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                harness = Harness(Path(directory))
                effects: list[str] = []

                def leaf(*, mode: str = mode, effects: list[str] = effects, harness: Harness = harness) -> list[str]:
                    effects.append(mode)
                    if mode == "typed":
                        raise FrameworkError("owned typed failure", error_code="quality.fixture_failure")
                    if mode == "worker_loss":
                        raise SystemExit("owned worker lost after effect")
                    if mode == "terminal_write":
                        harness.state.fail_write = "job_payload"
                    return []

                if mode == "notification":

                    def notification(**_kwargs: Any) -> None:
                        raise RuntimeError("owned notification transport unavailable")

                    harness.poller._maybe_dispatch_result_processing = notification
                with patch.object(harness.plugin._quality_ops(), "_read_smoke_register", side_effect=leaf):
                    job = harness.run("list_gates", {})
                self.assertEqual(effects, [mode], "uncertain operations must never be replayed")
                if mode == "terminal_write":
                    self.assertEqual(job["status"], "processing")
                    self.assertIsNone(job["result"])
                    self.assertEqual(harness.routes, [])
                    self.assertEqual(harness.completed, [])
                    harness.state.fail_write = ""
                elif mode == "notification":
                    self.assertEqual(job["status"], "completed")
                    self.assertEqual(job["result"]["smoke_count"], 0)
                    self.assertEqual(harness.completed, ["flow"])
                else:
                    self.assertEqual(job["status"], "error")
                    code = "quality.fixture_failure" if mode == "typed" else "registered_owner.worker_lost"
                    self.assertEqual(job["error"]["code"], code)
                    self.assertEqual(len(harness.failures), 1)
                print(f"PASS {mode}: exact-id result/error, no replay, terminal uncertainty remains visible")

    def test_synchronous_late_hook_and_full_battery_singleton(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            root = Path(directory)
            harness = Harness(root)
            # The real late boot hook continues to receive a full result.
            harness.plugin.set_action_factory(harness.factory)
            self.assertTrue(harness.plugin.ensure_full_smoke_schedule()["installed"])
            self.assertEqual(harness.scheduler.creates, 2)
            self.assertEqual(harness.state.rows.get("job", []), [])
            entered, release = threading.Event(), threading.Event()
            executions: list[str] = []

            def runner(campaign: dict[str, Any], _root: Path) -> dict[str, Any]:
                executions.append(campaign["campaign_id"])
                entered.set()
                assert release.wait(5), "fixture releases the existing full-battery runner"
                return {"result": "passed", "exit_code": 0, "registered_count": 1, "summary": {"total": 1, "passed": 1}}

            producer = FullBatteryProducer(root, "quality-admission-fixture", store=CampaignStore(root / "campaigns"), register=RegisterDouble(), runner=runner)
            harness.plugin._quality_ops().full_battery = producer
            stack.enter_context(patch.object(full_battery, "read_tree", return_value=FrozenTree("b" * 40, "d" * 40, {})))
            stack.enter_context(patch.object(full_battery, "battery_busy", return_value=False))
            try:
                first = harness.run("trigger_full_smoke_battery", {"trigger": "manual"})
                self.assertTrue(entered.wait(5))
                second = harness.run("trigger_full_smoke_battery", {"trigger": "manual"})
                self.assertEqual((first["result"]["status"], second["result"]["status"]), ("started", "already_running"))
                self.assertEqual(first["result"]["campaign_id"], second["result"]["campaign_id"])
                self.assertEqual(len(executions), 1)
            finally:
                release.set()
                with producer._slot:
                    pass
            self.assertEqual(len(harness.state.rows["job"]), 2)

    def test_declarations_and_bounded_volatile_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            harness = Harness(Path(directory))
            for verb in SELECTED:
                entry = harness.processes[f"service_interface::quality_service::{verb}"]
                self.assertTrue(entry["is_async"])
                self.assertTrue(entry["action_blueprint"]["metadata"]["is_async"])
                self.assertEqual(set(entry["output"]["properties"]), {"job_id", "status"})
            self.assertFalse(harness.processes["service_interface::quality_service::detect_scope_regex_gaps"]["is_async"])
            request = {"schema": "owned-opaque-request", "content": "x" * (2 * 1024 * 1024)}
            captured: list[dict[str, Any]] = []

            def produce(_root: Path, transferred: dict[str, Any]) -> dict[str, Any]:
                captured.append(transferred)
                return {"owned_length": len(transferred["content"])}

            action = harness.action("select_smokes", {"request": request})
            original = harness.processor._prepare_arguments

            def prepare(owned_action: Any) -> Any:
                self.assertEqual(threading.current_thread().name, "registered-quality_service", "payload decoding/template serialization must occur in the owner")
                return original(owned_action)

            with patch.object(smoke_proof, "select_smokes", side_effect=produce), patch.object(harness.processor, "_prepare_arguments", side_effect=prepare):
                response = harness.dispatch(action)
                job = harness.finish(response, action)
            self.assertEqual(job["result"], {"owned_length": len(request["content"])})
            self.assertEqual(captured, [request])
            persisted = harness.state.rows["job_payload"][0]["payload_data"]
            self.assertLess(len(persisted), 512, "admission must not persist or encode the caller-sized body")
            self.assertNotIn("content", persisted)
            self.assertIn("reconcile_without_replay", persisted)

    def test_private_pg_request_and_terminal_readback(self) -> None:
        from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture

        if not os.environ.get(MANIFEST_ENV):
            self.skipTest("ADVISORY private PostgreSQL absent; provision_test_pg.py --exec measures this leg")
        postgres_source = ROOT / "plugins/postgres_state_management_plugin/src"
        if not postgres_source.is_dir():
            self.skipTest("ADVISORY optional PostgreSQL provider source absent")
        sys.path.insert(0, str(postgres_source))
        from ananta.config.core_schemas import CoreSchemaDefinitions
        from ananta.core.state.async_job_manager import AsyncJobManager
        from ananta.services.schema_manager import SchemaManager

        with ledger_fixture("fast_return_census", schema="quality_registered_admission") as fixture, tempfile.TemporaryDirectory() as directory:
            schemas = SchemaManager(fixture.state, fixture.plugin)
            schemas.initialize_schemas([CoreSchemaDefinitions.get_job_schema(), CoreSchemaDefinitions.get_job_payload_schema()])
            fixture.state.set_schema_manager(schemas)
            harness = Harness(Path(directory))
            manager = AsyncJobManager(fixture.state, FlowRuntimeGraph(fixture.state))
            orchestrator = cast(Any, harness.processor.orchestrator)
            orchestrator.async_job_manager = manager
            harness.processor.state_service = fixture.state
            action = harness.action("list_gates", {})
            entered, release = threading.Event(), threading.Event()
            original = harness.plugin._quality_ops()._read_smoke_register

            def leaf() -> list[str]:
                entered.set()
                assert release.wait(5), "fixture releases actual registry reader"
                return original()

            with patch.object(harness.plugin._quality_ops(), "_read_smoke_register", side_effect=leaf), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(harness.processor.execute_action, action)
                try:
                    self.assertTrue(entered.wait(5), "real quality provider reached with private-PG job persistence")
                    self.assertTrue(wait([future], timeout=0.05).done, "private-PG admission must return before registry release")
                    response = future.result()
                    job_id = str(cast(dict[str, Any], response["data"])["job_id"])
                    reader = JobService(fixture.state, manager)
                    job = reader.get_job(job_id)["data"]["result"]["job"]
                    self.assertEqual(job["status"], "processing")
                    request: Any = manager.get_job_payload(job_id)
                    self.assertEqual(request["data"]["payload"]["parameters"]["body_storage"], "owner_memory")
                finally:
                    release.set()
                worker = manager._owners["quality_service"]._thread
                if worker is not None:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                terminal = reader.get_job(job_id)["data"]["result"]["job"]
                self.assertEqual((terminal["status"], terminal["result"]["smoke_count"]), ("completed", 0))
                print("PASS private PostgreSQL atomic request/job admission and exact-id terminal readback")


if __name__ == "__main__":
    unittest.main()
