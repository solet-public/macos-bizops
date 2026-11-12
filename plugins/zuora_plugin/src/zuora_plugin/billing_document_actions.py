"""Zuora billing documents: every operation in the vendor's Billing Documents, Invoices, Credit Memos and Debit Memos sections.

Checked against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/{billing-documents,invoices,credit-memos,debit-memos}``,
reference version 2026-11-06): path, method, query parameters, documented
request headers and required top-level body fields for all 4 Billing
Documents, 22 Invoices, 31 Credit Memos and 24 Debit Memos operations.

Lifecycle (vendor-defined, Invoice Settlement where Zuora says so):

* Invoices are generated (``generate_billing_documents``, a bill run) or
  created standalone, read with their items, taxation items, files and
  application parts, corrected through ``update_invoice``/``update_invoices``,
  posted (``post_invoices``) and unposted (the reversible pair), split, emailed,
  given a PDF file, reversed (a credit memo is applied to it), written off
  (a credit memo applied to the balance, synchronously or as an async job),
  cancelled (kept in Canceled status) and deleted (Zuora works as a job for
  large invoices).
* Credit and debit memos are created from product rate plan charges, from an
  invoice or in bulk, read with items, parts, taxation items and files,
  corrected singly or in bulk, posted and unposted, cancelled (Draft memos
  only), deleted (Cancelled memos only), emailed and rendered to PDF. A credit
  memo is applied and unapplied
  (the reversible pair, also as async jobs), refunded, reversed and written
  off; a debit memo is collected (credit and payment applied to it) and
  written off. Void is not delete: cancel, reverse and write-off keep the
  document; delete removes it.

Every write is one vendor call; nothing here replays a write.
"""

from __future__ import annotations

from typing import Any, Final

from .commerce_fields import IDEMPOTENT_HEADERS, PAGE_FIELDS, V1_HEADERS, page_check, vendor_body
from .typed_operations import BodyBuilder, Field, Operation, PathGate, QueryCheck, RawBody, multipart_file

# The three pdf-status routes document Zuora-Track-Id, Zuora-Entity-Ids and Zuora-Version only.
PDF_STATUS_HEADERS: Final[dict[str, Field]] = {key: spec for key, spec in V1_HEADERS.items() if key != "zuora_org_ids"}
PDF_MEDIA: Final[dict[str, str]] = {".pdf": "application/pdf"}
PDF_STATUS_MAX_KEYS: Final[int] = 50

INVOICE_KEY: Final[dict[str, Field]] = {"invoice_key": Field("invoice_key", "str_id", required=True)}
CREDIT_MEMO_KEY: Final[dict[str, Field]] = {"credit_memo_key": Field("credit_memo_key", "str_id", required=True)}
CREDIT_MEMO_ID: Final[dict[str, Field]] = {"credit_memo_id": Field("credit_memo_id", "str_id", required=True)}
DEBIT_MEMO_KEY: Final[dict[str, Field]] = {"debit_memo_key": Field("debit_memo_key", "str_id", required=True)}
DEBIT_MEMO_ID: Final[dict[str, Field]] = {"debit_memo_id": Field("debit_memo_id", "str_id", required=True)}
ITEM_ID: Final[Field] = Field("item_id", "str_id", required=True)
JOB_ID: Final[dict[str, Field]] = {"job_id": Field("job_id", "str_id", required=True)}

_INV = "/v1/invoices/{invoice_key}"
_CM = "/v1/credit-memos/{credit_memo_key}"
_DM = "/v1/debit-memos/{debit_memo_key}"

