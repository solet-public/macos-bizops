"""Cost-aware (model, effort) tier selection — the pure decision core.

Design of record: register issue iss_48ea8171, unit unt_4a6526fd (the
design document is that unit's deliverable). This module owns no I/O: the catalog it
selects over is loaded by the store layer, and the verb layer wraps the
result. Keeping the decision pure is what lets the smoke prove, against the
operator's own hand-computed numbers, that the algorithm reproduces them.

The operator's heuristic — effort has sharply diminishing returns inside one
model, and a stronger model at its base effort frequently beats a weaker
model's top effort on capability AND cost — is what a human uses to
approximate the cheapest clearing cell WITHOUT a complete table. With a
complete, fresh table the exact answer is a filtered argmin over cost, and
that argmin already prefers Opus-5 low over Sonnet-5 max because it is
cheaper. What the heuristic still contributes here is (1) the explanation —
every answer carries the per-model effort ladder with marginal
points-per-cost-unit so the diminishing returns are visible, and every
dominated cell is named — and (2) the near-tie rule: when two feasible cells
cost within ``cost_tolerance`` of each other, the one at the LOWER effort
ordinal wins, because effort buys reasoning tokens (latency, variance) that
the score alone does not show.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isfinite
from typing import Any, Final, Literal

EFFORT_ORDER: Final[tuple[str, ...]] = ("non_reasoning", "none", "low", "medium", "high", "xhigh", "max")
BillingObjective = Literal["metered_usd", "relative", "allowance_weighted"]
_DEFAULT_COST_TOLERANCE: Final[float] = 0.05


class TierSelectionError(Exception):
    """A deterministic refusal carrying a verb-layer code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Pin:
    """An exact accepted scored catalog tuple; provenance stays in the receipt."""

    runtime: str
    model: str
    effort: str


@dataclass(frozen=True, slots=True)
class Constraints:
    """Immutable per-call exclusions; correction means issuing a new selection."""

    effort_ceiling: str | None = None
    exclude_model_prefixes: tuple[str, ...] = ()
    exclude_runtimes: tuple[str, ...] = ()


_DEFAULT_CONSTRAINTS: Final[Constraints] = Constraints()


def _constraint_reason(cell: CapabilityCell, constraints: Constraints) -> str | None:
    if cell.runtime in constraints.exclude_runtimes:
        return "constraint_runtime"
    if cell.model.startswith(constraints.exclude_model_prefixes):
        return "constraint_model_prefix"
    if constraints.effort_ceiling is not None and cell.effort_ordinal > EFFORT_ORDER.index(constraints.effort_ceiling):
        return "constraint_effort_ceiling"
    return None


def _apply_constraints(catalog: tuple[CapabilityCell, ...], constraints: Constraints) -> tuple[tuple[CapabilityCell, ...], dict[str, int]]:
    excluded: dict[str, int] = {}
    if constraints != Constraints():
        excluded = dict.fromkeys(("constraint_runtime", "constraint_model_prefix", "constraint_effort_ceiling"), 0)
    kept: list[CapabilityCell] = []
    for cell in catalog:
        reason = _constraint_reason(cell, constraints)
        if reason is None:
            kept.append(cell)
        else:
            excluded[reason] += 1
    return tuple(kept), excluded


@dataclass(frozen=True, slots=True)
class CapabilityCell:
    """One accepted (runtime, model, effort) measurement, as the store serves it."""

    runtime: str
    model: str
    effort: str
    capability_score: float
    cost_per_task_usd: float | None
    relative_cost_multiplier: float | None
    measured_at: datetime
    acceptance: str
    plan_weight: float = 1.0
    enabled: bool = True

    @property
    def pair(self) -> tuple[str, str]:
        return (self.runtime, self.model)

    @property
    def effort_ordinal(self) -> int:
        return EFFORT_ORDER.index(self.effort)

    def cost(self, objective: BillingObjective) -> float | None:
        if objective == "metered_usd":
            return self.cost_per_task_usd
        if objective == "allowance_weighted":
            relative = self.relative_cost_multiplier
            return None if relative is None else relative * self.plan_weight
        return self.relative_cost_multiplier


