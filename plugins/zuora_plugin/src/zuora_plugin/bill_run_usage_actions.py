"""Zuora Bill Runs and Usage: every operation in the vendor's Bill Run, Billing Preview Run and Usage sections.

Checked against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/bill-run``, ``/billing-preview-run`` and ``/usage``,
reference version 2026-11-06): path, method, query parameters, documented
request headers and required top-level body fields for all 6 Bill Run, both
Billing Preview Run and all 6 Usage operations.

Bill run lifecycle: create (ad hoc or scheduled, with filters), read its
status, post its draft invoices (``post_bill_run``), cancel it (terminal;
``cancelOnce`` cancels one occurrence of a scheduled run), delete it, and
email its billing documents. A billing preview run forecasts future invoice
and credit memo items for a batch of accounts without billing them (create,
then read its status and result file URL).

Usage lifecycle: import a usage file (Zuora answers with a
``checkImportStatus`` URL; only Completed means imported, and the import is
atomic), or create one usage record through the object route, read it (with
``fields``), correct it, and delete it. The rate detail of a usage-based
invoice item is read by invoice item id.
"""

from __future__ import annotations

from typing import Any, Final

from .commerce_fields import IDEMPOTENT_HEADERS, V1_HEADERS, vendor_body
from .typed_operations import BodyBuilder, Field, Operation, PathGate, RawBody, multipart_file

BILL_RUN_ID: Final[dict[str, Field]] = {"bill_run_id": Field("bill_run_id", "str_id", required=True)}
USAGE_ID: Final[dict[str, Field]] = {"usage_id": Field("usage_id", "str_id", required=True)}
# X-Zuora-WSDL-Version: listed in the header parameters of POST /v1/object/usage, and named in the
# field notes of POST /v1/usage (the PRODUCT_RATE_PLAN_CHARGE_ID column) and GET /v1/object/usage/{id}
# (ProductRatePlanChargeNumber), which need it at 146 or higher.
WSDL_HEADER: Final[dict[str, Field]] = {"x_zuora_wsdl_version": Field("X-Zuora-WSDL-Version", "header")}
USAGE_CREATE_HEADERS: Final[dict[str, Field]] = {**IDEMPOTENT_HEADERS, **WSDL_HEADER}
USAGE_IMPORT_HEADERS: Final[dict[str, Field]] = {**IDEMPOTENT_HEADERS, **WSDL_HEADER}
USAGE_READ_HEADERS: Final[dict[str, Field]] = {**V1_HEADERS, **WSDL_HEADER}
REJECT_UNKNOWN: Final[dict[str, Field]] = {"reject_unknown_fields": Field("rejectUnknownFields", "bool")}
# The usage import field documents "excel, csv, and zip".
USAGE_MEDIA: Final[dict[str, str]] = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".zip": "application/zip",
}

_BILL_RUN = "/v1/bill-runs/{bill_run_id}"
_PREVIEW = "/v1/billing-preview-runs"
_USAGE = "/v1/object/usage/{usage_id}"


def usage_upload() -> BodyBuilder:
    """The usage file (``input_path``) as the ``file`` part of Zuora's import."""

    def build(params: dict[str, Any], gate: PathGate) -> RawBody:
        return multipart_file(gate, params.get("input_path"), param_name="input_path", media_types=USAGE_MEDIA)

    return build


BILL_RUN_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("create_bill_run", "POST", "/v1/bill-runs", header_fields=IDEMPOTENT_HEADERS, body=vendor_body(), envelope=True),
    Operation("get_bill_run", "GET", _BILL_RUN, path_fields=BILL_RUN_ID, header_fields=V1_HEADERS, envelope=True),
    Operation("post_bill_run", "PUT", _BILL_RUN + "/post", path_fields=BILL_RUN_ID, header_fields=V1_HEADERS, body=vendor_body("invoiceDate"), envelope=True),
    Operation("cancel_bill_run", "PUT", _BILL_RUN + "/cancel", path_fields=BILL_RUN_ID, header_fields=V1_HEADERS, body=vendor_body(optional=True), envelope=True),
    Operation("delete_bill_run", "DELETE", _BILL_RUN, path_fields=BILL_RUN_ID, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "email_bill_run_documents",
        "POST",
        "/v1/bill-runs/{bill_run_key}/emails",
        path_fields={"bill_run_key": Field("bill_run_key", "str_id", required=True)},
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(optional=True),
        envelope=True,
    ),
    Operation("create_billing_preview_run", "POST", _PREVIEW, header_fields=IDEMPOTENT_HEADERS, body=vendor_body("targetDate"), envelope=True),
    Operation(
        "get_billing_preview_run",
        "GET",
        _PREVIEW + "/{billing_preview_run_id}",
        path_fields={"billing_preview_run_id": Field("billing_preview_run_id", "str_id", required=True)},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
)

USAGE_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("import_usage", "POST", "/v1/usage", header_fields=USAGE_IMPORT_HEADERS, body=usage_upload(), envelope=True),
    Operation(
        "get_usage_rate_detail",
        "GET",
        "/v1/invoices/invoice-item/{invoice_item_id}/usage-rate-detail",
        path_fields={"invoice_item_id": Field("invoice_item_id", "str_id", required=True)},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation(
        "create_usage",
        "POST",
        "/v1/object/usage",
        query_fields=REJECT_UNKNOWN,
        header_fields=USAGE_CREATE_HEADERS,
        body=vendor_body("Quantity", "StartDateTime", "UOM"),
        envelope=True,
    ),
    Operation("get_usage", "GET", _USAGE, path_fields=USAGE_ID, query_fields={"fields": Field("fields", "text")}, header_fields=USAGE_READ_HEADERS, envelope=True),
    Operation("update_usage", "PUT", _USAGE, path_fields=USAGE_ID, query_fields=REJECT_UNKNOWN, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation("delete_usage", "DELETE", _USAGE, path_fields=USAGE_ID, header_fields=V1_HEADERS, envelope=True),
)
