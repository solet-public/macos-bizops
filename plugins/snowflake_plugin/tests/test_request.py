"""Offline unittest coverage for Snowflake REST operations and credential custody."""

from __future__ import annotations

import base64
import json
import logging
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote_from_bytes, quote_plus

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/snowflake_plugin/src"))

from snowflake_plugin import async_jobs, rest_client  # noqa: E402
from snowflake_plugin.app_config import SnowflakeAccountConfig  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402


class RestFixture(unittest.TestCase):
    config: SnowflakeAccountConfig
    key: rsa.RSAPrivateKey
    pem: bytes

    @classmethod
    def setUpClass(cls) -> None:
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        der = cls.key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.config = SnowflakeAccountConfig("org-account", "service_user", "WH", "DB", "PUBLIC", "ROLE", "key_pair", der)

    def call(self, params: dict[str, Any], handler: Any) -> dict[str, Any]:
        return rest_client.perform_request(self.config, params, timeout_seconds=30, transport=httpx.MockTransport(handler))


class RequestOperations(RestFixture):
    def test_all_five_methods_and_sql_delete_reach_vendor_unchanged(self) -> None:
        seen: list[tuple[str, str, Any]] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            seen.append((request.method, request.url.path, json.loads(request.content)))
            self.assertEqual(request.url.host, "org-account.snowflakecomputing.com")
            self.assertEqual(request.url.params["async"], "true")
            self.assertEqual(request.headers["X-Caller"], "same")
            self.assertTrue(request.headers["Authorization"].startswith("Bearer "))
            return httpx.Response(202, json={"statementHandle": "handle-1", "nextPage": "opaque-next"})

        for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method):
                path = "/api/v2/statements" if method == "POST" else "/api/v2/databases/X"
                body = {"statement": "DELETE FROM X.PUBLIC.T WHERE ID = 7"}
                result = self.call({"method": method, "path": path, "query": {"async": "true"}, "headers": {"X-Caller": "same"}, "body": body}, vendor)
                self.assertEqual(seen[-1], (method, path, body))
                self.assertEqual(result["body"]["statementHandle"], "handle-1")
                self.assertEqual(result["body"]["nextPage"], "opaque-next")
        self.assertEqual(len(seen), 5, "one physical call each, no SQL-handle polling")

    def test_status_errors_retry_after_and_pagination_are_data(self) -> None:
        for status, code in ((400, "snowflake.api_error"), (401, "snowflake.auth_failed"), (403, "snowflake.permission_denied"), (429, "snowflake.rate_limited"), (503, "snowflake.api_error")):
            with self.subTest(status=status):
                body = {"message": "vendor detail", "next": "opaque-cursor"}
                result = self.call({"method": "GET", "path": "/api/v2/databases"}, lambda _, status=status, body=body: httpx.Response(status, json=body, headers={"Retry-After": "17", "Link": "</next>; rel=next"}))
                self.assertEqual(result["status"], status)
                self.assertEqual(result["error_code"], code)
                self.assertEqual(result["headers"]["retry-after"], "17")
                self.assertEqual(result["headers"]["link"], "</next>; rel=next")
                self.assertEqual(result["body"], body)

    def test_redirect_is_surfaced_without_sending_credential_to_new_host(self) -> None:
        seen: list[str] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            return httpx.Response(307, headers={"Location": "https://other.invalid/path"})

        result = self.call({"method": "DELETE", "path": "/v1/data/pipes/X"}, vendor)
        self.assertEqual(seen, ["org-account.snowflakecomputing.com"])
        self.assertEqual(result["status"], 307)
        self.assertEqual(result["headers"]["location"], "https://other.invalid/path")

    def test_relative_bases_and_query_merge(self) -> None:
        for path in ("api/v2/cortex/v1/complete?existing=yes", "/v1/data/pipes/P/insertFiles", "/v2/streaming/hostname"):
            with self.subTest(path=path):

                def vendor(request: httpx.Request, path: str = path) -> httpx.Response:
                    self.assertEqual(request.url.params["extra"], "value")
                    if "existing" in path:
                        self.assertEqual(request.url.params["existing"], "yes")
                    return httpx.Response(200, text="complete text\nincluding trailing data")

                result = self.call({"method": "POST", "path": path, "query": {"extra": "value"}}, vendor)
                self.assertEqual(result["body"], "complete text\nincluding trailing data")

    def test_ambiguous_text_and_binary_bodies_preserved(self) -> None:
        for raw in (b'{"ok": true}\ntrailing text', b'data: {"a":1}\n\ndata: done\n', b"\x00\xff\x80complete"):
            with self.subTest(raw=raw):
                result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _, raw=raw: httpx.Response(200, content=raw))
                restored = base64.b64decode(result["body"]) if result["body_encoding"] == "base64" else result["body"].encode()
                self.assertEqual(restored, raw)

    def test_duplicate_json_members_preserved_as_full_text_with_advisory(self) -> None:
        for raw in (
            b'{"message":"first vendor diagnostic","message":"second vendor diagnostic"}',
            b'{"outer":[{"message":"first nested diagnostic","message":"second nested diagnostic"}],"after":"retained"}',
        ):
            with self.subTest(raw=raw):
                result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _, raw=raw: httpx.Response(200, content=raw))
                self.assertEqual(result["body_encoding"], "text")
                self.assertEqual(result["body"], raw.decode("utf-8"))
                self.assertIn("duplicate JSON members", result["advisory"])
                self.assertIn("preserved as text", result["advisory"])

    def test_unique_json_remains_structured_including_repeated_keys_in_distinct_objects(self) -> None:
        body = {"left": {"message": "first"}, "right": {"message": "second"}, "rows": [{"id": 1}, {"id": 2}]}
        result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _: httpx.Response(200, json=body))
        self.assertEqual(result["body_encoding"], "json")
        self.assertEqual(result["body"], body)
        self.assertNotIn("advisory", result)

    def test_bom_non_utf8_and_trailing_suffix_preserve_every_byte(self) -> None:
        cases = (
            (b'\xef\xbb\xbf{"message":"with UTF8 BOM"}', "text"),
            ('{"message":"with UTF16 BOM"}'.encode("utf-16"), "base64"),
            (b'{"message":"valid"} trailing non-JSON suffix', "text"),
            (b'{"message":"valid"}\x80 trailing non-UTF8 suffix', "base64"),
            (b"caf\xe9 vendor diagnostic", "base64"),
        )
        for raw, expected_encoding in cases:
            with self.subTest(raw=raw):
                result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _, raw=raw: httpx.Response(200, content=raw))
                self.assertEqual(result["body_encoding"], expected_encoding)
                restored = base64.b64decode(result["body"]) if expected_encoding == "base64" else result["body"].encode("utf-8")
                self.assertEqual(restored, raw)

    def test_duplicate_json_text_still_redacts_the_issued_jwt(self) -> None:
        token, redactor = rest_client.authentication(self.config)
        raw = '{"message":"' + token + '","message":"second vendor diagnostic"}'
        with patch.object(rest_client, "authentication", return_value=(token, redactor)):
            result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _: httpx.Response(200, content=raw.encode()))
        self.assertEqual(result["body"], '{"message":"[REDACTED]","message":"second vendor diagnostic"}')
        self.assertEqual(result["body_encoding"], "text")
        self.assertIn("duplicate JSON members", result["advisory"])

    def test_tenant_and_header_refusals_before_transport(self) -> None:
        vendor = MagicMock()
        for path in ("https://other.invalid/a", "//other.invalid/a", "http:other.invalid", "\\other.invalid/a", "/x\r\nHost: other.invalid"):
            with self.subTest(path=path), self.assertRaises(rest_client.RestRequestError):
                self.call({"method": "GET", "path": path}, vendor)
        for headers in ({"X-Test": "x\r\nevil: yes"}, {"x\nevil": "yes"}):
            with self.subTest(headers=headers), self.assertRaises(rest_client.RestRequestError):
                self.call({"method": "GET", "path": "/api/v2/x", "headers": headers}, vendor)
        vendor.assert_not_called()

    def test_caller_cannot_override_authority_headers(self) -> None:
        def vendor(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.headers["host"], "org-account.snowflakecomputing.com")
            self.assertNotEqual(request.headers["authorization"], "Bearer caller")
            self.assertEqual(request.headers["X-Snowflake-Authorization-Token-Type"], "KEYPAIR_JWT")
            self.assertNotIn("proxy-authorization", request.headers)
            return httpx.Response(204)

        self.call({"method": "DELETE", "path": "/api/v2/databases/X", "headers": {"Host": "other.invalid", "Authorization": "Bearer caller", "Proxy-Authorization": "caller", "X-Snowflake-Authorization-Token-Type": "OAUTH"}}, vendor)

    def test_jwt_signature_subject_expiry_and_key_custody(self) -> None:
        with patch.object(rest_client.time, "time", return_value=1700000000):
            token, _ = rest_client.authentication(self.config)
        header, claims, signature = token.split(".")
        parsed = json.loads(base64.urlsafe_b64decode(claims + "=="))
        self.assertEqual(parsed["sub"], "ORG-ACCOUNT.SERVICE_USER")
        self.assertTrue(parsed["iss"].startswith("ORG-ACCOUNT.SERVICE_USER.SHA256:"))
        self.assertEqual(parsed["exp"] - parsed["iat"], 60)
        self.key.public_key().verify(base64.urlsafe_b64decode(signature + "=="), f"{header}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())


def encodings(secret: bytes) -> dict[str, bytes]:
    """Independently enumerate the review-required encoding cases."""
    pct = quote_from_bytes(secret, safe="")
    forms = {"raw": secret, "percent_upper": pct.encode(), "percent_lower": pct.lower().encode()}
    # Lowercase only escapes; literal token letters retain their case.
    import re

    forms["percent_lower"] = re.sub(rb"%[0-9A-F]{2}", lambda m: m[0].lower(), pct.encode())
    if secret.isascii():
        forms["form_plus"] = quote_plus(secret.decode("ascii"), safe="").encode()
    for name, encoder in (("base64", base64.b64encode), ("base64url", base64.urlsafe_b64encode)):
        encoded = encoder(secret)
        forms[name] = encoded
        forms[name + "_unpadded"] = encoded.rstrip(b"=")
        forms[name + "_escaped"] = quote_from_bytes(encoded, safe="").encode()
        forms[name + "_unpadded_escaped"] = quote_from_bytes(encoded.rstrip(b"="), safe="").encode()
    return forms


class RequestRedaction(RestFixture):
    def test_reviewer_duplicate_member_issued_token_is_redacted_before_text_conversion(self) -> None:
        token = "synthetic-vendor-issued-token+/="
        raw = b'{"access_token":"synthetic-vendor-issued-token+/=","message":"first","message":"second"}'
        with self.assertLogs(rest_client.logger, level="INFO") as logs:
            result = self.call({"method": "POST", "path": "/api/v2/" + token}, lambda _: httpx.Response(200, content=raw, headers={"X-Echo": token}))
        self.assertNotIn(token, " ".join(logs.output))
        self.assertEqual(result["body"], '{"access_token":"[REDACTED]","message":"first","message":"second"}')
        self.assertEqual(result["body_encoding"], "text")
        self.assertIn("duplicate JSON members", result["advisory"])
        for form in encodings(token.encode()).values():
            self.assertNotIn(form, json.dumps(result).encode())

    def test_issued_token_discovery_precedes_every_fallback_body_representation(self) -> None:
        token = "synthetic-vendor-issued-token+/="
        forms = encodings(token.encode())
        text = '{"outer":[{"access_token":' + json.dumps(token) + ',"message":"first","message":"second"}],"echo":' + json.dumps([value.decode() for value in forms.values()]) + "}"
        expected = text
        # Independent expectation: every echoed form and the issuing value is gone.
        for form in sorted(forms.values(), key=len, reverse=True):
            expected = expected.replace(form.decode(), "[REDACTED]")
        cases = [
            (text.encode(), expected.encode()),
            ((text + " trailing non-JSON suffix").encode(), (expected + " trailing non-JSON suffix").encode()),
            (("data: " + text + "\n\ndata: done\n").encode(), ("data: " + expected + "\n\ndata: done\n").encode()),
            (b"\xef\xbb\xbf" + text.encode(), b"\xef\xbb\xbf" + expected.encode()),
            (text.encode() + b"\x80 trailing non-UTF8 suffix", expected.encode() + b"\x80 trailing non-UTF8 suffix"),
            (b"caf\xe9 " + text.encode(), b"caf\xe9 " + expected.encode()),
        ]
        for encoding in ("utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"):
            cases.append((text.encode(encoding), expected.encode(encoding)))
            cases.append((text.encode(encoding) + b"\xff", expected.encode(encoding) + b"\xff"))
        escaped = text.replace(token, token.replace("/", r"\/")).replace("synthetic", r"synthetic")
        for encoding in ("utf-8", "utf-16", "utf-32"):
            cases.append((escaped.encode(encoding) + b"\xff", expected.encode(encoding) + b"\xff"))
        for raw, expected_raw in cases:
            with self.subTest(raw_prefix=raw[:20], length=len(raw)):
                result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _, raw=raw: httpx.Response(200, content=raw, headers={"X-Echo": token}))
                restored = base64.b64decode(result["body"]) if result["body_encoding"] == "base64" else result["body"].encode()
                self.assertEqual(restored, expected_raw)
                self.assertEqual(result["headers"]["x-echo"], "[REDACTED]")
                for form in forms.values():
                    self.assertNotIn(form, json.dumps(result).encode())

    def test_every_duplicate_token_member_is_discovered_in_nested_objects(self) -> None:
        first, second = "first-issued-secret+/=", "second-issued-secret+/="
        text = '{"nested":[{"access_token":' + json.dumps(first) + ',"access_token":' + json.dumps(second) + '}],"echo":' + json.dumps([quote_plus(first, safe=""), base64.b64encode(second.encode()).decode()]) + "}"
        result = self.call({"method": "GET", "path": "/api/v2/anything"}, lambda _: httpx.Response(200, content=text.encode()))
        self.assertEqual(result["body"], '{"nested":[{"access_token":"[REDACTED]","access_token":"[REDACTED]"}],"echo":["[REDACTED]", "[REDACTED]"]}')

    def test_each_encoding_for_private_key_and_issued_jwt(self) -> None:
        token, redactor = rest_client.authentication(self.config)
        for kind, secret in (("DER", self.config.private_key_der), ("PEM", self.pem), ("JWT", token.encode())):
            for form, value in encodings(secret).items():
                with self.subTest(secret=kind, form=form):
                    self.assertEqual(redactor.binary(value), b"[REDACTED]")
                    if value.isascii():
                        text = value.decode("ascii")
                        self.assertEqual(redactor.value({"nested": [text], text: text}), {"nested": ["[REDACTED]"], "[REDACTED]": "[REDACTED]"})

    def test_audit_headers_query_json_text_and_binary_never_leak(self) -> None:
        token, _ = rest_client.authentication(self.config)
        # Freeze token generation, so the fake vendor can echo this call's JWT.
        for kind, secret in (("PEM", self.pem), ("DER", self.config.private_key_der), ("JWT", token.encode())):
            for form, value in encodings(secret).items():
                for response_type in ("json", "text", "binary"):
                    with self.subTest(secret=kind, form=form, response=response_type):
                        safe_path = quote_from_bytes(value, safe="")

                        def vendor(_: httpx.Request, response_type: str = response_type, value: bytes = value, safe_path: str = safe_path) -> httpx.Response:
                            if response_type == "json":
                                # DER raw is tested as binary, not lossy Unicode text.
                                body = {"echo": value.decode("ascii")} if value.isascii() else {"echo": "binary tested separately"}
                                return httpx.Response(200, json=body, headers={"Link": safe_path})
                            raw = value if response_type == "text" else b"\xff" + value
                            return httpx.Response(200, content=raw, headers={"Link": safe_path})

                        _, redactor = rest_client.authentication(self.config)
                        redactor.add(token.encode())
                        with patch.object(rest_client, "authentication", return_value=(token, redactor)):
                            # The issued token must be registered even when time advanced.
                            with self.assertLogs(rest_client.logger, level="INFO") as logs:
                                result = self.call({"method": "POST", "path": "/api/v2/" + safe_path, "query": {"secret": safe_path}}, vendor)
                        self.assertNotIn(safe_path, "".join(logs.output))
                        self.assertNotIn(safe_path, json.dumps(result))
                        raw_body = base64.b64decode(result["body"]) if result["body_encoding"] == "base64" else json.dumps(result["body"]).encode()
                        if value.isascii() or result["body_encoding"] == "base64":
                            self.assertNotIn(value, raw_body)

    def test_new_response_token_and_transport_diagnostics_are_redacted(self) -> None:
        issued = "new vendor issued+/token="

        def vendor(request: httpx.Request) -> httpx.Response:
            logging.getLogger("httpcore.http11").debug("unsafe response %s", issued)
            return httpx.Response(200, json={"access_token": issued, "echo": quote_plus(issued)}, headers={"Authorization": "Bearer " + issued, "X-Echo": base64.b64encode(issued.encode()).decode()})

        with self.assertLogs("httpcore.http11", level="DEBUG") as logs:
            result = self.call({"method": "GET", "path": "/api/v2/x"}, vendor)
        self.assertNotIn(issued, "".join(logs.output))
        self.assertEqual(result["body"], {"access_token": "[REDACTED]", "echo": "[REDACTED]"})
        self.assertEqual(result["headers"]["x-echo"], "[REDACTED]")


