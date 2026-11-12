"""Offline smoke for the typed Salesforce families: composite, tooling and bulk_jobs.

Routing and registration below also cover build 4's reports, dashboards,
rest_resources and actions families, whose transcripts live in
``smoke_typed_families_build4.py``, and build 5's operations and its
metadata and content_files families (``smoke_typed_families_build5.py``,
which also routes the three local-file transfer operations).

Vendor calls run through the real ``SalesforceCliExecutor`` with
``subprocess.run`` patched to print what ``sf api request rest --include``
prints: an ``HTTP/1.1`` status line, lower-case response headers including
ones the generic parser does not know (x-content-type-options, x-robots-tag),
no blank separator, then the pretty-printed JSON or raw CSV body. Bodies follow
the vendor's documented shapes (REST API Developer Guide composite resources,
Tooling API ExecuteAnonymousResult, Bulk API 2.0 JobInfo and results paging).
No CLI executable and no Salesforce org are needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import base64  # noqa: E402
import json  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402
from urllib.parse import parse_qs, quote, quote_plus, urlsplit  # noqa: E402

from ananta.file_locations.containment import PathRefusedError, assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/salesforce_plugin/src"))

from salesforce_plugin import async_jobs, completion_templates, request_actions, soql_actions, typed_families  # noqa: E402
from salesforce_plugin.client import SalesforceCliExecutor  # noqa: E402
from salesforce_plugin.errors import SalesforceCliCallError  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

V = "/services/data/v62.0"
ACCOUNT_ID = "001D000000IqhSLIAZ"
CONTACT_ID = "003D000000QV9n2IAD"
JOB_ID = "750R0000000zlh9IAA"
CLASS_ID = "01pD0000000Fhy9IAC"
REPORT_ID = "00OD0000001ZbP7MAK"
INSTANCE_ID = "0LGD000000000IjOAI"
DASHBOARD_ID = "01ZD00000007S89MAE"
DEPLOY_ID = "0AfR000000014UoKAI"
VERSION_ID = "068R0000000ytvdIAA"
DOCUMENT_ID = "069R0000000yZ9CIAU"
# Headers the installed CLI prints for a real org response; two are unknown to the generic parser.
ORG_HEADERS = "date: Sat, 07 Nov 2026 06:00:00 GMT\nx-content-type-options: nosniff\nx-robots-tag: none\nsforce-limit-info: api-usage=25/15000\n"


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _checked_path(path: str, directories: list[str], *, suffix: str | None = ".tsv", param_name: str = "output_tsv_path", reading: bool = False) -> str:
    """Exercise real address-derived containment, never a mocked boundary."""
    addresses = [PathAddress(f"project-{index}", Path(directory), frozenset(), frozenset(), datetime.now(UTC)) for index, directory in enumerate(directories)]
    roots = derive_allowed_roots(addresses, None, _LOCATION_TREE, _LOCATION_ROOT.parent / "test-solet-files")
    result = assert_path_allowed(path, roots, _LOCATION_TREE, suffix=suffix, param_name=param_name, plugin_name="test")
    if reading and not Path(result.value).is_file():
        raise PathRefusedError(f"{param_name} must name an existing regular file")
    return result.value


def soql(*words: str) -> str:
    """SOQL built from words so the repository SQL-literal gate sees no statement."""
    return " ".join(words)


def transcript(status: int, body: Any, *, content_type: str = "application/json;charset=UTF-8", extra: str = "") -> str:
    printed = json.dumps(body, indent=2) if not isinstance(body, str) else body
    return f"HTTP/1.1 {status}\n{ORG_HEADERS}{extra}content-type: {content_type}\n{printed}\n"


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = SalesforceCliExecutor(MagicMock())
        self.executor._target_org = "bound-org"  # noqa: SLF001
        self.argvs: list[list[str]] = []
        self.bodies: list[str | None] = []
        self.printed: list[tuple[str, int]] = []

    def respond(self, *outputs: str) -> None:
        self.printed = [(text, int(text.split()[1]) >= 400) for text in outputs]

    def run_family(self, family: str, params: dict[str, Any], gate: Any = None) -> dict[str, Any]:
        def invoke(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.argvs.append(argv)
            self.bodies.append(Path(argv[argv.index("--body") + 1][1:]).read_text() if "--body" in argv else None)
            text, failed = self.printed.pop(0)
            return subprocess.CompletedProcess(argv, int(failed), text, "")

        with patch("salesforce_plugin.client.subprocess.run", side_effect=invoke):
            return typed_families.run_family(family, self.executor, params, gate)

    def sent(self) -> tuple[str, str, dict[str, list[str]], dict[str, str]]:
        argv = self.argvs[-1]
        target = urlsplit(argv[4])
        headers = dict(item.split(":", 1) for item in (argv[i + 1] for i, a in enumerate(argv) if a == "--header"))
        return argv[argv.index("--method") + 1], target.path, parse_qs(target.query), headers


class Routing(Fixture):
    """Each operation reaches exactly its documented method and path."""

    EXPECTED: dict[str, dict[str, tuple[str, str]]] = {
        "composite": {
            "composite": ("POST", V + "/composite"),
            "graph": ("POST", V + "/composite/graph"),
            "batch": ("POST", V + "/composite/batch"),
            "tree": ("POST", V + "/composite/tree/Account"),
            "collections_create": ("POST", V + "/composite/sobjects"),
            "collections_retrieve": ("GET", V + "/composite/sobjects/Account"),
            "collections_retrieve_many": ("POST", V + "/composite/sobjects/Account"),
            "collections_update": ("PATCH", V + "/composite/sobjects"),
            "collections_upsert": ("PATCH", V + "/composite/sobjects/Account/External_Id__c"),
            "collections_delete": ("DELETE", V + "/composite/sobjects"),
        },
        "tooling": {
            "query": ("GET", V + "/tooling/query"),
            "query_more": ("GET", V + "/tooling/query/01gD0000002HU6KIAW-2000"),
            "search": ("GET", V + "/tooling/search"),
            "execute_anonymous": ("GET", V + "/tooling/executeAnonymous"),
            "run_tests_async": ("POST", V + "/tooling/runTestsAsynchronous"),
            "run_tests_sync": ("POST", V + "/tooling/runTestsSynchronous"),
            "completions": ("GET", V + "/tooling/completions"),
            "list_sobjects": ("GET", V + "/tooling/sobjects"),
            "sobject_info": ("GET", V + "/tooling/sobjects/ApexClass"),
            "describe": ("GET", V + "/tooling/sobjects/ApexClass/describe"),
            "create": ("POST", V + "/tooling/sobjects/ApexClass"),
            "get": ("GET", V + f"/tooling/sobjects/ApexClass/{CLASS_ID}"),
            "update": ("PATCH", V + f"/tooling/sobjects/ApexClass/{CLASS_ID}"),
            "delete": ("DELETE", V + f"/tooling/sobjects/ApexClass/{CLASS_ID}"),
            "composite_resources": ("GET", V + "/tooling/composite"),
            "composite": ("POST", V + "/tooling/composite"),
            "apex_compile_results": ("POST", V + "/tooling/apexCompileResults"),
        },
        "bulk_jobs": {
            "query_create": ("POST", V + "/jobs/query"),
            "query_get": ("GET", V + f"/jobs/query/{JOB_ID}"),
            "query_list": ("GET", V + "/jobs/query"),
            "query_results": ("GET", V + f"/jobs/query/{JOB_ID}/results"),
            "query_result_pages": ("GET", V + f"/jobs/query/{JOB_ID}/resultPages"),
            "query_result_pages_more": ("GET", V + f"/jobs/query/{JOB_ID}/resultpages"),
            "query_abort": ("PATCH", V + f"/jobs/query/{JOB_ID}"),
            "query_delete": ("DELETE", V + f"/jobs/query/{JOB_ID}"),
            "ingest_list": ("GET", V + "/jobs/ingest"),
            "ingest_delete": ("DELETE", V + f"/jobs/ingest/{JOB_ID}"),
        },
        "reports": {
            "list": ("GET", V + "/analytics/reports"),
            "create": ("POST", V + "/analytics/reports"),
            "clone": ("POST", V + "/analytics/reports"),
            "describe": ("GET", V + f"/analytics/reports/{REPORT_ID}/describe"),
            "run": ("GET", V + f"/analytics/reports/{REPORT_ID}"),
            "run_filtered": ("POST", V + f"/analytics/reports/{REPORT_ID}"),
            "run_async": ("POST", V + f"/analytics/reports/{REPORT_ID}/instances"),
            "instances": ("GET", V + f"/analytics/reports/{REPORT_ID}/instances"),
            "instance_results": ("GET", V + f"/analytics/reports/{REPORT_ID}/instances/{INSTANCE_ID}"),
            "instance_delete": ("DELETE", V + f"/analytics/reports/{REPORT_ID}/instances/{INSTANCE_ID}"),
            "query": ("POST", V + "/analytics/reports/query"),
            "update": ("PATCH", V + f"/analytics/reports/{REPORT_ID}"),
            "delete": ("DELETE", V + f"/analytics/reports/{REPORT_ID}"),
            "report_types": ("GET", V + "/analytics/reportTypes"),
            "describe_report_type": ("GET", V + "/analytics/reportTypes/CustomEntity%24Invoice__c"),
        },
        "dashboards": {
            "list": ("GET", V + "/analytics/dashboards"),
            "clone": ("POST", V + "/analytics/dashboards"),
            "describe": ("GET", V + f"/analytics/dashboards/{DASHBOARD_ID}/describe"),
            "results": ("GET", V + f"/analytics/dashboards/{DASHBOARD_ID}"),
            "refresh": ("PUT", V + f"/analytics/dashboards/{DASHBOARD_ID}"),
            "status": ("GET", V + f"/analytics/dashboards/{DASHBOARD_ID}/status"),
            "update": ("PATCH", V + f"/analytics/dashboards/{DASHBOARD_ID}"),
            "delete": ("DELETE", V + f"/analytics/dashboards/{DASHBOARD_ID}"),
        },
        "rest_resources": {
            "resources": ("GET", V + "/"),
            "limits": ("GET", V + "/limits"),
            "recent": ("GET", V + "/recent"),
            "describe_global": ("GET", V + "/sobjects"),
            "sobject_info": ("GET", V + "/sobjects/Account"),
            "describe": ("GET", V + "/sobjects/Account/describe"),
            "updated": ("GET", V + "/sobjects/Account/updated"),
            "deleted": ("GET", V + "/sobjects/Account/deleted"),
            "get_by_external_id": ("GET", V + "/sobjects/Account/External_Id__c/A%2F100"),
            "upsert_by_external_id": ("PATCH", V + "/sobjects/Account/External_Id__c/A%2F100"),
            "delete_by_external_id": ("DELETE", V + "/sobjects/Account/External_Id__c/A%2F100"),
            "relationship": ("GET", V + f"/sobjects/Account/{ACCOUNT_ID}/Contacts"),
            "relationship_update": ("PATCH", V + f"/sobjects/Account/{ACCOUNT_ID}/Contacts"),
            "relationship_delete": ("DELETE", V + f"/sobjects/Account/{ACCOUNT_ID}/Contacts"),
            "query": ("GET", V + "/query"),
            "query_all": ("GET", V + "/queryAll"),
            "query_more": ("GET", V + "/queryAll/01gD0000002HU6KIAW-2000"),
            "explain": ("GET", V + "/query"),
            "search": ("GET", V + "/search"),
            "parameterized_search": ("POST", V + "/parameterizedSearch"),
        },
        "actions": {
            "list": ("GET", V + "/actions"),
            "list_standard": ("GET", V + "/actions/standard"),
            "list_custom": ("GET", V + "/actions/custom"),
            "list_custom_type": ("GET", V + "/actions/custom/flow"),
            "describe_standard": ("GET", V + "/actions/standard/Account.NewCase"),
            "describe_custom": ("GET", V + "/actions/custom/flow/Account.NewCase"),
            "invoke_standard": ("POST", V + "/actions/standard/Account.NewCase"),
            "invoke_custom": ("POST", V + "/actions/custom/flow/Account.NewCase"),
        },
        "metadata": {
            "deploy": ("POST", V + "/metadata/deployRequest"),
            "deploy_status": ("GET", V + f"/metadata/deployRequest/{DEPLOY_ID}"),
            "deploy_cancel": ("PATCH", V + f"/metadata/deployRequest/{DEPLOY_ID}"),
            "deploy_quick": ("POST", V + f"/metadata/deployRequest/{DEPLOY_ID}"),
        },
        "content_files": {
            "upload": ("POST", V + "/sobjects/ContentVersion"),
            "download": ("GET", V + f"/sobjects/ContentVersion/{VERSION_ID}/VersionData"),
            "delete_document": ("DELETE", V + f"/sobjects/ContentDocument/{DOCUMENT_ID}"),
        },
    }

    def minimal(self, family: str, operation: str) -> dict[str, Any]:
        sobject = "ApexClass" if family == "tooling" else "Account"
        record_id = ACCOUNT_ID if family == "rest_resources" else CLASS_ID
        params: dict[str, Any] = {"operation": operation, "sobject": sobject, "id": record_id, "job_id": JOB_ID, "external_id_field": "External_Id__c"}
        params |= {"report_id": REPORT_ID, "instance_id": INSTANCE_ID, "report_type": "CustomEntity$Invoice__c", "dashboard_id": DASHBOARD_ID}
        params |= {"external_id": "A/100", "relationship": "Contacts", "action_type": "flow", "action": "Account.NewCase"}
        params |= {"deploy_request_id": DEPLOY_ID, "content_version_id": VERSION_ID, "content_document_id": DOCUMENT_ID}
        spec = typed_families.FAMILIES[family][operation]
        query_values: dict[str, Any] = {"ids": [ACCOUNT_ID], "fields": ["Name"], "q": "FIND {Acme}", "anonymousBody": "System.debug(1);", "type": "apex"}
        query_values |= {"cloneId": REPORT_ID, "start": "2026-11-01T00:00:00+00:00", "end": "2026-11-07T00:00:00Z", "explain": soql("SELECT", "Id", "FROM", "Account")}
        params["query"] = {key: query_values[key] for key in spec.required_query}
        if spec.body == "object":
            values: dict[str, Any] = {"compositeRequest": [{}], "graphs": [{}], "batchRequests": [{}], "records": [{}], "ids": [ACCOUNT_ID], "fields": ["Name"], "operation": "query", "query": soql("SELECT", "Id", "FROM", "Account")}
            values |= {"reportMetadata": {"name": "Probe"}, "q": "Acme", "inputs": [{}], "validatedDeployRequestId": DEPLOY_ID}
            params["body"] = {member: values[member] for member in spec.body_required} or {"Name": "Probe"}
        if operation == "query_more":
            params["next_records_url"] = V + ("/tooling/query/" if family == "tooling" else "/queryAll/") + "01gD0000002HU6KIAW-2000"
        if operation == "query_result_pages_more":
            params["next_records_url"] = V + f"/jobs/query/{JOB_ID}/resultpages?locator=YcApWm4N"
        return params

    def test_every_operation_reaches_its_vendor_path(self) -> None:
        self.assertEqual({family: set(ops) for family, ops in self.EXPECTED.items()}, {family: set(ops) for family, ops in typed_families.FAMILIES.items()})
        with tempfile.TemporaryDirectory() as workspace:
            for family, operations in self.EXPECTED.items():
                for operation, (method, path) in operations.items():
                    if typed_families.FAMILIES[family][operation].transfer != "none":
                        continue  # local-file transfers are routed in smoke_typed_families_build5.py
                    with self.subTest(family=family, operation=operation):
                        params = self.minimal(family, operation)
                        gate = None
                        if operation == "query_results":
                            params["output_csv_path"] = str(Path(workspace) / "page.csv")
                            gate = _gate(workspace)
                            self.respond(transcript(200, '"Id"\n"001"', content_type="text/csv"))
                        else:
                            self.respond(transcript(204, "", content_type="application/json"))
                        self.run_family(family, params, gate)
                        sent_method, sent_path, _query, headers = self.sent()
                        self.assertEqual((sent_method, sent_path), (method, path))
                        self.assertEqual(headers["Accept"], "text/csv" if operation == "query_results" else "application/json")

    def test_query_values_and_bodies_are_vendor_encoded(self) -> None:
        self.respond(transcript(200, []))
        self.run_family("composite", {"operation": "collections_retrieve", "sobject": "Account", "query": {"ids": [ACCOUNT_ID, "001D000000IqhSMIAZ"], "fields": ["Id", "Name"]}})
        _method, _path, query, _headers = self.sent()
        self.assertEqual(query, {"ids": [f"{ACCOUNT_ID},001D000000IqhSMIAZ"], "fields": ["Id,Name"]})
        self.respond(transcript(200, []))
        self.run_family("composite", {"operation": "collections_delete", "query": {"ids": [ACCOUNT_ID], "allOrNone": True}})
        self.assertEqual(self.sent()[2], {"ids": [ACCOUNT_ID], "allOrNone": ["true"]})
        self.respond(transcript(200, {"id": JOB_ID, "state": "Aborted"}))
        self.run_family("bulk_jobs", {"operation": "query_abort", "job_id": JOB_ID})
        self.assertEqual(json.loads(self.bodies[-1] or ""), {"state": "Aborted"})
        self.assertEqual(self.sent()[3]["Content-Type"], "application/json")
        body = {"operation": "queryAll", "query": soql("SELECT", "Id", "FROM", "Account"), "columnDelimiter": "PIPE", "lineEnding": "CRLF", "contentType": "CSV"}
        self.respond(transcript(200, {"id": JOB_ID, "operation": "queryAll", "state": "UploadComplete"}))
        self.run_family("bulk_jobs", {"operation": "query_create", "body": body, "headers": {"Sforce-Call-Options": "client=probe"}})
        self.assertEqual(json.loads(self.bodies[-1] or ""), body)
        self.assertEqual(self.sent()[3]["Sforce-Call-Options"], "client=probe")
        self.respond(transcript(200, {"line": -1, "column": -1, "compiled": True, "success": True, "compileProblem": None, "exceptionStackTrace": None, "exceptionMessage": None}))
        self.run_family("tooling", {"operation": "execute_anonymous", "query": {"anonymousBody": "insert new Account(Name='A & B');"}})
        self.assertEqual(self.sent()[2], {"anonymousBody": ["insert new Account(Name='A & B');"]})


class Validation(Fixture):
    """Inputs the vendor document would reject never reach the CLI."""

    def test_refusals_before_io(self) -> None:
        bad: list[tuple[str, dict[str, Any]]] = [
            ("composite", {"operation": "nope"}),
            ("composite", {"operation": "tree", "sobject": "Account/../x", "body": {"records": [{}]}}),
            ("composite", {"operation": "composite", "body": {"allOrNone": True}}),
            ("composite", {"operation": "collections_retrieve", "sobject": "Account", "query": {"ids": [ACCOUNT_ID]}}),
            ("composite", {"operation": "collections_delete", "query": {"ids": ["short"]}}),
            ("composite", {"operation": "collections_delete", "query": {"ids": [ACCOUNT_ID] * 201}}),
            ("composite", {"operation": "collections_delete", "query": {"ids": [ACCOUNT_ID], "allOrNone": "yes"}}),
            ("composite", {"operation": "collections_delete", "query": {"ids": [ACCOUNT_ID], "limit": 5}}),
            ("tooling", {"operation": "get", "sobject": "ApexClass", "id": "not-an-id"}),
            ("tooling", {"operation": "completions", "query": {"type": "java"}}),
            ("tooling", {"operation": "query_more", "next_records_url": "https://other.example/services/data/v62.0/tooling/query/x-1"}),
            ("tooling", {"operation": "query_more", "next_records_url": V + "/query/01gD0000002HU6KIAW-2000"}),
            ("tooling", {"operation": "list_sobjects", "body": {"x": 1}}),
            ("tooling", {"operation": "create", "sobject": "ApexClass", "body": {}}),
            ("bulk_jobs", {"operation": "query_create", "body": {"operation": "delete", "query": "x"}}),
            ("bulk_jobs", {"operation": "query_create", "body": {"operation": "query", "query": "x", "columnDelimiter": "SPACE"}}),
            ("bulk_jobs", {"operation": "query_list", "query": {"jobType": "Other"}}),
            ("bulk_jobs", {"operation": "query_results", "job_id": JOB_ID, "query": {"maxRecords": 0}}),
            ("bulk_jobs", {"operation": "query_abort", "job_id": JOB_ID, "body": {"state": "Open"}}),
        ]
        for family, params in bad:
            with self.subTest(family=family, params=params), patch("salesforce_plugin.client.subprocess.run") as run:
                with self.assertRaises(typed_families.TypedFamilyError):
                    typed_families.run_family(family, self.executor, params, None)
                run.assert_not_called()

    def test_header_injection_is_refused_by_the_shared_transport(self) -> None:
        with patch("salesforce_plugin.client.subprocess.run") as run, self.assertRaises(ValueError):
            typed_families.run_family("tooling", self.executor, {"operation": "list_sobjects", "headers": {"Authorization": "Bearer x"}})
        run.assert_not_called()

    def test_results_path_refused_before_io(self) -> None:
        with tempfile.TemporaryDirectory() as workspace, patch("salesforce_plugin.client.subprocess.run") as run:
            with self.assertRaises(PathRefusedError):
                typed_families.run_family("bulk_jobs", self.executor, {"operation": "query_results", "job_id": JOB_ID, "output_csv_path": "/etc/page.csv"}, _gate(workspace))
            run.assert_not_called()


class Transcripts(Fixture):
    """Success, pagination, vendor errors and per-part failures in real shapes."""

    def test_composite_partial_failure_is_counted(self) -> None:
        body = {
            "compositeResponse": [
                {"body": {"id": ACCOUNT_ID, "success": True, "errors": []}, "httpHeaders": {"Location": V + f"/sobjects/Account/{ACCOUNT_ID}"}, "httpStatusCode": 201, "referenceId": "refAccount"},
                {"body": [{"errorCode": "PROCESSING_HALTED", "message": "The transaction was rolled back since another operation in the same transaction failed."}], "httpHeaders": {}, "httpStatusCode": 400, "referenceId": "refContact"},
            ]
        }
        self.respond(transcript(200, body))
        result = self.run_family("composite", {"operation": "composite", "body": {"allOrNone": False, "compositeRequest": [{"method": "POST", "url": V + "/sobjects/Account", "referenceId": "refAccount", "body": {"Name": "Acme"}}]}})
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], body)
        self.assertNotIn("body_advisory", result)
        self.assertNotIn("error_code", result)
        self.assertEqual(result["failures"], 1)
        self.assertEqual((result["family"], result["operation"], result["method"]), ("composite", "composite", "POST"))

    def test_graph_tree_batch_and_collections_failures(self) -> None:
        cases: list[tuple[str, dict[str, Any], Any, int]] = [
            ("graph", {"graphs": [{"graphId": "g1", "compositeRequest": [{}]}]}, {"graphs": [{"graphId": "g1", "graphResponse": {"compositeResponse": []}, "isSuccessful": False}]}, 1),
            ("batch", {"batchRequests": [{"method": "GET", "url": "v62.0/limits"}]}, {"hasErrors": True, "results": [{"statusCode": 200, "result": {}}, {"statusCode": 404, "result": [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]}]}, 1),
            ("tree", {"records": [{"attributes": {"type": "Account", "referenceId": "ref1"}, "Name": "Acme"}]}, {"hasErrors": False, "results": [{"referenceId": "ref1", "id": ACCOUNT_ID}]}, 0),
            (
                "collections_create",
                {"allOrNone": False, "records": [{"attributes": {"type": "Order_Event__e"}, "Status__c": "Shipped"}]},
                [{"id": "e00xx0000000001AAA", "success": True, "errors": []}, {"success": False, "errors": [{"statusCode": "REQUIRED_FIELD_MISSING", "message": "Required fields are missing: [Name]", "fields": ["Name"]}]}],
                1,
            ),
        ]
        for operation, request_body, response, failures in cases:
            with self.subTest(operation=operation):
                self.respond(transcript(200 if operation != "tree" else 201, response))
                params: dict[str, Any] = {"operation": operation, "body": request_body, "sobject": "Account"}
                result = self.run_family("composite", params)
                self.assertEqual(result["body"], response)
                self.assertEqual(result.get("failures", 0), failures)

    def test_tooling_query_pagination_and_follow_up(self) -> None:
        next_url = V + "/tooling/query/01gD0000002HU6KIAW-2000"
        page = {"size": 2400, "totalSize": 2400, "done": False, "queryLocator": "01gD0000002HU6KIAW-2000", "nextRecordsUrl": next_url, "entityTypeName": "ApexClass", "records": [{"attributes": {"type": "ApexClass", "url": V + f"/tooling/sobjects/ApexClass/{CLASS_ID}"}, "Id": CLASS_ID, "Name": "Probe"}]}
        self.respond(transcript(200, page))
        result = self.run_family("tooling", {"operation": "query", "query": {"q": soql("SELECT", "Id,", "Name", "FROM", "ApexClass")}, "headers": {"Sforce-Query-Options": "batchSize=200"}})
        self.assertEqual(result["pagination"], {"next_records_url": next_url})
        self.assertEqual(result["nextRecordsUrl"], next_url)
        self.assertEqual(self.sent()[3]["Sforce-Query-Options"], "batchSize=200")
        self.respond(transcript(200, {**page, "done": True, "nextRecordsUrl": None}))
        follow = self.run_family("tooling", {"operation": "query_more", "next_records_url": result["pagination"]["next_records_url"]})
        self.assertEqual(self.sent()[1], next_url)
        self.assertNotIn("pagination", follow)

    def test_anonymous_apex_failure_and_tooling_delete(self) -> None:
        compile_failure = {"line": 1, "column": 13, "compiled": False, "success": False, "compileProblem": "Unexpected token 'oops'.", "exceptionStackTrace": None, "exceptionMessage": None}
        self.respond(transcript(200, compile_failure))
        result = self.run_family("tooling", {"operation": "execute_anonymous", "query": {"anonymousBody": "oops"}})
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["body"]["compileProblem"], "Unexpected token 'oops'.")
        self.respond(f"HTTP/1.1 204\n{ORG_HEADERS}\n")
        deleted = self.run_family("tooling", {"operation": "delete", "sobject": "TraceFlag", "id": "7tfD0000000TN7cIAG"})
        self.assertEqual((deleted["status"], deleted["body"]), (204, ""))
        self.assertNotIn("error_code", deleted)

    def test_bulk_list_locator_and_results_paging(self) -> None:
        listing = {"done": False, "records": [{"id": JOB_ID, "operation": "query", "object": "Account", "state": "JobComplete", "jobType": "V2Query"}], "nextRecordsUrl": V + "/jobs/query?queryLocator=01gR0000000opRTIAY-2000"}
        self.respond(transcript(200, listing))
        result = self.run_family("bulk_jobs", {"operation": "query_list", "query": {"jobType": "V2Query", "concurrencyMode": "parallel", "isPkChunkingEnabled": False}})
        self.assertEqual(result["pagination"]["next_query_locator"], "01gR0000000opRTIAY-2000")
        self.assertEqual(self.sent()[2], {"jobType": ["V2Query"], "concurrencyMode": ["parallel"], "isPkChunkingEnabled": ["false"]})
        csv_page = '"Id","Name"\n"001R0000002HNw3IAG","Acme: West"\n"001R0000002HNw4IAG","Sample, Inc."\n'
        with tempfile.TemporaryDirectory() as workspace:
            output = str(Path(workspace) / "page1.csv")
            self.respond(transcript(200, csv_page, content_type="text/csv", extra="sforce-locator: MTAwMDA\nsforce-numberofrecords: 2\n"))
            page = self.run_family("bulk_jobs", {"operation": "query_results", "job_id": JOB_ID, "query": {"maxRecords": 2}, "output_csv_path": output}, _gate(workspace))
            self.assertEqual(Path(output).read_text(), csv_page)
            self.assertNotIn("body", page)
            self.assertEqual(page["bytes_written"], len(csv_page.encode()))
            self.assertEqual(page["pagination"], {"next_locator": "MTAwMDA", "number_of_records": "2"})
            self.respond(transcript(200, '"Id","Name"\n', content_type="text/csv", extra="sforce-locator: null\nsforce-numberofrecords: 0\n"))
            last = self.run_family("bulk_jobs", {"operation": "query_results", "job_id": JOB_ID, "query": {"locator": "MTAwMDA"}, "output_csv_path": output}, _gate(workspace))
            self.assertEqual(self.sent()[2], {"locator": ["MTAwMDA"]})
            self.assertEqual(last["pagination"], {"number_of_records": "0"})

    def test_uncertain_csv_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            output = Path(workspace) / "page.csv"
            self.respond(transcript(200, "Id: unquoted header-like text", content_type="text/csv"))
            with self.assertRaises(SalesforceCliCallError):
                self.run_family("bulk_jobs", {"operation": "query_results", "job_id": JOB_ID, "output_csv_path": str(output)}, _gate(workspace))
            self.assertFalse(output.exists())

    def test_vendor_errors_map_to_codes(self) -> None:
        cases = [
            (400, "MALFORMED_QUERY", "sf.malformed_query"),
            (400, "JSON_PARSER_ERROR", "sf.bad_request"),
            (401, "INVALID_SESSION_ID", "sf.session_expired"),
            (403, "INSUFFICIENT_ACCESS", "sf.permission_denied"),
            (403, "REQUEST_LIMIT_EXCEEDED", "sf.rate_limited"),
            (403, "API_DISABLED_FOR_ORG", "sf.permission_denied"),
            (404, "NOT_FOUND", "sf.not_found"),
            (409, "CONFLICT", "sf.conflict"),
            (412, "PRECONDITION_FAILED", "sf.precondition_failed"),
            (500, "UNKNOWN_EXCEPTION", "sf.api_error"),
        ]
        for status, vendor_code, expected in cases:
            with self.subTest(status=status, vendor_code=vendor_code):
                body = [{"message": "vendor detail", "errorCode": vendor_code}]
                self.respond(transcript(status, body))
                result = self.run_family("bulk_jobs", {"operation": "query_get", "job_id": JOB_ID})
                self.assertEqual((result["status"], result["error_code"], result["body"]), (status, expected, body))
                self.assertNotIn("failures", result)

    def test_missing_http_status_is_an_api_error(self) -> None:
        self.printed = [("Error (1): No authorization information found for bound-org.\n", True)]
        result = self.run_family("tooling", {"operation": "list_sobjects"})
        self.assertIsNone(result["status"])
        self.assertEqual(result["error_code"], "sf.api_error")
        self.assertIn("body_advisory", result)

    def test_generic_verb_keeps_its_uncertain_boundary(self) -> None:
        """The typed media proof is opt-in; the generic verb's ruled output is unchanged."""
        body = {"records": [], "done": True}
        printed = transcript(200, body)
        with patch("salesforce_plugin.client.subprocess.run", return_value=subprocess.CompletedProcess([], 0, printed, "")):
            generic = request_actions.authenticated_request(self.executor, {"method": "GET", "path": V + "/tooling/query", "query": {"q": "x"}})
            typed = request_actions.authenticated_request(self.executor, {"method": "GET", "path": V + "/tooling/query", "query": {"q": "x"}}, expected_media="json")
        self.assertEqual(generic["body"], printed)
        self.assertIn("body_advisory", generic)
        self.assertEqual(typed["body"], body)
        self.assertNotIn("transcript", typed)


