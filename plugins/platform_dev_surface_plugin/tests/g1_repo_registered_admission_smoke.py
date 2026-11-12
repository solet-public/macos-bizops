#!/usr/bin/env python3
"""Repository admission through the real registry, factory, poller and provider.

Only persistence/output ports are doubles in the offline legs. File reads,
walks, search, Git and proposal checks use creator-owned repository fixtures.
The optional private-PG leg measures real atomic job/proposal persistence.
"""

# ruff: noqa: E402
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
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
from ananta.core.state.execution_token_context import get_current_flow_token_id
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType
from ananta.error_handling import FrameworkError
from ananta.services.discovery_service.service import DiscoveryService
from ananta.services.job_service.service import JobService
from async_job_atomic_completion_smoke import FixtureAction  # pyright: ignore[reportMissingImports] - retained core action fixture
from async_job_fixture_support import JobState  # pyright: ignore[reportMissingImports] - retained transactional state fixture
from platform_dev_surface_plugin.plugin import PlatformDevSurfacePlugin
from platform_dev_surface_plugin.quality.operations import QualityOperations
from platform_dev_surface_plugin.repo import operations
from platform_dev_surface_plugin.repo.operations import RepoOperations
from platform_dev_surface_plugin.repo.patch_store import PATCH_NAMESPACE, PATCH_TABLE, PatchStore

SELECTED = ("git_diff", "git_status", "list_files", "propose_patch", "read_file", "search")
PREFIX = "service_interface::repo_service::"
DIFF = "--- a/edit.txt\n+++ b/edit.txt\n@@ -1 +1 @@\n-old\n+new\n"
CANARY = "sk-ant-api03-" + "OWNEDCANARY1234567890abcdef"


