"""Model capability catalog verbs (iss_48ea8171): seed, refresh, read, select, record, withdraw, reinstate, history.

Validation layer between the plugin's ``@platform_process`` methods and the
store + pure selector. Every refusal is a ``CatalogError`` or
``TierSelectionError`` with a stable code; the plugin method maps it to a
failure envelope unchanged. Only ``refresh_model_capability_catalog`` touches the
network and the host's harness caches; every other verb reads or writes state.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, cast

import httpx
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_records

from .allowance_pool_readings import (
    read_allowance_pool_readings,
    record_allowance_pool_reading,
    retract_allowance_pool_reading,
)
from .dispatch_tier_selection import (
    EFFORT_ORDER,
    BillingObjective,
    CapabilityCell,
    Constraints,
    Pin,
    StaleReading,
    TierSelection,
    TierSelectionError,
    select_tier,
)
from .model_capability_fetch import DiscoveryRun, discover_readings
from .model_capability_harness import HarnessContext, run_process
from .model_capability_harness_pass import apply_harness_pass
from .model_capability_reconcile import catalog_models_by_runtime
from .model_capability_store import (
    CATALOG_PAGE_SIZE,
    CATALOG_READ_CEILING,
    HISTORY_DEFAULT_LIMIT,
    KNOWN_RUNTIMES,
    CatalogError,
    CatalogWindow,
    CellRead,
    CellTransition,
    ManualCell,
    close_refresh_run,
    default_billing_objective,
    load_seed_table,
    open_refresh_run,
    read_cell_history,
    read_cells_page,
    reconcile_discovery,
    record_manual_cell,
    reinstate_cells,
    resolve_catalog_window,
    seed_catalog,
    selection_catalog,
    withdraw_cells,
)
from .model_dispatch_policy import (
    AppliedFloor,
    DispatchPolicyError,
    applied_capability_floors,
    load_dispatch_policy,
    validate_dispatch_kind,
)
from .schema import CELL_ACCEPTANCE_ACCEPTED, TABLE_MODEL_CAPABILITY_REFRESH_RUN
from .usage_economics_profiles import (
    FlatRateQuotaProfile,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)
from .usage_economics_quota import plan_for_runtime

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_OBJECTIVES: frozenset[str] = frozenset({"metered_usd", "relative", "allowance_weighted"})
_REFRESH_PROCESS_KEY: Final[str] = "plugin::agent_messaging_plugin::refresh_model_capability_catalog"
_STALE_SELECTION_TRIGGER: Final[str] = "selector_stale"
_REFRESH_RUN_TRIGGERS: Final[frozenset[str]] = frozenset({"cron", "policy_change", _STALE_SELECTION_TRIGGER})
_REFRESH_REQUEST_DEDUPE: Final[timedelta] = timedelta(hours=12)
"""How long after a refresh run started the selector asks for no further one.

