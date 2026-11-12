"""Docs, Sheets and Slides contract tests through the real Google client.

Offline transcripts cover all 25 typed editor processes (Docs v1, Sheets v4,
Slides v1, discovery revision 20261106): the three registration artifacts,
dispatch to completion through the background job path, every documented
method and API-wide parameter on the wire, raw alt/callback replies, refused
credential parameters, request bodies checked against the discovery schema,
create/correct/reverse/terminal lifecycles, stale-write refusal, the account
contract and Google error mapping. No network is used.
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

from g_suite_plugin.editors_api_processes import EditorsAPIProcesses  # noqa: E402
from g_suite_plugin.editors_contract import CREDENTIAL_PARAMETERS, EDITOR_PRODUCTS, check_editor_call, editor_verbs, refuse_credential_parameters, validate_body, validate_value  # noqa: E402
from g_suite_plugin.gmail_actions import GmailIO, OutgoingAttachment  # noqa: E402
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from g_suite_plugin.workspace_actions import RAW_ALT_PRODUCTS, execute_method, raw_reply_mime  # noqa: E402
from g_suite_plugin.workspace_registry import WORKSPACE_METHODS, WORKSPACE_PRODUCTS, discovery_document, parameter_schemas, process_parameters  # noqa: E402
from gsuite_fakes import connect_accounts  # noqa: E402  # pyright: ignore[reportMissingImports]

STATE = {"session_id": "session-1", "flow_id": "flow-1"}
EDIT_SCOPES = ["https://www.googleapis.com/auth/documents", "https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/presentations"]
API_WIDE = {"$.xgafv", "alt", "callback", "fields", "prettyPrint", "quotaUser", "uploadType", "upload_protocol"}

# A realistic, schema-valid body for every method that takes one.
BODIES: dict[str, dict[str, Any]] = {
    "docs_documents_batch_update": {"requests": [{"insertText": {"location": {"index": 1}, "text": "Hello"}}, {"updateTextStyle": {"range": {"startIndex": 1, "endIndex": 6}, "textStyle": {"bold": True}, "fields": "bold"}}], "writeControl": {"requiredRevisionId": "rev-1"}},
    "docs_documents_create": {"title": "Throwaway"},
    "sheets_spreadsheets_get_by_data_filter": {"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "includeGridData": False},
    "sheets_spreadsheets_batch_update": {"requests": [{"addSheet": {"properties": {"title": "Added", "gridProperties": {"rowCount": 10, "columnCount": 2}}}}], "includeSpreadsheetInResponse": False},
    "sheets_spreadsheets_create": {"properties": {"title": "Throwaway"}, "sheets": [{"properties": {"title": "Data"}}]},
    "sheets_spreadsheets_developer_metadata_search": {"dataFilters": [{"developerMetadataLookup": {"metadataKey": "owner"}}]},
    "sheets_spreadsheets_values_clear": {},
    "sheets_spreadsheets_values_append": {"range": "Sheet1!A1:B2", "majorDimension": "ROWS", "values": [["a", 1], [True, 2.5]]},
    "sheets_spreadsheets_values_update": {"range": "Sheet1!A1:B2", "majorDimension": "ROWS", "values": [["a", 1], [True, 2.5]]},
    "sheets_spreadsheets_values_batch_get_by_data_filter": {"dataFilters": [{"a1Range": "Sheet1!A1:B2"}], "majorDimension": "ROWS"},
    "sheets_spreadsheets_values_batch_clear": {"ranges": ["Sheet1!A1:B2"]},
    "sheets_spreadsheets_values_batch_update": {"valueInputOption": "RAW", "data": [{"range": "Sheet1!A1", "values": [["x"]]}]},
    "sheets_spreadsheets_values_batch_update_by_data_filter": {"valueInputOption": "USER_ENTERED", "data": [{"dataFilter": {"a1Range": "Sheet1!A1"}, "values": [["=1+1"]]}]},
    "sheets_spreadsheets_values_batch_clear_by_data_filter": {"dataFilters": [{"a1Range": "Sheet1!A1:B2"}]},
    "sheets_spreadsheets_sheets_copy_to": {"destinationSpreadsheetId": "dest-1"},
    "slides_presentations_batch_update": {"requests": [{"createSlide": {"objectId": "slide_1", "slideLayoutReference": {"predefinedLayout": "TITLE_AND_BODY"}}}], "writeControl": {"requiredRevisionId": "rev-1"}},
    "slides_presentations_create": {"title": "Throwaway"},
}


class RecordingHttp:
    """Replay Google-shaped HTTP responses and capture the client's real requests."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **kwargs: Any) -> tuple[httplib2.Response, bytes]:
        parts = urlsplit(uri)
        self.requests.append({"method": method, "path": parts.path, "query": parse_qs(parts.query), "body": body, "headers": headers})
        status, data = self.responses.pop(0)
        raw = data if isinstance(data, bytes) else json.dumps(data).encode() if data is not None else b""
        content_type = "application/octet-stream" if isinstance(data, bytes) else "application/json"
        return httplib2.Response({"status": str(status), "etag": '"response-etag"', "content-type": content_type}), raw


