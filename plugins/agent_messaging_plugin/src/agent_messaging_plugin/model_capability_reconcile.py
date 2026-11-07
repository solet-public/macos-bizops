"""The crosscheck reconciler (iss_d136ae29): decide accepted | crosscheck_conflict
| pending_crosscheck for one (runtime, model, effort) cell from its freshest
readings, never trusting a source that cannot be corroborated -- and saying
so when the corroboration is missing rather than leaving the cell pending.

Pure and offline -- no fetch, no state-layer call. `refresh_model_capability_catalog`
(model_capability_verbs.py) is the only caller: it discovers the cells the
harness lists offer and the leaderboard scores (`model_capability_fetch`),
`model_capability_store.reconcile_discovery` writes the readings as
`model_capability_observation` rows, calls `reconcile_cell` per discovered cell,
and writes the resulting `model_capability_cell` row. Splitting the decision out
like this is what makes it testable against fixture readings with zero network
access.

## The three evidence situations

* No leaderboard reading for the cell (a delisted model whose detail page still
  answers, a row with no score): `reconcile_cell` returns None. A detail page
  alone is not evidence that a model is still measured (detail pages outlive
  leaderboard listings), so it neither creates nor renews a cell, and no
  `pending_crosscheck` cell is ever written for it (design part 4.3).
* Both pages report: the two-source agreement below decides.
* Only the leaderboard reports (the detail page 404s, errors, has no link or no
  longer parses): the reading is accepted ALONE with a note naming why
  (`single source: detail page <reason>`), provided it is in range (checked when
  the reading is made) and does not differ from the cell's prior accepted value
  by more than the tolerances below. A reading that does differ keeps the prior
  value and is reported as `single_source_unconfirmed` (design 1.6, G7).

## Why two `artificial_analysis_*` sources satisfy "at least two DISTINCT
source_ids" here

The brief (workbench/2026-09-21_dispatch_model_capability_crosscheck_refresh_run.md)
warns against a *fabricated* second source dressed as independent evidence --
concretely, a caller's own operational say-so treated as if it corroborated
itself. `artificial_analysis_leaderboard_html` and
`artificial_analysis_model_detail_html` are not that: they are two separately
fetched, separately rendered pages, verified live 2026-09-21 to be able to
diverge (the leaderboard aggregates many models on one cadence; a per-model
detail page is a different route, plausibly a different render/cache path).
They are not evidence of a *different organization's* measurement -- no
vendor publishes Artificial Analysis's own Intelligence Index, so no such
second organization exists for this specific metric -- but they are two
real, independently-fetched artifacts whose agreement is genuine corroboration
against a transient fetch/parse/cache fault, which is what this reconciler
actually protects against. `anthropic_pricing_page` / `openai_model_docs`
(not implemented here) would add cost-plausibility corroboration from a truly
different publisher as a fast-follow; they cannot corroborate
`capability_score` itself since they don't publish it.

## Tolerance, and why

`capability_score`: absolute tolerance 2.0 index points. Both sources render
the same underlying Artificial Analysis benchmark result; a live spot-check
2026-09-21 found them byte-identical (claude-sonnet-5 max: 38 on both). A
2.0-point allowance covers legitimate cache-propagation lag between the
leaderboard's aggregate render and a per-model detail page's own render
without masking a real score change -- the seed table's own effort-tier
deltas for one model are themselves usually >=1 point (e.g. claude-sonnet-5:
low=24, medium=28 -- a 4-point real step), so 2.0 is well inside "still the
same measurement," not inside "a different effort tier's plausible score."

`cost_per_task_usd`: relative tolerance 5%. Same-benchmark cost figures carry
cents-level precision; a few percent covers rendering/rounding drift between
the two pages without hiding a real re-pricing event, which historically (the
2026-09-19 seed vs. this fast-follow's own live spot-check) moves a cell's
cost by tens of percent, not single digits.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Final

from .model_capability_fetch import SOURCE_LEADERBOARD_HTML, SOURCE_MODEL_DETAIL_HTML, DiscoveredCell, DiscoveryRun, Reading
from .schema import (
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    CELL_ACCEPTANCE_WITHDRAWN,
)

CAPABILITY_SCORE_TOLERANCE: Final[float] = 2.0
"""Absolute Intelligence Index points. See module docstring."""

COST_RELATIVE_TOLERANCE: Final[float] = 0.05
"""Fraction of the larger reading. See module docstring."""

_REQUIRED_METRIC: Final[str] = "intelligence_index"
_OPTIONAL_METRIC: Final[str] = "cost_per_task_usd"


def disagreement(prior_score: float, prior_cost: float | None, score: float, cost: float | None) -> tuple[float, float] | None:
    """(absolute score delta, relative cost delta) when either exceeds its tolerance, else None.

    The one comparison of a held value against a new one, used for a single-source
    reading against the prior accepted value and for a manual reading against live ones."""
    score_delta = abs(prior_score - score)
    cost_delta = 0.0
    if prior_cost is not None and cost is not None:
        cost_delta = abs(prior_cost - cost) / (max(abs(prior_cost), abs(cost)) or 1.0)
    if score_delta <= CAPABILITY_SCORE_TOLERANCE and cost_delta <= COST_RELATIVE_TOLERANCE:
        return None
    return score_delta, cost_delta


@dataclass(frozen=True, slots=True)
class PriorValue:
    """The score and cost a cell already holds as accepted: what a single-source reading is compared with."""

    capability_score: float
    cost_per_task_usd: float | None


@dataclass(frozen=True, slots=True)
class ObservationRef:
    """The persisted identity of one Reading, once written -- what the cell's
    `agreeing_observation_ids` actually points at."""

    observation_id: str
    reading: Reading


@dataclass(frozen=True, slots=True)
class ReconcileOutcome:
    cell_key: tuple[str, str, str]
    acceptance: str
    capability_score: float | None
    cost_per_task_usd: float | None
    measured_at: str | None
    agreeing_observation_ids: list[str]
    disagreement_note: str | None
    single_source_unconfirmed: bool = False
    """True for a `pending_crosscheck` outcome that exists only because a single-source
    reading differed from the prior accepted value: the store keeps the prior cell."""


def _freshest_ok_by_source(refs: list[ObservationRef], *, metric: str) -> dict[str, ObservationRef]:
    """The single freshest `fetch_status=ok` reading per source_id for one metric."""
    by_source: dict[str, ObservationRef] = {}
    for ref in refs:
        if ref.reading.metric != metric or ref.reading.fetch_status != "ok" or ref.reading.value_number is None:
            continue
        current = by_source.get(ref.reading.source_id)
        if current is None or ref.reading.fetched_at > current.reading.fetched_at:
            by_source[ref.reading.source_id] = ref
    return by_source


def _conflict_outcome(cell_key: tuple[str, str, str], *, metric: str, spread_text: str, values: dict[str, float | None]) -> ReconcileOutcome:
    detail = ", ".join(f"{sid}={v}" for sid, v in sorted(values.items()))
    return ReconcileOutcome(
        cell_key=cell_key, acceptance=CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
        capability_score=None, cost_per_task_usd=None, measured_at=None,
        agreeing_observation_ids=[],
        disagreement_note=f"{metric} disagreement exceeds tolerance ({spread_text}): {detail}.",
    )


@dataclass(frozen=True, slots=True)
class _ScoreAgreement:
    value: float
    agreeing_ids: list[str]
    measured_at: str


def _reconcile_score(cell_key: tuple[str, str, str], score_by_source: dict[str, ObservationRef]) -> ReconcileOutcome | _ScoreAgreement:
    """Precondition: caller has already checked `len(score_by_source) >= 2`."""
    values = {sid: ref.reading.value_number for sid, ref in score_by_source.items()}
    assert all(v is not None for v in values.values())  # noqa: S101 -- _freshest_ok_by_source already filtered None
    spread = max(values.values()) - min(values.values())  # type: ignore[operator]
    if spread > CAPABILITY_SCORE_TOLERANCE:
        return _conflict_outcome(cell_key, metric=_REQUIRED_METRIC, spread_text=f"{spread:.1f} > {CAPABILITY_SCORE_TOLERANCE}", values=values)
    return _ScoreAgreement(
        value=sum(values.values()) / len(values),  # type: ignore[misc]
        agreeing_ids=[ref.observation_id for ref in score_by_source.values()],
        measured_at=max(ref.reading.fetched_at for ref in score_by_source.values()),
    )


@dataclass(frozen=True, slots=True)
class _CostAgreement:
    value: float | None
    agreeing_ids: list[str]
    note: str | None


def _reconcile_cost(cell_key: tuple[str, str, str], cost_by_source: dict[str, ObservationRef]) -> ReconcileOutcome | _CostAgreement:
    if not cost_by_source:
        return _CostAgreement(value=None, agreeing_ids=[], note=None)
    if len(cost_by_source) == 1:
        (only_source, only_ref), = cost_by_source.items()
        note = f"{_OPTIONAL_METRIC} corroborated by only 1 source ({only_source}); accepted on {_REQUIRED_METRIC}'s 2-source agreement alone."
        return _CostAgreement(value=only_ref.reading.value_number, agreeing_ids=[], note=note)
    values = {sid: ref.reading.value_number for sid, ref in cost_by_source.items()}
    larger = max(abs(v) for v in values.values() if v is not None) or 1.0  # type: ignore[arg-type]
    spread = (max(values.values()) - min(values.values())) / larger  # type: ignore[operator]
    if spread > COST_RELATIVE_TOLERANCE:
        return _conflict_outcome(cell_key, metric=_OPTIONAL_METRIC, spread_text=f"{spread:.1%} > {COST_RELATIVE_TOLERANCE:.0%}", values=values)
    return _CostAgreement(
        value=sum(values.values()) / len(values),  # type: ignore[misc]
        agreeing_ids=[ref.observation_id for ref in cost_by_source.values()],
        note=None,
    )


def _detail_gap(observations: list[ObservationRef]) -> str:
    """Why the detail page did not corroborate: the reason its freshest non-ok reading carries."""
    gaps = [ref.reading for ref in observations if ref.reading.source_id == SOURCE_MODEL_DETAIL_HTML and ref.reading.metric == _REQUIRED_METRIC and ref.reading.fetch_status != "ok"]
    if not gaps:
        return "was not fetched"
    freshest = max(gaps, key=lambda reading: reading.fetched_at)
    reason = freshest.value_text or freshest.fetch_status
    return f"unavailable ({freshest.fetch_status}: {reason})"


def _single_source_outcome(
    cell_key: tuple[str, str, str], leaderboard: dict[str, ObservationRef], cost_ref: ObservationRef | None, prior: PriorValue | None, gap: str,
) -> ReconcileOutcome:
    """The leaderboard reported and the detail page did not: accept it alone, with the reason, if it stays near the prior value."""
    (only_ref,) = leaderboard.values()
    score = only_ref.reading.value_number
    assert score is not None  # noqa: S101 -- _freshest_ok_by_source already filtered None
    cost = None if cost_ref is None else cost_ref.reading.value_number
    note = f"single source: detail page {gap}"
    moved = None if prior is None else disagreement(prior.capability_score, prior.cost_per_task_usd, score, cost)
    if moved is not None:
        return ReconcileOutcome(
            cell_key=cell_key, acceptance=CELL_ACCEPTANCE_PENDING_CROSSCHECK, capability_score=None, cost_per_task_usd=None,
            measured_at=None, agreeing_observation_ids=[], single_source_unconfirmed=True,
            disagreement_note=f"single source unconfirmed: the leaderboard reading moved {moved[0]:.1f} points and {moved[1]:.1%} on cost from the prior accepted value, past tolerance, and the detail page {gap}; the prior value is kept.",
        )
    ids = [only_ref.observation_id] + ([] if cost_ref is None else [cost_ref.observation_id])
    return ReconcileOutcome(
        cell_key=cell_key, acceptance=CELL_ACCEPTANCE_ACCEPTED, capability_score=score, cost_per_task_usd=cost,
        measured_at=only_ref.reading.fetched_at, agreeing_observation_ids=ids, disagreement_note=note,
    )


def reconcile_cell(cell_key: tuple[str, str, str], observations: list[ObservationRef], *, prior: PriorValue | None = None) -> ReconcileOutcome | None:
    """`observations` is every reading gathered for this (runtime, model,
    effort) cell across all sources in one refresh run. Never mutates state;
    the caller writes the returned outcome. `prior` is the cell's accepted
    value, used only to bound a single-source reading.

    None means "no leaderboard reading: nothing to decide" -- the caller writes no
    cell and no `pending_crosscheck` row. Otherwise `capability_score` must clear a
    real 2-source agreement, or be the leaderboard alone with the detail page's
    absence named (see the module docstring); `cost_per_task_usd` is corroborated
    the same way when possible but never blocks acceptance on its own
    missing/single-source evidence -- see `_reconcile_cost`."""
    score_by_source = _freshest_ok_by_source(observations, metric=_REQUIRED_METRIC)
    if SOURCE_LEADERBOARD_HTML not in score_by_source:
        return None

    if len(score_by_source) < 2:
        leaderboard_cost = _freshest_ok_by_source(observations, metric=_OPTIONAL_METRIC).get(SOURCE_LEADERBOARD_HTML)
        return _single_source_outcome(cell_key, score_by_source, leaderboard_cost, prior, _detail_gap(observations))

    cost_by_source = _freshest_ok_by_source(observations, metric=_OPTIONAL_METRIC)
    score = _reconcile_score(cell_key, score_by_source)
    if isinstance(score, ReconcileOutcome):
        return score

    cost = _reconcile_cost(cell_key, cost_by_source)
    if isinstance(cost, ReconcileOutcome):
        return cost

    return ReconcileOutcome(
        cell_key=cell_key, acceptance=CELL_ACCEPTANCE_ACCEPTED,
        capability_score=score.value, cost_per_task_usd=cost.value, measured_at=score.measured_at,
        agreeing_observation_ids=score.agreeing_ids + cost.agreeing_ids, disagreement_note=cost.note,
    )


# --------------------------------------------------------------------------- creation and the run report
# Pure decisions over plain rows; `model_capability_store.reconcile_discovery` does the state writes.

CellKey = tuple[str, str, str]

CREATION_CAP_PER_RUN: Final[int] = 30
"""New cells one refresh run may create (design G3). The rest are deferred, not dropped:
they stay discoverable and the next run takes the next 30."""

CREATION_LIVE_CEILING: Final[int] = 300
"""Live (non-withdrawn) rows at or above which a refresh creates no new cell (design G4).
Withdrawn rows are history and never count, so history cannot block discovery; the manual
record verb is not subject to it. It sits below the catalog read ceiling, so discovery can
never produce a truncated read."""

_UNRENEWABLE_NAMES_IN_NOTE: Final[int] = 10


def catalog_models_by_runtime(rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[str, ...]]:
    """runtime -> the launch ids the catalog already holds: what leaderboard rows are joined to when a harness list is unreadable."""
    found: dict[str, dict[str, None]] = {}
    for row in rows:
        found.setdefault(str(row["runtime"]), {})[str(row["model"])] = None
    return {runtime: tuple(models) for runtime, models in found.items()}


def group_readings(readings: Sequence[Reading]) -> dict[CellKey, list[Reading]]:
    grouped: dict[CellKey, list[Reading]] = {}
    for reading in readings:
        grouped.setdefault((reading.runtime, reading.model, reading.effort), []).append(reading)
    return grouped


@dataclass(frozen=True, slots=True)
class RefreshReport:
    """What `reconcile_discovery` did: the run row's counts, the result fields the refresh verb returns, and the run note."""

    counts: dict[str, int]
    result_fields: dict[str, Any]
    note: str | None


