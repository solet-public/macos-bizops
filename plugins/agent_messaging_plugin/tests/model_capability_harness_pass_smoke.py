#!/usr/bin/env python3
"""Smoke for the harness pass of the model catalog refresh (iss_0e805222, iss_52fdb8e5; rul_473f772f; design 3.3, 6.5).

The clock half of the cell lifecycle, with the harness lists and the leaderboard replaced by
hand-built discovery runs: a model a current `ok` list lacks starts `harness_unverified_since`;
after one window the refresh withdraws the cell, reversibly and with history; a relisted model
clears the clock and reinstates what the refresh withdrew; a manual withdrawal (null clock) is
never reversed; a harness-listed model the leaderboard dropped is never withdrawn; a cache-file,
unreadable, empty or old list moves nothing. It also pins the rules the same unit gives the
reconcile and the creation plan: a refresh never changes a withdrawn cell's acceptance (two
agreeing sources and a single source past tolerance), a derived launch id needs two sources and
starts its own clock, a read past the old ceiling neither suspends creation nor skips renewal,
a leaderboard row without a score takes no creation slot, and the capped/suspended label.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_harness_pass_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_fetch as fetch  # noqa: E402
from agent_messaging_plugin import model_capability_harness_pass as harness_pass  # noqa: E402
from agent_messaging_plugin import model_capability_reconcile as reconcile  # noqa: E402
from agent_messaging_plugin import model_capability_store as store  # noqa: E402
from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_WITHDRAWN,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
    get_model_capability_cell_schema,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_current = ""
_NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
_CLAUDE = "claude_code"
_CODEX = "codex"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(f"[{_current}] {label}")
        print(f"  FAIL  [{_current}] {label}")


def _typed(fake: RealShapeState) -> StateManagementInterface:
    return cast("StateManagementInterface", fake)


def _rows(state: RealShapeState) -> list[dict[str, Any]]:
    return state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL)


def _cell(state: RealShapeState, model: str, effort: str = "high", runtime: str = _CLAUDE) -> dict[str, Any]:
    return next(row for row in _rows(state) if row["runtime"] == runtime and row["model"] == model and row["effort"] == effort)


def _evidence(state: RealShapeState, metric: str) -> list[dict[str, Any]]:
    return [row for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION) if row["metric"] == metric]


def _preload(
    state: RealShapeState,
    model: str,
    effort: str = "high",
    *,
    runtime: str = _CLAUDE,
    acceptance: str = CELL_ACCEPTANCE_ACCEPTED,
    clock: str | None = None,
    score: float | None = 30.0,
    window: int | None = 72,
    measured_at: datetime | None = None,
    note: str | None = None,
) -> None:
    record: dict[str, Any] = {
        "provider": "anthropic" if runtime == _CLAUDE else "openai",
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "capability_score": score,
        "cost_per_task_usd": 1.0,
        "relative_cost_multiplier": None,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": (measured_at or _NOW).isoformat(),
        "accepted_at": _NOW.isoformat(),
        "acceptance": acceptance,
        "agreeing_observation_ids": [],
        "disagreement_note": note,
        "staleness_window_hours": window,
        "last_refresh_run_id": "run-before",
        "harness_unverified_since": clock,
    }
    state.upsert_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_CELL, "record": record, "conflict_columns": ["runtime", "model", "effort"]})


# --------------------------------------------------------------------------- hand-built discovery runs


def _summary(status: str = "ok", *, fetched_at: datetime | str | None = None) -> dict[str, object]:
    stamp = fetched_at.isoformat() if isinstance(fetched_at, datetime) else (_NOW.isoformat() if fetched_at is None else fetched_at)
    return {"status": status, "source": "fixture list", "detail": f"{status} fixture", "client_version": "9.9", "fetched_at": stamp, "models_offered": 1, "advisories": []}


def _row(index: int, creator: str = "Anthropic") -> fetch.LeaderboardRow:
    name = f"Fx Model {index:03d}"
    return fetch.LeaderboardRow(f"{name} (high)", name, "high", creator.lower(), "40", "$1.00", f"fx-{index:03d}", f"{name}|1M|{creator}|40|$1.00|a|b|c|Model|Providers|")


def _cell_for(model: str, effort: str = "high", *, runtime: str = _CLAUDE, derived: bool = False, index: int = 0) -> fetch.DiscoveredCell:
    provider = "anthropic" if runtime == _CLAUDE else "openai"
    note = "launch id derived from the leaderboard name; fixture list unreadable" if derived else None
    return fetch.DiscoveredCell(runtime, provider, model, effort, _row(index, provider.capitalize()), note, derived)


def _reading(cell: fetch.DiscoveredCell, source: str, metric: str, value: float | None, *, status: str = "ok", text: str | None = None) -> fetch.Reading:
    return fetch.Reading(source, cell.provider, cell.runtime, cell.model, cell.effort, metric, value, text, "fixture", status, _NOW.isoformat())


def _two_sources(cell: fetch.DiscoveredCell, *, score: float = 40.0, cost: float = 1.0) -> list[fetch.Reading]:
    return [_reading(cell, source, metric, value) for source in (fetch.SOURCE_LEADERBOARD_HTML, fetch.SOURCE_MODEL_DETAIL_HTML) for metric, value in (("intelligence_index", score), ("cost_per_task_usd", cost))]


def _one_source(cell: fetch.DiscoveredCell, *, score: float = 40.0, cost: float = 1.0) -> list[fetch.Reading]:
    leaderboard = [_reading(cell, fetch.SOURCE_LEADERBOARD_HTML, metric, value) for metric, value in (("intelligence_index", score), ("cost_per_task_usd", cost))]
    return [*leaderboard, _reading(cell, fetch.SOURCE_MODEL_DETAIL_HTML, "intelligence_index", None, status="http_error", text="HTTP 404")]


def _run(
    cells: Sequence[fetch.DiscoveredCell] = (),
    readings: Sequence[fetch.Reading] = (),
    *,
    claude: tuple[str, Sequence[str]] = ("ok", ()),
    codex: tuple[str, Sequence[str]] = ("ok", ()),
    fetched_at: datetime | str | None = None,
    leaderboard_ok: bool = True,
) -> fetch.DiscoveryRun:
    ordered = sorted(cells, key=lambda cell: cell.key)
    return fetch.DiscoveryRun(
        readings=tuple(readings),
        failures=(),
        sources_ok=(fetch.SOURCE_LEADERBOARD_HTML, fetch.SOURCE_MODEL_DETAIL_HTML),
        cells=tuple(ordered),
        leaderboard_ok=leaderboard_ok,
        leaderboard_rows=100 if leaderboard_ok else 0,
        harness={_CLAUDE: _summary(claude[0], fetched_at=fetched_at), _CODEX: _summary(codex[0], fetched_at=fetched_at)},
        harness_model_ids={_CLAUDE: tuple(claude[1]), _CODEX: tuple(codex[1])},
        alias_candidates=(),
        unmapped_creators=(),
        efforts_outside_vocabulary={},
        listed_not_launchable=(),
        creator_label_unmatched=(),
        advisories=(),
    )


def _pass(state: RealShapeState, run: fetch.DiscoveryRun, *, now: datetime = _NOW) -> harness_pass.HarnessPassReport:
    rows = store.read_cells_page(_typed(state), ceiling=None).rows
    return harness_pass.apply_harness_pass(_typed(state), store.open_refresh_run(_typed(state), trigger="manual"), run, rows, now=now)


def _reconcile(state: RealShapeState, run: fetch.DiscoveryRun, *, now: datetime = _NOW) -> store.RefreshReport:
    return store.reconcile_discovery(_typed(state), store.open_refresh_run(_typed(state), trigger="manual"), run, now=now, staleness_window_hours=72)


def _hours(count: float) -> str:
    return (_NOW - timedelta(hours=count)).isoformat()


def _withdraw_by_hand(state: RealShapeState, model: str, effort: str = "high") -> None:
    store.withdraw_cells(_typed(state), store.CellTransition(_CLAUDE, model, effort, "operator decision", CELL_ACCEPTANCE_ACCEPTED, "Harness-Pass-Smoke"), now=_NOW)


# --------------------------------------------------------------------------- the clock


def test_a_missing_model_starts_a_clock_and_keeps_serving() -> None:
    state = RealShapeState()
    _preload(state, "m-gone")
    _preload(state, "m-keep")
    report = _pass(state, _run(claude=("ok", ["m-keep"])))
    gone, keep = _cell(state, "m-gone"), _cell(state, "m-keep")
    _check(gone["harness_unverified_since"] == _NOW.isoformat() and gone["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "an absent model's clock starts at the run time and the cell keeps serving")
    _check(keep["harness_unverified_since"] is None, "a listed model starts no clock")
    _check(report.clock_started == ["claude_code/m-gone@high"] and report.withdrawn == [], "the report names the started clock and no withdrawal")
    started = _evidence(state, harness_pass.HARNESS_CLOCK_METRIC)
    _check(len(started) == 1 and started[0]["value_text"].startswith("started") and started[0]["source_id"] == "harness_claude_code", "one harness_clock observation records the start")
    _check(any("m-gone" in line and "clock started" in line for line in report.advisories), "an advisory names the model whose clock started")


def test_the_grace_window_keeps_the_cell_servable() -> None:
    state = RealShapeState()
    _preload(state, "m-in-window", clock=_hours(71.5))
    _preload(state, "m-at-window", "medium", clock=_hours(72))
    report = _pass(state, _run(claude=("ok", ["m-other"])))
    _check(_cell(state, "m-in-window")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "71.5 hours of absence withdraws nothing")
    _check(_cell(state, "m-at-window", "medium")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "exactly one window of absence is still inside the window")
    _check(report.withdrawn == [] and report.clock_started == [] and _cell(state, "m-in-window")["harness_unverified_since"] == _hours(71.5), "a running clock is neither restarted nor moved")


def test_after_one_window_the_cell_is_withdrawn_with_history() -> None:
    state = RealShapeState()
    _preload(state, "m-gone", clock=_hours(73), score=33.0)
    report = _pass(state, _run(claude=("ok", ["m-other"])))
    row = _cell(state, "m-gone")
    _check(row["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and report.withdrawn == ["claude_code/m-gone@high"], "after one window of absence the refresh withdraws the cell")
    _check(row["capability_score"] == 33.0 and row["cost_per_task_usd"] == 1.0 and row["measured_at"] == _NOW.isoformat() and row["accepted_at"] == _NOW.isoformat(), "the score, cost, measured_at and accepted_at are kept")
    _check(row["harness_unverified_since"] == _hours(73), "the clock stays set: it marks the withdrawal as the refresh's own")
    _check(str(row["disagreement_note"]).startswith("withdrawn ") and "has not listed m-gone since" in str(row["disagreement_note"]), "the note says who withdrew it and why")
    changes = _evidence(state, harness_pass.ACCEPTANCE_CHANGE_METRIC)
    _check(len(changes) == 1 and changes[0]["value_text"] == "accepted->withdrawn: harness_absent" and changes[0]["source_id"] == "harness_claude_code", "one acceptance_change observation records the move")
    _check(row["last_refresh_run_id"] == changes[0]["refresh_run_id"], "the cell is stamped with the run that moved it")
    _check(any("m-gone" in line and "withdrawn" in line for line in report.advisories), "an advisory names the withdrawn model")


def test_a_relisted_model_clears_the_clock_and_reinstates() -> None:
    state = RealShapeState()
    _preload(state, "m-auto", clock=_hours(100), acceptance=CELL_ACCEPTANCE_WITHDRAWN, score=33.0, note="withdrawn earlier: harness has not listed m-auto")
    _preload(state, "m-serving", clock=_hours(10))
    report = _pass(state, _run(claude=("ok", ["m-auto", "m-serving"])))
    auto = _cell(state, "m-auto")
    _check(auto["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and auto["harness_unverified_since"] is None and auto["disagreement_note"] is None, "a relisted model's automatic withdrawal is reversed: accepted, clock cleared, withdrawal note gone")
    _check(auto["capability_score"] == 33.0, "with its retained score")
    _check(report.reinstated == ["claude_code/m-auto@high"] and report.clock_cleared == ["claude_code/m-serving@high"], "the report names the reinstated cell and the cleared clock")
    serving = _cell(state, "m-serving")
    _check(serving["harness_unverified_since"] is None and serving["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "a serving cell whose model is listed again has its clock cleared")
    texts = sorted(str(row["value_text"]) for row in _evidence(state, harness_pass.ACCEPTANCE_CHANGE_METRIC) + _evidence(state, harness_pass.HARNESS_CLOCK_METRIC))
    _check(texts == ["cleared: harness_relisted", "withdrawn->accepted: harness_relisted"], f"both moves are recorded in the history (got {texts})")


def test_a_manual_withdrawal_is_not_reversed_by_the_refresh() -> None:
    state = RealShapeState()
    _preload(state, "m-human", score=9.0, clock=_hours(5))
    _withdraw_by_hand(state, "m-human")
    withdrawn = _cell(state, "m-human")
    _check(withdrawn["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and withdrawn["harness_unverified_since"] is None, "setup: the person's withdrawal leaves the clock null")
    cell = _cell_for("m-human")
    listed = _run([cell], _two_sources(cell, score=44.0), claude=("ok", ["m-human"]))
    pass_report = _pass(state, listed)
    refresh = _reconcile(state, listed)
    row = _cell(state, "m-human")
    _check(row["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and pass_report.reinstated == [], "a listed model with fresh readings does not bring a manual withdrawal back")
    _check(row["capability_score"] == 44.0 and row["harness_unverified_since"] is None, "the refresh may refresh the retained values; the clock stays null")
    _check(refresh.result_fields["withdrawn_left"] == ["claude_code/m-human@high"] and refresh.counts["cells_accepted"] == 0, "the run names the cell it left withdrawn and counts no acceptance")
    absent = _run(claude=("ok", ["m-other"]))
    later = _pass(state, absent, now=_NOW + timedelta(hours=200))
    _check(_cell(state, "m-human")["harness_unverified_since"] is None and later.clock_started == [], "an absent model's manual withdrawal never starts a clock either")


def test_withdraw_clears_a_running_clock() -> None:
    state = RealShapeState()
    _preload(state, "m-running", clock=_hours(40))
    _withdraw_by_hand(state, "m-running")
    _check(_cell(state, "m-running")["harness_unverified_since"] is None, "withdrawing a cell whose clock was running nulls the clock, so the refresh can never take the decision back")
    _check(_cell(state, "m-running")["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, "and the cell is withdrawn")
    store.reinstate_cells(_typed(state), store.CellTransition(_CLAUDE, "m-running", "high", "operator changed their mind", CELL_ACCEPTANCE_WITHDRAWN, "Harness-Pass-Smoke"), now=_NOW)
    _check(_cell(state, "m-running")["harness_unverified_since"] is None, "reinstating by hand does not set a clock")


def test_a_withdrawn_cell_stays_withdrawn_with_two_agreeing_sources() -> None:
    state = RealShapeState()
    _preload(state, "m-held", score=9.0, acceptance=CELL_ACCEPTANCE_WITHDRAWN, note="withdrawn by hand")
    cell = _cell_for("m-held")
    report = _reconcile(state, _run([cell], _two_sources(cell, score=20.0), claude=("ok", ["m-held"])))
    row = _cell(state, "m-held")
    _check(row["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and row["disagreement_note"] == "withdrawn by hand", "two agreeing fresh sources leave a withdrawn cell withdrawn, with its withdrawal note")
    _check(row["capability_score"] == 20.0 and report.counts["cells_accepted"] == 0 and report.result_fields["withdrawn_left"] == ["claude_code/m-held@high"], "the retained value is refreshed and the cell is named as left withdrawn")


def test_a_withdrawn_cell_stays_withdrawn_on_a_single_source_beyond_tolerance() -> None:
    state = RealShapeState()
    _preload(state, "m-held", score=9.0, acceptance=CELL_ACCEPTANCE_WITHDRAWN, note="withdrawn by hand")
    cell = _cell_for("m-held")
    _reconcile(state, _run([cell], _one_source(cell, score=29.0), claude=("ok", ["m-held"])))
    row = _cell(state, "m-held")
    _check(row["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and row["capability_score"] == 9.0, "a lone reading 20 points from the retained score neither reaccepts the cell nor rewrites its value")
    near = _cell_for("m-near")
    _preload(state, "m-near", score=9.0, acceptance=CELL_ACCEPTANCE_WITHDRAWN)
    _reconcile(state, _run([near], _one_source(near, score=10.0), claude=("ok", ["m-near"])))
    _check(_cell(state, "m-near")["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and _cell(state, "m-near")["capability_score"] == 10.0, "a lone reading inside tolerance refreshes the value and still leaves the cell withdrawn")


def test_a_harness_listed_unscored_model_keeps_its_score_and_is_never_withdrawn() -> None:
    state = RealShapeState()
    stale = _NOW - timedelta(days=10)
    _preload(state, "m-unscored", score=33.0, measured_at=stale)
    _preload(state, "m-unscored", "low", score=20.0, measured_at=stale, clock=_hours(100))
    run = _run(claude=("ok", ["m-unscored"]))
    report = _pass(state, run)
    refresh = _reconcile(state, run)
    first, second = _cell(state, "m-unscored"), _cell(state, "m-unscored", "low")
    _check(first["harness_unverified_since"] is None and second["harness_unverified_since"] is None, "a model the harness lists has no clock, however long the leaderboard has not scored it")
    _check(first["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and second["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and report.withdrawn == [] and report.clock_started == [], "it is never withdrawn and starts no clock")
    _check(first["capability_score"] == 33.0 and second["capability_score"] == 20.0, "and keeps its last score")
    _check([item["model"] for item in refresh.result_fields["unrenewable_stale"]] == ["m-unscored"], "the refresh names it under unrenewable_stale")


def test_a_cache_file_list_is_stale_and_cannot_start_a_clock() -> None:
    state = RealShapeState()
    _preload(state, "c-fresh", runtime=_CODEX)
    _preload(state, "c-expired", runtime=_CODEX, clock=_hours(200))
    report = _pass(state, _run(codex=("degraded", ["c-other"])))
    _check(_cell(state, "c-fresh", runtime=_CODEX)["harness_unverified_since"] is None, "a cache-file list starts no clock for a model it lacks")
    _check(_cell(state, "c-expired", runtime=_CODEX)["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and report.withdrawn == [], "and withdraws nothing, however old the clock")
    _check(any("codex harness list is degraded" in line for line in report.advisories), "the pass says loudly that it moved nothing and why")
    listing = _pass(state, _run(codex=("degraded", ["c-expired"])))
    _check(_cell(state, "c-expired", runtime=_CODEX)["harness_unverified_since"] == _hours(200) and listing.clock_cleared == [], "a degraded list that does list the model does not clear its clock either")


def test_an_empty_list_is_degraded_and_withdraws_nothing() -> None:
    state = RealShapeState()
    _preload(state, "m-expired", clock=_hours(500))
    _preload(state, "m-fresh", "low")
    for status in ("empty", "unreadable"):
        report = _pass(state, _run(claude=(status, [])))
        _check(_cell(state, "m-expired")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and _cell(state, "m-fresh", "low")["harness_unverified_since"] is None, f"a {status} list withdraws nothing and starts nothing")
        _check(report.withdrawn == [] and report.clock_started == [] and any(status in line for line in report.advisories), f"and the pass names the {status} list")


def test_an_old_or_undated_ok_list_moves_no_clock() -> None:
    state = RealShapeState()
    _preload(state, "m-expired", clock=_hours(500), window=168)
    for label, fetched in (("eight days old", _NOW - timedelta(days=8)), ("undated", "")):
        report = _pass(state, _run(claude=("ok", ["m-other"]), fetched_at=fetched))
        _check(_cell(state, "m-expired")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and report.withdrawn == [], f"an ok list that is {label} cannot prove a model absent")
    fresh = _pass(state, _run(claude=("ok", ["m-other"]), fetched_at=_NOW - timedelta(days=6)))
    _check(fresh.withdrawn == ["claude_code/m-expired@high"], "a six-day-old ok list is still current enough")


def test_unknown_window_preserves_cell_and_reports_uncertainty() -> None:
    state = RealShapeState()
    _preload(state, "m-unknown", clock=_hours(500), window=None)
    report = _pass(state, _run(claude=("ok", ["m-other"])))
    row = _cell(state, "m-unknown")
    _check(row["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and not report.withdrawn, "unknown window never withdraws")
    _check(any("m-unknown" in note and "unknown" in note for note in report.advisories), "unknown window is named")
    _check(harness_pass.refresh_outlook(row, now=_NOW)["undone_by_next_refresh"] is None, "outlook is unknown")


def test_an_unreadable_clock_restarts_loudly() -> None:
    state = RealShapeState()
    _preload(state, "m-garbled", clock="not a timestamp")
    report = _pass(state, _run(claude=("ok", ["m-other"])))
    _check(_cell(state, "m-garbled")["harness_unverified_since"] == _NOW.isoformat() and report.clock_started == ["claude_code/m-garbled@high"], "an unreadable clock is restarted at the run time, never a refusal")
    _check("unreadable" in str(_evidence(state, harness_pass.HARNESS_CLOCK_METRIC)[0]["value_text"]), "and the history says the stored clock was unreadable")


def test_a_cell_changed_after_the_read_is_skipped_loudly() -> None:
    state = RealShapeState()
    _preload(state, "m-raced", clock=_hours(100))
    rows = store.read_cells_page(_typed(state), ceiling=None).rows
    _cell(state, "m-raced")["updated_at"] = "2099-01-01T00:00:00+00:00"  # another writer touched the row after it was read
    report = harness_pass.apply_harness_pass(_typed(state), store.open_refresh_run(_typed(state), trigger="manual"), _run(claude=("ok", ["m-other"])), rows, now=_NOW)
    _check(_cell(state, "m-raced")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and report.withdrawn == [], "a cell another writer changed is not withdrawn on a stale read")
    _check(any("m-raced" in line and "changed after" in line for line in report.advisories) and _evidence(state, harness_pass.ACCEPTANCE_CHANGE_METRIC) == [], "the skip is named in an advisory and writes no evidence")


def test_an_auto_withdrawn_cell_is_excluded_by_the_selector_as_not_accepted() -> None:
    state = RealShapeState()
    _preload(state, "m-cheap", "medium", runtime=_CODEX, score=45.0, clock=_hours(100))
    _preload(state, "m-dear", "medium", runtime=_CODEX, score=45.0)
    state.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_CELL, "filters": {"model": "m-dear"}}, {"cost_per_task_usd": 2.0})
    params = {"required_score": 40, "billing_objective": "metered_usd"}
    before = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _pass(state, _run(codex=("ok", ["m-dear"])), now=_NOW)  # m-cheap is absent and its clock ran out
    after = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _check(before["selected"]["model"] == "m-cheap" and after["selected"]["model"] == "m-dear", "the automatically withdrawn cell is no longer selected")
    _check(after["excluded"]["not_accepted"] == before["excluded"]["not_accepted"] + 1, "it is excluded under the existing not_accepted counter")
    _check(_cell(state, "m-cheap", "medium", runtime=_CODEX)["capability_score"] == 45.0, "and it keeps its score")


# --------------------------------------------------------------------------- creation and the derived launch id


def test_a_derived_cell_with_two_sources_is_created_with_its_clock_running() -> None:
    state = RealShapeState()
    cell = _cell_for("m-derived", derived=True)
    report = _reconcile(state, _run([cell], _two_sources(cell), claude=("unreadable", [])))
    row = _cell(state, "m-derived")
    _check(report.result_fields["created"] == ["claude_code/m-derived@high"] and row["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "a derived launch id with two agreeing sources is created")
    _check(row["harness_unverified_since"] is not None and "derived from the leaderboard name" in str(row["disagreement_note"]), "its clock starts at creation and its note flags the derived id")
    plain = _cell_for("m-plain", index=1)
    _reconcile(state, _run([plain], _two_sources(plain), claude=("ok", ["m-plain"])))
    _check(_cell(state, "m-plain").get("harness_unverified_since") is None, "a launch id an ok list vouches for starts no clock")


def _lonely_and_solid() -> tuple[list[fetch.DiscoveredCell], list[fetch.DiscoveredCell]]:
    """31 derived cells whose names sort first (so they would take the slots), and 30 ordinary cells."""
    lonely = [_cell_for(f"a-lonely-{index:02d}", derived=True, index=index) for index in range(31)]
    solid = [_cell_for(f"z-solid-{index:02d}", index=100 + index) for index in range(30)]
    return lonely, solid


def test_a_derived_cell_with_one_source_is_not_created_and_takes_no_slot() -> None:
    state = RealShapeState()
    lonely, solid = _lonely_and_solid()
    readings = [reading for cell in lonely for reading in _one_source(cell)] + [reading for cell in solid for reading in _two_sources(cell)]
    report = _reconcile(state, _run([*lonely, *solid], readings, claude=("ok", [cell.model for cell in solid])))
    created = report.result_fields["created"]
    _check(len(created) == 30 and all("z-solid" in key for key in created), "the 30 creation slots go to cells with two sources; one-source derived cells take none")
    _check(len(report.result_fields["awaiting_second_source"]) == 31 and report.result_fields["deferred"] == [], "the 31 derived cells wait for the detail page and are not deferred")
    _check(not any("a-lonely" in str(row["model"]) for row in _rows(state)), "no one-source derived cell was written")
    _check("wait for the detail page" in str(report.note), "the run note says they wait")


def test_waiting_derived_cells_are_created_when_the_detail_page_answers() -> None:
    state = RealShapeState()
    lonely, _solid = _lonely_and_solid()
    run = _run(lonely, [reading for cell in lonely for reading in _two_sources(cell)], claude=("unreadable", []))
    again = _reconcile(state, run)
    _check(len(again.result_fields["created"]) == 30 and len(again.result_fields["deferred"]) == 1, "once the detail page answers they are created under the usual cap")
    _check(again.result_fields["awaiting_second_source"] == [], "and nothing waits any more")


def test_degraded_mode_never_refuses_renewal() -> None:
    state = RealShapeState()
    _preload(state, "m-known", score=30.0)
    cell = _cell_for("m-known", derived=True)
    report = _reconcile(state, _run([cell], _one_source(cell, score=31.0), claude=("unreadable", [])))
    _check(_cell(state, "m-known")["capability_score"] == 31.0 and report.counts["cells_accepted"] == 1, "an existing cell is renewed on one source inside tolerance in degraded mode")
    _check(report.result_fields["awaiting_second_source"] == [], "and is not mistaken for a new derived cell")


def test_a_derived_cell_never_confirmed_is_withdrawn_one_window_later() -> None:
    state = RealShapeState()
    cell = _cell_for("m-guess", derived=True)
    _reconcile(state, _run([cell], _two_sources(cell), claude=("unreadable", [])))
    created_at = datetime.fromisoformat(str(_cell(state, "m-guess")["harness_unverified_since"]))
    soon = _pass(state, _run(claude=("ok", ["m-other"]), fetched_at=created_at + timedelta(hours=10)), now=created_at + timedelta(hours=10))
    _check(soon.withdrawn == [] and _cell(state, "m-guess")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "an ok list that lacks the derived id does not withdraw it inside the window")
    late_at = created_at + timedelta(hours=73)
    late = _pass(state, _run(claude=("ok", ["m-other"]), fetched_at=late_at), now=late_at)
    _check(late.withdrawn == ["claude_code/m-guess@high"], "one window after creation an id no ok list ever confirmed is withdrawn")
    confirmed = _cell_for("m-sure", derived=True, index=2)
    _reconcile(state, _run([confirmed], _two_sources(confirmed), claude=("unreadable", [])))
    ok_at = datetime.now(UTC) + timedelta(hours=1)
    cleared = _pass(state, _run(claude=("ok", ["m-sure"]), fetched_at=ok_at), now=ok_at)
    _check(cleared.clock_cleared == ["claude_code/m-sure@high"] and _cell(state, "m-sure")["harness_unverified_since"] is None, "an ok list that confirms the derived id clears the clock")


# --------------------------------------------------------------------------- the catalog read, the creation plan, the schema


def _state_with_history_past_the_ceiling() -> RealShapeState:
    state = RealShapeState()
    for index in range(store.CATALOG_READ_CEILING + 10):
        _preload(state, f"a-history-{index:04d}", acceptance=CELL_ACCEPTANCE_WITHDRAWN, clock=None)
    _preload(state, "z-late", score=30.0, measured_at=_NOW - timedelta(days=5))
    return state


def test_creation_and_renewal_continue_past_the_old_read_ceiling() -> None:
    state = _state_with_history_past_the_ceiling()
    _check(store.read_cells_page(_typed(state)).truncated, "setup: the default read stops at the ceiling and z-late sorts after the cut")
    fresh = [_cell_for(f"m-new-{index}", index=index) for index in range(3)]
    late = _cell_for("z-late", index=50)
    run = _run([*fresh, late], [reading for cell in (*fresh, late) for reading in _two_sources(cell, score=44.0)], claude=("ok", [cell.model for cell in (*fresh, late)]))
    report = _reconcile(state, run)
    _check(report.result_fields["cells_created"] == 3 and report.result_fields["creation_suspended"] is False, "history past the read ceiling suspends no creation: three cells are created")
    _check(_cell(state, "z-late")["capability_score"] == 44.0 and report.counts["cells_accepted"] == 4, "a cell past the old cut is renewed rather than recreated or skipped")
    _check(len([row for row in _rows(state) if row["model"] == "z-late"]) == 1, "and there is still one row for it")
    _check(all("catalog read stopped" not in line for line in report.result_fields["advisories"]), "no advisory claims a truncated read")


def test_a_leaderboard_row_with_no_score_whose_detail_has_one_creates_no_cell_and_uses_no_slot() -> None:
    unscored = _cell_for("a-unscored", index=0)
    scored = [_cell_for(f"m-scored-{index:02d}", index=index + 1) for index in range(31)]
    readings: dict[reconcile.CellKey, list[fetch.Reading]] = {unscored.key: [_reading(unscored, fetch.SOURCE_LEADERBOARD_HTML, "intelligence_index", None, status="parse_error"), _reading(unscored, fetch.SOURCE_MODEL_DETAIL_HTML, "intelligence_index", 41.0)]}
    for cell in scored:
        readings[cell.key] = _two_sources(cell)
    plan = reconcile.plan_creation([unscored, *scored], set(), live_rows=0, readings_by_key=readings)
    _check(unscored not in plan.create and len(plan.create) == 30 and len(plan.deferred) == 1, "a row with no leaderboard score creates no cell and costs no slot: 30 created, only 1 deferred")


def test_the_label_says_which_limit_bound() -> None:
    cells = [_cell_for(f"m-label-{index:02d}", index=index) for index in range(40)]
    readings = {cell.key: _two_sources(cell) for cell in cells}
    at_cap = reconcile.plan_creation(cells, set(), live_rows=reconcile.CREATION_LIVE_CEILING - reconcile.CREATION_CAP_PER_RUN, readings_by_key=readings)
    _check(at_cap.capped is True and at_cap.suspended is False and len(at_cap.create) == 30, "headroom equal to the cap: the cap is the binding limit")
    below = reconcile.plan_creation(cells, set(), live_rows=reconcile.CREATION_LIVE_CEILING - 10, readings_by_key=readings)
    _check(below.suspended is True and below.capped is False and len(below.create) == 10, "headroom below the cap: the ceiling is the binding limit, and cells were still created")
    open_plan = reconcile.plan_creation(cells[:5], set(), live_rows=0, readings_by_key=readings)
    _check(open_plan.capped is False and open_plan.suspended is False and not open_plan.deferred, "nothing deferred: neither label is set")


def test_the_cell_table_declares_the_clock_column() -> None:
    columns = get_model_capability_cell_schema().columns
    clock = columns["harness_unverified_since"]
    # Operator enablement adds the enabled column.
    _check(len(columns) == 18, f"the cell table declares 18 columns (got {len(columns)})")
    _check(clock.not_null is False and clock.type.name == "TEXT", "harness_unverified_since is a nullable text column")
    _check("harness_unverified_since" not in {"provider", "runtime", "model", "effort"}, "and it is not part of the cell key")
    _check(next(iter(get_model_capability_cell_schema().indexes)).columns == ["runtime", "model", "effort"], "the unique key is still (runtime, model, effort)")


def test_a_renewal_keeps_the_clock_it_does_not_own() -> None:
    state = RealShapeState()
    _preload(state, "m-renew", clock=_hours(5))
    cell = _cell_for("m-renew")
    _reconcile(state, _run([cell], _two_sources(cell, score=31.0), claude=("ok", ["m-renew"])))
    _check(_cell(state, "m-renew")["capability_score"] == 31.0 and _cell(state, "m-renew")["harness_unverified_since"] == _hours(5), "a reconcile upsert renews the values and leaves the clock to the harness pass")


_TESTS: tuple[Callable[[], None], ...] = (
    test_a_missing_model_starts_a_clock_and_keeps_serving,
    test_the_grace_window_keeps_the_cell_servable,
    test_after_one_window_the_cell_is_withdrawn_with_history,
    test_a_relisted_model_clears_the_clock_and_reinstates,
    test_a_manual_withdrawal_is_not_reversed_by_the_refresh,
    test_withdraw_clears_a_running_clock,
    test_a_withdrawn_cell_stays_withdrawn_with_two_agreeing_sources,
    test_a_withdrawn_cell_stays_withdrawn_on_a_single_source_beyond_tolerance,
    test_a_harness_listed_unscored_model_keeps_its_score_and_is_never_withdrawn,
    test_a_cache_file_list_is_stale_and_cannot_start_a_clock,
    test_an_empty_list_is_degraded_and_withdraws_nothing,
    test_an_old_or_undated_ok_list_moves_no_clock,
    test_unknown_window_preserves_cell_and_reports_uncertainty,
    test_an_unreadable_clock_restarts_loudly,
    test_a_cell_changed_after_the_read_is_skipped_loudly,
    test_an_auto_withdrawn_cell_is_excluded_by_the_selector_as_not_accepted,
    test_a_derived_cell_with_two_sources_is_created_with_its_clock_running,
    test_a_derived_cell_with_one_source_is_not_created_and_takes_no_slot,
    test_waiting_derived_cells_are_created_when_the_detail_page_answers,
    test_degraded_mode_never_refuses_renewal,
    test_a_derived_cell_never_confirmed_is_withdrawn_one_window_later,
    test_creation_and_renewal_continue_past_the_old_read_ceiling,
    test_a_leaderboard_row_with_no_score_whose_detail_has_one_creates_no_cell_and_uses_no_slot,
    test_the_label_says_which_limit_bound,
    test_the_cell_table_declares_the_clock_column,
    test_a_renewal_keeps_the_clock_it_does_not_own,
)


def main() -> int:
    global _current
    print("model_capability_harness_pass_smoke")
    for test in _TESTS:
        _current = test.__name__
        print(f"\n{_current}")
        test()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
