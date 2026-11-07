"""``@platform_process`` declarations for the Zuora Payment Methods and Payments families (mixed into ``ZuoraPlugin``).

Same contract as ``account_processes``: EDGE, deferred completion, one vendor
operation from ``payment_actions`` per job, vendor RBAC the only boundary.
Card, bank-account, gateway-token and wallet-token values are redacted from
every echo.
"""

from __future__ import annotations

from typing import Any, Final

from ananta.core.actions.action_metadata import ParameterMetadata, platform_process

from .commerce_params import (
    ASYNC_JOB_HEADER_PARAMS,
    EDGE,
    IDEMPOTENT_HEADER_PARAMS,
    NONE,
    PAGE_PARAMS,
    QUEUED_NOTE,
    READ,
    V1_HEADER_PARAMS,
    WALLET_HEADER_PARAMS,
    WRITE,
    body,
    flag,
    integer,
    number,
    text,
)
from .workflow_processes import QUEUED

PAYMENT_METHOD_ID: Final[ParameterMetadata] = text("Payment method id.", required=True)
PROFILE_NUMBER: Final[ParameterMetadata] = integer("Stored credential profile number.", required=True)
PAYMENT_KEY: Final[ParameterMetadata] = text("Payment number or payment id.", required=True)
PART_ID: Final[ParameterMetadata] = text("Payment part id.", required=True)
PAYMENT_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    **PAGE_PARAMS,
    "account_id": text("Only payments of this customer account id."),
    "amount": number("Only payments of this amount (a number)."),
    "applied_amount": number("Only payments with this applied amount (a number)."),
    "created_by_id": text("Only payments created by this user id."),
    "created_date": text("Only payments created at this date-time."),
    "credit_balance_amount": number("Only payments with this credit balance amount (a number)."),
    "currency": text("Only payments in this currency."),
    "effective_date": text("Only payments effective on this date."),
    "number": text("Only the payment with this number."),
    "refund_amount": number("Only payments with this refunded amount (a number)."),
    "status": text("Only payments in this status: Draft, Processing, Processed, Error, Canceled or Posted."),
    "type": text("Only External or Electronic payments."),
    "unapplied_amount": number("Only payments with this unapplied amount (a number)."),
    "updated_by_id": text("Only payments last updated by this user id."),
    "updated_date": text("Only payments last updated at this date-time."),
    "sort": text("Up to two sort fields, comma-separated, each optionally prefixed - (ascending) or + (descending), e.g. -number,+amount."),
}