def repository(root: Path) -> None:
    """Mutate only this creator-owned fixture, never the checkout under test."""
    (root / "edit.txt").write_text("old\n", encoding="utf-8")
    (root / "fixture.txt").write_text("\n".join(f"needle line {i}" for i in range(operations._MAX_FILE_LINES + 23)), encoding="utf-8")
    environment = {key: value for key, value in os.environ.items() if key not in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"}}
    for args in (("init", "-q"), ("add", "edit.txt", "fixture.txt"), ("-c", "user.name=owned fixture", "-c", "user.email=fixture@localhost", "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")):
        subprocess.run(["git", *args], cwd=root, env=environment, capture_output=True, text=True, check=True)


class RepoJobState(JobState):  # pyright: ignore[reportUntypedBaseClass] - retained fixture has an explicit runtime sys.path
    """Retain the production job timestamp default in the state double."""

    def prepare_record(self, namespace: str, table_name: str, record: dict[str, object]) -> dict[str, object]:
        prepared = super().prepare_record(namespace, table_name, record)
        if table_name == "job":
            prepared.setdefault("created_at", datetime.now(UTC).isoformat())
        return prepared


class LiteralTemplates:
    def resolve_templates(self, action: dict[str, object], _context: dict[str, object]) -> dict[str, object]:
        return action


class Harness:
    """Bind the actual local-profile provider; keep production dispatch code."""

    def __init__(self, root: Path) -> None:
        self.state = RepoJobState()
        self.state.rows["flows"] = [{"id": "flow", "status": "active", "trigger_type": "bridge_process_call"}]
        self.trace = SimpleNamespace(actions={}, routes=[], failures=[], completed=[], receipts={}, times={})
        graph = FlowRuntimeGraph(cast(Any, self.state))
        graph.register_completion_callback(self.trace.completed.append)
        self.manager = AsyncJobManager(cast(Any, self.state), graph)
        self.plugin = PlatformDevSurfacePlugin()
        self.plugin._repo = RepoOperations(root, PatchStore(cast(Any, self.state)))
        self.plugin._quality = QualityOperations(ROOT, "repo-admission-fixture")
        self.plugin.set_ready()
        plugins = PluginManager()
        plugins.plugins[self.plugin.name] = self.plugin
        bindings = ServiceBindings(str(root))
        bindings._bindings = {name: ServiceBinding(name, self.plugin.name, BindingSource.CONFIG) for name in (ServiceName.REPO_SERVICE, ServiceName.QUALITY_SERVICE)}
        bindings._loaded = True
        orchestrator: Any = EventOrchestrator.__new__(EventOrchestrator)
        orchestrator.APP_HOME, orchestrator.plugin_manager, orchestrator.service_bindings = str(root), plugins, bindings
        orchestrator.async_job_manager, orchestrator.action_recorder = self.manager, None
        self.plugin.set_orchestrator_ref(orchestrator)
        self.processes: dict[str, Any] = {}
        scanner = ServiceInterfaceScanner(InvocationSchemaGenerator())
        for provider in ("repo_service", "quality_service"):
            module = importlib.import_module(f"ananta.services.{provider}.interfaces.public")
            scanner._scan_module_classes(module, module.__name__, self.processes)
        discovery: Any = DiscoveryService.__new__(DiscoveryService)
        discovery.processes = self.processes
        contexts = ExecutionContextManager()
        contexts.create_context("flow")
        self.processor = ActionProcessor(plugins, state_service=cast(Any, self.state), orchestrator=orchestrator, process_registry={"processes": self.processes}, execution_context_manager=contexts, discovery_service=discovery)
        self.factory = ActionFactory(process_registry={"processes": self.processes}, state_service=self.state._id_service, template_engine=cast(Any, LiteralTemplates()))
        self.jobs = JobService(cast(Any, self.state), self.manager)
        self.poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        self.poller.state_service, self.poller._flow_runtime_graph = self.state, graph
        self.poller.action_processor, self.poller._discovery_service = self.processor, discovery
        self.poller.execution_context_manager, self.poller._async_process_cache = contexts, {}
        self.poller._retrieve_action_details = lambda identity, **_kwargs: (self.trace.actions[identity].process_key, "repo admission", "{}", None, "s", "flow", None, {}, "inference", None, None)
        self.poller._update_action_status_to_completed = lambda _identity, on_status_written=None: on_status_written() if on_status_written else None
        self.poller._store_action_result = lambda *_args: None
        self.poller._record_work_products_after_success = lambda *_args: None
        self.poller._extract_and_store_attachments = lambda *_args: []
        self.poller._submit_returned_actions = lambda *_args: True
        self.poller._maybe_dispatch_result_processing = lambda **kwargs: self.trace.routes.append(kwargs["result"])
        self.poller._retrieve_failed_action_details = lambda identity: (self.trace.actions[identity].process_key, "{}", "repo admission", "captured error transport", "s", "flow", None, None)
        self.poller._process_error_processor_template = lambda _identity, message, *_args, **_kwargs: self.trace.failures.append(message)
        self.manager.set_registered_completion_handler(self.poller._complete_registered_job)

    def action(self, verb: str, arguments: dict[str, Any], *, token: bool = True) -> FixtureAction:
        key = PREFIX + verb if verb in SELECTED else "service_interface::quality_service::detect_scope_regex_gaps"
        self.factory._validate_required_arguments(key, arguments)
        runtime = self.factory.create_action({"process_key": key, "arguments": arguments, "notes": "repo admission", "flow_id": "flow", "session_id": "s", "error_processor_kind": "bridge_delivery"})
        identity = f"action-{len(self.trace.actions)}"
        graph = self.manager._flow_runtime_graph
        assert graph is not None
        token_id = graph.create_token("flow", TokenOwnerType.PROCESS, identity) if token else None
        action = FixtureAction(identity, key, " \n" + json.dumps(runtime["parameters"], indent=1) + " \n", "repo admission", "now", session_id="s", flow_id="flow", flow_token_id=token_id, source_plugin="trusted-repo-caller")
        self.trace.actions[identity] = action
        self.state.rows.setdefault("action_events", []).append({"id": identity, "flow_token_id": token_id})
        return action

    def dispatch(self, action: FixtureAction) -> dict[str, Any]:
        """Real transport preflight and result routing, with observed storage ports."""
        original_mark = self.poller._mark_action_completed

        def completed(identity: str, result: dict[str, Any], token: str | None, *, on_status_written: Any = None, owned_job_completion: bool = False) -> None:
            if not owned_job_completion:
                self.trace.receipts[identity] = result
            original_mark(identity, result, token, on_status_written=on_status_written, owned_job_completion=owned_job_completion)

        before = action.parameters
        with ExitStack() as stack:
            stack.enter_context(patch.object(self.poller, "_is_terminated_flow_sibling", return_value=False))
            stack.enter_context(patch.object(self.poller, "_resolve_io_context", return_value=None))
            stack.enter_context(patch.object(self.poller, "_mark_action_completed", side_effect=completed))
            started = time.perf_counter_ns()
            asyncio.run(self.poller._process_action(action))
            self.trace.times[action.id] = (time.perf_counter_ns() - started) / 1_000_000
        assert action.parameters != before, "real poller preflight canonicalizes the transport body"
        return self.trace.receipts[action.id]

    def finish(self, response: dict[str, Any]) -> dict[str, Any]:
        owner = self.manager._owners["repo_service"]
        worker = owner._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "creator must drain the owned repository worker"
        return cast(dict[str, Any], self.jobs.get_job(response["data"]["job_id"])["data"]["result"]["job"])

    def run(self, verb: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = self.dispatch(self.action(verb, arguments))
        assert response["success"] and "job_id" in response["data"], response
        return self.finish(response)


def arguments(verb: str) -> dict[str, Any]:
    return {"read_file": {"path": "fixture.txt"}, "list_files": {"path": ".", "depth": 2}, "search": {"query": "needle", "max_results": 3}, "propose_patch": {"unified_diff": DIFF}}.get(verb, {})


def park_target(harness: Harness, verb: str) -> tuple[Any, str]:
    if verb == "read_file":
        return Path, "read_text"
    if verb == "search":
        return operations, "run_bounded"
    return harness.plugin._repo_ops(), {"list_files": "_walk", "propose_patch": "_git_apply_check"}.get(verb, "_git")


@unittest.skipUnless(shutil.which("git"), "ADVISORY Git absent; repository fixture cannot run")
class RepoAdmissionTests(unittest.TestCase):
    def test_01_registered_methods_return_before_parked_leaves(self) -> None:
        for verb in SELECTED:
            with self.subTest(verb=verb), tempfile.TemporaryDirectory(prefix="repo-admission-") as directory:
                root = Path(directory)
                repository(root)
                self.check_parked(Harness(root), verb)

    def check_parked(self, harness: Harness, verb: str) -> None:
        action = harness.action(verb, arguments(verb))
        target, attribute = park_target(harness, verb)
        original, prepare = getattr(target, attribute), harness.processor._prepare_arguments
        entered, release = threading.Event(), threading.Event()
        leaf_threads: list[str] = []
        parse_threads: list[str] = []

        def leaf(*args: Any, **kwargs: Any) -> Any:
            if target is Path and args[0] != harness.plugin._repo_ops()._root / "fixture.txt":
                return original(*args, **kwargs)
            leaf_threads.append(threading.current_thread().name)
            self.assertEqual(get_current_flow_token_id(), action.flow_token_id)
            entered.set()
            self.assertTrue(release.wait(5), "creator releases the actual operation leaf")
            return original(*args, **kwargs)

        def prepared(queued: Any) -> Any:
            if queued.id == action.id:
                parse_threads.append(threading.current_thread().name)
            return prepare(queued)

        with patch.object(target, attribute, autospec=True if target is Path else None, side_effect=leaf), patch.object(harness.processor, "_prepare_arguments", side_effect=prepared), ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(harness.dispatch, action)
            try:
                self.assertTrue(entered.wait(5), f"valid public {verb} reached its actual leaf")
                print(f"REACHED {verb} leaf_threads={leaf_threads}", flush=True)
                self.assertTrue(wait([future], timeout=0.05).done, "registered_repo_receipt_precedes_parked_operation_leaf")
                response = future.result()
                self.assertLess(harness.trace.times[action.id], 50, "real poller/preflight admission crosses the census 50 ms line")
                self.assertEqual(response["data"], {"job_id": harness.state.rows["job"][0]["id"], "status": "queued"})
                self.assertEqual(harness.state.rows["job"][0]["status"], "processing")
                self.assertTrue({"action_status", "data", "actions", "error", "timestamp"} <= response.keys())
                self.assertEqual(harness.trace.routes, [])
                self.assertEqual(parse_threads, ["registered-repo_service"])
                self.assertTrue(all(name == "registered-repo_service" for name in leaf_threads))
                ordinary = harness.dispatch(harness.action("detect_scope_regex_gaps", {"paths": ["fixture.txt"]}, token=False))
                self.assertTrue(ordinary["success"])
                self.assertFalse(release.is_set(), "an ordinary registered action dispatches while repository work remains parked")
                self.assertEqual(len(harness.state.rows["job"]), 1)
            finally:
                release.set()
            response = future.result(timeout=5)
        job = harness.finish(response)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(job["group_id"], f"registered:{action.id}")
        self.assertEqual(job["flow_token_id"], action.flow_token_id)
        metadata = json.loads(job["metadata"])
        self.assertEqual((metadata["registered_action_id"], metadata["flow_id"], metadata["session_id"]), (action.id, "flow", "s"))
        expected = getattr(harness.plugin, verb)(**arguments(verb)) if verb != "propose_patch" else job["result"]
        self.assertEqual(job["result"], expected)
        self.assertEqual(sum("job_id" not in row.get("data", {}) for row in harness.trace.routes), 2)
        self.assertEqual(harness.trace.completed, ["flow"])
        self.assertEqual(harness.manager.update_job(job["id"], {"status": "completed", "result": {"stale": True}})["action_status"], "error")
        self.assertEqual(harness.finish(response)["result"], expected)
        print(f"PASS {verb}: real preflight {harness.trace.times[action.id]:.3f} ms, full terminal outcome, one owner completion")

    def test_bounded_shapes_confinement_scrub_and_proposal_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repo-controls-") as directory, tempfile.TemporaryDirectory(prefix="repo-outside-") as outside:
            root = Path(directory)
            repository(root)
            (root / "secret.txt").write_text(f"API_KEY = {CANARY}\n", encoding="utf-8")
            (root / "profile").mkdir()
            (root / "profile/private.txt").write_text("excluded", encoding="utf-8")
            (root / "escape").symlink_to(outside, target_is_directory=True)
            harness = Harness(root)
            read = harness.run("read_file", {"path": "fixture.txt"})["result"]
            self.assertEqual((read["total_lines"], len(read["content"].splitlines()), read["truncated"]), (operations._MAX_FILE_LINES + 23, operations._MAX_FILE_LINES, True))
            found = harness.run("search", {"query": "API_KEY"})["result"]
            self.assertTrue(found["hits"])
            self.assertNotIn(CANARY, json.dumps(found))
            self.assertIn("[REDACTED", found["hits"][0]["snippet"])
            for path, message in (("../escape", "OUTSIDE"), ("escape/anything", "OUTSIDE"), ("profile/private.txt", "denylist"), ("secret.txt", "credential-shaped")):
                failed = harness.run("read_file", {"path": path})
                self.assertEqual(failed["status"], "error")
                self.assertIn(message.casefold(), str(failed["error"]).casefold())
                self.assertNotIn(CANARY, json.dumps(failed))
            first = harness.run("propose_patch", {"unified_diff": DIFF})["result"]
            second = harness.run("propose_patch", {"unified_diff": DIFF.replace("-old", "-obsolete")})["result"]
            self.assertEqual((first["applies_cleanly"], second["applies_cleanly"]), (True, False))
            self.assertNotEqual(first["patch_id"], second["patch_id"])
            original = harness.state.read_state(PATCH_NAMESPACE, {"table": PATCH_TABLE, "filters": {"id": first["patch_id"]}})["data"]["records"][0]
            self.assertEqual((original["unified_diff"], original["principal"], original["paths"]), (DIFF, "plugin:trusted-repo-caller", ["edit.txt"]))
            self.assertEqual((root / "edit.txt").read_text(), "old\n", "proposals never apply their diff")
            rejected = harness.run("git_diff", {"ref": f"--output={root / 'forbidden'}"})
            self.assertEqual(rejected["status"], "error")
            self.assertFalse((root / "forbidden").exists())

    def test_serial_proposals_and_synchronous_reentrant_composition(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repo-order-") as directory:
            root = Path(directory)
            repository(root)
            harness = Harness(root)
            entered, release = threading.Event(), threading.Event()
            checks: list[str] = []
            original = harness.plugin._repo_ops()._git_apply_check

            def leaf(body: str) -> bool:
                checks.append(body)
                self.assertEqual(harness.plugin.read_file("edit.txt")["content"], "old")
                if len(checks) == 1:
                    entered.set()
                    self.assertTrue(release.wait(5))
                return original(body)

            with patch.object(harness.plugin._repo_ops(), "_git_apply_check", side_effect=leaf):
                try:
                    first = harness.dispatch(harness.action("propose_patch", {"unified_diff": DIFF}))
                    self.assertTrue(entered.wait(5))
                    second_body = DIFF.replace("+new", "+corrected")
                    second = harness.dispatch(harness.action("propose_patch", {"unified_diff": second_body}))
                    self.assertEqual(checks, [DIFF], "successor proposal cannot overtake its serial owner")
                    self.assertEqual(harness.jobs.get_job(second["data"]["job_id"])["data"]["result"]["job"]["status"], "queued")
                finally:
                    release.set()
                jobs = [harness.finish(response) for response in (first, second)]
            self.assertEqual(checks, [DIFF, second_body])
            self.assertEqual(len(harness.state.rows["job"]), 2, "internal composition makes no nested job")
            self.assertNotEqual(jobs[0]["result"]["patch_id"], jobs[1]["result"]["patch_id"])
            self.assertEqual(harness.plugin.read_file("edit.txt"), {"path": "edit.txt", "content": "old", "truncated": False, "total_lines": 1})

    def test_typed_failure_worker_loss_and_terminal_uncertainty_never_replay(self) -> None:
        for mode in ("typed", "worker_loss", "terminal_write", "notification"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="repo-failure-") as directory:
                root = Path(directory)
                repository(root)
                harness = Harness(root)
                effects: list[str] = []

                def leaf(*_args: Any, mode: str = mode, harness: Harness = harness, effects: list[str] = effects) -> bool:
                    effects.append(mode)
                    if mode == "typed":
                        raise FrameworkError("owned repository failure", error_code="repo.fixture_failure")
                    if mode == "worker_loss":
                        raise SystemExit("owned worker lost after effect")
                    if mode == "terminal_write":
                        harness.state.fail_write = "job_payload"
                    return True

                if mode == "notification":

                    def notification(**_kwargs: Any) -> None:
                        raise RuntimeError("owned notification unavailable")

                    harness.poller._maybe_dispatch_result_processing = notification
                with patch.object(harness.plugin._repo_ops(), "_git_apply_check", side_effect=leaf):
                    job = harness.run("propose_patch", {"unified_diff": DIFF})
                self.assertEqual(effects, [mode], "entered effects never replay")
                if mode == "terminal_write":
                    self.assertEqual((job["status"], job["result"], harness.trace.completed), ("processing", None, []))
                    harness.state.fail_write = ""
                elif mode == "notification":
                    self.assertEqual((job["status"], harness.trace.completed), ("completed", ["flow"]))
                    self.assertTrue(job["result"]["applies_cleanly"])
                else:
                    self.assertEqual(job["status"], "error")
                    self.assertEqual(job["error"]["code"], "repo.fixture_failure" if mode == "typed" else "registered_owner.worker_lost")
                    self.assertEqual(len(harness.trace.failures), 1)

    def test_large_input_is_decoded_only_in_owner_and_request_stays_bounded(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repo-payload-") as directory:
            root = Path(directory)
            repository(root)
            harness = Harness(root)
            action = harness.action("propose_patch", {"unified_diff": DIFF + "\n" * 300_000})
            prepare = harness.processor._prepare_arguments

            def owner_prepare(queued: Any) -> Any:
                self.assertEqual(threading.current_thread().name, "registered-repo_service")
                return prepare(queued)

            with patch.object(harness.processor, "_prepare_arguments", side_effect=owner_prepare):
                response = harness.dispatch(action)
                job = harness.finish(response)
            self.assertEqual(job["status"], "completed")
            request: Any = harness.manager.get_job_payload(response["data"]["job_id"])
            persisted = json.dumps(request["data"]["payload"])
            self.assertLess(len(persisted), 512)
            self.assertNotIn("unified_diff", persisted)
            self.assertIn("reconcile_without_replay", persisted)

    def test_declarations_and_server_context(self) -> None:
        with tempfile.TemporaryDirectory(prefix="repo-context-") as directory:
            root = Path(directory)
            repository(root)
            harness = Harness(root)
            for verb in SELECTED:
                entry = harness.processes[PREFIX + verb]
                self.assertTrue(entry["is_async"] and entry["action_blueprint"]["metadata"]["is_async"])
                self.assertEqual(set(entry["output"]["properties"]), {"job_id", "status"})
            source = harness.action("propose_patch", {"unified_diff": DIFF})
            action = QueuedAction(source.id, source.process_key, source.parameters, source.notes, source.created_at, session_id="s", flow_id="flow", flow_token_id=source.flow_token_id)
            harness.state.rows["flows"][0]["trigger_data"] = json.dumps({"authenticated_principal": {"client_id": "owned-external-repo", "operator_equivalent": False}})
            response = harness.dispatch(cast(Any, action))
            job = harness.finish(response)
            proposal = harness.state.read_state(PATCH_NAMESPACE, {"table": PATCH_TABLE, "filters": {"id": job["result"]["patch_id"]}})["data"]["records"][0]
            self.assertEqual(proposal["principal"], "external:owned-external-repo")

    def test_private_pg_atomic_request_proposal_and_exact_id_readback(self) -> None:
        from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture

        if not os.environ.get(MANIFEST_ENV):
            self.skipTest("ADVISORY private PostgreSQL absent; provision_test_pg.py --exec measures this leg")
        postgres_source = ROOT / "plugins/postgres_state_management_plugin/src"
        if not postgres_source.is_dir():
            self.skipTest("ADVISORY optional PostgreSQL provider source absent")
        sys.path.insert(0, str(postgres_source))
        from ananta.config.core_schemas import CoreSchemaDefinitions
        from ananta.services.schema_manager import SchemaManager

        with ledger_fixture("fast_return_census", schema="repo_registered_admission") as fixture, tempfile.TemporaryDirectory(prefix="repo-pg-") as directory:
            schemas = SchemaManager(fixture.state, fixture.plugin)
            plugin = PlatformDevSurfacePlugin()
            schemas.initialize_schemas([CoreSchemaDefinitions.get_job_schema(), CoreSchemaDefinitions.get_job_payload_schema(), *plugin.get_schema_definitions()])
            fixture.state.set_schema_manager(schemas)
            root = Path(directory)
            repository(root)
            harness = Harness(root)
            manager = AsyncJobManager(fixture.state, FlowRuntimeGraph(fixture.state))
            cast(Any, harness.processor.orchestrator).async_job_manager = manager
            harness.processor.state_service = fixture.state
            harness.plugin._repo = RepoOperations(root, PatchStore(cast(Any, fixture.state)))
            action = harness.action("propose_patch", {"unified_diff": DIFF})
            entered, release = threading.Event(), threading.Event()
            original = harness.plugin._repo_ops()._git_apply_check

            def leaf(body: str) -> bool:
                entered.set()
                self.assertTrue(release.wait(5))
                return original(body)

            with patch.object(harness.plugin._repo_ops(), "_git_apply_check", side_effect=leaf), ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(harness.processor.execute_action, action)
                try:
                    self.assertTrue(entered.wait(5), "real repository provider reached with private-PG admission")
                    self.assertTrue(wait([future], timeout=0.05).done, "private-PG job receipt precedes proposal check")
                    response = future.result()
                    identity = str(cast(dict[str, Any], response["data"])["job_id"])
                    reader = JobService(fixture.state, manager)
                    self.assertEqual(reader.get_job(identity)["data"]["result"]["job"]["status"], "processing")
                    request: Any = manager.get_job_payload(identity)
                    self.assertEqual(request["data"]["payload"]["parameters"]["body_storage"], "owner_memory")
                finally:
                    release.set()
                worker = manager._owners["repo_service"]._thread
                if worker is not None:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                job = reader.get_job(identity)["data"]["result"]["job"]
                self.assertEqual(job["status"], "completed")
                result: Any = job["result"]
                proposal_read: Any = fixture.state.read_state(PATCH_NAMESPACE, {"table": PATCH_TABLE, "filters": {"id": result["patch_id"]}, "limit": 1})
                self.assertEqual(proposal_read["action_status"], "completed")
                rows = proposal_read["data"]["records"]
                self.assertEqual((len(rows), rows[0]["unified_diff"], rows[0]["principal"]), (1, DIFF, "plugin:trusted-repo-caller"))
                self.assertEqual((root / "edit.txt").read_text(), "old\n")
                print("PASS private PostgreSQL atomic job/request, proposal identity and exact-id terminal readback")


if __name__ == "__main__":
    unittest.main()