@dataclass(frozen=True, slots=True)
class LadderStep:
    """One effort step inside a model, with its marginal efficiency."""

    from_effort: str
    to_effort: str
    score_gain: float
    cost_delta: float
    points_per_cost_unit: float | None


@dataclass(frozen=True, slots=True)
class StaleReading:
    """A feasible cell whose reading is older than the staleness window, and by how much."""

    runtime: str
    model: str
    effort: str
    measured_at: datetime
    age_hours: float
    window_hours: float
    selected: bool


@dataclass(frozen=True, slots=True)
class TierSelection:
    selected: CapabilityCell
    objective: BillingObjective
    required_score: float
    effective_required_score: float
    frontier: tuple[CapabilityCell, ...]
    dominated: tuple[tuple[CapabilityCell, CapabilityCell], ...]
    ladder: tuple[LadderStep, ...]
    excluded: dict[str, int]
    advisories: dict[str, int]
    stale_readings: tuple[StaleReading, ...]
    unknown_effort_cells: tuple[str, ...]
    pin: Pin | None
    pin_cost_delta: float | None
    pin_overrides_constraints: bool
    pin_below_threshold: bool


def _split_by_effort(catalog: tuple[CapabilityCell, ...]) -> tuple[tuple[CapabilityCell, ...], tuple[str, ...]]:
    """The cells whose effort is on the ladder, and the names of the rest (``runtime/model@effort``)."""
    known = tuple(cell for cell in catalog if cell.effort in EFFORT_ORDER)
    unknown = tuple(f"{cell.runtime}/{cell.model}@{cell.effort}" for cell in catalog if cell.effort not in EFFORT_ORDER)
    return known, unknown


@dataclass(frozen=True, slots=True)
class CatalogWindow:
    """Exact live value and honest projection into the integer cell column."""

    hours: float | None
    cell_hours: int | None
    source: str
    policy_revision_id: str | None
    advisories: list[str]

    def max_age(self) -> timedelta | None:
        return None if self.hours is None else timedelta(hours=self.hours)


