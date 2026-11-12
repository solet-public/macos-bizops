"""Typed legacy catalog process declarations; vendor RBAC controls writes and deletes."""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .commerce_params import EDGE, NONE, QUEUED_NOTE, READ, WRITE, body, flag, integer, text
from .workflow_processes import QUEUED


class CatalogProductProcesses:
    """Catalog operations dispatched by ZuoraPlugin through the shared worker."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="create_product",
        display_name="Zuora: Create Product",
        description="Create a product; Name, EffectiveStartDate and EffectiveEndDate are required. Every documented field and custom field is forwarded. (POST /v1/object/product)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "reject_unknown_fields": flag("rejectUnknownFields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
            "idempotency_key": text("Idempotency-Key request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_product(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_product", params, state)

    @platform_process(
        name="get_product",
        display_name="Zuora: Get Product",
        description="Read the object product by id, optionally selecting fields and a WSDL version. (GET /v1/object/product/{product_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "product_id": text("product_id. Forwarded to the vendor with its documented name.", required=True),
            "fields": text("fields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product", params, state)

    @platform_process(
        name="update_product",
        display_name="Zuora: Update Product",
        description="Correct a product, including optional fields and effective dates; use vendor-documented empty values to clear fields. (PUT /v1/object/product/{product_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "product_id": text("product_id. Forwarded to the vendor with its documented name.", required=True),
            "reject_unknown_fields": flag("rejectUnknownFields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_product(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_product", params, state)

    @platform_process(
        name="delete_product",
        display_name="Zuora: Delete Product",
        description="Delete a product by id; Zuora enforces references and vendor-role permissions. (DELETE /v1/object/product/{product_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "product_id": text("product_id. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_product(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_product", params, state)

    @platform_process(
        name="list_product_rate_plans",
        display_name="Zuora: List Product Rate Plans",
        description="List a product's rate plans with page/page_size and show_charge_definitions; completion includes next_page. (GET /v1/products/{product_key}/product-rate-plans)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "product_key": text("product_key. Forwarded to the vendor with its documented name.", required=True),
            "page": integer("page. Forwarded to the vendor with its documented name."),
            "page_size": integer("pageSize. Forwarded to the vendor with its documented name."),
            "show_charge_definitions": flag("show-charge-definitions. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_product_rate_plans(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_product_rate_plans", params, state)

    @platform_process(
        name="get_product_rate_plan",
        display_name="Zuora: Get Product Rate Plan",
        description="Read a product rate plan by id through the v1 rate-plan resource. (GET /v1/product-rate-plans/{rate_plan_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "rate_plan_id": text("rate_plan_id. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product_rate_plan(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product_rate_plan", params, state)

    @platform_process(
        name="get_product_rate_plans_by_external_id",
        display_name="Zuora: Get Product Rate Plans By External Id",
        description="Read all rate plans matching an external id; the external id is encoded as one path component. Zuora documents no pagination for this lookup. (GET /v1/product-rate-plans/external-id/{external_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "external_id": text("external_id. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product_rate_plans_by_external_id(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product_rate_plans_by_external_id", params, state)

    @platform_process(
        name="create_product_rate_plan",
        display_name="Zuora: Create Product Rate Plan",
        description="Create a product rate plan; body needs Name and ProductId and accepts effective dates, external ids, currencies and all vendor custom fields. (POST /v1/object/product-rate-plan)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "reject_unknown_fields": flag("rejectUnknownFields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
            "idempotency_key": text("Idempotency-Key request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_product_rate_plan(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_product_rate_plan", params, state)

    @platform_process(
        name="get_product_rate_plan_object",
        display_name="Zuora: Get Product Rate Plan Object",
        description="Read the object rate plan by id with optional fields and WSDL version. (GET /v1/object/product-rate-plan/{rate_plan_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "rate_plan_id": text("rate_plan_id. Forwarded to the vendor with its documented name.", required=True),
            "fields": text("fields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product_rate_plan_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product_rate_plan_object", params, state)

    @platform_process(
        name="update_product_rate_plan",
        display_name="Zuora: Update Product Rate Plan",
        description="Correct a rate plan, its grade, dates, currencies, external ids and optional fields with the vendor body unchanged. (PUT /v1/object/product-rate-plan/{rate_plan_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "rate_plan_id": text("rate_plan_id. Forwarded to the vendor with its documented name.", required=True),
            "reject_unknown_fields": flag("rejectUnknownFields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_product_rate_plan(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_product_rate_plan", params, state)

    @platform_process(
        name="delete_product_rate_plan",
        display_name="Zuora: Delete Product Rate Plan",
        description="Delete a rate plan by id; Zuora enforces its catalog-state and reference preconditions. (DELETE /v1/object/product-rate-plan/{rate_plan_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "rate_plan_id": text("rate_plan_id. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_product_rate_plan(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_product_rate_plan", params, state)

    @platform_process(
        name="get_product_rate_plan_charge",
        display_name="Zuora: Get Product Rate Plan Charge",
        description="Read a charge by id or number with optional charge definitions; x_zuora_wsdl_version greater than 155.0 is required by the vendor. (GET /v1/product-rate-plan-charges/{charge_key})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "charge_key": text("charge_key. Forwarded to the vendor with its documented name.", required=True),
            "show_charge_definitions": flag("show-charge-definitions. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields.", required=True),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product_rate_plan_charge(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product_rate_plan_charge", params, state)

    @platform_process(
        name="create_product_rate_plan_charge",
        display_name="Zuora: Create Product Rate Plan Charge",
        description="Create a one-time, recurring or usage product charge with the vendor object body, including tiers and every documented pricing field. (POST /v1/object/product-rate-plan-charge)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "reject_unknown_fields": flag("rejectUnknownFields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
            "idempotency_key": text("Idempotency-Key request header."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_product_rate_plan_charge(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_product_rate_plan_charge", params, state)

    @platform_process(
        name="get_product_rate_plan_charge_object",
        display_name="Zuora: Get Product Rate Plan Charge Object",
        description="Read the object charge by id with optional fields and WSDL version; versions above 155.0 expose enhanced prepaid fields. (GET /v1/object/product-rate-plan-charge/{charge_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "charge_id": text("charge_id. Forwarded to the vendor with its documented name.", required=True),
            "fields": text("fields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_product_rate_plan_charge_object(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_product_rate_plan_charge_object", params, state)

    @platform_process(
        name="update_product_rate_plan_charge",
        display_name="Zuora: Update Product Rate Plan Charge",
        description="Correct a product charge with the vendor object update schema, including tiers, pricing, prepaid and custom fields. (PUT /v1/object/product-rate-plan-charge/{charge_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "charge_id": text("charge_id. Forwarded to the vendor with its documented name.", required=True),
            "reject_unknown_fields": flag("rejectUnknownFields. Forwarded to the vendor with its documented name."),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
            "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header; use greater than 155.0 for the charge read and enhanced prepaid fields."),
            "body": body("Vendor JSON request document; every documented field is accepted, including custom fields, nulls and empty values. See the catalog lifecycle article for required fields."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_product_rate_plan_charge(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_product_rate_plan_charge", params, state)

    @platform_process(
        name="delete_product_rate_plan_charge",
        display_name="Zuora: Delete Product Rate Plan Charge",
        description="Delete a product rate plan charge by id, subject to Zuora references and vendor RBAC. (DELETE /v1/object/product-rate-plan-charge/{charge_id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "charge_id": text("charge_id. Forwarded to the vendor with its documented name.", required=True),
            "zuora_version": text("Zuora-Version request header."),
            "zuora_entity_ids": text("Zuora-Entity-Ids request header."),
            "zuora_org_ids": text("Zuora-Org-Ids request header."),
            "zuora_track_id": text("Zuora-Track-Id request header."),
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_product_rate_plan_charge(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_product_rate_plan_charge", params, state)
