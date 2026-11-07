"""Offline smoke for the build 4 typed Snowflake REST families: table, view, sequence and pipe.

Routes are written independently from snowflakedb/snowflake-rest-api-specs at
990e25d9 (table.yaml, view.yaml, sequence.yaml, pipe.yaml) and from the Snowpipe
REST API reference (insertFiles, insertReport, loadHistoryScan). Transcripts
follow those documents' response shapes: Table, View, Sequence and Pipe
objects, SuccessResponse, SuccessAcceptedResponse and ErrorResponse from
common.yaml, and the Snowpipe insertFiles response and load reports. The
Snowpipe v1 error envelope (code, message, success, data, headers) is the shape
the vendor's ingest SDK parses. No live account.
"""

from __future__ import annotations

import base64
import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote, quote_from_bytes, quote_plus

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/snowflake_plugin/src"))

from snowflake_plugin import async_jobs, completion_templates, rest_client, rest_families  # noqa: E402
from snowflake_plugin.app_config import SnowflakeAccountConfig  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402

Handler = Callable[[httpx.Request], httpx.Response]
BUILD_4 = frozenset({"table", "view", "sequence", "pipe"})
# Written out, not imported: smoke_rest_families.py routes these.
EARLIER_BUILDS = frozenset({"sql_api", "task", "stream", "dynamic_table", "database", "schema", "warehouse", "stage"})
REST_ERROR = {"code": "002003", "error_code": "002003", "message": "SQL compilation error:\nTable 'ORDERS' does not exist or not authorized.", "request_id": "7c2e1a90-1111-2222-3333-444455556666"}
SNOWPIPE_ERROR = {"data": None, "code": "390404", "message": "Specified object does not exist or not authorized. Pipe not found", "success": False, "headers": None}
_BASE = "/api/v2/databases/DB/schemas/PUBLIC"
_INGEST = "/v1/data/pipes/DB.PUBLIC.ORDERS"


class FamilyFixture(unittest.TestCase):
    config: SnowflakeAccountConfig

    @classmethod
    def setUpClass(cls) -> None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        cls.config = SnowflakeAccountConfig("org-account", "service_user", "WH", "DB", "PUBLIC", "ROLE", "key_pair", der)

    def plugin(self) -> SnowflakePlugin:
        plugin = SnowflakePlugin()
        loader = MagicMock()
        loader.resolve.return_value = self.config
        plugin._app_config_loader = loader
        plugin._login_timeout_seconds = MagicMock(return_value=30)  # type: ignore[method-assign]
        return plugin

    def run_family(self, family: str, params: dict[str, Any], handler: Handler) -> dict[str, Any]:
        real = rest_client.perform_request

        def with_transport(config: SnowflakeAccountConfig, request: dict[str, Any], *, timeout_seconds: int) -> dict[str, Any]:
            return real(config, request, timeout_seconds=timeout_seconds, transport=httpx.MockTransport(handler))

        with patch.object(rest_client, "perform_request", with_transport):
            outcome = rest_families.run_family(family, self.plugin(), params)
        self.assertEqual(outcome["action_status"], "completed", outcome)
        return outcome["data"]


def obj(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "database": "DB", "schema": "PUBLIC", "name": "ORDERS", **extra}


def coll(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "database": "DB", "schema": "PUBLIC", **extra}


FILES = {"files": [{"path": "2026/11/07/orders_0.csv.gz", "size": 18211}, {"path": "2026/11/07/orders_1.csv.gz"}]}

