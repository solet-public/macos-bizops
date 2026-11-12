"""Offline smoke for the typed Snowflake REST families.

Families: sql_api, task, stream, dynamic_table (build 2) and database, schema,
warehouse, stage (build 3). Transcripts follow the vendor's documented response
shapes (SQL API ResultSet, QueryStatus and QueryFailureStatus; REST
SuccessResponse, SuccessAcceptedResponse and ErrorResponse from
snowflake-rest-api-specs common.yaml; Database, Schema, Warehouse, Stage,
StageFile and FileTransferMaterial from their resource files). No live account.
Build 4's table, view, sequence and pipe families have their own smoke,
smoke_rest_object_families.py, which also checks that the two smokes together
route every declared family.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/snowflake_plugin/src"))

from snowflake_plugin import async_jobs, completion_templates, rest_client, rest_families  # noqa: E402
from snowflake_plugin.app_config import SnowflakeAccountConfig  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402

Handler = Callable[[httpx.Request], httpx.Response]
HANDLE = "01b8c2f4-0000-a1b2-0000-0004a1b2c3d5"
REST_ERROR = {"code": "390189", "error_code": "390189", "message": "vendor detail", "request_id": "9d1f2a54-1111-2222-3333-444455556666"}


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

        def with_transport(config: SnowflakeAccountConfig, request: dict[str, Any], *, timeout_seconds: int, result_hints: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
            return real(config, request, timeout_seconds=timeout_seconds, transport=httpx.MockTransport(handler), result_hints=result_hints)

        with patch.object(rest_client, "perform_request", with_transport):
            outcome = rest_families.run_family(family, self.plugin(), params)
        self.assertEqual(outcome["action_status"], "completed", outcome)
        return outcome["data"]


def scoped(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "database": "DB", "schema": "PUBLIC", "name": "NIGHTLY", **extra}


def db(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "name": "SALES", **extra}


def sch(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "database": "SALES", "name": "RAW", **extra}


def wh(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "name": "LOAD_WH", **extra}


# The families this smoke routes; build 4's families are routed by smoke_rest_object_families.py.
ROUTED_FAMILIES = frozenset({"sql_api", "task", "stream", "dynamic_table", "database", "schema", "warehouse", "stage"})

# Independently written vendor paths: family, params, method, path and query.
_BASE = "/api/v2/databases/DB/schemas/PUBLIC"
ROUTES: list[tuple[str, dict[str, Any], str, str, dict[str, str]]] = [
    ("sql_api", {"operation": "submit", "body": {"statement": "SELECT 1"}, "query": {"requestId": "r-1", "async": True}}, "POST", "/api/v2/statements", {"requestId": "r-1", "async": "true"}),
    ("sql_api", {"operation": "status", "statement_handle": HANDLE, "query": {"partition": 2}}, "GET", f"/api/v2/statements/{HANDLE}", {"partition": "2"}),
    ("sql_api", {"operation": "cancel", "statement_handle": HANDLE}, "POST", f"/api/v2/statements/{HANDLE}/cancel", {}),
    ("sql_api", {"operation": "status", "statement_handle": HANDLE, "query": {"requestId": "r-1"}}, "GET", f"/api/v2/statements/{HANDLE}", {"requestId": "r-1"}),
    ("sql_api", {"operation": "cancel", "statement_handle": HANDLE, "query": {"requestId": "r-2"}}, "POST", f"/api/v2/statements/{HANDLE}/cancel", {"requestId": "r-2"}),
    ("task", {"operation": "list", "database": "DB", "schema": "PUBLIC", "query": {"rootOnly": True, "like": "night%"}}, "GET", f"{_BASE}/tasks", {"rootOnly": "true", "like": "night%"}),
    ("task", {"operation": "create", "database": "DB", "schema": "PUBLIC", "query": {"createMode": "orReplace"}, "body": {"name": "NIGHTLY", "definition": "DELETE FROM T WHERE OLD"}}, "POST", f"{_BASE}/tasks", {"createMode": "orReplace"}),
    ("task", scoped("get"), "GET", f"{_BASE}/tasks/NIGHTLY", {}),
    ("task", scoped("create_or_alter", body={"name": "NIGHTLY", "definition": "SELECT 1"}), "PUT", f"{_BASE}/tasks/NIGHTLY", {}),
    ("task", scoped("delete", query={"ifExists": True}), "DELETE", f"{_BASE}/tasks/NIGHTLY", {"ifExists": "true"}),
    ("task", scoped("execute", query={"retryLast": False, "asyncExec": True}), "POST", f"{_BASE}/tasks/NIGHTLY:execute", {"retryLast": "false", "asyncExec": "true"}),
    ("task", scoped("resume"), "POST", f"{_BASE}/tasks/NIGHTLY:resume", {}),
    ("task", scoped("suspend"), "POST", f"{_BASE}/tasks/NIGHTLY:suspend", {}),
    ("task", scoped("dependents", query={"recursive": True}), "GET", f"{_BASE}/tasks/NIGHTLY/dependents", {"recursive": "true"}),
    ("task", scoped("current_graphs", query={"resultLimit": 5}), "GET", f"{_BASE}/tasks/NIGHTLY/current-graphs", {"resultLimit": "5"}),
    ("task", scoped("complete_graphs", query={"resultLimit": 10000, "errorOnly": True}), "GET", f"{_BASE}/tasks/NIGHTLY/complete-graphs", {"resultLimit": "10000", "errorOnly": "true"}),
    ("task", scoped("set_tags", tags=[{"tag_name": "COST", "tag_value": "ops"}]), "POST", f"{_BASE}/tasks/NIGHTLY:set-tags", {}),
    ("task", scoped("unset_tags", tags=[{"tag_name": "COST"}]), "POST", f"{_BASE}/tasks/NIGHTLY:unset-tags", {}),
    ("task", scoped("set_tags", tags=[{"tag_name": "COST", "tag_value": "ops"}], query={"ifExists": True}), "POST", f"{_BASE}/tasks/NIGHTLY:set-tags", {"ifExists": "true"}),
    ("task", scoped("unset_tags", tags=[{"tag_name": "COST"}], query={"ifExists": False}), "POST", f"{_BASE}/tasks/NIGHTLY:unset-tags", {"ifExists": "false"}),
    ("task", scoped("get_tags", query={"withLineage": True}), "GET", f"{_BASE}/tasks/NIGHTLY:get-tags", {"withLineage": "true"}),
    ("task", {"operation": "fetch_result", "result_handler": "01b8c2f4-r", "query": {"page": 1}}, "GET", "/api/v2/results/01b8c2f4-r", {"page": "1"}),
    ("stream", {"operation": "list", "database": "DB", "schema": "PUBLIC", "query": {"showLimit": 10, "fromName": "S"}}, "GET", f"{_BASE}/streams", {"showLimit": "10", "fromName": "S"}),
    ("stream", {"operation": "create", "database": "DB", "schema": "PUBLIC", "query": {"copyGrants": True}, "body": {"name": "S1", "stream_source": {"src_type": "table", "name": "T"}}}, "POST", f"{_BASE}/streams", {"copyGrants": "true"}),
    ("stream", scoped("get"), "GET", f"{_BASE}/streams/NIGHTLY", {}),
    ("stream", scoped("delete", query={"ifExists": False}), "DELETE", f"{_BASE}/streams/NIGHTLY", {"ifExists": "false"}),
    ("stream", scoped("clone", query={"targetDatabase": "DB2", "targetSchema": "S2"}, body={"name": "COPY"}), "POST", f"{_BASE}/streams/NIGHTLY:clone", {"targetDatabase": "DB2", "targetSchema": "S2"}),
    ("stream", scoped("set_tags", tags=[{"tag_name": "PII", "tag_value": "no"}]), "POST", f"{_BASE}/streams/NIGHTLY:set-tags", {}),
    ("stream", scoped("unset_tags", tags=[{"tag_name": "PII"}]), "POST", f"{_BASE}/streams/NIGHTLY:unset-tags", {}),
    ("stream", scoped("set_tags", tags=[{"tag_name": "PII", "tag_value": "no"}], query={"ifExists": True}), "POST", f"{_BASE}/streams/NIGHTLY:set-tags", {"ifExists": "true"}),
    ("stream", scoped("unset_tags", tags=[{"tag_name": "PII"}], query={"ifExists": True}), "POST", f"{_BASE}/streams/NIGHTLY:unset-tags", {"ifExists": "true"}),
    ("stream", scoped("get_tags"), "GET", f"{_BASE}/streams/NIGHTLY:get-tags", {}),
    ("stream", {"operation": "fetch_result", "result_handler": "01b8c2f4-r"}, "GET", "/api/v2/results/01b8c2f4-r", {}),
    ("dynamic_table", {"operation": "list", "database": "DB", "schema": "PUBLIC", "query": {"deep": True}}, "GET", f"{_BASE}/dynamic-tables", {"deep": "true"}),
    ("dynamic_table", {"operation": "create", "database": "DB", "schema": "PUBLIC", "body": {"name": "DT", "target_lag": {"type": "USER_DEFINED", "seconds": 60}, "warehouse": "WH", "query": "SELECT * FROM T"}}, "POST", f"{_BASE}/dynamic-tables", {}),
    ("dynamic_table", scoped("get"), "GET", f"{_BASE}/dynamic-tables/NIGHTLY", {}),
    ("dynamic_table", scoped("delete"), "DELETE", f"{_BASE}/dynamic-tables/NIGHTLY", {}),
    ("dynamic_table", scoped("clone", query={"createMode": "ifNotExists"}, body={"name": "DT2"}), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:clone", {"createMode": "ifNotExists"}),
    ("dynamic_table", scoped("undrop"), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:undrop", {}),
    ("dynamic_table", scoped("suspend"), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:suspend", {}),
    ("dynamic_table", scoped("resume", query={"ifExists": True}), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:resume", {"ifExists": "true"}),
    ("dynamic_table", scoped("refresh"), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:refresh", {}),
    ("dynamic_table", scoped("suspend_recluster"), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:suspend-recluster", {}),
    ("dynamic_table", scoped("resume_recluster"), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:resume-recluster", {}),
    ("dynamic_table", scoped("swap_with", query={"targetName": "DT_NEXT"}), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:swap-with", {"targetName": "DT_NEXT"}),
    ("dynamic_table", scoped("set_tags", tags=[{"tag_name": "TIER", "tag_value": "gold"}], query={"ifExists": True}), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:set-tags", {"ifExists": "true"}),
    ("dynamic_table", scoped("unset_tags", tags=[{"tag_name": "TIER"}]), "POST", f"{_BASE}/dynamic-tables/NIGHTLY:unset-tags", {}),
    ("dynamic_table", scoped("get_tags"), "GET", f"{_BASE}/dynamic-tables/NIGHTLY:get-tags", {}),
    ("dynamic_table", {"operation": "fetch_result", "result_handler": "01b8c2f4-r"}, "GET", "/api/v2/results/01b8c2f4-r", {}),
    ("database", {"operation": "list", "query": {"like": "sales%", "startsWith": "SA", "showLimit": 2, "fromName": "A", "history": True}}, "GET", "/api/v2/databases", {"like": "sales%", "startsWith": "SA", "showLimit": "2", "fromName": "A", "history": "true"}),
    ("database", {"operation": "create", "query": {"createMode": "ifNotExists", "kind": "transient"}, "body": {"name": "SCRATCH", "comment": "load area"}}, "POST", "/api/v2/databases", {"createMode": "ifNotExists", "kind": "transient"}),
    ("database", {"operation": "create_from_share", "query": {"createMode": "errorIfExists", "share": "PROVIDER_ACCT.SALES_SHARE"}, "body": {"name": "SHARED_SALES"}}, "POST", "/api/v2/databases:from-share", {"createMode": "errorIfExists", "share": "PROVIDER_ACCT.SALES_SHARE"}),
    ("database", db("clone", query={"kind": ""}, body={"name": "SALES_DEV", "point_of_time": {"point_of_time_type": "offset", "reference": "at", "offset": "-3600"}}), "POST", "/api/v2/databases/SALES:clone", {"kind": ""}),
    ("database", db("get"), "GET", "/api/v2/databases/SALES", {}),
    ("database", db("create_or_alter", body={"name": "SALES", "data_retention_time_in_days": 7}), "PUT", "/api/v2/databases/SALES", {}),
    ("database", db("delete", query={"ifExists": True, "restrict": False}), "DELETE", "/api/v2/databases/SALES", {"ifExists": "true", "restrict": "false"}),
    ("database", db("undrop"), "POST", "/api/v2/databases/SALES:undrop", {}),
    ("database", db("enable_replication", query={"ignore_edition_check": True}, body={"accounts": ["MYORG.DR_ACCOUNT"]}), "POST", "/api/v2/databases/SALES/replication:enable", {"ignore_edition_check": "true"}),
    ("database", db("disable_replication", body={"accounts": ["MYORG.DR_ACCOUNT"]}), "POST", "/api/v2/databases/SALES/replication:disable", {}),
    ("database", db("disable_replication"), "POST", "/api/v2/databases/SALES/replication:disable", {}),
    ("database", db("refresh_replication"), "POST", "/api/v2/databases/SALES/replication:refresh", {}),
    ("database", db("enable_failover", body={"accounts": ["MYORG.DR_ACCOUNT"]}), "POST", "/api/v2/databases/SALES/failover:enable", {}),
    ("database", db("disable_failover"), "POST", "/api/v2/databases/SALES/failover:disable", {}),
    ("database", db("primary_failover"), "POST", "/api/v2/databases/SALES/failover:primary", {}),
    ("database", db("set_tags", tags=[{"tag_name": "COST", "tag_value": "fin"}], query={"ifExists": True}), "POST", "/api/v2/databases/SALES:set-tags", {"ifExists": "true"}),
    ("database", db("unset_tags", tags=[{"tag_name": "COST"}]), "POST", "/api/v2/databases/SALES:unset-tags", {}),
    ("database", db("get_tags", query={"withLineage": False}), "GET", "/api/v2/databases/SALES:get-tags", {"withLineage": "false"}),
    ("database", {"operation": "fetch_result", "result_handler": "01b8c2f4-r"}, "GET", "/api/v2/results/01b8c2f4-r", {}),
    ("schema", {"operation": "list", "database": "SALES", "query": {"showLimit": 10000, "history": False}}, "GET", "/api/v2/databases/SALES/schemas", {"showLimit": "10000", "history": "false"}),
    ("schema", {"operation": "create", "database": "SALES", "query": {"createMode": "orReplace", "kind": "transient"}, "body": {"name": "STAGING", "managed_access": True}}, "POST", "/api/v2/databases/SALES/schemas", {"createMode": "orReplace", "kind": "transient"}),
    ("schema", sch("clone", query={"targetDatabase": "SALES_DEV", "kind": ""}, body={"name": "RAW_COPY"}), "POST", "/api/v2/databases/SALES/schemas/RAW:clone", {"targetDatabase": "SALES_DEV", "kind": ""}),
    ("schema", sch("undrop"), "POST", "/api/v2/databases/SALES/schemas/RAW:undrop", {}),
    ("schema", sch("get"), "GET", "/api/v2/databases/SALES/schemas/RAW", {}),
    ("schema", sch("create_or_alter", query={"kind": "permanent"}, body={"name": "RAW", "comment": "landing"}), "PUT", "/api/v2/databases/SALES/schemas/RAW", {"kind": "permanent"}),
    ("schema", sch("delete", query={"restrict": True}), "DELETE", "/api/v2/databases/SALES/schemas/RAW", {"restrict": "true"}),
    ("schema", sch("set_tags", tags=[{"tag_name": "PII", "tag_value": "yes"}]), "POST", "/api/v2/databases/SALES/schemas/RAW:set-tags", {}),
    ("schema", sch("unset_tags", tags=[{"tag_name": "PII"}], query={"ifExists": True}), "POST", "/api/v2/databases/SALES/schemas/RAW:unset-tags", {"ifExists": "true"}),
    ("schema", sch("get_tags"), "GET", "/api/v2/databases/SALES/schemas/RAW:get-tags", {}),
    ("schema", {"operation": "fetch_result", "result_handler": "01b8c2f4-r", "query": {"page": 0}}, "GET", "/api/v2/results/01b8c2f4-r", {"page": "0"}),
    ("warehouse", {"operation": "list", "query": {"like": "load%"}}, "GET", "/api/v2/warehouses", {"like": "load%"}),
    ("warehouse", {"operation": "create", "query": {"createMode": "ifNotExists"}, "body": {"name": "LOAD_WH", "warehouse_size": "XSMALL", "auto_suspend": 60, "initially_suspended": "true"}}, "POST", "/api/v2/warehouses", {"createMode": "ifNotExists"}),
    ("warehouse", wh("get"), "GET", "/api/v2/warehouses/LOAD_WH", {}),
    ("warehouse", wh("create_or_alter", body={"name": "LOAD_WH", "warehouse_size": "SMALL"}), "PUT", "/api/v2/warehouses/LOAD_WH", {}),
    ("warehouse", wh("delete", query={"ifExists": True}), "DELETE", "/api/v2/warehouses/LOAD_WH", {"ifExists": "true"}),
    ("warehouse", wh("resume", query={"ifExists": True}), "POST", "/api/v2/warehouses/LOAD_WH:resume", {"ifExists": "true"}),
    ("warehouse", wh("suspend"), "POST", "/api/v2/warehouses/LOAD_WH:suspend", {}),
    ("warehouse", wh("abort", query={"ifExists": False}), "POST", "/api/v2/warehouses/LOAD_WH:abort", {"ifExists": "false"}),
    ("warehouse", wh("enable"), "POST", "/api/v2/warehouses/LOAD_WH:enable", {}),
    ("warehouse", wh("disable", query={"ifExists": True}), "POST", "/api/v2/warehouses/LOAD_WH:disable", {"ifExists": "true"}),
    ("warehouse", wh("rename", query={"ifExists": True}, body={"name": "INGEST_WH"}), "POST", "/api/v2/warehouses/LOAD_WH:rename", {"ifExists": "true"}),
    ("warehouse", wh("set_tags", tags=[{"tag_name": "TEAM", "tag_value": "data"}]), "POST", "/api/v2/warehouses/LOAD_WH:set-tags", {}),
    ("warehouse", wh("unset_tags", tags=[{"tag_name": "TEAM"}]), "POST", "/api/v2/warehouses/LOAD_WH:unset-tags", {}),
    ("warehouse", wh("get_tags", query={"withLineage": True}), "GET", "/api/v2/warehouses/LOAD_WH:get-tags", {"withLineage": "true"}),
    ("warehouse", {"operation": "fetch_result", "result_handler": "01b8c2f4-r"}, "GET", "/api/v2/results/01b8c2f4-r", {}),
    ("stage", {"operation": "list", "database": "DB", "schema": "PUBLIC", "query": {"like": "land%"}}, "GET", f"{_BASE}/stages", {"like": "land%"}),
    ("stage", {"operation": "create", "database": "DB", "schema": "PUBLIC", "query": {"createMode": "orReplace"}, "body": {"name": "LANDING", "kind": "PERMANENT", "directory_table": {"enable": True}}}, "POST", f"{_BASE}/stages", {"createMode": "orReplace"}),
    ("stage", scoped("get"), "GET", f"{_BASE}/stages/NIGHTLY", {}),
    ("stage", scoped("delete", query={"ifExists": True}), "DELETE", f"{_BASE}/stages/NIGHTLY", {"ifExists": "true"}),
    ("stage", scoped("list_files", query={"pattern": ".*part_.*[.]csv"}), "GET", f"{_BASE}/stages/NIGHTLY/files", {"pattern": ".*part_.*[.]csv"}),
    ("stage", scoped("presigned_url", file_path="2026/11/part 0.csv", body={"expiration_time": 600}), "POST", f"{_BASE}/stages/NIGHTLY/files/2026%2F11%2Fpart%200.csv:presigned-url", {}),
    ("stage", scoped("presigned_url", file_path="part_0.csv"), "POST", f"{_BASE}/stages/NIGHTLY/files/part_0.csv:presigned-url", {}),
    ("stage", {"operation": "fetch_result", "result_handler": "01b8c2f4-r"}, "GET", "/api/v2/results/01b8c2f4-r", {}),
]


class Routing(FamilyFixture):
    def test_every_operation_has_an_independently_written_route(self) -> None:
        covered = {(family, params["operation"]) for family, params, *_ in ROUTES}
        declared = {(family, operation) for family, operations in rest_families.FAMILIES.items() if family in ROUTED_FAMILIES for operation in operations}
        self.assertEqual(covered, declared)

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
                sent = dict(request.url.params)
                if family == "sql_api" and params["operation"] == "submit":
                    self.assertEqual(data["request_id"], sent["requestId"])
                self.assertEqual(sent, query)
                expected_body = params.get("body", params.get("tags"))
                self.assertEqual(json.loads(request.content) if request.content else None, expected_body)
                self.assertEqual((data["family"], data["operation"], data["method"], data["status"]), (family, params["operation"], method, 200))

    def test_quoted_identifiers_are_percent_encoded_path_segments(self) -> None:
        seen: list[str] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.raw_path.decode())
            return httpx.Response(200, json={"status": "ok"})

        self.run_family("task", {"operation": "delete", "database": '"my db"', "schema": '"a/b"', "name": '"say ""hi"""'}, vendor)
        self.assertEqual(seen, ["/api/v2/databases/" + quote('"my db"', safe="") + "/schemas/" + quote('"a/b"', safe="") + "/tasks/" + quote('"say ""hi"""', safe="")])

    def test_sql_api_generates_request_id_and_keeps_caller_id(self) -> None:
        generated = rest_families.build_request("sql_api", {"operation": "submit", "body": {"statement": "DELETE FROM T"}})
        self.assertRegex(generated["query"]["requestId"], r"^[0-9a-f-]{36}$")
        kept = rest_families.build_request("sql_api", {"operation": "submit", "body": {"statement": "DELETE FROM T"}, "query": {"requestId": "mine", "retry": True}})
        self.assertEqual(kept["query"], {"requestId": "mine", "retry": "true"})


