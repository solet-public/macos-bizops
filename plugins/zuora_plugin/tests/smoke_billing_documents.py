"""Hermetic unittest coverage for the typed Zuora Billing Documents, Invoices, Credit and Debit Memos, Bill Run and Usage verbs (build 5).

httpx.MockTransport plays the tenant. Every transcript follows the response
fields in Zuora's v1 API reference (Billing Documents, Invoices, Credit
Memos, Debit Memos, Bill Run and Usage sections, reference version
2026-11-06): ``success`` plus ``processId``, ``reasons[{code, message}]`` and
``requestId`` error envelopes, top-level ``nextPage`` URLs on the page-numbered
lists, ``documents``/``creditmemos``/``debitmemos``/``applicationParts``/
``invoiceFiles`` collections, ``jobId``/``jobStatus`` on large-document
operations, ``checkImportStatus`` on usage import, the capitalised ``Success``
and ``Id`` of the object usage routes, and Zuora-Request-Id and RateLimit
headers. No vendor is contacted.

Run: SOLET_NAME=<name> .venv/bin/python plugins/zuora_plugin/tests/smoke_billing_documents.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from zuora_plugin import async_jobs, bill_run_usage_actions, billing_actions, billing_document_actions, typed_verbs  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.constants import REQUEST_REDACTION  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.export_containment import ExportPathRefusedError  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402
from zuora_plugin.typed_operations import Operation, PathGate, execute  # noqa: E402

_CLIENT_ID = "fixture-client-id"
_SECRET = "fixture-client-secret"
_TOKEN = "fixture-access-token"
_BASE = "https://tenant.example.test"
_STATE = {"session_id": "billing-session", "flow_id": "billing-flow"}
_HEADERS = {"Zuora-Request-Id": "7d2e3f40-req", "RateLimit-Limit": "40", "RateLimit-Remaining": "37", "RateLimit-Reset": "1"}
_DOCUMENT_OPS = (
    *billing_document_actions.BILLING_DOCUMENT_OPERATIONS,
    *billing_document_actions.INVOICE_OPERATIONS,
    *billing_document_actions.CREDIT_MEMO_OPERATIONS,
    *billing_document_actions.DEBIT_MEMO_OPERATIONS,
)
_RUN_USAGE_OPS = (*bill_run_usage_actions.BILL_RUN_OPERATIONS, *bill_run_usage_actions.USAGE_OPERATIONS)
_OPS: dict[str, Operation] = {op.name: op for op in (*_DOCUMENT_OPS, *_RUN_USAGE_OPS)}
_UPLOADS = {"upload_invoice_file", "upload_credit_memo_file", "upload_debit_memo_file", "import_usage"}
_INVOICE_ID = "8a90e5e48c1f4e2b018c2a7d1e3finv1"
_CM_ID = "8a90e5e48c1f4e2b018c2a7d1e3fcm01"
_DM_ID = "8a90e5e48c1f4e2b018c2a7d1e3fdm01"
_BILL_RUN_ID = "8a90e5e48c1f4e2b018c2a7d1e3fbr01"
_USAGE_ID = "8a90e5e48c1f4e2b018c2a7d1e3fus01"
_PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"

Handler = Callable[[httpx.Request], httpx.Response]


def vendor_json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body, headers=_HEADERS)


def refusal(code: int | str, message: str) -> dict[str, Any]:
    """Zuora's v1 error envelope."""
    return {"success": False, "processId": "6A5B4C3D2E1F0A9B", "reasons": [{"code": code, "message": message}], "requestId": "52b4c5d6-e7f8-4091-a2b3-c4d5e6f7a8b9"}


def memo(kind: str, memo_id: str, number: str, status: str = "Posted", **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "success": True,
        "id": memo_id,
        "number": number,
        "accountId": "8a90e5e48c1f4e2b018c2a7d1e3facct",
        "accountNumber": "A00000101",
        "amount": 50.0,
        "currency": "USD",
        "status": status,
        "referredInvoiceId": _INVOICE_ID,
        "reasonCode": "Correcting invoice error",
        "createdDate": "2026-11-07 09:10:00",
    }
    body["creditMemoDate" if kind == "credit" else "debitMemoDate"] = "2026-11-07"
    return {**body, **extra}