def key_text(key: CellKey) -> str:
    return f"{key[0]}/{key[1]}@{key[2]}"


def _number(raw: object) -> float | None:
    return None if raw is None or isinstance(raw, bool) or not isinstance(raw, (int, float)) else float(raw)


def prior_value(row: Mapping[str, Any] | None) -> PriorValue | None:
    """The score and cost an accepted or withdrawn cell holds; the bound a single-source reading is compared with.

    A withdrawn cell keeps its score, so a lone reading is bounded by it exactly as it is by an
    accepted cell's value: leaving the bound off would let a single source rewrite a withdrawn
    cell by any amount (iss_71a21363)."""
    if row is None or row.get("acceptance") not in (CELL_ACCEPTANCE_ACCEPTED, CELL_ACCEPTANCE_WITHDRAWN):
        return None
    score = _number(row.get("capability_score"))
    return None if score is None else PriorValue(score, _number(row.get("cost_per_task_usd")))


def reconcile_cells(processed: Sequence[DiscoveredCell], written: Mapping[CellKey, list[ObservationRef]], rows: Mapping[CellKey, Mapping[str, Any]]) -> dict[CellKey, ReconcileOutcome]:
    """The outcome of every processed cell that has a leaderboard reading; the rest have none (see `reconcile_cell`)."""
    outcomes: dict[CellKey, ReconcileOutcome] = {}
    for cell in processed:
        outcome = reconcile_cell(cell.key, written.get(cell.key, []), prior=prior_value(rows.get(cell.key)))
        if outcome is not None:
            outcomes[cell.key] = outcome
    return outcomes


