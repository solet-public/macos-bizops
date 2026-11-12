"""Hermetic generic-request contracts: no CLI executable or Salesforce org."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote, quote_plus

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin import async_jobs, completion_templates, request_actions  # noqa: E402
from salesforce_plugin.client import SalesforceCliExecutor  # noqa: E402
from salesforce_plugin.errors import SalesforceServiceError  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402


class AuthenticatedRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = MagicMock(spec=SalesforceCliExecutor)
        self.executor.run_rest.return_value = {"statusCode": 204, "headers": {}, "body": ""}

    def test_every_method_reaches_vendor_unchanged(self) -> None:
        for method in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
            with self.subTest(method=method):
                body = '{"operation":"hardDelete","Name":"quoted \\"value\\""}'
                result = request_actions.authenticated_request(
                    self.executor,
                    {"method": method, "path": "/services/data/v62.0/jobs/ingest", "body": body},
                )
                self.executor.run_rest.assert_called_with(
                    method,
                    "/services/data/v62.0/jobs/ingest",
                    body=body,
                    headers={},
                    structured_response=True,
                )
                self.assertEqual(result["status"], 204)

    def test_tooling_query_and_pagination_forwarded(self) -> None:
        next_url = "/services/data/v62.0/tooling/query/locator-2000"
        soql = " ".join(("SELECT", "Id", "FROM", "ApexClass"))
        self.executor.run_rest.return_value = {
            "statusCode": 200,
            "headers": {},
            "body": {"records": [], "nextRecordsUrl": next_url},
        }
        result = request_actions.authenticated_request(
            self.executor,
            {"method": "GET", "path": "/services/data/v62.0/tooling/query", "query": {"q": soql}},
        )
        self.executor.run_rest.assert_called_once_with(
            "GET",
            "/services/data/v62.0/tooling/query?q=SELECT+Id+FROM+ApexClass",
            body=None,
            headers={},
            structured_response=True,
        )
        self.assertEqual(result["nextRecordsUrl"], next_url)

    def test_soap_xml_post_forwarded_unchanged(self) -> None:
        body = '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body><retrieve/></soap:Body></soap:Envelope>'
        headers = {"Content-Type": "text/xml; charset=UTF-8", "SOAPAction": "retrieve"}
        self.executor.run_rest.return_value = {"statusCode": 200, "headers": {}, "body": "<result/>"}
        result = request_actions.authenticated_request(
            self.executor,
            {"method": "POST", "path": "/services/Soap/m/62.0", "headers": headers, "body": body},
        )
        self.executor.run_rest.assert_called_once_with(
            "POST",
            "/services/Soap/m/62.0",
            body=body,
            headers=headers,
            structured_response=True,
        )
        self.assertEqual(result["body"], "<result/>")

    def test_record_delete_forwarded_unchanged(self) -> None:
        path = "/services/data/v62.0/sobjects/Account/001000000000001AAA"
        request_actions.authenticated_request(self.executor, {"method": "DELETE", "path": path})
        self.executor.run_rest.assert_called_once_with("DELETE", path, body=None, headers={}, structured_response=True)

    def test_http_errors_rate_limit_and_stale_write_are_data(self) -> None:
        for status in (400, 401, 403, 404, 412, 429, 500, 503):
            with self.subTest(status=status):
                body = [{"errorCode": "VENDOR_ERROR", "message": "vendor rejection"}]
                self.executor.run_rest.return_value = {
                    "statusCode": status,
                    "headers": {"Retry-After": "17"},
                    "body": body,
                }
                result = request_actions.authenticated_request(
                    self.executor,
                    {"method": "PATCH", "path": "/services/apexrest/state", "headers": {"If-Match": "version-1"}},
                )
                self.assertEqual(result["status"], status)
                self.assertEqual(result["body"], body)
                self.assertEqual(result["headers"]["retry-after"], "17")

    def test_tenant_and_credential_integrity_before_io(self) -> None:
        for path in ("https://other.example/services/data", "//other.example/x", "\\other.example", "https://registered.example/x"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                request_actions.authenticated_request(self.executor, {"method": "POST", "path": path})
        for headers in ({"Authorization": "Bearer secret"}, {"aUtHoRiZaTiOn": "secret"}, {"Host": "other.example"}, {"X-Test": "ok\r\nHost: other"}, {"X-Test\r\nAuthorization": "secret"}):
            with self.subTest(headers=headers), self.assertRaises(ValueError):
                request_actions.authenticated_request(self.executor, {"method": "DELETE", "path": "/x", "headers": headers})
        self.executor.run_rest.assert_not_called()

    def test_audit_omits_secrets_and_result_is_returned_as_received(self) -> None:
        self.executor.run_rest.return_value = {
            "statusCode": 201,
            "headers": {"Set-Cookie": "credential-cookie", "Content-Type": "application/json"},
            "body": {"access_token": "vendor-token", "echo": "input-secret", "id": "new-id"},
        }
        with self.assertLogs(request_actions.logger, level="INFO") as audit:
            result = request_actions.authenticated_request(
                self.executor,
                {"method": "POST", "path": "/services/apexrest/create?password=query-secret", "query": {"token": "query-token"}, "headers": {"X-Api-Key": "header-secret"}, "body": '{"password":"input-secret"}'},
            )
        logged = str(audit.output)
        for secret in ("vendor-token", "input-secret", "query-secret", "query-token", "header-secret", "credential-cookie"):
            self.assertNotIn(secret, logged)
        self.assertEqual(result["body"], {"access_token": "vendor-token", "echo": "input-secret", "id": "new-id"})
        self.assertIn("duration_ms", audit.output[0])
        self.assertIn(result["correlation_id"], audit.output[0])
        self.assertIn("201", audit.output[0])

    def test_xml_body_as_received_and_transport_failure_audit(self) -> None:
        self.executor.run_rest.return_value = {
            "statusCode": 200,
            "headers": {},
            "body": "<sessionId>vendor-session</sessionId><echo>xml-secret</echo>",
        }
        result = request_actions.authenticated_request(
            self.executor,
            {"method": "POST", "path": "/services/Soap/u/62.0", "body": "<sessionId>xml-secret</sessionId>"},
        )
        self.assertEqual(result["body"], "<sessionId>vendor-session</sessionId><echo>xml-secret</echo>")
        self.executor.run_rest.side_effect = SalesforceServiceError("sf.auth_failed", "No HTTP response")
        with self.assertLogs(request_actions.logger, level="INFO") as audit, self.assertRaises(SalesforceServiceError):
            request_actions.authenticated_request(self.executor, {"method": "POST", "path": "/x?q=hidden"})
        self.assertNotIn("hidden", str(audit.output))
        self.assertIn("'status': None", audit.output[0])

    def test_promoted_transport_preserves_bytes_and_http_error(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        paths: list[Path] = []
        body = "<envelope>\n  Unicode: café\n</envelope>"

        def invoke(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.assertIn("--include", argv)
            self.assertNotIn("--json", argv)
            self.assertEqual(kwargs["errors"], "replace")
            self.assertEqual(kwargs["env"]["NO_COLOR"], "1")
            self.assertEqual(argv[argv.index("--method") + 1], "POST")
            self.assertEqual(argv[argv.index("--header") + 1], "Content-Type:text/xml")
            path = Path(argv[argv.index("--body") + 1][1:])
            paths.append(path)
            self.assertEqual(path.read_text(), body)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            return subprocess.CompletedProcess(argv, 1, 'HTTP/1.1 429\nretry-after: 2\ncontent-type: application/json\n[{"errorCode":"REQUEST_LIMIT_EXCEEDED"}]\n', "")

        with patch("salesforce_plugin.client.subprocess.run", side_effect=invoke):
            result = executor.run_rest("POST", "/services/Soap/u/62.0", body=body, headers={"Content-Type": "text/xml"}, structured_response=True)
        self.assertEqual(result["statusCode"], 429)
        self.assertTrue(paths)
        self.assertFalse(paths[0].exists())

    def test_installed_cli_include_transcripts(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        cases: list[tuple[int, str, Any, bool]] = [
            (200, 'content-type: application/json\n{\n  "records": [],\n  "done": true\n}\n', {"records": [], "done": True}, False),
            (204, "content-length: 0\n\n", "", True),
            (400, 'content-type: application/json\n[{"errorCode":"BAD_REQUEST"}]\n', [{"errorCode": "BAD_REQUEST"}], True),
            (500, 'content-type: application/json\n\n{"message":"unavailable"}\n', {"message": "unavailable"}, True),
            (200, "content-type: text/xml\n<soap:Envelope><result>ok</result></soap:Envelope>\n", "<soap:Envelope><result>ok</result></soap:Envelope>", False),
            (200, "content-type: text/plain\ncaf\ufffd\n", "caf\ufffd", False),
        ]
        for status, printed, expected, proven in cases:
            with self.subTest(status=status, printed=printed):
                transcript = f"HTTP/1.1 {status}\n" + printed
                with patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], int(status >= 400), transcript, "")) as run:
                    result = executor.run_rest("GET", "/services/data/v62.0/query", structured_response=True)
                self.assertIn("--include", run.call_args.args[0])
                self.assertNotIn("--json", run.call_args.args[0])
                self.assertEqual(result["statusCode"], status)
                self.assertEqual(result["body"], expected if proven else transcript)
                if not proven:
                    self.assertTrue(result["body_advisory"])
                    self.assertEqual(result["transcript"], transcript)
                if "\ufffd" in printed:
                    self.assertIn("encoding_advisory", result)

    def test_non_utf8_subprocess_decoding_preserves_legacy_strict_default(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        argv = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'HTTP/1.1 200\\ncontent-type: text/plain\\n\\ncaf\\xff\\n')"]
        with self.assertRaises(UnicodeDecodeError):
            executor._invoke(argv, None, "fake")  # noqa: SLF001
        completed = executor._invoke(argv, None, "fake", decode_errors="replace")  # noqa: SLF001
        self.assertIn("caf\ufffd", completed.stdout)

    def test_header_like_text_retains_original_transcript_and_advisory(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        transcript = "HTTP/1.1 200\ncontent-type: text/plain\nerror: body text\nnext line\n"
        with patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], 0, transcript, "")):
            result = executor.run_rest("GET", "/x", structured_response=True)
        self.assertIn("uncertain", result["body_advisory"])
        self.assertEqual(result["body"], transcript)
        self.assertEqual(result["transcript"], transcript)

    def test_result_as_received_and_audit_built_without_headers_body_or_query_values(self) -> None:
        secret = "demo + /secret? ÿÿÿ"
        self.executor.run_rest.return_value = {
            "statusCode": 200,
            "headers": {"Set-Cookie": ["session=cookie-secret/+; Secure"], "Authorization": "Bearer response-token/+"},
            "body": {"echo": secret},
        }
        with self.assertLogs(request_actions.logger, level="INFO") as audit:
            result = request_actions.authenticated_request(
                self.executor,
                {"method": "POST", "path": "/services/apexrest/echo", "query": {"key": secret}, "headers": {"X-Api-Key": secret}, "body": json.dumps({"secret": secret})},
            )
        self.assertEqual(result["body"], {"echo": secret})
        logged = str(audit.output)
        self.assertIn("/services/apexrest/echo", logged)
        for value in (secret, quote(secret, safe=""), quote_plus(secret, safe=""), "cookie-secret/+", "response-token/+"):
            self.assertNotIn(value, logged)

    def test_registration_and_deferred_completion(self) -> None:
        plugin = SalesforcePlugin()
        plugin._cli_executor = self.executor  # noqa: SLF001
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "job", "status": "queued"}}
        params = {"method": "DELETE", "path": "/services/data/v62.0/sobjects/Account/001x"}
        with patch.object(async_jobs, "_acquire_async_job_manager", return_value=manager), patch.object(async_jobs, "ensure_worker_started"):
            queued = plugin.authenticated_request(params, {"session_id": "session", "flow_id": "flow"})
        self.assertEqual(queued["data"]["status"], "queued")
        self.executor.run_rest.assert_not_called()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
        async_jobs._process_job(plugin, manager, "job", "authenticated_request")  # noqa: SLF001
        self.assertEqual(manager.update_job.call_args.args[1]["result"]["status"], 204)
        self.assertIn("authenticated_request", plugin.get_edge_process_definitions())
        guidance = " ".join(completion_templates._instructions("authenticated_request", "result"))  # noqa: SLF001
        self.assertIn("may have changed vendor state", guidance)
        kb = json.loads((REPO_ROOT / "plugins/salesforce_plugin/knowledge_base/processes/authenticated_request.json").read_text())
        self.assertEqual(kb["process_key"], "plugin::salesforce_plugin::authenticated_request")


class PrintedBoundaryTests(unittest.TestCase):
    def _assert_uncertain_body_preserved(self, body: str) -> None:
        self._assert_uncertain_transcript_preserved("HTTP/1.1 200\ncontent-type: text/plain\n" + body + "\n")

    def _assert_uncertain_transcript_preserved(self, transcript: str) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        with patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], 0, transcript, "")):
            result = executor.run_rest("GET", "/x", structured_response=True)
        self.assertEqual(result["body"], transcript)
        self.assertEqual(result["transcript"], transcript)
        self.assertTrue(result["body_advisory"])

    def test_header_like_vendor_detail_at_eof_preserves_full_output(self) -> None:
        self._assert_uncertain_transcript_preserved("HTTP/1.1 200\ncontent-type: text/plain\nserver: meaningful vendor detail\n")

    def test_status_line_at_eof_preserves_full_output(self) -> None:
        self._assert_uncertain_transcript_preserved("HTTP/1.1 200\n")

    def test_status_and_one_header_at_eof_preserve_full_output(self) -> None:
        self._assert_uncertain_transcript_preserved("HTTP/1.1 200\ncontent-type: text/plain\n")

    def test_header_like_prefix_before_internal_blank_line(self) -> None:
        self._assert_uncertain_body_preserved("error: meaningful vendor detail\n\nremaining message")

    def test_header_like_prefix_before_number(self) -> None:
        self._assert_uncertain_body_preserved("error: meaningful vendor detail\n42")

    def test_header_like_prefix_before_json_scalar(self) -> None:
        self._assert_uncertain_body_preserved('detail: meaningful vendor detail\n"scalar"')

    def test_proven_blank_separator_and_final_container_exception(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        cases = [
            ('HTTP/1.1 200\ncontent-type: application/json\n\n{\n"ok": true\n}\n', {"ok": True}),
            ('HTTP/1.1 200\ncontent-type: application/json\n{"ok":true}\n', {"ok": True}),
            ('HTTP/1.1 200\ncontent-type: application/json\n[{"ok":true}]\n', [{"ok": True}]),
        ]
        for transcript, expected in cases:
            with self.subTest(transcript=transcript), patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], 0, transcript, "")):
                result = executor.run_rest("GET", "/x", structured_response=True)
                self.assertEqual(result["body"], expected)
                self.assertNotIn("body_advisory", result)

    def test_non_http_printed_text_is_preserved_with_unknown_status(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        transcript = "unframed output\n\n42\n"
        with patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], 1, transcript, "")):
            result = executor.run_rest("GET", "/x", structured_response=True)
        self.assertIsNone(result["statusCode"])
        self.assertEqual(result["body"], transcript)
        self.assertEqual(result["transcript"], transcript)
        self.assertTrue(result["body_advisory"])

    def test_uncertain_full_transcript_is_returned_as_received(self) -> None:
        executor = SalesforceCliExecutor(MagicMock())
        executor._target_org = "bound-org"  # noqa: SLF001
        token = "vendor-token/+ ÿ"
        encoded = base64.b64encode(token.encode()).decode()
        printed_body = json.dumps({"access_token": token, "echo": encoded}, indent=2)
        transcript = "HTTP/1.1 200\ncontent-type: application/json\nset-cookie: session=cookie-secret; Secure\n" + printed_body + "\n"
        with patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], 0, transcript, "")):
            result = request_actions.authenticated_request(executor, {"method": "GET", "path": "/x"})
        self.assertTrue(result["body_advisory"])
        self.assertEqual(result["body"], transcript)
        self.assertEqual(result["transcript"], transcript)
        self.assertNotIn("_decoded_body", result)


if __name__ == "__main__":
    unittest.main()