class Validation(unittest.TestCase):
    def test_vendor_invalid_input_is_refused_before_any_call(self) -> None:
        cases: list[tuple[str, dict[str, Any], str]] = [
            ("task", {"operation": "drop_everything"}, "operation must be one of"),
            ("task", scoped("get", database="1bad"), "database must be a Snowflake identifier"),
            ("task", scoped("get", name="a.b"), "name must be a Snowflake identifier"),
            ("task", {"operation": "list", "database": "DB"}, "schema must be"),
            ("sql_api", {"operation": "status", "statement_handle": "../../api/v2/x"}, "statement_handle must be an opaque vendor handle"),
            ("task", {"operation": "fetch_result", "result_handler": "a/b"}, "result_handler must be"),
            ("task", scoped("get", query={"showLimit": 1}), "not documented for this operation"),
            ("task", scoped("execute", query={"retryLast": "yes"}), "retryLast must be a boolean"),
            ("stream", {"operation": "list", "database": "DB", "schema": "S", "query": {"showLimit": True}}, "showLimit must be an integer"),
            ("task", scoped("complete_graphs", query={"resultLimit": 10001}), "between 1 and 10000"),
            ("task", scoped("complete_graphs", query={"resultLimit": 0}), "between 1 and 10000"),
            ("task", scoped("current_graphs", query={"resultLimit": 10001}), "between 1 and 10000"),
            ("dynamic_table", {"operation": "create", "database": "DB", "schema": "S", "query": {"createMode": "replace"}, "body": {"name": "D", "target_lag": {}, "warehouse": "W", "query": "Q"}}, "createMode must be one of"),
            ("dynamic_table", {"operation": "create", "database": "DB", "schema": "S", "body": {"name": "D", "warehouse": "W", "query": "Q"}}, "missing required members: target_lag"),
            ("stream", {"operation": "create", "database": "DB", "schema": "S", "body": {"name": "S1"}}, "missing required members: stream_source"),
            ("task", scoped("create_or_alter"), "body must be a JSON object"),
            ("sql_api", {"operation": "submit", "body": {"statement": ""}}, "missing required members: statement"),
            ("task", scoped("get", body={"name": "x"}), "takes no body object"),
            ("task", scoped("set_tags", tags=[{"tag_name": "COST"}]), "non-empty string tag_value"),
            ("task", scoped("unset_tags", tags=[]), "non-empty array of tag objects"),
            ("task", scoped("get", tags=[{"tag_name": "COST"}]), "only accepted by set_tags"),
            ("dynamic_table", scoped("swap_with"), "['targetName'] are required"),
            ("task", scoped("get", query=["not", "an", "object"]), "query must be an object"),
            ("database", {"operation": "list", "query": {"showLimit": 0}}, "showLimit must be an integer between 1 and 10000"),
            ("schema", {"operation": "list", "database": "SALES", "query": {"showLimit": 10001}}, "showLimit must be an integer between 1 and 10000"),
            ("database", {"operation": "create", "query": {"kind": 1}, "body": {"name": "X"}}, "kind must be a string"),
            ("database", {"operation": "create", "body": {"comment": "no name"}}, "missing required members: name"),
            ("database", db("enable_replication", body={"accounts": []}), "missing required members: accounts"),
            ("database", db("enable_failover"), "body must be a JSON object"),
            ("database", db("disable_failover", body=["MYORG.DR"]), "body must be a JSON object"),
            ("database", db("delete", query={"restrict": "yes"}), "restrict must be a boolean"),
            ("database", db("get", query={"history": True}), "not documented for this operation"),
            ("database", {"operation": "get", "name": "1SALES"}, "name must be a Snowflake identifier"),
            ("schema", {"operation": "get", "name": "RAW"}, "database must be a Snowflake identifier"),
            ("schema", sch("clone", query={"targetSchema": "X"}, body={"name": "Y"}), "not documented for this operation"),
            ("warehouse", {"operation": "list", "query": {"showLimit": 5}}, "not documented for this operation"),
            ("warehouse", wh("rename", body={}), "missing required members: name"),
            ("warehouse", wh("use"), "operation must be one of"),
            ("warehouse", wh("resume", tags=[{"tag_name": "T"}]), "only accepted by set_tags"),
            ("stage", scoped("presigned_url"), "file_path must be a non-empty stage file path"),
            ("stage", scoped("presigned_url", file_path="a\nb.csv"), "without control characters"),
            ("stage", scoped("presigned_url", file_path="part.csv", body="600"), "body must be a JSON object"),
            ("stage", scoped("list_files", query={"pattern": ""}), "pattern must be a non-empty string"),
            ("stage", scoped("list_files", body={"pattern": "x"}), "takes no body object"),
            ("stage", scoped("set_tags", tags=[{"tag_name": "T", "tag_value": "v"}]), "operation must be one of"),
        ]
        for family, params, message in cases:
            with self.subTest(params=params):
                with self.assertRaises(rest_families.RestFamilyError) as raised:
                    rest_families.build_request(family, params)
                self.assertIn(message, str(raised.exception))

    def test_worker_maps_refusal_to_invalid_params_without_transport(self) -> None:
        plugin = SnowflakePlugin()
        plugin._app_config_loader = MagicMock()
        with patch.object(rest_client, "perform_request") as transport:
            outcome = rest_families.run_family("task", plugin, {"operation": "nope"})
        transport.assert_not_called()
        self.assertEqual(outcome["error"]["code"], "snowflake.invalid_params")


