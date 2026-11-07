"""Zuora Payment Methods and Payments: every operation in the vendor's two sections.

Checked against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/payment-methods`` and ``/payments``, reference version
2026-11-06): path, method, query parameters, documented request headers and
required top-level body fields for all 17 Payment Methods and all 15 Payments
operations.

Payment-method lifecycle (vendor-defined): create (attached to an account or
orphan), read, update or clear fields through ``update_payment_method``,
verify against the gateway, read a bank account's balance, scrub (erase the
sensitive data; terminal for the instrument's details) and delete. Stored
credential profiles are created, listed, given gateway profile data, and
ended by cancel or expire, the vendor's two terminal transitions. Apple Pay
and Google Pay wallet domains are registered, listed and unregistered. A web payment
session is created for the Payment Form. Decryption creates a payment method
(and optionally a payment) from an encrypted Apple Pay or Google Pay token.

Payment lifecycle: create (external or electronic, optionally applied), list
and read with its parts and item parts, update or clear fields, apply and
unapply to invoices and debit memos (the reversible pair), transfer an
unapplied payment to another account, refund (optionally unapplying first),
cancel (terminal for external payments) and delete (a cancelled payment).
Payment history is the vendor's: every write is one vendor call that Zuora
records; nothing here replays a write.

Every card number, security code, bank account number, IBAN, gateway token,
wallet token and encrypted payment token a caller sends is redacted from
vendor echoes, errors and audit lines.
"""

from __future__ import annotations

from typing import Final

from .commerce_fields import (
    ASYNC_JOB_HEADERS,
    IDEMPOTENT_HEADERS,
    PAGE_FIELDS,
    V1_HEADERS,
    WALLET_HEADERS,
    instrument_secrets,
    page_check,
    payment_secrets,
    vendor_body,
)
from .typed_operations import Field, Operation

PAYMENT_METHOD_ID: Final[dict[str, Field]] = {"payment_method_id": Field("payment_method_id", "str_id", required=True)}
PROFILE: Final[dict[str, Field]] = {**PAYMENT_METHOD_ID, "profile_number": Field("profile_number", "int_id", required=True)}
PAYMENT_KEY: Final[dict[str, Field]] = {"payment_key": Field("payment_key", "str_id", required=True)}
PAYMENT_PART: Final[dict[str, Field]] = {**PAYMENT_KEY, "part_id": Field("part_id", "str_id", required=True)}
PAYMENT_ITEM_PART: Final[dict[str, Field]] = {**PAYMENT_PART, "item_part_id": Field("item_part_id", "str_id", required=True)}

_PM = "/v1/payment-methods/{payment_method_id}"
_PAYMENT = "/v1/payments/{payment_key}"

PAYMENT_LIST_FIELDS: Final[dict[str, Field]] = {
    **PAGE_FIELDS,
    "account_id": Field("accountId", "text"),
    "amount": Field("amount", "number"),
    "applied_amount": Field("appliedAmount", "number"),
    "created_by_id": Field("createdById", "text"),
    "created_date": Field("createdDate", "text"),
    "credit_balance_amount": Field("creditBalanceAmount", "number"),
    "currency": Field("currency", "text"),
    "effective_date": Field("effectiveDate", "text"),
    "number": Field("number", "text"),
    "refund_amount": Field("refundAmount", "number"),
    "status": Field("status", "text"),
    "type": Field("type", "text"),
    "unapplied_amount": Field("unappliedAmount", "number"),
    "updated_by_id": Field("updatedById", "text"),
    "updated_date": Field("updatedDate", "text"),
    "sort": Field("sort", "text"),
}