def every_encoding(*values: str) -> list[str]:
    forms: list[str] = []
    for value in values:
        forms.extend([value, quote(value, safe=""), base64.b64encode(value.encode()).decode(), base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")])
    return forms


class TenantCase(unittest.TestCase):
    """A ZuoraClient over MockTransport plus a plugin gate rooted in a temporary workspace."""

    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="zuora_billing_smoke_")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.plugin = ZuoraPlugin()
        self.plugin.initialize({"export_allowed_roots": [str(self.root)]})
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

    def seen(self) -> list[tuple[str, str]]:
        return [(req.method, req.url.path) for req in self.requests]


class RegistrationTests(unittest.TestCase):
    def test_every_build5_verb_has_decorator_edge_handler_and_process_json(self) -> None:
        plugin = ZuoraPlugin()
        plugin.initialize({})
        actions = {action.name: action for action in discover_actions(plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", plugin, list(actions.values()))
        edges = plugin.get_edge_process_definitions()
        self.assertEqual((len(_DOCUMENT_OPS), len(_RUN_USAGE_OPS)), (80, 14))
        self.assertEqual(len(actions), 227)
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
                body = ({"input_path"} if name in _UPLOADS else {"body"}) if op.body is not None else set()
                self.assertEqual(set(actions[name].parameters), {*op.path_fields, *op.query_fields, *op.header_fields} | body)

    def test_documented_header_sets(self) -> None:
        v1 = {"zuora_version", "zuora_entity_ids", "zuora_org_ids", "zuora_track_id"}
        for name in ("get_invoice_pdf_status", "get_credit_memo_pdf_status", "get_debit_memo_pdf_status"):
            self.assertEqual(set(_OPS[name].header_fields), v1 - {"zuora_org_ids"}, name)
        self.assertEqual(set(_OPS["create_usage"].header_fields), v1 | {"idempotency_key", "x_zuora_wsdl_version"})
        self.assertEqual(set(_OPS["write_off_invoice_async"].header_fields), v1)
        for name in ("create_standalone_invoice", "refund_credit_memo", "collect_debit_memo", "import_usage", "create_bill_run", "email_bill_run_documents"):
            self.assertIn("idempotency_key", _OPS[name].header_fields, name)
        for name in ("delete_invoice", "apply_credit_memo", "post_bill_run", "update_usage"):
            self.assertNotIn("idempotency_key", _OPS[name].header_fields, name)
        for name in ("create_usage", "import_usage", "get_usage"):
            self.assertIn("x_zuora_wsdl_version", _OPS[name].header_fields, name)
        for name in ("update_usage", "delete_usage", "get_usage_rate_detail"):
            self.assertNotIn("x_zuora_wsdl_version", _OPS[name].header_fields, name)


# Every top-level body field any build 5 route marks required, so one body satisfies every verb's check.
_REQUIRED_BODY: dict[str, Any] = {
    "effectiveDate": "2026-11-07",
    "targetDate": "2026-12-01",
    "invoiceDate": "2026-11-07",
    "invoiceId": _INVOICE_ID,
    "sourceType": "Invoice",
    "totalAmount": 10.0,
    "type": "External",
    "Quantity": 1,
    "StartDateTime": "2026-11-01T00:00:00",
    "UOM": "Each",
}
_QUERY_SAMPLES: dict[str, tuple[Any, str]] = {"text": ("q-1", "q-1"), "number": (1.5, "1.5"), "bool": (True, "true"), "count": (2, "2")}


class WireTests(TenantCase):
    """Every declared request header and query field of every build 5 verb reaches the vendor request, and an unset one does not."""

    def minimal(self, op: Operation) -> dict[str, Any]:
        params: dict[str, Any] = {name: 1 if spec.kind == "int_id" else f"K-{name}" for name, spec in op.path_fields.items()}
        params.update({name: "K-1,K-2" for name, spec in op.query_fields.items() if spec.required})
        if op.name in _UPLOADS:
            suffix = ".csv" if op.name == "import_usage" else ".pdf"
            source = self.root / f"upload{suffix}"
            source.write_bytes(_PDF if suffix == ".pdf" else b"ACCOUNT_ID,UOM,QTY\nA00000101,Each,1\n")
            params["input_path"] = str(source)
        elif op.body is not None:
            params["body"] = dict(_REQUIRED_BODY)
        return params

    def test_every_declared_header_is_forwarded_and_none_is_invented(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"success": True}))
        for name, op in sorted(_OPS.items()):
            with self.subTest(verb=name):
                headers = {key: f"hdr-{key.replace('_', '-')}" for key in op.header_fields}
                self.run_op(name, client, {**self.minimal(op), **headers})
                sent = self.requests[-1].headers
                for key, spec in op.header_fields.items():
                    self.assertEqual(sent[spec.vendor], headers[key], spec.vendor)
                self.run_op(name, client, self.minimal(op))
                bare = self.requests[-1].headers
                for spec in op.header_fields.values():
                    self.assertNotIn(spec.vendor, bare)

    def test_every_declared_query_field_is_forwarded_with_its_vendor_name(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"success": True}))
        with_query = {name: op for name, op in _OPS.items() if op.query_fields}
        self.assertEqual(len(with_query), 19)  # the vendor reference documents query parameters on 19 of these 94 routes
        for name, op in sorted(with_query.items()):
            with self.subTest(verb=name):
                params = self.minimal(op)
                expected: dict[str, str] = {}
                for key, spec in op.query_fields.items():
                    if spec.required:
                        expected[spec.vendor] = params[key]
                        continue
                    value, wire = _QUERY_SAMPLES[spec.kind]
                    params[key], expected[spec.vendor] = value, wire
                self.run_op(name, client, params)
                self.assertEqual(dict(self.requests[-1].url.params), expected)


