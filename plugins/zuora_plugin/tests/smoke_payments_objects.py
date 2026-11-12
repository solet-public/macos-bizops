"""Hermetic unittest coverage for the typed Zuora Payment Methods, Payments, Custom Objects and Object Query verbs.

httpx.MockTransport plays the tenant. Every transcript follows the response
fields in Zuora's v1 API reference (Payment Methods, Payments, Custom Object
Definitions, Records and Jobs, and Object Queries sections, reference version
2026-11-06): ``success`` plus ``processId``, ``reasons[{code, message}]`` and
``requestId`` error envelopes, top-level ``nextPage`` URLs on the v1
page-numbered lists, the ``nextPage`` cursor of Object Query and the
``cursor`` of custom object records and jobs, ``count`` on custom object
lists, and Zuora-Request-Id and RateLimit headers. No vendor is contacted.

Run: SOLET_NAME=<name> .venv/bin/python plugins/zuora_plugin/tests/smoke_payments_objects.py
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
from zuora_plugin import async_jobs, custom_object_actions, data_query_actions, payment_actions, typed_verbs, workflow_actions  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402
from zuora_plugin.typed_operations import Operation, PathGate, execute  # noqa: E402

_CLIENT_ID = "fixture-client-id"
_SECRET = "fixture-client-secret"
_TOKEN = "fixture-access-token"
_BASE = "https://tenant.example.test"
_STATE = {"session_id": "payments-session", "flow_id": "payments-flow"}
_HEADERS = {"Zuora-Request-Id": "5c1d2e3f-req", "RateLimit-Limit": "40", "RateLimit-Remaining": "38", "RateLimit-Reset": "1"}
_PAYMENT_OPS = (*payment_actions.PAYMENT_METHOD_OPERATIONS, *payment_actions.PAYMENT_OPERATIONS)
_OBJECT_OPS = (*custom_object_actions.CUSTOM_OBJECT_OPERATIONS, *custom_object_actions.OBJECT_QUERY_OPERATIONS)
_OPS: dict[str, Operation] = {op.name: op for op in (*_PAYMENT_OPS, *_OBJECT_OPS)}
# Caller parameters an operation takes beyond its path, query, header and body fields.
_EXTRA_PARAMS: dict[str, set[str]] = {
    "upload_custom_object_job_file": {"input_path"},
    "query_objects": {"fields", "object_fields"},
    "query_object": {"fields", "object_fields"},
}
_NO_BODY_PARAM = {"upload_custom_object_job_file"}
_PM_ID = "8a90e5e48c1f4e2b018c2a7d1e3fpm01"
_PAYMENT_ID = "8a90e5e48c1f4e2b018c2a7d1e3fpay1"
_RECORD_ID = "258d65b2-7bc6-4142-88bc-5184931af493"
_JOB_ID = "a3b2c1d0-1111-4e2f-9a8b-7c6d5e4f3a21"

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


def refusal(code: int | str, message: str) -> dict[str, Any]:
    """Zuora's error envelope (v1 billing, Custom Objects and Object Query share it)."""
    return {"success": False, "processId": "5F4B6A7C8D9E0F1A", "reasons": [{"code": code, "message": message}], "requestId": "41a3b4c5-d6e7-4f80-9a1b-2c3d4e5f6a7b"}


def payment_record(number: str, status: str = "Processed", unapplied: float = 0.0) -> dict[str, Any]:
    return {
        "id": _PAYMENT_ID,
        "number": number,
        "accountId": "8a90e5e48c1f4e2b018c2a7d1e3facct",
        "amount": 120.0,
        "appliedAmount": 120.0 - unapplied,
        "unappliedAmount": unapplied,
        "refundAmount": 0.0,
        "creditBalanceAmount": 0.0,
        "currency": "USD",
        "effectiveDate": "2026-11-01",
        "status": status,
        "type": "External",
        "paymentMethodId": _PM_ID,
        "success": True,
    }


def custom_record(make: str) -> dict[str, Any]:
    return {"Id": _RECORD_ID, "make__c": make, "model__c": "Model T", "type": "default__vehicle", "CreatedById": "u-1", "CreatedDate": "2026-11-01T10:00:00.000Z", "UpdatedById": "u-1", "UpdatedDate": "2026-11-01T10:00:00.000Z"}


