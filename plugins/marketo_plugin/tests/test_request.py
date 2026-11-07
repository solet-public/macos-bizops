"""Hermetic generic request contract: real client over fake HTTP, plus job dispatch."""

from __future__ import annotations

import base64
import json
import re
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock
from urllib.parse import quote, quote_plus

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from marketo_plugin.app_config import MarketoInstanceConfig  # noqa: E402
from marketo_plugin.http_client import MarketoClient  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402
from marketo_plugin.request import execute_request, prepare_request  # noqa: E402
from smoke_async_dispatch import _FakeAsyncJobManager  # noqa: E402


class RequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.seen: list[httpx.Request] = []
        self.response: Callable[[httpx.Request], httpx.Response] = lambda _: httpx.Response(200, json={"success": True, "result": []})
        self.mints = 0
        self.client = MarketoClient(
            MarketoInstanceConfig(base_url="https://tenant.mktorest.test", client_id="test-client", client_secret="secret+value"),
            timeout_seconds=5.0,
        )
        self.client._http.close()
        self.client._http = httpx.Client(base_url="https://tenant.mktorest.test", transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/identity/oauth/token" and request.url.params.get("grant_type"):
            self.mints += 1
            return httpx.Response(200, json={"access_token": f"bearer-{self.mints}", "expires_in": 3600})
        return self.response(request)

    def test_every_method_reaches_registered_vendor_unchanged(self) -> None:
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                result = execute_request(
                    self.client,
                    prepare_request(
                        {
                            "method": method,
                            "path": "/rest/v1/example.json",
                            "query": {"filter": "a b"},
                            "json_body": {"input": [{"id": 7}]},
                            "headers": {"X-Correlation-ID": "caller-id", "Authorization": "caller-not-used"},
                        }
                    ),
                )
                actual = self.seen[-1]
                self.assertEqual(actual.method, method)
                self.assertEqual(actual.url.host, "tenant.mktorest.test")
                self.assertEqual(actual.url.path, "/rest/v1/example.json")
                self.assertEqual(actual.url.params["filter"], "a b")
                self.assertEqual(json.loads(actual.content), {"input": [{"id": 7}]})
                self.assertEqual(actual.headers["Authorization"], "Bearer bearer-1")
                self.assertEqual(actual.headers["X-Correlation-ID"], "caller-id")
                self.assertEqual(result["status"], 200)
        self.assertEqual(self.mints, 1)

    def test_bulk_extract_post_and_lead_delete_are_forwarded_unchanged(self) -> None:
        for method, path, body in (
            ("POST", "/bulk/v1/activities/export/create.json", {"format": "CSV", "filter": {"activityTypeIds": [1]}}),
            ("DELETE", "/rest/v1/leads/delete.json", {"input": [{"id": 7}]}),
        ):
            with self.subTest(method=method, path=path):
                execute_request(self.client, prepare_request({"method": method, "path": path, "json_body": body}))
                actual = self.seen[-1]
                self.assertEqual((actual.method, actual.url.path, json.loads(actual.content)), (method, path, body))

    def test_asset_form_encoding(self) -> None:
        self.client.request("POST", "/rest/asset/v1/program/7.json", form_body={"name": "A & B", "description": ""})
        actual = self.seen[-1]
        self.assertEqual(actual.headers["Content-Type"], "application/x-www-form-urlencoded")
        self.assertEqual(actual.content, b"name=A+%26+B&description=")

    def test_vendor_errors_http_failures_pagination_and_rate_limit_are_data(self) -> None:
        body = {"success": False, "errors": [{"code": "606", "message": "quota"}], "nextPageToken": "page-2", "moreResult": True}
        for status in (200, 400, 403, 429, 500, 503):
            with self.subTest(status=status):
                self.response = lambda _, status=status: httpx.Response(status, json=body, headers={"Retry-After": "17"})
                before = len(self.seen)
                result = self.client.request("DELETE", "/rest/v1/leads.json")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["body"], body)
                self.assertEqual(result["errors"], body["errors"])
                self.assertIs(result["success"], False)
                self.assertEqual(result["nextPageToken"], "page-2")
                self.assertEqual(result["retry_after"], "17")
                self.assertEqual(len([r for r in self.seen[before:] if r.url.path != "/identity/oauth/token"]), 1)

    def test_non_json_body_and_redirect_not_followed(self) -> None:
        for status in (200, 302, 500):
            self.response = lambda _, status=status: httpx.Response(status, text="plain vendor body", headers={"Location": "https://foreign.test/"})
            result = self.client.request("GET", "/bulk/v1/example/file.json")
            self.assertEqual((result["status"], result["body"]), (status, "plain vendor body"))
        self.assertTrue(all(r.url.host == "tenant.mktorest.test" for r in self.seen))

    def test_foreign_targets_and_crlf_headers_never_receive_credentials(self) -> None:
        for path in ("https://foreign.test/x", "//foreign.test/x", "///foreign.test/x", "https://tenant.mktorest.test/x", "\\\\foreign.test/x", "\n//foreign.test/x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.client.request("POST", path)
        for headers in ({"Host": "foreign.test"}, {"X-Test": "a\r\nb"}, {"X\nTest": "value"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                self.client.request("POST", "/rest/v1/example.json", headers=headers)
        self.assertEqual(self.seen, [])

    def test_matching_host_header_is_allowed(self) -> None:
        self.client.request("GET", "/rest/v1/example.json", headers={"Host": "tenant.mktorest.test"})
        self.assertEqual(self.seen[-1].headers["Host"], "tenant.mktorest.test")

    def test_audit_redacts_query_values_and_never_logs_bodies_or_credentials(self) -> None:
        self.response = lambda _: httpx.Response(200, json={"success": True, "access_token": "fresh-identity-token", "echo": "secret+value bearer-1"})
        with self.assertLogs(level="INFO") as captured:
            result = self.client.request("POST", "/identity/oauth/token?private=hidden-path", query={"other": "hidden-query"}, json_body={"private": "hidden-body"})
        logs = "\n".join(captured.output)
        for secret in ("secret+value", "secret%2Bvalue", "bearer-1", "hidden-path", "hidden-query", "hidden-body"):
            self.assertNotIn(secret, logs)
        for field in ("method=POST", "path=", "status=200", "duration_ms=", "correlation_id="):
            self.assertIn(field, logs)
        self.assertEqual(result["body"]["access_token"], "[REDACTED]")
        self.assertNotIn("secret+value", json.dumps(result))
        self.assertNotIn("bearer-1", json.dumps(result))
        self.assertNotIn("fresh-identity-token", json.dumps(result))

    def test_registered_secret_encodings_are_redacted_from_audit_and_echoes(self) -> None:
        secret = "tenant secret+/é=???>>>!"
        self.client.close()
        self.client = MarketoClient(
            MarketoInstanceConfig(base_url="https://tenant.mktorest.test", client_id="test-client", client_secret=secret),
            timeout_seconds=5.0,
        )
        self.client._http.close()
        self.client._http = httpx.Client(base_url="https://tenant.mktorest.test", transport=httpx.MockTransport(self.handle))
        self.addCleanup(self.client.close)
        encodings = self._assert_encoded_echoes(secret)
        self.assertNotEqual(encodings["base64"], encodings["base64url"])
        self.assertTrue(encodings["base64"].endswith("="))

    def test_bearer_encodings_are_redacted_from_audit_and_echoes(self) -> None:
        self.client.ensure_authenticated()
        self._assert_encoded_echoes("bearer-1")

    def _assert_encoded_echoes(self, secret: str) -> dict[str, str]:
        encoded_bytes = secret.encode()
        percent = quote(secret, safe="")
        encodings = {
            "raw": secret,
            "percent_upper": percent,
            "percent_lower": re.sub(r"%[0-9A-F]{2}", lambda match: match.group().lower(), percent),
            "percent_mixed": percent.replace("%2B", "%2b"),
            "form_plus": quote_plus(secret),
            "url_path": quote(secret),
            "all_bytes_percent": "".join(f"%{value:02x}" for value in encoded_bytes),
            "base64": base64.b64encode(encoded_bytes).decode(),
            "base64_unpadded": base64.b64encode(encoded_bytes).decode().rstrip("="),
            "base64url": base64.urlsafe_b64encode(encoded_bytes).decode(),
            "base64url_unpadded": base64.urlsafe_b64encode(encoded_bytes).decode().rstrip("="),
            "double_percent": quote(percent, safe=""),
            "form_plus_escaped": quote(quote_plus(secret), safe=""),
            "base64_url_escaped": quote(base64.b64encode(encoded_bytes).decode(), safe=""),
        }
        for name, encoded in encodings.items():
            with self.subTest(encoding=name):
                path_value = quote(encoded, safe="") if name == "raw" else encoded
                header_value = encoded if encoded.isascii() else quote(encoded, safe="")
                self.response = lambda r, encoded=encoded, header_value=header_value: httpx.Response(
                    200,
                    json={
                        "success": True,
                        "path": r.url.raw_path.decode("ascii"),
                        "query": r.url.params["echo"],
                        "header": r.headers["X-Echo"],
                        "json": json.loads(r.content),
                        "map": {encoded: encoded},
                    },
                    headers={"Retry-After": header_value},
                )
                # Raw Unicode cannot be a header value. Its JSON/path/query echoes still exercise it.
                with self.assertLogs(level="INFO") as captured:
                    result = self.client.request(
                        "POST",
                        f"/rest/v1/echo/{path_value}/end",
                        query={"echo": encoded},
                        headers={"X-Echo": header_value},
                        json_body={"echo": encoded},
                    )
                self.assertEqual(result["body"]["query"], "[REDACTED]")
                self.assertEqual(result["body"]["header"], "[REDACTED]")
                self.assertEqual(result["body"]["json"]["echo"], "[REDACTED]")
                self.assertEqual(result["body"]["map"], {"[REDACTED]": "[REDACTED]"})
                self.assertEqual(result["retry_after"], "[REDACTED]")
                logs = "\n".join(captured.output)
                self.assertIn("path=/rest/v1/echo/[REDACTED]/end", logs)
                self.assertNotIn(encoded, logs)
                self.assertNotIn(encoded, json.dumps(result, ensure_ascii=False))
                self.response = lambda _, encoded=encoded: httpx.Response(200, text="before " + encoded + " after")
                text_result = self.client.request("GET", "/rest/v1/echo.json")
                self.assertEqual(text_result["body"], "before [REDACTED] after")
        return encodings

    def test_one_expired_token_retry_uses_existing_cache(self) -> None:
        self.response = lambda r: httpx.Response(200, json={"success": r.headers["Authorization"] == "Bearer bearer-2", "errors": [{"code": "602"}]})
        result = self.client.request("POST", "/rest/v1/example.json")
        self.assertTrue(result["success"])
        self.assertEqual(self.mints, 2)
        self.response = lambda _: httpx.Response(200, json={"success": False, "errors": [{"code": "602"}]})
        before = self.mints
        result = self.client.request("POST", "/rest/v1/example.json")
        self.assertFalse(result["success"])
        self.assertEqual(self.mints, before + 1)

    def test_dispatch_registration_worker_and_historical_readback(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions
        from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

        plugin = MarketoPlugin()
        actions = discover_actions(plugin)
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, actions)
        self.assertIn("request", plugin.get_edge_process_definitions())
        plugin.logger = MagicMock()
        manager = _FakeAsyncJobManager()
        plugin.orchestrator_ref = MagicMock(async_job_manager=manager)
        plugin._require_client = lambda: self.client  # type: ignore[method-assign]
        kb = json.loads((REPO_ROOT / "plugins/marketo_plugin/knowledge_base/processes/request.json").read_text())
        self.assertEqual(kb["process_key"], "plugin::marketo_plugin::request")
        queued = plugin.request({"method": "DELETE", "path": "/rest/v1/leads/delete.json", "json_body": {"input": [{"id": 7}]}}, {"session_id": "session", "flow_id": "flow"})
        self.assertEqual(self.seen, [])
        self.assertEqual(queued["data"]["status"], "queued")
        job_id = queued["data"]["job_id"]
        instructions = str(manager._jobs[job_id]["metadata"])
        self.assertIn("may have changed vendor state", instructions)
        self.assertIn("Job completion is not vendor success", instructions)
        plugin._process_pending_jobs()
        self.assertEqual(self.seen[-1].method, "DELETE")
        result = plugin.check_marketo_job_status({"job_id": job_id}, {})["data"]
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"]["status"], 200)
        self.assertEqual(plugin.check_marketo_job_status({"job_id": job_id}, {})["data"], result)


if __name__ == "__main__":
    unittest.main()