class InvoiceTests(TenantCase):
    def test_standalone_invoice_create_post_correct_unpost_cancel_and_delete(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path == "/v1/invoices" and req.method == "POST":
                return vendor_json(200, {"success": True, "id": _INVOICE_ID, "invoiceNumber": "INV00000201", "status": "Draft", "amount": 300.0, "balance": 300.0, "invoiceDate": "2026-11-07"})
            if path == "/v1/invoices/bulk-post":
                return vendor_json(200, {"success": True, "invoices": [{"id": _INVOICE_ID, "success": True}]})
            if req.method == "DELETE":
                return vendor_json(200, {"success": True, "id": _INVOICE_ID, "jobId": None, "jobStatus": "Completed"})
            status = {"unpost": "Draft", "cancel": "Canceled"}.get(path.rsplit("/", 1)[-1], "Posted")
            return vendor_json(200, {"success": True, "id": _INVOICE_ID, "invoiceNumber": "INV00000201", "status": status, "dueDate": "2026-12-31"})

        client = self.client(vendor)
        key = {"invoice_key": "INV00000201"}
        body = {"accountNumber": "A00000101", "invoiceDate": "2026-11-07", "status": "Draft", "invoiceItems": [{"amount": 300.0, "chargeName": "Onboarding", "serviceStartDate": "2026-11-07"}]}
        created = self.run_op("create_standalone_invoice", client, {"body": body, "idempotency_key": "inv-acme-1"})
        self.assertEqual(created["data"]["status"], "Draft")
        posted = self.run_op("post_invoices", client, {"body": {"invoices": [{"id": _INVOICE_ID, "invoiceDate": "2026-11-07"}]}})
        self.assertTrue(posted["data"]["invoices"][0]["success"])
        self.run_op("update_invoice", client, {**key, "body": {"dueDate": "2026-12-31", "comments": ""}})
        self.assertEqual(self.run_op("unpost_invoice", client, key)["data"]["status"], "Draft")
        self.assertEqual(self.run_op("cancel_invoice", client, {**key, "zuora_version": "329.0"})["data"]["status"], "Canceled")
        self.assertEqual(self.run_op("delete_invoice", client, key)["data"]["jobStatus"], "Completed")
        self.assertEqual(
            self.seen(),
            [
                ("POST", "/v1/invoices"),
                ("POST", "/v1/invoices/bulk-post"),
                ("PUT", "/v1/invoices/INV00000201"),
                ("PUT", "/v1/invoices/INV00000201/unpost"),
                ("PUT", "/v1/invoices/INV00000201/cancel"),
                ("DELETE", "/v1/invoices/INV00000201"),
            ],
        )
        self.assertEqual(self.requests[0].headers["Idempotency-Key"], "inv-acme-1")
        self.assertEqual(json.loads(self.requests[2].content)["comments"], "")
        self.assertEqual(self.requests[4].headers["Zuora-Version"], "329.0")
        self.assertIsNone(self.requests[3].content or None)
        self.refuse("create_standalone_invoice", {"body": {"accountNumber": "A00000101"}})
        self.refuse("update_invoice", key)
        self.refuse("cancel_invoice", {"invoice_key": "../accounts"})

    def test_reverse_split_write_off_sync_and_async(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/write-off-async"):
                return vendor_json(200, {"success": True, "jobId": "wo-job-1", "jobStatus": "Pending"})
            if "/write-off-async-jobs/" in path:
                return vendor_json(200, {"success": True, "id": "wo-job-1", "status": "Completed", "invoiceId": _INVOICE_ID, "creditMemoId": _CM_ID})
            if path.endswith("/write-off") or path.endswith("/reverse"):
                return vendor_json(200, {"success": True, "creditMemo": {"id": _CM_ID}})
            return vendor_json(200, {"success": True, "jobId": None, "jobStatus": "Completed", "invoices": [{"id": "8a90split1"}, {"id": "8a90split2"}]})

        client = self.client(vendor)
        key = {"invoice_key": _INVOICE_ID}
        self.assertEqual(self.run_op("reverse_invoice", client, key)["data"]["creditMemo"]["id"], _CM_ID)
        self.run_op("split_invoice", client, {**key, "body": {"splitType": "Percentage", "splits": [{"percentage": 60}, {"percentage": 40}]}})
        self.run_op("write_off_invoice", client, {**key, "body": {"memoDate": "2026-11-07", "reasonCode": "Write-off"}})
        job = self.run_op("write_off_invoice_async", client, key)["data"]["jobId"]
        self.assertEqual(self.run_op("get_invoice_write_off_async_job", client, {"async_job_id": job})["data"]["creditMemoId"], _CM_ID)
        self.assertEqual(json.loads(self.requests[0].content), {})
        self.assertEqual(self.seen()[3], ("POST", f"/v1/invoices/{_INVOICE_ID}/write-off-async"))
        self.assertEqual(self.seen()[4], ("GET", "/v1/invoices/write-off-async-jobs/wo-job-1"))
        self.refuse("split_invoice", key)

    def test_reverse_refused_with_payments_applied_is_a_vendor_refusal(self) -> None:
        client = self.client(lambda _: vendor_json(200, refusal(59210020, "You cannot reverse an invoice with payments or credit memos applied.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("reverse_invoice", client, {"invoice_key": _INVOICE_ID})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")
        self.assertIn("payments or credit memos applied", str(raised.exception))
        self.denied("delete_invoice", {"invoice_key": _INVOICE_ID}, "Delete Invoice permission is required.")

    def test_items_taxation_files_application_parts_and_pdf_status(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/application-parts"):
                body: dict[str, Any] = {"success": True, "applicationParts": [{"appliedAmount": 40.0, "paymentId": "8a90pay1", "creditMemoId": None}]}
                if not req.url.params:
                    body["nextPage"] = f"{_BASE}/v1/invoices/{_INVOICE_ID}/application-parts?page=2&pageSize=20"
                return vendor_json(200, body)
            if path == "/v1/invoices/pdf-status":
                keys = req.url.params["invoiceKeys"].split(",")
                return vendor_json(200, {"success": True, "invoiceFiles": [{"invoiceNumber": key, "pdfGenerationStatus": "Generated", "pdfFileURL": f"/v1/files/{key}"} for key in keys]})
            if path.endswith("/taxation-items") and req.method == "GET":
                return vendor_json(200, {"success": True, "taxationItems": [{"jurisdiction": "CALIFORNIA", "name": "STATE TAX", "taxAmount": 6.0, "taxRate": 0.06, "taxRateType": "Percentage"}]})
            if path.endswith("/taxation-items"):
                return vendor_json(200, {"success": True, "taxationItems": [{"id": "8a90tax1", "taxAmount": 6.0}]})
            if path.endswith("/files"):
                return vendor_json(200, {"success": True, "invoiceFiles": [{"id": "8a90file2", "versionNumber": 2, "pdfFileUrl": "/v1/files/8a90file2"}]})
            page = int(req.url.params.get("page", "1"))
            items = {"success": True, "invoiceItems": [{"id": f"8a90item{page}", "chargeAmount": 100.0, "taxAmount": 6.0, "balance": 106.0}]}
            return vendor_json(200, {**items, "nextPage": f"{_BASE}{path}?page=2&pageSize=1"} if page == 1 else items)

        client = self.client(vendor)
        key = {"invoice_key": _INVOICE_ID}
        first = self.run_op("list_invoice_items", client, {**key, "page": 1, "page_size": 1})
        self.assertEqual(first["pagination"]["next_page"], 2)
        self.assertIsNone(self.run_op("list_invoice_items", client, {**key, "page": 2, "page_size": 1})["pagination"]["next_page"])
        parts = self.run_op("list_invoice_application_parts", client, key)
        self.assertEqual(parts["pagination"]["next_path"], f"/v1/invoices/{_INVOICE_ID}/application-parts?page=2&pageSize=20")
        self.assertEqual(parts["data"]["applicationParts"][0]["paymentId"], "8a90pay1")
        taxes = self.run_op("list_invoice_item_taxation_items", client, {**key, "item_id": "8a90item1", "page_size": 20})
        self.assertEqual(taxes["data"]["taxationItems"][0]["jurisdiction"], "CALIFORNIA")
        self.run_op("create_invoice_taxation_items", client, {**key, "body": {"taxationItems": [{"invoiceItemId": "8a90item1", "jurisdiction": "CALIFORNIA", "name": "STATE TAX", "taxAmount": 6.0, "taxDate": "2026-11-07", "taxRate": 0.06, "taxRateType": "Percentage"}]}})
        self.assertEqual(self.run_op("list_invoice_files", client, {**key, "page_size": 5})["data"]["invoiceFiles"][0]["versionNumber"], 2)
        status = self.run_op("get_invoice_pdf_status", client, {"invoice_keys": "INV001,INV002", "zuora_track_id": "pdf-check-1"})
        self.assertEqual([row["pdfGenerationStatus"] for row in status["data"]["invoiceFiles"]], ["Generated", "Generated"])
        self.assertEqual(self.requests[-1].headers["Zuora-Track-Id"], "pdf-check-1")
        self.refuse("get_invoice_pdf_status", {"invoice_keys": ",".join(f"INV{n:03d}" for n in range(51))})
        self.refuse("get_invoice_pdf_status", {})
        self.refuse("list_invoice_items", {**key, "page": 2})
        self.refuse("get_invoice_pdf_status", {"invoice_keys": "INV001", "zuora_track_id": "bad;id"})

    def test_application_parts_without_next_page_end_the_list(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"success": True, "applicationParts": []}))
        parts = self.run_op("list_debit_memo_application_parts", client, {"debit_memo_id": _DM_ID})
        self.assertIsNone(parts["pagination"]["next_path"])
        self.assertEqual(self.seen(), [("GET", f"/v1/debit-memos/{_DM_ID}/application-parts")])

    def test_billing_documents_list_generate_and_files_deletion_job(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/v1/billing-documents":
                self.assertEqual((req.url.params["accountNumber"], req.url.params["status"], req.url.params["sort"]), ("A00000101", "Posted", "-documentDate"))
                page = int(req.url.params["page"])
                docs = [{"id": f"doc-{page}", "documentNumber": f"INV0000{page}", "documentType": "Invoice", "amount": 10.0, "balance": 10.0, "status": "Posted"}]
                return vendor_json(200, {"success": True, "documents": docs, **({"nextPage": f"{_BASE}/v1/billing-documents?page=2&pageSize=1"} if page == 1 else {})})
            if req.url.path.endswith("/generate"):
                return vendor_json(200, {"success": True, "invoices": [{"id": _INVOICE_ID}], "creditMemos": []})
            if req.method == "POST":
                return vendor_json(200, {"success": True, "id": "del-job-1", "status": "Pending"})
            return vendor_json(200, {"success": True, "id": "del-job-1", "status": "Completed"})

        client = self.client(vendor)
        filters = {"account_number": "A00000101", "status": "Posted", "sort": "-documentDate", "page_size": 1}
        self.assertEqual(self.run_op("list_billing_documents", client, {**filters, "page": 1})["pagination"]["next_page"], 2)
        self.assertIsNone(self.run_op("list_billing_documents", client, {**filters, "page": 2})["pagination"]["next_page"])
        generated = self.run_op("generate_billing_documents", client, {"account_key": "A00000101", "body": {"effectiveDate": "2026-11-07", "targetDate": "2026-12-01", "autoPost": True}})
        self.assertEqual(generated["data"]["invoices"][0]["id"], _INVOICE_ID)
        self.run_op("create_billing_document_files_deletion_job", client, {"body": {"accountKeys": ["A00000101"]}, "idempotency_key": "erase-1"})
        self.assertEqual(self.run_op("get_billing_document_files_deletion_job", client, {"job_id": "del-job-1"})["data"]["status"], "Completed")
        self.assertEqual(self.seen()[2], ("POST", "/v1/accounts/A00000101/billing-documents/generate"))
        self.refuse("generate_billing_documents", {"account_key": "A00000101", "body": {"targetDate": "2026-12-01"}})


class CreditMemoTests(TenantCase):
    def test_create_from_invoice_post_apply_unapply_and_async_jobs(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("-async"):
                return vendor_json(200, {"success": True, "jobId": "cm-job-1", "jobStatus": "Pending"})
            if "/apply-async-jobs/" in path or "/unapply-async-jobs/" in path:
                return vendor_json(200, {"success": True, "id": "cm-job-1", "status": "Completed", "errors": []})
            status = "Draft" if req.method == "POST" else "Posted"
            return vendor_json(200, memo("credit", _CM_ID, "CM00000101", status, appliedAmount=40.0 if path.endswith("/apply") else 0.0, unappliedAmount=10.0 if path.endswith("/apply") else 50.0))

        client = self.client(vendor)
        key = {"credit_memo_key": "CM00000101"}
        apply = {"invoices": [{"invoiceId": _INVOICE_ID, "amount": 40.0}], "effectiveDate": "2026-11-07"}
        created = self.run_op("create_credit_memo_from_invoice", client, {"invoice_key": "INV00000201", "body": {"invoiceId": _INVOICE_ID, "items": [{"invoiceItemId": "8a90item1", "amount": 50.0, "skuName": "SKU-1"}], "reasonCode": "Correcting invoice error"}})
        self.assertEqual(created["data"]["status"], "Draft")
        self.run_op("post_credit_memo", client, key)
        self.assertEqual(self.run_op("apply_credit_memo", client, {**key, "body": apply})["data"]["unappliedAmount"], 10.0)
        self.run_op("unapply_credit_memo", client, {**key, "body": apply})
        self.run_op("apply_credit_memo_async", client, {**key, "body": apply})
        self.run_op("unapply_credit_memo_async", client, {**key, "body": apply})
        self.run_op("get_credit_memo_apply_async_job", client, {"apply_async_job_id": "cm-job-1"})
        self.run_op("get_credit_memo_unapply_async_job", client, {"unapply_async_job_id": "cm-job-1"})
        self.assertEqual(
            self.seen(),
            [
                ("POST", "/v1/credit-memos/invoice/INV00000201"),
                ("PUT", "/v1/credit-memos/CM00000101/post"),
                ("PUT", "/v1/credit-memos/CM00000101/apply"),
                ("PUT", "/v1/credit-memos/CM00000101/unapply"),
                ("PUT", "/v1/credit-memos/CM00000101/apply-async"),
                ("PUT", "/v1/credit-memos/CM00000101/unapply-async"),
                ("GET", "/v1/credit-memos/apply-async-jobs/cm-job-1"),
                ("GET", "/v1/credit-memos/unapply-async-jobs/cm-job-1"),
            ],
        )
        self.refuse("create_credit_memo_from_invoice", {"invoice_key": "INV00000201", "body": {"items": []}})
        self.refuse("create_credit_memos", {"body": {"memos": []}})
        self.refuse("apply_credit_memo", key)

    def test_list_filters_and_pages(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            params = req.url.params
            self.assertEqual((params["status"], params["unappliedAmount"], params["autoApplyUponPosting"], params["referredInvoiceId"]), ("Posted", "50.0", "false", _INVOICE_ID))
            page = int(params["page"])
            body: dict[str, Any] = {"success": True, "creditmemos": [memo("credit", f"{_CM_ID}{page}", f"CM0000010{page}")]}
            if page == 1:
                body["nextPage"] = f"{_BASE}/v1/credit-memos?page=2&pageSize=1&status=Posted"
            return vendor_json(200, body)

        client = self.client(vendor)
        params = {"page_size": 1, "status": "Posted", "unapplied_amount": 50.0, "auto_apply_upon_posting": False, "referred_invoice_id": _INVOICE_ID}
        self.assertEqual(self.run_op("list_credit_memos", client, {**params, "page": 1})["pagination"]["next_page"], 2)
        self.assertIsNone(self.run_op("list_credit_memos", client, {**params, "page": 2})["pagination"]["next_page"])
        self.refuse("list_credit_memos", {"amount": "50"})
        self.refuse("list_credit_memos", {"auto_apply_upon_posting": "false"})

    def test_refund_redacts_payment_values_and_credentials_in_every_form(self) -> None:
        card, token = "4111111111111111", "gw-tok-9a8b7c6d"
        forms = every_encoding(card, token, _CLIENT_ID, _SECRET, _TOKEN)
        body = {"totalAmount": 10.0, "type": "Electronic", "paymentMethodId": "8a90pm1", "gatewayOptions": [{"key": "Note", "value": "x"}], "creditCard": {"cardNumber": card, "tokenId": token}}
        calls = iter(("ok", "refused"))

        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.method, req.url.path), ("POST", f"/v1/credit-memos/{_CM_ID}/refund"))
            if next(calls) == "ok":
                return vendor_json(200, {"success": True, "id": "8a90refund1", "number": "R-00000011", "amount": 10.0, "status": "Processed", "creditMemoId": _CM_ID, "gatewayResponse": " ".join(forms)})
            return vendor_json(400, refusal(53000020, f"Gateway declined {forms[0]} token {forms[4]} for client {forms[8]} secret {forms[13]} bearer {forms[18]}"))

        client = self.client(vendor)
        with self.assertLogs("zuora_plugin", level="INFO") as audit:
            refunded = self.run_op("refund_credit_memo", client, {"credit_memo_key": _CM_ID, "body": body, "idempotency_key": "refund-1"})
            with self.assertRaises(ZuoraServiceError) as rejected:
                self.run_op("refund_credit_memo", client, {"credit_memo_key": _CM_ID, "body": body})
        rendered = json.dumps(refunded) + str(rejected.exception) + " ".join(audit.output)
        for form in forms:
            self.assertNotIn(form, rendered)
        self.assertIn(REQUEST_REDACTION, refunded["data"]["gatewayResponse"])
        self.assertEqual(refunded["data"]["number"], "R-00000011")
        self.assertEqual(rejected.exception.code, "zuora.validation_failed")
        self.refuse("refund_credit_memo", {"credit_memo_key": _CM_ID, "body": {"type": "External"}})

    def test_reverse_write_off_cancel_delete_and_parts(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/write-off") or path.endswith("/reverse"):
                return vendor_json(200, {"success": True, "debitMemo": {"id": _DM_ID}})
            if "/parts" in path:
                part = {"id": "8a90part1", "amount": 40.0, "invoiceId": _INVOICE_ID, "debitMemoId": None}
                return vendor_json(200, {"success": True, "creditMemoParts": [part]} if path.endswith("/parts") else {"success": True, **part})
            if req.method == "DELETE":
                return vendor_json(200, {"success": True})
            return vendor_json(200, memo("credit", _CM_ID, "CM00000101", "Canceled"))

        client = self.client(vendor)
        self.assertEqual(self.run_op("reverse_credit_memo", client, {"credit_memo_key": "CM00000101", "body": {"memoDate": "2026-11-07"}})["data"]["debitMemo"]["id"], _DM_ID)
        self.run_op("write_off_credit_memo", client, {"credit_memo_id": _CM_ID})
        self.assertEqual(self.run_op("list_credit_memo_parts", client, {"credit_memo_key": "CM00000101", "page_size": 5})["data"]["creditMemoParts"][0]["invoiceId"], _INVOICE_ID)
        self.assertEqual(self.run_op("get_credit_memo_part", client, {"credit_memo_key": "CM00000101", "part_id": "8a90part1"})["data"]["amount"], 40.0)
        self.assertEqual(self.run_op("cancel_credit_memo", client, {"credit_memo_key": "CM00000101"})["data"]["status"], "Canceled")
        self.run_op("delete_credit_memo", client, {"credit_memo_key": "CM00000101"})
        self.assertEqual(self.seen()[1], ("PUT", f"/v1/credit-memos/{_CM_ID}/write-off"))
        self.assertEqual(json.loads(self.requests[1].content), {})
        self.assertEqual(self.seen()[-1], ("DELETE", "/v1/credit-memos/CM00000101"))

    def test_delete_of_a_posted_memo_is_the_vendor_refusal(self) -> None:
        client = self.client(lambda _: vendor_json(200, refusal(53000010, "Only credit memos with the Cancelled status can be deleted.")))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("delete_credit_memo", client, {"credit_memo_key": "CM00000101"})
        self.assertIn("Cancelled status", str(raised.exception))
        self.denied("apply_credit_memo", {"credit_memo_key": "CM00000101", "body": {"invoices": []}}, "Apply Credit Memo permission is required.")


class DebitMemoTests(TenantCase):
    def test_create_post_collect_write_off_and_due_dates(self) -> None:
        card = "5555555555554444"

        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/collect"):
                sent = json.loads(req.content)
                return vendor_json(
                    200, {"success": True, "debitMemo": {"id": _DM_ID, "number": "DM00000101"}, "appliedCreditMemos": [{"id": _CM_ID, "appliedAmount": 20.0}], "appliedPayments": [], "processedPayment": {"amount": 30.0, "status": "Processed", "gatewayResponse": f"card {sent['payment']['creditCard']['cardNumber']}"}}
                )
            if path.endswith("/write-off"):
                return vendor_json(200, {"success": True, "creditMemo": {"id": _CM_ID}})
            if path == "/v1/debit-memos" and req.method == "PUT":
                return vendor_json(200, {"success": True, "debitMemos": [{"id": _DM_ID, "success": True}]})
            return vendor_json(200, memo("debit", _DM_ID, "DM00000101", "Draft" if req.method == "POST" else "Posted", balance=50.0, dueDate="2026-12-07"))

        client = self.client(vendor)
        key = {"debit_memo_key": "DM00000101"}
        created = self.run_op("create_debit_memo", client, {"body": {"accountNumber": "A00000101", "charges": [{"productRatePlanChargeId": "8a90prpc1", "amount": 50.0}], "reasonCode": "Charge Dispute"}, "idempotency_key": "late-fee-1"})
        self.assertEqual(created["data"]["status"], "Draft")
        self.run_op("post_debit_memo", client, key)
        collected = self.run_op("collect_debit_memo", client, {**key, "body": {"applyCredit": True, "applicationOrder": ["CreditMemo"], "collect": True, "payment": {"creditCard": {"cardNumber": card}}}})
        self.assertNotIn(card, json.dumps(collected))
        self.assertEqual(collected["data"]["processedPayment"]["amount"], 30.0)
        self.run_op("write_off_debit_memo", client, {**key, "body": {"amount": 20.0, "memoDate": "2026-11-07"}})
        self.run_op("update_debit_memo_due_dates", client, {"body": {"debitMemos": [{"id": _DM_ID, "dueDate": "2027-01-07"}]}})
        self.assertEqual(
            self.seen(),
            [
                ("POST", "/v1/debit-memos"),
                ("PUT", "/v1/debit-memos/DM00000101/post"),
                ("POST", "/v1/debit-memos/DM00000101/collect"),
                ("PUT", "/v1/debit-memos/DM00000101/write-off"),
                ("PUT", "/v1/debit-memos"),
            ],
        )
        self.assertEqual(self.requests[0].headers["Idempotency-Key"], "late-fee-1")
        self.refuse("create_debit_memo_from_invoice", {"invoice_key": "INV00000201", "body": {"items": [{"amount": 5.0}]}})
        self.refuse("create_debit_memos", {"body": {}})

    def test_list_items_unpost_cancel_and_delete(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/items"):
                self.assertEqual((req.url.params["skuName"], req.url.params["beAppliedAmount"]), ("Late fee", "0.0"))
                return vendor_json(200, {"success": True, "debitMemoItems": [{"id": "8a90dmi1", "amount": 50.0, "balance": 50.0, "skuName": "Late fee"}]})
            if req.method == "DELETE":
                return vendor_json(200, {"success": True})
            status = {"unpost": "Draft", "cancel": "Cancelled"}.get(path.rsplit("/", 1)[-1], "Posted")
            return vendor_json(200, memo("debit", _DM_ID, "DM00000101", status))

        client = self.client(vendor)
        key = {"debit_memo_key": "DM00000101"}
        items = self.run_op("list_debit_memo_items", client, {**key, "sku_name": "Late fee", "be_applied_amount": 0.0, "page_size": 20})
        self.assertIsNone(items["pagination"]["next_page"])
        self.assertEqual(self.run_op("unpost_debit_memo", client, key)["data"]["status"], "Draft")
        self.assertEqual(self.run_op("cancel_debit_memo", client, key)["data"]["status"], "Cancelled")
        self.run_op("delete_debit_memo", client, key)
        self.assertEqual(self.seen()[-1], ("DELETE", "/v1/debit-memos/DM00000101"))
        self.denied("write_off_debit_memo", key, "Enhanced write-off permission is not enabled.")


class UploadTests(TenantCase):
    def test_invoice_and_memo_pdf_uploads_are_multipart_with_a_fresh_boundary(self) -> None:
        source = self.root / "INV00000201-legal.pdf"
        source.write_bytes(_PDF)
        bodies: list[tuple[str, bytes]] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            bodies.append((req.headers["Content-Type"], req.content))
            return vendor_json(200, {"success": True, "fileId": "8a90file9"})

        client = self.client(vendor)
        for name, key in (("upload_invoice_file", {"invoice_key": "INV00000201"}), ("upload_credit_memo_file", {"credit_memo_key": "CM00000101"}), ("upload_debit_memo_file", {"debit_memo_key": "DM00000101"})):
            with self.subTest(verb=name):
                self.assertEqual(self.run_op(name, client, {**key, "input_path": str(source), "idempotency_key": f"{name}-1"})["data"]["fileId"], "8a90file9")
        self.assertEqual([path for _, path in self.seen()], ["/v1/invoices/INV00000201/files", "/v1/credit-memos/CM00000101/files", "/v1/debit-memos/DM00000101/files"])
        boundaries: set[str] = set()
        for content_type, content in bodies:
            self.assertTrue(content_type.startswith("multipart/form-data; boundary=zuora-plugin-"))
            boundary = content_type.split("boundary=", 1)[1]
            boundaries.add(boundary)
            self.assertTrue(content.startswith(f"--{boundary}\r\n".encode()))
            self.assertIn(b'Content-Disposition: form-data; name="file"; filename="INV00000201-legal.pdf"', content)
            self.assertIn(b"Content-Type: application/pdf\r\n\r\n" + _PDF + f"\r\n--{boundary}--\r\n".encode(), content)
        self.assertEqual(len(boundaries), 3)

    def test_upload_refuses_outside_roots_wrong_suffix_and_empty_file_before_network(self) -> None:
        outside = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        outside.write(_PDF)
        outside.close()
        self.addCleanup(Path(outside.name).unlink)
        (self.root / "invoice.docx").write_bytes(b"PK\x03\x04")
        (self.root / "empty.pdf").write_bytes(b"")
        client = self.client(lambda _: self.fail("no vendor request expected"))
        for path in (outside.name, str(self.root / "invoice.docx"), str(self.root / "empty.pdf"), str(self.root / "missing.pdf")):
            with self.subTest(path=path), self.assertRaises((ValueError, ExportPathRefusedError)):
                self.run_op("upload_invoice_file", client, {"invoice_key": "INV00000201", "input_path": path})
        self.refuse("upload_invoice_file", {"invoice_key": "INV00000201", "input_path": str(self.root / "invoice.docx")})
        self.refuse("import_usage", {"input_path": str(self.root / "usage.json")})
        self.assertEqual((self.token_mints, self.requests), (0, []))

    def test_usage_import_and_read_send_the_wsdl_version_only_when_given(self) -> None:
        source = self.root / "usage-dynamic.csv"
        source.write_bytes(b"ACCOUNT_ID,UOM,QTY,STARTDATE,ENDDATE,SUBSCRIPTION_ID,CHARGE_ID,DESCRIPTION,PRODUCT_RATE_PLAN_CHARGE_ID\nA00000101,Each,5,11/02/2026,11/02/2026,A-S00000101,C-00000007,Dynamic,PRPC-00000009\n")

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                return vendor_json(200, {"success": True, "size": 160, "checkImportStatus": "/v1/usage/2c92c0f9import2/status"})
            body: dict[str, Any] = {"Id": _USAGE_ID, "Quantity": 5, "UOM": "Each", "RbeStatus": "Pending"}
            if req.headers.get("X-Zuora-WSDL-Version") == "146":
                body["ProductRatePlanChargeNumber"] = "PRPC-00000009"
            return vendor_json(200, body)

        client = self.client(vendor)
        self.run_op("import_usage", client, {"input_path": str(source), "x_zuora_wsdl_version": "146"})
        dynamic = self.run_op("get_usage", client, {"usage_id": _USAGE_ID, "x_zuora_wsdl_version": "146"})
        plain = self.run_op("get_usage", client, {"usage_id": _USAGE_ID})
        self.run_op("import_usage", client, {"input_path": str(source)})
        self.assertEqual([req.headers.get("X-Zuora-WSDL-Version") for req in self.requests], ["146", "146", None, None])
        self.assertEqual(dynamic["data"]["ProductRatePlanChargeNumber"], "PRPC-00000009")
        self.assertNotIn("ProductRatePlanChargeNumber", plain["data"])
        self.refuse("get_usage", {"usage_id": _USAGE_ID, "x_zuora_wsdl_version": "  "})

    def test_usage_import_sends_the_csv_and_reports_the_status_link(self) -> None:
        source = self.root / "usage-2026-11.csv"
        source.write_bytes(b"ACCOUNT_ID,UOM,QTY,STARTDATE,ENDDATE,SUBSCRIPTION_ID,CHARGE_ID,DESCRIPTION\nA00000101,Each,12,11/01/2026,11/01/2026,A-S00000101,C-00000007,API calls\n")

        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.method, req.url.path), ("POST", "/v1/usage"))
            self.assertIn(b"Content-Type: text/csv", req.content)
            return vendor_json(200, {"success": True, "size": len(source.read_bytes()), "checkImportStatus": "/v1/usage/2c92c0f9import1/status"})

        imported = self.run_op("import_usage", self.client(vendor), {"input_path": str(source), "idempotency_key": "usage-nov"})
        self.assertEqual(imported["data"]["checkImportStatus"], "/v1/usage/2c92c0f9import1/status")
        self.assertEqual(self.requests[0].headers["Idempotency-Key"], "usage-nov")


class BillRunUsageTests(TenantCase):
    def test_bill_run_create_get_post_cancel_delete_and_email(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/emails"):
                self.assertEqual(json.loads(req.content), {})
                return vendor_json(200, {"success": True})
            if req.method == "DELETE":
                return vendor_json(200, {"success": True})
            status = {"post": "PostInProgress", "cancel": "Canceled"}.get(path.rsplit("/", 1)[-1], "Completed")
            return vendor_json(200, {"success": True, "id": _BILL_RUN_ID, "billRunNumber": "BR-00000016", "status": status, "autoPost": False, "targetDate": "2026-12-01"})

        client = self.client(vendor)
        key = {"bill_run_id": _BILL_RUN_ID}
        created = self.run_op("create_bill_run", client, {"body": {"billCycleDay": "AllBillCycleDays", "batches": ["AllBatches"], "targetDate": "2026-12-01", "invoiceDate": "2026-12-01"}, "idempotency_key": "br-dec"})
        self.assertEqual(created["data"]["billRunNumber"], "BR-00000016")
        self.assertEqual(self.run_op("get_bill_run", client, key)["data"]["status"], "Completed")
        self.assertEqual(self.run_op("post_bill_run", client, {**key, "body": {"invoiceDate": "2026-12-01"}})["data"]["status"], "PostInProgress")
        self.assertEqual(self.run_op("cancel_bill_run", client, {**key, "body": {"cancelOnce": True}})["data"]["status"], "Canceled")
        self.run_op("delete_bill_run", client, key)
        self.run_op("email_bill_run_documents", client, {"bill_run_key": "BR-00000016"})
        self.assertEqual(
            self.seen(),
            [
                ("POST", "/v1/bill-runs"),
                ("GET", f"/v1/bill-runs/{_BILL_RUN_ID}"),
                ("PUT", f"/v1/bill-runs/{_BILL_RUN_ID}/post"),
                ("PUT", f"/v1/bill-runs/{_BILL_RUN_ID}/cancel"),
                ("DELETE", f"/v1/bill-runs/{_BILL_RUN_ID}"),
                ("POST", "/v1/bill-runs/BR-00000016/emails"),
            ],
        )
        self.refuse("post_bill_run", {**key, "body": {}})

    def test_billing_preview_run_create_and_read_result(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                self.assertEqual(json.loads(req.content)["targetDate"], "2027-06-30")
                return vendor_json(200, {"success": True, "billingPreviewRunId": "2c92c0f8prev1"})
            return vendor_json(200, {"success": True, "runNumber": "BPR-00000003", "status": "Completed", "targetDate": "2027-06-30", "totalAccounts": 120, "succeededAccounts": 120, "resultFileUrl": "/v1/files/2c92c0f8prevfile"})

        client = self.client(vendor)
        run = self.run_op("create_billing_preview_run", client, {"body": {"targetDate": "2027-06-30", "batches": "Batch1", "assumeRenewal": "Autorenew"}, "idempotency_key": "preview-q2"})
        result = self.run_op("get_billing_preview_run", client, {"billing_preview_run_id": run["data"]["billingPreviewRunId"]})
        self.assertEqual((result["data"]["status"], result["data"]["resultFileUrl"]), ("Completed", "/v1/files/2c92c0f8prevfile"))
        self.assertEqual(self.seen(), [("POST", "/v1/billing-preview-runs"), ("GET", "/v1/billing-preview-runs/2c92c0f8prev1")])
        self.assertEqual(self.requests[0].headers["Idempotency-Key"], "preview-q2")
        self.refuse("create_billing_preview_run", {"body": {"batches": "Batch1"}})

    def test_usage_record_create_read_correct_delete_and_rate_detail(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            path = req.url.path
            if path.endswith("/usage-rate-detail"):
                return vendor_json(200, {"success": True, "data": {"invoiceItemId": "8a90item1", "quantity": 12, "uom": "Each", "amountWithoutTax": 24.0, "rateDetail": "12 Each x $2.00 = $24.00"}})
            if req.method == "POST":
                return vendor_json(200, {"Success": True, "Id": _USAGE_ID})
            if req.method == "GET":
                return vendor_json(200, {"Id": _USAGE_ID, "Quantity": 12, "UOM": "Each", "RbeStatus": "Pending"})
            return vendor_json(200, {"success": True, "id": _USAGE_ID})

        client = self.client(vendor)
        key = {"usage_id": _USAGE_ID}
        created = self.run_op("create_usage", client, {"body": {"AccountNumber": "A00000101", "Quantity": 12, "StartDateTime": "2026-11-01T00:00:00", "UOM": "Each", "SubscriptionNumber": "A-S00000101"}, "reject_unknown_fields": True, "x_zuora_wsdl_version": "119"})
        self.assertEqual(created["data"]["Id"], _USAGE_ID)
        self.assertEqual(self.run_op("get_usage", client, {**key, "fields": "Quantity,UOM,RbeStatus"})["data"]["RbeStatus"], "Pending")
        self.run_op("update_usage", client, {**key, "body": {"Quantity": 10}})
        self.run_op("delete_usage", client, key)
        detail = self.run_op("get_usage_rate_detail", client, {"invoice_item_id": "8a90item1"})
        self.assertEqual(detail["data"]["data"]["amountWithoutTax"], 24.0)
        self.assertEqual((self.requests[0].url.params["rejectUnknownFields"], self.requests[0].headers["X-Zuora-WSDL-Version"]), ("true", "119"))
        self.assertEqual(self.requests[1].url.params["fields"], "Quantity,UOM,RbeStatus")
        self.assertEqual(self.seen()[4], ("GET", "/v1/invoices/invoice-item/8a90item1/usage-rate-detail"))
        self.refuse("create_usage", {"body": {"Quantity": 12, "UOM": "Each"}})
        self.refuse("update_usage", {**key, "body": {"Quantity": 1}, "reject_unknown_fields": "yes"})

    def test_object_route_success_false_is_a_refusal(self) -> None:
        client = self.client(lambda _: vendor_json(200, {"Success": False, "Errors": [{"Code": "INVALID_VALUE", "Message": "The UOM is not configured."}]}))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("create_usage", client, {"body": {"AccountNumber": "A00000101", "Quantity": 1, "StartDateTime": "2026-11-01T00:00:00", "UOM": "Widget"}})
        self.assertEqual(raised.exception.code, "zuora.validation_failed")


class LegacyHeaderTests(TenantCase):
    def test_get_invoice_and_list_subscriptions_forward_the_v1_headers(self) -> None:
        out = self.root / "subs.tsv"

        def vendor(req: httpx.Request) -> httpx.Response:
            self.assertEqual((req.headers["Zuora-Version"], req.headers["Zuora-Entity-Ids"], req.headers["Zuora-Track-Id"]), ("329.0", "entity-1", "trace-7"))
            if req.url.path.startswith("/v1/invoices/"):
                return vendor_json(200, {"success": True, "id": _INVOICE_ID, "invoiceNumber": "INV00000201", "status": "Posted"})
            return vendor_json(200, {"success": True, "subscriptions": [{"id": "8a90sub1", "subscriptionNumber": "A-S00000101", "status": "Active"}]})

        client = self.client(vendor)
        headers = {"zuora_version": "329.0", "zuora_entity_ids": "entity-1", "zuora_track_id": "trace-7"}
        invoice = billing_actions.get_invoice(client, {"id": "INV00000201", **headers})
        self.assertEqual(invoice["invoice"]["status"], "Posted")
        billing_actions.list_subscriptions(client, {"account_id": "A00000101", "output_tsv_path": str(out), **headers}, self.gate)
        self.assertIn("A-S00000101", out.read_text(encoding="utf-8"))
        self.assertNotIn("Zuora-Org-Ids", self.requests[0].headers)

    def test_dispatch_refuses_a_bad_header_without_queueing(self) -> None:
        manager = MagicMock()
        self.plugin._async_job_manager = manager  # noqa: SLF001
        self.assertEqual(self.plugin.get_invoice({"id": "INV1", "zuora_track_id": "a:b"}, dict(_STATE))["error"]["code"], "zuora.invalid_params")
        self.assertEqual(self.plugin.list_subscriptions({"account_id": "A1", "output_tsv_path": "/tmp/x.tsv", "zuora_version": "é"}, dict(_STATE))["error"]["code"], "zuora.invalid_params")
        self.assertEqual(manager.create_job.call_count, 0)


class CredentialAndDispatchTests(TenantCase):
    def test_registered_credentials_redacted_raw_percent_and_base64(self) -> None:
        echoed = every_encoding(_CLIENT_ID, _SECRET, _TOKEN)
        for name, params in (
            ("get_credit_memo", {"credit_memo_key": "CM00000101"}),
            ("list_billing_documents", {"page_size": 5}),
            ("get_bill_run", {"bill_run_id": _BILL_RUN_ID}),
            ("get_usage", {"usage_id": _USAGE_ID}),
        ):
            with self.subTest(verb=name):
                ok = self.client(lambda _: vendor_json(200, {"success": True, "comment": " ".join(echoed), "documents": [{"documentNumber": " ".join(echoed)}]}))
                bad = self.client(lambda _: vendor_json(400, refusal(50000000, " ".join(echoed))))
                with self.assertLogs("zuora_plugin", level="INFO") as audit:
                    success = self.run_op(name, ok, params)
                    with self.assertRaises(ZuoraServiceError) as raised:
                        self.run_op(name, bad, params)
                rendered = json.dumps(success) + str(raised.exception) + " ".join(audit.output)
                for value in echoed:
                    self.assertNotIn(value, rendered)

    def test_expired_token_reminted_once_then_reported(self) -> None:
        statuses = iter((401, 200))
        recovering = self.client(lambda _: vendor_json(next(statuses), {"success": True, "creditmemos": []}))
        self.assertEqual(self.run_op("list_credit_memos", recovering, {})["status"], 200)
        self.assertEqual(self.token_mints, 2)
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("cancel_debit_memo", self.client(lambda _: vendor_json(401, {"message": "Authentication error"})), {"debit_memo_key": "DM1"})
        self.assertEqual(raised.exception.code, "zuora.auth_failed")

    def test_dispatch_queues_without_network_and_worker_completes(self) -> None:
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "memo-job", "status": "queued"}}
        params = {"credit_memo_key": "CM00000101"}
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
        self.plugin._async_job_manager = manager  # noqa: SLF001
        self.plugin._app_config_loader = MagicMock()  # noqa: SLF001
        self.plugin._client = self.client(lambda _: vendor_json(200, memo("credit", _CM_ID, "CM00000101")))  # noqa: SLF001
        with patch.object(async_jobs, "ensure_worker_started"):
            queued = self.plugin.get_credit_memo(params, dict(_STATE))
            self.assertEqual(queued["data"]["status"], "queued")
            self.assertEqual(self.requests, [])
            refused = self.plugin.refund_credit_memo({"credit_memo_key": "CM00000101", "body": {"type": "External"}}, dict(_STATE))
            self.assertEqual(refused["error"]["code"], "zuora.invalid_params")
            refused_upload = self.plugin.upload_invoice_file({"invoice_key": "INV1"}, dict(_STATE))
            self.assertEqual(refused_upload["error"]["code"], "zuora.invalid_params")
            self.assertEqual(manager.create_job.call_count, 1)
            async_jobs._process_job(self.plugin, manager, "memo-job", "get_credit_memo")  # noqa: SLF001
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "completed")
        self.assertEqual(completion["result"]["data"]["number"], "CM00000101")


if __name__ == "__main__":
    unittest.main()