# Independently written vendor routes: family, params, method, path and query sent.
ROUTES: list[tuple[str, dict[str, Any], str, str, dict[str, str]]] = [
    ("table", coll("list", query={"like": "ord%", "startsWith": "OR", "showLimit": 50, "fromName": "A", "history": True, "deep": True}), "GET", f"{_BASE}/tables", {"like": "ord%", "startsWith": "OR", "showLimit": "50", "fromName": "A", "history": "true", "deep": "true"}),
    ("table", coll("create", query={"createMode": "ifNotExists", "copyGrants": True}, body={"name": "ORDERS", "columns": [{"name": "ID", "datatype": "NUMBER(38,0)", "nullable": False}], "cluster_by": ["ID"]}), "POST", f"{_BASE}/tables", {"createMode": "ifNotExists", "copyGrants": "true"}),
    (
        "table",
        coll("create_as_select", query={"query": "SELECT * FROM RAW.ORDERS WHERE TS > '2026-01-01'", "createMode": "orReplace", "copyGrants": False}, body={"name": "ORDERS_2026"}),
        "POST",
        f"{_BASE}/tables:as-select",
        {"query": "SELECT * FROM RAW.ORDERS WHERE TS > '2026-01-01'", "createMode": "orReplace", "copyGrants": "false"},
    ),
    ("table", coll("create_using_template", query={"query": "SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) FROM TABLE(INFER_SCHEMA(LOCATION=>'@LANDING'))"}, body={"name": "INFERRED"}), "POST", f"{_BASE}/tables:using-template", {"query": "SELECT ARRAY_AGG(OBJECT_CONSTRUCT(*)) FROM TABLE(INFER_SCHEMA(LOCATION=>'@LANDING'))"}),
    ("table", coll("create_using_template", query={"query": "SELECT 1", "createMode": "errorIfExists"}), "POST", f"{_BASE}/tables:using-template", {"query": "SELECT 1", "createMode": "errorIfExists"}),
    ("table", obj("get"), "GET", f"{_BASE}/tables/ORDERS", {}),
    ("table", obj("create_or_alter", body={"name": "ORDERS", "comment": "orders fact", "data_retention_time_in_days": 3}), "PUT", f"{_BASE}/tables/ORDERS", {}),
    ("table", obj("delete", query={"ifExists": True}), "DELETE", f"{_BASE}/tables/ORDERS", {"ifExists": "true"}),
    (
        "table",
        obj("clone", query={"createMode": "orReplace", "copyGrants": True, "targetDatabase": "DEV", "targetSchema": "SCRATCH"}, body={"name": "ORDERS_COPY", "point_of_time": {"point_of_time_type": "offset", "reference": "before", "offset": "-600"}}),
        "POST",
        f"{_BASE}/tables/ORDERS:clone",
        {"createMode": "orReplace", "copyGrants": "true", "targetDatabase": "DEV", "targetSchema": "SCRATCH"},
    ),
    ("table", obj("create_like", query={"createMode": "ifNotExists", "copyGrants": False}, body={"name": "ORDERS_EMPTY"}), "POST", f"{_BASE}/tables/ORDERS:create-like", {"createMode": "ifNotExists", "copyGrants": "false"}),
    ("table", obj("undrop"), "POST", f"{_BASE}/tables/ORDERS:undrop", {}),
    ("table", obj("suspend_recluster", query={"ifExists": True}), "POST", f"{_BASE}/tables/ORDERS:suspend-recluster", {"ifExists": "true"}),
    ("table", obj("resume_recluster"), "POST", f"{_BASE}/tables/ORDERS:resume-recluster", {}),
    ("table", obj("swap_with", query={"targetName": "ORDERS_STAGE", "ifExists": False, "targetDatabase": "DB", "targetSchema": "STAGE"}), "POST", f"{_BASE}/tables/ORDERS:swap-with", {"targetName": "ORDERS_STAGE", "ifExists": "false", "targetDatabase": "DB", "targetSchema": "STAGE"}),
    ("table", obj("set_tags", tags=[{"tag_name": "GOV.PII", "tag_value": "none"}], query={"ifExists": True}), "POST", f"{_BASE}/tables/ORDERS:set-tags", {"ifExists": "true"}),
    ("table", obj("unset_tags", tags=[{"tag_name": "GOV.PII"}]), "POST", f"{_BASE}/tables/ORDERS:unset-tags", {}),
    ("table", obj("get_tags", query={"withLineage": True}), "GET", f"{_BASE}/tables/ORDERS:get-tags", {"withLineage": "true"}),
    ("table", {"operation": "fetch_result", "result_handler": "01b8c2f4-t", "query": {"page": 2}}, "GET", "/api/v2/results/01b8c2f4-t", {"page": "2"}),
    ("view", coll("list", query={"like": "v_%", "startsWith": "V_", "showLimit": 10, "fromName": "V_A", "deep": False}), "GET", f"{_BASE}/views", {"like": "v_%", "startsWith": "V_", "showLimit": "10", "fromName": "V_A", "deep": "false"}),
    ("view", coll("create", query={"createMode": "orReplace", "copyGrants": True}, body={"name": "V_OPEN", "secure": True, "columns": [{"name": "ID"}, {"name": "TOTAL"}], "query": "SELECT ID, TOTAL FROM ORDERS WHERE STATUS = 'OPEN'"}), "POST", f"{_BASE}/views", {"createMode": "orReplace", "copyGrants": "true"}),
    ("view", obj("get"), "GET", f"{_BASE}/views/ORDERS", {}),
    ("view", obj("delete", query={"ifExists": True}), "DELETE", f"{_BASE}/views/ORDERS", {"ifExists": "true"}),
    ("view", obj("set_tags", tags=[{"tag_name": "TIER", "tag_value": "gold"}]), "POST", f"{_BASE}/views/ORDERS:set-tags", {}),
    ("view", obj("unset_tags", tags=[{"tag_name": "TIER"}], query={"ifExists": False}), "POST", f"{_BASE}/views/ORDERS:unset-tags", {"ifExists": "false"}),
    ("view", obj("get_tags"), "GET", f"{_BASE}/views/ORDERS:get-tags", {}),
    ("view", {"operation": "fetch_result", "result_handler": "01b8c2f4-v"}, "GET", "/api/v2/results/01b8c2f4-v", {}),
    ("sequence", coll("list", query={"like": "seq%"}), "GET", f"{_BASE}/sequences", {"like": "seq%"}),
    ("sequence", coll("create", query={"createMode": "ifNotExists"}, body={"name": "ORDER_ID_SEQ", "start": 1000, "increment": 5, "ordered": True}), "POST", f"{_BASE}/sequences", {"createMode": "ifNotExists"}),
    ("sequence", obj("get"), "GET", f"{_BASE}/sequences/ORDERS", {}),
    ("sequence", obj("delete", query={"ifExists": True}), "DELETE", f"{_BASE}/sequences/ORDERS", {"ifExists": "true"}),
    ("sequence", obj("clone", query={"createMode": "orReplace", "targetDatabase": "DEV", "targetSchema": "SCRATCH"}, body={"name": "ORDER_ID_SEQ_DEV"}), "POST", f"{_BASE}/sequences/ORDERS:clone", {"createMode": "orReplace", "targetDatabase": "DEV", "targetSchema": "SCRATCH"}),
    ("sequence", obj("rename", query={"targetName": "ORDER_KEYS", "ifExists": True, "targetDatabase": "DB", "targetSchema": "KEYS"}), "POST", f"{_BASE}/sequences/ORDERS:rename", {"targetName": "ORDER_KEYS", "ifExists": "true", "targetDatabase": "DB", "targetSchema": "KEYS"}),
    ("sequence", {"operation": "fetch_result", "result_handler": "01b8c2f4-s"}, "GET", "/api/v2/results/01b8c2f4-s", {}),
    ("pipe", coll("list", query={"like": "ord%"}), "GET", f"{_BASE}/pipes", {"like": "ord%"}),
    ("pipe", coll("create", query={"createMode": "orReplace"}, body={"name": "ORDERS", "auto_ingest": False, "copy_statement": "COPY INTO ORDERS FROM @LANDING/orders/ FILE_FORMAT = (TYPE = CSV)"}), "POST", f"{_BASE}/pipes", {"createMode": "orReplace"}),
    ("pipe", obj("get"), "GET", f"{_BASE}/pipes/ORDERS", {}),
    ("pipe", obj("delete", query={"ifExists": True}), "DELETE", f"{_BASE}/pipes/ORDERS", {"ifExists": "true"}),
    ("pipe", obj("refresh", query={"ifExists": True, "prefix": "2026/11/", "modified_after": "2026-11-06T00:00:00Z"}), "POST", f"{_BASE}/pipes/ORDERS:refresh", {"ifExists": "true", "prefix": "2026/11/", "modified_after": "2026-11-06T00:00:00Z"}),
    ("pipe", obj("set_tags", tags=[{"tag_name": "OWNER", "tag_value": "ingest"}], query={"ifExists": True}), "POST", f"{_BASE}/pipes/ORDERS:set-tags", {"ifExists": "true"}),
    ("pipe", obj("unset_tags", tags=[{"tag_name": "OWNER"}]), "POST", f"{_BASE}/pipes/ORDERS:unset-tags", {}),
    ("pipe", obj("get_tags", query={"withLineage": False}), "GET", f"{_BASE}/pipes/ORDERS:get-tags", {"withLineage": "false"}),
    ("pipe", obj("insert_files", query={"requestId": "ingest-1", "showSkippedFiles": True}, body=FILES), "POST", f"{_INGEST}/insertFiles", {"requestId": "ingest-1", "showSkippedFiles": "true"}),
    ("pipe", obj("insert_report", query={"requestId": "report-1", "beginMark": "1_39"}), "GET", f"{_INGEST}/insertReport", {"requestId": "report-1", "beginMark": "1_39"}),
    ("pipe", obj("load_history_scan", query={"startTimeInclusive": "2026-11-07T10:00:00Z", "endTimeExclusive": "2026-11-07T10:15:00Z", "requestId": "scan-1"}), "GET", f"{_INGEST}/loadHistoryScan", {"startTimeInclusive": "2026-11-07T10:00:00Z", "endTimeExclusive": "2026-11-07T10:15:00Z", "requestId": "scan-1"}),
    ("pipe", {"operation": "fetch_result", "result_handler": "01b8c2f4-p"}, "GET", "/api/v2/results/01b8c2f4-p", {}),
]

