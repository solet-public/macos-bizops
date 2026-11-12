"""``@platform_process`` declarations for the Zuora Credit Memos family (mixed into ``ZuoraPlugin``).

Same contract as ``payment_processes``: EDGE, deferred completion, one vendor
operation from ``billing_document_actions`` per job, vendor RBAC the only
boundary.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .billing_document_params import CREDIT_MEMO_FILTER_PARAMS, CREDIT_MEMO_ID, CREDIT_MEMO_ITEM_FILTER_PARAMS, CREDIT_MEMO_KEY, INVOICE_KEY, ITEM_ID, PART_ID, PDF_INPUT, PDF_STATUS_HEADER_PARAMS, optional_body
from .commerce_params import EDGE, IDEMPOTENT_HEADER_PARAMS, NONE, PAGE_PARAMS, QUEUED_NOTE, READ, V1_HEADER_PARAMS, WRITE, body, text
from .workflow_processes import QUEUED


class CreditMemoProcesses:
    """Typed Credit Memo verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="list_credit_memos",
        display_name="Zuora: List Credit Memos",
        description="List credit memos (GET /v1/credit-memos) one page per call, filtered by account, amounts, dates, number, status, source invoice or accounting state and sorted; next_page is the page to pass back, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={**CREDIT_MEMO_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_credit_memos(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_credit_memos", params, state)

    @platform_process(
        name="create_credit_memo",
        display_name="Zuora: Create Credit Memo",
        description="Create an ad-hoc credit memo from product rate plan charges (POST /v1/credit-memos): accountId or accountNumber, charges with amounts, effectiveDate, reasonCode, autoPost." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("accountId or accountNumber, charges (productRatePlanChargeId, amount, ...), currency, customRates, effectiveDate, reasonCode, comment, autoPost, excludeFromAutoApplyRules, number."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_credit_memo", params, state)

    @platform_process(
        name="create_credit_memo_from_invoice",
        display_name="Zuora: Create Credit Memo From Invoice",
        description="Create an ad-hoc credit memo from an invoice (POST /v1/credit-memos/invoice/{invoiceKey}); body needs invoiceId and carries items, reasonCode, effectiveDate, autoPost and autoApplyToInvoiceUponPosting." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": body("invoiceId (required), items (invoiceItemId, amount, skuName), reasonCode, comment, effectiveDate, autoPost, autoApplyToInvoiceUponPosting, taxAutoCalculation."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_credit_memo_from_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_credit_memo_from_invoice", params, state)

    @platform_process(
        name="create_credit_memos",
        display_name="Zuora: Create Credit Memos",
        description="Create up to 50 credit memos in one call (POST /v1/credit-memos/bulk) from invoices (sourceType Invoice) or product rate plan charges (sourceType Standalone); body needs sourceType and memos." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("sourceType (required: Invoice or Standalone) and memos, each shaped like the single create."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_credit_memos(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_credit_memos", params, state)

    @platform_process(
        name="update_credit_memos",
        display_name="Zuora: Update Credit Memos",
        description="Correct or clear the basic and finance fields of up to 50 credit memos in one call (PUT /v1/credit-memos/bulk); body carries memos, each with its id." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("memos: each with id and the fields to change (comment, effectiveDate, reasonCode, items, transferredToAccounting, auto-apply flags)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_credit_memos(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_credit_memos", params, state)

    @platform_process(
        name="get_credit_memo_pdf_status",
        display_name="Zuora: Get Credit Memo PDF Status",
        description="Read the PDF generation status of up to 50 credit memos (GET /v1/credit-memos/pdf-status)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_keys": text("Comma-separated credit memo ids or numbers, at most 50.", required=True), **PDF_STATUS_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_credit_memo_pdf_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_credit_memo_pdf_status", params, state)

    @platform_process(
        name="get_credit_memo",
        display_name="Zuora: Get Credit Memo",
        description="Read one credit memo (GET /v1/credit-memos/{creditMemoKey}): status, amounts, applied, unapplied and refunded totals, source, reason and dates." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_credit_memo", params, state)

    @platform_process(
        name="update_credit_memo",
        display_name="Zuora: Update Credit Memo",
        description="Correct or clear a credit memo's basic and finance fields (PUT /v1/credit-memos/{creditMemoKey}): comment, effectiveDate, reasonCode, items, transferredToAccounting, auto-apply flags; send only the fields to change." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("Fields to change: autoApplyUponPosting, comment, effectiveDate, excludeFromAutoApplyRules, items, reasonCode, transferredToAccounting and custom fields."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_credit_memo", params, state)

    @platform_process(
        name="delete_credit_memo",
        display_name="Zuora: Delete Credit Memo",
        description="Delete a credit memo (DELETE /v1/credit-memos/{creditMemoKey}); Zuora deletes only a Cancelled credit memo, so cancel it first." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_credit_memo", params, state)

    @platform_process(
        name="post_credit_memo",
        display_name="Zuora: Post Credit Memo",
        description="Post a draft credit memo to activate it (PUT /v1/credit-memos/{creditMemoKey}/post); unpost_credit_memo returns it to Draft." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def post_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("post_credit_memo", params, state)

    @platform_process(
        name="unpost_credit_memo",
        display_name="Zuora: Unpost Credit Memo",
        description="Return a posted credit memo to Draft (PUT /v1/credit-memos/{creditMemoKey}/unpost); Zuora refuses it once the memo is applied or refunded." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unpost_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unpost_credit_memo", params, state)

    @platform_process(
        name="cancel_credit_memo",
        display_name="Zuora: Cancel Credit Memo",
        description="Cancel a draft credit memo (PUT /v1/credit-memos/{creditMemoKey}/cancel); Zuora cancels only Draft memos, and a Cancelled memo can then be deleted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_credit_memo", params, state)

    @platform_process(
        name="apply_credit_memo",
        display_name="Zuora: Apply Credit Memo",
        description="Apply a posted credit memo to invoices and debit memos (PUT /v1/credit-memos/{creditMemoKey}/apply), at document or item level; unapply_credit_memo reverses it." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("invoices and/or debitMemos (id, amount, items), effectiveDate, applicationRule."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def apply_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("apply_credit_memo", params, state)

    @platform_process(
        name="unapply_credit_memo",
        display_name="Zuora: Unapply Credit Memo",
        description="Unapply a credit memo from invoices and debit memos (PUT /v1/credit-memos/{creditMemoKey}/unapply): the applied amount returns to the memo's unapplied amount; the reverse of apply_credit_memo." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("invoices and/or debitMemos (id, amount, items) and effectiveDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unapply_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unapply_credit_memo", params, state)

    @platform_process(
        name="apply_credit_memo_async",
        display_name="Zuora: Apply Credit Memo Async",
        description="Apply a posted credit memo to invoices as a Zuora job (PUT /v1/credit-memos/{creditMemoKey}/apply-async) for large memos; read it with get_credit_memo_apply_async_job." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("invoices (invoiceId, amount, items) and effectiveDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def apply_credit_memo_async(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("apply_credit_memo_async", params, state)

    @platform_process(
        name="unapply_credit_memo_async",
        display_name="Zuora: Unapply Credit Memo Async",
        description="Unapply a credit memo from invoices as a Zuora job (PUT /v1/credit-memos/{creditMemoKey}/unapply-async); read it with get_credit_memo_unapply_async_job." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("invoices (invoiceId, amount, items) and effectiveDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unapply_credit_memo_async(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unapply_credit_memo_async", params, state)

    @platform_process(
        name="get_credit_memo_apply_async_job",
        display_name="Zuora: Get Credit Memo Apply Job",
        description="Read an asynchronous credit memo apply job (GET /v1/credit-memos/apply-async-jobs/{applyAsyncJobId}): its status and errors." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"apply_async_job_id": text("Job id from apply_credit_memo_async.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_credit_memo_apply_async_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_credit_memo_apply_async_job", params, state)

    @platform_process(
        name="get_credit_memo_unapply_async_job",
        display_name="Zuora: Get Credit Memo Unapply Job",
        description="Read an asynchronous credit memo unapply job (GET /v1/credit-memos/unapply-async-jobs/{unapplyAsyncJobId}): its status and errors." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"unapply_async_job_id": text("Job id from unapply_credit_memo_async.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_credit_memo_unapply_async_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_credit_memo_unapply_async_job", params, state)

    @platform_process(
        name="refund_credit_memo",
        display_name="Zuora: Refund Credit Memo",
        description="Refund the unapplied part of a posted credit memo to the customer (POST /v1/credit-memos/{creditMemoKey}/refund), electronically or externally; body needs totalAmount and type." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("totalAmount and type (required: Electronic or External), methodType, paymentMethodId, gatewayId, refundDate, items, reasonCode, referenceId, comment and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def refund_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("refund_credit_memo", params, state)

    @platform_process(
        name="reverse_credit_memo",
        display_name="Zuora: Reverse Credit Memo",
        description="Reverse a posted credit memo (PUT /v1/credit-memos/{creditMemoKey}/reverse); body may set applyEffectiveDate, memoDate, reasonCode and comment of the reversal." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": optional_body("applyEffectiveDate, memoDate, reasonCode, comment."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def reverse_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("reverse_credit_memo", params, state)

    @platform_process(
        name="write_off_credit_memo",
        display_name="Zuora: Write Off Credit Memo",
        description="Write off a fully unapplied credit memo (PUT /v1/credit-memos/{creditMemoId}/write-off): Zuora creates a debit memo and applies the credit memo to it in full; body may set memoDate, reasonCode and comment." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_id": CREDIT_MEMO_ID, "body": optional_body("memoDate, reasonCode, comment."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def write_off_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("write_off_credit_memo", params, state)

    @platform_process(
        name="email_credit_memo",
        display_name="Zuora: Email Credit Memo",
        description="Email a posted credit memo (POST /v1/credit-memos/{creditMemoKey}/emails) through the Manually email Credit Memo notification; body may set emailAddresses and includeAdditionalEmailAddresses." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": optional_body("emailAddresses, includeAdditionalEmailAddresses, pdfFileId, useEmailTemplateSetting."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def email_credit_memo(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("email_credit_memo", params, state)

    @platform_process(
        name="generate_credit_memo_pdf",
        display_name="Zuora: Generate Credit Memo PDF",
        description="Create a PDF file for a credit memo (POST /v1/credit-memos/{creditMemoKey}/pdfs); needs the billing document file generation feature and the Regenerate PDF permission." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def generate_credit_memo_pdf(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("generate_credit_memo_pdf", params, state)

    @platform_process(
        name="list_credit_memo_files",
        display_name="Zuora: List Credit Memo Files",
        description="List a credit memo's PDF files (GET /v1/credit-memos/{creditMemoKey}/files) one page per call, newest version first; next_page is the page to pass back." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_credit_memo_files(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_credit_memo_files", params, state)

    @platform_process(
        name="upload_credit_memo_file",
        display_name="Zuora: Upload Credit Memo File",
        description="Upload an externally generated PDF to a Draft or Posted credit memo (POST /v1/credit-memos/{creditMemoKey}/files, multipart/form-data) from input_path under registered path addresses." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "input_path": PDF_INPUT, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def upload_credit_memo_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("upload_credit_memo_file", params, state)

    @platform_process(
        name="list_credit_memo_items",
        display_name="Zuora: List Credit Memo Items",
        description="List a credit memo's items (GET /v1/credit-memos/{creditMemoKey}/items) one page per call, filtered by amount, SKU, service dates, source item or subscription and sorted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **CREDIT_MEMO_ITEM_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_credit_memo_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_credit_memo_items", params, state)

    @platform_process(
        name="get_credit_memo_item",
        display_name="Zuora: Get Credit Memo Item",
        description="Read one credit memo item (GET /v1/credit-memos/{creditMemoKey}/items/{cmitemid}): amounts, applied and refunded totals, SKU, source item and service period." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "item_id": ITEM_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_credit_memo_item(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_credit_memo_item", params, state)

    @platform_process(
        name="list_credit_memo_item_taxation_items",
        display_name="Zuora: List Credit Memo Item Taxation Items",
        description="List the taxation items of one credit memo item (GET /v1/credit-memos/{creditMemoId}/items/{cmitemid}/taxation-items) one page per call." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_id": CREDIT_MEMO_ID, "item_id": ITEM_ID, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_credit_memo_item_taxation_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_credit_memo_item_taxation_items", params, state)

    @platform_process(
        name="create_credit_memo_taxation_items",
        display_name="Zuora: Create Credit Memo Taxation Items",
        description="Add externally calculated taxation items to a credit memo (POST /v1/credit-memos/{creditMemoKey}/taxation-items); each item needs jurisdiction, name, taxAmount, taxRate and taxRateType." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "body": body("taxationItems: jurisdiction, name, taxAmount, taxRate, taxRateType, sourceTaxItemId, memoItemId, taxCode, taxDate and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_credit_memo_taxation_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_credit_memo_taxation_items", params, state)

    @platform_process(
        name="list_credit_memo_parts",
        display_name="Zuora: List Credit Memo Parts",
        description="List a credit memo's parts (GET /v1/credit-memos/{creditMemoKey}/parts) one page per call: the unapplied part and the parts applied to invoices and debit memos." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_credit_memo_parts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_credit_memo_parts", params, state)

    @platform_process(
        name="get_credit_memo_part",
        display_name="Zuora: Get Credit Memo Part",
        description="Read one credit memo part (GET /v1/credit-memos/{creditMemoKey}/parts/{partid}): the invoice or debit memo it is applied to and the amount." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"credit_memo_key": CREDIT_MEMO_KEY, "part_id": PART_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_credit_memo_part(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_credit_memo_part", params, state)