def client(product: str, responses: list[tuple[int, Any]]) -> tuple[Any, RecordingHttp]:
    http = RecordingHttp(responses)
    return build_from_document(json.dumps(discovery_document(product)), http=http), http


def sample(name: str, schema: dict[str, Any]) -> Any:
    """A vendor-valid value for one discovery parameter."""
    if name == "range":
        value: Any = "Sheet1!A1:B2"
    elif "enum" in schema:
        value = schema["enum"][-1]
    elif schema.get("type") == "boolean":
        value = True
    elif schema.get("type") == "integer":
        value = 7
    else:
        value = f"v-{name.strip('$.').replace('.', '-')}"
    return [value, value] if schema.get("repeated") else value


def required_params(verb: str) -> dict[str, Any]:
    return {name: sample(name, schema) for name, schema in parameter_schemas(verb).items() if schema.get("required")}


def expected_path(verb: str, params: dict[str, Any]) -> str:
    product, method = WORKSPACE_METHODS[verb]
    path = discovery_document(product)["servicePath"] + method["path"]
    for key, value in params.items():
        path = path.replace("{" + key + "}", quote(str(value), safe="")).replace("{+" + key + "}", quote(str(value), safe="/"))
    return "/" + path


def expected_query(params: dict[str, Any], schemas: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    """Each query argument as Google receives it: vendor name, string-encoded, repeated as given."""
    result: dict[str, list[str]] = {}
    for name, value in params.items():
        if schemas[name].get("location") != "path":
            values = value if isinstance(value, list) else [value]
            result[name] = [str(item).lower() if isinstance(item, bool) else str(item) for item in values]
    return result


class FakeJobs:
    """In-memory AsyncJobManager surface used by the deferred dispatch path."""

    def __init__(self) -> None:
        self.payloads: dict[str, dict[str, Any]] = {}
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        job_id = f"job-{len(self.payloads) + 1}"
        self.payloads[job_id] = kwargs["request_data"]
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}}

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, Any]:
        return {"action_status": "completed", "data": {"payload": self.payloads[job_id]}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        self.updates.append((job_id, updates))
        return {"action_status": "completed", "data": {"job_id": job_id}}


class EditorTranscriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.blobs: list[tuple[bytes, str, str]] = []
        self.io = GmailIO(attachment_loader=lambda key: OutgoingAttachment("x.txt", "text/plain", b"x"), blob_writer=self.store_blob)

    def store_blob(self, content: bytes, filename: str, mime: str) -> str:
        self.blobs.append((content, filename, mime))
        return f"blob-{len(self.blobs)}"

    def call(self, verb: str, params: dict[str, Any], *responses: tuple[int, Any], scopes: list[str] | None = None) -> tuple[dict[str, Any], RecordingHttp]:
        product, _ = WORKSPACE_METHODS[verb]
        service, http = client(product, list(responses))
        result = execute_method(service, verb, params, self.io, {"account_tier": "consumer", "granted_scopes": EDIT_SCOPES if scopes is None else scopes})
        return result, http

    def dispatching_plugin(self, emails: list[str], service_for: dict[str, Any] | None = None) -> tuple[GSuitePlugin, FakeJobs]:
        plugin = GSuitePlugin()
        connect_accounts(plugin, emails)
        jobs = FakeJobs()
        plugin._deferred.manager = jobs  # noqa: SLF001
        if service_for is not None:
            plugin._require_factory = lambda: SimpleNamespace(workspace_api=lambda product: service_for[product], credential_facts=lambda: {"granted_scopes": EDIT_SCOPES})  # type: ignore[method-assign]  # noqa: SLF001
        return plugin, jobs

    def run_job(self, plugin: GSuitePlugin, jobs: FakeJobs, job_id: str) -> dict[str, Any]:
        plugin._process_job({"id": job_id})  # noqa: SLF001
        return jobs.updates[-1][1]

    def test_every_method_registered_and_dispatched_to_completion(self) -> None:
        verbs = editor_verbs()
        counts = {product: sum(WORKSPACE_METHODS[verb][0] == product for verb in verbs) for product in EDITOR_PRODUCTS}
        self.assertEqual(counts, {"docs": 3, "sheets": 17, "slides": 5})
        for product in EDITOR_PRODUCTS:
            self.assertEqual(discovery_document(product)["revision"], "20261106")
        plugin = GSuitePlugin()
        definitions = plugin.get_edge_process_definitions()
        producers = plugin._deferred_producers()  # noqa: SLF001
        for verb in verbs:
            product, method = WORKSPACE_METHODS[verb]
            with self.subTest(verb=verb):
                self.assertIn(verb, definitions)
                self.assertIn(verb, producers)
                self.assertTrue(getattr(type(plugin), verb)._platform_process_metadata)
                declared = process_parameters(verb)
                self.assertIn("account", declared)
                self.assertLessEqual(API_WIDE, declared.keys())
                self.assertFalse(set(CREDENTIAL_PARAMETERS) & declared.keys())
                self.assertEqual("body" in declared, "request" in method)
                kb = json.loads((ROOT / f"plugins/g_suite_plugin/knowledge_base/processes/{verb}.json").read_text())
                self.assertEqual(kb["process_key"], f"plugin::g_suite_plugin::{verb}")
                self.assertIn(method["id"], kb["description"])
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400)
                self.assertEqual(kb["result_processor_customizations"]["result_type"], f"g_suite_{verb}_result")
                params = required_params(verb)
                if "request" in method:
                    params["body"] = BODIES[verb]
                reply = {"id": "object-1", "revisionId": "rev-2"}
                service, http = client(product, [(200, reply)])
                dispatching, jobs = self.dispatching_plugin(["ann@gmail.com"], {product: service})
                queued = getattr(dispatching, verb)(params, STATE)
                self.assertEqual(queued["data"]["status"], "queued", queued)
                self.assertEqual(http.requests, [])
                done = self.run_job(dispatching, jobs, queued["data"]["job_id"])
                self.assertEqual(done["status"], "completed", done)
                self.assertEqual(done["result"]["response"], reply)
                self.assertEqual(done["result"]["account"], "ann@gmail.com")
                self.assertEqual(done["result"]["method"], method["id"])
                request = http.requests[0]
                self.assertEqual(request["method"], method["httpMethod"])
                self.assertEqual(request["path"], expected_path(verb, params))
                if "body" in params:
                    self.assertEqual(json.loads(request["body"]), params["body"])

    def test_every_documented_and_api_wide_parameter_reaches_google(self) -> None:
        for verb in editor_verbs():
            with self.subTest(verb=verb):
                schemas = parameter_schemas(verb)
                params = {name: sample(name, schema) for name, schema in schemas.items() if name not in {"alt", "callback"}}
                params["alt"] = "json"
                check_editor_call(verb, params)
                result, http = self.call(verb, params, (200, {"ok": True}))
                self.assertEqual(result["response"], {"ok": True})
                request = http.requests[0]
                self.assertEqual(request["path"], expected_path(verb, {k: v for k, v in params.items() if schemas[k].get("location") == "path"}))
                expected = expected_query(params, schemas)
                self.assertEqual({name: request["query"].get(name) for name in expected}, expected)

    def test_raw_alt_and_callback_replies(self) -> None:
        cases = [({"alt": "proto"}, b"\x0a\x03doc", "application/x-protobuf", "proto"), ({"alt": "media"}, b"raw", "application/octet-stream", "media"), ({"callback": "handle"}, b'// API callback\nhandle({"documentId": "d"});', "text/javascript", "json")]
        for extra, reply, mime, alt in cases:
            with self.subTest(extra=extra):
                result, http = self.call("docs_documents_get", {"documentId": "d", **extra}, (200, reply))
                self.assertEqual(http.requests[0]["query"]["alt"], [alt])
                self.assertEqual(result["mime_type"], mime)
                self.assertEqual(self.blobs[-1], (reply, "docs.documents.get", mime))
                self.assertEqual(result["size"], len(reply))
                self.assertEqual(http.requests[0]["query"].get("callback"), [extra["callback"]] if "callback" in extra else None)

    def test_contract_helpers(self) -> None:
        self.assertIn(EditorsAPIProcesses, GSuitePlugin.__mro__)
        self.assertEqual(set(RAW_ALT_PRODUCTS), set(EDITOR_PRODUCTS))
        self.assertEqual(WORKSPACE_PRODUCTS[:3], ("drive", "people", "tasks"))
        self.assertLessEqual(set(EDITOR_PRODUCTS), set(WORKSPACE_PRODUCTS))
        refuse_credential_parameters("docs_documents_get", {"documentId": "d", "fields": "title"})
        with self.assertRaisesRegex(ValueError, "key cannot be passed"):
            refuse_credential_parameters("docs_documents_get", {"key": "x"})
        validate_body("docs_documents_get", None)
        validate_body("docs_documents_create", None)
        with self.assertRaisesRegex(ValueError, "takes no request body"):
            validate_body("docs_documents_get", {})
        validate_value(2**32 - 1, {"type": "integer", "format": "uint32"}, {}, "x")
        with self.assertRaisesRegex(ValueError, "x is outside the uint32 range"):
            validate_value(-1, {"type": "integer", "format": "uint32"}, {}, "x")
        validate_value({"free": ["form", 1]}, {"type": "object"}, {}, "x")
        _, docs_get = WORKSPACE_METHODS["docs_documents_get"]
        _, drive_get = WORKSPACE_METHODS["drive_files_get"]
        self.assertEqual(raw_reply_mime(docs_get, {"alt": "proto"}), "application/x-protobuf")
        self.assertEqual(raw_reply_mime(docs_get, {"alt": "media"}), "application/octet-stream")
        self.assertEqual(raw_reply_mime(docs_get, {"callback": "c"}), "text/javascript")
        self.assertIsNone(raw_reply_mime(docs_get, {"alt": "json"}))
        self.assertIsNone(raw_reply_mime(drive_get, {"alt": "proto"}))

    def test_credential_parameters_refused_without_echo_at_any_length(self) -> None:
        for verb in editor_verbs():
            for name in CREDENTIAL_PARAMETERS:
                messages: set[str] = set()
                for secret in ("k", "s3", "pw1", "short-pw", "ya29.a0-long-access-token-value-0123456789"):
                    with self.subTest(verb=verb, name=name, secret=secret):
                        plugin, jobs = self.dispatching_plugin(["ann@gmail.com"])
                        result = getattr(plugin, verb)({**required_params(verb), name: secret}, STATE)
                        self.assertEqual(result["error"]["code"], "gsuite.invalid_params")
                        message = result["error"]["message"]
                        self.assertIn(f"{name} cannot be passed", message)
                        self.assertIn("connect_account", message)
                        self.assertEqual(jobs.payloads, {})
                        messages.add(message)
                        if len(secret) >= 3:
                            serialized = json.dumps(result)
                            for variant in (secret, quote(secret, safe=""), quote_plus(secret), base64.b64encode(secret.encode()).decode(), base64.urlsafe_b64encode(secret.encode()).decode().rstrip("=")):
                                self.assertNotIn(variant, serialized)
                # One message for every value: nothing of the supplied value, at any length, reaches the reply.
                self.assertEqual(len(messages), 1)

    def test_request_bodies_checked_against_the_discovery_schema(self) -> None:
        refused = [
            ("docs_documents_batch_update", {"documentId": "d", "body": {"requests": [{"insertText": {"text": "x"}, "deleteContentRange": {"range": {"startIndex": 1, "endIndex": 2}}}]}}, "exactly one request kind"),
            ("docs_documents_batch_update", {"documentId": "d", "body": {"requests": [{}]}}, "exactly one request kind"),
            ("docs_documents_batch_update", {"documentId": "d", "body": {"requests": [{"insertText": {"location": {"index": "1"}, "text": "x"}}]}}, "body.requests[0].insertText.location.index must be an integer"),
            ("docs_documents_batch_update", {"documentId": "d", "body": {"requests": [{"insertText": {"location": {"index": 2**31}, "text": "x"}}]}}, "int32 range"),
            ("docs_documents_batch_update", {"documentId": "d", "body": {"requests": [{"inventedRequest": {}}]}}, "inventedRequest is not a field of Request"),
            ("docs_documents_batch_update", {"documentId": "d", "body": {"requests": {"insertText": {}}}}, "body.requests must be an array"),
            ("sheets_spreadsheets_values_update", {"spreadsheetId": "s", "range": "A1", "body": {"majorDimension": "DIAGONAL"}}, "must be one of"),
            ("sheets_spreadsheets_values_update", {"spreadsheetId": "s", "range": "A1", "body": {"values": "a,b"}}, "body.values must be an array"),
            ("sheets_spreadsheets_batch_update", {"spreadsheetId": "s", "body": {"requests": [{"addSheet": {"properties": {"hidden": "yes"}}}]}}, "hidden must be true or false"),
            ("sheets_spreadsheets_create", {"body": {"properties": {"title": 5}}}, "title must be a string"),
            ("slides_presentations_batch_update", {"presentationId": "p", "body": {"requests": [{"createSlide": {"insertionIndex": 1.5}}]}}, "insertionIndex must be an integer"),
            ("slides_presentations_create", {"body": ["title"]}, "body must be a JSON object"),
            ("slides_presentations_get", {"presentationId": "p", "body": {"title": "x"}}, "takes no request body"),
            ("slides_presentations_get", {"presentationId": "a/b"}, "vendor pattern"),
            ("sheets_spreadsheets_developer_metadata_get", {"spreadsheetId": "s", "metadataId": "7"}, "metadataId must be integer"),
            ("sheets_spreadsheets_values_get", {"spreadsheetId": "s", "range": "A1", "valueRenderOption": "RAW"}, "must be one of"),
            ("docs_documents_get", {"documentId": "d", "$.xgafv": "3"}, "must be one of"),
        ]
        for verb, params, fragment in refused:
            with self.subTest(verb=verb, fragment=fragment):
                plugin, jobs = self.dispatching_plugin(["ann@gmail.com"])
                result = getattr(plugin, verb)(params, STATE)
                self.assertEqual(result["error"]["code"], "gsuite.invalid_params")
                self.assertIn(fragment, result["error"]["message"])
                self.assertEqual(jobs.payloads, {})
        # JSON null clears an optional field; unchanged bodies pass through verbatim.
        check_editor_call("sheets_spreadsheets_batch_update", {"spreadsheetId": "s", "body": {"requests": [{"updateSheetProperties": {"properties": {"sheetId": 0, "tabColor": None}, "fields": "tabColor"}}]}})
        check_editor_call("sheets_spreadsheets_values_update", {"spreadsheetId": "s", "range": "A1", "body": {"values": [[None, {"nested": "any"}]]}})

    def test_document_lifecycle_create_correct_reverse_and_stale_write(self) -> None:
        created, http = self.call("docs_documents_create", {"body": {"title": "Throwaway"}}, (200, {"documentId": "doc-1", "title": "Throwaway", "revisionId": "rev-1"}))
        self.assertEqual(created["response"]["documentId"], "doc-1")
        self.assertEqual(http.requests[0]["path"], "/v1/documents")
        steps = [
            {"requests": [{"insertText": {"location": {"index": 1}, "text": "Helo world"}}], "writeControl": {"requiredRevisionId": "rev-1"}},
            {"requests": [{"replaceAllText": {"containsText": {"text": "Helo", "matchCase": True}, "replaceText": "Hello"}}], "writeControl": {"targetRevisionId": "rev-2"}},
            {"requests": [{"deleteContentRange": {"range": {"startIndex": 1, "endIndex": 12}}}]},
        ]
        for revision, body in enumerate(steps, start=2):
            check_editor_call("docs_documents_batch_update", {"documentId": "doc-1", "body": body})
            reply = {"documentId": "doc-1", "replies": [{}], "writeControl": {"requiredRevisionId": f"rev-{revision}"}}
            result, http = self.call("docs_documents_batch_update", {"documentId": "doc-1", "body": body}, (200, reply))
            self.assertEqual(result["response"], reply)
            self.assertEqual(json.loads(http.requests[0]["body"]), body)
        stale = {"error": {"code": 400, "message": "The required revision ID 'rev-1' does not match the latest revision.", "status": "FAILED_PRECONDITION"}}
        plugin, _ = self.dispatching_plugin(["ann@gmail.com"])
        service, http = client("docs", [(400, stale)])
        plugin._require_factory = lambda: SimpleNamespace(workspace_api=lambda product: service, credential_facts=lambda: {"granted_scopes": EDIT_SCOPES})  # type: ignore[method-assign]  # noqa: SLF001
        verb = "docs_documents_batch_update"
        result = plugin._run(lambda: plugin._deferred_producers()[verb]({"documentId": "doc-1", "body": steps[0]}), verb, {})  # noqa: SLF001
        self.assertEqual(result["error"]["code"], "gsuite.invalid_params")
        self.assertIn("does not match the latest revision", result["error"]["message"])
        self.assertEqual(len(http.requests), 1)
        result, http = self.call("docs_documents_get", {"documentId": "doc-1", "includeTabsContent": True, "suggestionsViewMode": "PREVIEW_WITHOUT_SUGGESTIONS"}, (200, {"documentId": "doc-1", "revisionId": "rev-4", "tabs": []}))
        self.assertEqual(result["response"]["revisionId"], "rev-4")
        self.assertEqual(http.requests[0]["query"]["includeTabsContent"], ["true"])
        result, http = self.call("drive_files_delete", {"fileId": "doc-1"}, (204, None), scopes=[])
        self.assertEqual(http.requests[0]["method"], "DELETE")

    def test_spreadsheet_lifecycle_values_metadata_sheets_and_terminal(self) -> None:
        flow: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            ("sheets_spreadsheets_create", {"body": BODIES["sheets_spreadsheets_create"]}, {"spreadsheetId": "ss-1", "sheets": [{"properties": {"sheetId": 0, "title": "Data"}}]}),
            ("sheets_spreadsheets_values_update", {"spreadsheetId": "ss-1", "range": "Data!A1:B2", "valueInputOption": "RAW", "body": {"values": [["name", "qty"], ["pen", 1]]}}, {"updatedCells": 4}),
            ("sheets_spreadsheets_values_update", {"spreadsheetId": "ss-1", "range": "Data!B2", "valueInputOption": "USER_ENTERED", "includeValuesInResponse": True, "body": {"values": [[2]]}}, {"updatedCells": 1, "updatedData": {"values": [["2"]]}}),
            ("sheets_spreadsheets_values_append", {"spreadsheetId": "ss-1", "range": "Data!A1", "valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS", "body": {"values": [["ink", 3]]}}, {"updates": {"updatedRange": "Data!A3:B3"}}),
            ("sheets_spreadsheets_values_batch_get", {"spreadsheetId": "ss-1", "ranges": ["Data!A1:B3", "Data!A1"]}, {"valueRanges": [{"range": "Data!A1:B3"}, {"range": "Data!A1"}]}),
            (
                "sheets_spreadsheets_batch_update",
                {"spreadsheetId": "ss-1", "body": {"requests": [{"createDeveloperMetadata": {"developerMetadata": {"metadataKey": "owner", "metadataValue": "ann", "location": {"spreadsheet": True}, "visibility": "DOCUMENT"}}}]}},
                {"replies": [{"createDeveloperMetadata": {"developerMetadata": {"metadataId": 42}}}]},
            ),
            ("sheets_spreadsheets_developer_metadata_get", {"spreadsheetId": "ss-1", "metadataId": 42}, {"metadataId": 42, "metadataKey": "owner"}),
            ("sheets_spreadsheets_batch_update", {"spreadsheetId": "ss-1", "body": {"requests": [{"updateDeveloperMetadata": {"dataFilters": [{"developerMetadataLookup": {"metadataId": 42}}], "developerMetadata": {"metadataValue": "bob"}, "fields": "metadataValue"}}]}}, {"replies": [{}]}),
            ("sheets_spreadsheets_developer_metadata_search", {"spreadsheetId": "ss-1", "body": BODIES["sheets_spreadsheets_developer_metadata_search"]}, {"matchedDeveloperMetadata": [{"developerMetadata": {"metadataId": 42, "metadataValue": "bob"}}]}),
            ("sheets_spreadsheets_batch_update", {"spreadsheetId": "ss-1", "body": {"requests": [{"deleteDeveloperMetadata": {"dataFilter": {"developerMetadataLookup": {"metadataId": 42}}}}]}}, {"replies": [{"deleteDeveloperMetadata": {}}]}),
            ("sheets_spreadsheets_sheets_copy_to", {"spreadsheetId": "ss-1", "sheetId": 0, "body": {"destinationSpreadsheetId": "ss-1"}}, {"sheetId": 9, "title": "Copy of Data"}),
            ("sheets_spreadsheets_batch_update", {"spreadsheetId": "ss-1", "body": {"requests": [{"deleteSheet": {"sheetId": 9}}]}}, {"replies": [{}]}),
            ("sheets_spreadsheets_values_clear", {"spreadsheetId": "ss-1", "range": "Data!A3:B3", "body": {}}, {"clearedRange": "Data!A3:B3"}),
            ("sheets_spreadsheets_values_batch_clear_by_data_filter", {"spreadsheetId": "ss-1", "body": BODIES["sheets_spreadsheets_values_batch_clear_by_data_filter"]}, {"clearedRanges": ["Sheet1!A1:B2"]}),
            ("sheets_spreadsheets_get", {"spreadsheetId": "ss-1", "ranges": ["Data!A1:B2"], "includeGridData": True, "excludeTablesInBandedRanges": True}, {"spreadsheetId": "ss-1", "sheets": [{"data": []}]}),
        ]
        for verb, params, reply in flow:
            with self.subTest(verb=verb, params=params):
                check_editor_call(verb, params)
                result, http = self.call(verb, params, (200, reply))
                self.assertEqual(result["response"], reply)
                if "body" in params:
                    self.assertEqual(json.loads(http.requests[0]["body"]), params["body"])
                if verb == "sheets_spreadsheets_get":
                    self.assertEqual(http.requests[0]["query"]["ranges"], ["Data!A1:B2"])
        _, http = self.call("drive_files_delete", {"fileId": "ss-1"}, (204, None), scopes=[])
        self.assertEqual((http.requests[0]["method"], http.requests[0]["path"]), ("DELETE", "/drive/v3/files/ss-1"))

    def test_presentation_lifecycle_pages_thumbnail_and_reversal(self) -> None:
        flow: list[tuple[str, dict[str, Any], dict[str, Any]]] = [
            ("slides_presentations_create", {"body": {"title": "Throwaway"}}, {"presentationId": "pr-1", "revisionId": "r1", "slides": [{"objectId": "p"}]}),
            ("slides_presentations_batch_update", {"presentationId": "pr-1", "body": BODIES["slides_presentations_batch_update"]}, {"presentationId": "pr-1", "replies": [{"createSlide": {"objectId": "slide_1"}}]}),
            (
                "slides_presentations_batch_update",
                {
                    "presentationId": "pr-1",
                    "body": {"requests": [{"insertText": {"objectId": "title_1", "text": "Corrected", "insertionIndex": 0}}, {"updatePageProperties": {"objectId": "slide_1", "pageProperties": {"pageBackgroundFill": {"solidFill": {"color": {"rgbColor": {"red": 1.0}}, "alpha": 0.5}}}, "fields": "pageBackgroundFill"}}]},
                },
                {"replies": [{}, {}]},
            ),
            ("slides_presentations_pages_get", {"presentationId": "pr-1", "pageObjectId": "slide_1"}, {"objectId": "slide_1", "pageType": "SLIDE"}),
            ("slides_presentations_pages_get_thumbnail", {"presentationId": "pr-1", "pageObjectId": "slide_1", "thumbnailProperties.mimeType": "PNG", "thumbnailProperties.thumbnailSize": "SMALL"}, {"contentUrl": "https://lh3.example/thumb", "width": 200, "height": 112}),
            ("slides_presentations_batch_update", {"presentationId": "pr-1", "body": {"requests": [{"deleteObject": {"objectId": "slide_1"}}], "writeControl": {"requiredRevisionId": "r3"}}}, {"replies": [{}]}),
            ("slides_presentations_get", {"presentationId": "pr-1"}, {"presentationId": "pr-1", "slides": [{"objectId": "p"}]}),
        ]
        for verb, params, reply in flow:
            with self.subTest(verb=verb):
                check_editor_call(verb, params)
                result, http = self.call(verb, params, (200, reply))
                self.assertEqual(result["response"], reply)
                if verb == "slides_presentations_get":
                    self.assertEqual(http.requests[0]["path"], "/v1/presentations/pr-1")
        _, http = self.call("slides_presentations_pages_get_thumbnail", {"presentationId": "pr-1", "pageObjectId": "s", "thumbnailProperties.thumbnailSize": "LARGE"}, (200, {"contentUrl": "u"}))
        self.assertEqual(http.requests[0]["query"]["thumbnailProperties.thumbnailSize"], ["LARGE"])

    def test_account_contract_scopes_and_error_mapping(self) -> None:
        verb = "sheets_spreadsheets_values_get"
        params = {"spreadsheetId": "ss-1", "range": "A1"}
        service, http = client("sheets", [(200, {"range": "A1", "values": [["1"]]})])
        plugin, jobs = self.dispatching_plugin(["ann@gmail.com", "bob@corp.example"], {"sheets": service})
        queued = plugin.sheets_spreadsheets_values_get({**params, "account": "bob@corp.example"}, STATE)
        done = self.run_job(plugin, jobs, queued["data"]["job_id"])
        self.assertEqual(done["result"]["account"], "bob@corp.example")
        unknown = plugin.sheets_spreadsheets_values_get({**params, "account": "carol@nowhere.example"}, STATE)
        self.assertEqual(unknown["error"]["code"], "gsuite.unknown_account")
        self.assertIn("ann@gmail.com", unknown["error"]["message"])
        self.assertEqual(len(jobs.payloads), 1)
        # Real factory: the stored grant (gmail.modify only) lacks every Sheets scope.
        plugin, jobs = self.dispatching_plugin(["ann@gmail.com"])
        queued = plugin.sheets_spreadsheets_values_get(params, STATE)
        failed = self.run_job(plugin, jobs, queued["data"]["job_id"])
        self.assertEqual(failed["error"]["code"], "gsuite.missing_scope")
        self.assertIn("https://www.googleapis.com/auth/spreadsheets", failed["error"]["message"])
        self.assertIn("connect_account", failed["error"]["message"])
        # A consumer account (ann@gmail.com) is served: editor APIs exist on consumer tiers.
        result, _ = self.call(verb, params, (200, {"range": "A1"}))
        self.assertEqual(result["response"], {"range": "A1"})
        plugin = GSuitePlugin()
        connect_accounts(plugin, ["ann@gmail.com"])
        mapping = [(401, "UNAUTHENTICATED", "gsuite.auth_expired"), (403, "PERMISSION_DENIED", "gsuite.permission_denied"), (404, "NOT_FOUND", "gsuite.not_found"), (429, "RESOURCE_EXHAUSTED", "gsuite.rate_limited"), (400, "INVALID_ARGUMENT", "gsuite.invalid_params")]
        for status, vendor_status, code in mapping:
            with self.subTest(status=status):
                error = {"error": {"code": status, "message": "denied", "status": vendor_status}}
                service, http = client("sheets", [(status, error)])
                plugin._require_factory = lambda service=service: SimpleNamespace(workspace_api=lambda product: service, credential_facts=lambda: {"granted_scopes": EDIT_SCOPES})  # type: ignore[method-assign]  # noqa: SLF001
                result = plugin._run(lambda: plugin._deferred_producers()[verb](params), verb, {})  # noqa: SLF001
                self.assertEqual(result["error"]["code"], code)
                self.assertEqual(len(http.requests), 1)
        service, _ = client("sheets", [(200, {"range": "A1", "values": [["vendor text"]]})])
        plugin._require_factory = lambda: SimpleNamespace(workspace_api=lambda product: service, credential_facts=lambda: {"granted_scopes": EDIT_SCOPES})  # type: ignore[method-assign]  # noqa: SLF001
        result = plugin._run(lambda: plugin._deferred_producers()[verb](params), verb, {})  # noqa: SLF001
        self.assertEqual(result["data"]["account"], "ann@gmail.com")
        self.assertEqual(result["data"]["response"]["values"], [["vendor text"]])


if __name__ == "__main__":
    unittest.main()