def _window_hours(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not isfinite(raw) or raw <= 0:
        return None
    return float(raw)


def catalog_staleness_window(cells: Sequence[dict[str, Any]]) -> timedelta | None:
    """Tightest observed positive window; no observation means unknown."""
    hours = [value for row in cells if (value := _window_hours(row.get("staleness_window_hours"))) is not None]
    return timedelta(hours=min(hours)) if hours else None


def _window_result(hours: float | None, source: str, revision: str | None, advisories: list[str]) -> CatalogWindow:
    cell_hours = None if hours is None or not float(hours).is_integer() else int(hours)
    if hours is None:
        advisories.append("Freshness window is unknown; accepted readings remain usable.")
    elif cell_hours is None:
        advisories.append(f"{source} window {hours:g} h is not representable in the integer cell column; cell storage uses null; live reads use the exact value.")
    return CatalogWindow(hours, cell_hours, source, revision, advisories)


def resolve_freshness_window(cells: Sequence[dict[str, Any]], *, requested: object = None, policy_hours: float | None = None, policy_status: str = "unconfigured", policy_revision_id: str | None = None) -> CatalogWindow:
    """Annotate explicit > active policy > observed > unknown without I/O."""
    if requested is not None:
        hours = _window_hours(requested)
        if hours is None:
            raise TierSelectionError("parameter_invalid", "staleness window must be a positive finite number.")
        return _window_result(hours, "request", None, [])
    if policy_status == "active" and policy_hours is not None:
        return _window_result(policy_hours, "policy", policy_revision_id, [])
    status = "cleared" if policy_status == "active" else policy_status
    advisories = [f"Catalog policy is {status}; using observed window if available."]
    observed = catalog_staleness_window(cells)
    hours = None if observed is None else observed.total_seconds() / 3600.0
    return _window_result(hours, "unknown" if hours is None else "observed_catalog", policy_revision_id, advisories)


def _fresh(cell: CapabilityCell, *, now: datetime, max_age: timedelta | None) -> bool:
    return max_age is None or now - cell.measured_at <= max_age


def _feasible_cells(
    catalog: tuple[CapabilityCell, ...],
    *,
    objective: BillingObjective,
    threshold: float,
    capability_floor_pairs: frozenset[tuple[str, str]] | None,
    now: datetime,
    max_age: timedelta | None,
) -> tuple[list[CapabilityCell], dict[str, int], dict[str, int]]:
    """Filter enabled cells to accepted, scored and priced candidates; age is advisory.

    Operator ruling rul_becb4bed: stale or unknown data never blocks progress.
    A reading past its window is still used with an advisory. Only a
    measured fact -- not accepted, floor-disallowed,
    unpriced, below the bar -- excludes a cell.
    """
    excluded = {
        "not_accepted": 0,
        "capability_floor_disallowed": 0,
        "unpriced": 0,
        "below_threshold": 0,
    }
    advisories = {"stale_reading_used": 0}
    feasible: list[CapabilityCell] = []
    for cell in catalog:
        if cell.acceptance != "accepted":
            excluded["not_accepted"] += 1
        elif capability_floor_pairs is not None and cell.pair not in capability_floor_pairs:
            excluded["capability_floor_disallowed"] += 1
        elif cell.cost(objective) is None:
            excluded["unpriced"] += 1
        elif cell.capability_score < threshold:
            excluded["below_threshold"] += 1
        else:
            feasible.append(cell)
            if not _fresh(cell, now=now, max_age=max_age):
                advisories["stale_reading_used"] += 1
    return feasible, excluded, advisories


def _stale_readings(
    feasible: list[CapabilityCell],
    selected: CapabilityCell,
    *,
    now: datetime,
    max_age: timedelta | None,
) -> tuple[StaleReading, ...]:
    """The feasible cells read past the window, oldest first, each with its age."""
    if max_age is None:
        return ()
    window_hours = max_age.total_seconds() / 3600.0
    stale = [
        StaleReading(
            runtime=cell.runtime,
            model=cell.model,
            effort=cell.effort,
            measured_at=cell.measured_at,
            age_hours=(now - cell.measured_at).total_seconds() / 3600.0,
            window_hours=window_hours,
            selected=cell == selected,
        )
        for cell in feasible
        if not _fresh(cell, now=now, max_age=max_age)
    ]
    return tuple(sorted(stale, key=lambda item: (-item.age_hours, item.runtime, item.model, item.effort)))


def _cost_of(cell: CapabilityCell, objective: BillingObjective) -> float:
    cost = cell.cost(objective)
    if cost is None:
        raise TierSelectionError("catalog_invalid", f"{cell.model}@{cell.effort} lost its price mid-selection.")
    return cost


def _pick_cheapest(feasible: list[CapabilityCell], *, objective: BillingObjective, cost_tolerance: float) -> CapabilityCell:
    """Exact argmin over cost; near-ties resolve to the lower effort, then the higher score."""
    cheapest = min(feasible, key=lambda cell: _cost_of(cell, objective))
    floor = _cost_of(cheapest, objective)
    near = [cell for cell in feasible if _cost_of(cell, objective) <= floor * (1.0 + cost_tolerance)]
    return min(near, key=lambda cell: (cell.effort_ordinal, -cell.capability_score, _cost_of(cell, objective)))


def _frontier(feasible: list[CapabilityCell], objective: BillingObjective) -> tuple[CapabilityCell, ...]:
    """Cells no other feasible cell beats on both axes at once."""
    kept: list[CapabilityCell] = []
    for cell in sorted(feasible, key=lambda item: (_cost_of(item, objective), -item.capability_score)):
        if all(cell.capability_score > other.capability_score for other in kept):
            kept.append(cell)
    return tuple(kept)


def _dominations(
    catalog: tuple[CapabilityCell, ...],
    frontier: tuple[CapabilityCell, ...],
    objective: BillingObjective,
) -> tuple[tuple[CapabilityCell, CapabilityCell], ...]:
    """(weaker, stronger) pairs where a different model's cell wins on both axes — the operator's examples."""
    found: list[tuple[CapabilityCell, CapabilityCell]] = []
    for weaker in catalog:
        weaker_cost = weaker.cost(objective)
        if weaker_cost is None:
            continue
        winners = [stronger for stronger in frontier if stronger.pair != weaker.pair and stronger.capability_score >= weaker.capability_score and _cost_of(stronger, objective) < weaker_cost]
        if winners:
            found.append((weaker, min(winners, key=lambda cell: _cost_of(cell, objective))))
    return tuple(found)


def _ladder(catalog: tuple[CapabilityCell, ...], selected: CapabilityCell, objective: BillingObjective) -> tuple[LadderStep, ...]:
    """The selected model's own effort ladder, step by step, with marginal efficiency."""
    rungs = sorted(
        (cell for cell in catalog if cell.pair == selected.pair and cell.cost(objective) is not None),
        key=lambda cell: cell.effort_ordinal,
    )
    steps: list[LadderStep] = []
    for lower, upper in zip(rungs, rungs[1:], strict=False):
        gain = upper.capability_score - lower.capability_score
        delta = _cost_of(upper, objective) - _cost_of(lower, objective)
        steps.append(LadderStep(lower.effort, upper.effort, gain, delta, gain / delta if delta > 0 else None))
    return tuple(steps)


def _nearest_pin_cells(catalog: tuple[CapabilityCell, ...], pin: Pin, required_score: float, objective: BillingObjective) -> list[str]:
    candidates = [cell for cell in catalog if cell.runtime == pin.runtime and cell.acceptance == "accepted" and cell.effort in EFFORT_ORDER]
    ranked = sorted(candidates, key=lambda cell: (abs(cell.capability_score - required_score), _pin_rank_cost(cell, objective), cell.model, cell.effort))
    return [f"{cell.runtime}/{cell.model}@{cell.effort}" for cell in ranked]


def _pin_rank_cost(cell: CapabilityCell, objective: BillingObjective) -> float:
    cost = cell.cost(objective)
    return float("inf") if cost is None else cost


def _resolve_pin(catalog: tuple[CapabilityCell, ...], pin: Pin | None, required_score: float, objective: BillingObjective) -> CapabilityCell | None:
    if pin is None:
        return None
    selected = next((cell for cell in catalog if (cell.runtime, cell.model, cell.effort) == (pin.runtime, pin.model, pin.effort) and cell.acceptance == "accepted" and cell.effort in EFFORT_ORDER), None)
    if selected is None:
        nearest = _nearest_pin_cells(catalog, pin, required_score, objective)
        raise TierSelectionError("pin_cell_unavailable", f"{pin.runtime}/{pin.model}@{pin.effort}: no accepted scored catalog cell; nearest accepted scored cells: {nearest}.")
    return selected


def _select_cell(feasible: list[CapabilityCell], pinned: CapabilityCell | None, *, objective: BillingObjective, cost_tolerance: float, original_count: int, excluded: dict[str, int], threshold: float) -> CapabilityCell:
    if pinned is not None:
        return pinned
    if not feasible:
        code = "catalog_unaccepted" if excluded["not_accepted"] == original_count else "no_cell_clears_threshold"
        raise TierSelectionError(code, f"no feasible cell for required_score={threshold}: {excluded}.")
    return _pick_cheapest(feasible, objective=objective, cost_tolerance=cost_tolerance)


def _pin_delta(pin: Pin | None, selected: CapabilityCell, feasible: list[CapabilityCell], objective: BillingObjective) -> float | None:
    cost = selected.cost(objective)
    if pin is None or cost is None or not feasible:
        return None
    return cost - min(_cost_of(cell, objective) for cell in feasible)


def _pin_flags(pin: Pin | None, selected: CapabilityCell, constraints: Constraints, threshold: float) -> tuple[bool, bool]:
    return pin is not None and _constraint_reason(selected, constraints) is not None, pin is not None and selected.capability_score < threshold


def _include_pinned_reading(feasible: list[CapabilityCell], selected: CapabilityCell, advisories: dict[str, int], *, capability_floor_pairs: frozenset[tuple[str, str]] | None, now: datetime, max_age: timedelta | None) -> list[CapabilityCell]:
    if selected in feasible:
        return feasible
    if not _fresh(selected, now=now, max_age=max_age):
        advisories["stale_reading_used"] += 1
    if capability_floor_pairs is not None and selected.pair not in capability_floor_pairs:
        advisories["pin_floor_disallowed"] = 1
    return [*feasible, selected]


def select_tier(
    catalog: tuple[CapabilityCell, ...],
    *,
    required_score: float,
    objective: BillingObjective,
    now: datetime,
    max_age: timedelta | None,
    capability_floor_pairs: frozenset[tuple[str, str]] | None = None,
    score_margin: float = 0.0,
    cost_tolerance: float = _DEFAULT_COST_TOLERANCE,
    constraints: Constraints = _DEFAULT_CONSTRAINTS,
    pin: Pin | None = None,
) -> TierSelection:
    """Cheapest enabled accepted cell clearing ``required_score + score_margin``.

    Allowance telemetry does not participate in eligibility or ranking.
    Neither does a reading past ``max_age``: it is used and reported in
    ``advisories`` and ``stale_readings`` (operator ruling rul_becb4bed).
    A cell whose effort is outside ``EFFORT_ORDER`` cannot be ordered, so it is
    excluded before anything else is computed, counted as ``excluded["unknown_effort"]``
    and named in ``unknown_effort_cells``; it never raises, so one such row
    cannot fail every selection. Refuses only on measured facts: an empty
    catalog, a catalog with no cell in the ladder, a catalog with nothing
    accepted, or a threshold nothing clears each raise a distinct code so the
    caller can tell an uncross-checked store from an impossible ask.
    """
    disabled_count = sum(not cell.enabled for cell in catalog)
    catalog = tuple(cell for cell in catalog if cell.enabled)
    pinned = _resolve_pin(catalog, pin, required_score, objective)
    if not catalog:
        raise TierSelectionError("catalog_empty", "no capability cells to select from.")
    if score_margin < 0 or cost_tolerance < 0:
        raise TierSelectionError("parameter_invalid", "score_margin and cost_tolerance must be non-negative.")
    known, unknown = _split_by_effort(catalog)
    if not known:
        raise TierSelectionError("catalog_unknown_effort", f"no capability cell has an effort in the ladder; excluded as unknown_effort: {list(unknown)}.")
    original_count = len(known)
    known, constraint_counts = _apply_constraints(known, constraints)
    threshold = required_score + score_margin
    feasible, excluded, advisories = _feasible_cells(
        known,
        objective=objective,
        threshold=threshold,
        capability_floor_pairs=capability_floor_pairs,
        now=now,
        max_age=max_age,
    )
    excluded.update(constraint_counts)
    excluded["operator_disabled"] = disabled_count
    excluded["unknown_effort"] = len(unknown)
    selected = _select_cell(feasible, pinned, objective=objective, cost_tolerance=cost_tolerance, original_count=original_count, excluded=excluded, threshold=threshold)
    overrides, below = _pin_flags(pin, selected, constraints, threshold)
    readings = _include_pinned_reading(feasible, selected, advisories, capability_floor_pairs=capability_floor_pairs, now=now, max_age=max_age)
    if max_age is None:
        advisories["unknown_freshness_window"] = len(readings)
    frontier = _frontier(feasible, objective)
    return TierSelection(
        selected=selected,
        objective=objective,
        required_score=required_score,
        effective_required_score=threshold,
        frontier=frontier,
        dominated=_dominations(known, frontier, objective),
        ladder=_ladder(known, selected, objective),
        excluded=excluded,
        advisories=advisories,
        stale_readings=_stale_readings(readings, selected, now=now, max_age=max_age),
        unknown_effort_cells=unknown,
        pin=pin,
        pin_cost_delta=_pin_delta(pin, selected, feasible, objective),
        pin_overrides_constraints=overrides,
        pin_below_threshold=below,
    )


__all__ = [
    "EFFORT_ORDER",
    "BillingObjective",
    "CapabilityCell",
    "Constraints",
    "LadderStep",
    "Pin",
    "StaleReading",
    "TierSelection",
    "TierSelectionError",
    "select_tier",
]
