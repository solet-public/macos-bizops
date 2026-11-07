"""Shared input shapes for the typed v1 commerce families: Orders, Subscriptions, Accounts, Payment Methods and Payments.

Checked against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/{orders,subscriptions,accounts,payment-methods,payments}``,
reference version 2026-11-06):

* Request headers. Every route in these sections documents ``Zuora-Version``,
  ``Zuora-Entity-Ids``, ``Zuora-Org-Ids`` and ``Zuora-Track-Id``; the POST
  routes that create orders, subscriptions or accounts add
  ``Idempotency-Key``. Two routes document a narrower set (see
  :data:`ASYNC_JOB_HEADERS` and :data:`PREVIEW_EXISTING_HEADERS`). The
  documented ``Accept-Encoding`` is negotiated by the HTTP client itself, and
  the plugin always sends a plain JSON body, so ``Content-Encoding`` is never
  needed. ``Authorization`` is the registered credential's bearer, never a
  caller value.
* Request bodies. The order, subscription and account documents carry
  hundreds of vendor fields, so a verb takes the vendor's JSON document as
  ``body`` and forwards it unchanged: every documented field is accepted. The
  documented required top-level fields are checked before any vendor call.
* Payment instruments. An account (or an order's new account) may embed a
  card, bank account or wallet token. Those values are secret material:
  :func:`payment_secrets` hands them to the redactor so a vendor echo, error
  or audit line never carries them. A payment-method body *is* an
  instrument, so :func:`instrument_secrets` treats its top level as one
  (there ``accountNumber`` is a bank account number, not a customer account).
"""

from __future__ import annotations

from typing import Any, Final

from .typed_operations import BodyBuilder, Field, PathGate, collect, object_param

V1_HEADERS: Final[dict[str, Field]] = {
    "zuora_version": Field("Zuora-Version", "header"),
    "zuora_entity_ids": Field("Zuora-Entity-Ids", "header"),
    "zuora_org_ids": Field("Zuora-Org-Ids", "header"),
    "zuora_track_id": Field("Zuora-Track-Id", "track_id"),
}
IDEMPOTENT_HEADERS: Final[dict[str, Field]] = {"idempotency_key": Field("Idempotency-Key", "header"), **V1_HEADERS}
# GET /v1/async-jobs/{jobId} documents only Zuora-Track-Id and Zuora-Version.
ASYNC_JOB_HEADERS: Final[dict[str, Field]] = {"zuora_version": V1_HEADERS["zuora_version"], "zuora_track_id": V1_HEADERS["zuora_track_id"]}
# The wallet-domain and web-payment-session routes document Idempotency-Key but no Zuora-Version.
WALLET_HEADERS: Final[dict[str, Field]] = {key: spec for key, spec in IDEMPOTENT_HEADERS.items() if key != "zuora_version"}
# POST /v1/subscriptions/{subscription-key}/preview documents no Zuora-Org-Ids.
PREVIEW_EXISTING_HEADERS: Final[dict[str, Field]] = {key: spec for key, spec in V1_HEADERS.items() if key != "zuora_org_ids"}

ACCOUNT_KEY: Final[dict[str, Field]] = {"account_key": Field("account_key", "str_id", required=True)}
ACCOUNT_NUMBER: Final[dict[str, Field]] = {"account_number": Field("account_number", "str_id", required=True)}
ORDER_NUMBER: Final[dict[str, Field]] = {"order_number": Field("order_number", "str_id", required=True)}
SUBSCRIPTION_KEY: Final[dict[str, Field]] = {"subscription_key": Field("subscription_key", "str_id", required=True)}
SUBSCRIPTION_NUMBER: Final[dict[str, Field]] = {"subscription_number": Field("subscription_number", "str_id", required=True)}
SUBSCRIPTION_VERSION: Final[dict[str, Field]] = {
    **SUBSCRIPTION_KEY,
    "version": Field("version", "str_id", required=True),
}