class Transcripts(FamilyFixture):
    def test_sql_api_partitioned_result_set_and_next_partition(self) -> None:
        result_set = {
            "resultSetMetaData": {
                "numRows": 5,
                "format": "jsonv2",
                "partitionInfo": [{"rowCount": 2, "uncompressedSize": 56}, {"rowCount": 2, "uncompressedSize": 56, "compressedSize": 40}, {"rowCount": 1, "uncompressedSize": 28, "compressedSize": 30}],
                "rowType": [{"name": "ID", "database": "DB", "schema": "PUBLIC", "table": "T", "type": "fixed", "scale": 0, "precision": 38, "nullable": False, "byteLength": None, "length": None, "collation": None}],
            },
            "data": [["1"], ["2"]],
            "code": "090001",
            "statementStatusUrl": f"/api/v2/statements/{HANDLE}?requestId=r-1",
            "requestId": "r-1",
            "sqlState": "00000",
            "statementHandle": HANDLE,
            "message": "Statement executed successfully.",
            "createdOn": 1700000000000,
        }
        data = self.run_family("sql_api", {"operation": "submit", "body": {"statement": "SELECT ID FROM T"}}, lambda _: httpx.Response(200, json=result_set))
        self.assertEqual(data["pagination"], {"partition": 0, "partition_count": 3, "next_partition": 1, "statement_handle": HANDLE})
        middle = self.run_family("sql_api", {"operation": "status", "statement_handle": HANDLE, "query": {"partition": 1}}, lambda _: httpx.Response(200, json=result_set))
        self.assertEqual(middle["pagination"]["next_partition"], 2)
        # Later partitions carry rows only (the vendor sends no metadata), so no hint is invented.
        last = self.run_family("sql_api", {"operation": "status", "statement_handle": HANDLE, "query": {"partition": 2}}, lambda _: httpx.Response(200, json={"data": [["5"]]}))
        self.assertNotIn("pagination", last)
        self.assertEqual(last["body"], {"data": [["5"]]})

    def test_sql_api_multi_statement_delete_script_returns_every_handle(self) -> None:
        handles = [HANDLE, HANDLE.replace("d5", "d6"), HANDLE.replace("d5", "d7")]
        body = {
            "resultSetMetaData": {"numRows": 1, "format": "jsonv2", "partitionInfo": [{"rowCount": 1, "uncompressedSize": 57}], "rowType": [{"name": "multiple statement execution", "type": "text", "nullable": False}]},
            "data": [["Multiple statements executed successfully."]],
            "code": "090001",
            "statementHandles": handles,
            "sqlState": "00000",
            "statementHandle": HANDLE.replace("d5", "d4"),
            "message": "Statement executed successfully.",
        }
        sent: list[Any] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            sent.append(json.loads(request.content))
            return httpx.Response(200, json=body)

        statement = "INSERT INTO SCRATCH.PUBLIC.T VALUES (7); DELETE FROM SCRATCH.PUBLIC.T WHERE ID = 7; SELECT COUNT(*) FROM SCRATCH.PUBLIC.T"
        data = self.run_family("sql_api", {"operation": "submit", "body": {"statement": statement, "parameters": {"MULTI_STATEMENT_COUNT": "3"}}}, vendor)
        self.assertEqual(sent, [{"statement": statement, "parameters": {"MULTI_STATEMENT_COUNT": "3"}}])
        self.assertEqual(data["pagination"]["statement_handles"], handles)
        self.assertNotIn("next_partition", data["pagination"])

    def test_sql_api_in_progress_failure_and_cancel(self) -> None:
        running = {"code": "333334", "message": "Asynchronous execution in progress. Use provided query id to perform query monitoring and management.", "statementHandle": HANDLE, "statementStatusUrl": f"/api/v2/statements/{HANDLE}"}
        data = self.run_family("sql_api", {"operation": "submit", "body": {"statement": "CALL LONG_PROC()"}, "query": {"async": True}}, lambda _: httpx.Response(202, json=running))
        self.assertEqual((data["status"], data["pagination"]), (202, {"statement_handle": HANDLE}))
        self.assertNotIn("error_code", data)
        failed = {"code": "002003", "message": "SQL compilation error:\nTable 'T' does not exist or not authorized.", "sqlState": "42S02", "statementHandle": HANDLE}
        data = self.run_family("sql_api", {"operation": "status", "statement_handle": HANDLE}, lambda _: httpx.Response(422, json=failed))
        self.assertEqual((data["status"], data["error_code"], data["body"]), (422, "snowflake.statement_failed", failed))
        cancelled = {"code": "000604", "message": "Statement cancelled.", "sqlState": "57014", "statementHandle": HANDLE, "statementStatusUrl": f"/api/v2/statements/{HANDLE}"}
        data = self.run_family("sql_api", {"operation": "cancel", "statement_handle": HANDLE}, lambda _: httpx.Response(200, json=cancelled))
        self.assertEqual(data["body"]["message"], "Statement cancelled.")

    def test_task_list_pages_with_from_name_and_link_relations(self) -> None:
        tasks = [
            {"name": "A_TASK", "database_name": "DB", "schema_name": "PUBLIC", "warehouse": "WH", "schedule": {"schedule_type": "MINUTES_TYPE", "minutes": 60}, "definition": "DELETE FROM T WHERE TS < CURRENT_DATE - 30", "state": "started", "owner": "ROLE", "owner_role_type": "ROLE"},
            {"name": "B_TASK", "database_name": "DB", "schema_name": "PUBLIC", "definition": "CALL P()", "predecessors": ["DB.PUBLIC.A_TASK"], "state": "suspended", "owner": "ROLE", "owner_role_type": "ROLE"},
        ]
        headers = {"Link": '</api/v2/results/01b8-r?page=1>; rel="next", </api/v2/results/01b8-r?page=0>; rel="first"', "X-Snowflake-Request-ID": "9d1f2a54-1111-2222-3333-444455556666"}
        data = self.run_family("task", {"operation": "list", "database": "DB", "schema": "PUBLIC", "query": {"showLimit": 2}}, lambda _: httpx.Response(200, json=tasks, headers=headers))
        self.assertEqual(data["body"], tasks)
        self.assertEqual(data["pagination"], {"links": {"next": "/api/v2/results/01b8-r?page=1", "first": "/api/v2/results/01b8-r?page=0"}, "next_from_name": "B_TASK"})
        short = self.run_family("task", {"operation": "list", "database": "DB", "schema": "PUBLIC", "query": {"showLimit": 3}}, lambda _: httpx.Response(200, json=tasks))
        self.assertNotIn("pagination", short, "a short page is the last page")

    def test_accepted_202_exposes_result_handler_for_fetch_result(self) -> None:
        accepted = {"code": "392604", "message": "Request execution in progress. Use the provided location header or result handler ID to perform query monitoring and management.", "resultHandler": "01b8c2f4-0000-r", "job_id": "01b8c2f4-0000"}
        data = self.run_family("dynamic_table", scoped("refresh"), lambda _: httpx.Response(202, json=accepted, headers={"Location": "/api/v2/results/01b8c2f4-0000-r"}))
        self.assertEqual(data["pagination"], {"location": "/api/v2/results/01b8c2f4-0000-r", "result_handler": "01b8c2f4-0000-r"})
        done = self.run_family("dynamic_table", {"operation": "fetch_result", "result_handler": data["pagination"]["result_handler"]}, lambda _: httpx.Response(200, json={"status": "Dynamic table NIGHTLY refreshed."}))
        self.assertEqual(done["body"], {"status": "Dynamic table NIGHTLY refreshed."})

    def test_task_execute_and_delete_success_messages_are_data(self) -> None:
        for operation, message in (("execute", "Task NIGHTLY is scheduled to run immediately."), ("delete", "NIGHTLY successfully dropped.")):
            with self.subTest(operation=operation):
                data = self.run_family("task", scoped(operation), lambda _, message=message: httpx.Response(200, json={"status": message}))
                self.assertEqual(data["body"], {"status": message})
                self.assertNotIn("error_code", data)

    def test_vendor_errors_map_to_typed_codes(self) -> None:
        for family, status, code in (
            ("task", 400, "snowflake.api_error"),
            ("stream", 401, "snowflake.auth_failed"),
            ("dynamic_table", 403, "snowflake.permission_denied"),
            ("task", 404, "snowflake.object_not_found"),
            ("stream", 408, "snowflake.timeout"),
            ("dynamic_table", 409, "snowflake.conflict"),
            ("task", 422, "snowflake.api_error"),
            ("sql_api", 429, "snowflake.rate_limited"),
            ("dynamic_table", 503, "snowflake.api_error"),
        ):
            with self.subTest(family=family, status=status):
                params = {"operation": "status", "statement_handle": HANDLE} if family == "sql_api" else scoped("get")
                data = self.run_family(family, params, lambda _, status=status: httpx.Response(status, json=REST_ERROR, headers={"Retry-After": "11"}))
                self.assertEqual((data["status"], data["error_code"], data["body"]), (status, code, REST_ERROR))
                self.assertEqual(data["headers"]["retry-after"], "11")

    def test_transport_failure_is_generic(self) -> None:
        with patch.object(rest_client, "perform_request", side_effect=httpx.ConnectError("boom at /?token=secret")):
            outcome = rest_families.run_family("task", self.plugin(), scoped("delete"))
        self.assertEqual(outcome["error"], {"code": "snowflake.api_error", "message": "Snowflake REST request failed; vendor state may have changed"})


