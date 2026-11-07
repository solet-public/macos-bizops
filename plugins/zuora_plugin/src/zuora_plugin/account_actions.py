"""Zuora Accounts: every operation in the vendor's Accounts section.

Checked against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/accounts``, reference version 2026-11-06).

Lifecycle inventory (vendor-defined): create (optionally with an embedded
payment method and a first subscription), read basic information or the full
summary (subscriptions, invoices, payments, usage), update or clear fields
through ``update_account`` (Zuora's own null semantics per field), read the
account's payment methods and its default, read and set the payment-method
cascading consent, and delete. The Accounts section publishes no close or
reopen route; delete is its terminal action. Zuora deletes asynchronously:
the answer carries ``jobId`` and ``jobStatus: Pending``, and the backend job
removes the account's transactions and sets its status to Cancelled. Zuora
refuses the delete for an account in a split-owner subscription or an owner
transfer; that refusal is reported with the vendor's reasons.

Payment-instrument values a caller embeds in a create body (card
number, security code, bank account, wallet tokens) are redacted from every
vendor echo, error and audit line.
"""

from __future__ import annotations

from typing import Final

from .commerce_fields import ACCOUNT_KEY, IDEMPOTENT_HEADERS, V1_HEADERS, payment_secrets, vendor_body
from .typed_operations import Field, Operation

ACCOUNT_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation(
        "create_account",
        "POST",
        "/v1/accounts",
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body("billToContact", "currency", "name"),
        secrets=payment_secrets,
        envelope=True,
    ),
    Operation("delete_account", "DELETE", "/v1/accounts/{account_key}", path_fields=ACCOUNT_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation("get_account", "GET", "/v1/accounts/{account_key}", path_fields=ACCOUNT_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "update_account",
        "PUT",
        "/v1/accounts/{account_key}",
        path_fields=ACCOUNT_KEY,
        header_fields=V1_HEADERS,
        body=vendor_body(),
        envelope=True,
    ),
    Operation(
        "list_account_payment_methods",
        "GET",
        "/v1/accounts/{account_key}/payment-methods",
        path_fields=ACCOUNT_KEY,
        query_fields={"is_default_only": Field("isDefaultOnly", "bool"), "is_active_only": Field("isActiveOnly", "bool")},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation(
        "get_account_payment_method_cascading",
        "GET",
        "/v1/accounts/{account_key}/payment-methods/cascading",
        path_fields=ACCOUNT_KEY,
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation(
        "update_account_payment_method_cascading",
        "PUT",
        "/v1/accounts/{account_key}/payment-methods/cascading",
        path_fields=ACCOUNT_KEY,
        header_fields=V1_HEADERS,
        body=vendor_body(),
        envelope=True,
    ),
    Operation(
        "get_account_default_payment_method",
        "GET",
        "/v1/accounts/{account_key}/payment-methods/default",
        path_fields=ACCOUNT_KEY,
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation(
        "get_account_summary",
        "GET",
        "/v1/accounts/{account_key}/summary",
        path_fields=ACCOUNT_KEY,
        query_fields={"exclude_usage": Field("excludeUsage", "bool")},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
)