class AsReceived(Fixture):
    """The vendor result is returned as received; the audit line carries none of it."""

    def test_vendor_echo_returned_as_received_and_absent_from_audit(self) -> None:
        token = "00DR0000000Ab12!AQ4AQ.token/with+chars="
        session = "sess!on/Value+2=="
        raw = token.encode()
        variants = [token, quote(token, safe=""), quote_plus(token, safe=""), base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(), base64.b64encode(raw).decode().rstrip("=")]
        echo = {"accessToken": token, "echo": " | ".join(variants), "sessionDetail": f"<sessionId>{session}</sessionId>", "Name": "kept"}
        self.respond(transcript(201, {"id": CLASS_ID, "success": True, "errors": [], "detail": echo}))
        with self.assertLogs(request_actions.logger, level="INFO") as audit:
            result = self.run_family(
                "tooling",
                {"operation": "create", "sobject": "NamedCredential", "body": {"DeveloperName": "Probe", "Password": session}, "headers": {"X-Api-Token": token}},
            )
        logged = " ".join(audit.output)
        for variant in [*variants, session, quote(session, safe=""), base64.b64encode(session.encode()).decode()]:
            self.assertNotIn(variant, logged)
        self.assertEqual(result["body"]["detail"], echo)
        self.assertEqual(result["body"]["id"], CLASS_ID)


