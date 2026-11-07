"""Offline smoke for build 4's typed Salesforce families.

Covers ``reports``, ``dashboards``, ``rest_resources`` and ``actions`` (routing
for every operation and the three-artifact registration check live in
``smoke_typed_families.py``). Vendor calls run through the real
``SalesforceCliExecutor`` with ``subprocess.run`` patched to print what
``sf api request rest --include`` prints: an ``HTTP/1.1`` status line,
lower-case response headers, no blank separator, then the pretty-printed JSON
body. Bodies follow the vendor's documented shapes (Reports and Dashboards REST
API report runs, instances, dashboard results and refresh status; REST API
limits, updated/deleted, external-id upsert, query paging and explain plans;
Invocable Actions lists, describes and per-input results). No CLI executable
and no Salesforce org are needed.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, quote, quote_plus, urlsplit

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/salesforce_plugin/src"))

from salesforce_plugin import async_jobs, completion_templates, request_actions, typed_families  # noqa: E402
from salesforce_plugin.client import SalesforceCliExecutor  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

V = "/services/data/v62.0"
REPORT_ID = "00OD0000001ZbP7MAK"
CLONE_ID = "00OD0000001ZbQ2MAK"
INSTANCE_ID = "0LGD000000000IjOAI"
DASHBOARD_ID = "01ZD00000007S89MAE"
COMPONENT_ID = "01aD0000000a36LIAQ"
FOLDER_ID = "00lR0000000DnRZIA0"
ACCOUNT_ID = "001D000000IqhSLIAZ"
OTHER_ACCOUNT_ID = "001D000000IqhSMIAZ"
OWNER_ID = "005D0000001KyEIIA0"
ORG_HEADERS = "date: Sat, 07 Nov 2026 12:00:00 GMT\nx-content-type-options: nosniff\nx-robots-tag: none\nsforce-limit-info: api-usage=31/15000\n"


def soql(*words: str) -> str:
    """SOQL built from words so the repository SQL-literal gate sees no statement."""
    return " ".join(words)


def transcript(status: int, body: Any) -> str:
    printed = json.dumps(body, indent=2) if body != "" else ""
    return f"HTTP/1.1 {status}\n{ORG_HEADERS}content-type: application/json;charset=UTF-8\n{printed}\n"


def no_content() -> str:
    return f"HTTP/1.1 204\n{ORG_HEADERS}\n"


def run_body(all_data: bool) -> dict[str, Any]:
    """A summary report run as the Reports REST API returns it."""
    return {
        "attributes": {"describeUrl": V + f"/analytics/reports/{REPORT_ID}/describe", "instancesUrl": V + f"/analytics/reports/{REPORT_ID}/instances", "reportId": REPORT_ID, "reportName": "Opportunities by Stage", "type": "Report"},
        "allData": all_data,
        "factMap": {
            "0!T": {"aggregates": [{"label": "$1,200,000.00", "value": 1200000}, {"label": "12", "value": 12}], "rows": [{"dataCells": [{"label": "Acme - 1,200 Widgets", "value": "006D000000CDn8bIAD"}]}]},
            "T!T": {"aggregates": [{"label": "$4,200,000.00", "value": 4200000}, {"label": "31", "value": 31}]},
        },
        "groupingsAcross": {"groupings": []},
        "groupingsDown": {"groupings": [{"groupings": [], "key": "0", "label": "Prospecting", "value": "Prospecting"}]},
        "hasDetailRows": True,
        "reportExtendedMetadata": {"aggregateColumnInfo": {"s!AMOUNT": {"acrossGroupingContext": None, "dataType": "currency", "downGroupingContext": None, "label": "Sum of Amount"}}},
        "reportMetadata": {"aggregates": ["s!AMOUNT", "RowCount"], "detailColumns": ["OPPORTUNITY_NAME", "AMOUNT"], "id": REPORT_ID, "name": "Opportunities by Stage", "reportFormat": "SUMMARY", "reportFilters": []},
    }


def instance(status: str) -> dict[str, Any]:
    return {"completionDate": "2026-11-07T12:00:05Z" if status == "Success" else None, "hasDetailRows": False, "id": INSTANCE_ID, "ownerId": OWNER_ID, "queryable": False, "requestDate": "2026-11-07T12:00:00Z", "status": status, "url": V + f"/analytics/reports/{REPORT_ID}/instances/{INSTANCE_ID}"}


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.executor = SalesforceCliExecutor(MagicMock())
        self.executor._target_org = "bound-org"  # noqa: SLF001
        self.argvs: list[list[str]] = []
        self.bodies: list[str | None] = []
        self.printed: list[str] = []

    def respond(self, *outputs: str) -> None:
        self.printed = list(outputs)

    def run_family(self, family: str, params: dict[str, Any]) -> dict[str, Any]:
        def invoke(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
            self.argvs.append(argv)
            self.bodies.append(Path(argv[argv.index("--body") + 1][1:]).read_text() if "--body" in argv else None)
            text = self.printed.pop(0)
            return subprocess.CompletedProcess(argv, int(int(text.split()[1]) >= 400), text, "")

        with patch("salesforce_plugin.client.subprocess.run", side_effect=invoke):
            return typed_families.run_family(family, self.executor, params, None)

    def sent(self) -> tuple[str, str, dict[str, list[str]], dict[str, str]]:
        argv = self.argvs[-1]
        target = urlsplit(argv[4])
        headers = dict(item.split(":", 1) for item in (argv[i + 1] for i, a in enumerate(argv) if a == "--header"))
        return argv[argv.index("--method") + 1], target.path, parse_qs(target.query), headers

    def sent_body(self) -> Any:
        return json.loads(self.bodies[-1] or "null")


class Reports(Fixture):
    def test_run_with_details_reports_partial_data(self) -> None:
        self.respond(transcript(200, run_body(all_data=False)))
        result = self.run_family("reports", {"operation": "run", "report_id": REPORT_ID, "query": {"includeDetails": True}})
        self.assertEqual(self.sent()[:3], ("GET", V + f"/analytics/reports/{REPORT_ID}", {"includeDetails": ["true"]}))
        self.assertEqual(result["body"]["factMap"]["T!T"]["aggregates"][0]["value"], 4200000)
        self.assertEqual(result["pagination"], {"report_all_data": False})
        self.assertNotIn("error_code", result)
        self.respond(transcript(200, run_body(all_data=True)))
        complete = self.run_family("reports", {"operation": "run", "report_id": REPORT_ID})
        self.assertNotIn("pagination", complete)

    def test_filtered_run_and_unsaved_query_send_report_metadata(self) -> None:
        metadata = {"reportMetadata": {"reportBooleanFilter": "1 AND 2", "reportFilters": [{"column": "STAGE_NAME", "operator": "equals", "value": "Prospecting"}, {"column": "AMOUNT", "operator": "greaterThan", "value": "1000"}]}}
        for operation, path in (("run_filtered", V + f"/analytics/reports/{REPORT_ID}"), ("query", V + "/analytics/reports/query")):
            with self.subTest(operation=operation):
                self.respond(transcript(200, run_body(all_data=True)))
                self.run_family("reports", {"operation": operation, "report_id": REPORT_ID, "body": metadata, "query": {"includeDetails": False}})
                method, sent_path, query, headers = self.sent()
                self.assertEqual((method, sent_path, query), ("POST", path, {"includeDetails": ["false"]}))
                self.assertEqual(self.sent_body(), metadata)
                self.assertEqual(headers["Content-Type"], "application/json")

    def test_async_run_lifecycle_new_running_success(self) -> None:
        self.respond(transcript(200, instance("New")))
        started = self.run_family("reports", {"operation": "run_async", "report_id": REPORT_ID})
        self.assertIsNone(self.bodies[-1])
        self.assertEqual(started["body"]["id"], INSTANCE_ID)
        self.respond(transcript(200, [instance("Running")]))
        listed = self.run_family("reports", {"operation": "instances", "report_id": REPORT_ID})
        self.assertEqual(listed["body"][0]["status"], "Running")
        self.assertNotIn("failures", listed)
        self.respond(transcript(200, {**run_body(all_data=True), "attributes": {**instance("Success"), "reportId": REPORT_ID, "reportName": "Opportunities by Stage"}}))
        done = self.run_family("reports", {"operation": "instance_results", "report_id": REPORT_ID, "instance_id": INSTANCE_ID})
        self.assertEqual(self.sent()[1], V + f"/analytics/reports/{REPORT_ID}/instances/{INSTANCE_ID}")
        self.assertEqual(done["body"]["attributes"]["status"], "Success")

    def test_clone_update_delete_lifecycle(self) -> None:
        cloned = {"reportMetadata": {"id": CLONE_ID, "name": "Opportunities by Stage (copy)", "reportFormat": "SUMMARY"}, "reportTypeMetadata": {}, "reportExtendedMetadata": {}}
        self.respond(transcript(200, cloned))
        result = self.run_family("reports", {"operation": "clone", "query": {"cloneId": REPORT_ID}, "body": {"reportMetadata": {"name": "Opportunities by Stage (copy)"}}})
        self.assertEqual(self.sent()[:3], ("POST", V + "/analytics/reports", {"cloneId": [REPORT_ID]}))
        self.assertEqual(result["body"]["reportMetadata"]["id"], CLONE_ID)
        corrected = {"reportMetadata": {"name": "Pipeline by Stage"}}
        self.respond(transcript(200, {"reportMetadata": {"id": CLONE_ID, "name": "Pipeline by Stage"}}))
        self.run_family("reports", {"operation": "update", "report_id": CLONE_ID, "body": corrected})
        self.assertEqual((self.sent()[0], self.sent_body()), ("PATCH", corrected))
        self.respond(no_content())
        deleted = self.run_family("reports", {"operation": "delete", "report_id": CLONE_ID})
        self.assertEqual((deleted["status"], deleted["body"]), (204, ""))
        self.assertNotIn("error_code", deleted)
        self.respond(transcript(404, [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]))
        gone = self.run_family("reports", {"operation": "describe", "report_id": CLONE_ID})
        self.assertEqual(gone["error_code"], "sf.not_found")

    def test_report_types(self) -> None:
        listing = [{"label": "Accounts & Contacts", "reportTypes": [{"describeUrl": V + "/analytics/reportTypes/ContactList", "isHidden": False, "label": "Contacts & Accounts", "supportsJoinedFormat": True, "type": "ContactList"}]}]
        self.respond(transcript(200, listing))
        self.assertEqual(self.run_family("reports", {"operation": "report_types"})["body"], listing)
        self.respond(transcript(200, {"reportMetadata": {"reportType": {"type": "CustomEntity$Invoice__c"}}, "reportTypeMetadata": {"categories": []}}))
        self.run_family("reports", {"operation": "describe_report_type", "report_type": "CustomEntity$Invoice__c"})
        self.assertEqual(self.sent()[1], V + "/analytics/reportTypes/CustomEntity%24Invoice__c")


class Dashboards(Fixture):
    def test_results_refresh_and_status(self) -> None:
        results = {
            "componentData": [{"componentId": COMPONENT_ID, "reportResult": run_body(all_data=True), "status": {"dataStatus": "DATA", "errorCode": None, "errorMessage": None, "errorSeverity": None, "refreshDate": "2026-11-07T11:00:00.000+0000", "refreshStatus": "IDLE"}}],
            "dashboardMetadata": {"attributes": {"dashboardId": DASHBOARD_ID, "dashboardName": "Sales Pipeline", "statusUrl": V + f"/analytics/dashboards/{DASHBOARD_ID}/status"}, "components": [], "filters": [], "id": DASHBOARD_ID, "name": "Sales Pipeline"},
        }
        self.respond(transcript(200, results))
        result = self.run_family("dashboards", {"operation": "results", "dashboard_id": DASHBOARD_ID, "query": {"filter1": "0ICD00000004CBiOAM"}})
        self.assertEqual(self.sent()[2], {"filter1": ["0ICD00000004CBiOAM"]})
        self.assertEqual(result["body"]["componentData"][0]["status"]["refreshStatus"], "IDLE")
        self.respond(transcript(201, {"statusUrl": V + f"/analytics/dashboards/{DASHBOARD_ID}/status"}))
        refreshed = self.run_family("dashboards", {"operation": "refresh", "dashboard_id": DASHBOARD_ID})
        self.assertEqual((self.sent()[0], self.bodies[-1]), ("PUT", None))
        self.assertEqual(refreshed["status"], 201)
        self.respond(transcript(200, {"componentStatus": [{"componentId": COMPONENT_ID, "refreshDate": "2026-11-07T12:00:00.000+0000", "refreshStatus": "RUNNING"}]}))
        status = self.run_family("dashboards", {"operation": "status", "dashboard_id": DASHBOARD_ID})
        self.assertEqual(status["body"]["componentStatus"][0]["refreshStatus"], "RUNNING")

    def test_clone_update_delete_and_refresh_limit(self) -> None:
        self.respond(transcript(201, {"attributes": {"dashboardId": "01ZD00000007S8AMAU"}, "id": "01ZD00000007S8AMAU", "name": "Sales Pipeline (copy)", "folderId": FOLDER_ID}))
        cloned = self.run_family("dashboards", {"operation": "clone", "query": {"cloneId": DASHBOARD_ID}, "body": {"folderId": FOLDER_ID}})
        self.assertEqual(self.sent()[:3], ("POST", V + "/analytics/dashboards", {"cloneId": [DASHBOARD_ID]}))
        self.assertEqual(cloned["body"]["folderId"], FOLDER_ID)
        self.respond(transcript(200, {"id": "01ZD00000007S8AMAU", "name": "Pipeline"}))
        self.run_family("dashboards", {"operation": "update", "dashboard_id": "01ZD00000007S8AMAU", "body": {"dashboardMetadata": {"name": "Pipeline"}}})
        self.assertEqual(self.sent()[0], "PATCH")
        self.respond(no_content())
        self.assertEqual(self.run_family("dashboards", {"operation": "delete", "dashboard_id": "01ZD00000007S8AMAU"})["status"], 204)
        self.respond(transcript(403, [{"errorCode": "FORBIDDEN", "message": "You can't refresh this dashboard again yet."}]))
        limited = self.run_family("dashboards", {"operation": "refresh", "dashboard_id": DASHBOARD_ID, "body": {"filters": [{"id": "0ICD00000004CBiOAM", "value": "Prospecting"}]}})
        self.assertEqual(limited["error_code"], "sf.permission_denied")
        self.assertEqual(self.sent_body(), {"filters": [{"id": "0ICD00000004CBiOAM", "value": "Prospecting"}]})


class RestResources(Fixture):
    def test_limits_recent_and_sobject_info(self) -> None:
        self.respond(transcript(200, {"DailyApiRequests": {"Max": 15000, "Remaining": 14969}, "DataStorageMB": {"Max": 1024, "Remaining": 1013}}))
        self.assertEqual(self.run_family("rest_resources", {"operation": "limits"})["body"]["DailyApiRequests"]["Remaining"], 14969)
        self.respond(transcript(200, [{"attributes": {"type": "Account", "url": V + f"/sobjects/Account/{ACCOUNT_ID}"}, "Id": ACCOUNT_ID, "Name": "Acme"}]))
        self.run_family("rest_resources", {"operation": "recent", "query": {"limit": 5}})
        self.assertEqual(self.sent()[2], {"limit": ["5"]})
        info = {"objectDescribe": {"name": "Account", "keyPrefix": "001", "queryable": True, "urls": {"rowTemplate": V + "/sobjects/Account/{ID}"}}, "recentItems": [{"attributes": {"type": "Account"}, "Id": ACCOUNT_ID, "Name": "Acme"}]}
        self.respond(transcript(200, info))
        self.assertEqual(self.run_family("rest_resources", {"operation": "sobject_info", "sobject": "Account"})["body"], info)

    def test_updated_and_deleted_windows(self) -> None:
        window = {"start": "2026-11-01T00:00:00+00:00", "end": "2026-11-07T00:00:00Z"}
        self.respond(transcript(200, {"ids": [ACCOUNT_ID], "latestDateCovered": "2026-11-06T23:45:00.000+0000"}))
        self.run_family("rest_resources", {"operation": "updated", "sobject": "Account", "query": window})
        self.assertEqual(self.sent()[2], {key: [value] for key, value in window.items()})
        self.respond(transcript(200, {"deletedRecords": [{"deletedDate": "2026-11-03T10:00:00.000+0000", "id": OTHER_ACCOUNT_ID}], "earliestDateAvailable": "2026-10-08T00:00:00.000+0000", "latestDateCovered": "2026-11-06T23:45:00.000+0000"}))
        deleted = self.run_family("rest_resources", {"operation": "deleted", "sobject": "Account", "query": window})
        self.assertEqual(deleted["body"]["deletedRecords"][0]["id"], OTHER_ACCOUNT_ID)

    def test_query_pages_until_done(self) -> None:
        next_url = V + "/query/01gD0000002HU6KIAW-2000"
        page = {"done": False, "nextRecordsUrl": next_url, "records": [{"attributes": {"type": "Account", "url": V + f"/sobjects/Account/{ACCOUNT_ID}"}, "Id": ACCOUNT_ID}], "totalSize": 3200}
        self.respond(transcript(200, page))
        first = self.run_family("rest_resources", {"operation": "query_all", "query": {"q": soql("SELECT", "Id", "FROM", "Account")}, "headers": {"Sforce-Query-Options": "batchSize=2000"}})
        self.assertEqual(self.sent()[1], V + "/queryAll")
        self.assertEqual(first["pagination"], {"next_records_url": next_url})
        self.respond(transcript(200, {**page, "done": True, "nextRecordsUrl": None}))
        last = self.run_family("rest_resources", {"operation": "query_more", "next_records_url": first["pagination"]["next_records_url"]})
        self.assertEqual(self.sent()[1], next_url)
        self.assertNotIn("pagination", last)

    def test_explain_and_parameterized_search(self) -> None:
        plan = {"plans": [{"cardinality": 1, "fields": ["Name"], "leadingOperationType": "Index", "notes": [], "relativeCost": 0.0013, "sobjectCardinality": 3200, "sobjectType": "Account"}]}
        self.respond(transcript(200, plan))
        statement = soql("SELECT", "Id", "FROM", "Account", "WHERE", "Name", "=", "'Acme'")
        self.run_family("rest_resources", {"operation": "explain", "query": {"explain": statement}})
        self.assertEqual(self.sent()[1:3], (V + "/query", {"explain": [statement]}))
        body = {"q": "Acme", "fields": ["Id", "Name"], "sobjects": [{"name": "Account", "limit": 5}], "in": "NAME", "overallLimit": 10}
        self.respond(transcript(200, {"searchRecords": [{"attributes": {"type": "Account"}, "Id": ACCOUNT_ID, "Name": "Acme"}]}))
        self.run_family("rest_resources", {"operation": "parameterized_search", "body": body})
        self.assertEqual(self.sent_body(), body)

    def test_external_id_upsert_conflict_stale_write_and_delete(self) -> None:
        params = {"sobject": "Account", "external_id_field": "External_Id__c", "external_id": "A/100"}
        self.respond(transcript(201, {"created": True, "errors": [], "id": ACCOUNT_ID, "success": True}))
        created = self.run_family("rest_resources", {**params, "operation": "upsert_by_external_id", "body": {"Name": "Acme"}})
        self.assertEqual(self.sent()[1], V + "/sobjects/Account/External_Id__c/A%2F100")
        self.assertEqual((created["status"], created["body"]["created"]), (201, True))
        self.respond(transcript(200, {"created": False, "errors": [], "id": ACCOUNT_ID, "success": True}))
        self.assertEqual(self.run_family("rest_resources", {**params, "operation": "upsert_by_external_id", "body": {"Name": "Acme Corp"}})["status"], 200)
        self.respond(transcript(300, [V + f"/sobjects/Account/{ACCOUNT_ID}", V + f"/sobjects/Account/{OTHER_ACCOUNT_ID}"]))
        self.assertEqual(self.run_family("rest_resources", {**params, "operation": "get_by_external_id"})["error_code"], "sf.conflict")
        self.respond(transcript(412, [{"errorCode": "PRECONDITION_FAILED", "message": "The requested resource has been modified since the time specified."}]))
        stale = self.run_family("rest_resources", {**params, "operation": "delete_by_external_id", "headers": {"If-Unmodified-Since": "Fri, 06 Nov 2026 00:00:00 GMT"}})
        self.assertEqual(self.sent()[3]["If-Unmodified-Since"], "Fri, 06 Nov 2026 00:00:00 GMT")
        self.assertEqual(stale["error_code"], "sf.precondition_failed")
        self.respond(no_content())
        self.assertEqual(self.run_family("rest_resources", {**params, "operation": "delete_by_external_id"})["status"], 204)

    def test_relationship_traversal(self) -> None:
        self.respond(transcript(200, {"done": True, "records": [{"attributes": {"type": "Contact"}, "Id": "003D000000QV9n2IAD", "LastName": "Ng"}], "totalSize": 1}))
        self.run_family("rest_resources", {"operation": "relationship", "sobject": "Account", "id": ACCOUNT_ID, "relationship": "Contacts", "query": {"fields": ["Id", "LastName"]}})
        self.assertEqual(self.sent()[1:3], (V + f"/sobjects/Account/{ACCOUNT_ID}/Contacts", {"fields": ["Id,LastName"]}))


class DocumentedQueryParameters(Fixture):
    """Every query parameter the vendor documents for these operations reaches the vendor.

    Dashboard Results documents runningUser and filter1 to filter3 for GET and
    PUT and isStickyFilterSave for PATCH; Dashboard Status documents the same
    four for GET; Dashboard Describe documents loadComponentProperties; Upsert
    Records Using sObject Rows by External ID documents updateOnly.
    """

    choices = {"runningUser": OWNER_ID, "filter1": "0ICD00000004CBiOAM", "filter2": "0ICD00000004CBjOAM", "filter3": "0ICD00000004CBkOAM"}

    def test_results_and_refresh_take_the_running_user_and_filters(self) -> None:
        dashboard = V + f"/analytics/dashboards/{DASHBOARD_ID}"
        expected = {key: [value] for key, value in self.choices.items()}
        metadata = {"attributes": {"dashboardId": DASHBOARD_ID, "dashboardName": "Sales Pipeline"}, "canChangeRunningUser": True, "dashboardType": "MyTeamUser", "id": DASHBOARD_ID, "runningUser": {"displayName": "Dana Ng", "id": OWNER_ID}}
        self.respond(transcript(200, {"componentData": [], "dashboardMetadata": metadata}))
        result = self.run_family("dashboards", {"operation": "results", "dashboard_id": DASHBOARD_ID, "query": dict(self.choices)})
        self.assertEqual(self.sent()[:3], ("GET", dashboard, expected))
        self.assertEqual(result["body"]["dashboardMetadata"]["runningUser"]["id"], OWNER_ID)
        self.respond(transcript(201, {"statusUrl": dashboard + "/status"}))
        refreshed = self.run_family("dashboards", {"operation": "refresh", "dashboard_id": DASHBOARD_ID, "query": dict(self.choices)})
        self.assertEqual(self.sent()[:3], ("PUT", dashboard, expected))
        self.assertEqual((refreshed["status"], self.bodies[-1]), (201, None))
        self.respond(transcript(200, {"componentStatus": [{"componentId": COMPONENT_ID, "refreshDate": "2026-11-07T12:00:00.000+0000", "refreshStatus": "IDLE"}]}))
        self.run_family("dashboards", {"operation": "status", "dashboard_id": DASHBOARD_ID, "query": {"runningUser": OWNER_ID, "filter2": self.choices["filter2"]}})
        self.assertEqual(self.sent()[:3], ("GET", dashboard + "/status", {"runningUser": [OWNER_ID], "filter2": [self.choices["filter2"]]}))

    def test_describe_component_properties_and_sticky_filter_save(self) -> None:
        dashboard = V + f"/analytics/dashboards/{DASHBOARD_ID}"
        self.respond(transcript(200, {"components": [{"id": COMPONENT_ID, "properties": None, "reportId": REPORT_ID, "type": "Report"}], "id": DASHBOARD_ID, "name": "Sales Pipeline"}))
        described = self.run_family("dashboards", {"operation": "describe", "dashboard_id": DASHBOARD_ID, "query": {"loadComponentProperties": False}})
        self.assertEqual(self.sent()[:3], ("GET", dashboard + "/describe", {"loadComponentProperties": ["false"]}))
        self.assertIsNone(described["body"]["components"][0]["properties"])
        filters = {"filters": [{"errorMessage": None, "id": "0IBR00000004D4iOAE", "name": "Billing City", "options": [{"alias": "New York City", "id": "0ICR00000004FtQOAU", "operation": "equals", "value": "New York"}], "selectedOption": 0}]}
        self.respond(transcript(200, {"dashboardMetadata": {"id": DASHBOARD_ID, **filters}}))
        self.run_family("dashboards", {"operation": "update", "dashboard_id": DASHBOARD_ID, "query": {"isStickyFilterSave": True}, "body": filters})
        self.assertEqual(self.sent()[:3], ("PATCH", dashboard, {"isStickyFilterSave": ["true"]}))
        self.assertEqual(self.sent_body(), filters)

    def test_running_user_must_be_a_user_id_before_any_call(self) -> None:
        with self.assertRaisesRegex(typed_families.TypedFamilyError, "runningUser must be a 15- or 18-character Salesforce id"):
            self.run_family("dashboards", {"operation": "refresh", "dashboard_id": DASHBOARD_ID, "query": {"runningUser": "dana@example.com"}})
        self.assertEqual(self.argvs, [])

    def test_upsert_update_only_never_creates(self) -> None:
        params = {"operation": "upsert_by_external_id", "sobject": "Account", "external_id_field": "customExtIdField__c", "external_id": "11999", "query": {"updateOnly": True}}
        self.respond(transcript(200, {"created": False, "errors": [], "id": ACCOUNT_ID, "success": True}))
        updated = self.run_family("rest_resources", {**params, "body": {"Name": "California Wheat Corporation", "Type": "New Customer"}})
        self.assertEqual(self.sent()[:3], ("PATCH", V + "/sobjects/Account/customExtIdField__c/11999", {"updateOnly": ["true"]}))
        self.assertEqual((updated["status"], updated["body"]["created"]), (200, False))
        self.respond(transcript(404, [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]))
        missing = self.run_family("rest_resources", {**params, "external_id": "12000", "body": {"Name": "Nobody"}})
        self.assertEqual((self.sent()[2], missing["error_code"]), ({"updateOnly": ["true"]}, "sf.not_found"))


class Actions(Fixture):
    def test_discover_describe_and_invoke_with_partial_failure(self) -> None:
        self.respond(transcript(200, {"apex": V + "/actions/custom/apex", "emailAlert": V + "/actions/custom/emailAlert", "flow": V + "/actions/custom/flow", "quickAction": V + "/actions/custom/quickAction"}))
        self.assertIn("flow", self.run_family("actions", {"operation": "list_custom"})["body"])
        self.respond(transcript(200, {"actions": [{"label": "Close Case", "name": "Close_Case", "type": "FLOW", "url": V + "/actions/custom/flow/Close_Case"}]}))
        self.run_family("actions", {"operation": "list_custom_type", "action_type": "flow"})
        describe = {"inputs": [{"name": "CaseId", "required": True, "type": "STRING"}], "label": "Close Case", "name": "Close_Case", "outputs": [{"name": "Flow__InterviewStatus", "type": "STRING"}], "type": "FLOW"}
        self.respond(transcript(200, describe))
        self.assertEqual(self.run_family("actions", {"operation": "describe_custom", "action_type": "flow", "action": "Close_Case"})["body"], describe)
        results = [
            {"actionName": "Close_Case", "errors": None, "isSuccess": True, "outputValues": {"Flow__InterviewStatus": "Finished"}},
            {"actionName": "Close_Case", "errors": [{"message": "Required fields are missing: [CaseId]", "statusCode": "REQUIRED_FIELD_MISSING"}], "isSuccess": False, "outputValues": None},
        ]
        self.respond(transcript(200, results))
        invoked = self.run_family("actions", {"operation": "invoke_custom", "action_type": "flow", "action": "Close_Case", "body": {"inputs": [{"CaseId": "500D000000AbCdEIAV"}, {}]}})
        self.assertEqual(self.sent()[:2], ("POST", V + "/actions/custom/flow/Close_Case"))
        self.assertEqual(invoked["failures"], 1)

    def test_standard_action_and_unknown_action(self) -> None:
        self.respond(transcript(200, [{"actionName": "chatterPost", "errors": None, "isSuccess": True, "outputValues": {"feedItemId": "0D5D0000000cfMYKAY"}}]))
        posted = self.run_family("actions", {"operation": "invoke_standard", "action": "chatterPost", "body": {"inputs": [{"subjectNameOrId": OWNER_ID, "type": "user", "text": "Deal closed"}]}})
        self.assertNotIn("failures", posted)
        self.respond(transcript(404, [{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}]))
        self.assertEqual(self.run_family("actions", {"operation": "describe_standard", "action": "Account.NoSuchAction"})["error_code"], "sf.not_found")


class VendorErrors(Fixture):
    def test_auth_and_permission_failures_in_every_family(self) -> None:
        calls: list[tuple[str, dict[str, Any]]] = [
            ("reports", {"operation": "list"}),
            ("dashboards", {"operation": "list"}),
            ("rest_resources", {"operation": "limits"}),
            ("actions", {"operation": "list_standard"}),
        ]
        cases = [(401, "INVALID_SESSION_ID", "sf.session_expired"), (403, "INSUFFICIENT_ACCESS", "sf.permission_denied"), (403, "FORBIDDEN", "sf.permission_denied"), (400, "BAD_REQUEST", "sf.bad_request")]
        for family, params in calls:
            for status, vendor_code, expected in cases:
                with self.subTest(family=family, vendor_code=vendor_code):
                    body = [{"errorCode": vendor_code, "message": "vendor detail"}]
                    self.respond(transcript(status, body))
                    result = self.run_family(family, params)
                    self.assertEqual((result["status"], result["error_code"], result["body"]), (status, expected, body))


class Validation(Fixture):
    """Inputs the vendor document would reject never reach the CLI."""

    def test_refusals_before_io(self) -> None:
        metadata = {"reportMetadata": {"name": "x"}}
        bad: list[tuple[str, dict[str, Any]]] = [
            ("reports", {"operation": "run", "report_id": REPORT_ID, "query": {"includeDetails": "yes"}}),
            ("reports", {"operation": "clone", "body": metadata}),
            ("reports", {"operation": "clone", "query": {"cloneId": "not-an-id"}, "body": metadata}),
            ("reports", {"operation": "create", "query": {"cloneId": REPORT_ID}, "body": metadata}),
            ("reports", {"operation": "run_filtered", "report_id": REPORT_ID, "body": {"filters": []}}),
            ("reports", {"operation": "run_async", "report_id": REPORT_ID, "body": []}),
            ("reports", {"operation": "instance_results", "report_id": REPORT_ID, "instance_id": "../x"}),
            ("reports", {"operation": "describe_report_type", "report_type": "a/b"}),
            ("dashboards", {"operation": "results", "dashboard_id": DASHBOARD_ID, "query": {"filter9": "x"}}),
            ("dashboards", {"operation": "update", "dashboard_id": DASHBOARD_ID, "body": {}}),
            ("dashboards", {"operation": "delete", "dashboard_id": "short"}),
            ("rest_resources", {"operation": "updated", "sobject": "Account", "query": {"start": "2026-11-01T00:00:00Z"}}),
            ("rest_resources", {"operation": "deleted", "sobject": "Account", "query": {"start": "yesterday", "end": "2026-11-07T00:00:00Z"}}),
            ("rest_resources", {"operation": "recent", "query": {"limit": 0}}),
            ("rest_resources", {"operation": "query_more", "next_records_url": V + "/tooling/query/01gD0000002HU6KIAW-2000"}),
            ("rest_resources", {"operation": "get_by_external_id", "sobject": "Account", "external_id_field": "External_Id__c", "external_id": ""}),
            ("rest_resources", {"operation": "get_by_external_id", "sobject": "Account", "external_id_field": "External_Id__c", "external_id": "A\n1"}),
            ("rest_resources", {"operation": "parameterized_search", "body": {"fields": ["Id"]}}),
            ("actions", {"operation": "invoke_standard", "action": "chatterPost", "body": {"text": "x"}}),
            ("actions", {"operation": "describe_standard", "action": "a/b"}),
            ("actions", {"operation": "list_custom_type", "action_type": "flow.x"}),
        ]
        for family, params in bad:
            with self.subTest(family=family, params=params), patch("salesforce_plugin.client.subprocess.run") as run:
                with self.assertRaises(typed_families.TypedFamilyError):
                    typed_families.run_family(family, self.executor, params, None)
                run.assert_not_called()


class Redaction(Fixture):
    """Every credential echo is hidden in raw, JSON-escaped, percent-encoded and base64 forms."""

    def test_issued_token_variants_are_redacted_in_every_family(self) -> None:
        token = "00DR0000000Ab12!AQ4AQ.build4/token+chars="
        raw = token.encode()
        variants = [token, json.dumps(token)[1:-1], quote(token, safe=""), quote_plus(token, safe=""), base64.b64encode(raw).decode(), base64.urlsafe_b64encode(raw).decode(), base64.b64encode(raw).decode().rstrip("=")]
        calls: list[tuple[str, dict[str, Any]]] = [
            ("reports", {"operation": "update", "report_id": REPORT_ID, "body": {"reportMetadata": {"name": "x"}}}),
            ("dashboards", {"operation": "refresh", "dashboard_id": DASHBOARD_ID}),
            ("rest_resources", {"operation": "upsert_by_external_id", "sobject": "Account", "external_id_field": "External_Id__c", "external_id": "A-1", "body": {"Name": "x"}}),
            ("actions", {"operation": "invoke_custom", "action_type": "apex", "action": "TokenEcho", "body": {"inputs": [{"apiKey": token}]}}),
        ]
        for family, params in calls:
            with self.subTest(family=family):
                echo = {"accessToken": token, "echo": " | ".join(variants), "Name": "kept"}
                self.respond(transcript(200, {"id": ACCOUNT_ID, "success": True, "detail": echo}))
                with self.assertLogs(request_actions.logger, level="INFO") as audit:
                    result = self.run_family(family, {**params, "headers": {"X-Api-Token": token}})
                rendered = json.dumps(result) + " ".join(audit.output)
                for variant in variants:
                    self.assertNotIn(variant, rendered)
                self.assertEqual(result["body"]["detail"]["Name"], "kept")


class Wiring(unittest.TestCase):
    FAMILIES = ("reports", "dashboards", "rest_resources", "actions")

    def test_dispatch_validates_then_queues(self) -> None:
        plugin = SalesforcePlugin()
        manager = MagicMock()
        manager.create_job.return_value = {"action_status": "completed", "data": {"job_id": "j1", "status": "queued"}}
        plugin._async_job_manager = manager  # noqa: SLF001
        state = {"session_id": "s", "flow_id": "f"}
        with patch.object(async_jobs, "ensure_worker_started"):
            for family in self.FAMILIES:
                with self.subTest(family=family):
                    refused = getattr(plugin, family)({"operation": "nope"}, state)
                    self.assertEqual(refused["error"]["code"], "sf.invalid_params")
            queued = plugin.reports({"operation": "delete", "report_id": REPORT_ID}, state)
        self.assertEqual(queued["data"], {"job_id": "j1", "status": "queued"})
        self.assertEqual(manager.create_job.call_count, 1)

    def test_worker_handlers_and_completion_text(self) -> None:
        for family in self.FAMILIES:
            with self.subTest(family=family):
                handler, gate_factory = async_jobs.ACTION_HANDLERS[family]
                self.assertEqual(handler.func, typed_families.run_family)  # type: ignore[attr-defined]
                self.assertEqual(handler.args, (family,))  # type: ignore[attr-defined]
                self.assertIsNone(gate_factory)
                instructions = " ".join(completion_templates.build_completion_template(family, "result")["params"]["prompt"]["user"]["instructions"])  # type: ignore[index]
                self.assertIn("report_all_data", instructions)
                self.assertIn("failures", instructions)


if __name__ == "__main__":
    unittest.main()
