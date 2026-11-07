"""Pure pricing and allowance projections for observed session usage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from agent_messaging_plugin.allowance_pool_readings import PoolReading
from agent_messaging_plugin.allowance_pools import AllowancePool
from agent_messaging_plugin.usage_economics import UsageVector
from agent_messaging_plugin.usage_economics_profiles import (
    MeteredApiProfile,
    UsageEconomicsProfileCatalog,
    UsageEconomicsProfileValidationError,
)
from agent_messaging_plugin.usage_economics_quota import plan_for_runtime


@dataclass(frozen=True)
class UnpricedCategory:
    name: str
    count: int | float | None


@dataclass(frozen=True)
class PriceResult:
    usd: float | None
    unpriced_categories: tuple[UnpricedCategory, ...]
    advisories: tuple[str, ...] = ()
    reason: str | None = None


@dataclass(frozen=True)
class PoolAttachment:
    pool_id: str
    profile_id: str
    percent_consumed: float | None
    as_of: datetime | None
    expired: bool
    next_reset_at: datetime | None
    reading_status: str


@dataclass(frozen=True)
class PoolAttachments:
    pools: tuple[PoolAttachment, ...]
    advisories: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExecutorMismatch:
    mismatch: bool
    model_mismatch: bool
    effort_mismatch: bool
    unknown_model_count: int
    unknown_effort_count: int


@dataclass(frozen=True)
class Estimate:
    usd: float | None
    basis: str
    cell: tuple[str, str, str] | None


@dataclass(frozen=True)
class AsOfAndLag:
    as_of: datetime | None
    lag_seconds: float | None


def _count(value: object) -> int | float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return value
    return None


def usage_from_ledger(entry: Mapping[str, Any]) -> UsageVector:
    """Copy S1's usage categories and breakdown while preserving unknowns."""
    return UsageVector(
        input_tokens=cast("int | None", _count(entry.get("input_tokens"))),
        cached_input_tokens=cast("int | None", _count(entry.get("cached_input_tokens"))),
        cache_write_input_tokens=cast("int | None", _count(entry.get("cache_write_input_tokens"))),
        output_tokens=cast("int | None", _count(entry.get("output_tokens"))),
        reasoning_output_tokens=cast("int | None", _count(entry.get("reasoning_output_tokens"))),
        tool_calls=cast("int | None", _count(entry.get("tool_calls"))),
    )


def _metered_profiles(
    catalog: UsageEconomicsProfileCatalog,
    provider: str,
    runtime: str,
    model: str,
    as_of: datetime,
) -> list[MeteredApiProfile]:
    return sorted(
        (profile for profile in catalog.profiles if isinstance(profile, MeteredApiProfile) and profile.provider == provider and profile.runtime == runtime and profile.model == model and profile.refresh_status == "current" and not profile.fixture_only and profile.effective_at <= as_of),
        key=lambda profile: (profile.effective_at, profile.profile_id),
        reverse=True,
    )


def choose_basis(
    catalog: UsageEconomicsProfileCatalog,
    provider: str,
    runtime: str,
    model: str,
    as_of: datetime,
) -> str:
    """Choose a list-equivalent or metered profile without refusing gaps."""
    if model == "unknown":
        return "unpriced:model_unknown"
    try:
        plan = plan_for_runtime(catalog, runtime)
    except UsageEconomicsProfileValidationError:
        return "unpriced:ambiguous_flat_rate_plan"
    metered = _metered_profiles(catalog, provider, runtime, model, as_of)
    if plan is not None and any(profile.provider == provider for profile in metered):
        return f"list_equivalent:{plan.profile_id}"
    if metered:
        return f"metered:{metered[0].profile_id}"
    return "unpriced:no_metered_profile_for_model"


def _add_token_category(
    name: str,
    amount: int | float | None,
    price: float | None,
    costs: list[float],
    categories: list[UnpricedCategory],
) -> None:
    if amount is None or price is None:
        categories.append(UnpricedCategory(name, amount))
    else:
        costs.append(float(amount) * price / 1_000_000)


def _price_output(
    usage: UsageVector,
    profile: MeteredApiProfile | None,
    costs: list[float],
    categories: list[UnpricedCategory],
    advisories: list[str],
) -> None:
    output = usage.output_tokens
    reasoning = usage.reasoning_output_tokens
    output_price = profile.output_per_mtok if profile else None
    reasoning_price = profile.reasoning_output_per_mtok if profile else None
    if output is None:
        categories.append(UnpricedCategory("output_tokens", None))
        if reasoning is None or reasoning_price is None:
            categories.append(UnpricedCategory("reasoning_output_tokens", reasoning))
        else:
            costs.append(float(reasoning) * reasoning_price / 1_000_000)
    elif reasoning is None:
        _add_token_category("output_tokens", output, output_price, costs, categories)
        categories.append(UnpricedCategory("reasoning_output_tokens", None))
    elif reasoning > output:
        advisories.append("reasoning_exceeds_output")
        _add_token_category("output_tokens", output, output_price, costs, categories)
    else:
        _add_token_category("output_tokens", output - reasoning, output_price, costs, categories)
        _price_reasoning(reasoning, reasoning_price, output_price, costs, categories, advisories)


