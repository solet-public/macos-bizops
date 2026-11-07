"""``@platform_process`` declarations for the Zuora Orders family (mixed into ``ZuoraPlugin``).

Every verb is EDGE, dispatches in milliseconds through the plugin's D0.3
deferred-completion worker, and runs exactly one vendor operation from
``order_actions.ORDER_OPERATIONS``. The vendor role on the registered OAuth
client decides what succeeds; a 403 completes the job with
``zuora.permission_denied`` and Zuora's reason, and an HTTP 200 carrying
``success: false`` completes it with ``zuora.validation_failed`` and Zuora's
reasons.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, platform_process

from .commerce_params import (
    ACCOUNT_NUMBER,
    ASYNC_JOB_HEADER_PARAMS,
    EDGE,
    IDEMPOTENT_HEADER_PARAMS,
    NONE,
    ORDER_FILTER_PARAMS,
    ORDER_NUMBER,
    ORDER_STATUS,
    PAGE_PARAMS,
    QUEUED_NOTE,
    READ,
    SUBSCRIPTION_KEY,
    SUBSCRIPTION_NUMBER,
    V1_HEADER_PARAMS,
    WRITE,
    body,
)
from .workflow_processes import QUEUED

_ORDER_BODY = body("The order document: orderDate (required), existingAccountNumber or newAccount, subscriptions with orderActions, orderLineItems, processingOptions, status, customFields and every other documented field.")
_PREVIEW_BODY = body("The order document as for create_order plus previewOptions (required): previewThruType, previewTypes and related fields.")
_RETURN_IDS = ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Also return the ids of the created records.")
_SUMMARIZE = ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Summarize the previewed invoice items.")


class OrderProcesses:
    """Typed Orders verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="preview_order",
        display_name="Zuora: Preview Order",
        description="Preview an order synchronously (POST /v1/orders/preview): the invoices, credit memos and metrics it would produce, with nothing saved. body needs orderDate and previewOptions; optional summarize_invoice_item." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": _PREVIEW_BODY, "summarize_invoice_item": _SUMMARIZE, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def preview_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("preview_order", params, state)

    @platform_process(
        name="preview_order_async",
        display_name="Zuora: Preview Order Asynchronously",
        description="Start an asynchronous order preview (POST /v1/async/orders/preview) for large orders; completion carries Zuora's jobId, which get_async_order_job reads. Same body as preview_order." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": _PREVIEW_BODY, "summarize_invoice_item": _SUMMARIZE, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def preview_order_async(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("preview_order_async", params, state)

    @platform_process(
        name="list_orders",
        display_name="Zuora: List Orders",
        description="List the tenant's orders (GET /v1/orders), one page per call, filtered by order or updated date range and status. Completion carries orders, Zuora's nextPage and next_page (the page number to pass back, null at the end)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={**PAGE_PARAMS, **ORDER_FILTER_PARAMS, "status": ORDER_STATUS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_orders(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_orders", params, state)

    @platform_process(
        name="create_order",
        display_name="Zuora: Create Order",
        description="Create an order synchronously (POST /v1/orders): create, amend, renew, cancel or transfer subscriptions and add order line items in one call, as Draft, Pending, Completed or Scheduled. body needs orderDate; optional return_ids and idempotency_key." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": _ORDER_BODY, "return_ids": _RETURN_IDS, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_order", params, state)

    @platform_process(
        name="create_order_async",
        display_name="Zuora: Create Order Asynchronously",
        description="Create a large order asynchronously (POST /v1/async/orders); completion carries Zuora's jobId, which get_async_order_job reads until the order is created. Same body as create_order." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": _ORDER_BODY, "return_ids": _RETURN_IDS, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_order_async(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_order_async", params, state)

    @platform_process(
        name="delete_order",
        display_name="Zuora: Delete Order",
        description="Delete an order (DELETE /v1/orders/{orderNumber}): the subscriptions it changed roll back to their previous version and its order line items are deleted. Zuora refuses when the affected charges are invoiced." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_order", params, state)

    @platform_process(
        name="get_order",
        display_name="Zuora: Get Order",
        description="Read one order with its subscriptions, order actions and line items (GET /v1/orders/{orderNumber}); get_account_details adds the owner account details." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "order_number": ORDER_NUMBER,
            "get_account_details": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Include subscription and invoice owner account details."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_order", params, state)

    @platform_process(
        name="update_order",
        display_name="Zuora: Update Order",
        description="Correct a draft or scheduled order (PUT /v1/orders/{orderNumber}). Zuora replaces the whole order: send every order action to keep, since omitted ones are removed. body needs orderDate." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, "body": _ORDER_BODY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_order", params, state)

    @platform_process(
        name="delete_order_async",
        display_name="Zuora: Delete Order Asynchronously",
        description="Delete a large order asynchronously (DELETE /v1/async/orders/{orderNumber}); completion carries Zuora's jobId and status. Same rollback and invoiced-charge rule as delete_order." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_order_async(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_order_async", params, state)

    @platform_process(
        name="get_async_order_job",
        display_name="Zuora: Get Async Order Job",
        description="Read the status and result of an asynchronous order job (GET /v1/async-jobs/{jobId}) started by create_order_async, preview_order_async or delete_order_async." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"job_id": ParameterMetadata(type=ParameterType.STRING, required=True, description="The jobId an async order verb returned."), **ASYNC_JOB_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_async_order_job(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_async_order_job", params, state)

    @platform_process(
        name="list_orders_by_subscription_owner",
        display_name="Zuora: List Orders by Subscription Owner",
        description="List the orders whose subscriptions an account owns (GET /v1/orders/subscriptionOwner/{accountNumber}), one page per call with a date-range filter; completion carries next_page." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_number": ACCOUNT_NUMBER, **PAGE_PARAMS, **ORDER_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_orders_by_subscription_owner(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_orders_by_subscription_owner", params, state)

    @platform_process(
        name="list_orders_by_subscription",
        display_name="Zuora: List Orders by Subscription",
        description="List the orders that changed a subscription (GET /v1/orders/subscription/{subscriptionNumber}), one page per call, filtered by date range and status; completion carries next_page." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_number": SUBSCRIPTION_NUMBER, **PAGE_PARAMS, **ORDER_FILTER_PARAMS, "status": ORDER_STATUS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_orders_by_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_orders_by_subscription", params, state)

    @platform_process(
        name="list_orders_by_invoice_owner",
        display_name="Zuora: List Orders by Invoice Owner",
        description="List the orders an account is invoiced for (GET /v1/orders/invoiceOwner/{accountNumber}), one page per call, filtered by date range and status; completion carries next_page." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_number": ACCOUNT_NUMBER, **PAGE_PARAMS, **ORDER_FILTER_PARAMS, "status": ORDER_STATUS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_orders_by_invoice_owner(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_orders_by_invoice_owner", params, state)

    @platform_process(
        name="update_order_custom_fields",
        display_name="Zuora: Update Order Custom Fields",
        description="Set, correct or clear (null) an order's custom fields (PUT /v1/orders/{orderNumber}/customFields), including those on its order actions; body is Zuora's customFields document." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, "body": body("customFields and subscriptions[].orderActions[].customFields to set; null clears a field."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_order_custom_fields(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_order_custom_fields", params, state)

    @platform_process(
        name="update_subscription_custom_fields",
        display_name="Zuora: Update Subscription Custom Fields",
        description="Set, correct or clear (null) the custom fields of a subscription and of its rate plans and charges (PUT /v1/subscriptions/{subscriptionNumber}/customFields)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_number": SUBSCRIPTION_NUMBER, "body": body("customFields and ratePlans[] (with charges[]) customFields to set; null clears a field."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_subscription_custom_fields(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_subscription_custom_fields", params, state)

    @platform_process(
        name="update_order_trigger_dates",
        display_name="Zuora: Update Order Trigger Dates",
        description="Set the trigger dates of a pending order's actions (PUT /v1/orders/{orderNumber}/triggerDates) for CreateSubscription, AddProduct, UpdateProduct, RemoveProduct, RenewSubscription or TermsAndConditions, and charge-level specific trigger dates." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, "body": body("subscriptions[] (subscriptionNumber) with orderActions[] (sequence, triggerDates, charges[].specificTriggerDate)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_order_trigger_dates(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_order_trigger_dates", params, state)

    @platform_process(
        name="activate_order",
        display_name="Zuora: Activate Order",
        description="Activate a draft order (PUT /v1/orders/{orderNumber}/activate) so its order actions take effect; Zuora refuses any order that is not a draft." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def activate_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("activate_order", params, state)

    @platform_process(
        name="cancel_order",
        display_name="Zuora: Cancel Order",
        description="Cancel a draft or scheduled order (PUT /v1/orders/{orderNumber}/cancel), its terminal state; optional body with cancelReason. Zuora refuses any other order status." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, "body": body("Optional: cancelReason.", required=False), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_order(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_order", params, state)

    @platform_process(
        name="rescan_commitment_associations",
        display_name="Zuora: Rescan Commitment Associations",
        description="Re-evaluate a commitment's account eligibility (POST /v1/orders/commitments/{commitmentNumber}/associations/rescan) and activate newly eligible accounts; additive only, never deactivates." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"commitment_number": ParameterMetadata(type=ParameterType.STRING, required=True, description="Commitment number."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def rescan_commitment_associations(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("rescan_commitment_associations", params, state)

    @platform_process(
        name="get_order_delta_metrics",
        display_name="Zuora: Get Order Delta Metrics",
        description="Read an order's delta metrics (GET /v1/orders/deltaMetrics/{orderNumber}): Order Delta Mrr and Order Delta Tcv. Needs the vendor's Order Delta Metrics feature." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"order_number": ORDER_NUMBER, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_order_delta_metrics(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_order_delta_metrics", params, state)

    @platform_process(
        name="list_pending_orders",
        display_name="Zuora: List Pending Orders",
        description="List the pending orders of a subscription (GET /v1/orders/subscription/{subscription-key}/pending): orders waiting for service activation or customer acceptance." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_pending_orders(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_pending_orders", params, state)