def _has_score_from(readings: Sequence[Reading], source_id: str) -> bool:
    return any(r.source_id == source_id and r.metric == _REQUIRED_METRIC and r.fetch_status == "ok" and r.value_number is not None for r in readings)


def has_leaderboard_score(readings: Sequence[Reading]) -> bool:
    return _has_score_from(readings, SOURCE_LEADERBOARD_HTML)


def has_two_sources(readings: Sequence[Reading]) -> bool:
    """Both the leaderboard and the detail page reported a usable score for the cell."""
    return has_leaderboard_score(readings) and _has_score_from(readings, SOURCE_MODEL_DETAIL_HTML)


@dataclass(frozen=True, slots=True)
class CreationPlan:
    """Which new cells this run creates, which wait for a slot, and which wait for a second source.

    `capped` means the per-run cap is what limited creation, `suspended` that the live-row
    ceiling was; both can be true of one run only through different cells, never the same one."""

    create: tuple[DiscoveredCell, ...]
    deferred: tuple[DiscoveredCell, ...]
    capped: bool
    suspended: bool
    awaiting_second_source: tuple[DiscoveredCell, ...] = ()


def _new_cells(cells: Sequence[DiscoveredCell], existing: Collection[CellKey], readings_by_key: Mapping[CellKey, Sequence[Reading]]) -> tuple[list[DiscoveredCell], tuple[DiscoveredCell, ...]]:
    """(the new cells that may be created, the derived-id cells still waiting for a second source)."""
    scored = [cell for cell in cells if cell.key not in existing and has_leaderboard_score(readings_by_key.get(cell.key, ()))]
    waiting = tuple(cell for cell in scored if cell.derived_launch_id and not has_two_sources(readings_by_key.get(cell.key, ())))
    waiting_keys = {cell.key for cell in waiting}
    return [cell for cell in scored if cell.key not in waiting_keys], waiting


