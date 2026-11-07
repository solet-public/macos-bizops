"""Parameter metadata shared by the typed v1 ``@platform_process`` declarations (Orders through Payments).

Mirrors :mod:`commerce_fields`: the same caller names, documented once so the
three process modules and their knowledge-base documents describe them alike.
"""

from __future__ import annotations

from typing import Final

from ananta.core.actions.action_metadata import ContextHandling, MergeErrorProcessorCustomizations, ParameterMetadata, ParameterType
from ananta.core.domain.enums import ProcessorPolicyCategory

QUEUED_NOTE: Final[str] = " Returns job_id/status queued at once; dispatch is not completion."
READ: Final[MergeErrorProcessorCustomizations] = MergeErrorProcessorCustomizations(retryable=True)
WRITE: Final[MergeErrorProcessorCustomizations] = MergeErrorProcessorCustomizations(retryable=False)
EDGE: Final[ProcessorPolicyCategory] = ProcessorPolicyCategory.EDGE
NONE: Final[ContextHandling] = ContextHandling.NONE


def _text(description: str, *, required: bool = False) -> ParameterMetadata:
    return ParameterMetadata(type=ParameterType.STRING, required=required, description=description)


def _flag(description: str) -> ParameterMetadata:
    return ParameterMetadata(type=ParameterType.BOOLEAN, required=False, description=description)


V1_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "zuora_version": _text("Zuora-Version header: the minor API version, e.g. 211.0; it changes the response shape."),
    "zuora_entity_ids": _text("Zuora-Entity-Ids header: the entity to act in (Multi-entity tenants)."),
    "zuora_org_ids": _text("Zuora-Org-Ids header: comma-separated org ids (Multi-Org tenants)."),
    "zuora_track_id": _text("Zuora-Track-Id header: a US-ASCII trace id without : ; \" or '; Zuora echoes it."),
}
IDEMPOTENT_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "idempotency_key": _text("Idempotency-Key header: repeat the same key to make a retried create safe."),
    **V1_HEADER_PARAMS,
}
ASYNC_JOB_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = {key: V1_HEADER_PARAMS[key] for key in ("zuora_version", "zuora_track_id")}
WALLET_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = {key: spec for key, spec in IDEMPOTENT_HEADER_PARAMS.items() if key != "zuora_version"}
PREVIEW_EXISTING_HEADER_PARAMS: Final[dict[str, ParameterMetadata]] = {key: spec for key, spec in V1_HEADER_PARAMS.items() if key != "zuora_org_ids"}


def text(description: str, *, required: bool = False) -> ParameterMetadata:
    """A string parameter."""
    return _text(description, required=required)


def flag(description: str) -> ParameterMetadata:
    """An optional boolean parameter."""
    return _flag(description)


def integer(description: str, *, required: bool = False) -> ParameterMetadata:
    """An integer parameter."""
    return ParameterMetadata(type=ParameterType.INTEGER, required=required, description=description)


def number(description: str) -> ParameterMetadata:
    """An optional number parameter."""
    return ParameterMetadata(type=ParameterType.FLOAT, required=False, description=description)


def strings(description: str) -> ParameterMetadata:
    """An optional list of strings."""
    return ParameterMetadata(type=ParameterType.LIST, required=False, description=description)


def body(description: str, *, required: bool = True) -> ParameterMetadata:
    """The vendor's JSON request document, forwarded unchanged."""
    return ParameterMetadata(type=ParameterType.OBJECT, required=required, description=description)


ACCOUNT_KEY: Final[ParameterMetadata] = _text("Account number or account id.", required=True)
ACCOUNT_NUMBER: Final[ParameterMetadata] = _text("Account number.", required=True)
ORDER_NUMBER: Final[ParameterMetadata] = _text("Order number.", required=True)
SUBSCRIPTION_KEY: Final[ParameterMetadata] = _text("Subscription number or subscription id.", required=True)
SUBSCRIPTION_NUMBER: Final[ParameterMetadata] = _text("Subscription number.", required=True)
VERSION: Final[ParameterMetadata] = _text("Subscription version number.", required=True)

PAGE_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "page": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Page number, from 1; needs page_size."),
    "page_size": ParameterMetadata(type=ParameterType.INTEGER, required=False, description="Records per page."),
}
ORDER_FILTER_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "date_filter_option": _text("orderDate (default) or updatedDate: the date start_date/end_date filter on."),
    "start_date": _text("Only orders dated on or after this date."),
    "end_date": _text("Only orders dated on or before this date."),
}
ORDER_STATUS: Final[ParameterMetadata] = _text("Only orders in this status.")
SEGMENT_PARAMS: Final[dict[str, ParameterMetadata]] = {
    "charge_detail": _text("last-segment (default), current-segment, all-segments or specific-segment (needs as_of_date)."),
    "as_of_date": _text("YYYY-MM-DD date for charge_detail specific-segment."),
    "exclude_rate_plans_with_no_charges": _flag("Leave out charges removed before the last segment."),
    "get_detailed_metrics": _flag("Include contracted and as-of-day MRR and TCV metrics."),
    "as_of_day": _text("YYYY-MM-DD date for the detailed metrics; needs get_detailed_metrics true."),
}
