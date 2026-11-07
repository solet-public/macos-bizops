"""Hermetic unittest coverage for typed Zuora SQL Data Query job and Files verbs.

Transcripts follow Zuora's v1 API reference (Data Queries and Files sections,
reference version 2026-11-06): ``{"data": {...}}`` job envelopes with
``queryStatus``/``dataFile``, the ``{code, message}`` Data Query error body,
the ``reasons`` Files error body, and binary file downloads. The pre-signed
``dataFile`` host is a second MockTransport that must never see the bearer.
Query strings are opaque fixture placeholders, never real statements.

Run: SOLET_NAME=<name> .venv/bin/python plugins/zuora_plugin/tests/smoke_query_jobs_files.py
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from zuora_plugin import data_query_actions, typed_verbs  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.constants import REQUEST_REDACTION  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.export_containment import ExportPathRefusedError  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402
from zuora_plugin.typed_operations import execute  # noqa: E402

_SECRET = "fixture-client-secret"
_TOKEN = "fixture-access-token"
_BASE = "https://tenant.example.test"
_RESULT_URL = "https://results.example-bucket.test/dq/2c92c0f8.csv?X-Amz-Signature=fixture-signature"
_QUERY = "data-query-fixture-statement-7"
_HEADERS = {"Zuora-Request-Id": "req-dq1", "Zuora-Track-Id": "track-1"}
_OPS = {op.name: op for op in data_query_actions.OPERATIONS}
_JOB_ID = "2c92c0f8-6f1e-4d37-a5ef-0c9d3c4e7a11"


def job(status: str, *, data_file: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": _JOB_ID,
        "query": _QUERY,
        "queryStatus": status,
        "sourceData": "LIVE",
        "useIndexJoin": False,
        "createdBy": "8a8a8a8a-0000-0000-0000-000000000001",
        "updatedOn": "2026-11-06T12:00:05.000Z",
        "remainingRetries": 3,
    }
    if data_file is not None:
        record.update({"dataFile": data_file, "outputRows": 2, "processingTime": 412})
    return {"data": record}


class TenantCase(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="zuora_query_smoke_")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        self.plugin = ZuoraPlugin()
        self.plugin.initialize({"export_allowed_roots": [str(self.root)]})
        self.requests: list[httpx.Request] = []
        self.presigned: list[httpx.Request] = []

    def client(self, handler: Callable[[httpx.Request], httpx.Response], presigned: Callable[[httpx.Request], httpx.Response] | None = None) -> ZuoraClient:
        client = ZuoraClient(ZuoraTenantConfig(base_url=_BASE, client_id="fixture-client", client_secret=_SECRET), timeout_seconds=2)
        client._http.close()  # noqa: SLF001

        def tenant(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/oauth/token":
                return httpx.Response(200, json={"access_token": _TOKEN, "token_type": "bearer", "expires_in": 3599})
            self.assertEqual(req.headers["Authorization"], f"Bearer {_TOKEN}")
            self.requests.append(req)
            return handler(req)

        def bucket(req: httpx.Request) -> httpx.Response:
            self.assertNotIn("Authorization", req.headers)
            self.presigned.append(req)
            if presigned is None:
                self.fail("no pre-signed download expected")
            return presigned(req)

        client._http = httpx.Client(base_url=_BASE, transport=httpx.MockTransport(tenant))  # noqa: SLF001
        client._presigned_http = httpx.Client(transport=httpx.MockTransport(bucket), follow_redirects=False)  # noqa: SLF001
        self.addCleanup(client.close)
        return client

    def run_op(self, name: str, client: ZuoraClient, params: dict[str, Any]) -> dict[str, Any]:
        gate = self.plugin._export_path_gate  # noqa: SLF001
        if name == "download_data_query_result":
            return data_query_actions.download_data_query_result(client, params, gate)
        if name == "download_file":
            return data_query_actions.download_file(client, params, gate)
        return execute(_OPS[name], client, params, gate)

    def refuse(self, name: str, params: dict[str, Any]) -> None:
        before = len(self.requests)
        client = self.client(lambda _: self.fail("no vendor request expected"))
        with self.assertRaises(ValueError):
            typed_verbs.validate_typed(name, params)
        with self.assertRaises(ValueError):
            self.run_op(name, client, params)
        self.assertEqual(len(self.requests), before)


class DataQueryJobTests(TenantCase):
    def test_submit_sends_vendor_body_with_documented_defaults(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=job("accepted"), headers=_HEADERS)

        client = self.client(vendor)
        submitted = self.run_op("submit_data_query", client, {"query": _QUERY})
        self.assertEqual(json.loads(self.requests[0].content), {"query": _QUERY, "outputFormat": "CSV", "compression": "NONE", "output": {"target": "S3"}})
        self.assertEqual(submitted["data"]["data"]["queryStatus"], "accepted")
        self.run_op("submit_data_query", client, {"query": _QUERY, "output_format": "DSV", "column_separator": "\t", "compression": "GZIP", "read_deleted": True, "source_data": "WAREHOUSE", "warehouse_size": "xsmall"})
        self.assertEqual(
            json.loads(self.requests[1].content),
            {"query": _QUERY, "outputFormat": "DSV", "columnSeparator": "\t", "compression": "GZIP", "readDeleted": True, "sourceData": "WAREHOUSE", "warehouseSize": "xsmall", "output": {"target": "S3"}},
        )

    def test_submit_validation_mirrors_vendor_rules(self) -> None:
        self.refuse("submit_data_query", {})
        self.refuse("submit_data_query", {"query": _QUERY, "output_format": "XLSX"})
        self.refuse("submit_data_query", {"query": _QUERY, "output_format": "DSV"})
        self.refuse("submit_data_query", {"query": _QUERY, "column_separator": ","})
        self.refuse("submit_data_query", {"query": _QUERY, "output_format": "DSV", "column_separator": "||"})
        self.refuse("submit_data_query", {"query": _QUERY, "warehouse_size": "xsmall"})
        self.refuse("submit_data_query", {"query": _QUERY, "compression": "BZIP2"})

    def test_vendor_query_error_body_is_mapped(self) -> None:
        client = self.client(lambda _: httpx.Response(400, json={"code": 40001, "message": "Unknown column in field list"}, headers=_HEADERS))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("submit_data_query", client, {"query": _QUERY})
        self.assertEqual(raised.exception.code, "zuora.query_failed")
        self.assertIn("Unknown column", str(raised.exception))

    def test_list_get_and_cancel_lifecycle(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/query/jobs":
                self.assertEqual(dict(req.url.params), {"queryStatus": "in_progress", "pageSize": "20"})
                return httpx.Response(200, json={"data": [job("in_progress")["data"]]}, headers=_HEADERS)
            if req.method == "DELETE":
                return httpx.Response(200, json=job("cancelled"), headers=_HEADERS)
            return httpx.Response(200, json=job("in_progress"), headers=_HEADERS)

        client = self.client(vendor)
        listed = self.run_op("list_data_query_jobs", client, {"query_status": "in_progress", "page_size": 20})
        self.assertEqual(listed["data"]["data"][0]["id"], _JOB_ID)
        self.assertEqual(self.run_op("get_data_query_job", client, {"job_id": _JOB_ID})["data"]["data"]["queryStatus"], "in_progress")
        cancelled = self.run_op("cancel_data_query_job", client, {"job_id": _JOB_ID})
        self.assertEqual((self.requests[-1].method, self.requests[-1].url.path), ("DELETE", f"/query/jobs/{_JOB_ID}"))
        self.assertEqual(cancelled["data"]["data"]["queryStatus"], "cancelled")
        self.refuse("list_data_query_jobs", {"query_status": "running"})

    def test_cancel_of_finished_job_is_vendor_refusal_and_403_is_permission(self) -> None:
        client = self.client(lambda _: httpx.Response(400, json={"code": 40003, "message": "Query job already completed and cannot be cancelled"}))
        with self.assertRaises(ZuoraServiceError) as finished:
            self.run_op("cancel_data_query_job", client, {"job_id": _JOB_ID})
        self.assertIn("cannot be cancelled", str(finished.exception))
        denied = self.client(lambda _: httpx.Response(403, json={"code": 40300, "message": "Data Query permission is not enabled for this role"}))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("get_data_query_job", denied, {"job_id": _JOB_ID})
        self.assertEqual(raised.exception.code, "zuora.permission_denied")
        self.assertIn("Data Query permission", str(raised.exception))


class ResultDownloadTests(TenantCase):
    def test_pending_job_reports_status_without_downloading(self) -> None:
        client = self.client(lambda _: httpx.Response(200, json=job("in_progress")))
        result = self.run_op("download_data_query_result", client, {"job_id": _JOB_ID, "output_path": str(self.root / "result.csv")})
        self.assertEqual((result["downloaded"], result["query_status"]), (False, "in_progress"))
        self.assertEqual(self.presigned, [])
        self.assertFalse((self.root / "result.csv").exists())

    def test_completed_job_downloads_vendor_url_without_bearer_and_scrubs_credentials(self) -> None:
        content = f"Id,Name\n8a90e5e4,Acme\n8a90e5e5,{_TOKEN} {base64.b64encode(_SECRET.encode()).decode()} {quote(_TOKEN)}\n".encode()

        def bucket(req: httpx.Request) -> httpx.Response:
            self.assertEqual(str(req.url), _RESULT_URL)
            return httpx.Response(200, content=content, headers={"Content-Type": "text/csv"})

        client = self.client(lambda _: httpx.Response(200, json=job("completed", data_file=_RESULT_URL)), bucket)
        out = self.root / "result.csv"
        result = self.run_op("download_data_query_result", client, {"job_id": _JOB_ID, "output_path": str(out)})
        written = out.read_bytes()
        self.assertTrue(result["downloaded"])
        self.assertEqual(result["output_rows"], 2)
        self.assertEqual(result["file"]["sha256"], hashlib.sha256(written).hexdigest())
        self.assertTrue(written.startswith(b"Id,Name\n8a90e5e4,Acme\n"))
        for leaked in (_TOKEN, base64.b64encode(_SECRET.encode()).decode(), quote(_TOKEN)):
            self.assertNotIn(leaked.encode(), written)
        self.assertIn(REQUEST_REDACTION.encode(), written)
        self.assertEqual(len(self.presigned), 1)

    def test_result_url_failures_are_reported_not_retried(self) -> None:
        expired = self.client(lambda _: httpx.Response(200, json=job("completed", data_file=_RESULT_URL)), lambda _: httpx.Response(403, content=b"<Error><Code>AccessDenied</Code></Error>"))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("download_data_query_result", expired, {"job_id": _JOB_ID, "output_path": str(self.root / "r.csv")})
        self.assertEqual(raised.exception.code, "zuora.api_error")
        self.assertEqual(len(self.presigned), 1)
        plain = self.client(lambda _: httpx.Response(200, json=job("completed", data_file="http://results.example-bucket.test/r.csv")))
        with self.assertRaises(ZuoraServiceError):
            self.run_op("download_data_query_result", plain, {"job_id": _JOB_ID, "output_path": str(self.root / "r.csv")})
        self.assertEqual(len(self.presigned), 1)

    def test_output_path_contained_before_any_vendor_call(self) -> None:
        client = self.client(lambda _: self.fail("no vendor request expected"))
        with self.assertRaises(ExportPathRefusedError):
            self.run_op("download_data_query_result", client, {"job_id": _JOB_ID, "output_path": "/elsewhere/result.csv"})
        with self.assertRaises(ExportPathRefusedError):
            self.run_op("download_file", client, {"file_id": "2c92c08a", "output_path": "relative.pdf"})
        self.refuse("download_file", {"file_id": "2c92c08a"})
        self.assertEqual(self.requests, [])


class FileTests(TenantCase):
    def test_download_status_and_restore(self) -> None:
        pdf = b"%PDF-1.7\n\x00\xff binary invoice body\n%%EOF\n"

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/status"):
                return httpx.Response(200, json={"success": True, "accessible": False, "status": "ARCHIVED"}, headers=_HEADERS)
            if req.url.path.endswith("/restore"):
                self.assertEqual(req.method, "PUT")
                return httpx.Response(200, json={"success": True, "fileId": "2c92c08a", "status": "RESTORED"}, headers=_HEADERS)
            return httpx.Response(200, content=pdf, headers={"Content-Type": "application/pdf", "Content-Disposition": 'attachment; filename="INV0001.pdf"'})

        client = self.client(vendor)
        self.assertEqual(self.run_op("get_file_status", client, {"file_id": "2c92c08a"})["data"]["status"], "ARCHIVED")
        self.assertEqual(self.run_op("restore_file", client, {"file_id": "2c92c08a"})["data"]["status"], "RESTORED")
        out = self.root / "INV0001.pdf"
        downloaded = self.run_op("download_file", client, {"file_id": "2c92c08a", "output_path": str(out)})
        self.assertEqual(out.read_bytes(), pdf)
        self.assertEqual(downloaded["content_type"], "application/pdf")
        self.assertEqual(downloaded["file"]["bytes"], len(pdf))

    def test_missing_file_reasons_are_mapped(self) -> None:
        client = self.client(lambda _: httpx.Response(404, json={"success": False, "processId": "p-1", "requestId": "r-1", "reasons": [{"code": "50000040", "message": "File not found"}]}))
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("download_file", client, {"file_id": "2c92c08a", "output_path": str(self.root / "missing.pdf")})
        self.assertEqual(raised.exception.code, "zuora.object_not_found")
        self.assertIn("File not found", str(raised.exception))
        self.assertFalse((self.root / "missing.pdf").exists())
        self.refuse("get_file_status", {"file_id": "../v1/files"})


if __name__ == "__main__":
    unittest.main()
