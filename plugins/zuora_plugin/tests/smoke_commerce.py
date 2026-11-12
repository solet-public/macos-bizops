"""Hermetic unittest coverage for the typed Zuora Orders, Subscriptions and Accounts verbs.

httpx.MockTransport plays the tenant. Every transcript follows the response
fields in Zuora's v1 API reference (Orders, Subscriptions and Accounts
sections, reference version 2026-11-06): ``success`` plus ``processId``,
``reasons[{code, message}]`` and ``requestId`` error envelopes (on a 4xx and
on an HTTP 200 carrying ``success: false``), top-level ``nextPage`` URLs on
page-numbered lists, ``jobId``/``status`` async jobs, and Zuora-Request-Id and
RateLimit headers. No vendor is contacted.

Run: SOLET_NAME=<name> .venv/bin/python plugins/zuora_plugin/tests/smoke_commerce.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from collections.abc import Callable  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

import httpx  # noqa: E402
from ananta.file_locations.resolution import SoletTree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from finish_support import finished  # noqa: E402  # pyright: ignore[reportMissingImports]
from zuora_plugin import account_actions, async_jobs, billing_actions, data_query_actions, order_actions, typed_verbs  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402
from zuora_plugin.typed_operations import Operation, PathGate, execute  # noqa: E402

_CLIENT_ID = "fixture-client-id"
_SECRET = "fixture-client-secret"
_TOKEN = "fixture-access-token"
_BASE = "https://tenant.example.test"
_STATE = {"session_id": "commerce-session", "flow_id": "commerce-flow"}
_HEADERS = {"Zuora-Request-Id": "8f1e2d3c-req", "RateLimit-Limit": "40", "RateLimit-Remaining": "39", "RateLimit-Reset": "1"}
_OPS: dict[str, Operation] = {op.name: op for op in (*order_actions.ORDER_OPERATIONS, *order_actions.SUBSCRIPTION_OPERATIONS, *account_actions.ACCOUNT_OPERATIONS, *data_query_actions.OPERATIONS)}
_NEW_VERBS = {op.name for op in (*order_actions.ORDER_OPERATIONS, *order_actions.SUBSCRIPTION_OPERATIONS, *account_actions.ACCOUNT_OPERATIONS)}

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


def refusal(code: int, message: str) -> dict[str, Any]:
    """Zuora's v1 error envelope."""
    return {"success": False, "processId": "5F4B6A7C8D9E0F1A", "reasons": [{"code": code, "message": message}], "requestId": "41a3b4c5-d6e7-4f80-9a1b-2c3d4e5f6a7b"}


def order_result(number: str, status: str = "Completed") -> dict[str, Any]:
    return {
        "success": True,
        "orderId": "8a90e5e48c1f4e2b018c2a7d1e3f0001",
        "orderNumber": number,
        "accountNumber": "A00000101",
        "accountId": "8a90e5e48c1f4e2b018c2a7d1e3f0aaa",
        "status": status,
        "subscriptionNumbers": ["A-S00000201"],
        "invoiceNumbers": [],
        "creditMemoNumbers": [],
        "orderLineItems": [],
    }


def order_record(number: str, status: str = "Completed") -> dict[str, Any]:
    return {
        "orderNumber": number,
        "orderDate": "2026-11-01",
        "status": status,
        "category": "NewSales",
        "existingAccountNumber": "A00000101",
        "createdDate": "2026-11-01 10:00:00",
        "updatedDate": "2026-11-01 10:00:00",
        "subscriptions": [{"subscriptionNumber": "A-S00000201", "orderActions": [{"type": "CreateSubscription", "sequence": 0}]}],
        "orderLineItems": [],
        "customFields": {},
    }


_ORDER_BODY: dict[str, Any] = {
    "orderDate": "2026-11-01",
    "existingAccountNumber": "A00000101",
    "subscriptions": [
        {
            "orderActions": [
                {
                    "type": "CreateSubscription",
                    "triggerDates": [{"name": "ContractEffective", "triggerDate": "2026-11-01"}],
                    "createSubscription": {"terms": {"initialTerm": {"termType": "EVERGREEN"}}, "subscribeToRatePlans": [{"productRatePlanId": "8a90e5e48c1f4e2b018c2a7d1e3f0prp"}]},
                }
            ]
        }
    ],
    "processingOptions": {"runBilling": False},
}


