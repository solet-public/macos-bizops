"""Drive, People and Tasks contract tests through the real Google client.

Offline transcripts cover every typed process, account binding, pagination,
request bodies, media, terminal transitions and Google error mapping. No live
credential or network is used.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

import httplib2
from googleapiclient.discovery import build_from_document

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/g_suite_plugin/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from g_suite_plugin.gmail_actions import GmailIO, OutgoingAttachment  # noqa: E402
from g_suite_plugin.oauth.service_factory import GoogleServiceFactory  # noqa: E402
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from g_suite_plugin.workspace_actions import execute_method, request_arguments  # noqa: E402
from g_suite_plugin.workspace_registry import WORKSPACE_METHODS, discovery_document, parameter_schemas, process_parameters  # noqa: E402
from gsuite_fakes import connect_accounts  # noqa: E402  # pyright: ignore[reportMissingImports]


class RecordingHttp:
    """Replay Google-shaped HTTP responses and capture actual client requests."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **kwargs: Any) -> tuple[httplib2.Response, bytes]:
        parts = urlsplit(uri)
        self.requests.append({"method": method, "path": parts.path, "query": parse_qs(parts.query), "body": body, "headers": headers})
        status, data = self.responses.pop(0)
        raw = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else b""
        return httplib2.Response({"status": str(status), "etag": '"response-etag"', "content-type": "application/octet-stream" if isinstance(data, bytes) else "application/json"}), raw


def client(product: str, responses: list[tuple[int, Any]]) -> tuple[Any, RecordingHttp]:
    http = RecordingHttp(responses)
    return build_from_document(json.dumps(discovery_document(product)), http=http), http


