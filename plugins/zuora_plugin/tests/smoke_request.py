"""Hermetic unittest coverage for generic tenant-authenticated Zuora requests.

Run through unittest or directly; httpx.MockTransport never contacts a vendor.
Workflow paths verified against Zuora's List workflows / Export a workflow
version API references, 2026-11-06.
"""

from __future__ import annotations

import base64
import json
import re
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, quote_plus

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from zuora_plugin import async_jobs, request_actions  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.constants import REQUEST_REDACTION  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402

_SECRET = "fixture-client-secret"
_TOKEN = "fixture-access-token"
_BASE = "https://tenant.example.test"
_STATE = {"session_id": "request-session", "flow_id": "request-flow"}


class _RequestClientCase(unittest.TestCase):
    def _client(
        self,
        handler: Callable[[httpx.Request], httpx.Response],
        *,
        client_id: str = "fixture-client",
        client_secret: str = _SECRET,
        token: str = _TOKEN,
    ) -> ZuoraClient:
        client = ZuoraClient(ZuoraTenantConfig(base_url=_BASE, client_id=client_id, client_secret=client_secret), timeout_seconds=2)
        client._http.close()  # noqa: SLF001

        def transport(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/oauth/token":
                self.assertEqual(parse_qs(req.content.decode())["client_secret"], [client_secret])
                return httpx.Response(200, json={"access_token": token, "expires_in": 3600})
            self.assertEqual(req.headers["Authorization"], f"Bearer {token}")
            self.assertEqual(req.url.host, "tenant.example.test")
            return handler(req)

        client._http = httpx.Client(base_url=_BASE, transport=httpx.MockTransport(transport))  # noqa: SLF001
        self.addCleanup(client.close)
        return client


class CredentialEncodingTests(_RequestClientCase):
    def _assert_encoded_redaction(self, encode: Callable[[str], str]) -> None:
        credentials = ("registered client /+?=???~~~", "registered secret /+?=???~~~é", "minted token /+?=???~~~")
        encoded = [encode(value) for value in credentials]
        payload = "|".join(encoded)
        for kind in ("json", "text", "binary"):
            with self.subTest(kind=kind):

                def vendor(_: httpx.Request, kind: str = kind) -> httpx.Response:
                    headers = {"Zuora-Track-Id": payload}
                    if kind == "json":
                        return httpx.Response(200, headers=headers, json={"echo": payload, "nested": [{value: value} for value in encoded], "nextPage": f"/workflows/{payload}"})
                    headers["Content-Type"] = "text/plain" if kind == "text" else "application/octet-stream"
                    return httpx.Response(200, headers=headers, content=b"prefix\x00" + payload.encode() + b"\x00suffix")

                client = self._client(vendor, client_id=credentials[0], client_secret=credentials[1], token=credentials[2])
                with self.assertLogs("zuora_plugin", level="INFO") as audit:
                    result = request_actions.request(client, {"method": "GET", "path": f"/workflows/{payload}", "query": {"echo": payload}, "headers": {"X-Trace": payload}})
                response = json.dumps(result)
                if kind == "binary":
                    decoded = base64.b64decode(result["body"], validate=True).decode()
                    self.assertIn(REQUEST_REDACTION, decoded)
                    response += decoded
                for value in encoded:
                    self.assertNotIn(value, " ".join(audit.output), "encoded credential survived in audit")
                    self.assertNotIn(value, response, "encoded credential survived in response")
                self.assertIn(REQUEST_REDACTION, result["headers"]["zuora-track-id"])

    def test_percent_upper_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: quote(value, safe=""))

    def test_percent_lower_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: re.sub(r"%[0-9A-F]{2}", lambda match: match[0].lower(), quote(value, safe="")))

    def test_percent_mixed_hex_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: re.sub(r"%([0-9A-F])([0-9A-F])", lambda match: "%" + match[1].lower() + match[2].upper(), quote(value, safe="")))

    def test_percent_all_bytes_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: "".join(f"%{byte:02X}" for byte in value.encode()))

    def test_form_plus_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: quote_plus(value, safe=""))

    def test_base64_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: base64.b64encode(value.encode()).decode())

    def test_base64url_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: base64.urlsafe_b64encode(value.encode()).decode())

    def test_unpadded_base64_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: base64.b64encode(value.encode()).decode().rstrip("="))

    def test_unpadded_base64url_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: base64.urlsafe_b64encode(value.encode()).decode().rstrip("="))

    def test_percent_encoded_base64_credentials_in_audit_and_response(self) -> None:
        self._assert_encoded_redaction(lambda value: quote(base64.b64encode(value.encode()).decode(), safe=""))

    def test_encoded_cached_token_audit_on_transport_error(self) -> None:
        def vendor(req: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("fixture transport failure", request=req)

        client = self._client(vendor)
        client.ensure_authenticated()
        encoded = base64.urlsafe_b64encode(_TOKEN.encode()).decode()
        with self.assertLogs("zuora_plugin", level="INFO") as audit, self.assertRaises(httpx.ConnectError):
            request_actions.request(client, {"method": "GET", "path": f"/workflows/{encoded}"})
        self.assertNotIn(encoded, " ".join(audit.output))


class RequestTests(_RequestClientCase):
    def test_methods_query_headers_and_json_forwarded_unchanged(self) -> None:
        seen: list[httpx.Request] = []

        def vendor(req: httpx.Request) -> httpx.Response:
            seen.append(req)
            return httpx.Response(200, json={"success": True})

        client = self._client(vendor)
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                body = {"Name": "corrected", "optional": None, "version": 7}
                result = request_actions.request(client, {"method": method, "path": "/objects/records/default/example", "query": {"page": 2}, "headers": {"Zuora-Version": "2025-08-12", "If-Match": '"v7"'}, "body": body})
                req = seen[-1]
                self.assertEqual(req.method, method)
                self.assertEqual(req.url.path, "/objects/records/default/example")
                self.assertEqual(req.url.params["page"], "2")
                self.assertEqual(req.headers["Zuora-Version"], "2025-08-12")
                self.assertEqual(req.headers["If-Match"], '"v7"')
                self.assertEqual(json.loads(req.content), body)
                self.assertEqual(result["status"], 200)
        self.assertEqual(len(seen), 6)

    def test_vendor_error_mapping_and_retry_after_are_data_without_replay(self) -> None:
        for status, code in ((400, "zuora.validation_failed"), (401, "zuora.auth_failed"), (403, "zuora.auth_failed"), (404, "zuora.object_not_found"), (412, "zuora.validation_failed"), (429, "zuora.rate_limited"), (500, "zuora.api_error")):
            with self.subTest(status=status):
                calls: list[str] = []

                def vendor(req: httpx.Request, calls: list[str] = calls, status: int = status) -> httpx.Response:
                    calls.append(req.method)
                    return httpx.Response(status, headers={"Retry-After": "17"}, json={"reasons": [{"message": "vendor detail"}]})

                result = request_actions.request(self._client(vendor), {"method": "DELETE", "path": "/workflows/versions/9"})
                self.assertEqual(calls, ["DELETE"])
                self.assertEqual(result["status"], status)
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(result["headers"]["retry-after"], "17")
                self.assertEqual(result["body"]["reasons"][0]["message"], "vendor detail")

    def test_tenant_and_header_protection_before_network(self) -> None:
        client = self._client(lambda _: self.fail("no vendor request expected"))
        for path in ("https://outside.test/steal", "//outside.test/steal", "\\\\outside.test/steal", "https:steal", "\n//outside.test", " https://outside.test"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                request_actions.request(client, {"method": "POST", "path": path})
        for headers in ({"Authorization": "caller-token"}, {"authorization": "caller-token"}, {"X-Header": "hello\r\nHost: outside.test"}, {"X\nHeader": "value"}, {"Host": "outside.test"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                request_actions.request(client, {"method": "POST", "path": "/workflows", "headers": headers})
        self.assertIsNone(client._token)  # noqa: SLF001 — no OAuth request either

    def test_secret_redaction_in_body_headers_pagination_and_audit(self) -> None:
        def vendor(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, headers={"X-Unselected": "ignored", "Set-Cookie": _TOKEN, "Zuora-Request-Id": "vendor-id", "Link": f'</workflows?token={_TOKEN}>; rel="next"'}, json={"access_token": "new-vendor-token", "echo": f"{_SECRET} {_TOKEN} caller-secret new-vendor-token", "nextPage": f"/workflows?token={_TOKEN}"}
            )

        with self.assertLogs("zuora_plugin", level="INFO") as audit:
            result = request_actions.request(self._client(vendor), {"method": "PATCH", "path": "/workflows?client_secret=caller-secret", "query": {"filter": "private-query"}, "body": {"client_secret": "caller-secret"}})
        rendered = json.dumps(result) + " ".join(audit.output)
        for secret in (_SECRET, _TOKEN, "caller-secret", "new-vendor-token", "private-query"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(result["body"]["access_token"], REQUEST_REDACTION)
        self.assertNotIn("set-cookie", result["headers"])
        self.assertNotIn("x-unselected", result["headers"])
        self.assertEqual(result["headers"]["zuora-request-id"], "vendor-id")
        for field in ("method=PATCH", "path=/workflows?", "status=200", "duration_ms=", "correlation_id=" + result["correlation_id"]):
            self.assertIn(field, audit.output[0])

    def test_workflow_backup_lists_and_downloads_export(self) -> None:
        calls: list[str] = []
        export = {"workflow": {"id": 42, "name": "Backup"}, "tasks": [], "linkages": []}

        def vendor(req: httpx.Request) -> httpx.Response:
            calls.append(req.url.path)
            if req.url.path == "/workflows":
                return httpx.Response(200, json={"data": [{"id": 42}], "pagination": {"page": 1, "total_pages": 2}}, headers={"Link": '</workflows?page=2>; rel="next"'})
            self.assertEqual(req.url.path, "/workflows/42/export")
            self.assertEqual(req.url.params["version"], "latest")
            return httpx.Response(200, json=export, headers={"Content-Disposition": 'attachment; filename="workflow.json"'})

        client = self._client(vendor)
        listing = request_actions.request(client, {"method": "GET", "path": "/workflows", "query": {"page": 1}})
        result = request_actions.request(client, {"method": "GET", "path": f"/workflows/{listing['body']['data'][0]['id']}/export", "query": {"version": "latest"}})
        self.assertEqual(result["body"], export)
        self.assertEqual(result["body_encoding"], "json")
        self.assertEqual(listing["pagination"]["pagination"], {"page": 1, "total_pages": 2})
        self.assertIn("link", listing["pagination"])
        self.assertEqual(calls, ["/workflows", "/workflows/42/export"])

    def test_text_binary_empty_and_redirect_responses(self) -> None:
        for status, content, content_type, encoding in ((200, b"name\nvalue\n", "text/csv", "text"), (200, b"\x00\xff\x01", "application/octet-stream", "base64"), (204, b"", "application/octet-stream", "base64"), (302, b"", "text/plain", "text")):
            with self.subTest(status=status, content_type=content_type):
                calls: list[str] = []

                def vendor(req: httpx.Request, calls: list[str] = calls, status: int = status, content: bytes = content, content_type: str = content_type) -> httpx.Response:
                    calls.append(req.url.path)
                    return httpx.Response(status, content=content, headers={"Content-Type": content_type, "Location": "https://outside.test/redirect"})

                result = request_actions.request(self._client(vendor), {"method": "GET", "path": "/v1/files/9"})
                self.assertEqual(result["body_encoding"], encoding)
                expected = base64.b64encode(content).decode() if encoding == "base64" else content.decode()
                self.assertEqual(result["body"], expected)
                self.assertEqual(calls, ["/v1/files/9"])

    def test_registration_and_deferred_worker_completes_vendor_response(self) -> None:
        plugin = ZuoraPlugin()
        plugin.initialize({})
        plugin._app_config_loader = MagicMock()  # noqa: SLF001
        plugin._client = self._client(lambda _: httpx.Response(412, json={"reason": "stale version"}))  # noqa: SLF001
        actions = discover_actions(plugin)
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", plugin, actions)
        self.assertIn("request", plugin.get_edge_process_definitions())
        self.assertIn("request", async_jobs.ACTION_HANDLERS)
        kb = json.loads((REPO_ROOT / "plugins/zuora_plugin/knowledge_base/processes/request.json").read_text())
        self.assertEqual(kb["process_key"], "plugin::zuora_plugin::request")
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "request-job", "status": "queued"}}
        params = {"method": "PATCH", "path": "/objects/records/default/example/9", "body": {"optional": None}}
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
        plugin._async_job_manager = manager  # noqa: SLF001
        with patch.object(async_jobs, "ensure_worker_started"), patch.object(plugin._client, "request", wraps=plugin._client.request) as call:  # noqa: SLF001
            queued = plugin.request(params, dict(_STATE))
            self.assertEqual(queued["data"]["status"], "queued")
            call.assert_not_called()
            async_jobs._process_job(plugin, manager, "request-job", "request")  # noqa: SLF001
            call.assert_called_once()
        completion = manager.update_job.call_args.args[1]
        self.assertEqual(completion["status"], "completed")
        self.assertEqual(completion["result"]["status"], 412)
        self.assertEqual(completion["result"]["error"]["code"], "zuora.validation_failed")
        self.assertEqual(plugin.request({"method": "GET", "path": "https://outside.test"}, dict(_STATE))["error"]["code"], "zuora.invalid_params")
        self.assertEqual(manager.create_job.call_count, 1)


if __name__ == "__main__":
    unittest.main()
