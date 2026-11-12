#!/usr/bin/env python3
"""Hermetic smoke for the typed Lead Database object verbs.

Covers companies, opportunities, opportunity roles, sales persons, named
accounts, named account lists (and their membership) and custom objects
through the real ``MarketoClient`` over ``httpx.MockTransport`` with
Marketo-shaped envelopes (``requestId``/``success``/``result``/
``nextPageToken``/``errors``), then through the plugin's dispatch -> worker ->
check_marketo_job_status path. Exercises: three-artifact registration per
verb, exact vendor paths and methods, pagination (query string and
compound-key body), per-record write results, vendor error envelopes, HTTP
401/403 and path-segment encoding.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import csv  # noqa: E402
import json  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from collections.abc import Callable  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

import httpx  # noqa: E402
from ananta.file_locations.resolution import SoletTree  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from marketo_plugin import lead_database_objects as objects  # noqa: E402
from marketo_plugin.app_config import MarketoInstanceConfig  # noqa: E402
from marketo_plugin.errors import MarketoEnvelopeError  # noqa: E402
from marketo_plugin.http_client import MarketoClient  # noqa: E402
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402
from smoke_async_dispatch import _FakeAsyncJobManager  # noqa: E402  # pyright: ignore[reportMissingImports]

KB_PROCESSES = REPO_ROOT / "plugins" / "marketo_plugin" / "knowledge_base" / "processes"
SECRET = "tenant secret+/é=?"
STATE = {"session_id": "session-1", "flow_id": "flow-1"}

Handler = Callable[[httpx.Request], httpx.Response]


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _bind_locations(plugin: Any, directories: list[str]) -> NonCallableMagicMock:
    """Supply a live address-book service through the production binding."""
    orchestrator = NonCallableMagicMock()
    orchestrator.APP_HOME = str(_LOCATION_TREE.app_home)
    orchestrator.plugin_manager.get_plugin.return_value = None
    orchestrator.get_service.return_value.list_path_addresses.return_value = {
        "action_status": "completed",
        "data": {"addresses": [{"name": f"project-{index}", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": directory}]} for index, directory in enumerate(directories)]},
    }
    plugin.orchestrator_ref = orchestrator
    return orchestrator


def _envelope(result: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"requestId": "e42b#14272d07d78", "success": True, "result": result, **extra}


def _describe_envelope(name: str, id_field: str, dedupe: list[str]) -> dict[str, Any]:
    return _envelope(
        [
            {
                "name": name,
                "description": f"Map of {name} attributes",
                "createdAt": "2016-01-11T20:59:42Z",
                "updatedAt": "2016-01-11T20:59:42Z",
                "idField": id_field,
                "dedupeFields": dedupe,
                "searchableFields": [dedupe, [id_field]],
                "fields": [
                    {"name": id_field, "displayName": "Id", "dataType": "string", "length": 36, "updateable": False},
                    {"name": dedupe[0], "displayName": "External Id", "dataType": "string", "length": 100, "updateable": False},
                ],
            }
        ]
    )


CUSTOM_TYPE = "car_c"


def _type_params(family: objects.ObjectFamily) -> dict[str, Any]:
    return {"api_name": CUSTOM_TYPE} if family.typed_by_api_name else {}


def _base(family: objects.ObjectFamily) -> str:
    return family.resolve_base(_type_params(family))


def _record_key(family: objects.ObjectFamily, n: int) -> dict[str, Any]:
    return {name: f"{name}-{n}" for name in family.dedupe_fields} or {"vin": f"vin-{n}"}


class _VendorTranscriptCase(unittest.TestCase):
    """Real MarketoClient over httpx.MockTransport, plus the plugin dispatch -> worker -> poll path."""

    def setUp(self) -> None:
        self.seen: list[httpx.Request] = []
        self.mints = 0
        self.vendor: Handler = lambda _: httpx.Response(200, json=_envelope([]))
        self.client = MarketoClient(
            MarketoInstanceConfig(base_url="https://tenant.mktorest.test", client_id="test-client", client_secret=SECRET),
            timeout_seconds=5.0,
        )
        self.client._http.close()  # pyright: ignore[reportPrivateUsage]
        self.client._http = httpx.Client(  # pyright: ignore[reportPrivateUsage]
            base_url="https://tenant.mktorest.test",
            transport=httpx.MockTransport(self._handle),
        )
        self.addCleanup(self.client.close)
        self.tmp = tempfile.mkdtemp(prefix="marketo_objects_")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if request.url.path == "/identity/oauth/token":
            self.mints += 1
            return httpx.Response(200, json={"access_token": f"minted-token-{self.mints}", "token_type": "bearer", "expires_in": 3599, "scope": "api@tenant"})
        return self.vendor(request)

    def _vendor_calls(self) -> list[httpx.Request]:
        return [request for request in self.seen if request.url.path != "/identity/oauth/token"]

    def _tsv(self, name: str) -> str:
        return str(Path(self.tmp) / name)

    def _plugin(self) -> tuple[MarketoPlugin, _FakeAsyncJobManager]:
        plugin = MarketoPlugin()
        plugin.logger = MagicMock()
        plugin.config_provider = {}
        manager = _FakeAsyncJobManager()
        orchestrator = _bind_locations(plugin, [self.tmp])
        orchestrator.async_job_manager = manager
        plugin._require_client = lambda: self.client  # type: ignore[method-assign]  # pyright: ignore[reportPrivateUsage]
        return plugin, manager

    def _run_job(self, verb: str, params: dict[str, Any]) -> tuple[dict[str, Any], _FakeAsyncJobManager]:
        plugin, manager = self._plugin()
        queued = getattr(plugin, verb)(params, STATE)
        self.assertEqual(queued["data"].get("status"), "queued", queued)
        before = len(self._vendor_calls())
        plugin._process_pending_jobs()  # pyright: ignore[reportPrivateUsage]
        self.assertGreaterEqual(len(self._vendor_calls()), before)
        polled = plugin.check_marketo_job_status({"job_id": queued["data"]["job_id"]}, {})
        return polled["data"], manager


class LeadDatabaseObjectTests(_VendorTranscriptCase):
    # ------------------------------------------------------------------
    # Registration: decorator + edge definition + KB json per verb
    # ------------------------------------------------------------------

    def test_every_verb_has_all_three_artifacts(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions
        from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

        plugin = MarketoPlugin()
        actions = discover_actions(plugin)
        PluginRegistrationValidator().validate_edge_process_provider("marketo_plugin", plugin, actions)
        edges = plugin.get_edge_process_definitions()
        self.assertEqual(len(objects.VERB_OPERATIONS), 31)
        for verb in objects.VERB_OPERATIONS:
            with self.subTest(verb=verb):
                self.assertTrue(callable(getattr(plugin, verb)))
                self.assertIn(verb, edges)
                kb = json.loads((KB_PROCESSES / f"{verb}.json").read_text(encoding="utf-8"))
                self.assertEqual(kb["process_key"], f"plugin::marketo_plugin::{verb}")
                self.assertEqual(kb["result_processor_customizations"]["result_type"], objects.result_type(verb))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400, len(kb["embedding_description"]))

    # ------------------------------------------------------------------
    # Exact vendor paths, methods and bodies
    # ------------------------------------------------------------------

    def test_describe_writes_fields_and_returns_keys_for_every_family(self) -> None:
        for family in objects.FAMILIES.values():
            if "describe" not in family.operations:
                continue
            with self.subTest(family=family.key):
                dedupe = list(family.dedupe_fields) or ["vin"]
                self.vendor = lambda _, family=family, dedupe=dedupe: httpx.Response(200, json=_describe_envelope(family.key, family.id_field, dedupe))
                params = {"output_tsv_path": self._tsv(f"describe_{family.key}.tsv"), **_type_params(family)}
                result, _ = self._run_job(f"describe_{family.key}", params)
                call = self._vendor_calls()[-1]
                self.assertEqual((call.method, call.url.path), ("GET", f"{_base(family)}/describe.json"))
                self.assertEqual(result["status"], "completed", result)
                handle = result["result"]
                self.assertEqual(handle["id_field"], family.id_field)
                self.assertEqual(handle["dedupe_fields"], dedupe)
                self.assertEqual(handle["row_count"], 2)
                with open(handle["path"], encoding="utf-8") as tsv:
                    self.assertEqual(next(csv.reader(tsv, delimiter="\t"))[0], "name")

    def test_get_pages_by_next_page_token_on_the_collection_path(self) -> None:
        pages = {
            None: _envelope([{"seq": 0, "id": 3433, "externalCompanyId": "acme-1"}], nextPageToken="GIYDAOBNGEYS2MBWKQ======"),
            "GIYDAOBNGEYS2MBWKQ======": _envelope([{"seq": 1, "id": 3434, "externalCompanyId": "acme-2"}]),
        }
        self.vendor = lambda request: httpx.Response(200, json=pages[request.url.params.get("nextPageToken")])
        result, _ = self._run_job(
            "get_companies",
            {"filter_type": "externalCompanyId", "filter_values": ["acme-1", "acme-2"], "fields": ["externalCompanyId", "company"], "output_tsv_path": self._tsv("companies.tsv")},
        )
        calls = self._vendor_calls()
        self.assertEqual([(c.method, c.url.path) for c in calls], [("GET", "/rest/v1/companies.json")] * 2)
        self.assertEqual(calls[0].url.params["filterType"], "externalCompanyId")
        self.assertEqual(calls[0].url.params["filterValues"], "acme-1,acme-2")
        self.assertEqual(calls[0].url.params["fields"], "externalCompanyId,company")
        self.assertEqual(calls[0].url.params["batchSize"], "300")
        self.assertEqual(calls[1].url.params["nextPageToken"], "GIYDAOBNGEYS2MBWKQ======")
        self.assertEqual((result["result"]["row_count"], result["result"]["truncated"]), (2, False))

    def test_get_respects_row_limit_and_reports_truncation(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"seq": n, "marketoGUID": f"guid-{n}"} for n in range(300)], nextPageToken="more", moreResult=True))
        result, _ = self._run_job(
            "get_opportunities",
            {"filter_type": "externalOpportunityId", "filter_values": ["o"], "output_tsv_path": self._tsv("opps.tsv"), "acknowledge_default_limit_override": True, "row_limit": 450},
        )
        self.assertEqual([c.url.path for c in self._vendor_calls()], ["/rest/v1/opportunities.json"] * 2)
        self.assertEqual((result["result"]["row_count"], result["result"]["truncated"]), (450, True))

    def test_opportunity_role_compound_key_query_uses_method_override(self) -> None:
        keys = [{"externalOpportunityId": "opp-1", "leadId": 1, "role": "Influencer"}]
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"seq": 0, "marketoGUID": "role-guid", **keys[0]}]))
        result, _ = self._run_job("get_opportunity_roles", {"compound_keys": keys, "fields": ["marketoGUID", "role"], "output_tsv_path": self._tsv("roles.tsv")})
        call = self._vendor_calls()[-1]
        self.assertEqual((call.method, call.url.path, call.url.params["_method"]), ("POST", "/rest/v1/opportunities/roles.json", "GET"))
        self.assertEqual(json.loads(call.content), {"filterType": "dedupeFields", "input": keys, "batchSize": 300, "fields": ["marketoGUID", "role"]})
        self.assertEqual((result["result"]["row_count"], result["result"]["truncated"]), (1, False))

    def test_sync_posts_collection_path_with_vendor_fields_for_every_family(self) -> None:
        for family in objects.FAMILIES.values():
            with self.subTest(family=family.key):
                self.vendor = lambda _, family=family: httpx.Response(
                    200,
                    json=_envelope(
                        [
                            {"seq": 0, family.id_field: "1", "status": "created"},
                            {"seq": 1, family.id_field: "2", "status": "updated"},
                            {"seq": 2, "status": "skipped", "reasons": [{"code": "1006", "message": "Field 'bogus' not found"}]},
                        ]
                    ),
                )
                records = [_record_key(family, n) for n in range(3)]
                action = "createOrUpdate" if "createOrUpdate" in family.sync_actions else "createOnly"
                result, manager = self._run_job(f"sync_{family.key}", {"records": records, "action": action, "dedupe_by": "dedupeFields", **_type_params(family)})
                call = self._vendor_calls()[-1]
                self.assertEqual((call.method, call.url.path), ("POST", f"{_base(family)}.json"))
                self.assertEqual(json.loads(call.content), {"input": records, "action": action, "dedupeBy": "dedupeFields"})
                self.assertEqual(result["result"]["tallies"], {"created": 1, "updated": 1, "skipped": 1})
                self.assertIn("changed vendor state", json.dumps(next(iter(manager._jobs.values()))["metadata"]))  # pyright: ignore[reportPrivateUsage]

    def test_update_only_sync_by_id_field_sends_only_updates_and_reports_missing_records(self) -> None:
        for family in objects.FAMILIES.values():
            with self.subTest(family=family.key):
                self.vendor = lambda _, family=family: httpx.Response(
                    200,
                    json=_envelope(
                        [
                            {"seq": 0, family.id_field: "guid-1", "status": "updated"},
                            {"seq": 1, "status": "skipped", "reasons": [{"code": "1013", "message": "Record not found"}]},
                        ]
                    ),
                )
                records = [{family.id_field: "guid-1", "description": "renamed"}, {family.id_field: "guid-404", "description": "absent"}]
                result, _ = self._run_job(f"sync_{family.key}", {"records": records, "action": "updateOnly", "dedupe_by": "idField", **_type_params(family)})
                self.assertEqual(json.loads(self._vendor_calls()[-1].content), {"input": records, "action": "updateOnly", "dedupeBy": "idField"})
                self.assertEqual(result["result"]["tallies"], {"updated": 1, "skipped": 1})
                self.assertNotIn("created", result["result"]["tallies"])

    def test_delete_posts_delete_path_and_preserves_skip_reasons(self) -> None:
        for family in objects.FAMILIES.values():
            with self.subTest(family=family.key):
                self.vendor = lambda _: httpx.Response(
                    200,
                    json=_envelope([{"seq": 0, "id": 3433, "status": "deleted"}, {"seq": 1, "status": "skipped", "reasons": [{"code": "1013", "message": "Record not found"}]}]),
                )
                keys = [{family.id_field: "a"}, {family.id_field: "b"}]
                result, _ = self._run_job(f"delete_{family.key}", {"keys": keys, "delete_by": "idField", **_type_params(family)})
                call = self._vendor_calls()[-1]
                self.assertEqual((call.method, call.url.path), ("POST", f"{_base(family)}/delete.json"))
                self.assertEqual(json.loads(call.content), {"input": keys, "deleteBy": "idField"})
                self.assertEqual(result["result"]["results"][1]["reasons"][0]["code"], "1013")
                self.assertEqual(result["result"]["tallies"], {"deleted": 1, "skipped": 1})

    # ------------------------------------------------------------------
    # Validation mirrors the vendor and never reaches the vendor or job ledger
    # ------------------------------------------------------------------

    def test_invalid_params_are_refused_at_dispatch(self) -> None:
        cases: list[tuple[str, dict[str, Any]]] = [
            ("sync_companies", {"records": [{"externalCompanyId": str(n)} for n in range(301)]}),
            ("sync_companies", {"records": [{"externalCompanyId": "a"}], "action": "createDuplicate"}),
            ("sync_sales_persons", {"records": [{}]}),
            ("delete_opportunities", {"keys": [{"marketoGUID": "a"}], "delete_by": "email"}),
            ("get_companies", {"compound_keys": [{"externalCompanyId": "a"}], "output_tsv_path": self._tsv("x.tsv")}),
            ("get_opportunity_roles", {"compound_keys": [{"externalOpportunityId": "o", "leadId": 1}], "output_tsv_path": self._tsv("x.tsv")}),
            ("get_opportunity_roles", {"compound_keys": [{"externalOpportunityId": "o", "leadId": 1, "role": "r"}], "filter_values": ["1"], "output_tsv_path": self._tsv("x.tsv")}),
            ("get_sales_persons", {"filter_type": "email", "filter_values": ["a"], "output_tsv_path": "/outside/roots.tsv"}),
            ("get_sales_persons", {"filter_type": "email", "filter_values": ["a"], "output_tsv_path": self._tsv("x.tsv"), "row_limit": 900}),
            ("sync_named_account_lists", {"records": [{"name": "Tier 1"}], "action": "createOrUpdate"}),
            ("get_named_account_lists", {"filter_type": "dedupeFields", "filter_values": ["Tier 1"], "fields": ["name"], "output_tsv_path": self._tsv("x.tsv")}),
            ("get_custom_objects", {"filter_type": "vin", "filter_values": ["a"], "output_tsv_path": self._tsv("x.tsv")}),
            ("sync_custom_objects", {"api_name": "..", "records": [{"vin": "a"}]}),
            ("delete_custom_objects", {"api_name": " ", "keys": [{"marketoGUID": "a"}]}),
            ("add_named_account_list_members", {"list_id": "list-guid", "named_account_ids": ["a", 7]}),
            ("remove_named_account_list_members", {"named_account_ids": ["a"]}),
            ("get_named_account_list_members", {"list_id": ".", "output_tsv_path": self._tsv("x.tsv")}),
            ("add_named_account_list_members", {"list_id": "list-guid", "named_account_ids": [f"g{n}" for n in range(301)]}),
        ]
        for verb, params in cases:
            with self.subTest(verb=verb, params=list(params)):
                plugin, manager = self._plugin()
                refused = getattr(plugin, verb)(params, STATE)
                self.assertEqual(refused["action_status"], "error", refused)
                self.assertEqual(manager._jobs, {})  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(self.seen, [])

    # ------------------------------------------------------------------
    # Error mapping: vendor envelope, HTTP 401/403
    # ------------------------------------------------------------------

    def test_vendor_error_envelope_and_http_refusals_are_classified(self) -> None:
        cases: list[tuple[Handler, str]] = [
            (lambda _: httpx.Response(200, json={"requestId": "1#2", "success": False, "errors": [{"code": "603", "message": "Access denied"}]}), "marketo.permission_denied"),
            (lambda _: httpx.Response(200, json={"requestId": "1#2", "success": False, "errors": [{"code": "1003", "message": "Invalid value for action"}]}), "marketo.validation_failed"),
            (lambda _: httpx.Response(403, json={"requestId": "1#2", "success": False, "errors": [{"code": "603", "message": "Access denied"}]}), "marketo.permission_denied"),
            (lambda _: httpx.Response(403, json={"message": "Forbidden"}), "marketo.permission_denied"),
            (lambda _: httpx.Response(403, text="<html>Forbidden</html>"), "marketo.permission_denied"),
            (lambda _: httpx.Response(401, text="Unauthorized"), "marketo.auth_failed"),
            (lambda _: httpx.Response(401, json={"error": "unauthorized"}), "marketo.auth_failed"),
            (lambda _: httpx.Response(503, text="Service Unavailable"), "marketo.api_error"),
        ]
        jobs: list[tuple[str, dict[str, Any]]] = [
            ("delete_companies", {"keys": [{"externalCompanyId": "a"}]}),
            ("remove_named_account_list_members", {"list_id": "list-guid", "named_account_ids": ["a"]}),
            ("get_custom_objects", {"api_name": CUSTOM_TYPE, "filter_type": "vin", "filter_values": ["a"], "output_tsv_path": self._tsv("err.tsv")}),
        ]
        for verb, params in jobs:
            for handler, code in cases:
                with self.subTest(verb=verb, code=code):
                    self.vendor = handler
                    result, _ = self._run_job(verb, params)
                    self.assertEqual(result["status"], "error")
                    self.assertEqual(result["error"]["code"], code, result)

    def test_envelope_error_raises_from_the_executor_directly(self) -> None:
        self.vendor = lambda _: httpx.Response(200, json={"success": False, "errors": [{"code": "1076", "message": "Duplicate"}]})
        request = objects.prepare("sync_opportunities", {"records": [{"externalOpportunityId": "a", "name": "A"}]}, lambda path: path)
        with self.assertRaises(MarketoEnvelopeError):
            objects.execute(self.client, request)


class NamedAccountListAndCustomObjectTests(_VendorTranscriptCase):
    def test_named_account_list_query_takes_no_fields_and_pages(self) -> None:
        pages = {
            None: _envelope(
                [{"seq": 0, "marketoGUID": "dff23271-f996-47d7-984f-f2676861b5fa", "name": "Tier 1", "createdAt": "2017-02-23T00:51:18Z", "updatedAt": "2017-02-23T00:51:18Z", "type": "default", "updateable": True}],
                nextPageToken="next-lists",
            ),
            "next-lists": _envelope([{"seq": 1, "marketoGUID": "dff23271-f996-47d7-984f-f2676861b5fb", "name": "CRM list", "type": "external", "updateable": False}]),
        }
        self.vendor = lambda request: httpx.Response(200, json=pages[request.url.params.get("nextPageToken")])
        result, _ = self._run_job("get_named_account_lists", {"filter_type": "dedupeFields", "filter_values": ["Tier 1", "CRM list"], "output_tsv_path": self._tsv("lists.tsv")})
        calls = self._vendor_calls()
        self.assertEqual([(c.method, c.url.path) for c in calls], [("GET", "/rest/v1/namedAccountLists.json")] * 2)
        self.assertNotIn("fields", calls[0].url.params)
        self.assertEqual((calls[0].url.params["filterType"], calls[0].url.params["filterValues"]), ("dedupeFields", "Tier 1,CRM list"))
        self.assertEqual((result["result"]["row_count"], result["result"]["truncated"]), (2, False))

    def test_named_account_list_members_read_add_and_remove(self) -> None:
        list_id = "dff23271-f996-47d7-984f-f2676861b5fa"
        member = {"seq": 0, "marketoGUID": "cff23271-f996-47d7-984f-f2676861b5fc", "name": "Acme Corp", "createdAt": "2017-02-23T00:51:18Z", "updatedAt": "2017-02-23T00:51:18Z"}
        pages = {None: _envelope([member], nextPageToken="next-members"), "next-members": _envelope([{**member, "seq": 1, "marketoGUID": "cff23271-0000", "name": "Globex"}])}
        self.vendor = lambda request: httpx.Response(200, json=pages[request.url.params.get("nextPageToken")])
        read, _ = self._run_job("get_named_account_list_members", {"list_id": list_id, "fields": ["name"], "output_tsv_path": self._tsv("members.tsv")})
        calls = self._vendor_calls()
        self.assertEqual([(c.method, c.url.path) for c in calls], [("GET", f"/rest/v1/namedAccountList/{list_id}/namedAccounts.json")] * 2)
        self.assertEqual((calls[0].url.params["fields"], calls[0].url.params["batchSize"]), ("name", "300"))
        self.assertEqual((read["result"]["row_count"], read["result"]["truncated"]), (2, False))
        for verb, suffix, status in (("add_named_account_list_members", ".json", "added"), ("remove_named_account_list_members", "/remove.json", "removed")):
            with self.subTest(verb=verb):
                self.vendor = lambda _, status=status: httpx.Response(200, json=_envelope([{"seq": 0, "marketoGUID": "cff23271-0000", "status": status}, {"seq": 1, "status": "skipped", "reasons": [{"code": "1013", "message": "Record not found"}]}]))
                written, _ = self._run_job(verb, {"list_id": list_id, "named_account_ids": ["cff23271-0000", "missing-guid"]})
                call = self._vendor_calls()[-1]
                self.assertEqual((call.method, call.url.path), ("POST", f"/rest/v1/namedAccountList/{list_id}/namedAccounts{suffix}"))
                self.assertEqual(json.loads(call.content), {"input": [{"marketoGUID": "cff23271-0000"}, {"marketoGUID": "missing-guid"}]})
                self.assertEqual(written["result"]["tallies"], {status: 1, "skipped": 1})

    def test_list_custom_object_types_filters_by_names_and_returns_api_names(self) -> None:
        car = {
            "name": "car_c",
            "displayName": "Car",
            "description": "Car object",
            "createdAt": "2016-01-23T00:51:18Z",
            "updatedAt": "2016-01-23T00:51:18Z",
            "idField": "marketoGUID",
            "dedupeFields": ["vin"],
            "searchableFields": [["vin"], ["marketoGUID"], ["customerId"]],
            "relationships": [{"field": "customerId", "type": "child", "relatedTo": {"name": "Lead", "field": "Id"}}],
        }
        self.vendor = lambda _: httpx.Response(200, json=_envelope([car]))
        result, _ = self._run_job("list_custom_object_types", {"names": ["car_c", "truck_c"], "output_tsv_path": self._tsv("types.tsv")})
        call = self._vendor_calls()[-1]
        self.assertEqual((call.method, call.url.path, call.url.params["names"]), ("GET", "/rest/v1/customobjects.json", "car_c,truck_c"))
        self.assertEqual((result["result"]["api_names"], result["result"]["row_count"]), (["car_c"], 1))
        self.assertIn("relationships", Path(result["result"]["path"]).read_text(encoding="utf-8"))

    # ------------------------------------------------------------------
    # Compound-key queries page on the JSON body; api_name is one encoded segment
    # ------------------------------------------------------------------

    def test_compound_key_query_pages_on_the_body_next_page_token(self) -> None:
        token = "GIYDAOBNGEYS2MBWKQ======"
        keys = [{"externalOpportunityId": f"opp-{n}", "leadId": n, "role": "Influencer"} for n in range(2)]

        def roles(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            if body.get("nextPageToken") == token:
                return httpx.Response(200, json=_envelope([{"seq": 1, "marketoGUID": "role-2", **keys[1]}]))
            return httpx.Response(200, json=_envelope([{"seq": 0, "marketoGUID": "role-1", **keys[0]}], nextPageToken=token, moreResult=True))

        self.vendor = roles
        result, _ = self._run_job("get_opportunity_roles", {"compound_keys": keys, "output_tsv_path": self._tsv("roles_paged.tsv")})
        calls = self._vendor_calls()
        self.assertEqual([(c.method, c.url.path, c.url.params["_method"]) for c in calls], [("POST", "/rest/v1/opportunities/roles.json", "GET")] * 2)
        self.assertNotIn("nextPageToken", json.loads(calls[0].content))
        self.assertEqual(json.loads(calls[1].content), {"filterType": "dedupeFields", "input": keys, "batchSize": 300, "nextPageToken": token})
        self.assertEqual((result["result"]["row_count"], result["result"]["truncated"]), (2, False))

    def test_custom_object_compound_query_and_paths_carry_the_encoded_api_name(self) -> None:
        keys = [{"vin": "19UYA42601A019296"}]
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"seq": 0, "marketoGUID": "dff23271-f996-47d7-984f-f2676861b5fb", "vin": keys[0]["vin"], "createdAt": "2016-01-23T00:51:18Z", "updatedAt": "2016-01-23T00:51:18Z"}]))
        result, _ = self._run_job("get_custom_objects", {"api_name": CUSTOM_TYPE, "compound_keys": keys, "filter_type": "dedupeFields", "output_tsv_path": self._tsv("cars.tsv")})
        call = self._vendor_calls()[-1]
        self.assertEqual((call.method, call.url.path, call.url.params["_method"]), ("POST", "/rest/v1/customobjects/car_c.json", "GET"))
        self.assertEqual(json.loads(call.content), {"filterType": "dedupeFields", "input": keys, "batchSize": 300})
        self.assertEqual(result["result"]["row_count"], 1)
        self.vendor = lambda _: httpx.Response(200, json=_envelope([{"seq": 0, "marketoGUID": "g", "status": "deleted"}]))
        self._run_job("delete_custom_objects", {"api_name": "odd/name?x", "keys": [{"marketoGUID": "g"}]})
        self.assertEqual(self._vendor_calls()[-1].url.raw_path, b"/rest/v1/customobjects/odd%2Fname%3Fx/delete.json")


if __name__ == "__main__":
    unittest.main()