# Snowpipe ingestion calls get a generated requestId when the caller sends none.
GENERATES_REQUEST_ID = {("pipe", "insert_files"), ("pipe", "insert_report"), ("pipe", "load_history_scan")}


class Routing(FamilyFixture):
    def test_every_build_4_operation_has_an_independently_written_route(self) -> None:
        covered = {(family, params["operation"]) for family, params, *_ in ROUTES}
        declared = {(family, operation) for family, operations in rest_families.FAMILIES.items() if family in BUILD_4 for operation in operations}
        self.assertEqual(covered, declared)

    def test_the_two_family_smokes_together_route_every_declared_family(self) -> None:
        self.assertEqual(set(rest_families.FAMILIES), BUILD_4 | EARLIER_BUILDS)

    def test_method_path_query_and_body_reach_the_vendor_unchanged(self) -> None:
        for family, params, method, path, query in ROUTES:
            with self.subTest(family=family, operation=params["operation"]):
                seen: list[httpx.Request] = []

                def vendor(request: httpx.Request, seen: list[httpx.Request] = seen) -> httpx.Response:
                    seen.append(request)
                    return httpx.Response(200, json={"status": "Statement executed successfully."})

                data = self.run_family(family, params, vendor)
                self.assertEqual(len(seen), 1, "one physical call, no polling or paging")
                request = seen[0]
                self.assertEqual(request.method, method)
                self.assertEqual(request.url.host, "org-account.snowflakecomputing.com")
                self.assertEqual(request.url.raw_path.decode().split("?")[0], path)
                self.assertTrue(request.headers["Authorization"].startswith("Bearer "))
                self.assertEqual(request.headers["Accept"], "application/json")
                self.assertEqual(dict(request.url.params), query)
                expected_body = params.get("body", params.get("tags"))
                self.assertEqual(json.loads(request.content) if request.content else None, expected_body)
                if expected_body is not None:
                    self.assertEqual(request.headers["Content-Type"], "application/json")
                self.assertEqual((data["family"], data["operation"], data["method"], data["status"]), (family, params["operation"], method, 200))

    def test_snowpipe_calls_generate_a_request_id_and_keep_the_callers(self) -> None:
        for family, operation in sorted(GENERATES_REQUEST_ID):
            with self.subTest(operation=operation):
                params = obj(operation, body=FILES) if operation == "insert_files" else obj(operation)
                if operation == "load_history_scan":
                    params["query"] = {"startTimeInclusive": "2026-11-07T10:00:00+00:00"}
                sent: list[str] = []

                def vendor(request: httpx.Request, sent: list[str] = sent) -> httpx.Response:
                    sent.append(request.url.params["requestId"])
                    return httpx.Response(200, json={"requestId": request.url.params["requestId"], "responseCode": "SUCCESS"})

                data = self.run_family(family, params, vendor)
                self.assertRegex(sent[0], r"^[0-9a-f-]{36}$")
                self.assertEqual(data["request_id"], sent[0])
        kept = rest_families.build_request("pipe", obj("insert_report", query={"requestId": "mine"}))
        self.assertEqual(kept["query"], {"requestId": "mine"})
        for family, params, *_ in ROUTES:
            if (family, params["operation"]) not in GENERATES_REQUEST_ID:
                self.assertNotIn("requestId", rest_families.build_request(family, params)["query"], params["operation"])

    def test_quoted_identifiers_are_encoded_in_resource_and_snowpipe_paths(self) -> None:
        seen: list[str] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path.decode().split("?")[0])
            return httpx.Response(200, json={"requestId": "x", "responseCode": "SUCCESS"})

        names = {"database": '"my db"', "schema": '"raw/v2"', "name": '"Orders ""EU"""'}
        self.run_family("table", {"operation": "delete", **names}, vendor)
        self.run_family("pipe", {"operation": "insert_report", **names}, vendor)
        encoded = [quote(names[key], safe="") for key in ("database", "schema", "name")]
        self.assertEqual(seen, [f"/api/v2/databases/{encoded[0]}/schemas/{encoded[1]}/tables/{encoded[2]}", f"/v1/data/pipes/{encoded[0]}.{encoded[1]}.{encoded[2]}/insertReport"])