def plan_creation(cells: Sequence[DiscoveredCell], existing: Collection[CellKey], *, live_rows: int, readings_by_key: Mapping[CellKey, Sequence[Reading]]) -> CreationPlan:
    """Which new cells this run may create (design G3, G4, 1.7).

    A new cell is a discovered pair with no row yet and a usable leaderboard score. A cell whose
    launch id was derived from a leaderboard name (no usable harness list vouches for it) also
    needs the detail page to agree: until it does it is listed in `awaiting_second_source`, takes
    no creation slot, and is found again on the next run (pass condition: the detail page answers;
    component: the daily refresh). At most `CREATION_CAP_PER_RUN` are created and none once the
    live rows reach `CREATION_LIVE_CEILING`; the rest are deferred -- they are not rows yet, so
    the next run finds them again. The caller reads the whole table, so `live_rows` is exact."""
    headroom = max(0, CREATION_LIVE_CEILING - live_rows)
    slots = min(CREATION_CAP_PER_RUN, headroom)
    new, waiting = _new_cells(cells, existing, readings_by_key)
    deferred = new[slots:]
    return CreationPlan(
        tuple(new[:slots]), tuple(deferred), capped=bool(deferred) and headroom >= CREATION_CAP_PER_RUN,
        suspended=bool(deferred) and headroom < CREATION_CAP_PER_RUN, awaiting_second_source=waiting,
    )


