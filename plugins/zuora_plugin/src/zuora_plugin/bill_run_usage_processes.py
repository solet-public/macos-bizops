"""``@platform_process`` declarations for the Zuora Bill Run, Billing Preview Run and Usage families (mixed into ``ZuoraPlugin``).

Same contract as ``payment_processes``: EDGE, deferred completion, one vendor
operation from ``bill_run_usage_actions`` per job, vendor RBAC the only
boundary.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import platform_process

from .billing_document_params import BILL_RUN_ID, REJECT_UNKNOWN, USAGE_ID, USAGE_INPUT, WSDL_HEADER_PARAMS, optional_body
from .commerce_params import EDGE, IDEMPOTENT_HEADER_PARAMS, NONE, QUEUED_NOTE, READ, V1_HEADER_PARAMS, WRITE, body, text
from .workflow_processes import QUEUED


class BillRunUsageProcesses:
    """Typed Bill Run and Usage verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # -- Bill runs ---------------------------------------------------------

    @platform_process(
        name="create_bill_run",
        display_name="Zuora: Create Bill Run",
        description="Create an ad-hoc or scheduled bill run (POST /v1/bill-runs) by batch, bill cycle day or account filters, with invoice and target dates or offsets, autoPost and autoEmail; read it with get_bill_run." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("billRunType, batches, billCycleDay, billRunFilters, invoiceDate, targetDate (or their month offsets and days of month), schedule, autoPost, autoEmail, autoRenewal, chargeTypeToExclude, name and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_bill_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_bill_run", params, state)

    @platform_process(
        name="get_bill_run",
        display_name="Zuora: Get Bill Run",
        description="Read one bill run (GET /v1/bill-runs/{billRunId}): number, status, filters, dates, schedule and settings; poll it after create_bill_run or post_bill_run." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"bill_run_id": BILL_RUN_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_bill_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_bill_run", params, state)

    @platform_process(
        name="post_bill_run",
        display_name="Zuora: Post Bill Run",
        description="Post a Completed bill run's invoices (PUT /v1/bill-runs/{billRunId}/post); Zuora works asynchronously, PostInProgress then Posted. Body needs invoiceDate, outside a closed accounting period." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"bill_run_id": BILL_RUN_ID, "body": body("invoiceDate (required, yyyy-mm-dd)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def post_bill_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("post_bill_run", params, state)

    @platform_process(
        name="cancel_bill_run",
        display_name="Zuora: Cancel Bill Run",
        description="Cancel a bill run in Draft status (PUT /v1/bill-runs/{billRunId}/cancel); for a scheduled run, cancelOnce true cancels this run only and false cancels every future run." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"bill_run_id": BILL_RUN_ID, "body": optional_body("cancelOnce (scheduled bill runs only)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_bill_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_bill_run", params, state)

    @platform_process(
        name="delete_bill_run",
        display_name="Zuora: Delete Bill Run",
        description="Delete a bill run (DELETE /v1/bill-runs/{billRunId}); Zuora deletes only bill runs in Canceled or Error status, so cancel it first." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"bill_run_id": BILL_RUN_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_bill_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_bill_run", params, state)

    @platform_process(
        name="email_bill_run_documents",
        display_name="Zuora: Email Bill Run Documents",
        description="Email the invoices and credit memos a bill run generated (POST /v1/bill-runs/{billRunKey}/emails) through their notification templates; resend false skips documents already emailed. An empty body is sent as {}." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"bill_run_key": text("Bill run id or number.", required=True), "body": optional_body("resend."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def email_bill_run_documents(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("email_bill_run_documents", params, state)

    @platform_process(
        name="create_billing_preview_run",
        display_name="Zuora: Create Billing Preview Run",
        description="Start a billing preview run (POST /v1/billing-preview-runs) that forecasts future invoice and credit memo items for batches of accounts up to targetDate, without billing them; read its status and result file with get_billing_preview_run." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("targetDate (required, yyyy-mm-dd), batches, filters, assumeRenewal, chargeTypeToExclude, includingDraftItems, includingEvergreenSubscription, storageOption, storeDifference, comparedBillingPreviewRunId, organizationLabels."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_billing_preview_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_billing_preview_run", params, state)

    @platform_process(
        name="get_billing_preview_run",
        display_name="Zuora: Get Billing Preview Run",
        description="Read a billing preview run (GET /v1/billing-preview-runs/{billingPreviewRunId}): status, run number, target date, account totals and the resultFileUrl of the CSV preview once it completes." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"billing_preview_run_id": text("Billing preview run id from create_billing_preview_run.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_billing_preview_run(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_billing_preview_run", params, state)

    # -- Usage -------------------------------------------------------------

    @platform_process(
        name="import_usage",
        display_name="Zuora: Import Usage",
        description="Import a usage file (POST /v1/usage, multipart/form-data) from input_path under export_allowed_roots; Zuora answers with a checkImportStatus URL, only Completed means imported, and a failing record rolls the whole file back. The PRODUCT_RATE_PLAN_CHARGE_ID column needs x_zuora_wsdl_version 146 or higher."
        + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"input_path": USAGE_INPUT, **IDEMPOTENT_HEADER_PARAMS, **WSDL_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def import_usage(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("import_usage", params, state)

    @platform_process(
        name="get_usage_rate_detail",
        display_name="Zuora: Get Usage Rate Detail",
        description="Read how a usage-based invoice item's amount was rated (GET /v1/invoices/invoice-item/{invoice-item-id}/usage-rate-detail), as on the PDF invoice; not for overage or multi-attribute charge models." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"invoice_item_id": text("Invoice item id.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_usage_rate_detail(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_usage_rate_detail", params, state)

    @platform_process(
        name="create_usage",
        display_name="Zuora: Create Usage Record",
        description="Create one usage record (POST /v1/object/usage); body needs Quantity, StartDateTime and UOM, plus AccountId or AccountNumber and the charge or subscription it rates against." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "body": body("Quantity, StartDateTime and UOM (required), AccountId or AccountNumber, ChargeId, ChargeNumber or ProductRatePlanChargeNumber, SubscriptionId or SubscriptionNumber, EndDateTime, Description, UniqueKey."),
            **REJECT_UNKNOWN,
            **IDEMPOTENT_HEADER_PARAMS,
            **WSDL_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_usage(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_usage", params, state)

    @platform_process(
        name="get_usage",
        display_name="Zuora: Get Usage Record",
        description="Read one usage record (GET /v1/object/usage/{id}): account, charge, quantity, unit, dates and rating status (RbeStatus); fields limits the fields returned, and x_zuora_wsdl_version 146 or higher adds ProductRatePlanChargeNumber." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"usage_id": USAGE_ID, "fields": text("Comma-separated usage fields to return."), **V1_HEADER_PARAMS, **WSDL_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_usage(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_usage", params, state)

    @platform_process(
        name="update_usage",
        display_name="Zuora: Update Usage Record",
        description="Correct a usage record (PUT /v1/object/usage/{id}): Quantity, StartDateTime, EndDateTime, UOM or RbeStatus; Zuora refuses records of a dynamic pricing usage charge." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"usage_id": USAGE_ID, "body": body("Fields to change: Quantity, StartDateTime, EndDateTime, UOM, RbeStatus."), **REJECT_UNKNOWN, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_usage(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_usage", params, state)

    @platform_process(
        name="delete_usage",
        display_name="Zuora: Delete Usage Record",
        description="Delete a usage record (DELETE /v1/object/usage/{id}); Zuora refuses records of a dynamic pricing usage charge." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"usage_id": USAGE_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_usage(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_usage", params, state)
