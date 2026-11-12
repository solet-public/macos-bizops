"""Hermetic multi-site and 3LO lifecycle transcripts; no real credentials or network."""

from __future__ import annotations

import io
import json
import sys
import unittest
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/jira_plugin/src"))

from jira_plugin.errors import JiraServiceError  # noqa: E402
from jira_plugin.http import Response  # noqa: E402
from jira_plugin.sites import INDEX_KEY, PREFIX, SiteStore, authorization  # noqa: E402


def completed(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data}


class Vault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.history: list[tuple[str, str]] = []

    def exists(self, key: str) -> dict[str, Any]:
        return completed({"exists": key in self.values})

    def retrieve(self, key: str) -> dict[str, Any]:
        return completed({"value": self.values[key]})

    def store(self, key: str, value: str, **kwargs: Any) -> dict[str, Any]:
        self.values[key] = value
        self.history.append(("store", key))
        return completed({})

    def rotate(self, key: str, value: str) -> dict[str, Any]:
        self.values[key] = value
        self.history.append(("rotate", key))
        return completed({})

    def delete(self, key: str) -> dict[str, Any]:
        del self.values[key]
        self.history.append(("delete", key))
        return completed({})


class AddressBook:
    def resolve_with_secrets(self, name: str) -> dict[str, Any]:
        if name == "jira_oauth_app":
            fields = {"client_id": "client-123", "client_secret": 'c"s/é', "redirect_uri": "https://app.example.test/callback", "scopes": "read:jira-work write:jira-work manage:jira-project offline_access"}
        else:
            fields = {"base_url": "https://" + ("second" if name == "second_entry" else "first") + ".atlassian.net", "email": "planner@example.test", "api_token": 'x/y"é', "expires_at": "2027-10-01T00:00:00Z", "scope_note": "registered role"}
        return completed({"entries": [{"field_type": k, "value": v} for k, v in fields.items()]})


class SitesSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.vault = Vault()
        self.calls: list[tuple[str, str, dict[str, str], Any]] = []
        self.store = SiteStore(self.vault, AddressBook(), self.http)

    def http(self, method: str, url: str, headers: dict[str, str], body: Any = None) -> Response:
        self.calls.append((method, url, headers, body))
        if url.endswith("accessible-resources"):
            return Response(200, [{"id": "11223344-5566-7788-9900-aabbccddeeff", "url": "https://second.atlassian.net", "name": "Second", "scopes": ["read:jira-work", "write:jira-work"]}], {})
        return Response(200, {"access_token": "access/é", "refresh_token": "rotating-refresh/é", "expires_in": 3600, "scope": "read:jira-work write:jira-work offline_access"}, {})

    def bind(self, site: str, version: int, entry: str = "first_entry", default: bool = False) -> dict[str, Any]:
        return self.store.manage({"operation": "bind_api_token", "site": site, "entry_name": entry, "expected_version": version, "set_default": default})

    def test_binding_correction_default_removal_and_stale_write(self) -> None:
        self.assertEqual(self.store.select(), "legacy")
        first = self.bind("first", 0, default=True)
        self.assertEqual(first["version"], 1)
        self.bind("second", 1, "second_entry")
        self.assertEqual(self.store.select(), "first")
        self.assertEqual(self.store.select("SECOND"), "second")
        a = self.store.credentials("first")
        b = self.store.credentials("second")
        self.assertNotEqual(a["base_url"], b["base_url"])
        self.assertNotEqual(self.store.key("first"), self.store.key("second"))
        with self.assertRaisesRegex(JiraServiceError, "expected_version"):
            self.bind("first", 0, "second_entry")
        self.assertEqual(self.store.credentials("first")["base_url"], a["base_url"])
        self.bind("first", 2, "second_entry")
        self.assertEqual(self.store.credentials("first")["base_url"], b["base_url"])
        self.store.manage({"operation": "set_default", "site": "second", "expected_version": 3})
        self.assertEqual(self.store.select(), "second")
        self.store.manage({"operation": "disconnect", "site": "second", "expected_version": 4})
        self.assertFalse(any(k.startswith(self.store.key("second")) for k in self.vault.values))
        self.assertEqual(self.store.select(), "legacy")
        with self.assertRaises(JiraServiceError):
            self.store.select("second")
        # Legacy remains independently resolvable, rather than silently disappearing.
        self.assertEqual(self.store.credentials("legacy")["base_url"], "https://first.atlassian.net")
        self.assertNotIn("x/y", json.dumps(self.store.describe()))
        self.assertIn(("rotate", INDEX_KEY), self.vault.history)

    def test_oauth_consent_exchange_resource_identity_refresh_and_cancel(self) -> None:
        begin = self.store.oauth({"operation": "begin", "site": "second", "base_url": "https://second.atlassian.net"})
        query = parse_qs(urlsplit(begin["authorize_url"]).query)
        self.assertEqual(query["audience"], ["api.atlassian.com"])
        self.assertIn("offline_access", query["scope"][0])
        self.assertNotIn("client_secret", query)
        code_key = PREFIX + "oauth_code__second"
        self.vault.values[code_key] = "one-use-code"
        params = {"operation": "complete", "site": "second", "state": query["state"][0], "code_vault_key": code_key, "expected_version": 0, "set_default": True}
        with self.assertRaises(JiraServiceError):
            self.store.oauth({**params, "state": "mismatch"})
        self.assertEqual(self.calls, [])
        result = self.store.oauth(params)
        self.assertEqual(result["default"], "second")
        self.assertEqual(self.calls[0][0:2], ("POST", "https://auth.atlassian.com/oauth/token"))
        self.assertEqual(self.calls[0][3]["grant_type"], "authorization_code")
        self.assertEqual(self.calls[0][3]["redirect_uri"], "https://app.example.test/callback")
        self.assertNotIn(code_key, self.vault.values)
        credentials = self.store.credentials("second")
        root, auth = authorization(credentials)
        self.assertEqual(root, "https://api.atlassian.com/ex/jira/11223344-5566-7788-9900-aabbccddeeff")
        self.assertEqual(auth, "Bearer access/é")
        self.assertNotIn("access/é", json.dumps(result))
        credentials["expires_at"] = 0
        self.store._write(self.store.credential_key("second"), credentials)
        refreshed = self.store.credentials("second")
        self.assertEqual(self.calls[-1][3]["grant_type"], "refresh_token")
        self.assertEqual(self.calls[-1][3]["refresh_token"], "rotating-refresh/é")
        self.assertGreater(refreshed["expires_at"], 0)
        with self.assertRaises(JiraServiceError):
            self.store.oauth(params)  # state was consumed
        self.store.oauth({"operation": "begin", "site": "new", "base_url": "https://first.atlassian.net"})
        self.assertTrue(self.store.oauth({"operation": "status", "site": "new"})["pending"])
        self.store.oauth({"operation": "cancel", "site": "new"})
        with self.assertRaises(JiraServiceError):
            self.store.oauth({"operation": "status", "site": "new"})

    def test_expired_or_wrong_site_grant_never_binds(self) -> None:
        begin = self.store.oauth({"operation": "begin", "site": "first", "base_url": "https://first.atlassian.net"})
        state = parse_qs(urlsplit(begin["authorize_url"]).query)["state"][0]
        code_key = PREFIX + "oauth_code"
        self.vault.values[code_key] = "code"
        with self.assertRaisesRegex(JiraServiceError, "does not include"):
            self.store.oauth({"operation": "complete", "site": "first", "state": state, "code_vault_key": code_key, "expected_version": 0})
        self.assertEqual(self.store.index()["sites"], {})
        pending_key = self.store.key("first") + "__pending"
        pending = self.store._read(pending_key)
        assert pending is not None
        pending["deadline"] = 0
        self.store._write(pending_key, pending)
        with self.assertRaisesRegex(JiraServiceError, "expired"):
            self.store.oauth({"operation": "complete", "site": "first", "state": state})

    def test_failed_index_publication_preserves_old_binding_and_disconnect_resumes_cleanup(self) -> None:
        self.bind("first", 0)
        before = self.store.credential_key("first")
        original_rotate = self.vault.rotate

        def fail_index(key: str, value: str) -> dict[str, Any]:
            if key == INDEX_KEY:
                return {"action_status": "error"}
            return original_rotate(key, value)

        self.vault.rotate = fail_index
        with self.assertRaises(JiraServiceError):
            self.bind("first", 1, "second_entry")
        self.assertEqual(self.store.credential_key("first"), before)
        self.assertEqual(self.store.credentials("first")["base_url"], "https://first.atlassian.net")
        self.vault.rotate = original_rotate
        original_delete = self.vault.delete
        self.vault.delete = lambda key: {"action_status": "error"}
        with self.assertRaises(JiraServiceError):
            self.store.manage({"operation": "disconnect", "site": "first", "expected_version": 1})
        self.assertEqual(self.store.describe()["credential_cleanup_pending"], ["first"])
        self.vault.delete = original_delete
        current = self.store.describe()["version"]
        self.store.manage({"operation": "disconnect", "site": "first", "expected_version": current})
        self.assertEqual(self.store.describe()["credential_cleanup_pending"], [])
        history = self.store.manage({"operation": "history"})["history"]
        self.assertEqual([h["operation"] for h in history], ["bind", "disconnect", "credentials_removed"])
        self.assertEqual([h["version"] for h in history], [1, 2, 3])

    def test_json_http_headers_null_body_errors_and_redirect_refusal(self) -> None:
        from jira_plugin.http import request

        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"ok": true}'
        response.headers.items.return_value = [("Retry-After", "2")]
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch("urllib.request.build_opener", return_value=opener) as build:
            result = request("PUT", "https://first.atlassian.net/rest/agile/1.0/sprint/37/properties/key", {"Authorization": "Bearer test-only"}, None)
            self.assertEqual(result.data, {"ok": True})
            self.assertEqual(result.headers, {"retry-after": "2"})
            req = opener.open.call_args.args[0]
            self.assertEqual(req.data, b"null")
            self.assertEqual(req.get_header("Authorization"), "Bearer test-only")
            self.assertIsNone(build.call_args.args[0]().redirect_request(None, None, 302, "Moved", {}, "https://other.example.test"))
            opener.open.side_effect = HTTPError("https://first.atlassian.net", 403, "denied", Message(), io.BytesIO(b'{"errorMessages":["Denied"]}'))
            self.assertEqual(request("GET", "https://first.atlassian.net", {}).status, 403)
            opener.open.side_effect = URLError("secret-reflection")
            with self.assertRaises(JiraServiceError) as caught:
                request("GET", "https://first.atlassian.net", {})
            self.assertNotIn("secret-reflection", str(caught.exception))

    def test_oauth_denial_is_generic_and_refresh_reply_requires_rotating_token(self) -> None:
        from jira_plugin.oauth import OAuthFlow

        flow = OAuthFlow(self.store)
        self.store.http = lambda *args, **kwargs: Response(401, {"error_description": "secret-reflection"}, {})
        with self.assertRaises(JiraServiceError) as caught:
            flow.token({"grant_type": "refresh_token"})
        self.assertNotIn("secret-reflection", str(caught.exception))
        self.store.http = lambda *args, **kwargs: Response(200, {"access_token": "new-access", "expires_in": 3600}, {})
        with self.assertRaisesRegex(JiraServiceError, "rotating refresh"):
            flow.token({"grant_type": "refresh_token"})

    def test_readiness_uses_only_injected_proxy_with_the_jira_caller(self) -> None:
        from ananta.services.vault_service.vault_service_proxy import VaultServiceProxy
        from jira_plugin.plugin import JiraPlugin

        plugin = JiraPlugin()
        raw = MagicMock()
        raw.exists.return_value = completed({"exists": False})
        proxy = VaultServiceProxy(raw, caller_plugin="jira_plugin")
        plugin.set_vault_service(proxy)
        orchestrator = MagicMock()

        def service(name: str) -> Any:
            self.assertEqual(name, "address_book_service", "consumer must never obtain the raw vault")
            return AddressBook()

        orchestrator.get_service.side_effect = service
        plugin.orchestrator_ref = orchestrator
        plugin.prepare_for_readiness()
        self.assertIs(plugin._require_sites().vault, proxy)
        result = plugin.sites({"operation": "list"}, {})
        self.assertEqual(result["action_status"], "completed")
        context = raw.exists.call_args.kwargs["call_context"]
        self.assertEqual(context.calling_plugin, "jira_plugin")
        self.assertEqual(context.principal_kind, "plugin")
        # A profile without a vault still boots legacy; new binding use reports its dependency.
        legacy = JiraPlugin()
        legacy.orchestrator_ref = orchestrator
        legacy.prepare_for_readiness()
        self.assertIsNotNone(legacy._client_factory)
        with self.assertRaises(JiraServiceError):
            legacy._require_sites()

    def test_additional_site_sdk_honors_timeout_set_correct_and_clear(self) -> None:
        from jira_plugin.constants import DEFAULT_REQUEST_TIMEOUT_SECONDS
        from jira_plugin.plugin import JiraPlugin
        from jira_plugin.site_context import CURRENT_SITE

        plugin = JiraPlugin()
        plugin._sites = MagicMock()
        plugin._sites.credentials.return_value = {"auth_type": "api_token", "base_url": "https://example.atlassian.net", "email": "planner@example.test", "api_token": "test-only"}
        token = CURRENT_SITE.set("example")
        try:
            for value in (1.25, 2.5, None):
                plugin.initialize({"request_timeout_seconds": value} if value is not None else {})
                with patch("jira_plugin.plugin.JIRA") as constructor:
                    plugin._require_client()
                    expected = DEFAULT_REQUEST_TIMEOUT_SECONDS if value is None else value
                    self.assertEqual(constructor.call_args.kwargs["timeout"], expected)
                    self.assertEqual(constructor.call_args.kwargs["server"], "https://example.atlassian.net")
        finally:
            CURRENT_SITE.reset(token)

    def test_registered_oauth_begin_preserves_callback_before_publication(self) -> None:
        from jira_plugin.plugin import JiraPlugin

        class PublicCallback(AddressBook):
            def resolve_with_secrets(self, name: str) -> dict[str, Any]:
                result = super().resolve_with_secrets(name)
                if name == "jira_oauth_app":
                    for field in result["data"]["entries"]:
                        if field["field_type"] == "redirect_uri":
                            field["value"] = "https://app.example.test/callback/public?continue=board%2F23"
                return result

        plugin = JiraPlugin()
        plugin._sites = SiteStore(Vault(), PublicCallback())
        result = plugin.oauth({"operation": "begin", "site": "new-site", "base_url": "https://first.atlassian.net"}, {})
        self.assertEqual(result["action_status"], "completed")
        # The published consent URL carries the registered callback intact and,
        # by construction, never the client secret.
        query = parse_qs(urlsplit(result["data"]["authorize_url"]).query)
        self.assertEqual(query["redirect_uri"], ["https://app.example.test/callback/public?continue=board%2F23"])
        self.assertNotIn('c"s/é', json.dumps(result))


if __name__ == "__main__":
    unittest.main()