class ResourceTranscripts(FamilyFixture):
    """Build 3 resource families against their documented vendor response shapes."""

    def test_database_list_pages_with_from_name_and_includes_dropped_history(self) -> None:
        databases = [
            {"name": "ANALYTICS", "kind": "PERMANENT", "created_on": "2026-01-04T10:11:12.000-08:00", "is_default": False, "is_current": False, "origin": "", "owner": "SYSADMIN", "comment": "", "options": "", "retention_time": 1, "owner_role_type": "ROLE"},
            {"name": "OLD_SALES", "kind": "TRANSIENT", "created_on": "2025-03-01T08:00:00.000-08:00", "dropped_on": "2026-11-01T09:00:00.000-07:00", "is_default": False, "is_current": False, "owner": "SYSADMIN", "options": "TRANSIENT", "retention_time": 0, "owner_role_type": "ROLE"},
        ]
        sent: list[dict[str, str]] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            sent.append(dict(request.url.params))
            return httpx.Response(200, json=databases, headers={"X-Snowflake-Request-ID": "9d1f2a54-1111-2222-3333-444455556666"})

        data = self.run_family("database", {"operation": "list", "query": {"showLimit": 2, "history": True}}, vendor)
        self.assertEqual(data["body"], databases)
        self.assertEqual(data["pagination"], {"next_from_name": "OLD_SALES"})
        following = self.run_family("database", {"operation": "list", "query": {"showLimit": 2, "history": True, "fromName": data["pagination"]["next_from_name"]}}, lambda _: httpx.Response(200, json=[]))
        self.assertEqual(sent, [{"showLimit": "2", "history": "true"}])
        self.assertNotIn("pagination", following, "an empty page ends the listing")

    def test_schema_create_conflict_then_or_replace_success(self) -> None:
        conflict = {"error_code": "002002", "code": "002002", "message": "SQL compilation error:\nObject 'STAGING' already exists.", "request_id": "1e1c5d0a-2222-3333-4444-555566667777"}
        data = self.run_family("schema", {"operation": "create", "database": "SALES", "body": {"name": "STAGING"}}, lambda _: httpx.Response(409, json=conflict))
        self.assertEqual((data["status"], data["error_code"], data["body"]), (409, "snowflake.conflict", conflict))
        done = self.run_family("schema", {"operation": "create", "database": "SALES", "query": {"createMode": "orReplace"}, "body": {"name": "STAGING"}}, lambda _: httpx.Response(200, json={"status": "Schema STAGING successfully created."}))
        self.assertEqual((done["status"], done["body"]), (200, {"status": "Schema STAGING successfully created."}))
        self.assertNotIn("error_code", done)

    def test_warehouse_get_suspend_and_drop_are_vendor_data(self) -> None:
        warehouse = {
            "name": "LOAD_WH",
            "state": "SUSPENDED",
            "type": "STANDARD",
            "size": "XSMALL",
            "warehouse_size": "XSMALL",
            "warehouse_type": "STANDARD",
            "min_cluster_count": 1,
            "max_cluster_count": 1,
            "started_clusters": 0,
            "running": 0,
            "queued": 0,
            "auto_suspend": 60,
            "auto_resume": "true",
            "available": "",
            "provisioning": "",
            "quiescing": "",
            "created_on": "2026-02-02T02:02:02.000-08:00",
            "owner": "SYSADMIN",
            "comment": "",
            "budget": None,
            "owner_role_type": "ROLE",
        }
        got = self.run_family("warehouse", wh("get"), lambda _: httpx.Response(200, json=warehouse))
        self.assertEqual(got["body"], warehouse)
        for operation, message in (("suspend", "Statement executed successfully."), ("abort", "Statement executed successfully."), ("delete", "LOAD_WH successfully dropped.")):
            with self.subTest(operation=operation):
                data = self.run_family("warehouse", wh(operation), lambda _, message=message: httpx.Response(200, json={"status": message}))
                self.assertEqual((data["method"], data["body"]), ("DELETE" if operation == "delete" else "POST", {"status": message}))
        missing = {"error_code": "002003", "code": "002003", "message": "Warehouse 'LOAD_WH' does not exist or not authorized.", "request_id": "0a0b0c0d-1111-2222-3333-444455556666"}
        gone = self.run_family("warehouse", wh("resume"), lambda _: httpx.Response(404, json=missing))
        self.assertEqual((gone["error_code"], gone["body"]), ("snowflake.object_not_found", missing))

    def test_stage_list_files_and_presigned_url(self) -> None:
        files = [
            {"name": "landing/2026/11/part_0.csv.gz", "size": "1048576", "md5": "8a1f6c2e4b0d9e7f3a5c1b2d4e6f8a0c", "last_modified": "Fri, 6 Nov 2026 18:46:17 GMT"},
            {"name": "landing/2026/11/part_1.csv.gz", "size": "524288", "md5": "1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f", "last_modified": "Fri, 6 Nov 2026 18:47:02 GMT"},
        ]
        link = '</api/v2/results/01b8c2f4-0000-r?page=1>; rel="next"'
        listed = self.run_family("stage", scoped("list_files", query={"pattern": ".*part_.*"}), lambda _: httpx.Response(200, json=files, headers={"Link": link}))
        self.assertEqual(listed["body"], files)
        self.assertEqual(listed["pagination"], {"links": {"next": "/api/v2/results/01b8c2f4-0000-r?page=1"}})
        material = {"presigned_url": "https://sfc-stage.s3.us-west-2.amazonaws.com/stages/1a2b/landing/2026/11/part_0.csv.gz?X-Amz-Expires=600&X-Amz-Signature=5e0f"}
        sent: list[Any] = []

        def vendor(request: httpx.Request) -> httpx.Response:
            sent.append((request.url.raw_path.decode(), json.loads(request.content)))
            return httpx.Response(200, json=material)

        data = self.run_family("stage", scoped("presigned_url", file_path="landing/2026/11/part_0.csv.gz", body={"expiration_time": 600}), vendor)
        self.assertEqual(sent, [(f"{_BASE}/stages/NIGHTLY/files/landing%2F2026%2F11%2Fpart_0.csv.gz:presigned-url", {"expiration_time": 600})])
        self.assertEqual(data["body"], material, "the URL the caller asked for is the operation's product")

    def test_database_replication_accepted_then_fetch_result(self) -> None:
        accepted = {"code": "392604", "message": "Request execution in progress. Use the provided location header or result handler ID to perform query monitoring and management.", "resultHandler": "01afef9d-0607-0550-0001-dd270c3902d7-r", "job_id": "01afef9d-0607-0550-0001-dd270c3902d7"}
        data = self.run_family("database", db("enable_replication", body={"accounts": ["MYORG.DR_ACCOUNT"]}), lambda _: httpx.Response(202, json=accepted, headers={"Location": "/api/v2/results/01afef9d-0607-0550-0001-dd270c3902d7-r"}))
        self.assertEqual(data["pagination"]["result_handler"], "01afef9d-0607-0550-0001-dd270c3902d7-r")
        done = self.run_family("database", {"operation": "fetch_result", "result_handler": data["pagination"]["result_handler"]}, lambda _: httpx.Response(200, json={"status": "Statement executed successfully."}))
        self.assertEqual(done["body"], {"status": "Statement executed successfully."})

    def test_resource_vendor_errors_map_to_typed_codes(self) -> None:
        for family, params, status, code in (
            ("database", db("delete"), 401, "snowflake.auth_failed"),
            ("schema", sch("delete"), 403, "snowflake.permission_denied"),
            ("warehouse", wh("delete"), 403, "snowflake.permission_denied"),
            ("stage", scoped("delete"), 401, "snowflake.auth_failed"),
            ("database", db("undrop"), 409, "snowflake.conflict"),
            ("stage", scoped("list_files"), 429, "snowflake.rate_limited"),
        ):
            with self.subTest(family=family, status=status):
                data = self.run_family(family, params, lambda _, status=status: httpx.Response(status, json=REST_ERROR))
                self.assertEqual((data["status"], data["error_code"], data["body"]), (status, code, REST_ERROR))


