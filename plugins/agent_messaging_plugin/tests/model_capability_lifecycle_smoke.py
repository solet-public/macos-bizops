#!/usr/bin/env python3
"""Lifecycle-verb smoke for the model capability catalog cell (iss_0e805222 U5, rul_2bff72b8).

Proves the retire / reopen / history half of the cell lifecycle and, with the harness
pass, its clock half (an automatic withdrawal after the grace window, its reversal by a
relisting, a reinstatement the next pass undoes, and a partial move reported exactly):

* create, read, correct, retire and reopen in ONE scenario: a recorded cell is
  withdrawn, keeps its score, is still read by the selector and excluded there
  as ``not_accepted``, fails a selection receipt issued before the withdrawal
  with ``selection_receipt_mismatch``, and is served again once reinstated;
* a stale premise is refused for both verbs (the caller's ``expected_acceptance``
  no longer holds, and a writer that moves the row between the read and the
  write), and a refused request moves nothing;
* every refusal and validation branch, including the escape for a cell whose
  effort is outside the ladder, and the all-efforts form;
* the reinstate answer says whether the next refresh would withdraw the cell again;
* the history verb pages the append-only observations in order with the
  state layer's ``after`` cursor.

PURE UNIT, offline: real-shape state fake, no database, network, or model turn.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import model_capability_harness_pass_smoke as clock_fixtures  # noqa: E402  -- hand-built discovery runs for the clock half
from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import StateOperationError  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import TierSelectionError  # noqa: E402
from agent_messaging_plugin.model_capability_store import (  # noqa: E402
    CatalogError,
    CellTransition,
    load_seed_table,
    read_cells,
    reinstate_cells,
    seed_catalog,
    selection_catalog,
    withdraw_cells,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    CELL_ACCEPTANCE_WITHDRAWN,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
    get_model_capability_cell_schema,
    get_model_capability_observation_schema,
    get_model_capability_refresh_run_schema,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
_STANDARDIZER = {"name", "id", "external_id", "namespace", "created_at", "updated_at", "created_by", "updated_by", "is_deleted"}
_ACTOR = "Lifecycle-Smoke"
_SELECT = {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "review"}
_SOL_MEDIUM = {"runtime": "codex", "model": "gpt-5.6-sol", "effort": "medium"}
_GPT6_SOL_HIGH: dict[str, Any] = {
    "runtime": "codex",
    "model": "gpt-6-sol",
    "effort": "high",
    "capability_score": 43,
    "cost_per_task_usd": 0.37,
    "source_ref": "https://artificialanalysis.ai/models/gpt-6-sol-high",
}


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _code(callback: object) -> str:
    try:
        assert callable(callback)
        callback()
    except (CatalogError, TierSelectionError) as exc:
        return exc.code
    return ""


def _typed(fake: RealShapeState) -> StateManagementInterface:
    """The fake IS the real shape; the cast is for pyright only."""
    return cast("StateManagementInterface", fake)


def _rows(state: RealShapeState, table: str) -> list[dict[str, Any]]:
    return state.rows(AGENT_ROLE_BINDING_NAMESPACE, table)


def _cell_row(state: RealShapeState, model: str, effort: str) -> dict[str, Any]:
    return next(row for row in _rows(state, TABLE_MODEL_CAPABILITY_CELL) if row["model"] == model and row["effort"] == effort)


def _accepted_state() -> RealShapeState:
    """A seeded catalog with every cell accepted, standing in for a cross-checked refresh run."""
    state = RealShapeState()
    seed_catalog(_typed(state), seed=load_seed_table(), staleness_window_hours=72)
    for row in _rows(state, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
        row["accepted_at"] = (_NOW - timedelta(hours=1)).isoformat()
        row["measured_at"] = (_NOW - timedelta(hours=1)).isoformat()
        row["last_refresh_run_id"] = "mcr-test-accepted"
    return state


def _params(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "runtime": "codex",
        "model": "gpt-6-sol",
        "effort": "high",
        "reason": "the model was pulled from the launcher",
        "expected_acceptance": CELL_ACCEPTANCE_ACCEPTED,
        "actor": _ACTOR,
    }
    return {**base, **over}


def _reinstate_params(**over: Any) -> dict[str, Any]:
    return _params(reason="the launcher lists it again", expected_acceptance=CELL_ACCEPTANCE_WITHDRAWN, **over)


def _recorded_state() -> RealShapeState:
    state = RealShapeState()
    verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "staleness_window_hours": 72})
    return state


def _changes(state: RealShapeState) -> list[dict[str, Any]]:
    return [row for row in _rows(state, TABLE_MODEL_CAPABILITY_OBSERVATION) if row["metric"] == "acceptance_change"]


def _verify(state: RealShapeState, receipt: dict[str, Any]) -> str:
    return _code(
        lambda: verbs.verify_selection_receipt(
            _typed(state),
            difficulty_score=38,
            selection_receipt=receipt,
            dispatch_kind="review",
            scope_tags=(),
            agent_runtime="codex",
            model="gpt-5.6-sol",
            effort="medium",
            now=_NOW,
        )
    )


def _holds(*conditions: object) -> bool:
    """All conditions true; a conjunction written as arguments, so one check line stays a single branch."""
    return all(conditions)


def _check_retired(state: RealShapeState, created: dict[str, Any]) -> None:
    withdrawn = verbs.withdraw_model_capability_cell(_typed(state), _params())
    cell = withdrawn["cells"][0]
    _check(
        _holds(withdrawn["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, cell["from_acceptance"] == "accepted", cell["to_acceptance"] == "withdrawn"),
        "retire: the answer names the move accepted -> withdrawn",
    )
    row = _cell_row(state, "gpt-6-sol", "high")
    _check(row["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, "retire: the stored cell is withdrawn")
    _check(
        _holds(row["capability_score"] == 43.0, row["cost_per_task_usd"] == 0.37, row["measured_at"] == created["measured_at"], row["accepted_at"] == created["accepted_at"]),
        "retire: score, cost, measured_at and accepted_at are kept",
    )
    note = str(row["disagreement_note"])
    _check(_holds(_ACTOR in note, "pulled from the launcher" in note), "retire: the cell note carries the reason and the actor")
    _check(_holds(note.startswith("withdrawn "), note.endswith(f"(by {_ACTOR})"), ": the model was pulled from the launcher (by" in note), "retire: the note reads 'withdrawn <stamp>: <reason> (by <actor>)'")
    _check(_holds(row["last_refresh_run_id"] == withdrawn["run_id"], row["last_refresh_run_id"] != created["last_refresh_run_id"]), "retire: the moved cell is stamped with the run row that moved it")
    listed = verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=True, now=_NOW)
    shown = next(c for c in listed["cells"] if c["model"] == "gpt-6-sol")
    _check(_holds(shown["acceptance"] == "withdrawn", shown["servable"] is False), "read: the catalog shows the cell unserved with include_unaccepted")
    default = verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=False, now=_NOW)
    _check(_holds(default["cells"] == [], default["total_cells"] == 1), "read: the default read hides the withdrawn cell but still counts the row")


def _check_reopened(state: RealShapeState, created: dict[str, Any]) -> None:
    reopened = verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())
    _check(_holds(reopened["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, reopened["cells"][0]["capability_score"] == 43.0), "reopen: the answer reports the retained score")
    back = _cell_row(state, "gpt-6-sol", "high")
    _check(
        _holds(back["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, back["capability_score"] == 43.0, back["accepted_at"] == created["accepted_at"], back["disagreement_note"] is None),
        "reopen: accepted again with the retained values, the withdrawal note cleared",
    )
    _check(_holds(back["last_refresh_run_id"] == reopened["run_id"], back["last_refresh_run_id"] != created["last_refresh_run_id"]), "reopen: the cell is stamped with the run row that reopened it")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 40, "billing_objective": "metered_usd"}, now=datetime.now(UTC))
    _check(picked["selected"]["model"] == "gpt-6-sol", "reopen: the selector serves the cell again")


def test_create_read_retire_reopen_in_one_scenario() -> None:
    state = _recorded_state()
    created = dict(_cell_row(state, "gpt-6-sol", "high"))
    _check(_holds(created["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, created["capability_score"] == 43.0), "create: the recorded cell is accepted with its score")
    _check_retired(state, created)
    _check_reopened(state, created)


def _check_declared_columns(state: RealShapeState) -> None:
    declared = {
        TABLE_MODEL_CAPABILITY_CELL: set(get_model_capability_cell_schema().columns),
        TABLE_MODEL_CAPABILITY_OBSERVATION: set(get_model_capability_observation_schema().columns),
        TABLE_MODEL_CAPABILITY_REFRESH_RUN: set(get_model_capability_refresh_run_schema().columns),
    }
    for table, columns in declared.items():
        stray = {key for row in _rows(state, table) for key in row} - columns - _STANDARDIZER
        _check(not stray, f"{table}: no undeclared columns written ({sorted(stray)})")


def test_run_rows_and_acceptance_change_evidence() -> None:
    state = _recorded_state()
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())
    runs = [r for r in _rows(state, TABLE_MODEL_CAPABILITY_REFRESH_RUN) if r["trigger"] == "manual"]
    _check(_holds(len(runs) == 3, all(r["status"] == "completed" for r in runs)), "each call writes one completed manual run row (record, withdraw, reinstate)")
    changes = _changes(state)
    _check([c["value_text"] for c in changes] == ["accepted->withdrawn: manual", "withdrawn->accepted: manual"], "one acceptance_change observation per move, naming both states")
    first = changes[0]
    _check(_holds(first["source_id"] == "manual_lifecycle", _ACTOR in first["fetch_method"], "withdraw_model_capability_cell" in first["fetch_method"]), "the actor and the verb are in fetch_method")
    _check(_holds(first["raw_excerpt"] == "the model was pulled from the launcher", first["fetch_status"] == "ok"), "the reason is the observation excerpt")
    _check(_holds(first["refresh_run_id"] == runs[1]["id"], changes[1]["refresh_run_id"] == runs[2]["id"]), "each observation belongs to its own call's run row")
    _check_declared_columns(state)


def test_selector_treats_a_withdrawn_cell_as_not_accepted() -> None:
    state = _accepted_state()
    before = verbs.select_dispatch_tier(_typed(state), _SELECT, now=_NOW)
    receipt = before["selection_receipt"]
    _check(receipt["selected"] == _SOL_MEDIUM and _verify(state, receipt) == "", "setup: sol medium is the cheapest clearing cell and its receipt verifies")
    rows_before = len(read_cells(_typed(state)))
    verbs.withdraw_model_capability_cell(_typed(state), _params(model="gpt-5.6-sol", effort="medium"))
    after = verbs.select_dispatch_tier(_typed(state), _SELECT, now=_NOW)
    _check(after["selected"]["model"] != "gpt-5.6-sol" or after["selected"]["effort"] != "medium", "a withdrawn cell is no longer selected")
    _check(after["excluded"]["not_accepted"] == before["excluded"]["not_accepted"] + 1, "it is excluded under the existing not_accepted counter")
    _check(after["unscored_cells"] == before["unscored_cells"], "it keeps its score, so it still flows into the selector rather than being dropped as unscored")
    catalog = selection_catalog(read_cells(_typed(state)))
    withdrawn = [c for c in catalog if c.model == "gpt-5.6-sol" and c.effort == "medium"]
    _check(len(read_cells(_typed(state))) == rows_before and len(withdrawn) == 1 and withdrawn[0].acceptance == "withdrawn", "the selector input still holds the withdrawn row, carrying acceptance withdrawn")
    _check(_verify(state, receipt) == "selection_receipt_mismatch", "a receipt issued before the withdrawal fails with selection_receipt_mismatch")
    verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params(model="gpt-5.6-sol", effort="medium"))
    _check(_verify(state, receipt) == "" and verbs.select_dispatch_tier(_typed(state), _SELECT, now=_NOW)["selection_receipt"] == receipt, "reversal: after reinstating, the same receipt verifies again")


def test_withdraw_with_a_stale_premise_is_refused() -> None:
    state = _recorded_state()
    runs_before = len(_rows(state, TABLE_MODEL_CAPABILITY_REFRESH_RUN))
    code = _code(lambda: verbs.withdraw_model_capability_cell(_typed(state), _params(expected_acceptance=CELL_ACCEPTANCE_PENDING_CROSSCHECK)))
    _check(code == "stale_cell_state", "withdraw with an expected_acceptance the cell does not hold: stale_cell_state")
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "the refused withdraw moved nothing")
    _check(len(_rows(state, TABLE_MODEL_CAPABILITY_REFRESH_RUN)) == runs_before and _changes(state) == [], "a premise refused before any write opens no run and writes no evidence")
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    _check(
        _code(lambda: verbs.withdraw_model_capability_cell(_typed(state), _params())) == "stale_cell_state",
        "a second withdraw of the same accepted premise is refused: the cell is already withdrawn",
    )
    _check(len(_changes(state)) == 1, "and writes no second acceptance_change")


def test_reinstate_with_a_stale_premise_is_refused() -> None:
    state = _recorded_state()
    code = _code(lambda: verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params()))
    _check(code == "stale_cell_state", "reinstate of a cell that is not withdrawn: stale_cell_state")
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and _changes(state) == [], "the refused reinstate moved nothing and wrote no evidence")


class _RacingState(RealShapeState):
    """A second writer: the first read of the cell table is followed by a touch of the row.

    The real database bumps ``updated_at`` on every update; this does the same
    to every cell row right after the read hands the old value to the caller.
    """

    def __init__(self) -> None:
        super().__init__()
        self.armed = False
        self.touches = 0

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        result = super().query_ordered(namespace, query)
        if self.armed and query.get("table") == TABLE_MODEL_CAPABILITY_CELL:
            self.armed = False
            self.touches += 1
            for row in self.rows(namespace, TABLE_MODEL_CAPABILITY_CELL):
                row["updated_at"] = f"2099-01-01T00:00:{self.touches:02d}+00:00"
        return result


def test_a_writer_between_the_read_and_the_write_is_refused() -> None:
    state = _RacingState()
    verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "staleness_window_hours": 72})
    state.armed = True
    code = _code(lambda: verbs.withdraw_model_capability_cell(_typed(state), _params()))
    _check(code == "stale_cell_state", "a row touched after the read fails the updated_at predicate: stale_cell_state")
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and _changes(state) == [], "nothing was moved and no acceptance_change was written")
    stopped = [r for r in _rows(state, TABLE_MODEL_CAPABILITY_REFRESH_RUN) if r["trigger"] == "manual"][-1]
    _check(stopped["status"] == "completed" and "stale_cell_state" in str(stopped["note"]), "the run row opened for it is closed with a note naming the refusal")
    withdrawn = verbs.withdraw_model_capability_cell(_typed(state), _params())
    _check(withdrawn["cells"][0]["to_acceptance"] == "withdrawn", "after reading again the withdraw succeeds")
    state.armed = True
    _check(_code(lambda: verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())) == "stale_cell_state", "reinstate is guarded by the same updated_at predicate")
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, "and a refused reinstate leaves the cell withdrawn")


def test_reinstate_without_a_retained_score_is_refused() -> None:
    state = _recorded_state()
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    _cell_row(state, "gpt-6-sol", "high")["capability_score"] = None
    try:
        verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())
        code, message = "", ""
    except CatalogError as exc:
        code, message = exc.code, exc.message
    _check(code == "parameter_invalid" and "record the cell instead" in message, "a cell with no retained score is refused and the message says to record it instead")
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, "the refused reinstate left the cell withdrawn")
    verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "staleness_window_hours": 72})
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "recording the cell is the way back, as the message says")


def test_all_efforts_form_moves_every_effort_or_none() -> None:
    state = _accepted_state()
    no_effort = _params(model="gpt-5.6-sol")
    del no_effort["effort"]
    efforts = sorted(str(r["effort"]) for r in read_cells(_typed(state), runtime="codex", model="gpt-5.6-sol"))
    _check(len(efforts) >= 2, f"setup: gpt-5.6-sol has several efforts ({efforts})")
    _cell_row(state, "gpt-5.6-sol", efforts[-1])["acceptance"] = CELL_ACCEPTANCE_PENDING_CROSSCHECK
    _check(_code(lambda: verbs.withdraw_model_capability_cell(_typed(state), no_effort)) == "stale_cell_state", "the last effort off the premise refuses the whole request")
    _check(
        all(r["acceptance"] != CELL_ACCEPTANCE_WITHDRAWN for r in read_cells(_typed(state), runtime="codex", model="gpt-5.6-sol")) and _changes(state) == [],
        "and no other effort was moved",
    )
    _cell_row(state, "gpt-5.6-sol", efforts[-1])["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
    done = verbs.withdraw_model_capability_cell(_typed(state), no_effort)
    _check(sorted(c["effort"] for c in done["cells"]) == efforts, "with the premise true everywhere every effort of the model is withdrawn")
    _check(len(_changes(state)) == len(efforts) and len({c["refresh_run_id"] for c in _changes(state)}) == 1, "one acceptance_change per effort under one run row")
    other = _cell_row(state, "gpt-5.6-luna", "low")
    _check(other["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "another model of the runtime is untouched")
    back = verbs.reinstate_model_capability_cell(_typed(state), {k: v for k, v in _reinstate_params(model="gpt-5.6-sol").items() if k != "effort"})
    _check(sorted(c["effort"] for c in back["cells"]) == efforts, "reinstate with no effort reopens every effort")


def test_refusals_and_the_ladder_escape() -> None:
    state = _recorded_state()
    cases: list[tuple[str, str, dict[str, Any]]] = [
        ("an unknown runtime is refused, never mapped to a provider", "parameter_invalid", _params(runtime="gemini")),
        ("a blank reason is refused", "parameter_invalid", _params(reason="  ")),
        ("a missing reason is refused", "parameter_invalid", {k: v for k, v in _params().items() if k != "reason"}),
        ("a reason over 512 characters is refused", "parameter_invalid", _params(reason="x" * 513)),
        ("a blank actor is refused", "parameter_invalid", _params(actor="")),
        ("an unknown expected_acceptance is refused", "parameter_invalid", _params(expected_acceptance="maybe")),
        ("withdraw expecting withdrawn is refused: nothing to withdraw", "parameter_invalid", _params(expected_acceptance=CELL_ACCEPTANCE_WITHDRAWN)),
        ("a model with no cell is cell_not_found", "cell_not_found", _params(model="gpt-9-nothing")),
        ("an effort with no cell is cell_not_found", "cell_not_found", _params(effort="low")),
    ]
    for label, expected, params in cases:
        _check(_code(lambda p=params: verbs.withdraw_model_capability_cell(_typed(state), p)) == expected, label)
    _check(
        _code(lambda: verbs.reinstate_model_capability_cell(_typed(state), _params(expected_acceptance=CELL_ACCEPTANCE_ACCEPTED))) == "parameter_invalid",
        "reinstate expecting anything but withdrawn is refused: it only reopens withdrawn cells",
    )
    _check(_code(lambda: verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params(runtime="gemini"))) == "parameter_invalid", "reinstate refuses an unknown runtime too")
    _check(_changes(state) == [] and _cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "no refusal moved anything")
    poisoned = dict(_cell_row(state, "gpt-6-sol", "high"), id="gen-poison", effort="ultra")
    _rows(state, TABLE_MODEL_CAPABILITY_CELL).append(poisoned)
    done = verbs.withdraw_model_capability_cell(_typed(state), _params(effort="ultra"))
    _check(done["cells"][0]["effort"] == "ultra", "a cell whose effort is outside the ladder can still be withdrawn")


def test_the_store_validates_without_the_verb_layer() -> None:
    """The store is called by the verbs today; its own guards must hold for any other caller."""
    state = _recorded_state()
    base = CellTransition("codex", "gpt-6-sol", "high", "why", CELL_ACCEPTANCE_ACCEPTED, _ACTOR)
    cases = {
        "a blank reason": replace(base, reason=" "),
        "a blank actor": replace(base, actor=""),
        "a blank effort": replace(base, effort=" "),
        "an unknown runtime": replace(base, runtime="gemini"),
        "an unknown expected_acceptance": replace(base, expected_acceptance="maybe"),
    }
    for label, request in cases.items():
        _check(_code(lambda r=request: withdraw_cells(_typed(state), r)) == "parameter_invalid", f"the store refuses {label} on its own")
    _check(_changes(state) == [] and len(_rows(state, TABLE_MODEL_CAPABILITY_REFRESH_RUN)) == 1, "and a refused request opens no run and writes no evidence")


class _MidRequestWriter(RealShapeState):
    """A second writer that lands between two moves of one request.

    Once armed, the first successful cell update is followed by a touch of every other cell
    row, as the real database bumps ``updated_at`` on every update by another session."""

    def __init__(self) -> None:
        super().__init__()
        self.armed = False

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        result = super().update_state(namespace, query, updates)
        if self.armed and query.get("table") == TABLE_MODEL_CAPABILITY_CELL:
            self.armed = False
            moved_id = query["filters"]["id"]
            for row in self.rows(namespace, TABLE_MODEL_CAPABILITY_CELL):
                if row["id"] != moved_id:
                    row["updated_at"] = "2099-01-01T00:00:00+00:00"
        return result


def _withdraw_all_efforts_with_a_racing_writer() -> tuple[RealShapeState, list[str], CatalogError | None]:
    state = _MidRequestWriter()
    seed_catalog(_typed(state), seed=load_seed_table(), staleness_window_hours=72)
    for row in _rows(state, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
    efforts = sorted(str(r["effort"]) for r in read_cells(_typed(state), runtime="codex", model="gpt-5.6-sol"))
    request = _params(model="gpt-5.6-sol")
    del request["effort"]
    state.armed = True
    try:
        verbs.withdraw_model_capability_cell(_typed(state), request)
    except CatalogError as exc:
        return state, efforts, exc
    return state, efforts, None


def _error_text(error: CatalogError | None) -> tuple[str, str]:
    """(code, message) of a refusal, or two empty strings when the request was not refused."""
    return ("", "") if error is None else (error.code, error.message)


def test_the_all_efforts_form_reports_exactly_what_moved_under_a_concurrent_write() -> None:
    state, efforts, error = _withdraw_all_efforts_with_a_racing_writer()
    moved = [e for e in efforts if _cell_row(state, "gpt-5.6-sol", e)["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN]
    code, message = _error_text(error)
    _check(_holds(code == "stale_cell_state", len(moved) == 1), f"a writer landing mid-request stops the request with stale_cell_state after exactly one move (moved {moved})")
    _check(f"already moved ['{moved[0]}']" in message, "the error names exactly the effort that moved")
    _check(_holds(str([e for e in efforts if e not in moved]) in message, "nothing was moved" not in message), "and the efforts that did not move, never claiming nothing moved")
    stopped = [r for r in _rows(state, TABLE_MODEL_CAPABILITY_REFRESH_RUN) if r["trigger"] == "manual"][-1]
    _check(f"moved {moved}" in str(stopped["note"]), "the run row's note carries the same account")
    _check(len(_changes(state)) == 1, "and the history holds the one acceptance_change that really happened")


def _absent_run(codex_models: Sequence[str], at: datetime) -> Any:  # noqa: ANN401 -- a DiscoveryRun built by the clock fixtures
    return clock_fixtures._run(codex=("ok", codex_models), fetched_at=at)  # noqa: SLF001


def _auto_withdrawn_state() -> tuple[RealShapeState, datetime]:
    """A recorded cell whose model the Codex list stopped listing, withdrawn by the pass 73 hours after the clock started.

    Both moments are in the past (80 and 7 hours ago), so the reinstate verb, which reads the wall clock, sees the window as passed."""
    state = _recorded_state()
    start = datetime.now(UTC) - timedelta(hours=80)
    clock_fixtures._pass(state, _absent_run(["gpt-other"], start), now=start)  # noqa: SLF001
    later = start + timedelta(hours=73)
    clock_fixtures._pass(state, _absent_run(["gpt-other"], later), now=later)  # noqa: SLF001
    return state, later


def test_the_clock_withdraws_after_the_grace_window_and_a_relist_reverses_it_with_history() -> None:
    state, later = _auto_withdrawn_state()
    cell = _cell_row(state, "gpt-6-sol", "high")
    _check(cell["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and cell["harness_unverified_since"] is not None, "after one window of absence the pass withdrew the cell and left its clock set")
    _check(_holds(cell["capability_score"] == 43.0, cell["cost_per_task_usd"] == 0.37), "with its score and cost kept")
    clock_fixtures._pass(state, _absent_run(["gpt-6-sol"], later), now=later)  # noqa: SLF001
    back = _cell_row(state, "gpt-6-sol", "high")
    _check(_holds(back["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, back["harness_unverified_since"] is None, back["disagreement_note"] is None), "a relisting reinstates it, clears the clock and drops the withdrawal note")
    trail = [o["value_text"] for o in _history(state)["observations"] if o["metric"] in ("acceptance_change", "harness_clock")]
    _check(
        [text.split(":")[0] for text in trail] == ["started", "accepted->withdrawn", "withdrawn->accepted"],
        f"the history reads back the whole trail in order: clock started, withdrawn, reinstated with the clock cleared (got {trail})",
    )
    walked, pages = _walk_history(state, 2)
    _check(_holds(len(walked) == len(set(walked)), pages <= _MAX_WALK_PAGES), "and a bounded paged walk visits each observation once")


def test_reinstating_an_automatic_withdrawal_by_hand_is_undone_by_the_next_pass() -> None:
    state, later = _auto_withdrawn_state()
    answer = verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())
    outlook = answer["cells"][0]["refresh_outlook"]
    _check(outlook["undone_by_next_refresh"] is True, "the reinstate answer says the next refresh withdraws the cell again: the clock is set and the window passed")
    clock_fixtures._pass(state, _absent_run(["gpt-other"], later + timedelta(hours=1)), now=later + timedelta(hours=1))  # noqa: SLF001
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, "and it does, while the model is still unlisted")
    verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())
    clock_fixtures._pass(state, _absent_run(["gpt-6-sol"], later + timedelta(hours=2)), now=later + timedelta(hours=2))  # noqa: SLF001
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "once the harness lists the model again the reinstatement stands")


def test_a_manual_withdrawal_survives_the_pass_whatever_the_harness_says() -> None:
    state = _recorded_state()
    start = datetime.now(UTC)
    clock_fixtures._pass(state, _absent_run(["gpt-other"], start), now=start)  # noqa: SLF001
    _check(_cell_row(state, "gpt-6-sol", "high")["harness_unverified_since"] is not None, "setup: the clock was running")
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    for hours, models in ((1, ["gpt-6-sol"]), (200, ["gpt-other"])):
        at = start + timedelta(hours=hours)
        report = clock_fixtures._pass(state, _absent_run(models, at), now=at)  # noqa: SLF001
        row = _cell_row(state, "gpt-6-sol", "high")
        _check(_holds(row["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, row["harness_unverified_since"] is None, report.reinstated == [], report.clock_started == []), f"{hours} hours on, with the model {'listed' if 'gpt-6-sol' in models else 'absent'}: the person's withdrawal stands and no clock restarts")


def test_reinstate_says_whether_the_next_refresh_would_undo_it() -> None:
    state = _recorded_state()
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    plain = verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())["cells"][0]["refresh_outlook"]
    _check(plain["undone_by_next_refresh"] is False and "no harness absence clock" in plain["detail"], "no absence clock: the refresh has no reason to withdraw it again")
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    request = CellTransition("codex", "gpt-6-sol", "high", "the launcher lists it again", CELL_ACCEPTANCE_WITHDRAWN, _ACTOR)
    row = _cell_row(state, "gpt-6-sol", "high")
    row["harness_unverified_since"] = (_NOW - timedelta(hours=100)).isoformat()
    undone = reinstate_cells(_typed(state), request, now=_NOW)["cells"][0]["refresh_outlook"]
    _check(undone["undone_by_next_refresh"] is True and "withdraws this cell again" in undone["detail"], "clock set and the 72-hour window passed: the next refresh withdraws it again, and the answer says so")
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    _cell_row(state, "gpt-6-sol", "high")["harness_unverified_since"] = (_NOW - timedelta(hours=10)).isoformat()
    inside = reinstate_cells(_typed(state), request, now=_NOW)["cells"][0]["refresh_outlook"]
    _check(inside["undone_by_next_refresh"] is False and "after" in inside["detail"], "clock set inside the window: not undone yet, the answer names when it would be")
    verbs.withdraw_model_capability_cell(_typed(state), _params())
    _cell_row(state, "gpt-6-sol", "high")["harness_unverified_since"] = "not a time"
    unknown = reinstate_cells(_typed(state), request, now=_NOW)["cells"][0]["refresh_outlook"]
    _check(unknown["undone_by_next_refresh"] is None, "an unreadable clock is reported as unknown, not as a refusal (rul_becb4bed)")


def _history_state() -> RealShapeState:
    state = _recorded_state()
    for _ in range(2):
        verbs.withdraw_model_capability_cell(_typed(state), _params())
        verbs.reinstate_model_capability_cell(_typed(state), _reinstate_params())
    return state


def _history(state: RealShapeState, **over: Any) -> dict[str, Any]:
    return verbs.read_model_capability_history(_typed(state), {"runtime": "codex", "model": "gpt-6-sol", "effort": "high", **over})


_MAX_WALK_PAGES = 12
"""A history walk is bounded: a cursor that stops advancing fails a named check in seconds instead of looping."""


def _walk_history(state: RealShapeState, limit: int) -> tuple[list[str], int]:
    """Every observation id in cursor order, and the number of pages read; one more than the bound when the cursor never ended."""
    walked: list[str] = []
    cursor: list[str] | None = None
    for pages in range(1, _MAX_WALK_PAGES + 1):
        page = _history(state, limit=limit, **({} if cursor is None else {"after": cursor}))
        walked.extend(o["id"] for o in page["observations"])
        cursor = page["next_after"]
        if cursor is None:
            return walked, pages
    return walked, _MAX_WALK_PAGES + 1


def _check_history_refusals(state: RealShapeState) -> None:
    refused = [
        _code(lambda: _history(state, limit=0)),
        _code(lambda: _history(state, limit=101)),
        _code(lambda: _history(state, after=["only-one"])),
        _code(lambda: _history(state, runtime="gemini")),
    ]
    _check(refused == ["parameter_invalid"] * 4, "a bad limit, cursor or runtime is refused")


def test_history_readback_is_ordered_complete_and_paged() -> None:
    state = _history_state()
    full = _history(state)
    obs = full["observations"]
    _check(_holds(len(obs) == 6, full["next_after"] is None), "all six observations (two readings, four moves) come back on one short page")
    stamps = [o["fetched_at"] for o in obs]
    _check(stamps == sorted(stamps), "ordered by fetched_at, oldest first")
    _check(_holds([o["metric"] for o in obs].count("acceptance_change") == 4, obs[-1]["value_text"] == "withdrawn->accepted: manual"), "the newest observation is the last reinstatement")
    _check(_holds(full["current"]["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, full["current"]["capability_score"] == 43.0), "the cell as it stands now rides along")
    walked, pages = _walk_history(state, 2)
    _check(_holds(walked == [o["id"] for o in obs], pages == 4), "paging by two walks every observation once, in the same order (the last full page is followed by an empty one)")
    page = _history(state, limit=4)
    _check(_holds(page["next_after"] is not None, len(_history(state, limit=4, after=page["next_after"])["observations"]) == 2), "a full page hands back a cursor; the next page holds the rest")
    other = _history(state, effort="low")
    _check(_holds(other["observations"] == [], other["current"] is None, other["next_after"] is None), "a cell with no record has an empty history and no current")
    _check_history_refusals(state)


def test_a_provider_fault_is_loud() -> None:
    state = _recorded_state()
    state.fail_next("update")
    try:
        verbs.withdraw_model_capability_cell(_typed(state), _params())
        raised = False
    except StateOperationError:
        raised = True
    _check(raised, "a failed state update raises instead of reporting a withdrawal")
    _check(_cell_row(state, "gpt-6-sol", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and _changes(state) == [], "and neither the cell nor the history moved")


if __name__ == "__main__":
    test_create_read_retire_reopen_in_one_scenario()
    test_run_rows_and_acceptance_change_evidence()
    test_selector_treats_a_withdrawn_cell_as_not_accepted()
    test_withdraw_with_a_stale_premise_is_refused()
    test_reinstate_with_a_stale_premise_is_refused()
    test_a_writer_between_the_read_and_the_write_is_refused()
    test_reinstate_without_a_retained_score_is_refused()
    test_all_efforts_form_moves_every_effort_or_none()
    test_the_all_efforts_form_reports_exactly_what_moved_under_a_concurrent_write()
    test_refusals_and_the_ladder_escape()
    test_the_store_validates_without_the_verb_layer()
    test_reinstate_says_whether_the_next_refresh_would_undo_it()
    test_the_clock_withdraws_after_the_grace_window_and_a_relist_reverses_it_with_history()
    test_reinstating_an_automatic_withdrawal_by_hand_is_undone_by_the_next_pass()
    test_a_manual_withdrawal_survives_the_pass_whatever_the_harness_says()
    test_history_readback_is_ordered_complete_and_paged()
    test_a_provider_fault_is_loud()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
