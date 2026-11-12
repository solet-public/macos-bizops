"""Zuora Orders and Subscriptions: every operation in the vendor's two sections.

Routes, query parameters, headers and required body fields were checked
against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/orders`` and ``/subscriptions``, reference version
2026-11-06). The vendor role on the registered OAuth client is the only
boundary; reads, writes and deletes are equally first-class.

Lifecycle inventory (vendor-defined):

* Orders: preview (sync or async), create (sync or async, ``Draft``,
  ``Pending``, ``Completed`` or ``Scheduled``), read one or list by tenant,
  subscription owner, invoice owner, subscription or pending state, correct a
  draft or scheduled order (``update_order``), correct its custom fields or
  trigger dates, activate a draft, cancel a draft or scheduled order, delete
  an order (sync or async), read the order's delta metrics, and rescan a
  commitment's account associations. An async job is read with
  ``get_async_order_job``.
* Subscriptions: preview new or existing, create, read current or a specific
  version, update, renew, cancel (terminal per ``cancellationPolicy``),
  suspend and resume (the vendor's close/reopen pair), delete, correct the
  custom fields of a version, and read metrics. The list by account is the
  existing ``list_subscriptions`` export verb.

Zuora publishes no ETag or version precondition on these routes: a concurrent
change is last-writer-wins, so read before you write. ``Idempotency-Key``
(where documented) lets a caller make a create safely repeatable; the plugin
itself never replays a write.
"""

from __future__ import annotations

from typing import Final

from .commerce_fields import (
    ACCOUNT_NUMBER,
    ASYNC_JOB_HEADERS,
    IDEMPOTENT_HEADERS,
    ORDER_LIST_FIELDS,
    ORDER_NUMBER,
    PREVIEW_EXISTING_HEADERS,
    SEGMENT_FIELDS,
    SUBSCRIPTION_KEY,
    SUBSCRIPTION_NUMBER,
    SUBSCRIPTION_VERSION,
    V1_HEADERS,
    page_check,
    segment_check,
    vendor_body,
)
from .typed_operations import Field, Operation

_RETURN_IDS: Final[dict[str, Field]] = {"return_ids": Field("returnIds", "bool")}
_SUMMARIZE: Final[dict[str, Field]] = {"summarize_invoice_item": Field("summarizeInvoiceItem", "bool")}
_ORDER_REQUIRED: Final[tuple[str, ...]] = ("orderDate",)
_PREVIEW_REQUIRED: Final[tuple[str, ...]] = ("orderDate", "previewOptions")
# renewalTerm is marked required but documents "Default is 0"; Zuora decides that one.
_SUBSCRIPTION_REQUIRED: Final[tuple[str, ...]] = ("accountKey", "contractEffectiveDate", "subscribeToRatePlans", "termType")
_SUBSCRIPTION_PREVIEW_REQUIRED: Final[tuple[str, ...]] = ("contractEffectiveDate", "subscribeToRatePlans", "termType")


