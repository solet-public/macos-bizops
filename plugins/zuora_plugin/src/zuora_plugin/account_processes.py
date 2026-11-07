"""``@platform_process`` declarations for the Zuora Accounts family (mixed into ``ZuoraPlugin``).

Same contract as ``order_processes``: EDGE, deferred completion, one vendor
operation from ``account_actions.ACCOUNT_OPERATIONS`` per job, vendor RBAC the
only boundary. Card, bank-account and wallet values in a create body are
redacted from every echo.
"""

from __future__ import annotations

from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, platform_process

from .commerce_params import ACCOUNT_KEY, EDGE, IDEMPOTENT_HEADER_PARAMS, NONE, QUEUED_NOTE, READ, V1_HEADER_PARAMS, WRITE, body
from .workflow_processes import QUEUED


class AccountProcesses:
    """Typed Accounts verbs. ``ZuoraPlugin`` supplies ``_dispatch_typed``."""

    def _dispatch_typed(self, action_name: str, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    @platform_process(
        name="create_account",
        display_name="Zuora: Create Account",
        description="Create a customer account (POST /v1/accounts), optionally with contacts, a payment method and a first subscription. body needs name, currency and billToContact; card and bank values are redacted from every echo." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"body": body("The account document: name, currency, billToContact (required), soldToContact, billCycleDay, paymentTerm, creditCard or paymentMethod, subscription, custom fields and the rest."), **IDEMPOTENT_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def create_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("create_account", params, state)

    @platform_process(
        name="delete_account",
        display_name="Zuora: Delete Account",
        description="Delete a customer account (DELETE /v1/accounts/{account-key}); Zuora runs a backend job that removes its transactions and sets it Cancelled, answering with jobId and jobStatus Pending." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def delete_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("delete_account", params, state)

    @platform_process(
        name="get_account",
        display_name="Zuora: Get Account",
        description="Read an account's basic info, billing and payment settings, contacts, tax info and metrics (GET /v1/accounts/{account-key}), without its subscriptions, invoices or payments." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_account", params, state)

    @platform_process(
        name="update_account",
        display_name="Zuora: Update Account",
        description="Correct an account (PUT /v1/accounts/{account-key}): name, contacts, bill cycle day, payment term, autopay, default payment method, templates, notes and custom fields; send only the fields to change." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, "body": body("The account fields to change, e.g. name, billToContact, soldToContact, billCycleDay, autoPay, defaultPaymentMethodId, notes, custom fields."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_account(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_account", params, state)

    @platform_process(
        name="list_account_payment_methods",
        display_name="Zuora: List Account Payment Methods",
        description="List an account's payment methods grouped by type (GET /v1/accounts/{account-key}/payment-methods), optionally only the default or only active ones." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "account_key": ACCOUNT_KEY,
            "is_default_only": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Only the default payment method."),
            "is_active_only": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Only active payment methods."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def list_account_payment_methods(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("list_account_payment_methods", params, state)

    @platform_process(
        name="get_account_payment_method_cascading",
        display_name="Zuora: Get Account Payment Method Cascading",
        description="Read an account's payment-method cascading configuration (GET /v1/accounts/{account-key}/payment-methods/cascading): the consent flag and the ordered fallback payment methods." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_account_payment_method_cascading(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_account_payment_method_cascading", params, state)

    @platform_process(
        name="update_account_payment_method_cascading",
        display_name="Zuora: Update Account Payment Method Cascading",
        description="Set or withdraw an account's payment-method cascading (PUT /v1/accounts/{account-key}/payment-methods/cascading): consent true or false and the priorities list of payment method ids and order." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, "body": body("consent and priorities[] (paymentMethodId, order)."), **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=WRITE,
        context_handling=NONE,
    )
    def update_account_payment_method_cascading(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("update_account_payment_method_cascading", params, state)

    @platform_process(
        name="get_account_default_payment_method",
        display_name="Zuora: Get Account Default Payment Method",
        description="Read an account's default payment method (GET /v1/accounts/{account-key}/payment-methods/default), masked as Zuora returns it." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={"account_key": ACCOUNT_KEY, **V1_HEADER_PARAMS},
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_account_default_payment_method(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_account_default_payment_method", params, state)

    @platform_process(
        name="get_account_summary",
        display_name="Zuora: Get Account Summary",
        description="Read an account summary (GET /v1/accounts/{account-key}/summary): basic info, contacts, tax info, subscriptions, invoices, payments and usage; exclude_usage leaves usage out." + QUEUED_NOTE,
        processor_policy_category=EDGE,
        parameters={
            "account_key": ACCOUNT_KEY,
            "exclude_usage": ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description="Leave usage records out of the summary."),
            **V1_HEADER_PARAMS,
        },
        return_value_schema=QUEUED,
        error_processor_customizations=READ,
        context_handling=NONE,
    )
    def get_account_summary(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return self._dispatch_typed("get_account_summary", params, state)