def _price_reasoning(
    reasoning: int | float,
    reasoning_price: float | None,
    output_price: float | None,
    costs: list[float],
    categories: list[UnpricedCategory],
    advisories: list[str],
) -> None:
    if reasoning_price is not None:
        costs.append(float(reasoning) * reasoning_price / 1_000_000)
    elif output_price is not None:
        costs.append(float(reasoning) * output_price / 1_000_000)
        advisories.append("reasoning_priced_as_output")
    else:
        categories.append(UnpricedCategory("reasoning_output_tokens", reasoning))


def _price_tool_calls(
    usage: UsageVector,
    profile: MeteredApiProfile | None,
    costs: list[float],
    categories: list[UnpricedCategory],
) -> None:
    amount = usage.tool_calls
    price = profile.tool_call_per_1k if profile else None
    if amount is None or price is None:
        categories.append(UnpricedCategory("tool_calls", amount))
    else:
        costs.append(float(amount) * price / 1_000)


def _unpriced_dimensions(breakdown: Mapping[str, Any], categories: list[UnpricedCategory]) -> None:
    for ttl in ("cache_write_1h_input_tokens", "cache_write_5m_input_tokens"):
        amount = _count(breakdown.get(ttl))
        if amount is not None and amount > 0:
            categories.append(UnpricedCategory(f"{ttl}_ttl_premium", amount))
    speeds = breakdown.get("speed")
    if isinstance(speeds, Mapping):
        for speed, turns in sorted(speeds.items(), key=lambda item: str(item[0])):
            count = _count(turns)
            if str(speed) != "standard" and count is not None:
                categories.append(UnpricedCategory(f"speed:{speed}", count))


def price_usage(
    usage: UsageVector,
    breakdown: Mapping[str, Any],
    profile: MeteredApiProfile | None,
    *,
    model: str | None = None,
) -> PriceResult:
    """Price reported token categories, treating reasoning as output detail."""
    if model == "unknown":
        names = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "tool_calls",
        )
        unpriced = [UnpricedCategory(name, _count(getattr(usage, name))) for name in names]
        _unpriced_dimensions(breakdown, unpriced)
        return PriceResult(None, tuple(unpriced), reason="model_unknown")
    categories: list[UnpricedCategory] = []
    advisories: list[str] = []
    costs: list[float] = []
    _add_token_category("input_tokens", usage.input_tokens, profile.input_per_mtok if profile else None, costs, categories)
    _add_token_category(
        "cached_input_tokens",
        usage.cached_input_tokens,
        profile.cached_input_per_mtok if profile else None,
        costs,
        categories,
    )
    _add_token_category(
        "cache_write_input_tokens",
        usage.cache_write_input_tokens,
        profile.cache_write_per_mtok if profile else None,
        costs,
        categories,
    )
    _price_output(usage, profile, costs, categories, advisories)
    _price_tool_calls(usage, profile, costs, categories)
    _unpriced_dimensions(breakdown, categories)
    reason = None if profile is not None else "no_metered_profile_for_model"
    return PriceResult(sum(costs) if costs else None, tuple(categories), tuple(advisories), reason)


def attach_pools(
    catalog: UsageEconomicsProfileCatalog,
    readings: Sequence[PoolReading],
    runtime: str,
    model: str,
    now: datetime,
) -> PoolAttachments:
    """Attach every applicable plan pool to its latest matching reading."""
    if model == "unknown":
        return PoolAttachments(())
    try:
        plan = plan_for_runtime(catalog, runtime)
    except UsageEconomicsProfileValidationError:
        return PoolAttachments((), ("ambiguous_flat_rate_plan",))
    if plan is None:
        return PoolAttachments(())
    attached = tuple(_pool_attachment(plan.profile_id, pool, readings, now) for pool in plan.allowance_pools if pool.applies(runtime=runtime, model=model))
    return PoolAttachments(attached)


def _pool_attachment(
    profile_id: str,
    pool: AllowancePool,
    readings: Sequence[PoolReading],
    now: datetime,
) -> PoolAttachment:
    latest = _latest_pool_reading(profile_id, pool.pool_id, readings)
    if latest is None:
        return PoolAttachment(pool.pool_id, profile_id, None, None, False, pool.next_reset_at, "unread")
    percent = _percent_consumed(pool, latest)
    expired = not latest.is_current(now)
    return PoolAttachment(
        pool.pool_id,
        profile_id,
        percent,
        latest.as_of,
        expired,
        latest.next_reset_at,
        "expired" if expired else "current",
    )


