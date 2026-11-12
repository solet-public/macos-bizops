"""All 28 operations in Zuora's five legacy catalog sections.

Verified against developer.zuora.com/v1-api-reference/api (2026-11-06
reference, retrieved 2026-11-07): Products, Product Rate Plans, Product Rate
Plan Charges, Product Charge Definitions and Catalog Groups. Commerce is a
separate API section and remains available through ``request``.

Bodies retain every vendor field, including custom fields, nulls and empty
values. Vendor RBAC and catalog-state preconditions determine permission to
update/delete; no local write policy or invented concurrency token applies.
The object routes expose WSDL-versioned fields; the newer charge read also
requires X-Zuora-WSDL-Version >155.0 in its introductory note, even though
the reference omits it from that route's header table.
"""

from __future__ import annotations

from typing import Final

from .commerce_fields import IDEMPOTENT_HEADERS, PAGE_FIELDS, PREVIEW_EXISTING_HEADERS, V1_HEADERS, page_check, vendor_body
from .typed_operations import Field, Operation

WSDL_HEADERS: Final[dict[str, Field]] = {**V1_HEADERS, "x_zuora_wsdl_version": Field("X-Zuora-WSDL-Version", "header")}
CREATE_HEADERS: Final[dict[str, Field]] = {**WSDL_HEADERS, "idempotency_key": IDEMPOTENT_HEADERS["idempotency_key"]}
DEFINITION_HEADERS: Final[dict[str, Field]] = dict(PREVIEW_EXISTING_HEADERS)
DEFINITION_CREATE_HEADERS: Final[dict[str, Field]] = {**DEFINITION_HEADERS, "idempotency_key": IDEMPOTENT_HEADERS["idempotency_key"]}
CHARGE_READ_HEADERS: Final[dict[str, Field]] = {**DEFINITION_HEADERS, "x_zuora_wsdl_version": Field("X-Zuora-WSDL-Version", "header", required=True)}
REJECT_UNKNOWN: Final[dict[str, Field]] = {"reject_unknown_fields": Field("rejectUnknownFields", "bool")}
FIELDS: Final[dict[str, Field]] = {"fields": Field("fields", "text")}
SHOW_DEFINITIONS: Final[dict[str, Field]] = {"show_charge_definitions": Field("show-charge-definitions", "bool")}
HIDE_INHERITED: Final[dict[str, Field]] = {"hide_inherited_values": Field("hide-inherited-values", "bool")}
PRODUCT_ID: Final[dict[str, Field]] = {"product_id": Field("product_id", "str_id", required=True)}
RATE_PLAN_ID: Final[dict[str, Field]] = {"rate_plan_id": Field("rate_plan_id", "str_id", required=True)}
CHARGE_ID: Final[dict[str, Field]] = {"charge_id": Field("charge_id", "str_id", required=True)}
DEFINITION_KEY: Final[dict[str, Field]] = {"definition_key": Field("definition_key", "str_id", required=True)}
GROUP_KEY: Final[dict[str, Field]] = {"catalog_group_key": Field("catalog_group_key", "str_id", required=True)}

_PRODUCT = "/v1/object/product"
_PLAN = "/v1/object/product-rate-plan"
_CHARGE = "/v1/object/product-rate-plan-charge"
_DEFINITION = "/v1/product-charge-definitions"
_GROUP = "/v1/catalog-groups"

PRODUCT_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("create_product", "POST", _PRODUCT, query_fields=REJECT_UNKNOWN, header_fields=CREATE_HEADERS, body=vendor_body("Name", "EffectiveStartDate", "EffectiveEndDate"), envelope=True),
    Operation("get_product", "GET", _PRODUCT + "/{product_id}", path_fields=PRODUCT_ID, query_fields=FIELDS, header_fields=WSDL_HEADERS, envelope=True),
    Operation("update_product", "PUT", _PRODUCT + "/{product_id}", path_fields=PRODUCT_ID, query_fields=REJECT_UNKNOWN, header_fields=WSDL_HEADERS, body=vendor_body(), envelope=True),
    Operation("delete_product", "DELETE", _PRODUCT + "/{product_id}", path_fields=PRODUCT_ID, header_fields=V1_HEADERS, envelope=True),
)

