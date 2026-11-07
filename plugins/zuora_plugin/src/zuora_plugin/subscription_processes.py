"""``@platform_process`` declarations for the Zuora Subscriptions family (mixed into ``ZuoraPlugin``).

Same contract as ``order_processes``: EDGE, deferred completion, one vendor
operation from ``order_actions.SUBSCRIPTION_OPERATIONS`` per job, vendor RBAC
the only boundary. The list of an account's subscriptions stays the existing
``list_subscriptions`` export verb.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, platform_process

from .commerce_params import (
    EDGE,
    IDEMPOTENT_HEADER_PARAMS,
    NONE,
    PREVIEW_EXISTING_HEADER_PARAMS,
    QUEUED_NOTE,
    READ,
    SEGMENT_PARAMS,
    SUBSCRIPTION_KEY,
    SUBSCRIPTION_NUMBER,
    V1_HEADER_PARAMS,
    VERSION,
    WRITE,
    body,
)
from .workflow_processes import QUEUED

_BILLING_OPTIONS = "optional runBilling, collect, targetDate, documentDate and credit options"


class SubscriptionProcesses:
    """Typed Subscriptions verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="preview_subscription",
        display_name="Zuora: Preview Subscription",
        description="Preview a new subscription's invoices and charges (POST /v1/subscriptions/preview) for an existing account or a previewAccountInfo, saving nothing. body needs contractEffectiveDate, subscribeToRatePlans and termType." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("The subscription preview document: contractEffectiveDate, subscribeToRatePlans, termType (required), accountKey or previewAccountInfo, previewType, targetDate and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def preview_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("preview_subscription", params, state)

    @platform_process(
        name="preview_existing_subscription",
        display_name="Zuora: Preview Existing Subscription",
        description="Preview an existing subscription's current and future invoices or credit memos (POST /v1/subscriptions/{subscription-key}/preview) between previewStartDate and previewThroughDate; Zuora needs Zuora-Version 207.0 or later." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "subscription_key": SUBSCRIPTION_KEY,
            "body": body("previewStartDate, previewThroughDate, quantityForUsageCharges and skipTax.", required=False),
            **PREVIEW_EXISTING_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def preview_existing_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("preview_existing_subscription", params, state)

    @platform_process(
        name="create_subscription",
        display_name="Zuora: Create Subscription",
        description="Create a subscription for an account (POST /v1/subscriptions). body needs accountKey, contractEffectiveDate, subscribeToRatePlans and termType; renewal terms, invoicing and custom fields as documented; optional idempotency_key." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("The subscription document: accountKey, contractEffectiveDate, subscribeToRatePlans, termType (required), initialTerm, renewalTerm, autoRenew, invoice options and custom fields."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_subscription", params, state)

    @platform_process(
        name="get_subscription",
        display_name="Zuora: Get Subscription",
        description="Read a subscription's current version with its rate plans and charges (GET /v1/subscriptions/{subscription-key}); choose the charge segments, detailed metrics and owner account details." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "subscription_key": SUBSCRIPTION_KEY,
            **SEGMENT_PARAMS,
            "get_invoice_owner_details": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Include invoiceOwnerAccountDetails."),
            "get_subscription_owner_details": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Include accountOwnerDetails."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_subscription", params, state)

    @platform_process(
        name="update_subscription",
        display_name="Zuora: Update Subscription",
        description="Amend a subscription (PUT /v1/subscriptions/{subscription-key}): add, change or remove rate plans, correct terms, renewal settings, notes and custom fields, with optional preview and billing. Completion carries subscriptionId and any invoice or credit memo." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, "body": body("add, change, update, remove, term and renewal fields, notes, custom fields, preview and billing options."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_subscription", params, state)

    @platform_process(
        name="get_subscription_version",
        display_name="Zuora: Get Subscription Version",
        description="Read one historical version of a subscription (GET /v1/subscriptions/{subscription-key}/versions/{version}) with the same charge-segment and metrics options as get_subscription." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, "version": VERSION, **SEGMENT_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_subscription_version(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_subscription_version", params, state)

    @platform_process(
        name="renew_subscription",
        display_name="Zuora: Renew Subscription",
        description=f"Renew a termed subscription with its existing renewal terms (PUT /v1/subscriptions/{{subscription-key}}/renew), extending it by a new term; {_BILLING_OPTIONS}." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, "body": body("Optional billing options: runBilling, collect, targetDate, documentDate, orderDate, applyCredit.", required=False), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def renew_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("renew_subscription", params, state)

    @platform_process(
        name="cancel_subscription",
        display_name="Zuora: Cancel Subscription",
        description=f"Cancel a subscription (PUT /v1/subscriptions/{{subscription-key}}/cancel), its terminal state: cancellationPolicy EndOfCurrentTerm, EndOfLastInvoicePeriod or SpecificDate (with cancellationEffectiveDate); {_BILLING_OPTIONS}." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, "body": body("cancellationPolicy (required), cancellationEffectiveDate, runBilling, collect, targetDate and credit options."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_subscription", params, state)

    @platform_process(
        name="suspend_subscription",
        display_name="Zuora: Suspend Subscription",
        description="Suspend an active subscription (PUT /v1/subscriptions/{subscription-key}/suspend) by suspendPolicy, optionally scheduling its resumption; resume_subscription reverses it." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, "body": body("suspendPolicy (required), suspend periods or date, optional resume schedule, extendsTerm and billing options."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def suspend_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("suspend_subscription", params, state)

    @platform_process(
        name="resume_subscription",
        display_name="Zuora: Resume Subscription",
        description="Resume a suspended subscription (PUT /v1/subscriptions/{subscription-key}/resume) by resumePolicy Today, FixedPeriodsFromSuspendDate or SpecificDate, optionally extending its term." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, "body": body("resumePolicy (required), resume periods or date, extendsTerm and billing options."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def resume_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("resume_subscription", params, state)

    @platform_process(
        name="delete_subscription",
        display_name="Zuora: Delete Subscription",
        description="Delete a subscription by number (PUT /v1/subscriptions/{subscription-key}/delete); Zuora's own refusal and reasons are reported if it cannot be deleted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"subscription_key": SUBSCRIPTION_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_subscription(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_subscription", params, state)

    @platform_process(
        name="update_subscription_version_custom_fields",
        display_name="Zuora: Update Subscription Version Custom Fields",
        description="Set, correct or clear (null) the custom fields of one subscription version and its rate plans and charges (PUT /v1/subscriptions/{subscriptionNumber}/versions/{version}/customFields)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "subscription_number": SUBSCRIPTION_NUMBER,
            "version": VERSION,
            "body": body("customFields and ratePlans[] (ratePlanId, charges[]) customFields to set; null clears a field."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_subscription_version_custom_fields(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_subscription_version_custom_fields", params, state)

    @platform_process(
        name="get_subscription_metrics",
        display_name="Zuora: Get Subscription Metrics",
        description="Read MRR and TCV metrics for a list of subscription numbers (GET /v1/subscriptions/subscription-metrics) as of a day (default today)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "subscription_numbers": ParameterMetadata(type=ParameterType.LIST, required=True, description="Subscription numbers, sent as subscriptionNumbers[]."),
            "as_of_day": ParameterMetadata(type=ParameterType.STRING, required=False, description="YYYY-MM-DD; default today."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_subscription_metrics(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_subscription_metrics", params, state)