def _latest_pool_reading(
    profile_id: str,
    pool_id: str,
    readings: Sequence[PoolReading],
) -> PoolReading | None:
    matches = [row for row in readings if row.profile_id == profile_id and row.pool_id == pool_id]
    return max(matches, key=lambda row: (row.as_of, row.recorded_at)) if matches else None


def _percent_consumed(pool: AllowancePool, reading: PoolReading) -> float | None:
    denominator = pool.limit or reading.consumed + reading.remaining
    return None if denominator <= 0 else reading.consumed / denominator * 100


def executor_mismatch(dispatch: Mapping[str, Any], observed: Mapping[str, Any]) -> ExecutorMismatch:
    """Compare observed executor identity while preserving unknown values."""
    selected = dispatch.get("selected")
    expected = selected if isinstance(selected, Mapping) else dispatch
    observed_model = observed.get("model")
    observed_effort = observed.get("effort")
    unknown_model = observed_model == "unknown"
    unknown_effort = observed_effort == "unknown"
    model_mismatch = isinstance(observed_model, str) and not unknown_model and observed_model != expected.get("model")
    effort_mismatch = isinstance(observed_effort, str) and not unknown_effort and observed_effort != expected.get("effort")
    return ExecutorMismatch(
        mismatch=model_mismatch or effort_mismatch,
        model_mismatch=model_mismatch,
        effort_mismatch=effort_mismatch,
        unknown_model_count=int(unknown_model),
        unknown_effort_count=int(unknown_effort),
    )


def _selected_cell(receipt: Mapping[str, Any]) -> tuple[str, str, str] | None:
    selected = receipt.get("selected")
    if not isinstance(selected, Mapping):
        return None
    runtime, model, effort = selected.get("runtime"), selected.get("model"), selected.get("effort")
    if all(isinstance(value, str) for value in (runtime, model, effort)):
        return cast("tuple[str, str, str]", (runtime, model, effort))
    return None


def _cell_cost(row: Mapping[str, Any], cell: tuple[str, str, str]) -> float | None:
    if (row.get("runtime"), row.get("model"), row.get("effort")) != cell:
        return None
    if row.get("acceptance") != "accepted":
        return None
    cost = _count(row.get("cost_per_task_usd"))
    return None if cost is None else float(cost)


def estimate_for(
    selection_receipt: Mapping[str, Any] | None,
    observed_tuples: Sequence[Mapping[str, Any] | tuple[str, str, str]],
    cells: Sequence[Mapping[str, Any]],
) -> Estimate:
    """Use a selector cell estimate or look up the observed tuple in the catalog."""
    if selection_receipt is not None:
        return _receipt_estimate(selection_receipt)

    for observed in observed_tuples:
        cell = _observed_cell(observed)
        if cell is None:
            continue
        estimate = _catalog_estimate(cell, cells)
        if estimate.usd is not None:
            return estimate
    return Estimate(None, "none", None)


def _receipt_estimate(receipt: Mapping[str, Any]) -> Estimate:
    cell = _selected_cell(receipt)
    if cell is None:
        return Estimate(None, "none", None)
    selected = receipt.get("selected")
    selected_cost = _count(selected.get("cost_per_task_usd")) if isinstance(selected, Mapping) else None
    receipt_cost = selected_cost if selected_cost is not None else _count(receipt.get("cost_per_task_usd"))
    return Estimate(None if receipt_cost is None else float(receipt_cost), "receipt", cell)


def _observed_cell(observed: Mapping[str, Any] | tuple[str, str, str]) -> tuple[str, str, str] | None:
    if isinstance(observed, Mapping):
        return _selected_cell({"selected": observed})
    return observed


def _catalog_estimate(cell: tuple[str, str, str], cells: Sequence[Mapping[str, Any]]) -> Estimate:
    cost = next((value for row in cells if (value := _cell_cost(row, cell)) is not None), None)
    return Estimate(cost, "catalog_lookup", cell) if cost is not None else Estimate(None, "none", cell)


def as_of_and_lag(
    last_poll_at: datetime | None,
    last_event_at: datetime | None,
    now: datetime,
) -> AsOfAndLag:
    """Use the older observed bound so lag never overstates freshness."""
    values = [value for value in (last_poll_at, last_event_at) if value is not None]
    as_of = min(values) if values else None
    return AsOfAndLag(as_of, None if as_of is None else (now - as_of).total_seconds())


__all__ = [
    "AsOfAndLag",
    "Estimate",
    "ExecutorMismatch",
    "PoolAttachment",
    "PoolAttachments",
    "PriceResult",
    "UnpricedCategory",
    "as_of_and_lag",
    "attach_pools",
    "choose_basis",
    "estimate_for",
    "executor_mismatch",
    "price_usage",
    "usage_from_ledger",
]