class RequestDispatch(RestFixture):
    def test_registration_dispatch_and_worker_completion_without_connector(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        plugin = SnowflakePlugin()
        plugin.initialize({})
        manager = MagicMock()
        plugin._async_job_manager = manager
        loader = MagicMock()
        loader.resolve.return_value = self.config
        plugin._app_config_loader = loader
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "job-rest", "status": "queued"}}
        params = {"method": "DELETE", "path": "/api/v2/databases/X"}
        with patch.object(async_jobs, "ensure_worker_started"), patch("snowflake_plugin.connection.connect") as connector:
            dispatch = plugin.request(params, {"session_id": "s", "flow_id": "f"})
            self.assertEqual(dispatch["data"]["status"], "queued")
            loader.resolve.assert_not_called()
            manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
            with patch.object(rest_client, "perform_request", return_value={"status": 204, "body": ""}) as transport:
                async_jobs._process_job(plugin, manager, "job-rest", "request")
            transport.assert_called_once_with(self.config, params, timeout_seconds=plugin._login_timeout_seconds())
            connector.assert_not_called()
        self.assertEqual(manager.update_job.call_args.args[1], {"status": "completed", "result": {"status": 204, "body": ""}})
        actions = discover_actions(plugin)
        self.assertIn("request", [action.name for action in actions])
        self.assertIn("request", plugin.get_edge_process_definitions())
        process = json.loads((ROOT / "plugins/snowflake_plugin/knowledge_base/processes/request.json").read_text())
        self.assertEqual(process["process_key"], "plugin::snowflake_plugin::request")
        self.assertTrue(200 <= len(process["embedding_description"]) <= 400)

    def test_transport_error_is_generic_and_audited_once(self) -> None:
        def vendor(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("unsafe details", request=request)

        with self.assertLogs(rest_client.logger, level="INFO") as logs, self.assertRaises(httpx.ReadTimeout):
            self.call({"method": "DELETE", "path": "/api/v2/x?secret=hidden"}, vendor)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("hidden", logs.output[0])
        self.assertIn("correlation_id", logs.output[0])
        plugin = MagicMock()
        plugin._app_config_loader.resolve.side_effect = RuntimeError("unsafe credential text")
        rest_client.run_request(plugin, {"method": "DELETE", "path": "/api/v2/x"})
        self.assertNotIn("unsafe", str(plugin._error.call_args))


if __name__ == "__main__":
    unittest.main()