class PaymentProcesses:
    """Typed Payment Methods and Payments verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    # -- Payment methods -------------------------------------------------

    @platform_process(
        name="create_payment_method",
        display_name="Zuora: Create Payment Method",
        description="Create a payment method (POST /v1/payment-methods): card, card reference, bank debit (ACH, SEPA, Bacs, BECS and others), PayPal, Apple Pay, Google Pay or Amazon Pay, on an account (accountKey) or orphaned; card and bank values are redacted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("The payment-method document: type, accountKey, cardNumber/expirationMonth/expirationYear/securityCode, bank fields, tokens, mandateInfo, makeDefault, gatewayOptions and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_payment_method", params, state)

    @platform_process(
        name="decrypt_payment_method",
        display_name="Zuora: Create Payment Method From Encrypted Token",
        description="Create an Apple Pay or Google Pay payment method from the wallet's encrypted token (POST /v1/payment-methods/decryption), optionally processing a payment; body needs integrationType, merchantID and paymentToken, which is redacted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("integrationType, merchantID, paymentToken (required), accountID, invoiceId, paymentGateway, processPayment, cardHolderInfo and MIT profile fields."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def decrypt_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("decrypt_payment_method", params, state)

    @platform_process(
        name="delete_payment_method",
        display_name="Zuora: Delete Payment Method",
        description="Delete a payment method (DELETE /v1/payment-methods/{payment-method-id}); Zuora refuses the account's default payment method until another is made default, with its reasons." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_payment_method", params, state)

    @platform_process(
        name="get_payment_method",
        display_name="Zuora: Get Payment Method",
        description="Read one payment method (GET /v1/payment-methods/{payment-method-id}): type, status, masked card or bank details, holder info, mandate and retry settings." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_payment_method", params, state)

    @platform_process(
        name="update_payment_method",
        display_name="Zuora: Update Payment Method",
        description="Correct or clear a payment method's fields (PUT /v1/payment-methods/{payment-method-id}): holder info, expiry, mandate, retry rule, gateway; send only the fields to change. A security code is redacted." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "body": body("Fields to change: accountHolderInfo, accountKey, expirationMonth, expirationYear, securityCode, mandateInfo, maxConsecutivePaymentFailures, paymentRetryWindow, useDefaultRetryRule, authGateway, gatewayOptions."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_payment_method", params, state)

    @platform_process(
        name="verify_payment_method",
        display_name="Zuora: Verify Payment Method",
        description="Verify a card or PayPal payment method with its gateway (PUT /v1/payment-methods/{payment-method-id}/verify): a zero or one dollar authorization or a billing-agreement check; body is optional." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "body": body("Optional: currencyCode, paymentGatewayName, securityCode, gatewayOptions.", required=False), **ASYNC_JOB_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def verify_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("verify_payment_method", params, state)

    @platform_process(
        name="scrub_payment_method",
        display_name="Zuora: Scrub Payment Method",
        description="Scrub a payment method (PUT /v1/payment-methods/{payment-method-id}/scrub): Zuora replaces its sensitive data and log copies with dummy values, a terminal action; scrub before anonymizing the account." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def scrub_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("scrub_payment_method", params, state)

    @platform_process(
        name="get_payment_method_balance",
        display_name="Zuora: Get Payment Method Balance",
        description="Read an ACH bank account payment method's real-time balance through Plaid Balance (GET /v1/payment-methods/{payment-method-id}/balance); needs Plaid Auth and Balance support enabled." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_payment_method_balance(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_payment_method_balance", params, state)

    @platform_process(
        name="list_stored_credential_profiles",
        display_name="Zuora: List Stored Credential Profiles",
        description="List a payment method's stored credential profiles (GET /v1/payment-methods/{payment-method-id}/profiles); include_all adds cancelled and expired ones." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "include_all": flag("Include cancelled and expired profiles."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_stored_credential_profiles(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_stored_credential_profiles", params, state)

    @platform_process(
        name="create_stored_credential_profile",
        display_name="Zuora: Create Stored Credential Profile",
        description="Create a stored credential (cardholder consent) profile on a payment method (POST /v1/payment-methods/{payment-method-id}/profiles); body needs consentAgreementSrc, status and type." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "body": body("consentAgreementSrc, status, type (required), action, agreedOn, authGateway, cardSecurityCode, consentAgreementRef, networkTransactionId."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_stored_credential_profile(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_stored_credential_profile", params, state)

    @platform_process(
        name="cancel_stored_credential_profile",
        display_name="Zuora: Cancel Stored Credential Profile",
        description="Cancel a stored credential profile (POST /v1/payment-methods/{payment-method-id}/profiles/{profile-number}/cancel), a terminal transition." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "profile_number": PROFILE_NUMBER, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_stored_credential_profile(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_stored_credential_profile", params, state)

    @platform_process(
        name="expire_stored_credential_profile",
        display_name="Zuora: Expire Stored Credential Profile",
        description="Expire a stored credential profile (POST /v1/payment-methods/{payment-method-id}/profiles/{profile-number}/expire), a terminal transition." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "profile_number": PROFILE_NUMBER, **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def expire_stored_credential_profile(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("expire_stored_credential_profile", params, state)

    @platform_process(
        name="add_stored_credential_profile_data",
        display_name="Zuora: Add Stored Credential Profile Data",
        description="Add gateway profile data to a stored credential profile (POST .../profiles/{profile-number}/profile-data); body needs authGateway and networkTransactionId." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_method_id": PAYMENT_METHOD_ID, "profile_number": PROFILE_NUMBER, "body": body("authGateway and networkTransactionId."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def add_stored_credential_profile_data(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("add_stored_credential_profile_data", params, state)

    @platform_process(
        name="list_wallet_domains",
        display_name="Zuora: List Wallet Domains",
        description="List the domains registered for a digital wallet (GET /v1/payment-methods/wallet/domains); wallet_type is required and domain_name narrows to one domain." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"wallet_type": text("Wallet type, e.g. ApplePay.", required=True), "domain_name": text("Only this domain."), **WALLET_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_wallet_domains(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_wallet_domains", params, state)

    @platform_process(
        name="register_wallet_domain",
        display_name="Zuora: Register Wallet Domain",
        description="Register a merchant domain for Apple Pay or Google Pay (POST /v1/payment-methods/wallet/domains); body needs domainName and walletType." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("domainName and walletType (ApplePay or GooglePay)."), **WALLET_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def register_wallet_domain(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("register_wallet_domain", params, state)

    @platform_process(
        name="unregister_wallet_domain",
        display_name="Zuora: Unregister Wallet Domain",
        description="Unregister a wallet domain (DELETE /v1/payment-methods/wallet/domains/{id})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"domain_id": text("Wallet domain id.", required=True), **WALLET_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unregister_wallet_domain(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unregister_wallet_domain", params, state)

    @platform_process(
        name="create_payment_session",
        display_name="Zuora: Create Payment Session",
        description="Create a web payment session for the Zuora Payment Form (POST /web-payments/sessions) and return its token; body needs amount, currency and processPayment." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("amount, currency, processPayment (required), accountId, authAmount, paymentGateway, storePaymentMethod, invoices, gatewayOptions."), **WALLET_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_payment_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_payment_session", params, state)

    # -- Payments ---------------------------------------------------------

    @platform_process(
        name="list_payments",
        display_name="Zuora: List Payments",
        description="List payments (GET /v1/payments) one page per call, filtered by account, amounts, currency, dates, number, status or type and sorted; next_page is the page to pass back, null at the end." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={**PAYMENT_FILTER_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_payments(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_payments", params, state)

    @platform_process(
        name="create_payment",
        display_name="Zuora: Create Payment",
        description="Create an external or electronic payment (POST /v1/payments), optionally applied to invoices or debit memos at once; body needs amount, currency and type." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("amount, currency, type (required), accountId or accountNumber, paymentMethodId, effectiveDate, invoices, debitMemos, gatewayOptions, comment and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_payment", params, state)

    @platform_process(
        name="update_payment",
        display_name="Zuora: Update Payment",
        description="Correct or clear a payment's comment, reference id, gateway state, finance information or payment schedule (PUT /v1/payments/{paymentKey})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "body": body("Fields to change: comment, referenceId, gatewayState, financeInformation, paymentScheduleKey, integration fields."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_payment", params, state)

    @platform_process(
        name="delete_payment",
        display_name="Zuora: Delete Payment",
        description="Delete a payment (DELETE /v1/payments/{paymentKey}); Zuora deletes only a payment in Canceled status, never an overpayment applied to credit balance, and reports its reasons otherwise." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_payment", params, state)

    @platform_process(
        name="get_payment",
        display_name="Zuora: Get Payment",
        description="Read one payment (GET /v1/payments/{paymentKey}): amounts applied, unapplied and refunded, status, gateway response, dates and payment method." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_payment", params, state)

    @platform_process(
        name="apply_payment",
        display_name="Zuora: Apply Payment",
        description="Apply an unapplied payment to invoices and debit memos (PUT /v1/payments/{paymentKey}/apply); unapply_payment reverses it." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "body": body("invoices[] and/or debitMemos[] with amounts and items, effectiveDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def apply_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("apply_payment", params, state)

    @platform_process(
        name="cancel_payment",
        display_name="Zuora: Cancel Payment",
        description="Cancel a payment (PUT /v1/payments/{paymentKey}/cancel): Zuora's terminal Canceled status, required before delete_payment; an overpayment applied to credit balance cannot be cancelled." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def cancel_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("cancel_payment", params, state)

    @platform_process(
        name="list_payment_parts",
        display_name="Zuora: List Payment Parts",
        description="List the parts of a payment (GET /v1/payments/{paymentKey}/parts): one per invoice or debit memo it was applied to, one page per call." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_payment_parts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_payment_parts", params, state)

    @platform_process(
        name="get_payment_part",
        display_name="Zuora: Get Payment Part",
        description="Read one payment part (GET /v1/payments/{paymentKey}/parts/{partid}): the billing document and amount it applied." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "part_id": PART_ID, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_payment_part(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_payment_part", params, state)

    @platform_process(
        name="list_payment_item_parts",
        display_name="Zuora: List Payment Item Parts",
        description="List a payment part's item parts (GET /v1/payments/{paymentKey}/parts/{partid}/item-parts): the invoice or debit memo items paid, one page per call." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "part_id": PART_ID, **PAGE_PARAMS, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_payment_item_parts(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_payment_item_parts", params, state)

    @platform_process(
        name="get_payment_item_part",
        display_name="Zuora: Get Payment Item Part",
        description="Read one payment item part (GET /v1/payments/{paymentKey}/parts/{partid}/item-parts/{itempartid})." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "part_id": PART_ID, "item_part_id": text("Payment item part id.", required=True), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_payment_item_part(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_payment_item_part", params, state)

    @platform_process(
        name="refund_payment",
        display_name="Zuora: Refund Payment",
        description="Refund the unapplied amount of a payment (POST /v1/payments/{paymentKey}/refunds), externally or through the gateway; body needs totalAmount and type." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "body": body("totalAmount, type (required), methodType, refundDate, reasonCode, comment, referenceId, gatewayOptions, financeInformation."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def refund_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("refund_payment", params, state)

    @platform_process(
        name="refund_payment_with_auto_unapply",
        display_name="Zuora: Refund Payment With Auto Unapply",
        description="Refund a payment, first unapplying it from invoices and debit memos (POST /v1/payments/{paymentKey}/refunds/unapply), optionally writing off; body needs type." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "body": body("type (required), totalAmount, invoices, debitMemos, writeOff, writeOffOptions, refundDate, reasonCode, comment."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def refund_payment_with_auto_unapply(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("refund_payment_with_auto_unapply", params, state)

    @platform_process(
        name="transfer_payment",
        display_name="Zuora: Transfer Payment",
        description="Transfer an unapplied payment to another customer account or detach it (PUT /v1/payments/{paymentKey}/transfer)." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "body": body("accountId of the target account (empty to detach) and effectiveDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def transfer_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("transfer_payment", params, state)

    @platform_process(
        name="unapply_payment",
        display_name="Zuora: Unapply Payment",
        description="Unapply a payment from invoices and debit memos (PUT /v1/payments/{paymentKey}/unapply), reversing apply_payment and returning the amount to unapplied." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"payment_key": PAYMENT_KEY, "body": body("invoices[] and/or debitMemos[] with amounts and items, effectiveDate."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def unapply_payment(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("unapply_payment", params, state)
