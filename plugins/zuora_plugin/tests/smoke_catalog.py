"""Real-shaped legacy catalog transcripts; no vendor or database is contacted.

Contracts: the five catalog sections of developer.zuora.com/v1-api-reference/api,
reference version 2026-11-06, retrieved 2026-11-07. Test wire expectations are
independent of the Operation declarations, including narrower definition headers
and the WSDL header documented in the charge read's introductory note.
"""

from __future__ import annotations

import json
import sys
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/zuora_plugin/src"))

from ananta.core.plugins.action_discovery import discover_actions  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from finish_support import finished  # noqa: E402  # pyright: ignore[reportMissingImports]
from zuora_plugin import async_jobs, catalog_actions, typed_verbs  # noqa: E402
from zuora_plugin.app_config import ZuoraTenantConfig  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.http_client import ZuoraClient  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402
from zuora_plugin.typed_operations import Operation, execute  # noqa: E402

OPS = {op.name: op for op in catalog_actions.OPERATIONS}
Handler = Callable[[httpx.Request], httpx.Response]
V1 = {"Zuora-Version", "Zuora-Entity-Ids", "Zuora-Org-Ids", "Zuora-Track-Id"}
OBJECTS = {"product": "product_id", "product_rate_plan": "rate_plan_id", "product_rate_plan_charge": "charge_id"}
CATALOG_ROUTES = {
    "list_product_rate_plans": ("GET", "/v1/products/K-1/product-rate-plans"),
    "get_product_rate_plan": ("GET", "/v1/product-rate-plans/K-1"),
    "get_product_rate_plans_by_external_id": ("GET", "/v1/product-rate-plans/external-id/K-1"),
    "get_product_rate_plan_charge": ("GET", "/v1/product-rate-plan-charges/K-1"),
    "create_product_charge_definition": ("POST", "/v1/product-charge-definitions"),
    "list_product_charge_definitions": ("GET", "/v1/product-charge-definitions"),
    "create_product_charge_definitions": ("POST", "/v1/product-charge-definitions/bulk"),
    "update_product_charge_definitions": ("PUT", "/v1/product-charge-definitions/bulk"),
    "get_product_charge_definition": ("GET", "/v1/product-charge-definitions/K-1"),
    "update_product_charge_definition": ("PUT", "/v1/product-charge-definitions/K-1"),
    "delete_product_charge_definition": ("DELETE", "/v1/product-charge-definitions/K-1"),
    "create_catalog_group": ("POST", "/v1/catalog-groups"),
    "list_catalog_groups": ("GET", "/v1/catalog-groups"),
    "get_catalog_group": ("GET", "/v1/catalog-groups/K-1"),
    "update_catalog_group": ("PUT", "/v1/catalog-groups/K-1"),
    "delete_catalog_group": ("DELETE", "/v1/catalog-groups/K-1"),
}
for _resource in OBJECTS:
    for _verb, _method in (("create", "POST"), ("get", "GET"), ("update", "PUT"), ("delete", "DELETE")):
        _name = f"{_verb}_{_resource}"
        if _verb == "get" and _resource != "product":
            _name += "_object"
        CATALOG_ROUTES[_name] = (_method, "/v1/object/" + _resource.replace("_", "-") + ("" if _verb == "create" else "/K-1"))


def refusal(message: str) -> dict[str, Any]:
    return {"success": False, "processId": "6A5B4C3D2E1F0A9B", "reasons": [{"code": 50000040, "message": message}], "requestId": "52b4c5d6-e7f8-4091-a2b3-c4d5e6f7a8b9"}


