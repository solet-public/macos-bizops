"""Typed legacy catalog process declarations; vendor RBAC controls writes and deletes."""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .commerce_params import EDGE, NONE, QUEUED_NOTE, READ, WRITE, body, flag, integer, text
from .workflow_processes import QUEUED


class CatalogDefinitionProcesses:
    """Catalog operations dispatched by ZuoraPlugin through the shared worker."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="create_product_charge_definition",
        display_name="Zuora: Create Product Charge Definition",
        description="Create an attribute-based pricing charge definition; identify the charge by productRatePlanChargeId or productRatePlanChargeNumber in body. (POST /v1/product-charge-definitions)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "idempotency_key": text("Idempotency-Key request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_product_charge_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_product_charge_definition", params, state)

    @platform_process(
        name="list_product_charge_definitions",
        display_name="Zuora: List Product Charge Definitions",
        description="Read charge definitions filtered by charge or rateplan with hide_inherited_values; Zuora documents no continuation on this route. (GET /v1/product-charge-definitions)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "charge": text("charge. Forwarded to the vendor with its documented name."),
            "rateplan": text("rateplan. Forwarded to the vendor with its documented name."),
            "hide_inherited_values": flag("hide-inherited-values. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_product_charge_definitions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_product_charge_definitions", params, state)

    @platform_process(
        name="create_product_charge_definitions",
        display_name="Zuora: Create Product Charge Definitions",
        description="Create up to 1000 charge definitions using body.productChargeDefinitions, including all documented pricing fields. (POST /v1/product-charge-definitions/bulk)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "idempotency_key": text("Idempotency-Key request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_product_charge_definitions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_product_charge_definitions", params, state)

    @platform_process(
        name="update_product_charge_definitions",
        display_name="Zuora: Update Product Charge Definitions",
        description="Correct charge definitions in bulk using body.productChargeDefinitions and their ids or numbers; the body is forwarded unchanged. (PUT /v1/product-charge-definitions/bulk)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_product_charge_definitions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_product_charge_definitions", params, state)

    @platform_process(
        name="get_product_charge_definition",
        display_name="Zuora: Get Product Charge Definition",
        description="Read a charge definition by id or number, optionally hiding inherited values. (GET /v1/product-charge-definitions/{definition_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "definition_key": text("definition_key. Forwarded to the vendor with its documented name.", required=True),
            "hide_inherited_values": flag("hide-inherited-values. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product_charge_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product_charge_definition", params, state)

    @platform_process(
        name="update_product_charge_definition",
        display_name="Zuora: Update Product Charge Definition",
        description="Correct a charge definition including prices, dates, tax and term fields; all documented body fields are accepted. (PUT /v1/product-charge-definitions/{definition_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "definition_key": text("definition_key. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_product_charge_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_product_charge_definition", params, state)

    @platform_process(
        name="delete_product_charge_definition",
        display_name="Zuora: Delete Product Charge Definition",
        description="Delete an attribute-based pricing charge definition by id or number; vendor feature and role permissions apply. (DELETE /v1/product-charge-definitions/{definition_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"definition_key": text("definition_key. Forwarded to the vendor with its documented name.", required=True), "zuora_version": text("Zuora-Version request header."), "zuora_entity_ids": text("Zuora-Entity-Ids request header."), "zuora_track_id": text("Zuora-Track-Id request header.")},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_product_charge_definition(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_product_charge_definition", params, state)

    @platform_process(
        name="create_catalog_group",
        display_name="Zuora: Create Catalog Group",
        description="Create a Grading or Display catalog group with name, description and productRatePlans membership and grades. (POST /v1/catalog-groups)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "idempotency_key": text("Idempotency-Key request header."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_catalog_group(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_catalog_group", params, state)

    @platform_process(
        name="list_catalog_groups",
        display_name="Zuora: List Catalog Groups",
        description="List all catalog groups with page and page_size; completion includes next_page and the vendor nextPage link. (GET /v1/catalog-groups)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "page": integer("page. Forwarded to the vendor with its documented name."),
            "page_size": integer("pageSize. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_catalog_groups(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_catalog_groups", params, state)

    @platform_process(
        name="get_catalog_group",
        display_name="Zuora: Get Catalog Group",
        description="Read a catalog group by id or number with its product rate plans and grades. (GET /v1/catalog-groups/{catalog_group_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "catalog_group_key": text("catalog_group_key. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_catalog_group(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_catalog_group", params, state)

    @platform_process(
        name="update_catalog_group",
        display_name="Zuora: Update Catalog Group",
        description="Correct a catalog group, add or remove rate plans and clear mutable fields with empty values; its type cannot change. (PUT /v1/catalog-groups/{catalog_group_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "catalog_group_key": text("catalog_group_key. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_catalog_group(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_catalog_group", params, state)

    @platform_process(
        name="delete_catalog_group",
        display_name="Zuora: Delete Catalog Group",
        description="Delete a catalog group by id or number; vendor RBAC is the only authorization boundary. (DELETE /v1/catalog-groups/{catalog_group_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "catalog_group_key": text("catalog_group_key. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_catalog_group(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_catalog_group", params, state)