class Plugin(FamilyFixture):
    def test_dispatch_refuses_before_job_and_queues_valid_input(self) -> None:
        plugin = self.plugin()
        with patch.object(async_jobs, "create_job") as create_job:
            refused = plugin.task({"operation": "delete", "database": "DB", "schema": "S", "name": "bad name"}, {})
            create_job.assert_not_called()
            self.assertEqual(refused["error"]["code"], "snowflake.invalid_params")
            create_job.return_value = {"action_status": "completed", "data": {"job_id": "j1", "status": "queued"}}
            deletes = (("sql_api", {"operation": "cancel", "statement_handle": HANDLE}), ("task", scoped("delete")), ("stream", scoped("delete")), ("dynamic_table", scoped("delete")), ("database", db("delete")), ("schema", sch("delete")), ("warehouse", wh("delete")), ("stage", scoped("delete")))
            for verb, params in deletes:
                queued = getattr(plugin, verb)(params, {"session_id": "s", "flow_id": "f"})
                self.assertEqual(queued["data"], {"job_id": "j1", "status": "queued"})
                self.assertEqual(create_job.call_args.kwargs["action_name"], verb)
            plugin.sql_api({"operation": "submit", "body": {"statement": "SELECT 1"}}, {"session_id": "s", "flow_id": "f"})
            pinned = create_job.call_args.kwargs["params"]["query"]["requestId"]
            self.assertRegex(pinned, r"^[0-9a-f-]{36}$")
            self.assertEqual(rest_families.build_request("sql_api", create_job.call_args.kwargs["params"])["query"]["requestId"], pinned)

    def test_worker_runs_family_job_and_completes_it(self) -> None:
        plugin = self.plugin()
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": scoped("suspend")}}
        vendor = {"status": 200, "headers": {}, "body": {"status": "Statement executed successfully."}, "body_encoding": "json", "correlation_id": "c-1"}
        with patch.object(rest_client, "perform_request", return_value={**vendor, "_hints": {}}) as transport:
            async_jobs._process_job(plugin, manager, "job-1", "task")
        self.assertEqual(transport.call_args.args[1]["path"], f"{_BASE}/tasks/NIGHTLY:suspend")
        manager.update_job.assert_called_with("job-1", {"status": "completed", "result": {**vendor, "family": "task", "operation": "suspend", "method": "POST"}})

    def test_every_family_is_a_worker_rest_action(self) -> None:
        for family in rest_families.FAMILIES:
            self.assertIn(family, async_jobs.PLUGIN_ACTIONS)
            handler, gated = async_jobs.ACTION_HANDLERS[family]
            self.assertFalse(gated)
            self.assertEqual(handler.func, rest_families.run_family)  # type: ignore[attr-defined]
            self.assertIn("pagination", " ".join(completion_templates.build_completion_template(family, "result")["params"]["prompt"]["user"]["instructions"]))  # type: ignore[index]