class Validation(unittest.TestCase):
    def test_vendor_invalid_input_is_refused_before_any_call(self) -> None:
        too_many = {"files": [{"path": f"f{index}.csv"} for index in range(5001)]}
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("table", coll("truncate"), "operation must be one of"),
            ("table", obj("get", name="a.b"), "name must be a Snowflake identifier"),
            ("table", coll("create_as_select", body={"name": "T"}), "query parameters ['query'] are required"),
            ("table", coll("create_using_template"), "query parameters ['query'] are required"),
            ("table", obj("swap_with"), "query parameters ['targetName'] are required"),
            ("table", coll("list", query={"showLimit": 10001}), "between 1 and 10000"),
            ("table", coll("list", query={"deep": "yes"}), "deep must be a boolean"),
            ("table", coll("create", body={"comment": "no name"}), "missing required members: name"),
            ("table", obj("clone", body={"point_of_time": {}}), "missing required members: name"),
            ("table", obj("undrop", body={"name": "X"}), "takes no body object"),
            ("table", obj("get", query={"ifExists": True}), "not documented for this operation"),
            ("view", coll("create", body={"name": "V", "query": "SELECT 1"}), "missing required members: columns"),
            ("view", coll("create", query={"createMode": "replace"}, body={"name": "V", "columns": [{"name": "A"}], "query": "SELECT 1 A"}), "createMode must be one of"),
            ("view", coll("list", query={"history": True}), "not documented for this operation"),
            ("sequence", obj("rename", query={"ifExists": True}), "query parameters ['targetName'] are required"),
            ("sequence", coll("list", query={"showLimit": 5}), "not documented for this operation"),
            ("sequence", obj("clone", body={}), "missing required members: name"),
            ("pipe", coll("create", body={"name": "P"}), "missing required members: copy_statement"),
            ("pipe", obj("refresh", query={"modified_after": "yesterday"}), "modified_after must be an ISO-8601 timestamp"),
            ("pipe", obj("load_history_scan"), "query parameters ['startTimeInclusive'] are required"),
            ("pipe", obj("load_history_scan", query={"startTimeInclusive": "2026-13-01T00:00:00Z"}), "startTimeInclusive must be an ISO-8601 timestamp"),
            ("pipe", obj("load_history_scan", query={"startTimeInclusive": 1700000000}), "startTimeInclusive must be an ISO-8601 timestamp"),
            ("pipe", obj("insert_files"), "body must be a JSON object"),
            ("pipe", obj("insert_files", body={"files": []}), "missing required members: files"),
            ("pipe", obj("insert_files", body={"files": "a.csv"}), "body.files must be an array of 1 to 5000"),
            ("pipe", obj("insert_files", body=too_many), "body.files must be an array of 1 to 5000"),
            ("pipe", obj("insert_files", body={"files": [{"size": 10}]}), "every file needs a path"),
            ("pipe", obj("insert_files", body={"files": ["a.csv"]}), "every file needs a path"),
            ("pipe", obj("insert_files", body={"files": [{"path": "é" * 513}]}), "1 to 1024 UTF-8 bytes"),
            ("pipe", obj("insert_files", body={"files": [{"path": "a.csv", "size": -1}]}), "non-negative integer"),
            ("pipe", obj("insert_files", body={"files": [{"path": "a.csv", "size": True}]}), "non-negative integer"),
            ("pipe", obj("insert_report", query={"showSkippedFiles": True}), "not documented for this operation"),
            ("pipe", obj("insert_report", database="1db"), "database must be a Snowflake identifier"),
        ]
        for family, params, message in cases:
            with self.subTest(family=family, params=str(params)[:120]), self.assertRaises(rest_families.RestFamilyError) as raised:
                rest_families.build_request(family, params)
            self.assertIn(message, str(raised.exception))

    def test_documented_limits_are_accepted_at_the_edge(self) -> None:
        exactly = {"files": [{"path": "é" * 512, "size": 0}] + [{"path": f"f{index}.csv"} for index in range(4999)]}
        self.assertEqual(len(rest_families.build_request("pipe", obj("insert_files", body=exactly))["body"]["files"]), 5000)
        self.assertEqual(rest_families.build_request("table", coll("list", query={"showLimit": 10000}))["query"], {"showLimit": 10000})
        for stamp in ("2026-11-07T10:00:00Z", "2026-11-07T10:00:00.123-08:00", "2026-11-07"):
            self.assertEqual(rest_families.build_request("pipe", obj("refresh", query={"modified_after": stamp}))["query"], {"modified_after": stamp})