def minimal(op: Operation) -> dict[str, Any]:
    params: dict[str, Any] = dict.fromkeys(op.path_fields, "K-1")
    if op.name == "get_product_rate_plan_charge":
        params["x_zuora_wsdl_version"] = "156.0"
    if op.body is not None:
        if op.name.endswith("product_rate_plan_charge"):
            params["body"] = {
                "Name": "Seats",
                "ProductRatePlanId": "K-1",
                "ChargeType": "Recurring",
                "ChargeModel": "Flat Fee Pricing",
                "BillCycleType": "DefaultFromCustomer",
                "BillingPeriod": "Month",
                "TriggerEvent": "ContractEffective",
                "UseDiscountSpecificAccountingCode": False,
                "ProductRatePlanChargeTierData": {"ProductRatePlanChargeTier": [{"Currency": "USD", "Price": 10}]},
            }
        elif op.name == "create_product":
            params["body"] = {"Name": "Service", "EffectiveStartDate": "2026-01-01", "EffectiveEndDate": "2030-01-01"}
        elif op.name == "create_product_rate_plan":
            params["body"] = {"Name": "Monthly", "ProductId": "K-1"}
        elif "catalog_group" in op.name:
            params["body"] = {"name": "Plans", "description": "", "productRatePlans": [{"id": "K-1", "grade": 1}], "type": "Grading"} if op.method == "POST" else {"description": "", "add": [{"id": "K-1", "grade": 2}], "remove": ["old-plan"]}
        elif op.name.endswith("definitions"):
            params["body"] = {"productChargeDefinitions": [{"productRatePlanChargeNumber": "PRPC-000001", "prices": [{"currency": "USD", "price": 12}]}]}
        else:
            params["body"] = {"description": "", "productRatePlanChargeNumber": "PRPC-000001", "prices": [{"currency": "USD", "price": 12}]}
    return params


def response_for(op: Operation) -> dict[str, Any]:
    if "/object/" in op.path:
        return object_response(op.method)
    if "catalog_group" in op.name:
        group = {"id": "K-1", "catalogGroupNumber": "CG-00000001", "name": "Plans", "type": "Grading", "productRatePlans": [{"id": "plan-1", "grade": 1, "status": "Active"}]}
        return {"catalogGroups": [group]} if op.name.startswith("list_") else {"success": True} if op.method == "DELETE" else group
    if "charge_definition" in op.name:
        return definition_response(op.name)
    if op.name == "list_product_rate_plans" or op.name == "get_product_rate_plans_by_external_id":
        return {"productRatePlans": [{"id": "K-1", "name": "Monthly", "status": "Active"}], "success": True}
    return {"id": "K-1", "name": "Monthly", "success": True}


def object_response(method: str) -> dict[str, Any]:
    if method == "GET":
        return {"Id": "K-1", "Name": "Service", "UpdatedDate": "2026-11-07T10:00:00"}
    if method == "DELETE":
        return {"id": "K-1", "success": True}
    return {"Id": "K-1", "Success": True}


def definition_response(name: str) -> dict[str, Any]:
    created = {"chargeDefinitionId": "K-1", "chargeDefinitionNumber": "PCD-00000001", "success": True}
    definition = {"productChargeDefinitionId": "K-1", "productChargeDefinitionNumber": "PCD-00000001", "prices": [{"currency": "USD", "price": 12}], "success": True}
    if name == "create_product_charge_definition":
        return created
    if name == "create_product_charge_definitions":
        return {"summary": {"successCount": 1, "failureCount": 0, "failures": []}, "results": [created], "success": True}
    if name in {"list_product_charge_definitions", "update_product_charge_definitions"}:
        return {"chargeDefinitions": [definition], "success": True}
    if name == "delete_product_charge_definition":
        return {"success": True}
    return definition