PAYMENT_METHOD_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("create_payment_method", "POST", "/v1/payment-methods", header_fields=IDEMPOTENT_HEADERS, body=vendor_body(), secrets=instrument_secrets, envelope=True),
    Operation(
        "decrypt_payment_method",
        "POST",
        "/v1/payment-methods/decryption",
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body("integrationType", "merchantID", "paymentToken"),
        secrets=instrument_secrets,
        envelope=True,
    ),
    Operation("delete_payment_method", "DELETE", _PM, path_fields=PAYMENT_METHOD_ID, header_fields=V1_HEADERS, envelope=True),
    Operation("get_payment_method", "GET", _PM, path_fields=PAYMENT_METHOD_ID, header_fields=V1_HEADERS, envelope=True),
    Operation("update_payment_method", "PUT", _PM, path_fields=PAYMENT_METHOD_ID, header_fields=V1_HEADERS, body=vendor_body(), secrets=instrument_secrets, envelope=True),
    Operation(
        "verify_payment_method",
        "PUT",
        _PM + "/verify",
        path_fields=PAYMENT_METHOD_ID,
        header_fields=ASYNC_JOB_HEADERS,
        body=vendor_body(optional=True),
        secrets=instrument_secrets,
        envelope=True,
    ),
    Operation("scrub_payment_method", "PUT", _PM + "/scrub", path_fields=PAYMENT_METHOD_ID, header_fields=V1_HEADERS, envelope=True),
    Operation("get_payment_method_balance", "GET", _PM + "/balance", path_fields=PAYMENT_METHOD_ID, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "list_stored_credential_profiles",
        "GET",
        _PM + "/profiles",
        path_fields=PAYMENT_METHOD_ID,
        query_fields={"include_all": Field("includeAll", "bool")},
        header_fields=V1_HEADERS,
        envelope=True,
    ),
    Operation(
        "create_stored_credential_profile",
        "POST",
        _PM + "/profiles",
        path_fields=PAYMENT_METHOD_ID,
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body("consentAgreementSrc", "status", "type"),
        secrets=instrument_secrets,
        envelope=True,
    ),
    Operation("cancel_stored_credential_profile", "POST", _PM + "/profiles/{profile_number}/cancel", path_fields=PROFILE, header_fields=IDEMPOTENT_HEADERS, envelope=True),
    Operation("expire_stored_credential_profile", "POST", _PM + "/profiles/{profile_number}/expire", path_fields=PROFILE, header_fields=IDEMPOTENT_HEADERS, envelope=True),
    Operation(
        "add_stored_credential_profile_data",
        "POST",
        _PM + "/profiles/{profile_number}/profile-data",
        path_fields=PROFILE,
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body("authGateway", "networkTransactionId"),
        envelope=True,
    ),
    Operation(
        "list_wallet_domains",
        "GET",
        "/v1/payment-methods/wallet/domains",
        query_fields={"wallet_type": Field("walletType", "text", required=True), "domain_name": Field("domainName", "text")},
        header_fields=WALLET_HEADERS,
        envelope=True,
    ),
    Operation("register_wallet_domain", "POST", "/v1/payment-methods/wallet/domains", header_fields=WALLET_HEADERS, body=vendor_body("domainName", "walletType"), envelope=True),
    Operation(
        "unregister_wallet_domain",
        "DELETE",
        "/v1/payment-methods/wallet/domains/{domain_id}",
        path_fields={"domain_id": Field("domain_id", "str_id", required=True)},
        header_fields=WALLET_HEADERS,
        envelope=True,
    ),
    Operation(
        "create_payment_session",
        "POST",
        "/web-payments/sessions",
        header_fields=WALLET_HEADERS,
        body=vendor_body("amount", "currency", "processPayment"),
        secrets=payment_secrets,
        revealed_keys=frozenset({"token"}),
    ),
)

PAYMENT_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("list_payments", "GET", "/v1/payments", query_fields=PAYMENT_LIST_FIELDS, header_fields=V1_HEADERS, query_check=page_check, paging="page_number", envelope=True),
    Operation(
        "create_payment",
        "POST",
        "/v1/payments",
        header_fields=IDEMPOTENT_HEADERS,
        body=vendor_body("amount", "currency", "type"),
        secrets=payment_secrets,
        envelope=True,
    ),
    Operation("update_payment", "PUT", _PAYMENT, path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation("delete_payment", "DELETE", _PAYMENT, path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation("get_payment", "GET", _PAYMENT, path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation("apply_payment", "PUT", _PAYMENT + "/apply", path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation("cancel_payment", "PUT", _PAYMENT + "/cancel", path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "list_payment_parts",
        "GET",
        _PAYMENT + "/parts",
        path_fields=PAYMENT_KEY,
        query_fields=PAGE_FIELDS,
        header_fields=V1_HEADERS,
        query_check=page_check,
        paging="page_number",
        envelope=True,
    ),
    Operation("get_payment_part", "GET", _PAYMENT + "/parts/{part_id}", path_fields=PAYMENT_PART, header_fields=V1_HEADERS, envelope=True),
    Operation(
        "list_payment_item_parts",
        "GET",
        _PAYMENT + "/parts/{part_id}/item-parts",
        path_fields=PAYMENT_PART,
        query_fields=PAGE_FIELDS,
        header_fields=V1_HEADERS,
        query_check=page_check,
        paging="page_number",
        envelope=True,
    ),
    Operation("get_payment_item_part", "GET", _PAYMENT + "/parts/{part_id}/item-parts/{item_part_id}", path_fields=PAYMENT_ITEM_PART, header_fields=V1_HEADERS, envelope=True),
    Operation("refund_payment", "POST", _PAYMENT + "/refunds", path_fields=PAYMENT_KEY, header_fields=IDEMPOTENT_HEADERS, body=vendor_body("totalAmount", "type"), envelope=True),
    Operation("refund_payment_with_auto_unapply", "POST", _PAYMENT + "/refunds/unapply", path_fields=PAYMENT_KEY, header_fields=IDEMPOTENT_HEADERS, body=vendor_body("type"), envelope=True),
    Operation("transfer_payment", "PUT", _PAYMENT + "/transfer", path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
    Operation("unapply_payment", "PUT", _PAYMENT + "/unapply", path_fields=PAYMENT_KEY, header_fields=V1_HEADERS, body=vendor_body(), envelope=True),
)