BILLING_DOCUMENT_LIST_FIELDS: Final[dict[str, Field]] = {
    **PAGE_FIELDS,
    "account_id": Field("accountId", "text"),
    "account_number": Field("accountNumber", "text"),
    "document_date": Field("documentDate", "text"),
    "status": Field("status", "text"),
    "sort": Field("sort", "text"),
}
CREDIT_MEMO_LIST_FIELDS: Final[dict[str, Field]] = {
    **PAGE_FIELDS,
    "account_id": Field("accountId", "text"),
    "account_number": Field("accountNumber", "text"),
    "amount": Field("amount", "number"),
    "applied_amount": Field("appliedAmount", "number"),
    "auto_apply_upon_posting": Field("autoApplyUponPosting", "bool"),
    "created_by_id": Field("createdById", "text"),
    "created_date": Field("createdDate", "text"),
    "credit_memo_date": Field("creditMemoDate", "text"),
    "currency": Field("currency", "text"),
    "exclude_from_auto_apply_rules": Field("excludeFromAutoApplyRules", "bool"),
    "number": Field("number", "text"),
    "referred_invoice_id": Field("referredInvoiceId", "text"),
    "refund_amount": Field("refundAmount", "number"),
    "source_id": Field("sourceId", "text"),
    "status": Field("status", "text"),
    "target_date": Field("targetDate", "text"),
    "tax_amount": Field("taxAmount", "number"),
    "total_tax_exempt_amount": Field("totalTaxExemptAmount", "number"),
    "transferred_to_accounting": Field("transferredToAccounting", "text"),
    "unapplied_amount": Field("unappliedAmount", "number"),
    "updated_by_id": Field("updatedById", "text"),
    "updated_date": Field("updatedDate", "text"),
    "sort": Field("sort", "text"),
}
DEBIT_MEMO_LIST_FIELDS: Final[dict[str, Field]] = {
    **PAGE_FIELDS,
    "account_id": Field("accountId", "text"),
    "account_number": Field("accountNumber", "text"),
    "amount": Field("amount", "number"),
    "balance": Field("balance", "number"),
    "be_applied_amount": Field("beAppliedAmount", "number"),
    "created_by_id": Field("createdById", "text"),
    "created_date": Field("createdDate", "text"),
    "currency": Field("currency", "text"),
    "debit_memo_date": Field("debitMemoDate", "text"),
    "due_date": Field("dueDate", "text"),
    "number": Field("number", "text"),
    "referred_invoice_id": Field("referredInvoiceId", "text"),
    "status": Field("status", "text"),
    "target_date": Field("targetDate", "text"),
    "tax_amount": Field("taxAmount", "number"),
    "total_tax_exempt_amount": Field("totalTaxExemptAmount", "number"),
    "updated_by_id": Field("updatedById", "text"),
    "updated_date": Field("updatedDate", "text"),
    "sort": Field("sort", "text"),
}
_MEMO_ITEM_FILTERS: Final[dict[str, Field]] = {
    **PAGE_FIELDS,
    "amount": Field("amount", "number"),
    "created_by_id": Field("createdById", "text"),
    "created_date": Field("createdDate", "text"),
    "id": Field("id", "text"),
    "service_end_date": Field("serviceEndDate", "text"),
    "service_start_date": Field("serviceStartDate", "text"),
    "sku": Field("sku", "text"),
    "sku_name": Field("skuName", "text"),
    "source_item_id": Field("sourceItemId", "text"),
    "subscription_id": Field("subscriptionId", "text"),
    "updated_by_id": Field("updatedById", "text"),
    "updated_date": Field("updatedDate", "text"),
    "sort": Field("sort", "text"),
}
CREDIT_MEMO_ITEM_FIELDS: Final[dict[str, Field]] = {**_MEMO_ITEM_FILTERS, "applied_amount": Field("appliedAmount", "number"), "refund_amount": Field("refundAmount", "number")}
DEBIT_MEMO_ITEM_FIELDS: Final[dict[str, Field]] = {**_MEMO_ITEM_FILTERS, "be_applied_amount": Field("beAppliedAmount", "number")}


def pdf_keys_check(param: str) -> QueryCheck:
    """Zuora's pdf-status routes take at most 50 comma-separated document keys."""

    def check(params: dict[str, Any]) -> None:
        value = params.get(param)
        if isinstance(value, str) and len([key for key in value.split(",") if key.strip()]) > PDF_STATUS_MAX_KEYS:
            raise ValueError(f"{param} takes at most {PDF_STATUS_MAX_KEYS} comma-separated keys (Zuora's documented maximum)")

    return check


def pdf_upload() -> BodyBuilder:
    """The one PDF file (``input_path``) the invoice, credit memo and debit memo file routes accept."""

    def build(params: dict[str, Any], gate: PathGate) -> RawBody:
        return multipart_file(gate, params.get("input_path"), param_name="input_path", media_types=PDF_MEDIA)

    return build