def every_encoding(*values: str) -> list[str]:
    forms: list[str] = []
    for value in values:
        forms.extend([value, quote(value, safe=""), base64.b64encode(value.encode()).decode(), base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")])
    return forms


class TenantCase(unittest.TestCase):
    """A ZuoraClient over MockTransport plus a plugin gate rooted in a temporary workspace."""

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="zuora_payments_smoke_")
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

    def denied(self, name: str, params: dict[str, Any], message: str) -> None:
        client = self.client(lambda _: vendor_json(403, refusal(50000040, message)))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op(name, client, params)
        self.assertEqual(raised.exception.code, "zuora.permission_denied")
        self.assertIn(message, str(raised.exception))


class RegistrationTests(unittest.TestCase):
    def test_every_build4_verb_has_decorator_edge_handler_and_process_json(self) -> None:
        plugin = ZuoraPlugin()
        plugin.initialize({})
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", plugin, list(actions.values()))
        edges = plugin.get_edge_process_definitions()
        self.assertEqual((len(_PAYMENT_OPS), len(_OBJECT_OPS)), (32, 20))
        self.assertEqual(len(actions), 255)
        for name in sorted(_OPS):
            with self.subTest(verb=name):
                self.assertIn(name, actions)
                self.assertIn(name, edges)
                self.assertIn(name, async_jobs.ACTION_HANDLERS)
                errors = edges[name].error_processor_template_customizations
                assert errors is not None
                self.assertEqual(errors.retryable, _OPS[name].method == "GET")
                kb = json.loads((REPO_ROOT / "plugins/zuora_plugin/knowledge_base/processes" / f"{name}.json").read_text(encoding="utf-8"))
                self.assertEqual(kb["process_key"], f"plugin::zuora_plugin::{name}")
                self.assertEqual(kb["display_name"], actions[name].display_name)
                self.assertEqual(kb["description"], actions[name].description)
                self.assertEqual(kb["result_processor_customizations"]["result_type"], typed_verbs.result_type(name))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400, len(kb["embedding_description"]))
                op = _OPS[name]
                body = {"body"} if op.body is not None and name not in _NO_BODY_PARAM else set()
                expected = {*op.path_fields, *op.query_fields, *op.header_fields, *_EXTRA_PARAMS.get(name, set())} | body
                self.assertEqual(set(actions[name].parameters), expected)

    def test_documented_header_sets(self) -> None:
        self.assertEqual(set(_OPS["verify_payment_method"].header_fields), {"zuora_version", "zuora_track_id"})
        for name in ("list_wallet_domains", "register_wallet_domain", "unregister_wallet_domain", "create_payment_session"):
            self.assertEqual(set(_OPS[name].header_fields), {"idempotency_key", "zuora_entity_ids", "zuora_org_ids", "zuora_track_id"}, name)
        self.assertIn("idempotency_key", _OPS["create_payment"].header_fields)
        self.assertNotIn("idempotency_key", _OPS["delete_payment"].header_fields)
        self.assertIn("if_match", _OPS["update_custom_object_record"].header_fields)
        self.assertIn("idempotency_key", _OPS["query_objects"].header_fields)