# Page-numbered v1 lists: ``page`` depends on ``pageSize`` (vendor: "You must set pageSize before specifying page").
PAGE_FIELDS: Final[dict[str, Field]] = {"page": Field("page", "count"), "page_size": Field("pageSize", "count")}
CHARGE_DETAILS: Final[tuple[str, ...]] = ("last-segment", "current-segment", "all-segments", "specific-segment")
SEGMENT_FIELDS: Final[dict[str, Field]] = {
    "charge_detail": Field("charge-detail", "choice", choices=CHARGE_DETAILS),
    "as_of_date": Field("as-of-date", "date"),
    "exclude_rate_plans_with_no_charges": Field("exclude-rate-plans-with-no-charges", "bool"),
    "get_detailed_metrics": Field("getDetailedMetrics", "bool"),
    "as_of_day": Field("asOfDay", "date"),
}
ORDER_LIST_FIELDS: Final[dict[str, Field]] = {
    **PAGE_FIELDS,
    "date_filter_option": Field("dateFilterOption", "choice", choices=("orderDate", "updatedDate")),
    "start_date": Field("startDate", "text"),
    "end_date": Field("endDate", "text"),
    "status": Field("status", "text"),
}

# Keys whose values are payment-instrument secrets anywhere in a request body.
_PAYMENT_SECRET_KEYS: Final[frozenset[str]] = frozenset({"cardnumber", "securitycode", "cardsecuritycode", "bankaccountnumber", "iban", "applepaymentdata", "googlepaymenttoken", "amazonpaytoken", "paymenttoken"})
# Keys that are payment secrets only inside a payment-method or credit-card object
# (``accountNumber`` is otherwise the customer account number, which is not secret).
_INSTRUMENT_SECRET_KEYS: Final[frozenset[str]] = frozenset({"accountnumber", "tokenid", "secondtokenid", "thirdtokenid", "baid", "preapprovalkey"})
_INSTRUMENT_CONTAINERS: Final[frozenset[str]] = frozenset({"paymentmethod", "creditcard"})


def page_check(params: dict[str, Any]) -> None:
    """Refuse ``page`` without ``page_size``, as Zuora documents for its page-numbered lists."""
    if params.get("page") is not None and params.get("page_size") is None:
        raise ValueError("page needs page_size: Zuora pages by pageSize, so set page_size before page")


def segment_check(params: dict[str, Any]) -> None:
    """Mirror the vendor's pairing rules for the subscription read options."""
    specific = params.get("charge_detail") == "specific-segment"
    if specific != (params.get("as_of_date") is not None):
        raise ValueError("charge_detail specific-segment and as_of_date go together: Zuora reads as-of-date only for that segment choice")
    if params.get("as_of_day") is not None and params.get("get_detailed_metrics") is not True:
        raise ValueError("as_of_day applies only with get_detailed_metrics true")


def subscription_read_options(params: dict[str, Any]) -> dict[str, Any]:
    """The documented subscription read options (charge segments, detailed metrics), validated and vendor-keyed."""
    segment_check(params)
    return collect(SEGMENT_FIELDS, params)


def vendor_body(*required: str, optional: bool = False) -> BodyBuilder:
    """``body``: the vendor's JSON request document, forwarded unchanged.

    ``required`` lists the top-level fields the vendor reference marks
    required; a missing one is refused before any vendor call. An ``optional``
    body defaults to ``{}`` so the vendor receives a JSON document either way.
    """

    def build(params: dict[str, Any], _gate: PathGate) -> dict[str, Any]:
        document = object_param(params, "body", required=not optional)
        if document is None:
            return {}
        missing = [name for name in required if document.get(name) is None]
        if missing:
            raise ValueError(f"body is missing Zuora's required field(s): {', '.join(missing)}")
        return document

    return build


def payment_secrets(params: dict[str, Any]) -> list[str]:
    """Card, bank-account and wallet-token values inside ``body``, for redaction."""
    return _instrument_values(params.get("body"), inside=False)


def instrument_secrets(params: dict[str, Any]) -> list[str]:
    """Secret values in a payment-method ``body``, whose top level is itself the instrument."""
    return _instrument_values(params.get("body"), inside=True)


def _instrument_values(value: Any, *, inside: bool) -> list[str]:
    """Walk a JSON-shaped value; ``inside`` is true below a payment-method or credit-card object."""
    if isinstance(value, list):
        return [secret for item in value for secret in _instrument_values(item, inside=inside)]
    if not isinstance(value, dict):
        return []
    return [secret for key, item in value.items() for secret in _entry_values(str(key).lower(), item, inside=inside)]


def _entry_values(folded: str, item: Any, *, inside: bool) -> list[str]:
    if isinstance(item, str):
        return [item] if item and _is_instrument_key(folded, inside=inside) else []
    return _instrument_values(item, inside=inside or folded in _INSTRUMENT_CONTAINERS)


def _is_instrument_key(folded: str, *, inside: bool) -> bool:
    return folded in _PAYMENT_SECRET_KEYS or (inside and folded in _INSTRUMENT_SECRET_KEYS)
