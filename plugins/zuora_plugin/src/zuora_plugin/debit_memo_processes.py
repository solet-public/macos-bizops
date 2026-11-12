"""``@platform_process`` declarations for the Zuora Debit Memos family (mixed into ``ZuoraPlugin``).

Same contract as ``payment_processes``: EDGE, deferred completion, one vendor
operation from ``billing_document_actions`` per job, vendor RBAC the only
boundary.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .billing_document_params import DEBIT_MEMO_FILTER_PARAMS, DEBIT_MEMO_ID, DEBIT_MEMO_ITEM_FILTER_PARAMS, DEBIT_MEMO_KEY, INVOICE_KEY, ITEM_ID, PDF_INPUT, PDF_STATUS_HEADER_PARAMS, optional_body
from .commerce_params import EDGE, IDEMPOTENT_HEADER_PARAMS, NONE, PAGE_PARAMS, QUEUED_NOTE, READ, V1_HEADER_PARAMS, WRITE, body, text
from .workflow_processes import QUEUED


class DebitMemoProcesses:
    """Typed Debit Memo verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="list_debit_memos",
        display_name="Zuora: List Debit Memos",
        description="List debit memos (GET /v1/debit-memos) one page per call, filtered by account, amounts, balance, dates, due date, number, status or source invoice and sorted; next_page is the page to pass back, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={**DEBIT_MEMO_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_debit_memos(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_debit_memos", params, state)

    @platform_process(
        name="create_debit_memo",
        display_name="Zuora: Create Debit Memo",
        description="Create an ad-hoc debit memo from product rate plan charges (POST /v1/debit-memos): accountId or accountNumber, charges with amounts, dueDate, effectiveDate, reasonCode, autoPost, autoPay." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("accountId or accountNumber, charges (productRatePlanChargeId, amount, ...), currency, customRates, dueDate, effectiveDate, reasonCode, comment, autoPost, autoPay, number."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_debit_memo", params, state)

    @platform_process(
        name="create_debit_memo_from_invoice",
        display_name="Zuora: Create Debit Memo From Invoice",
        description="Create an ad-hoc debit memo from an invoice (POST /v1/debit-memos/invoice/{invoiceKey}); body needs invoiceId and carries items, reasonCode, effectiveDate, contacts, autoPost and autoPay." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": body("invoiceId (required), items (amount, skuName, taxItems), reasonCode, comment, effectiveDate, billToContactId, soldToContactId, autoPost, autoPay, taxAutoCalculation."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_debit_memo_from_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_debit_memo_from_invoice", params, state)

    @platform_process(
        name="create_debit_memos",
        display_name="Zuora: Create Debit Memos",
        description="Create up to 50 debit memos in one call (POST /v1/debit-memos/bulk) from invoices (sourceType Invoice) or product rate plan charges (sourceType Standalone); body needs sourceType and memos." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("sourceType (required: Invoice or Standalone) and memos, each shaped like the single create."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_debit_memos(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_debit_memos", params, state)

    @platform_process(
        name="update_debit_memos",
        display_name="Zuora: Update Debit Memos",
        description="Correct or clear the basic and finance fields of up to 50 debit memos in one call (PUT /v1/debit-memos/bulk); body carries memos, each with its id." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("memos: each with id and the fields to change (comment, dueDate, effectiveDate, reasonCode, items, autoPay, transferredToAccounting)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_debit_memos(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_debit_memos", params, state)

    @platform_process(
        name="update_debit_memo_due_dates",
        display_name="Zuora: Update Debit Memo Due Dates",
        description="Change the due dates of several debit memos in one call (PUT /v1/debit-memos); Zuora marks this route for deprecation in favour of update_debit_memos." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("debitMemos: each with id and dueDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_debit_memo_due_dates(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_debit_memo_due_dates", params, state)

    @platform_process(
        name="get_debit_memo_pdf_status",
        display_name="Zuora: Get Debit Memo PDF Status",
        description="Read the PDF generation status of up to 50 debit memos (GET /v1/debit-memos/pdf-status)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_keys": text("Comma-separated debit memo ids or numbers, at most 50.", required=True), **PDF_STATUS_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_debit_memo_pdf_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_debit_memo_pdf_status", params, state)

    @platform_process(
        name="get_debit_memo",
        display_name="Zuora: Get Debit Memo",
        description="Read one debit memo (GET /v1/debit-memos/{debitMemoKey}): status, amount, balance, amount applied to it, due date, source invoice and reason." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_debit_memo", params, state)

    @platform_process(
        name="update_debit_memo",
        display_name="Zuora: Update Debit Memo",
        description="Correct or clear a debit memo's basic and finance fields (PUT /v1/debit-memos/{debitMemoKey}): comment, dueDate, effectiveDate, reasonCode, items, autoPay, transferredToAccounting; send only the fields to change." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "body": body("Fields to change: autoPay, comment, dueDate, effectiveDate, items, reasonCode, transferredToAccounting and custom fields."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_debit_memo", params, state)

    @platform_process(
        name="delete_debit_memo",
        display_name="Zuora: Delete Debit Memo",
        description="Delete a debit memo (DELETE /v1/debit-memos/{debitMemoKey}); Zuora deletes only a Cancelled debit memo, so cancel it first." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_debit_memo", params, state)

    @platform_process(
        name="post_debit_memo",
        display_name="Zuora: Post Debit Memo",
        description="Post a draft debit memo to activate it (PUT /v1/debit-memos/{debitMemoKey}/post); unpost_debit_memo returns it to Draft." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def post_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("post_debit_memo", params, state)

    @platform_process(
        name="unpost_debit_memo",
        display_name="Zuora: Unpost Debit Memo",
        description="Return a posted debit memo to Draft (PUT /v1/debit-memos/{debitMemoKey}/unpost); Zuora refuses it once a credit memo or payment is applied to it." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unpost_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unpost_debit_memo", params, state)

    @platform_process(
        name="cancel_debit_memo",
        display_name="Zuora: Cancel Debit Memo",
        description="Cancel a draft debit memo (PUT /v1/debit-memos/{debitMemoKey}/cancel); Zuora cancels only Draft memos, and a Cancelled memo can then be deleted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_debit_memo", params, state)

    @platform_process(
        name="collect_debit_memo",
        display_name="Zuora: Collect Debit Memo",
        description="Collect a posted debit memo (POST /v1/debit-memos/{debitMemoKey}/collect): apply unapplied credit memos and payments oldest-first-largest-first, then process a payment for any open balance." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "body": optional_body("applyCredit, applicationOrder (CreditMemo, UnappliedPayment), collect, payment (paymentMethodId, gatewayId, ...)."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def collect_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("collect_debit_memo", params, state)

    @platform_process(
        name="write_off_debit_memo",
        display_name="Zuora: Write Off Debit Memo",
        description="Write off a debit memo (PUT /v1/debit-memos/{debitMemoKey}/write-off): Zuora creates a credit memo and applies it to the debit memo; needs the enhanced write-off permission. Body may set amount, items, memoDate and reasonCode." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "body": optional_body("amount, items, memoDate, reasonCode, comment, revenueImpacting, nonRevenueWriteOffAccountingCode and custom fields."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def write_off_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("write_off_debit_memo", params, state)

    @platform_process(
        name="email_debit_memo",
        display_name="Zuora: Email Debit Memo",
        description="Email a posted debit memo (POST /v1/debit-memos/{debitMemoKey}/emails) through the Manually email Debit Memo notification; body may set emailAddresses and includeAdditionalEmailAddresses." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "body": optional_body("emailAddresses, includeAdditionalEmailAddresses, pdfFileId, useEmailTemplateSetting."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def email_debit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("email_debit_memo", params, state)

    @platform_process(
        name="generate_debit_memo_pdf",
        display_name="Zuora: Generate Debit Memo PDF",
        description="Create a PDF file for a debit memo (POST /v1/debit-memos/{debitMemoKey}/pdfs); needs the billing document file generation feature and the Regenerate PDF permission." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def generate_debit_memo_pdf(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("generate_debit_memo_pdf", params, state)

    @platform_process(
        name="list_debit_memo_application_parts",
        display_name="Zuora: List Debit Memo Application Parts",
        description="List the payments and credit memos applied to a debit memo (GET /v1/debit-memos/{debitMemoId}/application-parts); Zuora documents no page parameter, so next_path names its nextPage link for the request verb." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_id": DEBIT_MEMO_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_debit_memo_application_parts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_debit_memo_application_parts", params, state)

    @platform_process(
        name="list_debit_memo_files",
        display_name="Zuora: List Debit Memo Files",
        description="List a debit memo's PDF files (GET /v1/debit-memos/{debitMemoKey}/files) one page per call, newest version first; next_page is the page to pass back." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_debit_memo_files(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_debit_memo_files", params, state)

    @platform_process(
        name="upload_debit_memo_file",
        display_name="Zuora: Upload Debit Memo File",
        description="Upload an externally generated PDF to a Draft or Posted debit memo (POST /v1/debit-memos/{debitMemoKey}/files, multipart/form-data) from input_path under registered path addresses." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "input_path": PDF_INPUT, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def upload_debit_memo_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("upload_debit_memo_file", params, state)

    @platform_process(
        name="list_debit_memo_items",
        display_name="Zuora: List Debit Memo Items",
        description="List a debit memo's items (GET /v1/debit-memos/{debitMemoKey}/items) one page per call, filtered by amount, SKU, service dates, source item or subscription and sorted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, **DEBIT_MEMO_ITEM_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_debit_memo_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_debit_memo_items", params, state)

    @platform_process(
        name="get_debit_memo_item",
        display_name="Zuora: Get Debit Memo Item",
        description="Read one debit memo item (GET /v1/debit-memos/{debitMemoKey}/items/{dmitemid}): amount, balance, amount applied to it, SKU, source item and service period." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "item_id": ITEM_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_debit_memo_item(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_debit_memo_item", params, state)

    @platform_process(
        name="list_debit_memo_item_taxation_items",
        display_name="Zuora: List Debit Memo Item Taxation Items",
        description="List the taxation items of one debit memo item (GET /v1/debit-memos/{debitMemoId}/items/{dmitemid}/taxation-items) one page per call." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_id": DEBIT_MEMO_ID, "item_id": ITEM_ID, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_debit_memo_item_taxation_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_debit_memo_item_taxation_items", params, state)

    @platform_process(
        name="create_debit_memo_taxation_items",
        display_name="Zuora: Create Debit Memo Taxation Items",
        description="Add externally calculated taxation items to a debit memo (POST /v1/debit-memos/{debitMemoKey}/taxation-items); each item needs jurisdiction, name, taxAmount, taxRate and taxRateType." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"debit_memo_key": DEBIT_MEMO_KEY, "body": body("taxationItems: jurisdiction, name, taxAmount, taxRate, taxRateType, sourceTaxItemId, memoItemId, taxCode, taxDate and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_debit_memo_taxation_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_debit_memo_taxation_items", params, state)