class PaymentMethodTests(TenantCase):
    def test_read_correct_verify_scrub_and_delete_lifecycle(self) -> None:
        seen: list[tuple[str, str, Any]] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
            if req.method == "GET" and req.url.path.endswith("/balance"):
                return vendor_json(200, {"success": True, "availableBalance": 1520.75, "currency": "USD", "balanceUpdatedTime": "2026-11-07T09:00:00Z"})
            if req.method == "GET":
                return vendor_json(200, {"success": True, "id": _PM_ID, "type": "CreditCard", "status": "Active", "cardNumber": "************1111", "expirationMonth": 12, "expirationYear": 2030})
            if req.url.path.endswith("/verify"):
                return vendor_json(200, {"success": True, "paymentMethodId": _PM_ID})
            return vendor_json(200, {"success": True, "id": _PM_ID})

        client = self.client(vendor)
        key = {"payment_method_id": _PM_ID}
        self.assertEqual(self.run_op("get_payment_method", client, key)["data"]["status"], "Active")
        self.run_op("update_payment_method", client, {**key, "body": {"expirationMonth": 1, "expirationYear": 2031, "cardHolderInfo": {"addressLine2": None}}})
        self.assertEqual(self.run_op("verify_payment_method", client, {**key, "zuora_version": "329.0"})["data"]["paymentMethodId"], _PM_ID)
        self.assertEqual(self.run_op("get_payment_method_balance", client, key)["data"]["availableBalance"], 1520.75)
        self.run_op("scrub_payment_method", client, key)
        self.run_op("delete_payment_method", client, key)
        self.assertEqual(
            [(method, path) for method, path, _ in seen],
            [
                ("GET", f"/v1/payment-methods/{_PM_ID}"),
                ("PUT", f"/v1/payment-methods/{_PM_ID}"),
                ("PUT", f"/v1/payment-methods/{_PM_ID}/verify"),
                ("GET", f"/v1/payment-methods/{_PM_ID}/balance"),
                ("PUT", f"/v1/payment-methods/{_PM_ID}/scrub"),
                ("DELETE", f"/v1/payment-methods/{_PM_ID}"),
            ],
        )
        self.assertEqual(seen[1][2]["cardHolderInfo"], {"addressLine2": None})
        self.assertEqual(seen[2][2], {})
        self.assertIsNone(seen[4][2])
        self.assertEqual(self.requests[2].headers["Zuora-Version"], "329.0")
        self.refuse("update_payment_method", key)

    def test_deleting_the_default_payment_method_is_the_vendor_refusal(self) -> None:
        client = self.client(lambda _: vendor_json(200, refusal(53000040, "Cannot delete the default payment method of an account.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_payment_method", client, {"payment_method_id": _PM_ID})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("default payment method", str(raised.exception))
        self.denied("scrub_payment_method", {"payment_method_id": _PM_ID}, "Scrub Sensitive Data of Specific Payment Method permission is required.")

    def test_stored_credential_profile_list_add_data_cancel_and_expire(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "GET":
                self.assertEqual(req.url.params["includeAll"], "true")
                return vendor_json(200, {"success": True, "profiles": [{"number": 1, "status": "Active", "type": "Recurring"}, {"number": 2, "status": "Cancelled", "type": "Unscheduled"}]})
            return vendor_json(200, {"success": True, "paymentMethodId": _PM_ID, "number": 1})

        client = self.client(vendor)
        listed = self.run_op("list_stored_credential_profiles", client, {"payment_method_id": _PM_ID, "include_all": True})
        self.assertEqual([p["status"] for p in listed["data"]["profiles"]], ["Active", "Cancelled"])
        self.run_op("add_stored_credential_profile_data", client, {"payment_method_id": _PM_ID, "profile_number": 1, "body": {"authGateway": "gw-1", "networkTransactionId": "nt-778"}})
        self.run_op("cancel_stored_credential_profile", client, {"payment_method_id": _PM_ID, "profile_number": "1"})
        self.run_op("expire_stored_credential_profile", client, {"payment_method_id": _PM_ID, "profile_number": 1, "idempotency_key": "exp-1"})
        self.assertEqual(
            [req.url.path for req in self.requests[1:]],
            [f"/v1/payment-methods/{_PM_ID}/profiles/1/profile-data", f"/v1/payment-methods/{_PM_ID}/profiles/1/cancel", f"/v1/payment-methods/{_PM_ID}/profiles/1/expire"],
        )
        self.assertEqual(self.requests[3].headers["Idempotency-Key"], "exp-1")
        self.refuse("cancel_stored_credential_profile", {"payment_method_id": _PM_ID, "profile_number": "first"})
        self.refuse("add_stored_credential_profile_data", {"payment_method_id": _PM_ID, "profile_number": 1, "body": {"authGateway": "gw-1"}})

    def test_wallet_domains_and_payment_session(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertNotIn("Zuora-Version", req.headers)
            if req.url.path == "/web-payments/sessions":
                return vendor_json(200, {"token": "session-token-fixture", "echo": f"{_SECRET} {_TOKEN}"})
            if req.method == "GET":
                self.assertEqual((req.url.params["walletType"], req.url.params["domainName"]), ("ApplePay", "shop.example.com"))
                return vendor_json(200, {"success": True, "domains": [{"id": "402881a38924ff1001892502da090021", "domainName": "shop.example.com", "walletType": "ApplePay"}]})
            if req.method == "POST":
                return vendor_json(200, {"success": True, "id": "402881a38924ff1001892502da090021", "domainName": "shop.example.com", "walletType": "GooglePay"})
            return vendor_json(200, {"success": True})

        client = self.client(vendor)
        listed = self.finish_op("list_wallet_domains", client, {"wallet_type": "ApplePay", "domain_name": "shop.example.com", "zuora_track_id": "wallet-1"})["data"]
        self.assertEqual(listed["data"]["domains"][0]["walletType"], "ApplePay")
        self.run_op("register_wallet_domain", client, {"body": {"domainName": "shop.example.com", "walletType": "GooglePay"}})
        self.run_op("unregister_wallet_domain", client, {"domain_id": "402881a38924ff1001892502da090021"})
        session = self.finish_op("create_payment_session", client, {"body": {"amount": 25.0, "currency": "USD", "processPayment": False, "accountId": "8a90acct"}})["data"]
        self.assertEqual(session["data"]["token"], "session-token-fixture")
        self.assertEqual(session["data"]["echo"], f"{_SECRET} {_TOKEN}")
        self.assertEqual(self.requests[2].method, "DELETE")
        self.refuse("list_wallet_domains", {"domain_name": "shop.example.com"})
        self.refuse("register_wallet_domain", {"body": {"domainName": "shop.example.com"}})
        self.refuse("create_payment_session", {"body": {"amount": 25.0, "currency": "USD"}})


class PaymentTests(TenantCase):
    def test_list_payments_filters_and_follows_next_page(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            page = int(req.url.params["page"])
            self.assertEqual((req.url.params["pageSize"], req.url.params["status"], req.url.params["unappliedAmount"], req.url.params["sort"]), ("2", "Processed", "0.0", "-number"))
            body: dict[str, Any] = {"success": True, "payments": [payment_record(f"P-0000010{page}{n}") for n in (1, 2)] if page == 1 else [payment_record("P-00000121")]}
            if page == 1:
                body["nextPage"] = f"{_BASE}/v1/payments?page=2&pageSize=2&status=Processed"
            return vendor_json(200, body)

        client = self.client(vendor)
        params = {"page_size": 2, "status": "Processed", "unapplied_amount": 0.0, "sort": "-number"}
        first = self.run_op("list_payments", client, {**params, "page": 1})
        self.assertEqual((first["pagination"]["next_page"], len(first["data"]["payments"])), (2, 2))
        last = self.run_op("list_payments", client, {**params, "page": 2})
        self.assertIsNone(last["pagination"]["next_page"])
        self.refuse("list_payments", {"page": 2})
        self.refuse("list_payments", {"amount": "120"})
        self.refuse("list_payments", {"amount": True})

    def test_create_apply_unapply_transfer_refund_cancel_and_delete(self) -> None:
        seen: list[tuple[str, str, Any]] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
            path = req.url.path
            if req.method == "POST" and path == "/v1/payments":
                return vendor_json(200, payment_record("P-00000201", unapplied=120.0))
            if path.endswith("/refunds") or path.endswith("/refunds/unapply"):
                return vendor_json(200, {"success": True, "id": "8a90refund01", "number": "R-00000001", "amount": 20.0, "status": "Processed", "paymentId": _PAYMENT_ID})
            if path.endswith("/cancel"):
                return vendor_json(200, payment_record("P-00000201", status="Canceled", unapplied=100.0))
            if req.method == "DELETE":
                return vendor_json(200, {"success": True})
            return vendor_json(200, payment_record("P-00000201", unapplied=0.0))

        client = self.client(vendor)
        key = {"payment_key": "P-00000201"}
        invoices = {"invoices": [{"invoiceId": "8a90inv01", "amount": 100.0}], "effectiveDate": "2026-11-02"}
        created = self.run_op("create_payment", client, {"body": {"accountId": "8a90acct", "amount": 120.0, "currency": "USD", "type": "External", "effectiveDate": "2026-11-01"}, "idempotency_key": "pay-1"})
        self.assertEqual(created["data"]["unappliedAmount"], 120.0)
        self.run_op("apply_payment", client, {**key, "body": invoices})
        self.run_op("unapply_payment", client, {**key, "body": invoices})
        self.run_op("transfer_payment", client, {**key, "body": {"accountId": "8a90acct2"}})
        self.run_op("update_payment", client, {**key, "body": {"comment": "", "referenceId": "chk-7781"}})
        refund = self.run_op("refund_payment", client, {**key, "body": {"totalAmount": 20.0, "type": "External", "methodType": "Check"}, "idempotency_key": "ref-1"})
        self.assertEqual(refund["data"]["number"], "R-00000001")
        self.run_op("refund_payment_with_auto_unapply", client, {**key, "body": {"type": "Electronic", "totalAmount": 10.0}})
        self.assertEqual(self.run_op("cancel_payment", client, key)["data"]["status"], "Canceled")
        self.assertEqual(self.run_op("delete_payment", client, key)["data"], {"success": True})
        self.assertEqual(
            [(method, path) for method, path, _ in seen],
            [
                ("POST", "/v1/payments"),
                ("PUT", "/v1/payments/P-00000201/apply"),
                ("PUT", "/v1/payments/P-00000201/unapply"),
                ("PUT", "/v1/payments/P-00000201/transfer"),
                ("PUT", "/v1/payments/P-00000201"),
                ("POST", "/v1/payments/P-00000201/refunds"),
                ("POST", "/v1/payments/P-00000201/refunds/unapply"),
                ("PUT", "/v1/payments/P-00000201/cancel"),
                ("DELETE", "/v1/payments/P-00000201"),
            ],
        )
        self.assertEqual(seen[1][2], invoices)
        self.assertIsNone(seen[7][2])
        self.assertEqual(self.requests[5].headers["Idempotency-Key"], "ref-1")
        self.refuse("create_payment", {"body": {"amount": 1.0, "currency": "USD"}})
        self.refuse("refund_payment", {**key, "body": {"type": "External"}})
        self.refuse("refund_payment_with_auto_unapply", {**key, "body": {"totalAmount": 5.0}})

    def test_delete_uncancelled_payment_refused_and_rbac_denial_kept(self) -> None:
        client = self.client(lambda _: vendor_json(400, refusal(53510030, "Only payments with the Canceled status can be deleted.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_payment", client, {"payment_key": "P-00000202"})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("Canceled status", str(raised.exception))
        self.denied("refund_payment", {"payment_key": "P-1", "body": {"totalAmount": 1.0, "type": "External"}}, "You do not have the Refund Payment permission.")

    def test_parts_and_item_parts_paths_and_paging(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/item-parts"):
                return vendor_json(200, {"success": True, "itemParts": [{"id": "ip-1", "invoiceItemId": "ii-1", "amount": 50.0}]})
            if "/item-parts/" in req.url.path:
                return vendor_json(200, {"success": True, "id": "ip-1", "invoiceItemId": "ii-1", "amount": 50.0})
            if req.url.path.endswith("/parts"):
                return vendor_json(200, {"success": True, "parts": [{"id": "pp-1", "invoiceId": "8a90inv01", "amount": 100.0}], "nextPage": f"{_BASE}/v1/payments/P-1/parts?page=2&pageSize=1"})
            return vendor_json(200, {"success": True, "id": "pp-1", "invoiceId": "8a90inv01", "amount": 100.0})

        client = self.client(vendor)
        parts = self.run_op("list_payment_parts", client, {"payment_key": "P-1", "page": 1, "page_size": 1})
        self.assertEqual(parts["pagination"]["next_page"], 2)
        self.assertEqual(self.run_op("get_payment_part", client, {"payment_key": "P-1", "part_id": "pp-1"})["data"]["invoiceId"], "8a90inv01")
        items = self.run_op("list_payment_item_parts", client, {"payment_key": "P-1", "part_id": "pp-1"})
        self.assertIsNone(items["pagination"]["next_page"])
        self.run_op("get_payment_item_part", client, {"payment_key": "P-1", "part_id": "pp-1", "item_part_id": "ip-1"})
        self.assertEqual(
            [req.url.path for req in self.requests],
            ["/v1/payments/P-1/parts", "/v1/payments/P-1/parts/pp-1", "/v1/payments/P-1/parts/pp-1/item-parts", "/v1/payments/P-1/parts/pp-1/item-parts/ip-1"],
        )
        self.refuse("get_payment_item_part", {"payment_key": "P-1", "part_id": "../pp-1", "item_part_id": "ip-1"})


class CustomObjectTests(TenantCase):
    def test_definition_create_read_update_and_delete(self) -> None:
        definitions = {"definitions": {"vehicle": {"schema": {"object": "vehicle", "label": "Vehicle", "properties": {"make__c": {"type": "string", "label": "Make"}}, "required": ["make__c"], "filterable": ["make__c"]}}}}

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/objects/migrations":
                return vendor_json(200, {"actions": json.loads(req.content)["actions"], "jobId": _JOB_ID})
            if req.method == "DELETE":
                return httpx.Response(200, headers=_HEADERS)
            if req.url.path.endswith("/vehicle"):
                return vendor_json(200, {"Id": "def-1", "type": "vehicle", "schema": definitions["definitions"]["vehicle"]["schema"]})
            if req.method == "GET":
                self.assertEqual(req.url.params["select"], "type")
                return vendor_json(200, {"count": 1, "definitions": {"vehicle": {"type": "vehicle"}}})
            return vendor_json(200, {"count": 1, "definitions": {"vehicle": {"Id": "def-1", "type": "vehicle"}}})

        client = self.client(vendor)
        self.run_op("create_custom_object_definitions", client, {"body": definitions})
        self.assertEqual(self.run_op("list_custom_object_definitions", client, {"select": "type"})["data"]["count"], 1)
        self.assertEqual(self.run_op("get_custom_object_definition", client, {"object": "vehicle"})["data"]["type"], "vehicle")
        migrated = self.run_op("update_custom_object_definition", client, {"body": {"actions": [{"type": "addField", "namespace": "default", "object": "vehicle", "field": {"name": "vin__c", "type": "string"}}]}})
        self.assertEqual(migrated["data"]["jobId"], _JOB_ID)
        deleted = self.run_op("delete_custom_object_definition", client, {"object": "vehicle"})
        self.assertIsNone(deleted["data"])
        self.assertEqual([req.method for req in self.requests], ["POST", "GET", "GET", "POST", "DELETE"])
        self.refuse("update_custom_object_definition", {"body": {"action": []}})
        self.refuse("list_custom_object_definitions", {"select": "label"})

    def test_definition_with_records_cannot_be_deleted(self) -> None:
        client = self.client(lambda _: vendor_json(400, refusal("71012560", "Cannot delete the definition of vehicle because records exist.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_custom_object_definition", client, {"object": "vehicle"})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("records exist", str(raised.exception))

    def test_record_create_replace_with_if_match_patch_clear_and_delete(self) -> None:
        seen: list[tuple[str, str, dict[str, str], Any]] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            seen.append((req.method, req.url.path, dict(req.headers), json.loads(req.content) if req.content else None))
            if req.method == "POST":
                return vendor_json(200, {"records": [custom_record("Ford")]})
            if req.method == "PUT" and req.headers.get("If-Match") == "stale-etag":
                return vendor_json(412, refusal("71001000", "The record was modified by another request."))
            if req.method == "DELETE":
                return httpx.Response(200, headers=_HEADERS)
            record = custom_record("Ford")
            if req.method == "PATCH":
                record["model__c"] = None
            return vendor_json(200, record)

        client = self.client(vendor)
        rec = {"object": "vehicle", "record_id": _RECORD_ID}
        created = self.run_op("create_custom_object_records", client, {"object": "vehicle", "body": {"allowPartialSuccess": True, "records": [{"make__c": "Ford", "model__c": "Model T"}]}})
        self.assertEqual(created["data"]["records"][0]["Id"], _RECORD_ID)
        self.assertEqual(self.run_op("get_custom_object_record", client, rec)["data"]["make__c"], "Ford")
        self.run_op("update_custom_object_record", client, {**rec, "body": {"make__c": "Ford", "model__c": "Model A"}, "if_match": "etag-2"})
        with self.assertRaises(ZuoraServiceError) as stale:
            self.run_op("update_custom_object_record", client, {**rec, "body": {"make__c": "Ford"}, "if_match": "stale-etag"})
        self.assertEqual(stale.exception.code, "zuora.validation_failed")
        self.assertIn("modified by another request", str(stale.exception))
        patched = self.run_op("patch_custom_object_record", client, {**rec, "body": {"model__c": None}})
        self.assertIsNone(patched["data"]["model__c"])
        self.run_op("delete_custom_object_record", client, rec)
        self.assertEqual([m for m, *_ in seen], ["POST", "GET", "PUT", "PUT", "PATCH", "DELETE"])
        self.assertEqual(seen[2][2]["if-match"], "etag-2")
        self.assertEqual(seen[4][3], {"model__c": None})
        self.assertEqual(seen[0][1], "/objects/records/default/vehicle")
        self.refuse("create_custom_object_records", {"object": "vehicle", "body": {"allowPartialSuccess": True}})
        self.refuse("patch_custom_object_record", rec)

    def test_record_list_follows_cursor_and_mirrors_vendor_rules(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if "cursor" not in req.url.params:
                self.assertEqual(req.url.params["q"], "make__c.EQ:Ford")
                return vendor_json(200, {"count": 2, "records": [custom_record("Ford"), custom_record("Ford")], "cursor": "b3Zlcg=="})
            self.assertEqual(req.url.params["cursor"], "b3Zlcg==")
            return vendor_json(200, {"count": 1, "records": [custom_record("Ford")]})

        client = self.client(vendor)
        first = self.run_op("list_custom_object_records", client, {"object": "vehicle", "q": "make__c.EQ:Ford", "page_size": 2})
        self.assertEqual(first["pagination"]["next_cursor"], "b3Zlcg==")
        last = self.run_op("list_custom_object_records", client, {"object": "vehicle", "page_size": 2, "cursor": first["pagination"]["next_cursor"]})
        self.assertIsNone(last["pagination"]["next_cursor"])
        by_ids = self.client(lambda req: vendor_json(200, {"count": 2, "records": [custom_record("Ford")] * len(req.url.params.get_list("ids"))}))
        self.run_op("list_custom_object_records", by_ids, {"object": "vehicle", "ids": [_RECORD_ID, "82ecc9f7-b192-4f88-a4a3-4b2af6c750a1"]})
        self.assertEqual(self.requests[-1].url.params.get_list("ids"), [_RECORD_ID, "82ecc9f7-b192-4f88-a4a3-4b2af6c750a1"])
        self.refuse("list_custom_object_records", {"object": "vehicle", "cursor": "b3Zlcg=="})
        self.refuse("list_custom_object_records", {"object": "vehicle", "ids": []})

    def test_batch_update_and_delete(self) -> None:
        client = self.client(lambda req: vendor_json(200, {"success": True} if json.loads(req.content)["action"]["type"] == "delete" else {"records": {_RECORD_ID: custom_record("Fiat")}}))
        self.run_op("batch_custom_object_records", client, {"object": "vehicle", "body": {"action": {"type": "update", "allowPartialSuccess": True, "records": {_RECORD_ID: {"make__c": "Fiat"}}}}})
        self.run_op("batch_custom_object_records", client, {"object": "vehicle", "body": {"action": {"type": "delete", "ids": [_RECORD_ID]}}})
        self.assertEqual([req.url.path for req in self.requests], ["/objects/batch/default/vehicle"] * 2)
        self.refuse("batch_custom_object_records", {"object": "vehicle", "body": {"ids": [_RECORD_ID]}})


class BulkJobTests(TenantCase):
    def test_submit_upload_poll_errors_and_cancel(self) -> None:
        csv = b"make__c,model__c\nFord,Model T\nFiat,500\n"
        source = self.root / "vehicles.csv"
        source.write_bytes(csv)

        def vendor(req: httpx.Request) -> httpx.Response:
            job = {"Id": _JOB_ID, "namespace": "default", "object": "vehicle", "operation": "create", "status": "pending", "CreatedById": "u-1", "CreatedDate": "2026-11-07T10:00:00Z", "UpdatedById": "u-1", "UpdatedDate": "2026-11-07T10:00:00Z"}
            if req.url.path.endswith("/files"):
                self.assertEqual(req.headers["Content-Type"], "text/csv")
                self.assertEqual(req.content, csv)
                return vendor_json(200, {**job, "status": "accepted"})
            if req.url.path.endswith("/errors"):
                return vendor_json(200, {"errors": [{"row": 2, "code": 71012010, "message": "make__c is required", "record": {"model__c": "500"}}]})
            if req.url.path.endswith("/cancel"):
                self.assertEqual(req.method, "PATCH")
                return vendor_json(200, {**job, "status": "cancelled"})
            if req.method == "POST":
                return vendor_json(200, job)
            return vendor_json(200, {**job, "status": "completed", "recordsProcessed": 2, "processingTime": 1200})

        client = self.client(vendor)
        submitted = self.run_op("submit_custom_object_job", client, {"body": {"namespace": "default", "object": "vehicle", "operation": "create"}})
        uploaded = self.run_op("upload_custom_object_job_file", client, {"job_id": submitted["data"]["Id"], "input_path": str(source)})
        self.assertEqual(uploaded["data"]["status"], "accepted")
        self.assertEqual(self.run_op("get_custom_object_job", client, {"job_id": _JOB_ID})["data"]["recordsProcessed"], 2)
        self.assertEqual(self.run_op("list_custom_object_job_errors", client, {"job_id": _JOB_ID})["data"]["errors"][0]["row"], 2)
        self.assertEqual(self.run_op("cancel_custom_object_job", client, {"job_id": _JOB_ID})["data"]["status"], "cancelled")
        self.assertIsNone(self.requests[-1].content or None)
        self.refuse("submit_custom_object_job", {"body": {"namespace": "default", "object": "vehicle"}})

    def test_upload_refuses_outside_roots_wrong_suffix_and_empty_file_before_network(self) -> None:
        outside = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        outside.write(b"a\n1\n")
        outside.close()
        self.addCleanup(Path(outside.name).unlink)
        (self.root / "rows.txt").write_bytes(b"a\n1\n")
        (self.root / "empty.csv").write_bytes(b"")
        client = self.client(lambda _: self.fail("no vendor request expected"))
        for path in (outside.name, str(self.root / "rows.txt"), str(self.root / "empty.csv"), str(self.root / "missing.csv")):
            with self.subTest(path=path), self.assertRaises((ValueError, PathRefusedError)):
                self.run_op("upload_custom_object_job_file", client, {"job_id": _JOB_ID, "input_path": path})
        with self.assertRaises(ValueError):
            typed_verbs.validate_typed("upload_custom_object_job_file", {"job_id": _JOB_ID})
        self.assertEqual((self.token_mints, self.requests), (0, []))

    def test_job_list_cursor_ends_on_a_short_page(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.params["status"], "completed")
            count = 2 if "cursor" not in req.url.params else 1
            jobs = [{"Id": f"job-{n}", "status": "completed", "operation": "create", "object": "vehicle", "namespace": "default"} for n in range(count)]
            return vendor_json(200, {"count": count, "jobs": jobs, "cursor": jobs[-1]["Id"]})

        client = self.client(vendor)
        first = self.run_op("list_custom_object_jobs", client, {"page_size": 2, "status": "completed"})
        self.assertEqual(first["pagination"]["next_cursor"], "job-1")
        last = self.run_op("list_custom_object_jobs", client, {"page_size": 2, "status": "completed", "cursor": "job-1"})
        self.assertIsNone(last["pagination"]["next_cursor"])
        self.assertEqual(last["pagination"]["vendor"], "job-0")


class ObjectQueryTests(TenantCase):
    def test_list_sends_documented_array_parameters_and_follows_next_page(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/object-query/accounts")
            params = req.url.params
            self.assertEqual(params.get_list("filter[]"), ["currency.EQ:CAD", "status.EQ:Active"])
            self.assertEqual(params.get_list("expand[]"), ["billto"])
            self.assertEqual(params["sort[]"], "name.DESC")
            self.assertEqual(params["account.fields[]"], "name,accountNumber,balance")
            self.assertEqual(params["billto.fields[]"], "firstname,lastname")
            self.assertEqual(params["fields[]"], "id,name")
            self.assertEqual(params["pageSize"], "2")
            accounts = [{"id": f"8a90acct{n}", "name": f"Oh Canada {n}", "accountNumber": f"A0000{n}", "balance": 10.0 * n, "billTo": {"firstName": "Grace", "lastName": "Hopper"}} for n in (1, 2)]
            body: dict[str, Any] = {"data": accounts}
            if "cursor" not in params:
                body["nextPage"] = "W3sidmFsdWUiOiJPaCBDYW5hZGEifV0="
            return vendor_json(200, body)

        client = self.client(vendor)
        params: dict[str, Any] = {
            "object": "accounts",
            "page_size": 2,
            "filter": ["currency.EQ:CAD", "status.EQ:Active"],
            "expand": ["billto"],
            "sort": "name.DESC",
            "fields": ["id", "name"],
            "object_fields": {"account": ["name", "accountNumber", "balance"], "billto": ["firstname", "lastname"]},
            "include_null_fields": False,
        }
        first = self.run_op("query_objects", client, params)
        self.assertEqual(first["pagination"]["next_cursor"], "W3sidmFsdWUiOiJPaCBDYW5hZGEifV0=")
        last = self.run_op("query_objects", client, {**params, "cursor": first["pagination"]["next_cursor"]})
        self.assertIsNone(last["pagination"]["next_cursor"])
        self.assertEqual(self.requests[0].url.params["includeNullFields"], "false")

    def test_get_by_key_on_a_custom_object_and_vendor_rules(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual(req.url.path, "/object-query/default__cars/A-CAR-1")
            self.assertEqual(req.headers["Zuora-Entity-Ids"], "entity-1")
            return vendor_json(200, {"id": "a1b2", "make__c": "Ford"})

        result = self.run_op("query_object", self.client(vendor), {"object": "default__cars", "key": "A-CAR-1", "zuora_entity_ids": "entity-1"})
        self.assertEqual(result["data"]["make__c"], "Ford")
        self.assertNotIn("pagination", result)
        self.refuse("query_objects", {"object": "accounts", "page_size": 100})
        self.refuse("query_objects", {"object": "accounts", "object_fields": {"account": []}})
        self.refuse("query_objects", {"object": "accounts", "object_fields": ["name"]})
        self.refuse("query_objects", {"object": "../v1/accounts"})
        self.refuse("query_object", {"object": "accounts"})

    def test_vendor_query_error_is_reported_with_reasons(self) -> None:
        client = self.client(lambda _: vendor_json(400, refusal(50000020, "invoice-items requires a filter[] query parameter.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("query_objects", client, {"object": "invoice-items"})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("filter[]", str(raised.exception))
        self.denied("query_objects", {"object": "payments"}, "The role has no Object Query access.")


class Build3FollowUpHeaderTests(TenantCase):
    """Build 3 review follow-up: the Workflow and Files verbs take the request headers their reference documents."""

    def test_workflow_routes_take_exactly_their_documented_headers(self) -> None:
        ops = {op.name: op for op in workflow_actions.OPERATIONS}
        self.assertEqual(set(ops), set(workflow_actions.WORKFLOW_HEADER_KEYS))
        self.assertEqual(set(ops["export_workflow"].header_fields), {"zuora_track_id"})
        self.assertEqual(set(ops["list_workflows"].header_fields), {"zuora_entity_ids", "zuora_org_ids", "zuora_track_id"})
        self.assertEqual(set(ops["run_workflow"].header_fields), {"idempotency_key", "zuora_entity_ids", "zuora_org_ids", "zuora_track_id"})
        self.assertEqual(set(ops["create_workflow_auth_provider"].header_fields), {"idempotency_key", "zuora_track_id", "zuora_version"})

        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.headers["Zuora-Entity-Ids"], req.headers["Idempotency-Key"]), ("entity-1", "run-77"))
            return vendor_json(200, {"id": 9001, "status": "Queued", "type": "Workflow::Instance"})

        run = execute(ops["run_workflow"], self.client(vendor), {"workflow_id": 12, "zuora_entity_ids": "entity-1", "idempotency_key": "run-77"}, self.gate)
        self.assertEqual(run["data"]["status"], "Queued")
        with self.assertRaises(ValueError):
            execute(ops["export_workflow"], self.client(lambda _: self.fail("no vendor request expected")), {"workflow_id": 12, "zuora_track_id": "trace;1"}, self.gate)

    def test_files_routes_send_the_v1_headers(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.headers["Zuora-Version"], req.headers["Zuora-Org-Ids"]), ("329.0", "org-1"))
            return vendor_json(200, {"success": True, "fileId": "2c92c0f8file", "status": "Archived"})

        status = execute(data_query_actions.FILE_STATUS, self.client(vendor), {"file_id": "2c92c0f8file", "zuora_version": "329.0", "zuora_org_ids": "org-1"}, self.gate)
        self.assertEqual(status["data"]["status"], "Archived")
        for op in (data_query_actions.DOWNLOAD_FILE, data_query_actions.FILE_STATUS, data_query_actions.RESTORE_FILE):
            self.assertEqual(set(op.header_fields), {"zuora_version", "zuora_entity_ids", "zuora_org_ids", "zuora_track_id"}, op.name)


class CredentialAndDispatchTests(TenantCase):
    def test_expired_token_reminted_once_then_reported(self) -> None:
        statuses = iter((401, 200))
        recovering = self.client(lambda _: vendor_json(next(statuses), {"data": []}))
        self.assertEqual(self.run_op("query_objects", recovering, {"object": "usages"})["status"], 200)
        self.assertEqual(self.token_mints, 2)
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_payment", self.client(lambda _: vendor_json(401, {"message": "Authentication error"})), {"payment_key": "P-1"})
        self.assertEqual(raised.exception.code, "zuora.auth_failed")

    def test_dispatch_queues_without_network_and_worker_completes(self) -> None:
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "query-job", "status": "queued"}}
        params = {"object": "subscriptions", "filter": ["status.EQ:Active"]}
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
        self.plugin._async_job_manager = manager  # noqa: SLF001
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(200, {"data": [{"subscriptionNumber": "A-S1", "status": "Active"}]}))  # noqa: SLF001
        with patch.object(async_jobs, "ensure_worker_started"):
            queued = self.plugin.query_objects(params, dict(_STATE))
            self.assertEqual(queued["data"]["status"], "queued")
            self.assertEqual(self.requests, [])
            refused = self.plugin.create_payment({"body": {"amount": 5.0}}, dict(_STATE))
            self.assertEqual(refused["error"]["code"], "zuora.invalid_params")
            refused_upload = self.plugin.upload_custom_object_job_file({"job_id": _JOB_ID}, dict(_STATE))
            self.assertEqual(refused_upload["error"]["code"], "zuora.invalid_params")
            self.assertEqual(manager.create_job.call_count, 1)
            async_jobs._process_job(self.plugin, manager, "query-job", "query_objects")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "completed")
        self.assertEqual(completion["result"]["data"]["data"][0]["subscriptionNumber"], "A-S1")
        self.assertIsNone(completion["result"]["pagination"]["next_cursor"])

    def test_worker_reports_success_false_as_job_error(self) -> None:
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": {"payment_key": "P-1"}}}
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(200, refusal(53510040, "Only external payments can be cancelled.")))  # noqa: SLF001
        async_jobs._process_job(self.plugin, manager, "payment-job", "cancel_payment")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "error")
        self.assertEqual(completion["error"]["code"], "zuora.validation_failed")
        self.assertIn("Only external payments", completion["error"]["message"])


if __name__ == "__main__":
    unittest.main()
