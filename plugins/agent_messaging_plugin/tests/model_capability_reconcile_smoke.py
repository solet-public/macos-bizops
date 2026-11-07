#!/usr/bin/env python3
"""Smoke: the crosscheck reconciler's agreement/conflict logic (iss_d136ae29) and the
discovery refresh's evidence rules (iss_0e805222, iss_52fdb8e5; design 1.6, 4.3, G7).

Fixture-based, no network, no state layer -- exercises `reconcile_cell`
directly against hand-built `Reading`s: agreeing, disagreeing, a missing detail page
(the leaderboard is accepted alone, with the reason and within tolerance of the prior
value), no leaderboard reading at all (no outcome, so no `pending_crosscheck` cell),
one source `fetch_status != ok`, and a fully-absent metric.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_reconcile_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin.model_capability_fetch import Reading  # noqa: E402
from agent_messaging_plugin.model_capability_reconcile import (  # noqa: E402
    CAPABILITY_SCORE_TOLERANCE,
    COST_RELATIVE_TOLERANCE,
    ObservationRef,
    PriorValue,
    ReconcileOutcome,
    disagreement,
    reconcile_cell,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
)

_CELL = ("claude_code", "claude-sonnet-5", "max")
_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _reading(source_id: str, metric: str, value: float | None, *, fetch_status: str = "ok", fetched_at: str = "2026-09-21T12:00:00+00:00", value_text: str | None = None) -> Reading:
    return Reading(
        source_id=source_id, provider="anthropic", runtime=_CELL[0], model=_CELL[1], effort=_CELL[2],
        metric=metric, value_number=value, value_text=value_text, raw_excerpt="fixture", fetch_status=fetch_status,
        fetched_at=fetched_at,
    )


def _ref(obs_id: str, reading: Reading) -> ObservationRef:
    return ObservationRef(observation_id=obs_id, reading=reading)


def _decide(obs: list[ObservationRef]) -> ReconcileOutcome:
    """The outcome for observations that must produce one (a leaderboard reading is present)."""
    outcome = reconcile_cell(_CELL, obs)
    if outcome is None:
        raise AssertionError("reconcile_cell returned no outcome for observations that carry a leaderboard reading")
    return outcome


def scenario_agreeing_two_sources_accepts() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0)),
        _ref("obs3", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.09)),
        _ref("obs4", _reading("artificial_analysis_model_detail_html", "cost_per_task_usd", 5.09)),
    ]
    outcome = _decide(obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, "two agreeing sources (score+cost) -> accepted")
    _check(outcome.capability_score == 38.0, f"agreed capability_score is the mean of agreeing readings (got {outcome.capability_score})")
    _check(outcome.cost_per_task_usd == 5.09, f"agreed cost_per_task_usd is the mean of agreeing readings (got {outcome.cost_per_task_usd})")
    _check(set(outcome.agreeing_observation_ids) == {"obs1", "obs2", "obs3", "obs4"}, "agreeing_observation_ids names every corroborating reading")


def scenario_score_within_tolerance_accepts() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0 + CAPABILITY_SCORE_TOLERANCE)),
    ]
    outcome = _decide(obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, f"score spread exactly at tolerance ({CAPABILITY_SCORE_TOLERANCE}) still accepts")


def scenario_score_beyond_tolerance_conflicts() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0 + CAPABILITY_SCORE_TOLERANCE + 0.1)),
    ]
    outcome = _decide(obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT, "score spread just past tolerance -> crosscheck_conflict")
    _check(outcome.capability_score is None, "a conflicted cell carries no capability_score")
    _check(outcome.disagreement_note is not None and "intelligence_index" in outcome.disagreement_note, "disagreement_note names the disagreeing metric")


def scenario_cost_beyond_tolerance_conflicts_even_with_score_agreement() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0)),
        _ref("obs3", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.00)),
        _ref("obs4", _reading("artificial_analysis_model_detail_html", "cost_per_task_usd", 5.00 * (1 + COST_RELATIVE_TOLERANCE + 0.05))),
    ]
    outcome = _decide(obs)
    _check(outcome.acceptance == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT, "agreeing score but disagreeing cost still refuses acceptance")
    _check(outcome.disagreement_note is not None and "cost_per_task_usd" in outcome.disagreement_note, "disagreement_note names cost, not score, as the disagreeing metric")


def scenario_a_missing_detail_page_accepts_the_single_source_with_a_note() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.09)),
        _ref("obs3", _reading("artificial_analysis_model_detail_html", "intelligence_index", None, fetch_status="http_error", value_text="https://x/models/y: HTTP 404")),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome is not None and outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, "the leaderboard alone is accepted when the detail page is missing (it no longer sits pending forever)")
    if outcome is None:
        return
    _check(outcome.capability_score == 38.0 and outcome.cost_per_task_usd == 5.09 and outcome.measured_at == "2026-09-21T12:00:00+00:00", "it carries the leaderboard's score, cost and reading time")
    _check(outcome.disagreement_note is not None and outcome.disagreement_note.startswith("single source: detail page unavailable (http_error") and "HTTP 404" in outcome.disagreement_note, "the note says it is a single source and why")
    _check(set(outcome.agreeing_observation_ids) == {"obs1", "obs2"} and not outcome.single_source_unconfirmed, "it cites only the leaderboard observations")


def scenario_an_unparsed_or_unfetched_detail_page_is_the_same_single_source_case() -> None:
    unparsed = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs3", _reading("artificial_analysis_model_detail_html", "intelligence_index", None, fetch_status="parse_error", value_text="no Intelligence Index on the detail page")),
    ]
    again = reconcile_cell(_CELL, unparsed)
    _check(again is not None and again.acceptance == CELL_ACCEPTANCE_ACCEPTED and "parse_error" in str(again.disagreement_note), "a detail page that no longer parses is the same single-source case")
    silent = reconcile_cell(_CELL, [_ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0))])
    _check(silent is not None and "was not fetched" in str(silent.disagreement_note), "with no detail reading at all the note says it was not fetched")


def scenario_single_source_within_tolerance_accepts_with_a_note() -> None:
    obs = [_ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0 + CAPABILITY_SCORE_TOLERANCE))]
    outcome = reconcile_cell(_CELL, obs, prior=PriorValue(38.0, None))
    _check(outcome is not None and outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, f"a single-source score exactly {CAPABILITY_SCORE_TOLERANCE} points from the prior accepted value still accepts")
    cost_obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.00 * (1 + COST_RELATIVE_TOLERANCE) - 0.01)),
    ]
    inside = reconcile_cell(_CELL, cost_obs, prior=PriorValue(38.0, 5.00))
    _check(inside is not None and inside.acceptance == CELL_ACCEPTANCE_ACCEPTED, "a single-source cost inside the relative tolerance accepts")


def scenario_single_source_beyond_tolerance_keeps_the_prior() -> None:
    obs = [_ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0 + CAPABILITY_SCORE_TOLERANCE + 0.1))]
    outcome = reconcile_cell(_CELL, obs, prior=PriorValue(38.0, None))
    _check(outcome is not None and outcome.single_source_unconfirmed and outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK, "a single-source score just past the tolerance is unconfirmed and keeps the prior (the store writes nothing)")
    _check(outcome is not None and outcome.capability_score is None and outcome.disagreement_note is not None and "prior value is kept" in outcome.disagreement_note, "and the note says the prior value is kept")
    cost_obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_leaderboard_html", "cost_per_task_usd", 5.00 * (1 + COST_RELATIVE_TOLERANCE) + 0.5)),
    ]
    moved = reconcile_cell(_CELL, cost_obs, prior=PriorValue(38.0, 5.00))
    _check(moved is not None and moved.single_source_unconfirmed, "a single-source cost past the relative tolerance is unconfirmed too")
    fresh = reconcile_cell(_CELL, obs)
    _check(fresh is not None and fresh.acceptance == CELL_ACCEPTANCE_ACCEPTED, "with no prior value there is nothing to compare with and the single source is accepted")


def scenario_the_tolerance_comparison_is_shared() -> None:
    _check(disagreement(38.0, 5.0, 38.0 + CAPABILITY_SCORE_TOLERANCE, 5.0) is None, "a score delta exactly at the tolerance is not a disagreement")
    moved = disagreement(38.0, 5.0, 41.0, 5.0)
    _check(moved is not None and moved[0] == 3.0 and moved[1] == 0.0, "a score delta past it is reported with its size")
    _check(disagreement(38.0, None, 38.0, 9.0) is None, "a cost with nothing to compare is not a disagreement")


def scenario_no_pending_cell_for_a_model_without_a_leaderboard_row() -> None:
    detail_only = [
        _ref("obs1", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "cost_per_task_usd", 5.09)),
    ]
    _check(reconcile_cell(_CELL, detail_only) is None, "a detail page alone is not evidence the model is still measured: no outcome, so no cell and no pending_crosscheck row")
    _check(reconcile_cell(_CELL, detail_only, prior=PriorValue(38.0, 5.09)) is None, "and it renews nothing even when the cell already exists")
    _check(reconcile_cell(_CELL, []) is None, "zero observations: no outcome, never a crash")
    failed_leaderboard = [_ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", None, fetch_status="parse_error"))]
    _check(reconcile_cell(_CELL, failed_leaderboard) is None, "a leaderboard reading that did not parse (a rejected out-of-range value) leaves no outcome, so the prior value stands")


def scenario_one_source_fetch_failed_does_not_count() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_model_detail_html", "intelligence_index", None, fetch_status="http_error")),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome is not None and "single source" in str(outcome.disagreement_note), "a failed fetch (fetch_status != ok) does not count toward the 2-source agreement; the cell is a named single source")


def scenario_seed_alone_never_satisfies_two_source() -> None:
    """The brief's explicit anti-pattern check: seed_table must never, by
    itself or paired with only one real fetch, satisfy the requirement."""
    obs = [
        _ref("obs1", _reading("seed_table", "intelligence_index", 38.0)),
        _ref("obs2", _reading("seed_table", "intelligence_index", 38.0)),
    ]
    # Two seed_table readings collapse to one distinct source_id by construction
    # (the reconciler's freshest-per-source-id dedup) -- this is the seed
    # attempting to corroborate itself and must not accept.
    _check(reconcile_cell(_CELL, obs) is None, "readings from the SAME non-leaderboard source (e.g. seed_table twice) are no evidence at all")
    mixed = [
        _ref("obs1", _reading("seed_table", "intelligence_index", 38.0)),
        _ref("obs2", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0)),
    ]
    outcome = reconcile_cell(_CELL, mixed)
    _check(outcome is not None and outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED and outcome.single_source_unconfirmed is False, "the seed plus a leaderboard reading are two distinct sources (unchanged rule)")


def scenario_stale_reading_ignored_for_freshest_pick() -> None:
    obs = [
        _ref("obs1", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 30.0, fetched_at="2026-09-01T00:00:00+00:00")),
        _ref("obs2", _reading("artificial_analysis_leaderboard_html", "intelligence_index", 38.0, fetched_at="2026-09-21T00:00:00+00:00")),
        _ref("obs3", _reading("artificial_analysis_model_detail_html", "intelligence_index", 38.0, fetched_at="2026-09-21T00:00:00+00:00")),
    ]
    outcome = reconcile_cell(_CELL, obs)
    _check(outcome is not None and outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED, "a stale older reading from the same source is superseded by the freshest one, not double-counted")
    _check(outcome is not None and outcome.capability_score == 38.0, "the freshest reading's value wins, not the stale one")
    _check(outcome is not None and "obs1" not in outcome.agreeing_observation_ids, "the stale superseded reading is never cited as agreeing evidence")


def main() -> int:
    print("model_capability_reconcile_smoke")
    scenario_agreeing_two_sources_accepts()
    scenario_score_within_tolerance_accepts()
    scenario_score_beyond_tolerance_conflicts()
    scenario_cost_beyond_tolerance_conflicts_even_with_score_agreement()
    scenario_a_missing_detail_page_accepts_the_single_source_with_a_note()
    scenario_an_unparsed_or_unfetched_detail_page_is_the_same_single_source_case()
    scenario_single_source_within_tolerance_accepts_with_a_note()
    scenario_single_source_beyond_tolerance_keeps_the_prior()
    scenario_the_tolerance_comparison_is_shared()
    scenario_no_pending_cell_for_a_model_without_a_leaderboard_row()
    scenario_one_source_fetch_failed_does_not_count()
    scenario_seed_alone_never_satisfies_two_source()
    scenario_stale_reading_ignored_for_freshest_pick()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
