"""Hermetic unittest coverage for the typed Zuora Workflow verbs.

httpx.MockTransport plays the tenant: every transcript below follows the
response shapes in Zuora's v1 API reference (Workflows section, reference
version 2026-11-06) — ``data`` + ``pagination.next_page`` lists, ``Errors`` /
``errors`` / ``reasons`` error envelopes, Zuora-Request-Id and RateLimit
headers. No vendor is contacted.

Run: SOLET_NAME=<name> .venv/bin/python plugins/zuora_plugin/tests/smoke_workflows.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import base64  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from collections.abc import Callable  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402
from urllib.parse import parse_qs, quote  # noqa: E402

import httpx  # noqa: E402
from ananta.file_locations.containment import PathRefusedError  # noqa: E402
from ananta.file_locations.resolution import SoletTree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from finish_support import finished  # noqa: E402  # pyright: ignore[reportMissingImports]
from zuora_plugin import async_jobs, typed_verbs, workflow_actions  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402
from zuora_plugin.typed_operations import PathGate, execute  # noqa: E402

_CLIENT_ID = "fixture-client-id"
_SECRET = "fixture-client-secret"
_TOKEN = "fixture-access-token"
_BASE = "https://tenant.example.test"
_STATE = {"session_id": "workflow-session", "flow_id": "workflow-flow"}
_HEADERS = {"Zuora-Request-Id": "req-7f3a", "RateLimit-Limit": "100", "RateLimit-Remaining": "99", "RateLimit-Reset": "60"}
_OPS = {op.name: op for op in workflow_actions.OPERATIONS}

Handler = Callable[[httpx.Request], httpx.Response]


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _bind_locations(plugin: Any, directories: list[str]) -> NonCallableMagicMock:
    """Supply a live address-book service through the production binding."""
    orchestrator = NonCallableMagicMock()
    orchestrator.APP_HOME = str(_LOCATION_TREE.app_home)
    orchestrator.plugin_manager.get_plugin.return_value = None
    orchestrator.get_service.return_value.list_path_addresses.return_value = {
        "action_status": "completed",
        "data": {"addresses": [{"name": f"project-{index}", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": directory}]} for index, directory in enumerate(directories)]},
    }
    plugin.orchestrator_ref = orchestrator
    return orchestrator


def vendor_json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body, headers=_HEADERS)


def workflow_record(workflow_id: int, *, status: str = "Active", description: str = "Nightly invoice backup") -> dict[str, Any]:
    return {
        "id": workflow_id,
        "name": f"Backup {workflow_id}",
        "description": description,
        "status": status,
        "createdAt": "2026-10-01T08:00:00.000Z",
        "updatedAt": "2026-11-01T08:00:00.000Z",
        "interval": "0 0 * * *",
        "timezone": "UTC",
        "priority": "Medium",
        "calloutTrigger": False,
        "ondemandTrigger": True,
        "scheduledTrigger": True,
        "active_version": {"id": workflow_id * 10, "definitionId": workflow_id, "version": "1.2", "type": "Workflow::Setup", "status": "Active"},
        "latest_inactive_versions": [],
    }


class TenantCase(unittest.TestCase):
    """A ZuoraClient over MockTransport plus a plugin gate rooted in a temporary workspace."""

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="zuora_workflow_smoke_")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.plugin = ZuoraPlugin()
        self.plugin.initialize({})
        _bind_locations(self.plugin, [str(self.root)])
        self.token_mints = 0
        self.requests: list[httpx.Request] = []

    @property
    def gate(self) -> PathGate:
        return self.plugin._export_path_gate  # noqa: SLF001

    def client(self, handler: Handler, *, token: str = _TOKEN, client_id: str = _CLIENT_ID, secret: str = _SECRET) -> ZuoraClient:
        client = ZuoraClient(ZuoraTenantConfig(base_url=_BASE, client_id=client_id, client_secret=secret), timeout_seconds=2)
        client._http.close()  # noqa: SLF001

        def transport(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/oauth/token":
                self.token_mints += 1
                self.assertEqual(parse_qs(req.content.decode())["client_secret"], [secret])
                return httpx.Response(200, json={"access_token": token, "token_type": "bearer", "expires_in": 3599, "scope": "user.platform", "jti": "fixture-jti"})
            self.assertEqual(req.headers["Authorization"], f"Bearer {token}")
            self.assertEqual(req.url.host, "tenant.example.test")
            self.requests.append(req)
            return handler(req)

        client._http = httpx.Client(base_url=_BASE, transport=httpx.MockTransport(transport))  # noqa: SLF001
        self.addCleanup(client.close)
        return client

    def run_op(self, name: str, client: ZuoraClient, params: dict[str, Any]) -> dict[str, Any]:
        return execute(_OPS[name], client, params, self.gate)

    def finish_op(self, name: str, client: ZuoraClient, params: dict[str, Any]) -> dict[str, Any]:
        """The published envelope: the operation, then the plugin's one finish step."""
        return finished(client, lambda c: self.run_op(name, c, params), params, name=name)

    def refuse(self, name: str, params: dict[str, Any]) -> None:
        """Dispatch and worker both refuse the input; no OAuth mint and no vendor request happen."""
        before = (self.token_mints, len(self.requests))
        client = self.client(lambda _: self.fail("no vendor request expected"))
        with self.assertRaises(ValueError):
            typed_verbs.validate_typed(name, params)
        with self.assertRaises(ValueError):
            self.run_op(name, client, params)
        self.assertEqual((self.token_mints, len(self.requests)), before)