RATE_PLAN_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("list_product_rate_plans", "GET", "/v1/products/{product_key}/product-rate-plans", path_fields={"product_key": Field("product_key", "str_id", required=True)}, query_fields={**PAGE_FIELDS, **SHOW_DEFINITIONS}, query_check=page_check, header_fields=V1_HEADERS, paging="page_number", envelope=True),
    Operation("get_product_rate_plan", "GET", "/v1/product-rate-plans/{rate_plan_id}", path_fields=RATE_PLAN_ID, header_fields=V1_HEADERS, envelope=True),
    Operation("get_product_rate_plans_by_external_id", "GET", "/v1/product-rate-plans/external-id/{external_id}", path_fields={"external_id": Field("external_id", "text", required=True)}, header_fields=V1_HEADERS, envelope=True),
    Operation("create_product_rate_plan", "POST", _PLAN, query_fields=REJECT_UNKNOWN, header_fields=CREATE_HEADERS, body=vendor_body("Name", "ProductId"), envelope=True),
    Operation("get_product_rate_plan_object", "GET", _PLAN + "/{rate_plan_id}", path_fields=RATE_PLAN_ID, query_fields=FIELDS, header_fields=WSDL_HEADERS, envelope=True),
    Operation("update_product_rate_plan", "PUT", _PLAN + "/{rate_plan_id}", path_fields=RATE_PLAN_ID, query_fields=REJECT_UNKNOWN, header_fields=WSDL_HEADERS, body=vendor_body(), envelope=True),
    Operation("delete_product_rate_plan", "DELETE", _PLAN + "/{rate_plan_id}", path_fields=RATE_PLAN_ID, header_fields=V1_HEADERS, envelope=True),
)

# Required fields are the top-level fields marked required on the vendor's
# create/update object schemas (the update schema has no ChargeType or Name).
_CHARGE_REQUIRED: Final[tuple[str, ...]] = ("BillCycleType", "BillingPeriod", "ChargeModel", "ProductRatePlanChargeTierData", "ProductRatePlanId", "TriggerEvent", "UseDiscountSpecificAccountingCode")
CHARGE_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("get_product_rate_plan_charge", "GET", "/v1/product-rate-plan-charges/{charge_key}", path_fields={"charge_key": Field("charge_key", "str_id", required=True)}, query_fields=SHOW_DEFINITIONS, header_fields=CHARGE_READ_HEADERS, envelope=True),
    Operation("create_product_rate_plan_charge", "POST", _CHARGE, query_fields=REJECT_UNKNOWN, header_fields=CREATE_HEADERS, body=vendor_body(*_CHARGE_REQUIRED, "ChargeType", "Name"), envelope=True),
    Operation("get_product_rate_plan_charge_object", "GET", _CHARGE + "/{charge_id}", path_fields=CHARGE_ID, query_fields=FIELDS, header_fields=WSDL_HEADERS, envelope=True),
    Operation("update_product_rate_plan_charge", "PUT", _CHARGE + "/{charge_id}", path_fields=CHARGE_ID, query_fields=REJECT_UNKNOWN, header_fields=WSDL_HEADERS, body=vendor_body(*_CHARGE_REQUIRED), envelope=True),
    Operation("delete_product_rate_plan_charge", "DELETE", _CHARGE + "/{charge_id}", path_fields=CHARGE_ID, header_fields=V1_HEADERS, envelope=True),
)

DEFINITION_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("create_product_charge_definition", "POST", _DEFINITION, header_fields=DEFINITION_CREATE_HEADERS, body=vendor_body(), envelope=True),
    Operation("list_product_charge_definitions", "GET", _DEFINITION, query_fields={"charge": Field("charge", "text"), "rateplan": Field("rateplan", "text"), **HIDE_INHERITED}, header_fields=DEFINITION_HEADERS, envelope=True),
    Operation("create_product_charge_definitions", "POST", _DEFINITION + "/bulk", header_fields=DEFINITION_CREATE_HEADERS, body=vendor_body(), envelope=True),
    Operation("update_product_charge_definitions", "PUT", _DEFINITION + "/bulk", header_fields=DEFINITION_HEADERS, body=vendor_body(), envelope=True),
    Operation("get_product_charge_definition", "GET", _DEFINITION + "/{definition_key}", path_fields=DEFINITION_KEY, query_fields=HIDE_INHERITED, header_fields=DEFINITION_HEADERS, envelope=True),
    Operation("update_product_charge_definition", "PUT", _DEFINITION + "/{definition_key}", path_fields=DEFINITION_KEY, header_fields=DEFINITION_HEADERS, body=vendor_body(), envelope=True),
    Operation("delete_product_charge_definition", "DELETE", _DEFINITION + "/{definition_key}", path_fields=DEFINITION_KEY, header_fields=DEFINITION_HEADERS, envelope=True),
)

GROUP_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("create_catalog_group", "POST", _GROUP, header_fields=IDEMPOTENT_HEADERS, body=vendor_body(), envelope=True),
    Operation("list_catalog_groups", "GET", _GROUP, query_fields=PAGE_FIELDS, query_check=page_check, header_fields=V1_HEADERS, paging="page_number", envelope=True),
    Operation("get_catalog_group", "GET", _GROUP + "/{catalog_group_key}", path_fields=GROUP_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation("update_catalog_group", "PUT", _GROUP + "/{catalog_group_key}", path_fields=GROUP_KEY, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation("delete_catalog_group", "DELETE", _GROUP + "/{catalog_group_key}", path_fields=GROUP_KEY, header_fields=V1_HEADERS, envelope=True),
)

OPERATIONS: Final[tuple[Operation, ...]] = (*PRODUCT_OPERATIONS, *RATE_PLAN_OPERATIONS, *CHARGE_OPERATIONS, *DEFINITION_OPERATIONS, *GROUP_OPERATIONS)