def required_params(verb: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, schema in parameter_schemas(verb).items():
        if not schema.get("required"):
            continue
        value: Any = schema.get("enum", ["test-id"])[0]
        if schema.get("type") == "integer":
            value = int(schema.get("minimum", 1))
        if name == "resourceName":
            value = "contactGroups/test-id" if "contact_groups" in verb else "otherContacts/test-id" if "other_contacts" in verb else "people/test-id"
        result[name] = value
    for name, value in list(result.items()):
        if parameter_schemas(verb)[name].get("repeated"):
            result[name] = [value]
    return result


class WorkspaceTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blobs: list[tuple[bytes, str, str]] = []
        self.io = GmailIO(attachment_loader=lambda key: OutgoingAttachment("test.txt", "text/plain", b"contents"), blob_writer=self.store_blob)

    def store_blob(self, content: bytes, filename: str, mime: str) -> str:
        self.blobs.append((content, filename, mime))
        return "blob-test"

    def call(self, verb: str, params: dict[str, Any], *responses: tuple[int, Any], tier: str = "workspace_user") -> tuple[dict[str, Any], RecordingHttp]:
        product, _ = WORKSPACE_METHODS[verb]
        service, http = client(product, list(responses))
        result = execute_method(service, verb, params, self.io, {"account_tier": tier, "granted_scopes": []})
        return result, http

    def test_every_vendor_method_and_registration(self) -> None:
        plugin = GSuitePlugin()
        counts = {product: sum(p == product for p, _ in WORKSPACE_METHODS.values()) for product in ("drive", "people", "tasks")}
        self.assertEqual(counts, {"drive": 64, "people": 24, "tasks": 14})
        definitions = plugin.get_edge_process_definitions()
        producers = plugin._deferred_producers()  # noqa: SLF001
        for verb, (product, method) in WORKSPACE_METHODS.items():
            with self.subTest(verb=verb):
                metadata = getattr(type(plugin), verb)._platform_process_metadata
                self.assertIn(verb, definitions)
                self.assertIn(verb, producers)
                self.assertIn("account", process_parameters(verb))
                self.assertTrue(metadata)
                kb = json.loads((ROOT / f"plugins/g_suite_plugin/knowledge_base/processes/{verb}.json").read_text())
                self.assertEqual(kb["process_key"], f"plugin::g_suite_plugin::{verb}")
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400)
                params = required_params(verb)
                if "request" in method:
                    params["body"] = {"etag": "version-1"}
                response = {"id": "object-1", "nextPageToken": "next-2", "nextSyncToken": "sync-3"}
                result, http = self.call(verb, params, (200, response))
                if not method.get("response") and not method.get("supportsMediaDownload"):
                    self.assertIsNone(result["response"])
                elif method["id"] == "drive.files.export":
                    self.assertEqual(result["file_blob_key"], "blob-test")
                else:
                    self.assertEqual(result["response"], response)
                request = http.requests[0]
                expected = discovery_document(product)["servicePath"] + method["path"]
                for key, value in params.items():
                    expected = expected.replace("{" + key + "}", quote(str(value), safe="")).replace("{+" + key + "}", quote(str(value), safe="/"))
                self.assertEqual(request["path"], "/" + expected)
                self.assertEqual(request["method"], method["httpMethod"])
                if "body" in params:
                    self.assertEqual(json.loads(request["body"]), params["body"])

    def test_paging_queries_and_field_masks(self) -> None:
        cases = [
            ("drive_files_list", {"q": "trashed = false and 'parent' in parents", "pageToken": "p2", "pageSize": 1000, "supportsAllDrives": True, "$.xgafv": "2"}, {"files": [{"id": "f"}], "nextPageToken": "p3"}),
            ("people_people_connections_list", {"resourceName": "people/me", "personFields": "names,emailAddresses", "pageToken": "p2", "requestSyncToken": True}, {"connections": [{"resourceName": "people/p"}], "nextPageToken": "p3", "nextSyncToken": "s3"}),
            ("tasks_tasks_list", {"tasklist": "@default", "showCompleted": True, "showHidden": True, "pageToken": "p2"}, {"items": [{"id": "task", "status": "completed"}], "nextPageToken": "p3"}),
        ]
        for verb, params, response in cases:
            result, http = self.call(verb, params, (200, response))
            self.assertEqual(result["response"], response)
            self.assertEqual(http.requests[0]["query"]["pageToken"], ["p2"])
            self.assertEqual(http.requests[0]["query"]["fields"], ["*"])
            if verb == "drive_files_list":
                self.assertEqual(http.requests[0]["query"]["$.xgafv"], ["2"])
        _, http = self.call("drive_comments_get", {"fileId": "f", "commentId": "c", "fields": "id,content"}, (200, {"id": "c"}))
        self.assertEqual(http.requests[0]["query"]["fields"], ["id,content"])

    def test_media_transfer(self) -> None:
        result, http = self.call("drive_files_export", {"fileId": "f", "mimeType": "application/pdf"}, (200, b"%PDF-test"))
        self.assertEqual(result["file_blob_key"], "blob-test")
        self.assertEqual(self.blobs[-1][0], b"%PDF-test")
        result, _ = self.call("drive_files_get", {"fileId": "f", "alt": "media"}, (200, b"contents"))
        self.assertEqual(result["file_blob_key"], "blob-test")
        _, http = self.call("drive_files_update", {"fileId": "f", "body": {"description": "corrected"}, "blob_key": "blob-source"}, (200, {"id": "f"}))
        self.assertEqual(http.requests[0]["method"], "PATCH")
        self.assertIn(b"contents", http.requests[0]["body"])

    def test_corrections_reversals_and_terminals(self) -> None:
        cases = [
            ("drive_files_update", {"fileId": "f", "body": {"trashed": True}}, {"trashed": True}),
            ("drive_files_update", {"fileId": "f", "body": {"trashed": False}}, {"trashed": False}),
            ("people_people_update_contact", {"resourceName": "people/p", "updatePersonFields": "names", "body": {"etag": "current", "names": [{"givenName": "Corrected"}]}}, {"etag": "next"}),
            ("tasks_tasks_patch", {"tasklist": "l", "task": "t", "body": {"status": "completed"}}, {"status": "completed"}),
            ("tasks_tasks_patch", {"tasklist": "l", "task": "t", "body": {"status": "needsAction", "completed": None}}, {"status": "needsAction"}),
        ]
        for verb, params, response in cases:
            result, http = self.call(verb, params, (200, response))
            self.assertEqual(result["response"], response)
            self.assertEqual(json.loads(http.requests[0]["body"]), params["body"])
        for verb, params in [("drive_files_delete", {"fileId": "f"}), ("people_people_delete_contact", {"resourceName": "people/p"}), ("tasks_tasks_delete", {"tasklist": "l", "task": "t"})]:
            result, http = self.call(verb, params, (204, None))
            self.assertIn(result["response"], (None, {}))
            self.assertEqual(http.requests[0]["method"], "DELETE")

    def test_factory_isolation_scopes_and_etags(self) -> None:
        before = json.dumps(discovery_document("drive"), sort_keys=True)
        factory = GoogleServiceFactory(SimpleNamespace(get_access_token=lambda: "fake-access", credential_facts=lambda: {}))  # type: ignore[arg-type]
        factory.workspace_api("drive")
        self.assertEqual(before, json.dumps(discovery_document("drive"), sort_keys=True))
        _, http = self.call("tasks_tasks_patch", {"tasklist": "l", "task": "t", "body": {"title": "Corrected"}, "if_match": '"etag-v1"'}, (200, {"etag": '"etag-v2"'}))
        self.assertEqual(http.requests[0]["headers"]["If-Match"], '"etag-v1"')
        result, _ = self.call("drive_files_get", {"fileId": "f"}, (200, {"id": "f"}))
        self.assertEqual(result["response_headers"]["etag"], '"response-etag"')
        service, http = client("tasks", [(412, {"error": {"message": "Precondition Failed"}})])
        with self.assertRaisesRegex(Exception, "Precondition Failed"):
            execute_method(service, "tasks_tasks_patch", {"tasklist": "l", "task": "t", "body": {"title": "Stale"}, "if_match": '"stale"'}, self.io, {"granted_scopes": []})
        self.assertEqual(len(http.requests), 1)
        service, http = client("tasks", [(200, {})])
        with self.assertRaisesRegex(Exception, "Reconnect.*connect_account"):
            execute_method(service, "tasks_tasks_list", {"tasklist": "l"}, self.io, {"granted_scopes": ["https://www.googleapis.com/auth/contacts"]})
        self.assertEqual(http.requests, [])
        execute_method(service, "tasks_tasks_list", {"tasklist": "l"}, self.io, {"granted_scopes": ["https://www.googleapis.com/auth/tasks.readonly"]})
        self.assertEqual(len(http.requests), 1)

    def test_validation(self) -> None:
        for params in ({}, {"fileId": 12}, {"fileId": "f", "acknowledgeAbuse": "yes"}):
            with self.assertRaises(ValueError):
                request_arguments("drive_files_get", params)
        for value in (0, 1001, True, "20"):
            with self.assertRaises(ValueError):
                request_arguments("drive_files_list", {"pageSize": value})
        with self.assertRaises(ValueError):
            self.call("tasks_tasks_patch", {"tasklist": "l", "task": "t", "body": []}, (200, {}))
        self.assertFalse({"access_token", "oauth_token", "key"} & process_parameters("drive_files_get").keys())

    def test_account_errors_tiers_and_redaction(self) -> None:
        plugin = GSuitePlugin()
        accounts = connect_accounts(plugin, ["ann@gmail.com", "bob@example.com"])
        store = accounts.token_store("ann@gmail.com")
        secrets = store.issued_secrets()
        verb = "tasks_tasklists_list"
        for status, reason, code in [(401, "authError", "gsuite.auth_expired"), (403, "insufficientPermissions", "gsuite.missing_scope"), (404, "notFound", "gsuite.not_found"), (429, "rateLimitExceeded", "gsuite.rate_limited"), (400, "failedPrecondition", "gsuite.invalid_params")]:
            error = {"error": {"code": status, "message": "stale etag" if status == 400 else "denied", "errors": [{"reason": reason}]}}
            service, http = client("tasks", [(status, error)])
            plugin._require_factory = lambda service=service: SimpleNamespace(workspace_api=lambda product: service, credential_facts=lambda: {"granted_scopes": []})  # type: ignore[method-assign]  # noqa: SLF001
            result = plugin._run(lambda: plugin._deferred_producers()[verb]({}), verb, {})  # noqa: SLF001
            self.assertEqual(result["error"]["code"], code)
            self.assertEqual(len(http.requests), 1)
        service, http = client("tasks", [(200, {"items": [], "secret": " ".join(secrets)})])
        plugin._require_factory = lambda service=service: SimpleNamespace(workspace_api=lambda product: service, credential_facts=lambda: {"granted_scopes": []})  # type: ignore[method-assign]  # noqa: SLF001
        result = plugin._run(lambda: plugin._deferred_producers()[verb]({}), verb, {})  # noqa: SLF001
        self.assertEqual(result["data"]["account"], "ann@gmail.com")
        serialized = json.dumps(result)
        for secret in secrets:
            self.assertNotIn(secret, serialized)
        result = plugin._run(lambda: {}, verb, {"account": "unknown"})  # noqa: SLF001
        self.assertEqual(result["error"]["code"], "gsuite.unknown_account")
        service, http = client("people", [(200, {"people": []})])
        directory = "people_people_list_directory_people"
        with self.assertRaisesRegex(Exception, "tier is consumer"):
            execute_method(service, directory, {"readMask": "names", "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"]}, self.io, {"account_tier": "consumer", "granted_scopes": []})
        self.assertEqual(http.requests, [])
        result = execute_method(service, directory, {"readMask": "names", "sources": ["DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE"]}, self.io, {"account_tier": "workspace_user", "granted_scopes": ["https://www.googleapis.com/auth/directory.readonly"]})
        self.assertEqual(result["response"], {"people": []})
        for secret in secrets:
            variants = [secret, quote(secret, safe=""), quote_plus(secret), base64.b64encode(secret.encode()).decode(), base64.urlsafe_b64encode(secret.encode()).decode().rstrip("=")]
            service, _ = client("tasks", [(403, {"error": {"message": " ".join(variants)}})])
            plugin._require_factory = lambda service=service: SimpleNamespace(workspace_api=lambda product: service, credential_facts=lambda: {"granted_scopes": []})  # type: ignore[method-assign]  # noqa: SLF001
            result = plugin._run(lambda: plugin._deferred_producers()[verb]({}), verb, {})  # noqa: SLF001
            for variant in variants:
                self.assertNotIn(variant, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