class Transcripts(FamilyFixture):
    """Real-shaped vendor responses for each build 4 family."""

    def test_table_list_pages_with_from_name_then_ends(self) -> None:
        tables = [
            {
                "name": "CUSTOMERS",
                "kind": "PERMANENT",
                "cluster_by": [],
                "change_tracking": False,
                "data_retention_time_in_days": 1,
                "comment": "",
                "created_on": "2026-02-01T10:00:00.000-08:00",
                "database_name": "DB",
                "schema_name": "PUBLIC",
                "rows": 1200,
                "bytes": 52224,
                "owner": "SYSADMIN",
                "automatic_clustering": False,
                "search_optimization": False,
                "owner_role_type": "ROLE",
                "table_type": "NORMAL",
            },
            {
                "name": "ORDERS",
                "kind": "PERMANENT",
                "cluster_by": ["ORDER_DATE"],
                "change_tracking": True,
                "data_retention_time_in_days": 7,
                "comment": "orders fact",
                "created_on": "2026-03-04T08:30:00.000-08:00",
                "database_name": "DB",
                "schema_name": "PUBLIC",
                "rows": 98113,
                "bytes": 4194304,
                "owner": "SYSADMIN",
                "automatic_clustering": True,
                "search_optimization": False,
                "owner_role_type": "ROLE",
                "table_type": "NORMAL",
            },
        ]
        data = self.run_family("table", coll("list", query={"showLimit": 2}), lambda _: httpx.Response(200, json=tables))
        self.assertEqual(data["body"], tables)
        self.assertEqual(data["pagination"], {"next_from_name": "ORDERS"})
        sent: list[dict[str, str]] = []

        def last_page(request: httpx.Request) -> httpx.Response:
            sent.append(dict(request.url.params))
            return httpx.Response(200, json=tables[1:])

        following = self.run_family("table", coll("list", query={"showLimit": 2, "fromName": data["pagination"]["next_from_name"]}), last_page)
        self.assertEqual(sent, [{"showLimit": "2", "fromName": "ORDERS"}])
        self.assertNotIn("pagination", following, "a short page ends the listing")

    def test_table_clone_accepted_then_fetch_result(self) -> None:
        accepted = {"code": "202", "message": "Request execution in progress. Use the provided location header or result handler ID to perform query monitoring and management.", "resultHandler": "01b8c2f4-0000-a1b2-0000-0004a1b2c3d5"}
        data = self.run_family("table", obj("clone", body={"name": "ORDERS_COPY"}), lambda _: httpx.Response(202, json=accepted, headers={"Location": "/api/v2/results/01b8c2f4-0000-a1b2-0000-0004a1b2c3d5"}))
        self.assertEqual(data["pagination"], {"location": "/api/v2/results/01b8c2f4-0000-a1b2-0000-0004a1b2c3d5", "result_handler": accepted["resultHandler"]})
        done = self.run_family("table", {"operation": "fetch_result", "result_handler": data["pagination"]["result_handler"]}, lambda _: httpx.Response(200, json={"status": "Table ORDERS_COPY successfully created."}))
        self.assertEqual((done["status"], done["body"]), (200, {"status": "Table ORDERS_COPY successfully created."}))

    def test_table_swap_and_drop_and_undrop_are_vendor_data(self) -> None:
        for params, message in ((obj("swap_with", query={"targetName": "ORDERS_STAGE"}), "Statement executed successfully."), (obj("delete"), "ORDERS successfully dropped."), (obj("undrop"), "Table ORDERS successfully restored.")):
            with self.subTest(operation=params["operation"]):
                data = self.run_family("table", params, lambda _, message=message: httpx.Response(200, json={"status": message}))
                self.assertEqual((data["status"], data["body"]), (200, {"status": message}))
                self.assertNotIn("error_code", data)

    def test_view_create_conflict_then_get(self) -> None:
        conflict = {"error_code": "002002", "code": "002002", "message": "SQL compilation error:\nObject 'V_OPEN' already exists.", "request_id": "1e1c5d0a-2222-3333-4444-555566667777"}
        body = {"name": "V_OPEN", "columns": [{"name": "ID"}], "query": "SELECT ID FROM ORDERS"}
        data = self.run_family("view", coll("create", body=body), lambda _: httpx.Response(409, json=conflict))
        self.assertEqual((data["status"], data["error_code"], data["body"]), (409, "snowflake.conflict", conflict))
        view = {
            "name": "V_OPEN",
            "secure": True,
            "kind": "PERMANENT",
            "recursive": False,
            "columns": [{"name": "ID", "datatype": "NUMBER(38,0)", "nullable": True}],
            "comment": None,
            "query": "SELECT ID FROM ORDERS",
            "created_on": "2026-11-07T09:00:00.000-08:00",
            "database_name": "DB",
            "schema_name": "PUBLIC",
            "owner": "SYSADMIN",
            "owner_role_type": "ROLE",
        }
        got = self.run_family("view", {**obj("get"), "name": "V_OPEN"}, lambda _: httpx.Response(200, json=view))
        self.assertEqual(got["body"], view)

    def test_sequence_get_and_rename(self) -> None:
        sequence = {"name": "ORDER_ID_SEQ", "start": 1000, "increment": 5, "ordered": True, "comment": "", "created_on": "2026-11-01T10:00:00.000-07:00", "database_name": "DB", "schema_name": "PUBLIC", "owner": "SYSADMIN", "owner_role_type": "ROLE"}
        got = self.run_family("sequence", {**obj("get"), "name": "ORDER_ID_SEQ"}, lambda _: httpx.Response(200, json=sequence))
        self.assertEqual(got["body"], sequence)
        renamed = self.run_family("sequence", obj("rename", query={"targetName": "ORDER_KEYS"}), lambda _: httpx.Response(200, json={"status": "Statement executed successfully."}))
        self.assertEqual(renamed["status"], 200)

    def test_pipe_get_shows_copy_statement_and_invalid_reason(self) -> None:
        pipe = {"name": "ORDERS", "comment": "", "auto_ingest": False, "copy_statement": "COPY INTO ORDERS FROM @LANDING/orders/", "created_on": "2026-11-07T08:00:00.000-08:00", "database_name": "DB", "schema_name": "PUBLIC", "owner": "SYSADMIN", "pattern": None, "owner_role_type": "ROLE", "invalid_reason": None}
        data = self.run_family("pipe", obj("get"), lambda _: httpx.Response(200, json=pipe))
        self.assertEqual(data["body"], pipe)

    def test_insert_files_success_is_queued_with_unmatched_and_skipped_files(self) -> None:
        response = {"requestId": "ingest-1", "responseCode": "SUCCESS", "unmatchedPatternFiles": ["2026/11/07/readme.txt"], "skippedFiles": ["2026/11/07/orders_0.csv.gz"]}
        data = self.run_family("pipe", obj("insert_files", query={"requestId": "ingest-1", "showSkippedFiles": True}, body=FILES), lambda _: httpx.Response(200, json=response))
        self.assertEqual((data["status"], data["body"], data["request_id"]), (200, response, "ingest-1"))
        self.assertNotIn("pagination", data)

    def test_insert_report_continues_from_next_begin_mark(self) -> None:
        report = {
            "pipe": "DB.PUBLIC.ORDERS",
            "completeResult": True,
            "nextBeginMark": "1_40",
            "files": [
                {
                    "path": "2026/11/07/orders_1.csv.gz",
                    "stageLocation": "s3://landing-bucket/orders/",
                    "fileSize": 18211,
                    "timeReceived": "2026-11-07T10:02:11.453Z",
                    "lastInsertTime": "2026-11-07T10:02:58.575Z",
                    "rowsInserted": 412,
                    "rowsParsed": 412,
                    "errorsSeen": 0,
                    "errorLimit": 1,
                    "complete": True,
                    "status": "LOADED",
                }
            ],
            "statistics": {"activeFilesCount": 1},
        }
        data = self.run_family("pipe", obj("insert_report", query={"beginMark": "1_39"}), lambda _: httpx.Response(200, json=report))
        self.assertEqual(data["body"], report)
        self.assertEqual(data["pagination"], {"next_begin_mark": "1_40"})

    def test_load_history_scan_incomplete_range_hints_the_next_start(self) -> None:
        scan = {
            "pipe": "DB.PUBLIC.ORDERS",
            "completeResult": "false",
            "startTimeInclusive": "2026-11-07T10:00:00.000Z",
            "endTimeExclusive": "2026-11-07T11:00:00.000Z",
            "rangeStartTime": "2026-11-07T10:00:03.112Z",
            "rangeEndTime": "2026-11-07T10:41:19.874Z",
            "files": [
                {
                    "path": "2026/11/07/orders_0.csv.gz",
                    "stageLocation": "s3://landing-bucket/orders/",
                    "fileSize": 18211,
                    "timeReceived": "2026-11-07T10:00:03.112Z",
                    "lastInsertTime": "2026-11-07T10:00:49.001Z",
                    "rowsInserted": 0,
                    "rowsParsed": 3,
                    "errorsSeen": 3,
                    "errorLimit": 1,
                    "firstError": "Numeric value 'abc' is not recognized",
                    "firstErrorLineNum": 2,
                    "firstErrorCharacterPos": 7,
                    "firstErrorColumnName": '"ORDERS"["ID":1]',
                    "systemError": None,
                    "complete": True,
                    "status": "LOAD_FAILED",
                }
            ],
        }
        query = {"startTimeInclusive": "2026-11-07T10:00:00Z", "endTimeExclusive": "2026-11-07T11:00:00Z"}
        data = self.run_family("pipe", obj("load_history_scan", query=query), lambda _: httpx.Response(200, json=scan))
        self.assertEqual(data["body"]["files"][0]["status"], "LOAD_FAILED")
        self.assertEqual(data["pagination"], {"next_start_time_inclusive": "2026-11-07T10:41:19.874Z"})
        complete = self.run_family("pipe", obj("load_history_scan", query=query), lambda _: httpx.Response(200, json={**scan, "completeResult": "true"}))
        self.assertNotIn("pagination", complete, "a complete scan needs no follow-up call")

    def test_vendor_errors_map_to_typed_codes(self) -> None:
        cases: list[tuple[str, dict[str, Any], int, str, dict[str, Any]]] = [
            ("table", obj("get"), 400, "snowflake.api_error", REST_ERROR),
            ("view", obj("delete"), 401, "snowflake.auth_failed", REST_ERROR),
            ("sequence", obj("rename", query={"targetName": "X"}), 403, "snowflake.permission_denied", REST_ERROR),
            ("table", obj("get"), 404, "snowflake.object_not_found", REST_ERROR),
            ("view", obj("get"), 408, "snowflake.timeout", REST_ERROR),
            ("table", obj("swap_with", query={"targetName": "X"}), 409, "snowflake.conflict", REST_ERROR),
            ("pipe", obj("refresh"), 429, "snowflake.rate_limited", REST_ERROR),
            ("pipe", obj("insert_files", body=FILES), 400, "snowflake.api_error", {**SNOWPIPE_ERROR, "code": "390400", "message": "Pipe is in STOPPED_CLONED state"}),
            ("pipe", obj("insert_files", body=FILES), 401, "snowflake.auth_failed", {**SNOWPIPE_ERROR, "code": "390144", "message": "JWT token is invalid."}),
            ("pipe", obj("insert_files", body=FILES), 403, "snowflake.permission_denied", {**SNOWPIPE_ERROR, "code": "390403", "message": "Insufficient privileges to operate on pipe 'ORDERS'"}),
            ("pipe", obj("insert_report"), 404, "snowflake.object_not_found", SNOWPIPE_ERROR),
            ("pipe", obj("insert_files", body=FILES), 415, "snowflake.api_error", {**SNOWPIPE_ERROR, "code": "390415", "message": "Unsupported media type"}),
            ("pipe", obj("load_history_scan", query={"startTimeInclusive": "2026-11-07T10:00:00Z"}), 429, "snowflake.rate_limited", {**SNOWPIPE_ERROR, "code": "390429", "message": "Too many requests"}),
            ("sequence", obj("get"), 503, "snowflake.api_error", REST_ERROR),
        ]
        for family, params, status, code, body in cases:
            with self.subTest(family=family, operation=params["operation"], status=status):
                data = self.run_family(family, params, lambda _, status=status, body=body: httpx.Response(status, json=body, headers={"Retry-After": "17"}))
                self.assertEqual((data["status"], data["error_code"], data["body"]), (status, code, body))
                self.assertEqual(data["headers"]["retry-after"], "17")

    def test_transport_failure_is_generic(self) -> None:
        with patch.object(rest_client, "perform_request", side_effect=httpx.ConnectError("boom at /v1/data/pipes/x?jwt=secret")):
            outcome = rest_families.run_family("pipe", self.plugin(), obj("insert_files", body=FILES))
        self.assertEqual(outcome["error"], {"code": "snowflake.api_error", "message": "Snowflake REST request failed; vendor state may have changed"})