def _read(name: str, path: str, fields: dict[str, Field], **extra: Any) -> Operation:
    """A GET on a v1 billing route with the four documented headers."""
    return Operation(name, "GET", path, path_fields=fields, header_fields=extra.pop("headers", V1_HEADERS), envelope=True, **extra)


def _page(name: str, path: str, fields: dict[str, Field], query: dict[str, Field] = PAGE_FIELDS) -> Operation:
    """A page-numbered v1 list: ``page`` needs ``page_size``; ``next_page`` comes from Zuora's ``nextPage`` link."""
    return _read(name, path, fields, query_fields=query, query_check=page_check, paging="page_number")


def _write(name: str, method: str, path: str, fields: dict[str, Field], body: BodyBuilder | None = None, *, idempotent: bool = False, **extra: Any) -> Operation:
    """A v1 billing write: ``Idempotency-Key`` where the vendor documents it, ``success: false`` is a refusal."""
    headers = IDEMPOTENT_HEADERS if idempotent else V1_HEADERS
    return Operation(name, method, path, path_fields=fields, header_fields=headers, body=body, envelope=True, **extra)


def _upload(name: str, path: str, fields: dict[str, Field]) -> Operation:
    return _write(name, "POST", path, fields, pdf_upload(), idempotent=True)


def _pdf_status(name: str, path: str, keys: str) -> Operation:
    query = {keys: Field(_camel(keys), "text", required=True)}
    return _read(name, path, {}, query_fields=query, query_check=pdf_keys_check(keys), headers=PDF_STATUS_HEADERS)


def _camel(snake: str) -> str:
    head, *rest = snake.split("_")
    return head + "".join(part.title() for part in rest)


BILLING_DOCUMENT_OPERATIONS: Final[tuple[Operation, ...]] = (
    _page("list_billing_documents", "/v1/billing-documents", {}, BILLING_DOCUMENT_LIST_FIELDS),
    _write(
        "generate_billing_documents",
        "POST",
        "/v1/accounts/{account_key}/billing-documents/generate",
        {"account_key": Field("account_key", "str_id", required=True)},
        vendor_body("effectiveDate", "targetDate"),
        idempotent=True,
    ),
    _write("create_billing_document_files_deletion_job", "POST", "/v1/accounts/billing-documents/files/deletion-jobs", {}, vendor_body(), idempotent=True),
    _read("get_billing_document_files_deletion_job", "/v1/accounts/billing-documents/files/deletion-jobs/{job_id}", JOB_ID),
)

INVOICE_OPERATIONS: Final[tuple[Operation, ...]] = (
    _write("create_standalone_invoice", "POST", "/v1/invoices", {}, vendor_body("invoiceDate"), idempotent=True),
    _write("create_standalone_invoices", "POST", "/v1/invoices/batch", {}, vendor_body(), idempotent=True),
    _write("update_invoices", "PUT", "/v1/invoices", {}, vendor_body()),
    _write("post_invoices", "POST", "/v1/invoices/bulk-post", {}, vendor_body(), idempotent=True),
    _pdf_status("get_invoice_pdf_status", "/v1/invoices/pdf-status", "invoice_keys"),
    _write("update_invoice", "PUT", _INV, INVOICE_KEY, vendor_body()),
    _write("delete_invoice", "DELETE", _INV, INVOICE_KEY),
    _write("cancel_invoice", "PUT", _INV + "/cancel", INVOICE_KEY),
    _write("unpost_invoice", "PUT", _INV + "/unpost", INVOICE_KEY),
    _write("reverse_invoice", "PUT", _INV + "/reverse", INVOICE_KEY, vendor_body(optional=True)),
    _write("split_invoice", "PUT", _INV + "/split", INVOICE_KEY, vendor_body()),
    _write("write_off_invoice", "PUT", _INV + "/write-off", INVOICE_KEY, vendor_body(optional=True)),
    _write("write_off_invoice_async", "POST", _INV + "/write-off-async", INVOICE_KEY, vendor_body(optional=True)),
    _read("get_invoice_write_off_async_job", "/v1/invoices/write-off-async-jobs/{async_job_id}", {"async_job_id": Field("async_job_id", "str_id", required=True)}),
    _write("email_invoice", "POST", _INV + "/emails", INVOICE_KEY, vendor_body(optional=True), idempotent=True),
    _read("list_invoice_application_parts", _INV + "/application-parts", INVOICE_KEY, paging="next_link"),
    _page("list_invoice_files", _INV + "/files", INVOICE_KEY),
    _upload("upload_invoice_file", _INV + "/files", INVOICE_KEY),
    _page("list_invoice_items", _INV + "/items", INVOICE_KEY),
    _page("list_invoice_item_taxation_items", _INV + "/items/{item_id}/taxation-items", {**INVOICE_KEY, "item_id": ITEM_ID}),
    _write("create_invoice_taxation_items", "POST", _INV + "/taxation-items", INVOICE_KEY, vendor_body(), idempotent=True),
)

