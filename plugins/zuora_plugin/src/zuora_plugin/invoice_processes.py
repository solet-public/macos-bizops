"""``@platform_process`` declarations for the Zuora Billing Documents and Invoices families (mixed into ``ZuoraPlugin``).

Same contract as ``payment_processes``: EDGE, deferred completion, one vendor
operation from ``billing_document_actions`` per job, vendor RBAC the only
boundary. ``get_invoice`` (the Invoices section's single-invoice read) stays
declared in ``plugin.py`` with its original contract.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .billing_document_params import BILLING_DOCUMENT_FILTER_PARAMS, INVOICE_KEY, ITEM_ID, JOB_ID, PDF_INPUT, PDF_STATUS_HEADER_PARAMS, optional_body
from .commerce_params import ACCOUNT_KEY, EDGE, IDEMPOTENT_HEADER_PARAMS, NONE, PAGE_PARAMS, QUEUED_NOTE, READ, V1_HEADER_PARAMS, WRITE, body, text
from .workflow_processes import QUEUED


class InvoiceProcesses:
    """Typed Billing Documents and Invoices verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # -- Billing documents -----------------------------------------------

    @platform_process(
        name="list_billing_documents",
        display_name="Zuora: List Billing Documents",
        description="List invoices, credit memos and debit memos together (GET /v1/billing-documents) one page per call, filtered by account, document date or status and sorted; next_page is the page to pass back, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={**BILLING_DOCUMENT_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_billing_documents(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_billing_documents", params, state)

    @platform_process(
        name="generate_billing_documents",
        display_name="Zuora: Generate Billing Documents",
        description="Generate an account's invoices and credit memos now (POST /v1/accounts/{key}/billing-documents/generate), draft or posted, like a one-account bill run; body needs effectiveDate and targetDate, and may set autoPost, subscriptionIds and chargeTypeToExclude." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, "body": body("effectiveDate and targetDate (required, yyyy-mm-dd), autoPost, autoRenew, chargeTypeToExclude, commitmentKeys, creditMemoReasonCode, includeSubscriptions, includeOrderLineItems, subscriptionIds."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def generate_billing_documents(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("generate_billing_documents", params, state)

    @platform_process(
        name="create_billing_document_files_deletion_job",
        display_name="Zuora: Delete Billing Document Files",
        description="Start a job that deletes the PDF files of accounts' billing documents (POST /v1/accounts/billing-documents/files/deletion-jobs) for accountIds or accountKeys; read it with get_billing_document_files_deletion_job." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("accountIds or accountKeys: the accounts whose billing document files are deleted."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_billing_document_files_deletion_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_billing_document_files_deletion_job", params, state)

    @platform_process(
        name="get_billing_document_files_deletion_job",
        display_name="Zuora: Get Billing Document Files Deletion Job",
        description="Read a billing document files deletion job (GET /v1/accounts/billing-documents/files/deletion-jobs/{jobId}): Pending, Processing, Completed or Error." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"job_id": JOB_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_billing_document_files_deletion_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_billing_document_files_deletion_job", params, state)

    # -- Invoices ----------------------------------------------------------

    @platform_process(
        name="create_standalone_invoice",
        display_name="Zuora: Create Standalone Invoice",
        description="Create a standalone invoice for non-recurring goods or services (POST /v1/invoices); body needs invoiceDate and carries accountId or accountNumber, invoiceItems, status (Draft or Posted) and the rest." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("The invoice document: invoiceDate (required), accountId or accountNumber, invoiceItems with charge, amount and service dates, dueDate, status, currency, billToContact and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_standalone_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_standalone_invoice", params, state)

    @platform_process(
        name="create_standalone_invoices",
        display_name="Zuora: Create Standalone Invoices",
        description="Create up to 50 standalone invoices in one call (POST /v1/invoices/batch); body carries invoices, each shaped like create_standalone_invoice's document, and useSingleTransaction." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("invoices (each with invoiceDate, account, items) and useSingleTransaction."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_standalone_invoices(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_standalone_invoices", params, state)

    @platform_process(
        name="update_invoices",
        display_name="Zuora: Update Invoices",
        description="Correct or clear fields on up to 50 invoices in one call (PUT /v1/invoices): due date, comments, transferred-to-accounting, custom fields, items; body carries invoices, each with its id." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("invoices: each with id and the fields to change (dueDate, comments, invoiceDate, transferredToAccounting, custom fields, invoiceItems)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_invoices(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_invoices", params, state)

    @platform_process(
        name="post_invoices",
        display_name="Zuora: Post Invoices",
        description="Post up to 50 draft invoices in one call (POST /v1/invoices/bulk-post), optionally setting each invoiceDate; unpost_invoice reverses a post. Zuora reports each invoice's own success." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("invoices: each with id and optional invoiceDate."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def post_invoices(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("post_invoices", params, state)

    @platform_process(
        name="get_invoice_pdf_status",
        display_name="Zuora: Get Invoice PDF Status",
        description="Read the PDF generation status of up to 50 invoices (GET /v1/invoices/pdf-status): None, Pending, Processing, Generated, Error, Obsolete or Archived per file." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_keys": text("Comma-separated invoice ids or numbers, at most 50.", required=True), **PDF_STATUS_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_invoice_pdf_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_invoice_pdf_status", params, state)

    @platform_process(
        name="update_invoice",
        display_name="Zuora: Update Invoice",
        description="Correct or clear one invoice's fields (PUT /v1/invoices/{invoiceKey}): due date, comments, invoice date of a draft, transferred-to-accounting, custom fields and items; send only the fields to change." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": body("Fields to change: dueDate, comments, invoiceDate, transferredToAccounting, autoPay, templateId, invoiceItems, custom fields."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_invoice", params, state)

    @platform_process(
        name="delete_invoice",
        display_name="Zuora: Delete Invoice",
        description="Delete an invoice (DELETE /v1/invoices/{invoiceKey}); Zuora deletes one under 100 items at once and larger ones as a job (jobId, jobStatus). Cancel, reverse and write-off keep the invoice instead." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_invoice", params, state)

    @platform_process(
        name="cancel_invoice",
        display_name="Zuora: Cancel Invoice",
        description="Cancel an invoice (PUT /v1/invoices/{invoiceKey}/cancel); the invoice is kept in Canceled status. Zuora cancels one of 1,000 or more items as a job (jobStatus Pending until done)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_invoice", params, state)

    @platform_process(
        name="unpost_invoice",
        display_name="Zuora: Unpost Invoice",
        description="Return a posted invoice to Draft (PUT /v1/invoices/{invoiceKey}/unpost), the reverse of post_invoices; Zuora refuses it while payments or adjustments are applied." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unpost_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unpost_invoice", params, state)

    @platform_process(
        name="reverse_invoice",
        display_name="Zuora: Reverse Invoice",
        description="Reverse a posted invoice with nothing applied to it (PUT /v1/invoices/{invoiceKey}/reverse): Zuora applies a credit memo to it; body may set applyEffectiveDate, memoDate, reasonCode and comment." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": optional_body("applyEffectiveDate, memoDate, reasonCode, comment."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def reverse_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("reverse_invoice", params, state)

    @platform_process(
        name="split_invoice",
        display_name="Zuora: Split Invoice",
        description="Split a draft invoice into 2 to 20 invoices (PUT /v1/invoices/{invoiceKey}/split) by amounts or percentages that add up to the whole; body carries splitType and splits." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": body("splitType (Percentage or Amount) and splits, each with its share."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def split_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("split_invoice", params, state)

    @platform_process(
        name="write_off_invoice",
        display_name="Zuora: Write Off Invoice",
        description="Write off a posted invoice (PUT /v1/invoices/{invoiceKey}/write-off): Zuora creates a credit memo and applies it to the open balance; body may set memoDate, reasonCode, comment and items." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": optional_body("memoDate, reasonCode, comment, amount, items, revenueImpacting and custom fields of the write-off credit memo."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def write_off_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("write_off_invoice", params, state)

    @platform_process(
        name="write_off_invoice_async",
        display_name="Zuora: Write Off Invoice Async",
        description="Write off a large posted invoice as a Zuora job (POST /v1/invoices/{invoiceKey}/write-off-async); read the job with get_invoice_write_off_async_job. Same body as write_off_invoice." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": optional_body("memoDate, reasonCode, comment, amount, items, revenueImpacting and custom fields of the write-off credit memo."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def write_off_invoice_async(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("write_off_invoice_async", params, state)

    @platform_process(
        name="get_invoice_write_off_async_job",
        display_name="Zuora: Get Invoice Write-Off Job",
        description="Read an asynchronous invoice write-off job (GET /v1/invoices/write-off-async-jobs/{asyncJobId}): status, the invoice and the credit memo it created." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"async_job_id": text("Async job id from write_off_invoice_async.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_invoice_write_off_async_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_invoice_write_off_async_job", params, state)

    @platform_process(
        name="email_invoice",
        display_name="Zuora: Email Invoice",
        description="Email a posted invoice (POST /v1/invoices/{invoiceKey}/emails) to the bill-to contact or given addresses; body may set emailAddresses, includeAdditionalEmailAddresses and useEmailTemplateSetting." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": optional_body("emailAddresses, includeAdditionalEmailAddresses, pdfFileId, useEmailTemplateSetting."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def email_invoice(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("email_invoice", params, state)

    @platform_process(
        name="list_invoice_application_parts",
        display_name="Zuora: List Invoice Application Parts",
        description="List the payments and credit memos applied to an invoice (GET /v1/invoices/{invoiceKey}/application-parts); Zuora documents no page parameter, so next_path names its nextPage link for the request verb." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_invoice_application_parts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_invoice_application_parts", params, state)

    @platform_process(
        name="list_invoice_files",
        display_name="Zuora: List Invoice Files",
        description="List an invoice's PDF files (GET /v1/invoices/{invoiceKey}/files) one page per call, newest first, with each file's id and URL; next_page is the page to pass back." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_invoice_files(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_invoice_files", params, state)

    @platform_process(
        name="upload_invoice_file",
        display_name="Zuora: Upload Invoice File",
        description="Upload a PDF file to an invoice (POST /v1/invoices/{invoiceKey}/files, multipart/form-data) from input_path under registered path addresses; Zuora takes up to 4 MB and 50 files per invoice." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "input_path": PDF_INPUT, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def upload_invoice_file(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("upload_invoice_file", params, state)

    @platform_process(
        name="list_invoice_items",
        display_name="Zuora: List Invoice Items",
        description="List an invoice's items (GET /v1/invoices/{invoiceKey}/items) one page per call: charge, amounts, tax, service period and balance; next_page is the page to pass back." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_invoice_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_invoice_items", params, state)

    @platform_process(
        name="list_invoice_item_taxation_items",
        display_name="Zuora: List Invoice Item Taxation Items",
        description="List the taxation items of one invoice item (GET /v1/invoices/{invoiceKey}/items/{itemId}/taxation-items) one page per call: jurisdiction, rate, amount, exemptions." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "item_id": ITEM_ID, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_invoice_item_taxation_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_invoice_item_taxation_items", params, state)

    @platform_process(
        name="create_invoice_taxation_items",
        display_name="Zuora: Create Invoice Taxation Items",
        description="Add externally calculated taxation items to a draft invoice (POST /v1/invoices/{invoiceKey}/taxation-items); each item needs invoiceItemId, jurisdiction, name, taxAmount, taxDate, taxRate and taxRateType." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_key": INVOICE_KEY, "body": body("taxationItems: invoiceItemId, jurisdiction, name, taxAmount, taxDate, taxRate, taxRateType, taxCode, exemptAmount and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_invoice_taxation_items(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_invoice_taxation_items", params, state)
