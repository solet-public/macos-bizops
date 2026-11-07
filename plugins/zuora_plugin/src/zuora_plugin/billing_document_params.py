"""Parameter metadata for the build 5 ``@platform_process`` declarations: Billing Documents, Invoices, Credit and Debit Memos, Bill Runs and Usage.

Mirrors :mod:`billing_document_actions` and :mod:`bill_run_usage_actions`:
the same caller names, documented once so the process modules and their
knowledge-base documents describe them alike.
"""

from __future__ import annotations

from typing import Final

from ananta.core.actions.action_metadata import ParameterMetadata

from .commerce_params import PAGE_PARAMS, PREVIEW_EXISTING_HEADER_PARAMS, body, flag, number, text

# The pdf-status routes document Zuora-Track-Id, Zuora-Entity-Ids and Zuora-Version (no Zuora-Org-Ids).
PDF_STATUS_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = dict(PREVIEW_EXISTING_HEADER_PARAMS)

INVOICE_KEY: Final[ParameterMetadata] = text("Invoice id or invoice number, e.g. INV-00000001.", required=True)
CREDIT_MEMO_KEY: Final[ParameterMetadata] = text("Credit memo id or number, e.g. CM00000001.", required=True)
CREDIT_MEMO_ID: Final[ParameterMetadata] = text("Credit memo id (this route documents the id).", required=True)
DEBIT_MEMO_KEY: Final[ParameterMetadata] = text("Debit memo id or number, e.g. DM00000001.", required=True)
DEBIT_MEMO_ID: Final[ParameterMetadata] = text("Debit memo id (this route documents the id).", required=True)
ITEM_ID: Final[ParameterMetadata] = text("Document item id.", required=True)
PART_ID: Final[ParameterMetadata] = text("Credit memo part id.", required=True)
JOB_ID: Final[ParameterMetadata] = text("Job id Zuora returned when the job was created.", required=True)
BILL_RUN_ID: Final[ParameterMetadata] = text("Bill run id.", required=True)
USAGE_ID: Final[ParameterMetadata] = text("Usage record id.", required=True)
PDF_INPUT: Final[ParameterMetadata] = text("ABSOLUTE path of the .pdf file to upload, under an export_allowed_roots entry; Zuora takes up to 4 MB.", required=True)
USAGE_INPUT: Final[ParameterMetadata] = text("ABSOLUTE path of the usage file (.csv, .xls, .xlsx or .zip in Zuora's usage template) under an export_allowed_roots entry.", required=True)
WSDL_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "x_zuora_wsdl_version": text("X-Zuora-WSDL-Version header: the WSDL version whose usage fields are used; 146 or higher selects the product rate plan charge fields."),
}
REJECT_UNKNOWN: Final[dict[str, ParameterMetadata]] = {"reject_unknown_fields": flag("Make Zuora answer 400 when the body carries a field it does not know.")}
OPTIONAL_BODY_NOTE: Final[str] = "Optional; omit it to send an empty JSON document."


def optional_body(description: str) -> ParameterMetadata:
    """A vendor body whose documented fields are all optional."""
    return body(f"{description} {OPTIONAL_BODY_NOTE}", required=False)


_DOCUMENT_FILTERS: Final[dict[str, ParameterMetadata]] = {
    **PAGE_PARAMS,
    "account_id": text("Only documents of this customer account id."),
    "account_number": text("Only documents of this customer account number."),
    "status": text("Only documents in this status, e.g. Draft, Posted or Canceled."),
    "sort": text("Sort field with - (ascending) or + (descending), e.g. -documentDate or +number."),
}
BILLING_DOCUMENT_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    **_DOCUMENT_FILTERS,
    "document_date": text("Only documents dated on this date (invoice, credit memo or debit memo date)."),
}
_MEMO_FILTERS: Final[dict[str, ParameterMetadata]] = {
    **_DOCUMENT_FILTERS,
    "amount": number("Only memos of this total amount (a number)."),
    "created_by_id": text("Only memos created by this user id."),
    "created_date": text("Only memos created at this date-time."),
    "currency": text("Only memos in this currency."),
    "number": text("Only the memo with this number."),
    "referred_invoice_id": text("Only memos that refer to this invoice id."),
    "target_date": text("Only memos with this target date."),
    "tax_amount": number("Only memos with this tax amount (a number)."),
    "total_tax_exempt_amount": number("Only memos with this tax-exempt amount (a number)."),
    "updated_by_id": text("Only memos last updated by this user id."),
    "updated_date": text("Only memos last updated at this date-time."),
}
CREDIT_MEMO_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    **_MEMO_FILTERS,
    "applied_amount": number("Only credit memos with this applied amount (a number)."),
    "auto_apply_upon_posting": flag("Only credit memos that are (or are not) applied automatically when posted."),
    "credit_memo_date": text("Only credit memos dated on this date."),
    "exclude_from_auto_apply_rules": flag("Only credit memos that are (or are not) excluded from auto-apply rules."),
    "refund_amount": number("Only credit memos with this refunded amount (a number)."),
    "source_id": text("Only credit memos from this source id (an invoice, product rate plan charge or other source)."),
    "transferred_to_accounting": text("Only credit memos with this accounting transfer state: Processing, Yes, Error or Ignore."),
    "unapplied_amount": number("Only credit memos with this unapplied amount (a number)."),
}
DEBIT_MEMO_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    **_MEMO_FILTERS,
    "balance": number("Only debit memos with this open balance (a number)."),
    "be_applied_amount": number("Only debit memos with this amount applied to them (a number)."),
    "debit_memo_date": text("Only debit memos dated on this date."),
    "due_date": text("Only debit memos due on this date."),
}
_ITEM_FILTERS: Final[dict[str, ParameterMetadata]] = {
    **PAGE_PARAMS,
    "amount": number("Only items of this amount (a number)."),
    "created_by_id": text("Only items created by this user id."),
    "created_date": text("Only items created at this date-time."),
    "id": text("Only the item with this id."),
    "service_end_date": text("Only items whose service ends on this date."),
    "service_start_date": text("Only items whose service starts on this date."),
    "sku": text("Only items with this SKU."),
    "sku_name": text("Only items with this SKU name."),
    "source_item_id": text("Only items created from this source item id."),
    "subscription_id": text("Only items of this subscription id."),
    "updated_by_id": text("Only items last updated by this user id."),
    "updated_date": text("Only items last updated at this date-time."),
    "sort": text("Sort field with - (ascending) or + (descending), e.g. -amount."),
}
CREDIT_MEMO_ITEM_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    **_ITEM_FILTERS,
    "applied_amount": number("Only items with this applied amount (a number)."),
    "refund_amount": number("Only items with this refunded amount (a number)."),
}
DEBIT_MEMO_ITEM_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    **_ITEM_FILTERS,
    "be_applied_amount": number("Only items with this amount applied to them (a number)."),
}
