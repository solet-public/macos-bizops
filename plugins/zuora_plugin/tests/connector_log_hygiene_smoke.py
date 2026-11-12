"""Connector log lines carry no credential, header, body or query value.

Hygiene is structural: each connector builds its audit and log lines only from
method, path without query values, status, duration and correlation id, and the
platform's logging setup (``ananta.logging_setup``) keeps third-party request
lines (httpx, httpcore, urllib3) below the level they would be built at. This
smoke only verifies the captured text: it drives one failing request per
connector family that logs, through the real logging machinery, and checks that
no Authorization value, request body, query value or configured credential
literal appears in plain or percent-encoded form.
"""

from __future__ import annotations

import json
import logging
import sys
import unittest
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib.parse import quote, quote_plus

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[3]
for plugin in ("jira_plugin", "marketo_plugin", "salesforce_plugin", "snowflake_plugin", "zuora_plugin"):
    sys.path.insert(0, str(ROOT / "plugins" / plugin / "src"))

import ananta.logging_setup  # noqa: E402, F401  # pyright: ignore[reportUnusedImport] — the platform's third-party logger levels
from jira_plugin.plugin import JiraPlugin  # noqa: E402
from marketo_plugin.app_config import MarketoInstanceConfig  # noqa: E402
from marketo_plugin.http_client import MarketoClient  # noqa: E402
from salesforce_plugin import request_actions as salesforce_request  # noqa: E402
from snowflake_plugin import rest_client as snowflake_rest  # noqa: E402
from snowflake_plugin.app_config import SnowflakeAccountConfig  # noqa: E402
from zuora_plugin import request_actions as zuora_request  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402

CLIENT_SECRET = "configured secret/+?=é"
ISSUED_TOKEN = "issued-token/+?="  # travels in the Authorization header, so ASCII
HEADER_VALUE = "header-secret/+?="  # header values travel as ASCII
BODY_VALUE = "body secret/+?=é"
QUERY_VALUE = "query secret/+?=é"


def forms(values: Iterable[str]) -> list[str]:
    """Each value in plain and percent-encoded form."""
    return [form for value in values for form in (value, quote(value, safe=""), quote_plus(value, safe=""))]


class ConnectorLogHygiene(unittest.TestCase):
    def capture(self, action: Callable[[], Any]) -> str:
        """Every record any logger emits while ``action`` runs, rendered as text."""
        with self.assertLogs(level="DEBUG") as captured:
            logging.getLogger("connector_log_hygiene").debug("capture opened")
            try:
                action()
            except Exception:  # noqa: BLE001 — the failing request may raise; only its log text is checked
                pass
        return "\n".join(captured.output)

    def assert_clean(self, text: str, *extra: str) -> None:
        for form in forms((CLIENT_SECRET, ISSUED_TOKEN, HEADER_VALUE, BODY_VALUE, QUERY_VALUE, *extra)):
            self.assertNotIn(form, text)
        self.assertNotIn("Bearer", text)

    def test_platform_logging_keeps_third_party_request_lines_unbuilt(self) -> None:
        for name in ("httpx", "httpcore", "urllib3"):
            self.assertGreaterEqual(logging.getLogger(name).getEffectiveLevel(), logging.WARNING, name)

    def test_salesforce_failing_request(self) -> None:
        executor = MagicMock()
        executor.run_rest.return_value = {"statusCode": 401, "headers": {"WWW-Authenticate": f"Bearer {ISSUED_TOKEN}"}, "body": {"echo": BODY_VALUE}}
        params = {"method": "POST", "path": "/services/apexrest/hygiene", "query": {"q": QUERY_VALUE}, "headers": {"X-Api-Key": HEADER_VALUE}, "body": json.dumps({"password": BODY_VALUE})}
        text = self.capture(lambda: salesforce_request.authenticated_request(executor, params))
        self.assertIn("/services/apexrest/hygiene", text)
        self.assertIn("'status': 401", text)
        self.assert_clean(text)

    def test_snowflake_failing_request(self) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        config = SnowflakeAccountConfig("org-account", "service_user", "WH", "DB", "PUBLIC", "ROLE", "key_pair", der)
        sent: list[str] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            sent.append(request.headers["Authorization"].removeprefix("Bearer "))
            return httpx.Response(401, json={"echo": BODY_VALUE})

        params = {"method": "POST", "path": "/api/v2/statements", "query": {"q": QUERY_VALUE}, "headers": {"X-Api-Key": HEADER_VALUE}, "body": {"password": BODY_VALUE}}
        text = self.capture(lambda: snowflake_rest.perform_request(config, params, timeout_seconds=5, transport=httpx.MockTransport(vendor)))
        self.assertIn("/api/v2/statements", text)
        self.assertIn("'status': 401", text)
        self.assertEqual(len(sent), 1)
        self.assert_clean(text, *sent)

    def test_marketo_failing_request(self) -> None:
        client = MarketoClient(MarketoInstanceConfig(base_url="https://tenant.mktorest.test", client_id="hygiene-client", client_secret=CLIENT_SECRET), timeout_seconds=5.0)
        client._http.close()  # pyright: ignore[reportPrivateUsage]

        def vendor(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/identity/oauth/token":
                return httpx.Response(200, json={"access_token": ISSUED_TOKEN, "expires_in": 3600})
            return httpx.Response(500, json={"success": False, "echo": BODY_VALUE})

        client._http = httpx.Client(base_url="https://tenant.mktorest.test", transport=httpx.MockTransport(vendor))  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(client.close)
        text = self.capture(lambda: client.request("POST", "/rest/v1/leads.json", query={"q": QUERY_VALUE}, headers={"X-Api-Key": HEADER_VALUE}, json_body={"password": BODY_VALUE}))
        self.assertIn("/rest/v1/leads.json", text)
        self.assertIn("status=500", text)
        self.assert_clean(text)

    def test_zuora_failing_request(self) -> None:
        client = ZuoraClient(ZuoraTenantConfig(base_url="https://tenant.example.test", client_id="hygiene-client", client_secret=CLIENT_SECRET), timeout_seconds=2)
        client._http.close()  # pyright: ignore[reportPrivateUsage]

        def vendor(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/oauth/token":
                return httpx.Response(200, json={"access_token": ISSUED_TOKEN, "expires_in": 3600})
            return httpx.Response(400, json={"echo": BODY_VALUE})

        client._http = httpx.Client(base_url="https://tenant.example.test", transport=httpx.MockTransport(vendor))  # pyright: ignore[reportPrivateUsage]
        self.addCleanup(client.close)
        params = {"method": "POST", "path": "/v1/accounts", "query": {"q": QUERY_VALUE}, "headers": {"X-Api-Key": HEADER_VALUE}, "body": {"password": BODY_VALUE}}
        text = self.capture(lambda: zuora_request.request(client, params))
        self.assertIn("/v1/accounts", text)
        self.assertIn("status=400", text)
        self.assert_clean(text)

    def test_jira_failing_request(self) -> None:
        plugin = JiraPlugin()
        plugin.logger = logging.getLogger("jira_plugin.hygiene")

        def produce() -> dict[str, Any]:
            raise RuntimeError(f"vendor echoed {CLIENT_SECRET} {ISSUED_TOKEN} {BODY_VALUE} {QUERY_VALUE}")

        text = self.capture(lambda: plugin._run(produce, "boards"))  # pyright: ignore[reportPrivateUsage]
        self.assertIn("boards: Jira operation failed", text)
        self.assert_clean(text)


if __name__ == "__main__":
    unittest.main()