def forms(secret: str) -> dict[str, str]:
    """Raw, percent-encoded and base64 spellings of one secret."""
    raw = secret.encode()
    out = {"raw": secret, "percent": quote_from_bytes(raw, safe=""), "form": quote_plus(secret, safe="")}
    for name, encode in (("base64", base64.b64encode), ("base64url", base64.urlsafe_b64encode)):
        encoded = encode(raw).decode()
        out[name] = encoded
        out[name + "_unpadded"] = encoded.rstrip("=")
        out[name + "_percent"] = quote(encoded, safe="")
    return out


class Redaction(FamilyFixture):
    def test_issued_jwt_and_vendor_token_variants_never_reach_result_or_hints(self) -> None:
        token, redactor = rest_client.authentication(self.config)
        issued = "vendor-issued-ingest+token/=="
        jwt_forms = forms(token)
        issued_forms = forms(issued)
        every = {**{"jwt_" + key: value for key, value in jwt_forms.items()}, **{"issued_" + key: value for key, value in issued_forms.items()}}
        body = {
            "code": "390144",
            "message": "echo " + " ".join(every.values()),
            "resultHandler": "01b8-" + jwt_forms["base64url_unpadded"][:40],
            "nextBeginMark": "1_" + jwt_forms["percent"],
            "completeResult": "false",
            "rangeEndTime": "2026-11-07T10:41:19.874Z " + issued_forms["base64"],
            "access_token": issued,
            "files": [{"path": value} for value in every.values()],
        }
        headers = {"Location": "/api/v2/results/x?t=" + jwt_forms["percent"], "Link": f'</api/v2/results/x?page=1&t={issued_forms["base64_percent"]}>; rel="next"', "X-Echo": jwt_forms["base64"]}
        cases = (
            ("table", obj("clone", body={"name": "ORDERS_COPY"})),
            ("view", obj("get")),
            ("sequence", obj("rename", query={"targetName": "X"})),
            ("pipe", obj("insert_report")),
            ("pipe", obj("load_history_scan", query={"startTimeInclusive": "2026-11-07T10:00:00Z"})),
            ("pipe", obj("insert_files", body=FILES)),
        )
        for family, params in cases:
            with self.subTest(family=family, operation=params["operation"]), patch.object(rest_client, "authentication", return_value=(token, redactor)):
                data = self.run_family(family, params, lambda _: httpx.Response(202, json=body, headers=headers))
                rendered = json.dumps(data)
                for name, value in every.items():
                    self.assertNotIn(value, rendered, name)
                hint = data["pagination"]
                self.assertIn("[REDACTED]", hint["location"])
                self.assertIn("[REDACTED]", hint["links"]["next"])
                self.assertIn("[REDACTED]", hint["next_begin_mark"])
                self.assertIn("[REDACTED]", hint["next_start_time_inclusive"])


