"""Runtime-id joins and ledger-quality aggregation for unit usage."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from .allowance_pool_readings import read_pool_readings
from .model_capability_store import CatalogError
from .session_lifecycle_store import list_managed_sessions
from .session_usage_helpers import (
    _is_number,
    _load_catalog,
    _managed_runtime,
    _response_advisories,
    _runtime_session_id,
    _session_ids,
    _session_total_rows,
    _text,
    _try_read,
    _usage_dict,
)
from .session_usage_pricing import _profile_provider, _tuple_total
from .usage_economics_profiles import UsageEconomicsProfileCatalog
from .usage_pricing import executor_mismatch

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

__all__ = [
    "_new_usage_stats",
    "_prepare_unit_context",
    "_record_all_runtime_totals",
    "_runtime_for_total",
    "_managed_for_usage",
    "_receipt_for_managed",
    "_unit_coverage",
    "_usage_notes",
]


def _collect_runtime_ids(
    state: StateManagementInterface,
    managed: Sequence[Mapping[str, Any]],
) -> tuple[
    dict[str, list[str]],
    dict[str, Mapping[str, Any] | None],
    dict[str, int],
    set[str],
    list[str],
    set[str],
]:
    contexts: dict[str, Mapping[str, Any] | None] = {}
    ids_by_instance: dict[str, list[str]] = {}
    source_counts = {"session_claude_mapping": 0, "session_context_status": 0}
    all_ids: set[str] = set()
    advisories: list[str] = []
    mapping_read_failures: set[str] = set()
    for session in managed:
        instance_id = _text(session.get("agent_instance_id")) or ""
        ids, context, counts, warnings, mapping_read_failed = _session_ids(state, session)
        contexts[instance_id], ids_by_instance[instance_id] = context, ids
        all_ids.update(ids)
        advisories.extend(warnings)
        if mapping_read_failed:
            mapping_read_failures.add(instance_id)
        for source, count in counts.items():
            source_counts[source] += count
    return ids_by_instance, contexts, source_counts, all_ids, advisories, mapping_read_failures


def _read_runtime_totals(
    runtime_ids: set[str],
    totals_reader: Callable[..., Any] | None,
) -> tuple[dict[str, Mapping[str, Any]], set[str], dict[str, set[str]], list[str]]:
    totals: dict[str, Mapping[str, Any]] = {}
    missing: set[str] = set()
    causes: dict[str, set[str]] = {}
    advisories: list[str] = []
    if totals_reader is None:
        return (
            totals,
            set(runtime_ids),
            {rid: {"ledger_service_unavailable"} for rid in runtime_ids},
            ["ledger_service_unavailable"],
        )
    ordered = sorted(runtime_ids)
    for start in range(0, len(ordered), 200):
        chunk = ordered[start : start + 200]
        response, error = _try_read(lambda runtime_ids=chunk: totals_reader(runtime_session_ids=runtime_ids))
        reason = f"ledger_usage_unavailable:{type(error).__name__}" if error else None
        if reason is None and not isinstance(response, Mapping):
            reason = "ledger_usage_unavailable:invalid_response"
        if reason is not None:
            advisories.append(reason)
            missing.update(chunk)
            for rid in chunk:
                causes.setdefault(rid, set()).add(reason)
            continue
        totals.update(_session_total_rows(response))
        missing.update(response.get("not_found", []))
        advisories.extend(_response_advisories(response))
    return totals, missing, causes, advisories


def _mark_runtime_missing(
    runtime_id: str,
    not_found: set[str],
    incomplete: set[str],
    causes: dict[str, set[str]],
) -> None:
    incomplete.add(runtime_id)
    causes.setdefault(runtime_id, set()).add(
        "not_found" if runtime_id in not_found else "missing_from_totals_response",
    )


def _mark_meter_status(
    runtime_id: str,
    meter: str,
    status: str,
    has_usage: bool,
    measured_zero: bool,
    stats: dict[str, Any],
    causes: dict[str, set[str]],
    advisories: list[str],
) -> None:
    incomplete = stats["incomplete_ids"]
    if meter == "supported" and (has_usage or measured_zero):
        stats["usable_ids"].add(runtime_id)
    if meter != "supported":
        stats["unsupported_count"] += 1
        incomplete.add(runtime_id)
        causes.setdefault(runtime_id, set()).add("meter_unsupported")
        advisories.append(f"meter_unsupported:{runtime_id}")
    if status != "complete":
        stats["partial_count"] += 1
        incomplete.add(runtime_id)
        causes.setdefault(runtime_id, set()).add(f"status:{status}")
        advisories.append(f"usage_incomplete:{runtime_id}:{status}")
    if meter == "supported" and not has_usage and not measured_zero:
        incomplete.add(runtime_id)
        causes.setdefault(runtime_id, set()).add("usage_missing_for_supported_meter")
        advisories.append(f"usage_missing_for_supported_meter:{runtime_id}")


def _record_unknown_model_events(total: Mapping[str, Any], stats: dict[str, Any]) -> None:
    for item in _response_advisories(total):
        if item.startswith("model_unavailable:"):
            try:
                stats["unknown_model_events"] += int(item.split(":", 1)[1].split(" ", 1)[0])
            except ValueError:
                stats["unknown_model_events"] += 1


def _record_runtime_quality(
    runtime_id: str,
    total: Mapping[str, Any],
    stats: dict[str, Any],
    causes: dict[str, set[str]],
    advisories: list[str],
) -> Mapping[str, Any]:
    meter = _text(total.get("meter")) or "unsupported"
    status = _text(total.get("status")) or "unknown"
    by_model = total.get("by_model", {})
    has_usage = bool(by_model)
    event_count = total.get("usage_event_count")
    measured_zero = meter == "supported" and _is_number(event_count) and event_count == 0
    _mark_meter_status(runtime_id, meter, status, has_usage, measured_zero, stats, causes, advisories)
    _record_unknown_model_events(total, stats)
    return by_model


def _runtime_attribution(
    session: Mapping[str, Any],
    runtime_id: str,
    total: Mapping[str, Any],
    context: Mapping[str, Any] | None,
    context_id: str | None,
    catalog: UsageEconomicsProfileCatalog | None,
    stats: dict[str, Any],
) -> tuple[str, str, str]:
    current = runtime_id == context_id
    effort = _text(context.get("effort")) if current and context else None
    runtime = _runtime_for_total(session, total, context if current else None)
    stats["historical_effort_count"] += int(not current and context_id is not None)
    stats["effort_unknown_count"] += int(effort is None)
    effort = effort or "unknown"
    provider = _profile_provider(context if current else None, runtime_id if current else None, runtime, catalog)
    stats["unknown_provider_count"] += int(provider == "unknown")
    return provider, runtime, effort


def _runtime_for_total(
    session: Mapping[str, Any],
    total: Mapping[str, Any],
    context: Mapping[str, Any] | None,
) -> str:
    return (_text(context.get("runtime")) if context else None) or _text(total.get("adapter")) or _managed_runtime(session)


def _record_observed_models(
    session: Mapping[str, Any],
    runtime_id: str,
    provider: str,
    runtime: str,
    effort: str,
    by_model: Mapping[str, Any],
    stats: dict[str, Any],
) -> None:
    instance_id = _text(session.get("agent_instance_id")) or ""
    tuples = stats["tuple_totals"].setdefault(instance_id, {})
    dispatch = {"model": _text(session.get("model")), "effort": _text(session.get("effort"))}
    for model, usage_entry in by_model.items():
        stats["unknown_model_count"] += int(model == "unknown")
        mismatch = executor_mismatch(dispatch, {"model": model, "effort": effort})
        stats["unknown_effort_tuple_count"] += mismatch.unknown_effort_count
        _tuple_total(
            tuples,
            provider=provider,
            runtime=runtime,
            model=model,
            effort=effort,
            usage_entry=usage_entry,
            runtime_session_id=runtime_id,
        )
        output_tokens = _usage_dict(usage_entry).get("output_tokens")
        if _is_number(output_tokens):
            stats["estimate_observed"].append((output_tokens, (runtime, model, effort)))


def _record_runtime_total(
    session: Mapping[str, Any],
    runtime_id: str,
    context: Mapping[str, Any] | None,
    total: Mapping[str, Any] | None,
    not_found: set[str],
    catalog: UsageEconomicsProfileCatalog | None,
    stats: dict[str, Any],
    causes: dict[str, set[str]],
    advisories: list[str],
) -> None:
    if total is None or runtime_id in not_found:
        _mark_runtime_missing(runtime_id, not_found, stats["incomplete_ids"], causes)
        return
    stats["sibling_rows_resolved"] += int(total.get("sibling_rows_resolved") or 0)
    by_model = _record_runtime_quality(runtime_id, total, stats, causes, advisories)
    context_id = _runtime_session_id(context)
    provider, runtime, effort = _runtime_attribution(session, runtime_id, total, context, context_id, catalog, stats)
    _record_observed_models(session, runtime_id, provider, runtime, effort, by_model, stats)


def _new_usage_stats() -> dict[str, Any]:
    return {
        "incomplete_ids": set(),
        "usable_ids": set(),
        "unsupported_count": 0,
        "partial_count": 0,
        "effort_unknown_count": 0,
        "historical_effort_count": 0,
        "unknown_model_count": 0,
        "unknown_model_events": 0,
        "unknown_provider_count": 0,
        "unknown_effort_tuple_count": 0,
        "sibling_rows_resolved": 0,
        "tuple_totals": {},
        "estimate_observed": [],
        "unpriced_models": set(),
        "unpriced_category_count": 0,
        "unpriced_token_count": 0,
        "unpriced_token_lower_bound_count": 0,
    }


def _prepare_unit_context(
    state: StateManagementInterface,
    managed: Sequence[Mapping[str, Any]],
    totals_reader: Callable[..., Any] | None,
    now: datetime,
) -> dict[str, Any]:
    catalog, catalog_warning = _load_catalog(now)
    readings, error = _try_read(lambda: read_pool_readings(state))
    advisories = [catalog_warning] if catalog_warning else []
    if error:
        code = error.code if isinstance(error, CatalogError) else type(error).__name__
        advisories.append(f"pool_readings_unavailable:{code}")
    (
        ids_by_instance,
        contexts,
        source_counts,
        ids,
        join_warnings,
        mapping_read_failures,
    ) = _collect_runtime_ids(state, managed)
    totals, not_found, causes, ledger_warnings = _read_runtime_totals(ids, totals_reader)
    advisories.extend(join_warnings)
    advisories.extend(ledger_warnings)
    return {
        "catalog": catalog,
        "readings": readings or (),
        "advisories": advisories,
        "ids_by_instance": ids_by_instance,
        "contexts": contexts,
        "source_counts": source_counts,
        "mapping_read_failures": mapping_read_failures,
        "ids": ids,
        "totals": totals,
        "not_found": not_found,
        "causes": causes,
    }


def _record_all_runtime_totals(
    managed: Sequence[Mapping[str, Any]],
    context_data: Mapping[str, Any],
    stats: dict[str, Any],
) -> None:
    for session in managed:
        instance_id = _text(session.get("agent_instance_id")) or ""
        context = context_data["contexts"].get(instance_id)
        for runtime_id in context_data["ids_by_instance"].get(instance_id, []):
            _record_runtime_total(
                session,
                runtime_id,
                context,
                context_data["totals"].get(runtime_id),
                context_data["not_found"],
                context_data["catalog"],
                stats,
                context_data["causes"],
                context_data["advisories"],
            )


def _usage_notes(
    managed: Sequence[Mapping[str, Any]],
    ids: set[str],
    totals: Mapping[str, Mapping[str, Any]],
    not_found: set[str],
    causes: dict[str, set[str]],
    source_counts: dict[str, int],
    stats: dict[str, Any],
    workers_without_ids: int,
    advisories: list[str],
) -> dict[str, Any]:
    missing = {rid for rid in ids if rid in not_found or rid not in totals}
    unknown_events = stats["unknown_model_events"] or stats["unknown_model_count"]
    incomplete = stats["incomplete_ids"]
    return {
        "managed_session_count": len(managed),
        "workers_without_runtime_ids": workers_without_ids,
        "runtime_session_id_count": len(ids),
        "runtime_session_ids_from": source_counts,
        "runtime_session_ids_resolved": sum(rid in totals for rid in ids),
        "runtime_session_ids_not_found": len(missing),
        "runtime_session_ids_missing_values": sorted(missing),
        "runtime_session_ids_incomplete": len(incomplete),
        "runtime_session_ids_incomplete_values": sorted(incomplete),
        "runtime_session_id_causes": {rid: sorted(reasons) for rid, reasons in sorted(causes.items()) if reasons},
        "unsupported_meter_count": stats["unsupported_count"],
        "partial_or_bounded_totals_count": stats["partial_count"],
        "effort_unknown_sessions": stats["effort_unknown_count"],
        "historical_effort_unavailable": stats["historical_effort_count"],
        "unknown_model_count": stats["unknown_model_count"],
        "unknown_model_events": unknown_events,
        "unknown_provider_count": stats["unknown_provider_count"],
        "unknown_effort_tuple_count": stats["unknown_effort_tuple_count"],
        "unpriced_models": sorted(stats["unpriced_models"]),
        "unpriced_category_count": stats["unpriced_category_count"],
        "unpriced_token_count": stats["unpriced_token_count"],
        "unpriced_token_lower_bound_count": stats["unpriced_token_lower_bound_count"],
        "sibling_rows_resolved": stats["sibling_rows_resolved"],
        "advisories": sorted(set(advisories)),
    }


def _managed_for_usage(
    state: StateManagementInterface,
    key: str,
    identity: str,
) -> tuple[list[dict[str, Any]], str | None]:
    column = "unit_id" if key == "unit_id" else "agent_instance_id"
    rows, error = _try_read(lambda: list_managed_sessions(state, {column: identity}))
    return rows or [], f"managed_session_read_unavailable:{type(error).__name__}" if error else None


def _unit_coverage(
    ids: set[str],
    totals: Mapping[str, Mapping[str, Any]],
    not_found: set[str],
    incomplete: set[str],
    usable_ids: set[str],
    workers_without_ids: int,
    mapping_read_failures: set[str],
) -> str:
    if not usable_ids:
        return "none"
    if incomplete or workers_without_ids or mapping_read_failures:
        return "partial"
    return "partial" if _has_missing_runtime_totals(ids, totals, not_found) else "full"


def _has_missing_runtime_totals(
    ids: set[str],
    totals: Mapping[str, Mapping[str, Any]],
    not_found: set[str],
) -> bool:
    return any(runtime_id in not_found or runtime_id not in totals for runtime_id in ids)


def _receipt_for_managed(managed: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next(
        (row for row in managed if isinstance(row.get("selection_receipt"), Mapping) and row["selection_receipt"]),
        None,
    )