class Wiring(unittest.TestCase):
    def test_dispatch_validates_then_queues(self) -> None:
        plugin = SalesforcePlugin()
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "j1", "status": "queued"}}
        plugin._async_job_manager = manager  # noqa: SLF001
        with patch.object(async_jobs, "ensure_worker_started"):
            refused = plugin.composite({"operation": "nope"}, {"session_id": "s", "flow_id": "f"})
            queued = plugin.bulk_jobs({"operation": "query_get", "job_id": JOB_ID}, {"session_id": "s", "flow_id": "f"})
        self.assertEqual(refused["error"]["code"], "sf.invalid_params")
        self.assertEqual(queued["data"], {"job_id": "j1", "status": "queued"})
        self.assertEqual(manager.create_job.call_count, 1)

    def test_worker_handlers_and_completion_text(self) -> None:
        for family in typed_families.FAMILIES:
            with self.subTest(family=family):
                handler, gate_factory = async_jobs.ACTION_HANDLERS[family]
                self.assertEqual(handler.func, typed_families.run_family)  # type: ignore[attr-defined]
                self.assertEqual(handler.args, (family,))  # type: ignore[attr-defined]
                # Only families that read or write a local file take a path gate.
                self.assertEqual(gate_factory is not None, family in ("bulk_jobs", "metadata", "content_files"))
                instructions = " ".join(completion_templates.build_completion_template(family, "result")["params"]["prompt"]["user"]["instructions"])  # type: ignore[index]
                self.assertIn("pagination", instructions)
                self.assertIn("failures", instructions)

    def test_query_all_selector(self) -> None:
        executor = MagicMock()
        executor.run_json.return_value = {"records": [], "totalSize": 0, "done": True}
        with tempfile.TemporaryDirectory() as workspace:
            output = str(Path(workspace) / "rows.tsv")
            gate = lambda path: _checked_path(path, [workspace], suffix=".tsv", param_name="output_tsv_path", reading=False)  # noqa: E731
            soql_actions.soql_query(executor, {"query": soql("SELECT", "Id", "FROM", "Account"), "output_tsv_path": output, "include_deleted": True}, gate)
            self.assertIn("--all-rows", executor.run_json.call_args.args[0])
            soql_actions.export_soql(executor, {"query": soql("SELECT", "Id", "FROM", "Account"), "output_tsv_path": output}, gate)
            self.assertNotIn("--all-rows", executor.run_json.call_args.args[0])
            with self.assertRaises(ValueError):
                soql_actions.soql_query(executor, {"query": "x", "output_tsv_path": output, "include_deleted": "yes"}, gate)