def manual_disagreement_note(prior: PriorValue, outcome: ReconcileOutcome) -> str | None:
    """Q8: the note for a manual reading that disagrees with two agreeing live pages, or None when they agree.

    The cross-check's job, catching a transient fetch fault, is served by the note and the
    run result's `manual_conflicts`; excluding a model an operator deliberately recorded
    with a named source would be a gate nobody can pass (rul_becb4bed). Nothing is
    excluded: the cell stays accepted with its manual values until an operator re-records
    it or the live sources agree with it within tolerance."""
    if outcome.capability_score is None:
        return None
    moved = disagreement(prior.capability_score, prior.cost_per_task_usd, outcome.capability_score, outcome.cost_per_task_usd)
    if moved is None:
        return None
    return f"manual versus live disagreement: score delta {moved[0]:.3f}, cost delta {moved[1]:.1%} (manual value kept)"


def _stamp(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def unrenewable_stale(run: DiscoveryRun, rows: Mapping[CellKey, Mapping[str, Any]], renewed: Collection[CellKey], *, now: datetime) -> list[dict[str, Any]]:
    """Accepted cells of harness-listed models that no leaderboard reading renewed this run.

    These stay accepted with their last score -- a model is never withdrawn because the
    leaderboard dropped it -- and are named so the staleness is a standing, loud fact. A
    failed or thin leaderboard page renews nothing, which says nothing about the models,
    so the list is empty then; a runtime whose harness list was unusable has no listed models."""
    if not run.leaderboard_ok:
        return []
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for key, row in rows.items():
        listed = key[1] in run.harness_model_ids.get(key[0], ())
        if listed and key not in renewed and row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED:
            groups.setdefault((key[0], key[1]), []).append(row)
    return [_stale_entry(runtime, model, members, now) for (runtime, model), members in sorted(groups.items())]


def _stale_entry(runtime: str, model: str, rows: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    stamps = [stamp for stamp in (_stamp(row.get("measured_at")) for row in rows) if stamp is not None]
    last = max(stamps) if stamps else None
    return {
        "runtime": runtime, "model": model, "efforts": sorted(str(row["effort"]) for row in rows),
        "last_measured_at": None if last is None else last.isoformat(), "age_hours": None if last is None else round((now - last).total_seconds() / 3600, 1),
    }


def _creation_notes(plan: CreationPlan, created: int) -> list[str]:
    parts: list[str] = []
    if created:
        parts.append(f"{created} cell(s) created")
    if plan.deferred:
        why = f"capped at {CREATION_CAP_PER_RUN} per run" if plan.capped else f"live-row ceiling {CREATION_LIVE_CEILING} reached"
        parts.append(f"{len(plan.deferred)} new cell(s) deferred ({why})")
    if plan.awaiting_second_source:
        parts.append(f"{len(plan.awaiting_second_source)} new cell(s) with a derived launch id wait for the detail page to agree")
    return parts


def refresh_note(run: DiscoveryRun, plan: CreationPlan, created: int, unrenewable: Sequence[Mapping[str, Any]]) -> str | None:
    """The run row's note: what changed beyond the counts, in one line."""
    parts = _creation_notes(plan, created)
    if unrenewable:
        names = ", ".join(f"{item['runtime']}/{item['model']}" for item in unrenewable[:_UNRENEWABLE_NAMES_IN_NOTE])
        parts.append(f"{len(unrenewable)} harness-listed model(s) have no leaderboard row to renew them: {names}")
    parts.extend(f"{runtime} harness list {summary['status']}" for runtime, summary in run.harness.items() if summary["status"] != "ok")
    if run.failures:
        parts.append(f"{len(run.failures)} source fetch(es) failed")
    return "; ".join(parts) or None


def refresh_result_fields(
    run: DiscoveryRun, plan: CreationPlan, *, created: Sequence[str], unconfirmed: Sequence[str], manual_conflicts: Sequence[str], withdrawn_left: Sequence[str], unrenewable: Sequence[Mapping[str, Any]], advisories: Sequence[str],
) -> dict[str, Any]:
    """The discovery fields the refresh verb returns beside the run row's counts."""
    return {
        "cells_created": len(created), "created": list(created),
        "creation_capped": plan.capped, "creation_suspended": plan.suspended,
        "deferred": [key_text(cell.key) for cell in plan.deferred],
        "awaiting_second_source": [key_text(cell.key) for cell in plan.awaiting_second_source],
        "withdrawn_left": list(withdrawn_left),
        "unrenewable_stale": list(unrenewable),
        "alias_candidates": [dict(item) for item in run.alias_candidates],
        "harness": {runtime: dict(summary) for runtime, summary in run.harness.items()},
        "unmapped_creators": [{"creator": creator, "rows": rows} for creator, rows in run.unmapped_creators],
        "efforts_outside_vocabulary": {model: list(efforts) for model, efforts in run.efforts_outside_vocabulary.items()},
        "listed_not_launchable": list(run.listed_not_launchable),
        "creator_label_unmatched": list(run.creator_label_unmatched),
        "single_source_unconfirmed": list(unconfirmed), "manual_conflicts": list(manual_conflicts),
        "advisories": list(advisories),
    }


# --------------------------------------------------------------------------- the pass over one refresh (pure helpers the store calls)

@dataclass(slots=True)
class RunTally:
    """What one reconcile pass counted and named; the store fills it, `build_report` reads it."""


    counts: Counter[str] = field(default_factory=lambda: Counter[str]())
    created: list[str] = field(default_factory=lambda: list[str]())
    unconfirmed: list[str] = field(default_factory=lambda: list[str]())
    manual_conflicts: list[str] = field(default_factory=lambda: list[str]())
    withdrawn_left: list[str] = field(default_factory=lambda: list[str]())


def cost_floor(outcomes: Mapping[CellKey, ReconcileOutcome]) -> float | None:
    priced = [o.cost_per_task_usd for o in outcomes.values() if o.acceptance == CELL_ACCEPTANCE_ACCEPTED and o.cost_per_task_usd]
    return min(priced) if priced else None


def flag_new_cell(outcome: ReconcileOutcome, cell: DiscoveredCell) -> ReconcileOutcome:
    """A new cell carries the flag its harness list earned it (a degraded list, a derived launch id) beside any reconcile note."""
    notes = [note for note in (outcome.disagreement_note, cell.creation_note) if note]
    return replace(outcome, disagreement_note="; ".join(notes) or None)


def processed_cells(run: DiscoveryRun, by_key: Mapping[CellKey, Mapping[str, Any]], plan: CreationPlan) -> list[DiscoveredCell]:
    """Every discovered cell that already has a row, plus the new ones the plan lets this run create."""
    create_keys = {cell.key for cell in plan.create}
    return [cell for cell in run.cells if cell.key in by_key or cell.key in create_keys]


def build_report(run: DiscoveryRun, plan: CreationPlan, tally: RunTally, *, unrenewable: Sequence[Mapping[str, Any]]) -> RefreshReport:
    advisories = list(run.advisories)
    advisories.extend(f"{key} is withdrawn: this run recorded its readings and left it withdrawn" for key in tally.withdrawn_left)
    fields = refresh_result_fields(
        run, plan, created=tally.created, unconfirmed=tally.unconfirmed, manual_conflicts=tally.manual_conflicts, withdrawn_left=tally.withdrawn_left,
        unrenewable=unrenewable, advisories=advisories,
    )
    counts = {"cells_accepted": tally.counts["accepted"], "cells_conflicted": tally.counts["conflicted"], "cells_unchanged": tally.counts["unchanged"]}
    return RefreshReport(counts=counts, result_fields=fields, note=refresh_note(run, plan, len(tally.created), unrenewable))


__all__ = [
    "CAPABILITY_SCORE_TOLERANCE",
    "COST_RELATIVE_TOLERANCE",
    "CREATION_CAP_PER_RUN",
    "CREATION_LIVE_CEILING",
    "CellKey",
    "CreationPlan",
    "ObservationRef",
    "PriorValue",
    "ReconcileOutcome",
    "RefreshReport",
    "RunTally",
    "build_report",
    "catalog_models_by_runtime",
    "cost_floor",
    "disagreement",
    "flag_new_cell",
    "group_readings",
    "has_leaderboard_score",
    "has_two_sources",
    "key_text",
    "manual_disagreement_note",
    "plan_creation",
    "processed_cells",
    "prior_value",
    "reconcile_cell",
    "reconcile_cells",
    "refresh_note",
    "refresh_result_fields",
    "unrenewable_stale",
]