class Plugin(FamilyFixture):
    def test_dispatch_refuses_before_job_and_queues_deletes_and_ingestion(self) -> None:
        plugin = self.plugin()
        state = {"session_id": "s", "flow_id": "f"}
        with patch.object(async_jobs, "create_job") as create_job:
            refused = plugin.pipe(obj("insert_files", body={"files": []}), state)
            create_job.assert_not_called()
            self.assertEqual(refused["error"]["code"], "snowflake.invalid_params")
            create_job.return_value = {"action_status": "completed", "data": {"job_id": "j1", "status": "queued"}}
            for verb in sorted(BUILD_4):
                queued = getattr(plugin, verb)(obj("delete"), state)
                self.assertEqual(queued["data"], {"job_id": "j1", "status": "queued"})
                self.assertEqual(create_job.call_args.kwargs["action_name"], verb)
            plugin.pipe(obj("insert_files", body=FILES), state)
            pinned = create_job.call_args.kwargs["params"]["query"]["requestId"]
            self.assertRegex(pinned, r"^[0-9a-f-]{36}$")
            self.assertEqual(rest_families.build_request("pipe", create_job.call_args.kwargs["params"])["query"]["requestId"], pinned)

    def test_worker_runs_a_pipe_job_and_completes_it(self) -> None:
        plugin = self.plugin()
        manager = MagicMock()
        payload = obj("insert_files", body=FILES, query={"requestId": "ingest-9"})
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": payload}}
        vendor = {"status": 200, "headers": {}, "body": {"requestId": "ingest-9", "responseCode": "SUCCESS"}, "body_encoding": "json", "correlation_id": "c-1"}
        with patch.object(rest_client, "perform_request", return_value=dict(vendor)) as transport:
            async_jobs._process_job(plugin, manager, "job-1", "pipe")
        self.assertEqual(transport.call_args.args[1]["path"], f"{_INGEST}/insertFiles")
        manager.update_job.assert_called_with("job-1", {"status": "completed", "result": {**vendor, "family": "pipe", "operation": "insert_files", "method": "POST", "request_id": "ingest-9"}})

    def test_completion_template_names_snowpipe_hints_and_queued_is_not_loaded(self) -> None:
        for family in sorted(BUILD_4):
            instructions = " ".join(completion_templates.build_completion_template(family, "result")["params"]["prompt"]["user"]["instructions"])  # type: ignore[index]
            self.assertIn("next_begin_mark", instructions)
            self.assertIn("queued the files, not that they were loaded", instructions)