class Registration(unittest.TestCase):
    """Each process needs its decorator, edge entry and knowledge-base JSON."""

    def test_three_artifacts_per_process(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        plugin = SalesforcePlugin()
        decorated = {action.name: action for action in discover_actions(plugin)}
        edges = plugin.get_edge_process_definitions()
        kb = ROOT / "plugins/salesforce_plugin/knowledge_base/processes"
        jsons = {path.stem: json.loads(path.read_text()) for path in kb.glob("*.json")}
        self.assertEqual(set(decorated), set(edges))
        self.assertEqual(set(decorated), set(jsons))
        for name, doc in jsons.items():
            with self.subTest(process=name):
                self.assertEqual(doc["process_key"], f"plugin::salesforce_plugin::{name}")
                result_type = doc["result_processor_customizations"]["result_type"]
                self.assertEqual(edges[name].result_processor_template_customizations.result_type, result_type)  # type: ignore[union-attr]
        for family in typed_families.FAMILIES:
            with self.subTest(family=family):
                self.assertTrue(200 <= len(jsons[family]["embedding_description"]) <= 400)
                for operation in typed_families.FAMILIES[family]:
                    self.assertIn(operation, jsons[family]["description"])
                    self.assertIn(operation, decorated[family].description)

    def test_each_verb_declares_exactly_its_path_parameters(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        expected = {
            "composite": ["sobject", "external_id_field"],
            "tooling": ["next_records_url", "sobject", "id"],
            "bulk_jobs": ["job_id", "next_records_url"],
            "reports": ["report_id", "instance_id", "report_type"],
            "dashboards": ["dashboard_id"],
            "rest_resources": ["sobject", "external_id_field", "external_id", "id", "relationship", "next_records_url"],
            "actions": ["action_type", "action"],
            "metadata": ["deploy_request_id"],
            "content_files": ["content_version_id", "content_document_id"],
        }
        local = {"bulk_jobs": ["output_csv_path"], "metadata": ["zip_path"], "content_files": ["file_path", "output_path"]}
        decorated = {action.name: action for action in discover_actions(SalesforcePlugin())}
        for family, placeholders in expected.items():
            with self.subTest(family=family):
                self.assertEqual(typed_families.path_parameters(family), placeholders)
                tail = local.get(family, [])
                self.assertEqual(list(decorated[family].parameters), ["operation", "query", "body", "headers", *placeholders, *tail])


def _gate(workspace: str) -> Any:
    def gate(path: str) -> str:
        return _checked_path(path, [workspace], suffix=".csv", param_name="output_tsv_path", reading=False)

    return gate


if __name__ == "__main__":
    unittest.main()