class TenantCase(unittest.TestCase):
    """A ZuoraClient over MockTransport plus a plugin gate rooted in a temporary workspace."""

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="zuora_commerce_smoke_")
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

    def client(self, handler: Handler) -> ZuoraClient:
        client = ZuoraClient(ZuoraTenantConfig(base_url=_BASE, client_id=_CLIENT_ID, client_secret=_SECRET), timeout_seconds=2)
        client._http.close()  # noqa: SLF001

        def transport(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/oauth/token":
                self.token_mints += 1
                self.assertEqual(parse_qs(req.content.decode())["client_secret"], [_SECRET])
                return httpx.Response(200, json={"access_token": _TOKEN, "token_type": "bearer", "expires_in": 3599, "scope": "user.platform", "jti": "fixture-jti"})
            self.assertEqual(req.headers["Authorization"], f"Bearer {_TOKEN}")
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
    def test_every_commerce_verb_has_decorator_edge_handler_and_process_json(self) -> None:
        plugin = ZuoraPlugin()
        plugin.initialize({})
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", plugin, list(actions.values()))
        edges = plugin.get_edge_process_definitions()
        self.assertEqual(len(_NEW_VERBS), 43)
        for name in sorted(_NEW_VERBS):
            with self.subTest(verb=name):
                self.assertIn(name, actions)
                self.assertIn(name, async_jobs.ACTION_HANDLERS)
                errors = edges[name].error_processor_template_customizations
                assert errors is not None
                self.assertEqual(errors.retryable, _OPS[name].method == "GET")
                kb = json.loads((REPO_ROOT / "plugins/zuora_plugin/knowledge_base/processes" / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(kb["process_key"], f"plugin::zuora_plugin::{name}")
                self.assertEqual(kb["description"], actions[name].description)
                self.assertEqual(kb["result_processor_customizations"]["result_type"], typed_verbs.result_type(name))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400, len(kb["embedding_description"]))
                declared = set(actions[name].parameters)
                fields = _OPS[name]
                expected = {*fields.path_fields, *fields.query_fields, *fields.header_fields} | ({"body"} if fields.body is not None else set())
                self.assertEqual(declared, expected)

    def test_every_v1_commerce_route_checks_the_success_envelope(self) -> None:
        for name in _NEW_VERBS:
            self.assertTrue(_OPS[name].envelope, name)


class OrderTests(TenantCase):
    def test_create_order_sends_body_headers_and_query_and_returns_vendor_result(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.method, req.url.path), ("POST", "/v1/orders"))
            self.assertEqual(req.url.params["returnIds"], "true")
            self.assertEqual(req.headers["Idempotency-Key"], "order-2026-11-01-a")
            self.assertEqual(req.headers["Zuora-Version"], "329.0")
            self.assertEqual(req.headers["Zuora-Track-Id"], "trace-order-1")
            self.assertNotIn("Zuora-Org-Ids", req.headers)
            self.assertEqual(json.loads(req.content), _ORDER_BODY)
            return vendor_json(200, order_result("O-00000301"))

        result = self.run_op(
            "create_order",
            self.client(vendor),
            {"body": _ORDER_BODY, "return_ids": True, "idempotency_key": "order-2026-11-01-a", "zuora_version": "329.0", "zuora_track_id": "trace-order-1"},
        )
        self.assertEqual(result["data"]["orderNumber"], "O-00000301")
        self.assertEqual((result["method"], result["path"], result["status"]), ("POST", "/v1/orders", 200))

    def test_success_false_on_http_200_is_a_typed_error_never_a_completion(self) -> None:
        client = self.client(lambda _: vendor_json(200, refusal(53100020, "The order date must not be before the account's effective date.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("create_order", client, {"body": _ORDER_BODY})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("effective date", str(raised.exception))
        self.assertEqual(len(self.requests), 1)

    def test_required_body_fields_and_shapes_refused_before_network(self) -> None:
        self.refuse("create_order", {})
        self.refuse("create_order", {"body": {"existingAccountNumber": "A00000101"}})
        self.refuse("preview_order", {"body": {"orderDate": "2026-11-01"}})
        self.refuse("create_order", {"body": ["not", "an", "object"]})
        self.refuse("cancel_subscription", {"subscription_key": "A-S00000201", "body": {"cancellationEffectiveDate": "2026-12-01"}})
        self.refuse("create_account", {"body": {"name": "Acme", "currency": "USD"}})

    def test_list_orders_follows_vendor_next_page_url(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            page = int(req.url.params["page"])
            self.assertEqual(req.url.params["pageSize"], "2")
            self.assertEqual(req.url.params["dateFilterOption"], "updatedDate")
            body: dict[str, Any] = {"success": True, "orders": [order_record(f"O-0000030{page}{n}") for n in (1, 2)] if page == 1 else [order_record("O-00000321")]}
            if page == 1:
                body["nextPage"] = f"{_BASE}/v1/orders?page=2&pageSize=2&dateFilterOption=updatedDate"
            return vendor_json(200, body)

        client = self.client(vendor)
        first = self.run_op("list_orders", client, {"page": 1, "page_size": 2, "date_filter_option": "updatedDate", "start_date": "2026-10-01"})
        self.assertEqual(first["pagination"]["next_page"], 2)
        self.assertEqual(len(first["data"]["orders"]), 2)
        last = self.run_op("list_orders", client, {"page": first["pagination"]["next_page"], "page_size": 2, "date_filter_option": "updatedDate"})
        self.assertIsNone(last["pagination"]["next_page"])
        self.assertEqual(self.requests[0].url.params["startDate"], "2026-10-01")

    def test_list_filters_mirror_vendor_rules(self) -> None:
        self.refuse("list_orders", {"page": 2})
        self.refuse("list_orders", {"date_filter_option": "createdDate"})
        self.refuse("list_orders_by_invoice_owner", {"account_number": "../A1"})

    def test_draft_order_correct_activate_cancel_and_delete(self) -> None:
        seen: list[tuple[str, str, Any]] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
            if req.url.path.endswith("/activate"):
                return vendor_json(200, order_result("O-00000400", status="Completed"))
            if req.url.path.endswith("/cancel"):
                return vendor_json(400, refusal(53200030, "Only draft or scheduled orders can be cancelled."))
            if req.method == "DELETE":
                return vendor_json(200, {"success": True})
            if req.method == "GET":
                return vendor_json(200, {"success": True, "order": order_record("O-00000400", status="Draft")})
            return vendor_json(200, order_result("O-00000400", status="Draft"))

        client = self.client(vendor)
        draft = self.run_op("get_order", client, {"order_number": "O-00000400", "get_account_details": True})
        self.assertEqual(draft["data"]["order"]["status"], "Draft")
        corrected = self.run_op("update_order", client, {"order_number": "O-00000400", "body": {**_ORDER_BODY, "status": "Draft", "description": "corrected"}})
        self.assertEqual(corrected["data"]["status"], "Draft")
        activated = self.run_op("activate_order", client, {"order_number": "O-00000400"})
        self.assertEqual(activated["data"]["status"], "Completed")
        with self.assertRaises(ZuoraServiceError) as refused:
            self.run_op("cancel_order", client, {"order_number": "O-00000400", "body": {"cancelReason": "duplicate"}})
        self.assertEqual(refused.exception.code, "zuora.validation_failed")
        self.assertIn("Only draft or scheduled", str(refused.exception))
        self.assertEqual(self.run_op("delete_order", client, {"order_number": "O-00000400"})["data"], {"success": True})
        self.assertEqual(
            [(method, path) for method, path, _ in seen],
            [
                ("GET", "/v1/orders/O-00000400"),
                ("PUT", "/v1/orders/O-00000400"),
                ("PUT", "/v1/orders/O-00000400/activate"),
                ("PUT", "/v1/orders/O-00000400/cancel"),
                ("DELETE", "/v1/orders/O-00000400"),
            ],
        )
        self.assertEqual(seen[3][2], {"cancelReason": "duplicate"})
        self.assertEqual(self.requests[0].url.params["getAccountDetails"], "true")

    def test_cancel_order_without_body_sends_an_empty_document(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"success": True, "orderNumber": "O-1", "accountNumber": "A00000101", "status": "Cancelled", "cancelReason": None}))
        self.assertEqual(self.run_op("cancel_order", client, {"order_number": "O-1"})["data"]["status"], "Cancelled")
        self.assertEqual(json.loads(self.requests[0].content), {})

    def test_async_create_then_poll_job(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                self.assertEqual(req.url.path, "/v1/async/orders")
                return httpx.Response(202, json={"success": True, "jobId": "2c9081a03c6d7b51013c6d7e2dfc09fa"}, headers=_HEADERS)
            self.assertEqual(req.url.path, "/v1/async-jobs/2c9081a03c6d7b51013c6d7e2dfc09fa")
            return vendor_json(200, {"success": True, "status": "Completed", "errors": None, "result": order_result("O-00000500")})

        client = self.client(vendor)
        started = self.run_op("create_order_async", client, {"body": _ORDER_BODY})
        job = self.run_op("get_async_order_job", client, {"job_id": started["data"]["jobId"], "zuora_version": "329.0"})
        self.assertEqual(job["data"]["result"]["orderNumber"], "O-00000500")
        self.assertEqual(started["status"], 202)

    def test_async_job_documents_no_entity_or_org_header(self) -> None:
        self.assertEqual(set(_OPS["get_async_order_job"].header_fields), {"zuora_version", "zuora_track_id"})
        self.assertNotIn("zuora_org_ids", _OPS["preview_existing_subscription"].header_fields)
        self.assertIn("idempotency_key", _OPS["delete_order_async"].header_fields)
        self.assertNotIn("idempotency_key", _OPS["delete_order"].header_fields)

    def test_header_values_mirror_vendor_rules(self) -> None:
        self.refuse("get_order", {"order_number": "O-1", "zuora_track_id": "trace:1"})
        self.refuse("get_order", {"order_number": "O-1", "zuora_track_id": 'trace"1'})
        self.refuse("get_order", {"order_number": "O-1", "zuora_version": "329.0\r\nX-Injected: 1"})
        self.refuse("get_order", {"order_number": "O-1", "zuora_entity_ids": "entité"})
        self.refuse("get_order", {"order_number": "O-1", "zuora_org_ids": ""})


class SubscriptionTests(TenantCase):
    def test_get_subscription_forwards_segment_metrics_and_owner_options(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/v1/subscriptions/A-S00000201")
            params = dict(req.url.params)
            self.assertEqual(
                params,
                {"charge-detail": "specific-segment", "as-of-date": "2026-10-15", "getDetailedMetrics": "true", "asOfDay": "2026-10-31", "getInvoiceOwnerDetails": "true", "exclude-rate-plans-with-no-charges": "false"},
            )
            return vendor_json(200, {"success": True, "subscriptionNumber": "A-S00000201", "status": "Active", "version": 3, "ratePlans": [], "contractedNetMrr": 120.0})

        result = self.run_op(
            "get_subscription",
            self.client(vendor),
            {
                "subscription_key": "A-S00000201",
                "charge_detail": "specific-segment",
                "as_of_date": "2026-10-15",
                "get_detailed_metrics": True,
                "as_of_day": "2026-10-31",
                "get_invoice_owner_details": True,
                "exclude_rate_plans_with_no_charges": False,
            },
        )
        self.assertEqual(result["data"]["version"], 3)

    def test_segment_pairing_rules_refused_before_network(self) -> None:
        self.refuse("get_subscription", {"subscription_key": "A-S1", "charge_detail": "specific-segment"})
        self.refuse("get_subscription", {"subscription_key": "A-S1", "as_of_date": "2026-10-15"})
        self.refuse("get_subscription_version", {"subscription_key": "A-S1", "version": "2", "as_of_day": "2026-10-31"})
        self.refuse("get_subscription", {"subscription_key": "A-S1", "charge_detail": "every-segment"})

    def test_suspend_resume_renew_cancel_and_delete_lifecycle(self) -> None:
        answers = {
            "suspend": {"success": True, "subscriptionId": "8a90e5e4sub0002", "suspendDate": "2026-11-10", "resumeDate": "2026-12-10", "termEndDate": "2027-11-01", "totalDeltaTcv": -100.0},
            "resume": {"success": True, "subscriptionId": "8a90e5e4sub0003", "resumeDate": "2026-11-20", "termEndDate": "2027-11-01", "totalDeltaTcv": 100.0},
            "renew": {"success": True, "termStartDate": "2027-11-01", "termEndDate": "2028-11-01", "totalDeltaMrr": 0, "totalDeltaTcv": 1200.0},
            "cancel": {"success": True, "subscriptionId": "8a90e5e4sub0004", "cancelledDate": "2026-12-01", "totalDeltaMrr": -100.0, "totalDeltaTcv": -1100.0},
            "delete": {"success": True},
        }
        bodies: dict[str, Any] = {}

        def vendor(req: httpx.Request) -> httpx.Response:
            action = req.url.path.rsplit("/", 1)[1]
            self.assertEqual((req.method, req.url.path), ("PUT", f"/v1/subscriptions/A-S00000201/{action}"))
            bodies[action] = json.loads(req.content) if req.content else None
            return vendor_json(200, answers[action])

        client = self.client(vendor)
        key = {"subscription_key": "A-S00000201"}
        suspended = self.run_op("suspend_subscription", client, {**key, "body": {"suspendPolicy": "SpecificDate", "suspendSpecificDate": "2026-11-10", "resume": True, "resumePolicy": "FixedPeriodsFromSuspendDate", "resumePeriods": "1", "resumePeriodsType": "Month"}})
        resumed = self.run_op("resume_subscription", client, {**key, "body": {"resumePolicy": "Today", "extendsTerm": True}})
        renewed = self.run_op("renew_subscription", client, key)
        cancelled = self.run_op("cancel_subscription", client, {**key, "body": {"cancellationPolicy": "SpecificDate", "cancellationEffectiveDate": "2026-12-01", "runBilling": True}})
        deleted = self.run_op("delete_subscription", client, key)
        self.assertEqual((suspended["data"]["suspendDate"], resumed["data"]["resumeDate"]), ("2026-11-10", "2026-11-20"))
        self.assertEqual(renewed["data"]["termEndDate"], "2028-11-01")
        self.assertEqual(cancelled["data"]["cancelledDate"], "2026-12-01")
        self.assertEqual(deleted["data"], {"success": True})
        self.assertEqual(bodies["renew"], {})
        self.assertIsNone(bodies["delete"])
        self.assertEqual(bodies["cancel"]["cancellationPolicy"], "SpecificDate")

    def test_resume_without_suspension_is_the_vendor_refusal(self) -> None:
        client = self.client(lambda _: vendor_json(200, refusal(58730020, "The subscription is not suspended.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("resume_subscription", client, {"subscription_key": "A-S00000201", "body": {"resumePolicy": "Today"}})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("not suspended", str(raised.exception))

    def test_custom_field_set_correct_and_clear_on_a_version(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/v1/subscriptions/A-S00000201/versions/2/customFields")
            return vendor_json(200, {"success": True})

        client = self.client(vendor)
        for value in ("CRM-17", "CRM-18", None):
            self.run_op("update_subscription_version_custom_fields", client, {"subscription_number": "A-S00000201", "version": "2", "body": {"customFields": {"CrmId__c": value}}})
        self.assertEqual([json.loads(req.content)["customFields"]["CrmId__c"] for req in self.requests], ["CRM-17", "CRM-18", None])

    def test_metrics_send_repeated_subscription_numbers(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.params.get_list("subscriptionNumbers[]"), ["A-S00000201", "A-S00000202"])
            return vendor_json(200, {"success": True, "subscriptionMetrics": [{"subscriptionNumber": "A-S00000201", "contractedMrr": 100.0, "totalContractedValue": 1200.0}]})

        result = self.run_op("get_subscription_metrics", self.client(vendor), {"subscription_numbers": ["A-S00000201", "A-S00000202"], "as_of_day": "2026-11-01"})
        self.assertEqual(result["data"]["subscriptionMetrics"][0]["contractedMrr"], 100.0)
        self.refuse("get_subscription_metrics", {"subscription_numbers": []})

    def test_legacy_subscription_export_forwards_read_options_on_every_page(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.params["charge-detail"], "all-segments")
            self.assertEqual(req.url.params["getDetailedMetrics"], "true")
            page = int(req.url.params["page"])
            count = 40 if page == 1 else 3
            return vendor_json(200, {"success": True, "subscriptions": [{"id": f"s{page}-{n}", "subscriptionNumber": f"A-S{page:03d}{n:03d}", "status": "Active"} for n in range(count)]})

        output = self.root / "subs.tsv"
        result = billing_actions.list_subscriptions(self.client(vendor), {"account_id": "A00000101", "output_tsv_path": str(output), "charge_detail": "all-segments", "get_detailed_metrics": True}, self.gate)
        self.assertEqual(len(self.requests), 2)
        self.assertEqual((result["row_count"], result["truncated"]), (43, False))
        manager = MagicMock()
        self.plugin._async_job_manager = manager  # noqa: SLF001
        refused = self.plugin.list_subscriptions({"account_id": "A00000101", "output_tsv_path": str(output), "as_of_date": "2026-10-01"}, dict(_STATE))
        self.assertEqual(refused["error"]["code"], "zuora.invalid_params")
        manager.create_job.assert_not_called()


class AccountTests(TenantCase):
    def test_customer_account_number_is_not_treated_as_a_secret(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"success": True, "accountNumber": "A00000103", "accountId": "8a90e5e4acct0003"}))
        result = self.run_op("create_account", client, {"body": {"name": "Beta", "currency": "EUR", "billToContact": {"firstName": "B", "lastName": "C"}, "accountNumber": "A00000103"}})
        self.assertEqual(result["data"]["accountNumber"], "A00000103")

    def test_read_update_cascading_and_delete_account(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/summary"):
                self.assertEqual(req.url.params["excludeUsage"], "true")
                return vendor_json(200, {"success": True, "basicInfo": {"accountNumber": "A00000101", "status": "Active"}, "subscriptions": [], "invoices": [], "payments": [], "usage": []})
            if path.endswith("/payment-methods/cascading"):
                if req.method == "PUT":
                    self.assertEqual(json.loads(req.content), {"consent": False, "priorities": []})
                    return vendor_json(200, {"success": True})
                return vendor_json(200, {"success": True, "consent": True, "priorities": [{"paymentMethodId": "8a90e5e4pm0001", "order": 1}]})
            if path.endswith("/payment-methods/default"):
                return vendor_json(200, {"success": True, "id": "8a90e5e4pm0001", "type": "CreditCard", "cardNumber": "************1111", "isDefault": True, "status": "Active"})
            if path.endswith("/payment-methods"):
                self.assertEqual(req.url.params["isActiveOnly"], "true")
                return vendor_json(200, {"success": True, "defaultPaymentMethodId": "8a90e5e4pm0001", "returnedPaymentMethodType": ["creditcard"], "creditcard": [{"id": "8a90e5e4pm0001", "cardNumber": "************1111"}]})
            if req.method == "DELETE":
                return vendor_json(200, {"success": True, "id": "8a90e5e4acct0001", "jobId": "8a90e5e4job0001", "jobStatus": "Pending"})
            if req.method == "PUT":
                return vendor_json(200, {"success": True})
            return vendor_json(200, {"success": True, "basicInfo": {"accountNumber": "A00000101", "name": "Acme", "status": "Active"}})

        client = self.client(vendor)
        key = {"account_key": "A00000101"}
        self.assertEqual(self.run_op("get_account", client, key)["data"]["basicInfo"]["name"], "Acme")
        self.assertEqual(self.run_op("get_account_summary", client, {**key, "exclude_usage": True})["data"]["usage"], [])
        self.run_op("update_account", client, {**key, "body": {"notes": "", "billCycleDay": 15}})
        self.assertEqual(self.run_op("list_account_payment_methods", client, {**key, "is_active_only": True})["data"]["defaultPaymentMethodId"], "8a90e5e4pm0001")
        self.assertTrue(self.run_op("get_account_default_payment_method", client, key)["data"]["isDefault"])
        self.assertTrue(self.run_op("get_account_payment_method_cascading", client, key)["data"]["consent"])
        self.run_op("update_account_payment_method_cascading", client, {**key, "body": {"consent": False, "priorities": []}})
        deleted = self.run_op("delete_account", client, key)
        self.assertEqual((deleted["data"]["jobStatus"], deleted["data"]["jobId"]), ("Pending", "8a90e5e4job0001"))
        self.assertEqual(json.loads(self.requests[2].content), {"notes": "", "billCycleDay": 15})
        self.refuse("update_account", key)

    def test_delete_account_rbac_and_vendor_precondition(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("A00000101"):
                return vendor_json(403, refusal(50000040, "You do not have the Delete Account permission."))
            return vendor_json(400, refusal(53100120, "The account is the subscription owner of a subscription whose invoice owner is another account."))

        client = self.client(vendor)
        with self.assertRaises(ZuoraServiceError) as denied:
            self.run_op("delete_account", client, {"account_key": "A00000101"})
        self.assertEqual(denied.exception.code, "zuora.permission_denied")
        self.assertIn("Delete Account permission", str(denied.exception))
        with self.assertRaises(ZuoraServiceError) as refused:
            self.run_op("delete_account", client, {"account_key": "A00000104"})
        self.assertEqual(refused.exception.code, "zuora.validation_failed")
        self.assertEqual([req.method for req in self.requests], ["DELETE", "DELETE"])


class CredentialAndDispatchTests(TenantCase):
    def test_expired_token_reminted_once_then_reported(self) -> None:
        statuses = iter((401, 200))
        recovering = self.client(lambda _: vendor_json(next(statuses), {"success": True, "basicInfo": {"accountNumber": "A1"}}))
        self.assertEqual(self.run_op("get_account", recovering, {"account_key": "A1"})["status"], 200)
        self.assertEqual(self.token_mints, 2)
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_order", self.client(lambda _: vendor_json(401, {"message": "Authentication error"})), {"order_number": "O-1"})
        self.assertEqual(raised.exception.code, "zuora.auth_failed")

    def test_dispatch_queues_without_network_and_worker_completes(self) -> None:
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "order-job", "status": "queued"}}
        params = {"order_number": "O-00000400"}
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
        self.plugin._async_job_manager = manager  # noqa: SLF001
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(200, {"success": True, "order": order_record("O-00000400")}))  # noqa: SLF001
        with patch.object(async_jobs, "ensure_worker_started"):
            queued = self.plugin.get_order(params, dict(_STATE))
            self.assertEqual(queued["data"]["status"], "queued")
            self.assertEqual(self.requests, [])
            refused = self.plugin.create_account({"body": {"name": "No currency"}}, dict(_STATE))
            self.assertEqual(refused["error"]["code"], "zuora.invalid_params")
            self.assertEqual(manager.create_job.call_count, 1)
            async_jobs._process_job(self.plugin, manager, "order-job", "get_order")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "completed")
        self.assertEqual(completion["result"]["data"]["order"]["orderNumber"], "O-00000400")

    def test_worker_reports_success_false_as_job_error(self) -> None:
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": {"order_number": "O-1"}}}
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(200, refusal(53200030, "Only draft orders can be activated.")))  # noqa: SLF001
        async_jobs._process_job(self.plugin, manager, "order-job", "activate_order")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "error")
        self.assertEqual(completion["error"]["code"], "zuora.validation_failed")
        self.assertIn("Only draft orders", completion["error"]["message"])


class DataQueryListHintTests(TenantCase):
    def test_list_jobs_reports_may_have_more_and_sends_documented_headers(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.headers["Zuora-Entity-Ids"], "entity-1")
            size = int(req.url.params.get("pageSize", "3"))
            return vendor_json(200, {"data": [{"id": f"job-{n}", "queryStatus": "completed", "outputRows": 10} for n in range(min(size, 3))]})

        client = self.client(vendor)
        full = self.run_op("list_data_query_jobs", client, {"page_size": 2, "zuora_entity_ids": "entity-1"})
        self.assertEqual(full["pagination"]["returned"], 2)
        self.assertTrue(full["pagination"]["may_have_more"])
        self.assertIsNone(full["pagination"]["next_page"])
        self.assertIn("page_size", full["pagination"]["hint"])
        short = self.run_op("list_data_query_jobs", client, {"page_size": 5, "zuora_entity_ids": "entity-1"})
        self.assertFalse(short["pagination"]["may_have_more"])
        unsized = self.run_op("list_data_query_jobs", client, {"zuora_entity_ids": "entity-1"})
        self.assertIsNone(unsized["pagination"]["may_have_more"])

    def test_submit_accepts_idempotency_key(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.headers["Idempotency-Key"], "dq-1")
            return vendor_json(200, {"data": {"id": "job-1", "queryStatus": "accepted"}})

        result = self.run_op("submit_data_query", self.client(vendor), {"query": "data-query-fixture-statement-8", "idempotency_key": "dq-1"})
        self.assertEqual(result["data"]["data"]["queryStatus"], "accepted")


if __name__ == "__main__":
    unittest.main()