Deliberately long: a refresh can fail (the detail-source parse_error,
iss_6cb8fe86), and a failing refresh must not be re-requested on every
selection. The stored staleness window is 72h, so a stale catalog is still
re-requested several times a day."""
_USAGE_ECONOMICS_PROFILE_PATH = Path(__file__).resolve().parents[2] / "model_profiles" / "usage_economics.v1.json"


def seed_model_capability_catalog(state: StateManagementInterface, *, staleness_window_hours: int | None) -> dict[str, Any]:
    """Load the declarative seed and write it as pending cells (never accepted)."""
    seed = load_seed_table()
    if staleness_window_hours is None:
        return seed_catalog(state, seed=seed)
    return seed_catalog(state, seed=seed, staleness_window_hours=staleness_window_hours)


_VALID_TRIGGERS: frozenset[str] = frozenset({"manual", "cron", "policy_change", _STALE_SELECTION_TRIGGER})


_CODEX_BINARY_UNCONFIGURED: Final[Path] = Path("<codex binary not configured>")
"""Stands in for a Codex binary nobody supplied. It is not absolute, so the Codex reader refuses to run it
(a binary is never looked up on PATH), reads the cache file as a DEGRADED list, and says so."""


def _discover_or_close(
    state: StateManagementInterface,
    run_id: str,
    client: httpx.Client | None,
    context: HarnessContext,
    known_models: Mapping[str, Sequence[str]],
) -> DiscoveryRun:
    """Run discovery over `client` (a real one when None); a fault closes the run row before it propagates."""
    active_client = client or httpx.Client()
    try:
        return discover_readings(active_client, context, catalog_models=known_models)
    except Exception as exc:  # noqa: BLE001 -- a refresh run must close cleanly even on an unanticipated fetch-layer fault
        close_refresh_run(
            state,
            run_id,
            counts={"cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": 0},
            sources_ok=[],
            sources_failed=[f"unexpected: {exc}"],
            note=f"refresh aborted before reconciliation: {exc}",
        )
        raise CatalogError("refresh_fetch_failed", f"model capability refresh could not fetch: {exc}") from exc
    finally:
        if client is None:
            active_client.close()


def refresh_model_capability_catalog(
    state: StateManagementInterface,
    *,
    trigger: str,
    client: httpx.Client | None = None,
    codex_binary: Path | None = None,
    home: Path | None = None,
) -> dict[str, Any]:
    """iss_d136ae29, iss_0e805222, iss_52fdb8e5 -- the discovery refresh: learn which models
    exist from the harness lists (the launching Codex binary's own listing, Claude Code's own
    cache), take score and cost from the Artificial Analysis leaderboard and each model's detail
    page, reconcile, and write. This is the only verb that can move a cell to `accepted`;
    `seed_model_capability_catalog` never does.

    `codex_binary` is the absolute path of the binary that launches Codex lanes; None means it
    was not supplied, and the Codex list is then read from Codex's cache file as a degraded list
    with a loud advisory (never a refusal, never a PATH lookup). `home` is the directory the
    harness caches live under (default: the process's home).

    After discovery and before the reconcile it runs the harness pass: a cell whose model a
    current, readable harness list stops listing starts a clock, is withdrawn automatically after
    one staleness window, and is reinstated when the model is listed again; a withdrawal made by
    a person is never reversed (see `model_capability_harness_pass`).

    A source that fails to fetch is recorded on the run's `sources_failed` column, never silently
    dropped -- and never aborts the whole run: one bad URL for one model+effort must not blank out
    every other cell's otherwise-good readings.

    `client` is an injection point for tests (an `httpx.Client` built on a
    `MockTransport`) -- production callers never pass it and get a real one.
    """
    if trigger not in _VALID_TRIGGERS:
        raise CatalogError("parameter_invalid", f"trigger must be one of {sorted(_VALID_TRIGGERS)}; got {trigger!r}.")
    run_id = open_refresh_run(state, trigger=trigger)
    known_models = catalog_models_by_runtime(read_cells_page(state, ceiling=None).rows)  # a state fault here propagates as itself, not as a fetch fault
    context = HarnessContext(home=home or Path.home(), codex_binary=codex_binary or _CODEX_BINARY_UNCONFIGURED, run=run_process, now=datetime.now(UTC))
    run = _discover_or_close(state, run_id, client, context, known_models)
    sources_failed = [f"{source_id}: {reason}" for source_id, reason in run.failures]
    # The harness pass runs first, so the reconcile below sees a relisted model as accepted again and a withdrawn one as withdrawn.
    harness_pass = apply_harness_pass(state, run_id, run, read_cells_page(state, ceiling=None).rows, now=context.now)
    report = reconcile_discovery(state, run_id, run, now=context.now)
    note = "; ".join(part for part in (report.note, harness_pass.note()) if part) or None
    close_refresh_run(state, run_id, counts=report.counts, sources_ok=list(run.sources_ok), sources_failed=sources_failed, note=note)
    return {
        "run_id": run_id,
        "trigger": trigger,
        "readings_gathered": len(run.readings),
        "sources_ok": list(run.sources_ok),
        "sources_failed": sources_failed,
        **report.counts,
        **report.result_fields,
        **harness_pass.result_fields(),
        "advisories": [*report.result_fields["advisories"], *harness_pass.advisories],
    }


def _required_text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("parameter_invalid", f"{field} is required and must be a non-blank string.")
    return raw.strip()


def _optional_number(raw: object, field: str) -> float | None:
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("parameter_invalid", f"{field} must be a number.")
    return float(raw)


def record_model_capability_cell(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Operator ruling rul_9e7a67ba: anyone with better information records one
    catalog cell directly, as accepted, with the source it came from."""
    score = _optional_number(params.get("capability_score"), "capability_score")
    if score is None:
        raise CatalogError("parameter_invalid", "capability_score is required.")
    note_raw = params.get("note")
    if note_raw is not None and not isinstance(note_raw, str):
        raise CatalogError("parameter_invalid", "note must be a string when given.")
    window_raw = params.get("staleness_window_hours")
    if window_raw not in (None, "") and (isinstance(window_raw, bool) or not isinstance(window_raw, int)):
        raise CatalogError("parameter_invalid", "staleness_window_hours must be an integer.")
    cell = ManualCell(
        runtime=_required_text(params.get("runtime"), "runtime"),
        model=_required_text(params.get("model"), "model"),
        effort=_required_text(params.get("effort"), "effort"),
        capability_score=score,
        cost_per_task_usd=_optional_number(params.get("cost_per_task_usd"), "cost_per_task_usd"),
        source_ref=_required_text(params.get("source_ref"), "source_ref"),
        note=(note_raw.strip() or None) if isinstance(note_raw, str) else None,
    )
    if window_raw in (None, ""):
        return record_manual_cell(state, cell)
    return record_manual_cell(state, cell, staleness_window_hours=window_raw)


def _transition_request(params: dict[str, Any]) -> CellTransition:
    """The caller's lifecycle request; effort omitted or blank means every effort of the model."""
    effort_raw = params.get("effort")
    return CellTransition(
        runtime=_required_text(params.get("runtime"), "runtime"),
        model=_required_text(params.get("model"), "model"),
        effort=None if effort_raw is None or effort_raw == "" else _required_text(effort_raw, "effort"),
        reason=_required_text(params.get("reason"), "reason"),
        expected_acceptance=_required_text(params.get("expected_acceptance"), "expected_acceptance"),
        actor=_required_text(params.get("actor"), "actor"),
    )


def withdraw_model_capability_cell(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Operator ruling rul_2bff72b8: retire a catalog cell (or every effort of a model) from dispatch.

    The score, cost and measured_at are kept, so `reinstate_model_capability_cell`
    can reopen the cell; the selector excludes it as `not_accepted`. Refused with
    `stale_cell_state` when the cell no longer holds the caller's `expected_acceptance`.
    """
    return withdraw_cells(state, _transition_request(params))


def reinstate_model_capability_cell(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Operator ruling rul_2bff72b8: reopen a withdrawn catalog cell as accepted with its retained values.

    Refused with `parameter_invalid` when the cell kept no score (record it instead)
    and with `stale_cell_state` when it is no longer withdrawn. The answer says
    whether the next refresh would withdraw it again.
    """
    return reinstate_cells(state, _transition_request(params))


def _history_limit(raw: object) -> int:
    if raw is None or raw == "":
        return HISTORY_DEFAULT_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= CATALOG_PAGE_SIZE:
        raise CatalogError("parameter_invalid", f"limit must be an integer between 1 and {CATALOG_PAGE_SIZE}.")
    return raw


def _history_cursor(raw: object) -> list[object] | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, list) or len(raw) != 2 or any(not isinstance(item, str) or not item for item in raw):
        raise CatalogError("parameter_invalid", "after must be the next_after list [fetched_at, id] of the previous page.")
    return list(raw)


def read_model_capability_history(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Operator ruling rul_2bff72b8: the append-only history of one cell, oldest first, a page at a time."""
    return read_cell_history(
        state,
        runtime=_required_text(params.get("runtime"), "runtime"),
        model=_required_text(params.get("model"), "model"),
        effort=_required_text(params.get("effort"), "effort"),
        limit=_history_limit(params.get("limit")),
        after=_history_cursor(params.get("after")),
    )


def _is_fresh(row: dict[str, Any], *, now: datetime, max_age: timedelta | None) -> bool | None:
    measured = row.get("measured_at")
    if max_age is None or not isinstance(measured, str):
        return None
    try:
        measured_at = datetime.fromisoformat(measured.replace("Z", "+00:00"))
    except ValueError:
        return None
    return measured_at.tzinfo is not None and now - measured_at <= max_age


def read_model_capability_catalog(
    state: StateManagementInterface,
    *,
    runtime: str | None,
    model: str | None,
    include_unaccepted: bool,
    now: datetime,
) -> dict[str, Any]:
    """The catalog as stored, each row annotated with whether dispatch may use it now.

    An accepted row is servable whatever its age (operator ruling rul_becb4bed);
    ``is_fresh`` only reports whether it is inside its window.
    """
    read = read_cells_page(state, runtime=runtime, model=model)
    window = resolve_catalog_window(state, read.rows)
    annotated: list[dict[str, Any]] = []
    for row in read.rows:
        accepted = row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED
        if not accepted and not include_unaccepted:
            continue
        annotated.append({**row, "enabled": row.get("enabled", True), "is_fresh": _is_fresh(row, now=now, max_age=window.max_age()), "servable": accepted and row.get("enabled", True)})
    result: dict[str, Any] = {
        "cells": annotated,
        "total_cells": len(read.rows),
        "servable_cells": sum(1 for row in annotated if row["servable"]),
        "as_of": now.isoformat(),
        "freshness_window": asdict(window),
    }
    warnings = [*window.advisories, *_truncation_warnings(read)]
    if warnings:
        result["warnings"] = warnings
    return result


def _truncation_warnings(read: CellRead) -> list[str]:
    """The notice for a catalog read that stopped at the ceiling; the answer still runs over the rows read."""
    if read.first_unconsidered is None:
        return []
    named = "/".join(read.first_unconsidered)
    return [f"catalog read truncated at {CATALOG_READ_CEILING} rows; the first row not considered is {named}, so cells from there on are missing from this answer."]


def _plan_for(runtime: str | None, *, now: datetime) -> FlatRateQuotaProfile | None:
    """The current flat-rate plan covering the runtime constraint; none without a constraint or a covering plan."""
    if runtime is None:
        return None
    try:
        return plan_for_runtime(load_usage_economics_profile_catalog(_USAGE_ECONOMICS_PROFILE_PATH, as_of=now), runtime)
    except UsageEconomicsProfileValidationError as exc:
        raise CatalogError("usage_economics_invalid", f"usage economics profiles unreadable: {exc}") from exc


def _plan_weight_fn(plan: FlatRateQuotaProfile | None) -> Callable[[str, str], float] | None:
    """The plan's declared weight per (runtime, model); cells outside the plan's runtimes weigh 1.0."""
    if plan is None or plan.dispatch_weights is None:
        return None
    weights = plan.dispatch_weights
    return lambda runtime, model: weights.weight_for(model) if runtime in plan.included_runtimes else 1.0


def _objective(raw: object, *, plan: FlatRateQuotaProfile | None, now: datetime) -> BillingObjective:
    weighted = plan is not None and plan.dispatch_weights is not None
    if raw is None or raw == "":
        return "allowance_weighted" if weighted else default_billing_objective(None, now=now)
    if not isinstance(raw, str) or raw not in _OBJECTIVES:
        raise CatalogError("parameter_invalid", f"billing_objective must be one of {sorted(_OBJECTIVES)}; got {raw!r}.")
    if raw == "allowance_weighted" and not weighted:
        raise CatalogError(
            "parameter_invalid",
            "allowance_weighted is plan-scoped: name a runtime covered by a current flat-rate plan that declares dispatch_weights.",
        )
    return cast(BillingObjective, raw)


def _selection_warnings(raw_objective: object, plan: FlatRateQuotaProfile | None) -> dict[str, list[str]]:
    """``{"warnings": [...]}`` when a plan covers the runtime but declares no weights and the caller took the default ranking."""
    if plan is None or plan.dispatch_weights is not None or raw_objective not in (None, ""):
        return {}
    return {"warnings": [f"plan {plan.profile_id} declares no dispatch_weights; ranking equals metered dollars."]}


def _number(raw: object, field: str, *, default: float) -> float:
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("parameter_invalid", f"{field} must be a number.")
    return float(raw)


def _capability_floor_pairs(
    dispatch_kind: str | None,
    scope_tags: tuple[str, ...],
) -> tuple[frozenset[tuple[str, str]] | None, str | None, tuple[AppliedFloor, ...]]:
    """Capability-floor pairs only; dispatch kind is provenance, never a filter."""
    if dispatch_kind is None and not scope_tags:
        return None, None, ()
    policy = load_dispatch_policy()
    if dispatch_kind is not None:
        validate_dispatch_kind(dispatch_kind)
    allowed: frozenset[tuple[str, str]] | None = None
    floors = applied_capability_floors(policy, scope_tags=scope_tags, brief_text="")
    for floor in floors:
        floor_pairs = frozenset(policy.capability_floors[floor.tag].floor_pairs)
        allowed = floor_pairs if allowed is None else allowed & floor_pairs
    return allowed, policy.policy_version, floors


def _window(state: StateManagementInterface, cells: list[dict[str, Any]], max_staleness_hours: object) -> CatalogWindow:
    requested = None if max_staleness_hours == "" else max_staleness_hours
    return resolve_catalog_window(state, cells, requested=requested)


def _cell_payload(cell: CapabilityCell, *, weighted: bool) -> dict[str, Any]:
    payload = asdict(cell)
    payload["measured_at"] = cell.measured_at.isoformat()
    if not weighted:
        del payload["plan_weight"]
    return payload


def _cost_basis(plan: FlatRateQuotaProfile, selected: CapabilityCell) -> dict[str, Any]:
    """The declared weight table behind an allowance_weighted answer, for the audit trail."""
    weights = plan.dispatch_weights
    if weights is None:
        raise CatalogError("catalog_invalid", f"plan {plan.profile_id} lost its dispatch_weights mid-selection.")
    return {
        "plan_profile_id": plan.profile_id,
        "profile_version": plan.profile_version,
        "plan_id": plan.plan_id,
        "basis": weights.basis,
        "ruling_id": weights.ruling_id,
        "evidence_ref": weights.evidence_ref,
        "declared_at": weights.declared_at.isoformat(),
        "default_weight": weights.default_weight,
        "by_model_prefix": dict(weights.by_model_prefix),
        "selected_weight": selected.plan_weight,
        "note": weights.note,
    }


def _stale_reading_payload(reading: StaleReading) -> dict[str, Any]:
    payload = asdict(reading)
    payload["measured_at"] = reading.measured_at.isoformat()
    return payload


def _matching_refresh_run(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((dict(row) for row in rows if row.get("trigger") in _REFRESH_RUN_TRIGGERS and row.get("sources_ok") != ["operator_policy"]), None)


def _recent_refresh_run(state: StateManagementInterface, *, now: datetime) -> tuple[dict[str, Any] | None, list[str]]:
    """The newest refresh run, in flight or finished, that started inside the dedupe window.

    Seed runs and direct cell recordings are not discovery refreshes and do not count.
    """
    try:
        rows = require_records(
            state.query_ordered(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_MODEL_CAPABILITY_REFRESH_RUN,
                    "filters": {"started_at": {"op": "gte", "value": (now - _REFRESH_REQUEST_DEDUPE).isoformat()}},
                    "order_by": [["started_at", "desc"], ["id", "desc"]],
                    "limit": 50,
                },
            ),
        )
        recent = _matching_refresh_run(rows)
    except Exception as exc:  # noqa: BLE001 — only the optional advisory history lookup
        logger.warning("refresh history unavailable; retaining catalog refresh request: %s", exc, exc_info=True)
        return None, [f"refresh history unavailable ({exc}); no recent refresh could be confirmed."]
    return recent, []


def _refresh_advice(
    state: StateManagementInterface,
    selection: TierSelection,
    *,
    now: datetime,
) -> tuple[dict[str, Any] | None, list[str]]:
    """The operator notice and, when none is recent, the refresh request for a selection made from stale readings.

    The request is a returned action the plugin wrapper submits; this read
    path writes nothing. A refresh run started inside ``_REFRESH_REQUEST_DEDUPE``
    suppresses the request, whatever its outcome, so a failing refresh is not
    re-requested on every selection.
    """
    stale = selection.stale_readings
    if not stale:
        return None, []
    oldest = max(reading.age_hours for reading in stale)
    window = stale[0].window_hours
    summary = f"{len(stale)} feasible reading(s) are older than the {window:g}h window (oldest {oldest:g}h); the selection used them."
    recent, history_warnings = _recent_refresh_run(state, now=now)
    if recent is not None:
        return None, [
            f"{summary} No refresh was requested: refresh run {recent.get('id')} started {recent.get('started_at')} ({recent.get('status')}) and may not have renewed the catalog. If readings stay stale, tell the operator and file an issue.",
        ]
    request = {
        "process_key": _REFRESH_PROCESS_KEY,
        "arguments": {"trigger": _STALE_SELECTION_TRIGGER},
    }
    return request, [
        *history_warnings,
        f"{summary} A catalog refresh was requested; the catalog may stay stale if it fails. If readings are still stale after it, tell the operator and file an issue.",
    ]


def _pin_payload(selection: TierSelection, pin_basis: str | None) -> dict[str, Any]:
    return {
        "selection_mode": "cheapest" if selection.pin is None else "pinned",
        "pin_basis": pin_basis,
        "pin_cost_delta": selection.pin_cost_delta,
        "pin_overrides_constraints": selection.pin_overrides_constraints,
        "pin_below_threshold": selection.pin_below_threshold,
    }


def _pin_receipt(selection: TierSelection, pin_basis: str | None) -> dict[str, Any]:
    return {"pin": None if selection.pin is None else asdict(selection.pin), "pin_basis": pin_basis}


def _selection_payload(
    selection: TierSelection,
    *,
    run_id: str | None,
    score_margin: float,
    cost_tolerance: float,
    max_staleness_hours: int | None,
    runtime_constraint: str | None,
    constraints: Constraints,
    pin_basis: str | None,
    plan: FlatRateQuotaProfile | None,
) -> dict[str, Any]:
    weighted = selection.objective == "allowance_weighted"
    payload = {
        "selected": _cell_payload(selection.selected, weighted=weighted),
        **_pin_payload(selection, pin_basis),
        "billing_objective": selection.objective,
        "required_score": selection.required_score,
        "effective_required_score": selection.effective_required_score,
        "frontier": [_cell_payload(cell, weighted=weighted) for cell in selection.frontier],
        "dominated": [{"weaker": _cell_payload(weaker, weighted=weighted), "stronger": _cell_payload(stronger, weighted=weighted)} for weaker, stronger in selection.dominated],
        "ladder": [asdict(step) for step in selection.ladder],
        "excluded": dict(selection.excluded),
        "advisories": dict(selection.advisories),
        "stale_readings": [_stale_reading_payload(reading) for reading in selection.stale_readings],
        "catalog_run_id": run_id,
    }
    if weighted and plan is not None:
        payload["cost_basis"] = _cost_basis(plan, selection.selected)
    payload["selection_receipt"] = {
        "required_score": selection.required_score,
        "billing_objective": selection.objective,
        "score_margin": score_margin,
        "cost_tolerance": cost_tolerance,
        "max_staleness_hours": max_staleness_hours,
        "runtime_constraint": runtime_constraint,
        **_constraint_payload(constraints),
        **_pin_receipt(selection, pin_basis),
        "selected": {
            "runtime": selection.selected.runtime,
            "model": selection.selected.model,
            "effort": selection.selected.effort,
        },
    }
    return payload


def _required_score(params: dict[str, Any]) -> float:
    raw = params.get("required_score")
    if raw is None or raw == "":
        raise CatalogError("parameter_invalid", "required_score is required.")
    return _number(raw, "required_score", default=0.0)


def _runtime_constraint(raw: object) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str) or raw not in KNOWN_RUNTIMES:
        raise CatalogError("parameter_invalid", f"runtime must be one of {list(KNOWN_RUNTIMES)}; got {raw!r}.")
    return raw


def _constraint_strings(params: dict[str, Any], name: str) -> tuple[str, ...]:
    raw = params.get(name, [])
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise CatalogError("parameter_invalid", f"{name} must be a list of non-empty strings.")
    return tuple(raw)


def _constraints(params: dict[str, Any]) -> Constraints:
    ceiling = params.get("effort_ceiling")
    if ceiling is not None and (not isinstance(ceiling, str) or ceiling not in EFFORT_ORDER):
        raise CatalogError("parameter_invalid", f"effort_ceiling must be null or one of {list(EFFORT_ORDER)}.")
    return Constraints(ceiling, _constraint_strings(params, "exclude_model_prefixes"), _constraint_strings(params, "exclude_runtimes"))


def _constraint_payload(constraints: Constraints) -> dict[str, Any]:
    return {
        "effort_ceiling": constraints.effort_ceiling,
        "exclude_model_prefixes": list(constraints.exclude_model_prefixes),
        "exclude_runtimes": list(constraints.exclude_runtimes),
    }


def _pin_inputs(params: dict[str, Any], runtime: str | None) -> tuple[Pin | None, str | None]:
    raw = params.get("pin")
    basis = params.get("pin_basis")
    if raw is None:
        if basis is not None:
            raise CatalogError("parameter_invalid", "pin_basis requires pin.")
        return None, None
    pin = _parse_pin(raw)
    if not isinstance(basis, str) or not basis.strip():
        raise CatalogError("parameter_invalid", "pin requires nonempty pin_basis provenance.")
    if runtime is not None and runtime != pin.runtime:
        raise CatalogError("parameter_invalid", "explicit runtime must match pin.runtime.")
    return pin, basis


def _parse_pin(raw: object) -> Pin:
    if not isinstance(raw, dict) or set(raw) != {"runtime", "model", "effort"}:
        raise CatalogError("parameter_invalid", "pin must identify exactly runtime, model and effort.")
    if any(not isinstance(value, str) or not value.strip() for value in raw.values()) or raw["effort"] not in EFFORT_ORDER:
        raise CatalogError("parameter_invalid", "pin needs three nonempty strings and a known effort.")
    return Pin(raw["runtime"], raw["model"], raw["effort"])


def _canonical_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    required = {"required_score", "billing_objective", "score_margin", "cost_tolerance", "max_staleness_hours", "runtime_constraint", "selected"}
    defaults = {**_constraint_payload(Constraints()), "pin": None, "pin_basis": None}
    if not required <= receipt.keys() or receipt.keys() - required - defaults.keys():
        raise CatalogError("selection_receipt_invalid", "selection_receipt has missing or unexpected fields.")
    return {**defaults, **receipt}


def _unscored_pin(rows: list[dict[str, Any]], pin: Pin | None) -> bool:
    if pin is None:
        return False
    requested = asdict(pin)
    return any({key: row.get(key) for key in requested} == requested and row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED and row.get("capability_score") is None for row in rows)


def _pin_error_message(message: str, code: str, unscored_pin: bool) -> str:
    if unscored_pin and code == "pin_cell_unavailable":
        return f"Requested accepted tuple has no capability score. {message}"
    return message


def _select_within(
    catalog: tuple[CapabilityCell, ...],
    runtime: str | None,
    unscored_pin: bool = False,
    **kwargs: Any,
) -> TierSelection:
    """Run the pure selector over only ``runtime``'s cells, filtered before domination and choice.

    A refusal names the constrained runtime.
    """
    candidates = catalog if runtime is None else tuple(cell for cell in catalog if cell.runtime == runtime)
    try:
        return select_tier(candidates, **kwargs)
    except TierSelectionError as exc:
        message = exc.message if runtime is None else f"runtime={runtime}: {exc.message}"
        message = _pin_error_message(message, exc.code, unscored_pin)
        raise TierSelectionError(exc.code, message) from exc


def _pin_warnings(selection: TierSelection) -> list[str]:
    return [f"{key}: accepted pin overrides this exclusion." for key in ("pin_floor_disallowed",) if selection.advisories.get(key)]


def _attach_notices(
    payload: dict[str, Any],
    state: StateManagementInterface,
    selection: TierSelection,
    *,
    plan_warnings: list[str],
    now: datetime,
) -> None:
    """Add the operator notices (``warnings``) and, for a stale selection with no recent refresh, the ``refresh_request``."""
    refresh_request, stale_notice = _refresh_advice(state, selection, now=now)
    warnings = [*plan_warnings, *stale_notice]
    warnings.extend(_pin_warnings(selection))
    if warnings:
        payload["warnings"] = warnings
    if refresh_request is not None:
        payload["refresh_request"] = refresh_request


def select_dispatch_tier(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Cheapest accepted, policy-allowed (model, effort) clearing the required score, within ``runtime`` when given.

    A reading past its staleness window is used, not excluded: the answer lists
    the stale readings with their ages and, when no refresh run started recently,
    carries a ``refresh_request`` action for the plugin wrapper to submit. The
    ``selection_receipt`` holds neither, so an advisory selection still verifies.
    """
    moment = now or datetime.now(UTC)
    required_score = _required_score(params)
    dispatch_kind = params.get("dispatch_kind")
    if dispatch_kind is not None and not isinstance(dispatch_kind, str):
        raise DispatchPolicyError("dispatch_policy_violation", f"unknown dispatch_kind {dispatch_kind!r}.")
    floor_pairs, policy_version, floors = _capability_floor_pairs(
        dispatch_kind,
        _scope_tags(params.get("scope_tags")),
    )
    runtime = _runtime_constraint(params.get("runtime"))
    constraints = _constraints(params)
    pin, pin_basis = _pin_inputs(params, runtime)
    read = read_cells_page(state)
    rows = read.rows
    plan = _plan_for(runtime, now=moment)
    catalog = selection_catalog(rows, _plan_weight_fn(plan))
    objective = _objective(params.get("billing_objective"), plan=plan, now=moment)
    score_margin = _number(params.get("score_margin"), "score_margin", default=0.0)
    cost_tolerance = _number(params.get("cost_tolerance"), "cost_tolerance", default=0.05)
    raw_max_staleness = params.get("max_staleness_hours")
    max_staleness_hours = None if raw_max_staleness is None or raw_max_staleness == "" else raw_max_staleness
    window = _window(state, rows, params.get("max_staleness_hours"))
    selection = _select_within(
        catalog,
        runtime,
        unscored_pin=_unscored_pin(rows, pin),
        required_score=required_score,
        objective=objective,
        now=moment,
        max_age=window.max_age(),
        capability_floor_pairs=floor_pairs,
        score_margin=score_margin,
        cost_tolerance=cost_tolerance,
        constraints=constraints,
        pin=pin,
    )
    run_ids = {str(row.get("last_refresh_run_id")) for row in rows if row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED}
    payload = _selection_payload(
        selection,
        run_id=max(run_ids) if run_ids else None,
        score_margin=score_margin,
        cost_tolerance=cost_tolerance,
        max_staleness_hours=max_staleness_hours,
        runtime_constraint=runtime,
        constraints=constraints,
        pin_basis=pin_basis,
        plan=plan,
    )
    _attach_notices(
        payload,
        state,
        selection,
        plan_warnings=[
            *_selection_warnings(params.get("billing_objective"), plan).get("warnings", []),
            *_truncation_warnings(read),
            *window.advisories,
        ],
        now=moment,
    )
    payload["freshness_window"] = asdict(window)
    payload["policy_version"] = policy_version
    payload["capability_floors"] = [asdict(floor) for floor in floors]
    payload["unscored_cells"] = len(rows) - len(catalog)
    return payload


def record_pool_reading(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Record one explicit allowance-pool reading against the shipped usage profile."""
    return record_allowance_pool_reading(state, params, profile_path=_USAGE_ECONOMICS_PROFILE_PATH, now=now or datetime.now(UTC))


def read_pool_readings_view(state: StateManagementInterface, params: dict[str, Any], *, now: datetime | None = None) -> dict[str, Any]:
    """Recorded allowance-pool readings, each marked current or expired."""
    return read_allowance_pool_readings(state, params, now=now or datetime.now(UTC))


def retract_pool_reading(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Delete one recorded allowance-pool reading."""
    return retract_allowance_pool_reading(state, params)


def _receipt_runtime_constraint(selection_receipt: dict[str, Any], *, agent_runtime: str, model: str, effort: str) -> object:
    """Check the receipt's selected tuple against the spawn tuple; return its runtime constraint."""
    selected = selection_receipt.get("selected")
    if not isinstance(selected, dict) or set(selected) != {"runtime", "model", "effort"}:
        raise CatalogError("selection_receipt_invalid", "selection_receipt.selected must identify runtime, model, and effort.")
    if selected != {"runtime": agent_runtime, "model": model, "effort": effort}:
        raise CatalogError("selection_receipt_mismatch", "spawn tuple differs from selection_receipt.selected.")
    constraint = selection_receipt["runtime_constraint"]
    if constraint is not None and constraint != agent_runtime:
        raise CatalogError("selection_receipt_mismatch", "spawn runtime differs from selection_receipt.runtime_constraint.")
    return constraint


def verify_selection_receipt(
    state: StateManagementInterface,
    *,
    difficulty_score: object,
    selection_receipt: object,
    dispatch_kind: str,
    scope_tags: tuple[str, ...],
    agent_runtime: str,
    model: str,
    effort: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replay the selector and prove the requested spawn matches its exact selection."""
    if isinstance(difficulty_score, bool) or not isinstance(difficulty_score, (int, float)):
        raise CatalogError("difficulty_score_required", "spawn_session requires a numeric difficulty_score.")
    if not isinstance(selection_receipt, dict):
        raise CatalogError("selection_receipt_required", "spawn_session requires a selector-issued selection_receipt.")
    canonical = _canonical_receipt(selection_receipt)
    if selection_receipt.get("required_score") != float(difficulty_score):
        raise CatalogError("selection_receipt_mismatch", "selection_receipt.required_score must equal difficulty_score.")
    constraint = _receipt_runtime_constraint(selection_receipt, agent_runtime=agent_runtime, model=model, effort=effort)
    if selection_receipt["billing_objective"] == "allowance_weighted" and constraint is None:
        raise CatalogError("selection_receipt_mismatch", "an allowance_weighted selection_receipt must carry its runtime_constraint.")
    actual = select_dispatch_tier(
        state,
        {
            "required_score": difficulty_score,
            "dispatch_kind": dispatch_kind,
            "scope_tags": list(scope_tags),
            "billing_objective": selection_receipt["billing_objective"],
            "score_margin": selection_receipt["score_margin"],
            "cost_tolerance": selection_receipt["cost_tolerance"],
            "max_staleness_hours": selection_receipt["max_staleness_hours"],
            "runtime": constraint,
            "effort_ceiling": canonical["effort_ceiling"],
            "exclude_model_prefixes": canonical["exclude_model_prefixes"],
            "exclude_runtimes": canonical["exclude_runtimes"],
            "pin": canonical["pin"],
            "pin_basis": canonical["pin_basis"],
        },
        now=now,
    )
    if _canonical_receipt(actual["selection_receipt"]) != canonical:
        raise CatalogError("selection_receipt_mismatch", "selection_receipt is not the current selector answer.")
    return selection_receipt


def _scope_tags(raw: object) -> tuple[str, ...]:
    if raw is None or raw == "":
        return ()
    if not isinstance(raw, (list, tuple)) or any(not isinstance(item, str) or not item for item in raw):
        raise CatalogError("parameter_invalid", "scope_tags must be a list of non-empty strings.")
    return tuple(raw)


__all__ = [
    "read_model_capability_catalog",
    "read_model_capability_history",
    "read_pool_readings_view",
    "record_model_capability_cell",
    "record_pool_reading",
    "refresh_model_capability_catalog",
    "reinstate_model_capability_cell",
    "retract_pool_reading",
    "seed_model_capability_catalog",
    "select_dispatch_tier",
    "verify_selection_receipt",
    "withdraw_model_capability_cell",
]
