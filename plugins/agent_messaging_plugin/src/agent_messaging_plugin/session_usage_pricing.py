"""Usage tuple pricing and the S1 session-window wrapper."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .allowance_pool_readings import PoolReading
from .model_capability_store import CatalogError, read_cells_page
from .session_usage_helpers import (
    _USAGE_FIELDS,
    UsageVerbError,
    _is_number,
    _known_unpriced_tokens,
    _load_catalog,
    _merge_model_totals,
    _merge_usage,
    _metered_profile,
    _pricing_entry,
    _stamp,
    _text,
    _try_read,
    provider_for_runtime,
)
from .usage_economics_profiles import (
    UsageEconomicsProfileCatalog,
)
from .usage_pricing import (
    as_of_and_lag,
    attach_pools,
    choose_basis,
    estimate_for,
    price_usage,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

__all__ = [
    "_estimate",
    "_pool_payloads",
    "_pricing_for_tuple",
    "_profile_provider",
    "_tuple_total",
    "_unit_as_of",
    "read_session_usage_window",
]


def _profile_provider(
    context: Mapping[str, Any] | None,
    current_id: str | None,
    runtime: str,
    catalog: UsageEconomicsProfileCatalog | None,
) -> str:
    if context is not None and current_id is not None and _text(context.get("provider")):
        return str(context["provider"])
    return provider_for_runtime(catalog, runtime) or "unknown"


def _tuple_total(
    totals: dict[tuple[str, str, str, str], dict[str, Any]],
    *,
    provider: str,
    runtime: str,
    model: str,
    effort: str,
    usage_entry: Mapping[str, Any],
    runtime_session_id: str,
) -> dict[str, Any]:
    key = (provider, runtime, model, effort)
    item = totals.setdefault(
        key,
        dict.fromkeys(_USAGE_FIELDS) | {"breakdown": {}, "missing_fields": set(), "runtime_session_ids": set()},
    )
    _merge_usage(item, usage_entry)
    item["runtime_session_ids"].add(runtime_session_id)
    return item


def _pricing_for_tuple(
    catalog: UsageEconomicsProfileCatalog | None,
    provider: str,
    runtime: str,
    model: str,
    usage_entry: Mapping[str, Any],
    as_of: datetime,
) -> tuple[str, Any, list[str]]:
    usage, breakdown = _pricing_entry(usage_entry)
    advisories: list[str] = []
    if model == "unknown":
        basis = "unpriced:model_unknown"
    elif catalog is None:
        basis = "unpriced:catalog_unavailable"
    elif provider == "unknown":
        basis = "unpriced:provider_unknown"
    elif runtime == "unknown":
        basis = "unpriced:runtime_unknown"
    else:
        basis = choose_basis(catalog, provider, runtime, model, as_of)
    profile = _metered_profile(catalog, provider, runtime, model, as_of) if not basis.startswith("unpriced:") else None
    result = price_usage(usage, breakdown, profile, model=model)
    advisories.extend(result.advisories)
    return basis, result, advisories


def _pool_payloads(
    catalog: UsageEconomicsProfileCatalog | None,
    readings: Sequence[PoolReading],
    runtime: str,
    model: str,
    now: datetime,
) -> tuple[list[str], list[dict[str, Any]], list[str]]:
    if catalog is None or runtime == "unknown":
        return [], [], []
    attached = attach_pools(catalog, readings, runtime, model, now)
    pools = [
        {
            "pool_id": pool.pool_id,
            "percent_consumed": pool.percent_consumed,
            "as_of": None if pool.as_of is None else pool.as_of.isoformat(),
            "expired": pool.expired,
        }
        for pool in attached.pools
    ]
    return [pool.pool_id for pool in attached.pools], pools, list(attached.advisories)


def _unit_as_of(
    session_rows: Sequence[Mapping[str, Any]],
    now: datetime,
) -> tuple[datetime | None, float | None]:
    values: list[datetime] = []
    for row in session_rows:
        per_session = as_of_and_lag(_stamp(row.get("last_poll_at")), _stamp(row.get("last_event_at")), now)
        if per_session.as_of is not None:
            values.append(per_session.as_of)
    as_of = min(values) if values else None
    overall = as_of_and_lag(as_of, None, now)
    return overall.as_of, overall.lag_seconds


def _catalog_cells(
    state: StateManagementInterface,
    observed: Sequence[tuple[int | float, tuple[str, str, str]]],
) -> tuple[list[dict[str, Any]], str | None]:
    cells: list[dict[str, Any]] = []
    advisory: str | None = None
    read, error = _try_read(lambda: read_cells_page(state))
    if error is None:
        cells = read.rows
        if read.first_unconsidered is not None:
            advisory = "model_capability_catalog_truncated"
    else:
        code = error.code if isinstance(error, CatalogError) else type(error).__name__
        advisory = f"model_capability_catalog_unavailable:{code}"
    return cells, advisory


def _estimate(
    state: StateManagementInterface,
    receipt_row: Mapping[str, Any] | None,
    observed: Sequence[tuple[int | float, tuple[str, str, str]]],
    now: datetime,
) -> tuple[dict[str, Any], int | float | None, str | None]:
    receipt = receipt_row.get("selection_receipt") if receipt_row is not None else None
    selected_receipt = receipt if isinstance(receipt, Mapping) else None
    difficulty = receipt_row.get("difficulty_score") if receipt_row is not None else None
    difficulty_score = float(difficulty) if _is_number(difficulty) else None
    cells, advisory = _catalog_cells(state, observed) if selected_receipt is None and observed else ([], None)
    estimate = estimate_for(
        selected_receipt,
        [cell for _output, cell in sorted(observed, key=lambda item: item[0], reverse=True)],
        cells,
    )
    return (
        {
            "usd": estimate.usd,
            "basis": estimate.basis,
            "cell": None if estimate.cell is None else list(estimate.cell),
        },
        difficulty_score,
        advisory,
    )


def _window_as_of(response: Mapping[str, Any], now: datetime) -> tuple[datetime, list[str]]:
    top_level = _stamp(response.get("as_of"))
    if top_level is not None:
        return top_level, []
    sessions = response.get("sessions", [])
    values = [bound.as_of for row in sessions if (bound := as_of_and_lag(_stamp(row.get("last_poll_at")), _stamp(row.get("last_event_at")), now)).as_of is not None]
    return (min(values), []) if values else (now, ["pricing_as_of_unavailable:using_call_time"])


def _window_runtime(response: Mapping[str, Any], vendor: str | None) -> str:
    runtimes = {_text(row.get("adapter")) for row in response.get("sessions", [])} - {None}
    if len(runtimes) == 1:
        return next(iter(runtimes)) or "unknown"
    if runtimes:
        return "unknown"
    return vendor or "unknown"


def _window_pricing(
    response: Mapping[str, Any],
    catalog: UsageEconomicsProfileCatalog | None,
    vendor: str | None,
    now: datetime,
) -> tuple[dict[str, dict[str, Any]], int | float, list[str], list[str], list[str]]:
    aggregate: dict[str, dict[str, Any]] = {}
    for row in response.get("sessions", []):
        _merge_model_totals(aggregate, row.get("by_model"))
    runtime = _window_runtime(response, vendor)
    provider = provider_for_runtime(catalog, runtime) or "unknown"
    as_of, advisories = _window_as_of(response, now)
    pricing: dict[str, dict[str, Any]] = {}
    unpriced_count: int | float = 0
    unpriced_models: set[str] = set()
    for model, model_total in sorted(aggregate.items()):
        basis, price_result, price_advisories = _pricing_for_tuple(
            catalog,
            provider,
            runtime,
            model,
            model_total,
            as_of,
        )
        pricing[model] = {"usd": price_result.usd, "basis": basis}
        known_count, lower_bound = _known_unpriced_tokens(
            price_result.unpriced_categories,
            model=model,
        )
        unpriced_count += known_count
        if basis.startswith("unpriced:") or known_count > 0:
            unpriced_models.add(model)
        if lower_bound:
            advisories.append(f"unpriced_token_count_lower_bound:{model}")
        advisories.extend(f"pricing:{item}" for item in price_advisories)
        missing = model_total.get("missing_fields")
        if isinstance(missing, set) and missing:
            advisories.append(f"usage_categories_incomplete:{model}:{','.join(sorted(missing))}")
    return pricing, unpriced_count, sorted(unpriced_models), advisories, sorted(aggregate)


def _validate_window_params(params: Mapping[str, Any]) -> None:
    forbidden = sorted({"runtime_session_ids", "unit_id", "agent_instance_id"} & set(params))
    if forbidden:
        raise UsageVerbError(
            "parameter_invalid",
            f"read_session_usage_window does not accept session identity fields: {', '.join(forbidden)}",
        )
    if "priced" in params and not isinstance(params["priced"], bool):
        raise UsageVerbError("parameter_invalid", "priced must be a boolean")


def _read_window_response(
    params: Mapping[str, Any],
    totals_reader: Callable[..., Any] | None,
) -> dict[str, Any]:
    if totals_reader is None:
        return {
            "status": "unknown",
            "sessions": [],
            "advisories": ["ledger_service_unavailable"],
        }
    window_params = {name: params[name] for name in ("since", "until", "vendor") if name in params}
    response, error = _try_read(lambda: totals_reader(**window_params))
    if isinstance(error, ValueError):
        raise error
    if error is not None:
        return {
            "status": "unknown",
            "sessions": [],
            "advisories": [f"ledger_service_unavailable:{type(error).__name__}"],
        }
    if not isinstance(response, dict):
        return {
            "status": "unknown",
            "sessions": [],
            "advisories": ["ledger_service_unavailable:invalid_response"],
        }
    return response


def _price_window_response(
    response: dict[str, Any],
    params: Mapping[str, Any],
    catalog: UsageEconomicsProfileCatalog | None,
    now: datetime,
) -> dict[str, Any]:
    if catalog is None:
        catalog, catalog_advisory = _load_catalog(now)
    else:
        catalog_advisory = None
    pricing, unpriced_count, unpriced_models, advisories, _models = _window_pricing(
        response,
        catalog,
        _text(params.get("vendor")),
        now,
    )
    if catalog_advisory is not None:
        advisories.append(catalog_advisory)
    return {
        **response,
        "pricing_by_model": pricing,
        "unpriced_token_count": unpriced_count,
        "unpriced_models": unpriced_models,
        "advisories": [*response.get("advisories", []), *advisories],
    }


def read_session_usage_window(
    params: Mapping[str, Any],
    *,
    totals_reader: Callable[..., Any] | None,
    catalog: UsageEconomicsProfileCatalog | None,
    now: datetime,
) -> dict[str, Any]:
    """Wrap one S1 window read; raw mode is an exact S1 pass-through."""
    _validate_window_params(params)
    response = _read_window_response(params, totals_reader)
    return _price_window_response(response, params, catalog, now) if params.get("priced", False) is True else response