class Registration(unittest.TestCase):
    """Each process needs its decorator, edge entry and knowledge-base JSON."""

    def test_three_artifacts_per_process(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        plugin = SnowflakePlugin()
        decorated = {action.name: action for action in discover_actions(plugin)}
        edges = plugin.get_edge_process_definitions()
        kb = ROOT / "plugins/snowflake_plugin/knowledge_base/processes"
        jsons = {path.stem: json.loads(path.read_text()) for path in kb.glob("*.json")}
        self.assertEqual(set(decorated), set(edges))
        self.assertEqual(set(decorated), set(jsons))
        for family in rest_families.FAMILIES:
            with self.subTest(family=family):
                doc = jsons[family]
                self.assertEqual(doc["process_key"], f"plugin::snowflake_plugin::{family}")
                self.assertTrue(200 <= len(doc["embedding_description"]) <= 400)
                result_type = doc["result_processor_customizations"]["result_type"]
                self.assertEqual(edges[family].result_processor_template_customizations.result_type, result_type)  # type: ignore[union-attr]
                for operation in rest_families.FAMILIES[family]:
                    self.assertIn(operation, doc["description"])

    def test_each_verb_declares_exactly_its_vendor_path_parameters(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        scoped_object = ["database", "schema", "name", "result_handler"]
        # Independently written from the vendor paths: placeholders in first-use order, and tag support.
        expected: dict[str, tuple[list[str], bool]] = {
            "sql_api": (["statement_handle"], False),
            "task": (scoped_object, True),
            "stream": (scoped_object, True),
            "dynamic_table": (scoped_object, True),
            "database": (["name", "result_handler"], True),
            "schema": (["database", "name", "result_handler"], True),
            "warehouse": (["name", "result_handler"], True),
            "stage": (["database", "schema", "name", "file_path", "result_handler"], False),
        }
        self.assertEqual(set(expected), ROUTED_FAMILIES)
        decorated = {action.name: action for action in discover_actions(SnowflakePlugin())}
        for family, (placeholders, tags) in expected.items():
            with self.subTest(family=family):
                self.assertEqual(rest_families.path_parameters(family), placeholders)
                self.assertEqual(rest_families.takes_tags(family), tags)
                declared = list(decorated[family].parameters)
                self.assertEqual(declared, ["operation", "query", "body", *placeholders, *(["tags"] if tags else [])])


if __name__ == "__main__":
    unittest.main()