CREDIT_MEMO_OPERATIONS: Final[tuple[Operation, ...]] = (
    _page("list_credit_memos", "/v1/credit-memos", {}, CREDIT_MEMO_LIST_FIELDS),
    _write("create_credit_memo", "POST", "/v1/credit-memos", {}, vendor_body(), idempotent=True),
    _write("create_credit_memo_from_invoice", "POST", "/v1/credit-memos/invoice/{invoice_key}", INVOICE_KEY, vendor_body("invoiceId"), idempotent=True),
    _write("create_credit_memos", "POST", "/v1/credit-memos/bulk", {}, vendor_body("sourceType"), idempotent=True),
    _write("update_credit_memos", "PUT", "/v1/credit-memos/bulk", {}, vendor_body()),
    _pdf_status("get_credit_memo_pdf_status", "/v1/credit-memos/pdf-status", "credit_memo_keys"),
    _read("get_credit_memo", _CM, CREDIT_MEMO_KEY),
    _write("update_credit_memo", "PUT", _CM, CREDIT_MEMO_KEY, vendor_body()),
    _write("delete_credit_memo", "DELETE", _CM, CREDIT_MEMO_KEY),
    _write("post_credit_memo", "PUT", _CM + "/post", CREDIT_MEMO_KEY),
    _write("unpost_credit_memo", "PUT", _CM + "/unpost", CREDIT_MEMO_KEY),
    _write("cancel_credit_memo", "PUT", _CM + "/cancel", CREDIT_MEMO_KEY),
    _write("apply_credit_memo", "PUT", _CM + "/apply", CREDIT_MEMO_KEY, vendor_body()),
    _write("unapply_credit_memo", "PUT", _CM + "/unapply", CREDIT_MEMO_KEY, vendor_body()),
    _write("apply_credit_memo_async", "PUT", _CM + "/apply-async", CREDIT_MEMO_KEY, vendor_body()),
    _write("unapply_credit_memo_async", "PUT", _CM + "/unapply-async", CREDIT_MEMO_KEY, vendor_body()),
    _read("get_credit_memo_apply_async_job", "/v1/credit-memos/apply-async-jobs/{apply_async_job_id}", {"apply_async_job_id": Field("apply_async_job_id", "str_id", required=True)}),
    _read("get_credit_memo_unapply_async_job", "/v1/credit-memos/unapply-async-jobs/{unapply_async_job_id}", {"unapply_async_job_id": Field("unapply_async_job_id", "str_id", required=True)}),
    _write("refund_credit_memo", "POST", _CM + "/refund", CREDIT_MEMO_KEY, vendor_body("totalAmount", "type"), idempotent=True),
    _write("reverse_credit_memo", "PUT", _CM + "/reverse", CREDIT_MEMO_KEY, vendor_body(optional=True)),
    _write("write_off_credit_memo", "PUT", "/v1/credit-memos/{credit_memo_id}/write-off", CREDIT_MEMO_ID, vendor_body(optional=True)),
    _write("email_credit_memo", "POST", _CM + "/emails", CREDIT_MEMO_KEY, vendor_body(optional=True), idempotent=True),
    _write("generate_credit_memo_pdf", "POST", _CM + "/pdfs", CREDIT_MEMO_KEY, idempotent=True),
    _page("list_credit_memo_files", _CM + "/files", CREDIT_MEMO_KEY),
    _upload("upload_credit_memo_file", _CM + "/files", CREDIT_MEMO_KEY),
    _page("list_credit_memo_items", _CM + "/items", CREDIT_MEMO_KEY, CREDIT_MEMO_ITEM_FIELDS),
    _read("get_credit_memo_item", _CM + "/items/{item_id}", {**CREDIT_MEMO_KEY, "item_id": ITEM_ID}),
    _page("list_credit_memo_item_taxation_items", "/v1/credit-memos/{credit_memo_id}/items/{item_id}/taxation-items", {**CREDIT_MEMO_ID, "item_id": ITEM_ID}),
    _write("create_credit_memo_taxation_items", "POST", _CM + "/taxation-items", CREDIT_MEMO_KEY, vendor_body(), idempotent=True),
    _page("list_credit_memo_parts", _CM + "/parts", CREDIT_MEMO_KEY),
    _read("get_credit_memo_part", _CM + "/parts/{part_id}", {**CREDIT_MEMO_KEY, "part_id": Field("part_id", "str_id", required=True)}),
)

