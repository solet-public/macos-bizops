"""Public usage-verb facade, preserving plugin import compatibility."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .allowance_pool_readings import PoolReading
from .session_usage_helpers import (
    _USAGE_FIELDS,
    USAGE_ECONOMICS_PROFILE_PATH,
    UsageVerbError,
    _known_unpriced_tokens,
    _runtime_session_id,
    _serialize_unpriced_categories,
    _stamp,
    _text,
    provider_for_runtime,
)
from .session_usage_pricing import (
    _estimate,
    _pool_payloads,
    _pricing_entry,
    _pricing_for_tuple,
    _profile_provider,
    _unit_as_of,
    read_session_usage_window,
)
from .session_usage_unit import (
    _managed_for_usage,
    _new_usage_stats,
    _prepare_unit_context,
    _receipt_for_managed,
    _record_all_runtime_totals,
    _runtime_for_total,
    _unit_coverage,
    _usage_notes,
)
from .usage_economics_profiles import UsageEconomicsProfileCatalog
from .usage_pricing import executor_mismatch

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


def _tuple_event_as_of(
    tuple_total: Mapping[str, Any],
    totals: Mapping[str, Mapping[str, Any]],
) -> datetime | None:
    return next(
        (stamp for runtime_id in sorted(tuple_total["runtime_session_ids"]) if (stamp := _stamp(totals.get(runtime_id, {}).get("last_event_at"))) is not None),
        None,
    )


def _render_observed_tuple(
    session: Mapping[str, Any],
    tuple_key: tuple[str, str, str, str],
    tuple_total: Mapping[str, Any],
    catalog: UsageEconomicsProfileCatalog | None,
    readings: Sequence[PoolReading],
    now: datetime,
    stats: dict[str, Any],
    advisories: list[str],
) -> tuple[dict[str, Any], bool]:
    provider, runtime, model, effort = tuple_key
    basis, priced, price_warnings = _pricing_for_tuple(
        catalog,
        provider,
        runtime,
        model,
        tuple_total,
        _tuple_event_as_of(tuple_total, stats["totals"]) or now,
    )
    advisories.extend(f"pricing:{item}" for item in price_warnings)
    stats["unpriced_models"].update({model} if basis.startswith("unpriced:") or priced.unpriced_categories else ())
    categories = priced.unpriced_categories
    stats["unpriced_category_count"] += len(categories)
    known_count, lower_bound = _known_unpriced_tokens(categories, model=model)
    stats["unpriced_token_count"] += known_count
    stats["unpriced_token_lower_bound_count"] += int(lower_bound)
    mismatch = executor_mismatch(
        {"model": _text(session.get("model")), "effort": _text(session.get("effort"))},
        {"model": model, "effort": effort},
    )
    pool_ids, pools, pool_warnings = _pool_payloads(catalog, readings, runtime, model, now)
    advisories.extend(f"pool:{item}" for item in pool_warnings)
    usage, _ = _pricing_entry(tuple_total)
    payload = {
        "model": model,
        "effort": effort,
        "usage": {field: getattr(usage, field) for field in _USAGE_FIELDS},
        "priced_usd": priced.usd,
        "pricing_basis": basis,
        "unpriced_categories": _serialize_unpriced_categories(categories),
        "pool_ids": pool_ids,
        "pool_readings": pools,
    }
    return payload, mismatch.mismatch


def _render_unit_sessions(
    managed: Sequence[Mapping[str, Any]],
    ids_by_instance: Mapping[str, list[str]],
    contexts: Mapping[str, Mapping[str, Any] | None],
    totals: Mapping[str, Mapping[str, Any]],
    catalog: UsageEconomicsProfileCatalog | None,
    readings: Sequence[PoolReading],
    now: datetime,
    stats: dict[str, Any],
    advisories: list[str],
) -> tuple[list[dict[str, Any]], int]:
    rendered: list[dict[str, Any]] = []
    workers_without_ids = sum(not ids for ids in ids_by_instance.values())
    stats["totals"] = totals
    for session in managed:
        rendered.append(
            _render_one_unit_session(
                session,
                ids_by_instance,
                contexts,
                totals,
                catalog,
                readings,
                now,
                stats,
                advisories,
            )
        )
    return rendered, workers_without_ids


def _render_one_unit_session(
    session: Mapping[str, Any],
    ids_by_instance: Mapping[str, list[str]],
    contexts: Mapping[str, Mapping[str, Any] | None],
    totals: Mapping[str, Mapping[str, Any]],
    catalog: UsageEconomicsProfileCatalog | None,
    readings: Sequence[PoolReading],
    now: datetime,
    stats: dict[str, Any],
    advisories: list[str],
) -> dict[str, Any]:
    instance_id = _text(session.get("agent_instance_id")) or ""
    runtime_ids = ids_by_instance.get(instance_id, [])
    context = contexts.get(instance_id)
    context_id = _runtime_session_id(context)
    current = context_id in runtime_ids
    current_total = totals.get(context_id) if context_id else None
    runtime = _runtime_for_total(session, current_total or {}, context if current else None)
    provider = _profile_provider(context if current else None, context_id if current else None, runtime, catalog)
    observed, mismatch = _render_session_observed(
        session,
        stats["tuple_totals"].get(instance_id, {}),
        catalog,
        readings,
        now,
        stats,
        advisories,
    )
    if not runtime_ids:
        advisories.append(f"runtime_session_id_unavailable:{instance_id}")
    return {
        "agent_instance_id": instance_id,
        "runtime_session_ids": runtime_ids,
        "provider": provider,
        "runtime": runtime,
        "dispatch_model": _text(session.get("model")),
        "dispatch_effort": _text(session.get("effort")),
        "observed": observed,
        "executor_mismatch": mismatch,
    }


def _render_session_observed(
    session: Mapping[str, Any],
    tuples: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
    catalog: UsageEconomicsProfileCatalog | None,
    readings: Sequence[PoolReading],
    now: datetime,
    stats: dict[str, Any],
    advisories: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    observed: list[dict[str, Any]] = []
    mismatch = False
    for key, total in sorted(tuples.items()):
        payload, differs = _render_observed_tuple(session, key, total, catalog, readings, now, stats, advisories)
        observed.append(payload)
        mismatch |= differs
    return observed, mismatch


def _empty_unit_answer(advisories: Sequence[str]) -> dict[str, Any]:
    return {
        "coverage": "none",
        "sessions": [],
        "estimate": {"usd": None, "basis": "none", "cell": None},
        "difficulty_score": None,
        "as_of": None,
        "lag_seconds": None,
        "notes": {"advisories": list(advisories)},
    }


def _valid_input(params: Mapping[str, Any]) -> tuple[str, str]:
    unit_id = _text(params.get("unit_id"))
    agent_instance_id = _text(params.get("agent_instance_id"))
    if (unit_id is None) == (agent_instance_id is None):
        raise UsageVerbError(
            "parameter_invalid",
            "provide exactly one of unit_id or agent_instance_id",
        )
    return ("unit_id", unit_id) if unit_id is not None else ("agent_instance_id", agent_instance_id or "")


def _assemble_unit_answer(
    state: StateManagementInterface,
    managed: Sequence[Mapping[str, Any]],
    context_data: Mapping[str, Any],
    stats: dict[str, Any],
    now: datetime,
) -> dict[str, Any]:
    ids_by_instance = context_data["ids_by_instance"]
    contexts = context_data["contexts"]
    totals = context_data["totals"]
    ids = context_data["ids"]
    not_found = context_data["not_found"]
    advisories = context_data["advisories"]
    sessions, workers_without_ids = _render_unit_sessions(
        managed,
        ids_by_instance,
        contexts,
        totals,
        context_data["catalog"],
        context_data["readings"],
        now,
        stats,
        advisories,
    )
    coverage = _unit_coverage(
        ids,
        totals,
        not_found,
        stats["incomplete_ids"],
        stats["usable_ids"],
        workers_without_ids,
        context_data["mapping_read_failures"],
    )
    receipt = _receipt_for_managed(managed)
    estimate, difficulty, estimate_warning = _estimate(state, receipt, stats["estimate_observed"], now)
    if estimate_warning:
        advisories.append(estimate_warning)
    as_of, lag = _unit_as_of(tuple(totals.values()), now)
    notes = _usage_notes(
        managed,
        ids,
        totals,
        not_found,
        context_data["causes"],
        context_data["source_counts"],
        stats,
        workers_without_ids,
        advisories,
    )
    return {
        "coverage": coverage,
        "sessions": sessions,
        "estimate": estimate,
        "difficulty_score": difficulty,
        "as_of": as_of.isoformat() if as_of else None,
        "lag_seconds": lag,
        "notes": notes,
    }


def read_unit_usage(
    state: StateManagementInterface | None,
    params: Mapping[str, Any],
    *,
    totals_reader: Callable[..., Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """Join each managed session to the existing ledger totals and pricing."""
    key, identity = _valid_input(params)
    if state is None:
        return _empty_unit_answer(["state_service_unavailable"])
    managed, error = _managed_for_usage(state, key, identity)
    if error:
        return _empty_unit_answer([error])
    managed.sort(key=lambda row: (str(row.get("created_at") or ""), str(row.get("id") or "")))
    if not managed:
        return _empty_unit_answer(["managed_session_not_found"])
    context_data = _prepare_unit_context(state, managed, totals_reader, now)
    stats = _new_usage_stats()
    _record_all_runtime_totals(managed, context_data, stats)
    return _assemble_unit_answer(state, managed, context_data, stats, now)


__all__ = [
    "USAGE_ECONOMICS_PROFILE_PATH",
    "UsageVerbError",
    "provider_for_runtime",
    "read_session_usage_window",
    "read_unit_usage",
]