class CatalogCase(unittest.TestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []
        self.plugin = ZuoraPlugin()
        self.plugin.initialize({})

    def client(self, handler: Handler, *, client_id: str = "fixture-client", secret: str = "fixture-secret", token: str = "fixture-token") -> ZuoraClient:
        client = ZuoraClient(ZuoraTenantConfig(base_url="https://tenant.example.test", client_id=client_id, client_secret=secret), timeout_seconds=2)
        client._http.close()  # noqa: SLF001

        def transport(req: httpx.Request) -> httpx.Response:
            if req.url.path == "/oauth/token":
                return httpx.Response(200, json={"access_token": token, "expires_in": 3599, "token_type": "bearer"})
            self.assertEqual(req.headers["Authorization"], f"Bearer {token}")
            self.assertEqual(req.url.host, "tenant.example.test")
            self.requests.append(req)
            return handler(req)

        client._http = httpx.Client(base_url="https://tenant.example.test", transport=httpx.MockTransport(transport))  # noqa: SLF001
        self.addCleanup(client.close)
        return client

    def run_op(self, name: str, client: ZuoraClient, params: dict[str, Any]) -> dict[str, Any]:
        return execute(OPS[name], client, params, self.plugin._export_path_gate)  # noqa: SLF001

    def finish_op(self, name: str, client: ZuoraClient, params: dict[str, Any]) -> dict[str, Any]:
        """The published envelope: the operation, then the plugin's one finish step."""
        return finished(client, lambda c: self.run_op(name, c, params), params, name=name)


class RegistrationTests(CatalogCase):
    def test_three_artifacts_and_worker_contract_for_every_catalog_verb(self) -> None:
        actions = {action.name: action for action in discover_actions(self.plugin)}
        PluginRegistrationValidator().validate_edge_process_provider("zuora_plugin", self.plugin, list(actions.values()))
        edges = self.plugin.get_edge_process_definitions()
        self.assertEqual(len(actions), 255)
        self.assertEqual(set(OPS), set(CATALOG_ROUTES))
        for name, op in OPS.items():
            with self.subTest(verb=name):
                self.assertIn(name, actions)
                self.assertIn(name, edges)
                self.assertIn(name, async_jobs.ACTION_HANDLERS)
                kb = json.loads((ROOT / "plugins/zuora_plugin/knowledge_base/processes" / f"{name}.json").read_text())
                self.assertEqual(kb["process_key"], f"plugin::zuora_plugin::{name}")
                self.assertEqual(kb["description"], actions[name].description)
                self.assertEqual(kb["display_name"], actions[name].display_name)
                self.assertEqual(kb["result_processor_customizations"]["result_type"], typed_verbs.result_type(name))
                self.assertTrue(200 <= len(kb["embedding_description"]) <= 400)
                expected_params = {*op.path_fields, *op.query_fields, *op.header_fields} | ({"body"} if op.body else set())
                self.assertEqual(set(actions[name].parameters), expected_params)
                customizations = edges[name].error_processor_template_customizations
                assert customizations is not None
                self.assertEqual(customizations.retryable, op.method == "GET")


def expected_catalog_headers(op: Operation) -> set[str]:
    headers = set(V1)
    if "charge_definition" in op.name or op.name == "get_product_rate_plan_charge":
        headers.remove("Zuora-Org-Ids")
    if "/object/" in op.path and op.method != "DELETE" or op.name == "get_product_rate_plan_charge":
        headers.add("X-Zuora-WSDL-Version")
    if op.method == "POST":
        headers.add("Idempotency-Key")
    return headers


def expected_catalog_query(op: Operation) -> dict[str, str]:
    query: dict[str, str] = {}
    if "/object/" in op.path:
        query.update({"fields": "Id,Name"} if op.method == "GET" else {"rejectUnknownFields": "true"} if op.method in {"POST", "PUT"} else {})
    if op.name in {"list_catalog_groups", "list_product_rate_plans"}:
        query.update(page="2", pageSize="3")
    if op.name in {"list_product_rate_plans", "get_product_rate_plan_charge"}:
        query["show-charge-definitions"] = "true"
    if op.name in {"list_product_charge_definitions", "get_product_charge_definition"}:
        query["hide-inherited-values"] = "true"
    if op.name == "list_product_charge_definitions":
        query.update(charge="PRPC-000001", rateplan="PRP-000001")
    return query


def populated_params(op: Operation, query: dict[str, str]) -> dict[str, Any]:
    params = minimal(op)
    for key, spec in op.query_fields.items():
        wire = query[spec.vendor]
        params[key] = True if spec.kind == "bool" else int(wire) if spec.kind == "count" else wire
    for key in op.header_fields:
        params[key] = "156.0" if key == "x_zuora_wsdl_version" else f"header-{key}"
    if op.body:
        params["body"]["CustomField__c"] = "forwarded"
    return params


class WireTests(CatalogCase):
    def test_all_methods_paths_headers_queries_and_body_fields(self) -> None:
        for name, op in OPS.items():
            with self.subTest(verb=name):
                self.assertEqual({spec.vendor for spec in op.header_fields.values()}, expected_catalog_headers(op))
                expected_query = expected_catalog_query(op)
                params = populated_params(op, expected_query)
                client = self.client(lambda _, op=op: httpx.Response(200, json=response_for(op)))
                typed_verbs.validate_typed(name, params)
                result = self.run_op(name, client, params)
                req = self.requests[-1]
                self.assertEqual((req.method, req.url.path), CATALOG_ROUTES[name])
                self.assertEqual(dict(req.url.params), expected_query)
                for key, spec in op.header_fields.items():
                    self.assertEqual(req.headers[spec.vendor], params[key])
                if op.body:
                    self.assertEqual(json.loads(req.content), params["body"])
                self.assertEqual(result["data"], response_for(op))

    def test_external_id_is_one_encoded_component_and_dot_paths_are_refused(self) -> None:
        client = self.client(lambda _: httpx.Response(200, json={"productRatePlans": [], "success": True}))
        self.run_op("get_product_rate_plans_by_external_id", client, {"external_id": "ERP / EU #1?x=2"})
        self.assertEqual(self.requests[-1].url.raw_path, b"/v1/product-rate-plans/external-id/ERP%20%2F%20EU%20%231%3Fx%3D2")
        for value in (".", ".."):
            with self.assertRaisesRegex(ValueError, "dot path segment"):
                typed_verbs.validate_typed("get_product_rate_plans_by_external_id", {"external_id": value})

    def test_validation_refuses_only_documented_required_shapes_before_network(self) -> None:
        for name, params in (("create_product", {"body": {"Name": "Service"}}), ("create_product_rate_plan", {"body": {"Name": "Plan"}}), ("create_product_rate_plan_charge", {"body": {}}), ("get_product_rate_plan_charge", {"charge_key": "PRPC-1"}), ("list_catalog_groups", {"page": 2})):
            with self.subTest(verb=name), self.assertRaises(ValueError):
                typed_verbs.validate_typed(name, params)
        self.assertEqual(self.requests, [])

    def test_pagination_and_last_page_use_raw_control_keys(self) -> None:
        for name, collection, path in (("list_catalog_groups", "catalogGroups", "/v1/catalog-groups"), ("list_product_rate_plans", "productRatePlans", "/v1/products/P-1/product-rate-plans")):

            def vendor(req: httpx.Request, collection: str = collection, path: str = path) -> httpx.Response:
                body: dict[str, Any] = {collection: [{"id": "K-1"}]}
                if req.url.params.get("page", "1") == "1":
                    body["nextPage"] = f"https://tenant.example.test{path}?page=2&pageSize=1"
                return httpx.Response(200, json=body)

            client = self.client(vendor)
            params = {"page_size": 1, **({"product_key": "P-1"} if name == "list_product_rate_plans" else {})}
            first = self.finish_op(name, client, params)["data"]
            self.assertEqual(first["pagination"]["next_page"], 2)
            self.assertEqual(first["data"]["nextPage"], f"https://tenant.example.test{path}?page=2&pageSize=1")
            last = self.finish_op(name, client, {**params, "page": 2})["data"]
            self.assertIsNone(last["pagination"]["next_page"])


class LifecycleTests(CatalogCase):
    def test_catalog_group_create_read_correct_clear_reverse_membership_and_delete(self) -> None:
        group: dict[str, Any] = {}

        def vendor(req: httpx.Request) -> httpx.Response:
            if req.method == "POST":
                group.update(json.loads(req.content), id="K-1", catalogGroupNumber="CG-00000001")
            elif req.method == "PUT":
                body = json.loads(req.content)
                group.update({key: value for key, value in body.items() if key not in {"add", "remove"}})
                members = {plan["id"]: plan for plan in group["productRatePlans"]}
                members.update({plan["id"]: plan for plan in body.get("add", [])})
                for key in body.get("remove", []):
                    members.pop(key, None)
                group["productRatePlans"] = list(members.values())
            elif req.method == "DELETE":
                group.clear()
                return httpx.Response(200, json={"success": True})
            elif not group:
                return httpx.Response(404, json=refusal("Catalog group not found"))
            return httpx.Response(200, json=group)

        client = self.client(vendor)
        self.run_op("create_catalog_group", client, minimal(OPS["create_catalog_group"]))
        key = {"catalog_group_key": "K-1"}
        self.run_op("update_catalog_group", client, {**key, "body": {"description": "corrected", "add": [{"id": "plan-2", "grade": 3}], "remove": ["K-1"]}})
        changed = self.run_op("get_catalog_group", client, key)["data"]
        self.assertEqual(changed["description"], "corrected")
        self.assertEqual(changed["productRatePlans"], [{"id": "plan-2", "grade": 3}])
        self.run_op("update_catalog_group", client, {**key, "body": {"description": "", "add": [{"id": "K-1", "grade": 1}], "remove": ["plan-2"]}})
        restored = self.run_op("get_catalog_group", client, key)["data"]
        self.assertEqual(restored["description"], "")
        self.assertEqual(restored["productRatePlans"], [{"id": "K-1", "grade": 1}])
        self.run_op("delete_catalog_group", client, key)
        with self.assertRaises(ZuoraServiceError) as raised:
            self.run_op("get_catalog_group", client, key)
        self.assertEqual(raised.exception.code, "zuora.object_not_found")

    def test_effective_date_retirement_reversal_and_bulk_definition_correction(self) -> None:
        for name, body in (
            ("update_product", {"EffectiveEndDate": "2026-11-07", "Description": ""}),
            ("update_product", {"EffectiveEndDate": "2030-01-01"}),
            ("update_product_rate_plan", {"EffectiveEndDate": "2026-11-07", "Grade": 2}),
            ("update_product_rate_plan", {"EffectiveEndDate": "2030-01-01", "Grade": 1}),
            ("update_product_charge_definition", {"effectiveEndDate": "2026-11-07", "prices": []}),
            ("update_product_charge_definitions", {"productChargeDefinitions": [{"id": "PCD-1", "prices": [{"currency": "USD", "price": 20}]}]}),
        ):
            with self.subTest(verb=name):
                client = self.client(lambda _, name=name: httpx.Response(200, json=response_for(OPS[name])))
                self.run_op(name, client, {**minimal(OPS[name]), "body": body})
                self.assertEqual(json.loads(self.requests[-1].content), body)

    def test_vendor_auth_conflict_and_terminal_refusals_are_preserved_without_write_replay(self) -> None:
        for status, code in ((401, "zuora.auth_failed"), (403, "zuora.permission_denied"), (409, "zuora.validation_failed"), (400, "zuora.validation_failed"), (200, "zuora.validation_failed")):
            for name in ("update_product", "update_product_rate_plan_charge", "delete_product", "delete_catalog_group", "delete_product_charge_definition"):
                with self.subTest(status=status, verb=name):
                    before = len(self.requests)
                    client = self.client(lambda _, status=status: httpx.Response(status, json=refusal("Catalog was changed or is referenced; fixture-token")))
                    error = self.finish_op(name, client, minimal(OPS[name]))["error"]
                    self.assertEqual(error["code"], code)
                    if status != 401:  # a vendor refusal's detail is returned as received
                        self.assertIn("Catalog was changed or is referenced", error["message"])
                    if status != 401:  # the client re-mints once after 401, never for a state conflict
                        self.assertEqual(len(self.requests) - before, 1)


class RedactionTests(CatalogCase):
    def test_envelope_refusal_uses_raw_success(self) -> None:
        secret = 'escaped"\\é'
        encoded = json.dumps(secret)[1:-1]
        client = self.client(lambda _: httpx.Response(200, json=refusal(encoded + " success")), secret=secret, token="success")
        with self.assertLogs("zuora_plugin", level="INFO") as audit:
            error = self.finish_op("update_product", client, {"product_id": "K-1", "body": {"Description": "corrected"}})["error"]
        self.assertEqual(error["code"], "zuora.validation_failed")
        self.assertIn(encoded, error["message"])
        self.assertNotIn(encoded, " ".join(audit.output))


if __name__ == "__main__":
    unittest.main()