DEBIT_MEMO_OPERATIONS: Final[tuple[Operation, ...]] = (
    _page("list_debit_memos", "/v1/debit-memos", {}, DEBIT_MEMO_LIST_FIELDS),
    _write("create_debit_memo", "POST", "/v1/debit-memos", {}, vendor_body(), idempotent=True),
    _write("create_debit_memo_from_invoice", "POST", "/v1/debit-memos/invoice/{invoice_key}", INVOICE_KEY, vendor_body("invoiceId"), idempotent=True),
    _write("create_debit_memos", "POST", "/v1/debit-memos/bulk", {}, vendor_body("sourceType"), idempotent=True),
    _write("update_debit_memos", "PUT", "/v1/debit-memos/bulk", {}, vendor_body()),
    _write("update_debit_memo_due_dates", "PUT", "/v1/debit-memos", {}, vendor_body()),
    _pdf_status("get_debit_memo_pdf_status", "/v1/debit-memos/pdf-status", "debit_memo_keys"),
    _read("get_debit_memo", _DM, DEBIT_MEMO_KEY),
    _write("update_debit_memo", "PUT", _DM, DEBIT_MEMO_KEY, vendor_body()),
    _write("delete_debit_memo", "DELETE", _DM, DEBIT_MEMO_KEY),
    _write("post_debit_memo", "PUT", _DM + "/post", DEBIT_MEMO_KEY),
    _write("unpost_debit_memo", "PUT", _DM + "/unpost", DEBIT_MEMO_KEY),
    _write("cancel_debit_memo", "PUT", _DM + "/cancel", DEBIT_MEMO_KEY),
    _write("collect_debit_memo", "POST", _DM + "/collect", DEBIT_MEMO_KEY, vendor_body(optional=True), idempotent=True),
    _write("write_off_debit_memo", "PUT", _DM + "/write-off", DEBIT_MEMO_KEY, vendor_body(optional=True)),
    _write("email_debit_memo", "POST", _DM + "/emails", DEBIT_MEMO_KEY, vendor_body(optional=True), idempotent=True),
    _write("generate_debit_memo_pdf", "POST", _DM + "/pdfs", DEBIT_MEMO_KEY, idempotent=True),
    _read("list_debit_memo_application_parts", "/v1/debit-memos/{debit_memo_id}/application-parts", DEBIT_MEMO_ID, paging="next_link"),
    _page("list_debit_memo_files", _DM + "/files", DEBIT_MEMO_KEY),
    _upload("upload_debit_memo_file", _DM + "/files", DEBIT_MEMO_KEY),
    _page("list_debit_memo_items", _DM + "/items", DEBIT_MEMO_KEY, DEBIT_MEMO_ITEM_FIELDS),
    _read("get_debit_memo_item", _DM + "/items/{item_id}", {**DEBIT_MEMO_KEY, "item_id": ITEM_ID}),
    _page("list_debit_memo_item_taxation_items", "/v1/debit-memos/{debit_memo_id}/items/{item_id}/taxation-items", {**DEBIT_MEMO_ID, "item_id": ITEM_ID}),
    _write("create_debit_memo_taxation_items", "POST", _DM + "/taxation-items", DEBIT_MEMO_KEY, vendor_body(), idempotent=True),
)