ORDER_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation(
        "preview_order",
        "POST",
        "/v1/orders/preview",
        query_fields=_SUMMARIZE,
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(*_PREVIEW_REQUIRED),
        envelope=True,
    ),
    Operation(
        "preview_order_async",
        "POST",
        "/v1/async/orders/preview",
        query_fields=_SUMMARIZE,
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(*_PREVIEW_REQUIRED),
        envelope=True,
    ),
    Operation("list_orders", "GET", "/v1/orders", query_fields=ORDER_LIST_FIELDS, header_fields=V1_HEADERS, query_check=page_check, paging="page_number", envelope=True),
    Operation(
        "create_order",
        "POST",
        "/v1/orders",
        query_fields=_RETURN_IDS,
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(*_ORDER_REQUIRED),
        envelope=True,
    ),
    Operation(
        "create_order_async",
        "POST",
        "/v1/async/orders",
        query_fields=_RETURN_IDS,
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(*_ORDER_REQUIRED),
        envelope=True,
    ),
    Operation("delete_order", "DELETE", "/v1/orders/{order_number}", path_fields=ORDER_NUMBER, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "get_order",
        "GET",
        "/v1/orders/{order_number}",
        path_fields=ORDER_NUMBER,
        query_fields={"get_account_details": Field("getAccountDetails", "bool")},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation(
        "update_order",
        "PUT",
        "/v1/orders/{order_number}",
        path_fields=ORDER_NUMBER,
        header_fields=V1_HEADERS,
        body=vendor_body(*_ORDER_REQUIRED),
        envelope=True,
    ),
    Operation("delete_order_async", "DELETE", "/v1/async/orders/{order_number}", path_fields=ORDER_NUMBER, header_fields=IDEMPOTENT_HEADERS, envelope=True),
    Operation(
        "get_async_order_job",
        "GET",
        "/v1/async-jobs/{job_id}",
        path_fields={"job_id": Field("job_id", "str_id", required=True)},
        header_fields=ASYNC_JOB_HEADERS,
        envelope=True,
    ),
    Operation(
        "list_orders_by_subscription_owner",
        "GET",
        "/v1/orders/subscriptionOwner/{account_number}",
        path_fields=ACCOUNT_NUMBER,
        query_fields={key: spec for key, spec in ORDER_LIST_FIELDS.items() if key != "status"},
        header_fields=V1_HEADERS,
        query_check=page_check,
        paging="page_number",
        envelope=True,
    ),
    Operation(
        "list_orders_by_subscription",
        "GET",
        "/v1/orders/subscription/{subscription_number}",
        path_fields=SUBSCRIPTION_NUMBER,
        query_fields=ORDER_LIST_FIELDS,
        header_fields=V1_HEADERS,
        query_check=page_check,
        paging="page_number",
        envelope=True,
    ),
    Operation(
        "list_orders_by_invoice_owner",
        "GET",
        "/v1/orders/invoiceOwner/{account_number}",
        path_fields=ACCOUNT_NUMBER,
        query_fields=ORDER_LIST_FIELDS,
        header_fields=V1_HEADERS,
        query_check=page_check,
        paging="page_number",
        envelope=True,
    ),
    Operation("update_order_custom_fields", "PUT", "/v1/orders/{order_number}/customFields", path_fields=ORDER_NUMBER, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation(
        "update_subscription_custom_fields",
        "PUT",
        "/v1/subscriptions/{subscription_number}/customFields",
        path_fields=SUBSCRIPTION_NUMBER,
        header_fields=V1_HEADERS,
        body=vendor_body(),
        envelope=True,
    ),
    Operation("update_order_trigger_dates", "PUT", "/v1/orders/{order_number}/triggerDates", path_fields=ORDER_NUMBER, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation("activate_order", "PUT", "/v1/orders/{order_number}/activate", path_fields=ORDER_NUMBER, header_fields=V1_HEADERS, envelope=True),
    Operation("cancel_order", "PUT", "/v1/orders/{order_number}/cancel", path_fields=ORDER_NUMBER, header_fields=V1_HEADERS, body=vendor_body(optional=True), envelope=True),
    Operation(
        "rescan_commitment_associations",
        "POST",
        "/v1/orders/commitments/{commitment_number}/associations/rescan",
        path_fields={"commitment_number": Field("commitment_number", "str_id", required=True)},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation("get_order_delta_metrics", "GET", "/v1/orders/deltaMetrics/{order_number}", path_fields=ORDER_NUMBER, header_fields=V1_HEADERS, envelope=True),
    Operation("list_pending_orders", "GET", "/v1/orders/subscription/{subscription_key}/pending", path_fields=SUBSCRIPTION_KEY, header_fields=V1_HEADERS, envelope=True),
)

SUBSCRIPTION_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation(
        "preview_subscription",
        "POST",
        "/v1/subscriptions/preview",
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(*_SUBSCRIPTION_PREVIEW_REQUIRED),
        envelope=True,
    ),
    Operation(
        "preview_existing_subscription",
        "POST",
        "/v1/subscriptions/{subscription_key}/preview",
        path_fields=SUBSCRIPTION_KEY,
        header_fields=PREVIEW_EXISTING_HEADERS,
        body=vendor_body(optional=True),
        envelope=True,
    ),
    Operation(
        "create_subscription",
        "POST",
        "/v1/subscriptions",
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body(*_SUBSCRIPTION_REQUIRED),
        envelope=True,
    ),
    Operation(
        "get_subscription",
        "GET",
        "/v1/subscriptions/{subscription_key}",
        path_fields=SUBSCRIPTION_KEY,
        query_fields={
            **SEGMENT_FIELDS,
            "get_invoice_owner_details": Field("getInvoiceOwnerDetails", "bool"),
            "get_subscription_owner_details": Field("getSubscriptionOwnerDetails", "bool"),
        },
        header_fields=V1_HEADERS,
        query_check=segment_check,
        envelope=True,
    ),
    Operation("update_subscription", "PUT", "/v1/subscriptions/{subscription_key}", path_fields=SUBSCRIPTION_KEY, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation(
        "get_subscription_version",
        "GET",
        "/v1/subscriptions/{subscription_key}/versions/{version}",
        path_fields=SUBSCRIPTION_VERSION,
        query_fields=SEGMENT_FIELDS,
        header_fields=V1_HEADERS,
        query_check=segment_check,
        envelope=True,
    ),
    Operation("renew_subscription", "PUT", "/v1/subscriptions/{subscription_key}/renew", path_fields=SUBSCRIPTION_KEY, header_fields=V1_HEADERS, body=vendor_body(optional=True), envelope=True),
    Operation(
        "cancel_subscription",
        "PUT",
        "/v1/subscriptions/{subscription_key}/cancel",
        path_fields=SUBSCRIPTION_KEY,
        header_fields=V1_HEADERS,
        body=vendor_body("cancellationPolicy"),
        envelope=True,
    ),
    Operation(
        "resume_subscription",
        "PUT",
        "/v1/subscriptions/{subscription_key}/resume",
        path_fields=SUBSCRIPTION_KEY,
        header_fields=V1_HEADERS,
        body=vendor_body("resumePolicy"),
        envelope=True,
    ),
    Operation(
        "suspend_subscription",
        "PUT",
        "/v1/subscriptions/{subscription_key}/suspend",
        path_fields=SUBSCRIPTION_KEY,
        header_fields=V1_HEADERS,
        body=vendor_body("suspendPolicy"),
        envelope=True,
    ),
    Operation("delete_subscription", "PUT", "/v1/subscriptions/{subscription_key}/delete", path_fields=SUBSCRIPTION_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "update_subscription_version_custom_fields",
        "PUT",
        "/v1/subscriptions/{subscription_number}/versions/{version}/customFields",
        path_fields={**SUBSCRIPTION_NUMBER, "version": Field("version", "str_id", required=True)},
        header_fields=V1_HEADERS,
        body=vendor_body(),
        envelope=True,
    ),
    Operation(
        "get_subscription_metrics",
        "GET",
        "/v1/subscriptions/subscription-metrics",
        query_fields={
            "subscription_numbers": Field("subscriptionNumbers[]", "str_list", required=True),
            "as_of_day": Field("asOfDay", "date"),
        },
        header_fields=V1_HEADERS,
        envelope=True,
    ),
)