class Registration(unittest.TestCase):
    def test_each_build_4_verb_declares_exactly_its_vendor_path_parameters(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        scoped_object = ["database", "schema", "name", "result_handler"]
        expected: dict[str, tuple[list[str], bool]] = {"table": (scoped_object, True), "view": (scoped_object, True), "sequence": (scoped_object, False), "pipe": (scoped_object, True)}
        decorated = {action.name: action for action in discover_actions(SnowflakePlugin())}
        edges = SnowflakePlugin().get_edge_process_definitions()
        kb = ROOT / "plugins/snowflake_plugin/knowledge_base/processes"
        for family, (placeholders, tags) in expected.items():
            with self.subTest(family=family):
                self.assertEqual(rest_families.path_parameters(family), placeholders)
                self.assertEqual(rest_families.takes_tags(family), tags)
                self.assertEqual(list(decorated[family].parameters), ["operation", "query", "body", *placeholders, *(["tags"] if tags else [])])
                doc = json.loads((kb / f"{family}.json").read_text())
                self.assertEqual(doc["result_processor_customizations"]["result_type"], f"snowflake_{family}_result")
                self.assertEqual(edges[family].result_processor_template_customizations.result_type, f"snowflake_{family}_result")  # type: ignore[union-attr]
                self.assertTrue(200 <= len(doc["embedding_description"]) <= 400)


if __name__ == "__main__":
    unittest.main()