class RegistrationTests(unittest.TestCase):
    def test_every_typed_verb_has_decorator_edge_handler_and_process_json(self) -> None:
        plugin = ZuoraPlugin()
        plugin.initialize({})
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", plugin, list(actions.values()))
        edges = plugin.get_edge_process_definitions()
        self.assertEqual(len(typed_verbs.TYPED_HANDLERS), 245)
        for name, retryable in typed_verbs.TYPED_RETRYABLE.items():
            with self.subTest(verb=name):
                self.assertIn(name, actions)
                self.assertIn(name, edges)
                errors = edges[name].error_processor_template_customizations
                results = edges[name].result_processor_template_customizations
                assert errors is not None and results is not None
                self.assertEqual(errors.retryable, retryable)
                self.assertIn(name, async_jobs.ACTION_HANDLERS)
                kb = json.loads((REPO_ROOT / "plugins/zuora_plugin/knowledge_base/processes" / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(kb["process_key"], f"plugin::zuora_plugin::{name}")
                self.assertEqual(kb["result_processor_customizations"]["result_type"], typed_verbs.result_type(name))
                self.assertEqual(results.result_type, typed_verbs.result_type(name))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400, len(kb["embedding_description"]))

    def test_write_and_delete_verbs_are_not_retryable(self) -> None:
        for name in ("update_workflow", "delete_workflow", "delete_workflow_version", "run_workflow", "stop_workflow_run", "import_workflow", "delete_workflow_auth_provider"):
            self.assertFalse(typed_verbs.TYPED_RETRYABLE[name], name)
        for name in ("list_workflows", "get_workflow", "export_workflow", "get_workflow_run"):
            self.assertTrue(typed_verbs.TYPED_RETRYABLE[name], name)


class DefinitionLifecycleTests(TenantCase):
    def test_list_pages_follow_vendor_next_page(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            page = int(req.url.params.get("page", "1"))
            next_page = f"{_BASE}/workflows?page={page + 1}&page_length=2" if page == 1 else None
            records = [workflow_record(page * 10 + offset) for offset in (1, 2)] if page == 1 else [workflow_record(21)]
            return vendor_json(200, {"data": records, "pagination": {"next_page": next_page, "page": page, "page_length": len(records)}})

        client = self.client(vendor)
        first = self.run_op("list_workflows", client, {"page_length": 2, "name": "Backup", "scheduled_trigger": True})
        self.assertEqual(first["pagination"]["page"], 1)
        self.assertEqual(first["pagination"]["next_page"], 2)
        self.assertEqual([record["id"] for record in first["data"]["data"]], [11, 12])
        second = self.run_op("list_workflows", client, {"page": first["pagination"]["next_page"], "page_length": 2})
        self.assertIsNone(second["pagination"]["next_page"])
        self.assertEqual(self.requests[0].url.params["scheduled_trigger"], "true")
        self.assertEqual(self.requests[0].url.params["name"], "Backup")
        self.assertEqual(self.requests[1].url.params["page"], "2")
        self.assertEqual(self.token_mints, 1)

    def test_list_page_length_above_vendor_maximum_is_refused_before_network(self) -> None:
        self.refuse("list_workflows", {"page_length": 51})
        self.refuse("list_workflow_tasks", {"page_length": 101})

    def test_backup_export_to_file_then_restore_by_import(self) -> None:
        export = {
            "workflow": {"id": 42, "name": "Invoice backup", "status": "Active", "version": "1.2", "interval": "0 0 * * *"},
            "tasks": [{"id": 900, "action_type": "Export", "name": "Export invoices", "parameters": {"object": "Invoice"}}],
            "linkages": [{"linkage_type": "Start", "source_workflow_id": 42, "source_task_id": None, "target_task_id": 900}],
        }
        backup = self.root / "workflow-42.json"

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "GET":
                self.assertEqual(req.url.path, "/workflows/42/export")
                self.assertEqual(req.url.params["version"], "latest")
                return vendor_json(200, export)
            self.assertEqual((req.method, req.url.path), ("POST", "/workflows/import"))
            self.assertEqual(req.url.params["activate"], "true")
            self.assertEqual(json.loads(req.content), export)
            return vendor_json(200, {"id": 77, "definitionId": 43, "definitionName": "Invoice backup", "version": "1.0", "type": "Workflow::Setup", "status": "Active"})

        client = self.client(vendor)
        exported = self.run_op("export_workflow", client, {"workflow_id": 42, "version": "latest", "output_path": str(backup)})
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), export)
        self.assertEqual(exported["file"]["path"], str(backup.resolve()))
        self.assertNotIn("data", exported)
        restored = self.run_op("import_workflow", client, {"input_path": str(backup), "activate": True})
        self.assertEqual(restored["data"]["definitionId"], 43)

    def test_export_inline_and_output_path_outside_roots_refused_before_network(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"workflow": {"id": 5}, "tasks": [], "linkages": []}))
        self.assertEqual(self.run_op("export_workflow", client, {"workflow_id": "5"})["data"]["workflow"]["id"], 5)
        self.requests.clear()
        with self.assertRaises(PathRefusedError):
            self.run_op("export_workflow", client, {"workflow_id": 5, "output_path": "/elsewhere/workflow.json"})
        with self.assertRaises(PathRefusedError):
            self.run_op("export_workflow", client, {"workflow_id": 5, "output_path": str(self.root / "workflow.txt")})
        self.assertEqual(self.requests, [])

    def test_import_needs_exactly_one_source(self) -> None:
        self.refuse("import_workflow", {})
        self.refuse("import_workflow", {"definition": {"workflow": {}}, "input_path": str(self.root / "x.json")})
        self.refuse("import_workflow_version", {"workflow_id": 42, "definition": {"workflow": {"id": 42}}})

    def test_import_version_sends_required_version_and_definition(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/workflows/42/versions/import")
            self.assertEqual(req.url.params["version"], "1.3")
            return vendor_json(200, workflow_record(42))

        result = self.run_op("import_workflow_version", self.client(vendor), {"workflow_id": 42, "version": "1.3", "definition": {"workflow": {"id": 42}, "tasks": [], "linkages": []}})
        self.assertEqual(result["status"], 200)

    def test_correct_clear_close_reopen_and_roll_back(self) -> None:
        bodies: list[dict[str, Any]] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.method, req.url.path), ("PATCH", "/workflows/42"))
            body = json.loads(req.content)
            bodies.append(body)
            return vendor_json(200, workflow_record(42, status=body.get("status", "Active"), description=body.get("description", "Nightly invoice backup")))

        client = self.client(vendor)
        self.run_op("update_workflow", client, {"workflow_id": 42, "name": "Invoice backup v2"})
        cleared = self.run_op("update_workflow", client, {"workflow_id": 42, "description": ""})
        closed = self.run_op("update_workflow", client, {"workflow_id": 42, "status": "Inactive"})
        reopened = self.run_op("update_workflow", client, {"workflow_id": 42, "status": "Active"})
        self.run_op("update_workflow", client, {"workflow_id": 42, "active_workflow_version_id": 410})
        self.assertEqual(bodies, [{"name": "Invoice backup v2"}, {"description": ""}, {"status": "Inactive"}, {"status": "Active"}, {"active_workflow_version_id": 410}])
        self.assertEqual(cleared["data"]["description"], "")
        self.assertEqual((closed["data"]["status"], reopened["data"]["status"]), ("Inactive", "Active"))
        self.refuse("update_workflow", {"workflow_id": 42})
        self.refuse("update_workflow", {"workflow_id": 42, "status": "Paused"})

    def test_stale_update_is_the_vendor_422_and_never_replayed(self) -> None:
        def vendor(_: httpx.Request) -> httpx.Response:
            return vendor_json(422, {"success": False, "reasons": [{"code": 53100320, "message": "active_workflow_version_id does not belong to this workflow"}]})

        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("update_workflow", self.client(vendor), {"workflow_id": 42, "active_workflow_version_id": 9})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("does not belong", str(raised.exception))
        self.assertEqual(len(self.requests), 1)

    def test_delete_definition_and_version_terminal_and_rbac(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/workflows/42":
                return vendor_json(200, {"id": 42, "success": True})
            if req.url.path == "/workflows/versions/410":
                return vendor_json(403, {"Errors": ["You do not have Workflow Manage Access permission"]})
            return vendor_json(404, {"Errors": ["Workflow not found"]})

        client = self.client(vendor)
        deleted = self.run_op("delete_workflow", client, {"workflow_id": 42})
        self.assertEqual(deleted["data"], {"id": 42, "success": True})
        with self.assertRaises(ZuoraServiceError) as denied:
            self.run_op("delete_workflow_version", client, {"version_id": 410})
        self.assertEqual(denied.exception.code, "zuora.permission_denied")
        self.assertIn("Workflow Manage Access", str(denied.exception))
        with self.assertRaises(ZuoraServiceError) as missing:
            self.run_op("delete_workflow", client, {"workflow_id": 43})
        self.assertEqual(missing.exception.code, "zuora.object_not_found")
        self.assertIn("Workflow not found", str(missing.exception))
        self.assertEqual([req.method for req in self.requests], ["DELETE", "DELETE", "DELETE"])

    def test_identifiers_cannot_leave_their_path_segment(self) -> None:
        for bad in ("../tasks", "42/run", "", 0, -1, True, "4 2"):
            with self.subTest(value=bad):
                self.refuse("get_workflow", {"workflow_id": bad})
        for bad in ("../../oauth", "run/stop", "a b"):
            with self.subTest(value=bad):
                self.refuse("get_workflow_run", {"workflow_run_id": bad})


class RunAndTaskTests(TenantCase):
    def test_run_poll_and_stop(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                self.assertEqual(req.url.path, "/workflows/42/run")
                self.assertEqual(json.loads(req.content), {"AccountId": "8a90e5e4", "Mode": "dry"})
                return vendor_json(200, {"id": 5501, "name": "WF-5501", "definitionId": 42, "originalWorkflowId": 42, "status": "Queued", "type": "Workflow::Instance", "tags": [], "createdAt": "2026-11-06T12:00:00Z", "updatedAt": "2026-11-06T12:00:00Z"})
            if req.method == "GET":
                self.assertEqual(req.url.path, "/workflows/workflow_runs/5501")
                return vendor_json(200, {"id": 5501, "status": "Processing", "cpuTime": 1.2, "runTime": 3.4, "messages": {}, "tasks": {"Processing": 1, "Success": 2}, "finishedAt": None})
            self.assertEqual((req.method, req.url.path), ("PUT", "/workflows/workflow_runs/5501/stop"))
            return vendor_json(200, {"success": True})

        client = self.client(vendor)
        run = self.run_op("run_workflow", client, {"workflow_id": 42, "parameters": {"AccountId": "8a90e5e4", "Mode": "dry"}})
        run_id = str(run["data"]["id"])
        self.assertEqual(self.run_op("get_workflow_run", client, {"workflow_run_id": run_id})["data"]["status"], "Processing")
        self.assertEqual(self.run_op("stop_workflow_run", client, {"workflow_run_id": run_id})["data"], {"success": True})

    def test_run_reserved_words_refused_and_conflict_mapped(self) -> None:
        for word in sorted(workflow_actions.RUN_RESERVED_PARAMETERS):
            with self.subTest(word=word):
                self.refuse("run_workflow", {"workflow_id": 42, "parameters": {word: "x"}})
        client = self.client(lambda _: vendor_json(409, {"errors": ["Workflow is already running for this object"]}))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("run_workflow", client, {"workflow_id": 42})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("already running", str(raised.exception))
        self.assertEqual(json.loads(self.requests[0].content), {})

    def test_tasks_list_filters_get_rerun_and_batch_correct(self) -> None:
        task = {"id": 900, "task_id": 900, "workflow_id": 5501, "name": "Export invoices", "action_type": "Export", "status": "Error", "error": "timeout", "error_class": "Net::ReadTimeout", "instance": True, "tags": ["nightly"]}

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/workflows/tasks":
                return vendor_json(200, {"data": [task], "pagination": {"next_page": None, "page": 1, "page_length": 1}})
            if req.url.path == "/workflows/tasks/batch_update":
                return vendor_json(200, {"data": [{**task, "name": "Export invoices (fixed)"}], "pagination": {"next_page": None, "page": 1, "page_length": 1}})
            if req.url.path.endswith("/rerun"):
                return vendor_json(200, {**task, "status": "Queued"})
            return vendor_json(200, task)

        client = self.client(vendor)
        listed = self.run_op("list_workflow_tasks", client, {"instance": True, "workflow_id": "5501", "tags": ["nightly", "billing"], "page_length": 100})
        self.assertIsNone(listed["pagination"]["next_page"])
        self.assertEqual(self.requests[0].url.params.get_list("tags[]"), ["nightly", "billing"])
        self.assertEqual(self.run_op("get_workflow_task", client, {"task_id": "900"})["data"]["error_class"], "Net::ReadTimeout")
        self.assertEqual(self.run_op("rerun_workflow_task", client, {"task_id": 900})["data"]["status"], "Queued")
        updated = self.run_op("update_workflow_tasks", client, {"tasks": [{"id": 900, "name": "Export invoices (fixed)"}]})
        self.assertEqual(json.loads(self.requests[-1].content), {"data": [{"id": 900, "name": "Export invoices (fixed)"}]})
        self.assertEqual(updated["data"]["data"][0]["name"], "Export invoices (fixed)")
        self.refuse("update_workflow_tasks", {"tasks": [{"name": "no id"}]})
        self.refuse("update_workflow_tasks", {"tasks": []})

    def test_usage_dates_and_metric(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(dict(req.url.params), {"startDate": "2026-10-01", "endDate": "2026-10-31", "metrics": "taskCount"})
            return vendor_json(200, {"metrics": [{"date": "2026-10-01", "values": {"taskCount": 120, "workflowCount": 4}}]})

        result = self.run_op("get_workflow_usage", self.client(vendor), {"start_date": "2026-10-01", "end_date": "2026-10-31"})
        self.assertEqual(result["data"]["metrics"][0]["values"]["taskCount"], 120)
        self.refuse("get_workflow_usage", {"start_date": "2026-13-01", "end_date": "2026-10-31"})
        self.refuse("get_workflow_usage", {"start_date": "2026-10-01", "end_date": "2026-10-31", "metrics": "cpu"})


class AuthTests(TenantCase):
    def test_auth_provider_lifecycle_returns_vendor_results_as_received(self) -> None:
        password = "callout pass/+?é"
        variants = [password, quote(password, safe=""), base64.b64encode(password.encode()).decode(), base64.urlsafe_b64encode(password.encode()).decode().rstrip("=")]

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                sent = json.loads(req.content)["auth_provider"]
                self.assertEqual(sent["credentials"]["password"], password)
                return httpx.Response(201, json={"id": "ap-1", "status": "created", "echo": variants}, headers=_HEADERS)
            if req.method == "PUT":
                return vendor_json(422, {"errors": [f"credentials rejected: {variant}" for variant in variants]})
            return vendor_json(200, {"status": "deleted"})

        client = self.client(vendor)
        with self.assertLogs("zuora_plugin", level="INFO") as audit:
            created = self.finish_op("create_workflow_auth_provider", client, {"name": "billing-basic", "service_name": "billing", "auth_type": "basic_auth", "credentials": {"username": "svc", "password": password}})["data"]
            rejected = self.finish_op("update_workflow_auth_provider", client, {"provider_id": "ap-1", "active": False, "credentials": {"password": password}})["error"]
            deleted = self.finish_op("delete_workflow_auth_provider", client, {"provider_id": "ap-1"})["data"]
        # The audit line is built without the request body, so the sent password never reaches it.
        for variant in variants:
            self.assertNotIn(variant, " ".join(audit.output))
        self.assertEqual(created["data"]["status"], "created")
        self.assertEqual(created["data"]["echo"], variants)
        self.assertEqual(rejected["code"], "zuora.validation_failed")
        self.assertEqual(deleted["data"], {"status": "deleted"})
        self.refuse("update_workflow_auth_provider", {"provider_id": "ap-1", "auth_type": "hmac"})
        self.refuse("create_workflow_auth_provider", {"name": "x", "service_name": "y", "auth_type": "kerberos", "credentials": {}})

    def test_expired_token_is_reminted_once_then_reported(self) -> None:
        statuses = iter((401, 200))

        def recovering(_: httpx.Request) -> httpx.Response:
            status = next(statuses)
            return vendor_json(status, workflow_record(42) if status == 200 else {"message": "Authentication error"})

        self.assertEqual(self.run_op("get_workflow", self.client(recovering), {"workflow_id": 42})["status"], 200)
        self.assertEqual(self.token_mints, 2)
        self.token_mints, self.requests = 0, []
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_workflow", self.client(lambda _: vendor_json(401, {"message": "Authentication error"})), {"workflow_id": 42})
        self.assertEqual(raised.exception.code, "zuora.auth_failed")
        self.assertEqual((self.token_mints, len(self.requests)), (2, 2))


class DeferredDispatchTests(TenantCase):
    def test_dispatch_queues_without_network_and_worker_completes(self) -> None:
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "wf-job", "status": "queued"}}
        params = {"workflow_id": 42}
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": {"notes": "zuora_plugin.get_workflow", **params}}}
        self.plugin._async_job_manager = manager  # noqa: SLF001
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(200, workflow_record(42)))  # noqa: SLF001
        with patch.object(async_jobs, "ensure_worker_started"):
            queued = self.plugin.get_workflow(params, dict(_STATE))
            self.assertEqual(queued["data"]["status"], "queued")
            self.assertEqual(self.requests, [])
            refused = self.plugin.delete_workflow({"workflow_id": "../x"}, dict(_STATE))
            self.assertEqual(refused["error"]["code"], "zuora.invalid_params")
            self.assertEqual(manager.create_job.call_count, 1)
            async_jobs._process_job(self.plugin, manager, "wf-job", "get_workflow")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "completed")
        self.assertEqual(completion["result"]["data"]["id"], 42)

    def test_worker_reports_vendor_permission_denial_as_job_error(self) -> None:
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": {"workflow_id": 42}}}
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(403, {"Errors": ["Workflow Run Access is required"]}))  # noqa: SLF001
        async_jobs._process_job(self.plugin, manager, "wf-job", "run_workflow")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "error")
        self.assertEqual(completion["error"]["code"], "zuora.permission_denied")
        self.assertIn("Workflow Run Access", completion["error"]["message"])


if __name__ == "__main__":
    unittest.main()
