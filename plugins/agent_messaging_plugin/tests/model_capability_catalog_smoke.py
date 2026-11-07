#!/usr/bin/env python3
"""Store + verb smoke for the model capability catalog (iss_48ea8171, §9 item 1).

Proves the contract that makes the catalog safe to consult:

* a seed writes every cell as ``pending_crosscheck`` and NEVER ``accepted``;
* ``select_dispatch_tier`` refuses ``catalog_unaccepted`` while nothing is accepted
  and serves the operator's verdict once cells are accepted; age and unknown quota
  are advisories with a refresh request, never refusals (rul_becb4bed);
* re-seeding never downgrades an accepted cell (idempotent, start-up safe);
* the selected answer carries the current dispatch-policy version;
* the default billing objective is derived from the economics profiles;
* every seeded record names only declared schema columns (the drift class
  the real-shape fake exists to catch);
* ``record_model_capability_cell`` (operator ruling rul_9e7a67ba) writes a
  caller's reading as an accepted, immediately servable cell with its source
  kept as observation evidence, reports the values it replaced, and refuses
  malformed input.

PURE UNIT, offline: real-shape state fake, no database, network, or model turn.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from math import inf, nan
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin import model_catalog_policy as policy  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import TierSelectionError  # noqa: E402
from agent_messaging_plugin.model_capability_fetch import (  # noqa: E402
    SOURCE_LEADERBOARD_HTML,
    SOURCE_MODEL_DETAIL_HTML,
    DiscoveredCell,
    DiscoveryRun,
    LeaderboardRow,
    Reading,
)
from agent_messaging_plugin.model_capability_store import (  # noqa: E402
    CATALOG_PAGE_SIZE,
    CATALOG_READ_CEILING,
    CatalogError,
    default_billing_objective,
    load_seed_table,
    open_refresh_run,
    read_cells,
    read_cells_page,
    reconcile_discovery,
    seed_catalog,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
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
_NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)  # on or after the newest usage_economics effective_at
_STANDARDIZER = {"name", "id", "external_id", "namespace", "created_at", "updated_at", "created_by", "updated_by", "is_deleted"}


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


def _empty_policy_state() -> RealShapeState:
    """Explicitly unconfigured state for unknown-window acceptance tests."""
    return RealShapeState()


def _policy_change(state: RealShapeState, action: str, expected: int | None, **params: Any) -> dict[str, Any]:
    return policy.set_catalog_configuration(
        _typed(state),
        {"stable_id": "catalog", "action": action, "expected_revision": expected, "reason": "explicit catalog smoke data", **params},
        actor="catalog-smoke-operator",
        source=False,
    )["current"]


def _policy_state() -> RealShapeState:
    """Catalog-local fixture; the generic fake has no production defaults."""
    state = _empty_policy_state()
    _policy_change(state, "set", None, values={"staleness_window_hours": 72})
    return state


def _accept_all(state: RealShapeState, *, measured_at: datetime) -> None:
    """Stand in for a real cross-checked refresh run: flip every cell to accepted."""
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["acceptance"] = CELL_ACCEPTANCE_ACCEPTED
        row["accepted_at"] = measured_at.isoformat()
        row["measured_at"] = measured_at.isoformat()
        row["last_refresh_run_id"] = "mcr-test-accepted"


def test_seed_writes_pending_never_accepted() -> None:
    state = _policy_state()
    seed = load_seed_table()
    result = seed_catalog(_typed(state), seed=seed)
    cells = read_cells(_typed(state))
    _check(result["cells_seeded"] == len(seed.cells) == len(cells), f"seed writes every cell once ({len(cells)})")
    _check(all(row["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK for row in cells), "every seeded cell is pending_crosscheck")
    _check(not any(row["acceptance"] == CELL_ACCEPTANCE_ACCEPTED for row in cells), "a seed never accepts")
    runs = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)
    _check(len(runs) == 1 and runs[0]["trigger"] == "seed" and runs[0]["status"] == "completed", "one completed seed run row")
    observations = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)
    priced = sum(1 for cell in seed.cells if cell.cost_per_task_usd is not None)
    _check(len(observations) == len(seed.cells) + priced, "one observation per seeded metric, source seed_table")


def _cell(rows: list[dict[str, object]], model: str, effort: str) -> dict[str, object]:
    return next(row for row in rows if row["model"] == model and row["effort"] == effort)


def test_seed_derives_relative_multipliers() -> None:
    state = _policy_state()
    seed_catalog(_typed(state), seed=load_seed_table())
    cells = read_cells(_typed(state))
    luna = _cell(cells, "gpt-5.6-luna", "low")
    sonnet = _cell(cells, "claude-sonnet-5", "max")
    haiku = _cell(cells, "claude-haiku-4.5", "non_reasoning")
    _check(luna["relative_cost_multiplier"] == 1.0, "the cheapest priced seed cell is the 1x anchor")
    _check(round(float(str(sonnet["relative_cost_multiplier"]))) == 509, "sonnet-5 max is 509x the anchor, as the operator's table says")
    _check(haiku["relative_cost_multiplier"] is None, "an unpriced cell gets no multiplier, never a guessed one")


def test_seeded_records_name_only_declared_columns() -> None:
    state = _policy_state()
    seed_catalog(_typed(state), seed=load_seed_table())
    declared = {
        TABLE_MODEL_CAPABILITY_CELL: set(get_model_capability_cell_schema().columns),
        TABLE_MODEL_CAPABILITY_OBSERVATION: set(get_model_capability_observation_schema().columns),
        TABLE_MODEL_CAPABILITY_REFRESH_RUN: set(get_model_capability_refresh_run_schema().columns),
    }
    for table, columns in declared.items():
        rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, table)
        stray = {key for row in rows for key in row} - columns - _STANDARDIZER
        _check(not stray, f"{table}: no undeclared columns written ({sorted(stray)})")


def test_selector_refuses_until_accepted_then_serves() -> None:
    state = _policy_state()
    seed_catalog(_typed(state), seed=load_seed_table())
    params = {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "review"}
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), params, now=_NOW)) == "catalog_unaccepted", "pending cells are never served: catalog_unaccepted")
    _accept_all(state, measured_at=_NOW - timedelta(hours=1))
    picked = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _check(picked["selected"]["model"] == "gpt-5.6-sol" and picked["selected"]["effort"] == "medium", "review kind, score 38: sol medium (39, $0.50) beats terra xhigh (38, $0.63)")
    _check("quota_unknown" not in picked["excluded"] and "stale" not in picked["excluded"], "neither unknown quota nor age is an exclusion class")
    _check(picked["advisories"]["stale_reading_used"] == 0 and picked["stale_readings"] == [] and "refresh_request" not in picked, "fresh readings raise no stale advisory and no refresh request")
    _check(picked["policy_version"] == "model-dispatch-policy-v3", "policy version reported when a kind was consulted")
    _check(picked["catalog_run_id"] == "mcr-test-accepted", "the accepting run id rides the answer")
    _check(
        picked["selection_receipt"]["selected"] == {"runtime": "codex", "model": "gpt-5.6-sol", "effort": "medium"},
        "selector returns a spawn-verifiable receipt for its cheapest answer",
    )
    receipt = picked["selection_receipt"]
    _check(
        verbs.verify_selection_receipt(
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
        == receipt,
        "spawn receipt replays to the current cheapest-clearing tuple",
    )
    _check(
        _code(
            lambda: verbs.verify_selection_receipt(
                _typed(state),
                difficulty_score=38,
                selection_receipt=receipt,
                dispatch_kind="review",
                scope_tags=(),
                agent_runtime="codex",
                model="gpt-5.6-sol",
                effort="high",
                now=_NOW,
            )
        )
        == "selection_receipt_mismatch",
        "a spawn tuple not selected by the receipt fails closed",
    )
    unconstrained = verbs.select_dispatch_tier(_typed(state), {"required_score": 45, "billing_objective": "metered_usd"}, now=_NOW)
    _check(unconstrained["selected"]["model"] == "gpt-6-astra" and unconstrained["selected"]["effort"] == "low", "no kind, score 45: astra low -- the operator's example")
    _check(unconstrained["policy_version"] is None, "policy version is null when no kind was consulted")
    relative = verbs.select_dispatch_tier(_typed(state), {"required_score": 45}, now=_NOW)
    _check(relative["billing_objective"] == "relative" and relative["selected"]["model"] == "gpt-6-astra", "default objective on this checkout is relative (flat-rate plan declared) and agrees")


def test_aged_readings_still_select_and_a_tighter_window_changes_only_the_advisory() -> None:
    state = _accepted_state(measured_hours_ago=1)
    params = {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "review"}
    receipt = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)["selection_receipt"]
    _accept_all(state, measured_at=_NOW - timedelta(hours=100))
    aged = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _check(aged["selection_receipt"] == receipt and aged["advisories"]["stale_reading_used"] > 0, "past the 72h window still selects the same cell, with a stale advisory (rul_becb4bed)")
    _check(
        all(row["servable"] and not row["is_fresh"] for row in verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=False, now=_NOW)["cells"]),
        "age never makes an accepted cell unservable; is_fresh only reports it",
    )
    _accept_all(state, measured_at=_NOW - timedelta(hours=10))
    tightened = verbs.select_dispatch_tier(_typed(state), {**params, "max_staleness_hours": 6}, now=_NOW)
    _check(tightened["selection_receipt"]["selected"] == receipt["selected"] and tightened["advisories"]["stale_reading_used"] > 0, "a caller-tightened window only changes the advisory")
    loosened = verbs.select_dispatch_tier(_typed(state), {**params, "max_staleness_hours": 500}, now=_NOW)
    _check(loosened["advisories"]["stale_reading_used"] == 0, "a caller cannot loosen the window (500h clamps to stored 72h; fresh at 10h)")


def _accepted_state(*, measured_hours_ago: int) -> RealShapeState:
    state = _policy_state()
    seed_catalog(_typed(state), seed=load_seed_table())
    _accept_all(state, measured_at=_NOW - timedelta(hours=measured_hours_ago))
    return state


def test_stale_selection_reports_readings_and_requests_one_refresh() -> None:
    """rul_becb4bed: with every reading past the window the best cell is selected, the ages are reported and a refresh is requested once."""
    params = {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "review"}
    fresh = verbs.select_dispatch_tier(_typed(_accepted_state(measured_hours_ago=1)), params, now=_NOW)
    state = _accepted_state(measured_hours_ago=100)
    stale = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _check(stale["selected"]["model"] == fresh["selected"]["model"] and stale["selected"]["effort"] == fresh["selected"]["effort"], "every reading stale: the same best cell is chosen as with fresh readings")
    readings = stale["stale_readings"]
    _check(len(readings) == stale["advisories"]["stale_reading_used"] > 0, "the stale readings are listed and counted")
    _check(
        all(reading["age_hours"] == 100 and reading["window_hours"] == 72 and reading["measured_at"] == (_NOW - timedelta(hours=100)).isoformat() for reading in readings),
        "each stale reading carries its measured_at, age_hours and window_hours",
    )
    _check(sum(1 for reading in readings if reading["selected"]) == 1, "exactly one stale reading is the selected cell's")
    _check(
        set(stale["selection_receipt"]) == {"required_score", "billing_objective", "score_margin", "cost_tolerance", "max_staleness_hours", "runtime_constraint", "selected", "effort_ceiling", "exclude_model_prefixes", "exclude_runtimes", "pin", "pin_basis"},
        "the receipt keeps its seven keys; stale readings ride beside it",
    )
    request = stale["refresh_request"]
    _check(
        request == {"process_key": "plugin::agent_messaging_plugin::refresh_model_capability_catalog", "arguments": {"trigger": "selector_stale"}},
        "no recent refresh run: a refresh_model_capability_catalog request is returned",
    )
    _check(any("refresh was requested" in line and "may stay stale if it fails" in line for line in stale["warnings"]), "the operator notice says a refresh was requested and may fail")
    _check(not state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)[1:], "the selection writes no refresh-run row of its own")


def test_refresh_request_is_deduplicated_against_recent_runs() -> None:
    params = {"required_score": 38, "billing_objective": "metered_usd"}
    state = _accepted_state(measured_hours_ago=100)
    seed_runs = len(state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN))
    _check(seed_runs >= 1 and "refresh_request" in verbs.select_dispatch_tier(_typed(state), params, now=_NOW), "a recent seed run does not suppress the request")
    run_id = open_refresh_run(_typed(state), trigger="cron")
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN):
        if row.get("id") == run_id:
            row["started_at"] = (_NOW - timedelta(hours=2)).isoformat()
    again = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    _check("refresh_request" not in again and again["stale_readings"], "a refresh run started 2h ago suppresses the request but the stale readings are still reported")
    _check(any("No refresh was requested" in line and run_id in line for line in again["warnings"]), "the notice names the run that suppressed the request")
    _check(again["selected"] == verbs.select_dispatch_tier(_typed(state), params, now=_NOW)["selected"], "suppression never changes the selection")
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN):
        if row.get("id") == run_id:
            row["started_at"] = (_NOW - timedelta(hours=13)).isoformat()
    _check("refresh_request" in verbs.select_dispatch_tier(_typed(state), params, now=_NOW), "a run older than the 12h dedupe window no longer suppresses")


def test_receipts_verify_after_readings_cross_the_window() -> None:
    """A spawn is never refused because a reading aged between select and spawn, nor because it was stale when selected."""
    params = {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "review"}
    state = _accepted_state(measured_hours_ago=1)
    receipt = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)["selection_receipt"]
    later = _NOW + timedelta(hours=100)
    replay = verbs.select_dispatch_tier(_typed(state), params, now=later)
    _check(replay["advisories"]["stale_reading_used"] > 0, "by spawn time every reading has crossed its window")
    spawn = {"difficulty_score": 38, "dispatch_kind": "review", "scope_tags": (), "agent_runtime": "codex", "model": "gpt-5.6-sol", "effort": "medium"}
    _check(verbs.verify_selection_receipt(_typed(state), selection_receipt=receipt, now=later, **spawn) == receipt, "a receipt issued from fresh readings still verifies after they go stale")
    stale_receipt = verbs.select_dispatch_tier(_typed(state), params, now=later)["selection_receipt"]
    _check(verbs.verify_selection_receipt(_typed(state), selection_receipt=stale_receipt, now=later, **spawn) == stale_receipt, "a receipt issued from stale readings verifies")
    _check(
        _code(lambda: verbs.verify_selection_receipt(_typed(state), selection_receipt={**stale_receipt, "stale_readings": []}, now=later, **spawn)) == "selection_receipt_invalid",
        "verification stays strict: an extra receipt field is still refused",
    )


def test_unreadable_quota_profile_is_a_warning_not_a_refusal() -> None:
    state = _accepted_state(measured_hours_ago=1)
    params = {"required_score": 38, "billing_objective": "metered_usd"}
    expected = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)["selected"]
    original = verbs._USAGE_ECONOMICS_PROFILE_PATH
    verbs._USAGE_ECONOMICS_PROFILE_PATH = original.with_name("usage_economics.missing.json")
    try:
        picked = verbs.select_dispatch_tier(_typed(state), params, now=_NOW)
    finally:
        verbs._USAGE_ECONOMICS_PROFILE_PATH = original
    _check(picked["selected"] == expected and "quota_unknown_used" not in picked["advisories"], "quota profile is not read for explicit unconstrained metered selection")
    _check(not any("quota" in line for line in picked.get("warnings", [])), "independent quota warnings do not enter selection")


def test_reseed_preserves_accepted_cells() -> None:
    state = _policy_state()
    seed = load_seed_table()
    seed_catalog(_typed(state), seed=seed)
    _accept_all(state, measured_at=_NOW)
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        if row["model"] == "claude-opus-5" and row["effort"] == "low":
            row["capability_score"] = 41.0
    result = seed_catalog(_typed(state), seed=seed)
    _check(result["cells_seeded"] == 0 and len(result["accepted_cells_preserved"]) == len(seed.cells), "re-seed touches no accepted cell")
    opus = next(row for row in read_cells(_typed(state)) if row["model"] == "claude-opus-5" and row["effort"] == "low")
    _check(opus["capability_score"] == 41.0 and opus["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "an accepted, refreshed value survives a re-seed")
    _check(len(read_cells(_typed(state))) == len(seed.cells), "re-seed creates no duplicate rows")


def test_read_verb_and_refusals() -> None:
    state = _policy_state()
    seed_catalog(_typed(state), seed=load_seed_table())
    hidden = verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=False, now=_NOW)
    _check(hidden["cells"] == [] and hidden["total_cells"] > 0 and hidden["servable_cells"] == 0, "read hides pending cells unless asked")
    shown = verbs.read_model_capability_catalog(_typed(state), runtime="codex", model="gpt-6-astra", include_unaccepted=True, now=_NOW)
    _check(len(shown["cells"]) == 5 and all(not row["servable"] for row in shown["cells"]), "read filters by runtime/model and marks pending cells unservable")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {}, now=_NOW)) == "parameter_invalid", "missing required_score refuses")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {"required_score": 1, "billing_objective": "tokens"}, now=_NOW)) == "parameter_invalid", "unknown objective refuses")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(state), {"required_score": 1, "max_staleness_hours": 0}, now=_NOW)) == "parameter_invalid", "non-positive window refuses")
    _check(_code(lambda: verbs.select_dispatch_tier(_typed(_policy_state()), {"required_score": 1}, now=_NOW)) == "catalog_empty", "an unseeded catalog refuses as empty")


def test_billing_objective_is_derived() -> None:
    _check(default_billing_objective("claude_code", now=_NOW) == "relative", "claude_code on this checkout: flat-rate plan -> relative")
    _check(default_billing_objective("codex", now=_NOW) == "metered_usd", "codex on this checkout: no flat-rate profile -> metered_usd")


def test_provider_fault_is_loud() -> None:
    from ananta.llm.agent_messaging.state_results import StateOperationError

    state = _policy_state()
    state.fail_next("write")
    try:
        seed_catalog(_typed(state), seed=load_seed_table())
    except StateOperationError:
        _check(True, "a failed state write raises StateOperationError, never a quiet partial seed")
        return
    _check(False, "a failed state write raises StateOperationError, never a quiet partial seed")


_GPT6_SOL_HIGH = {
    "runtime": "codex",
    "model": "gpt-6-sol",
    "effort": "high",
    "capability_score": 43,
    "cost_per_task_usd": 0.37,
    "source_ref": "https://artificialanalysis.ai/models/gpt-6-sol-high",
    "note": "released 2026-09-22",
}


def test_record_verb_accepts_and_serves_a_new_model() -> None:
    state = _policy_state()
    result = verbs.record_model_capability_cell(_typed(state), dict(_GPT6_SOL_HIGH))
    _check(result["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and result["previous"] is None, "a recorded reading for a new model is accepted, with no previous cell")
    cell = _cell(read_cells(_typed(state)), "gpt-6-sol", "high")
    _check(cell["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and cell["capability_score"] == 43.0 and cell["cost_per_task_usd"] == 0.37, "the cell holds the recorded score and cost")
    _check(cell["provider"] == "openai" and cell["agreeing_observation_ids"] == result["observation_ids"], "provider derives from runtime; the cell cites its evidence rows")
    _check_record_evidence_and_selection(state)


def _check_record_evidence_and_selection(state: RealShapeState) -> None:
    observations = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)
    _check(
        len(observations) == 2 and all(o["source_id"] == "manual_record" and _GPT6_SOL_HIGH["source_ref"] in o["fetch_method"] for o in observations),
        "one observation per metric, source manual_record, fetch_method naming the caller's source",
    )
    runs = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)
    _check(len(runs) == 1 and runs[0]["trigger"] == "manual" and runs[0]["status"] == "completed" and runs[0]["cells_accepted"] == 1, "one completed manual run row")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 40, "billing_objective": "metered_usd"}, now=datetime.now(UTC))
    _check(picked["selected"]["model"] == "gpt-6-sol" and picked["selected"]["effort"] == "high", "the recorded cell is servable to select_dispatch_tier at once")


def test_manual_relative_costs_follow_the_whole_table() -> None:
    state = _policy_state()
    for model, score, cost in (("gpt-6-sol", 50, 10.0), ("gpt-6-luna", 40, 1.0)):
        verbs.record_model_capability_cell(
            _typed(state),
            {
                "runtime": "codex",
                "model": model,
                "effort": "high",
                "capability_score": score,
                "cost_per_task_usd": cost,
                "source_ref": "review cost control",
            },
        )
    rows = read_cells(_typed(state))
    _check(_cell(rows, "gpt-6-sol", "high")["relative_cost_multiplier"] == 10.0, "prior manual cell is rebased to the current price floor")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 35, "billing_objective": "relative"}, now=datetime.now(UTC))
    _check(picked["selected"]["model"] == "gpt-6-luna", "relative selector chooses the cheaper eligible cell")


def _live_run(cell: DiscoveredCell, *, score: float, cost: float) -> DiscoveryRun:
    """A discovery run whose two live sources both report `score` and `cost` for one cell."""
    readings = tuple(
        Reading(
            source_id=source,
            provider=cell.provider,
            runtime=cell.runtime,
            model=cell.model,
            effort=cell.effort,
            metric=metric,
            value_number=value,
            value_text=None,
            raw_excerpt="review control",
            fetch_status="ok",
            fetched_at=datetime.now(UTC).isoformat(),
        )
        for source in (SOURCE_LEADERBOARD_HTML, SOURCE_MODEL_DETAIL_HTML)
        for metric, value in (("intelligence_index", score), ("cost_per_task_usd", cost))
    )
    return DiscoveryRun(
        readings=readings,
        failures=(),
        sources_ok=(SOURCE_LEADERBOARD_HTML, SOURCE_MODEL_DETAIL_HTML),
        cells=(cell,),
        leaderboard_ok=True,
        leaderboard_rows=100,
        harness={},
        harness_model_ids={},
        alias_candidates=(),
        unmapped_creators=(),
        efforts_outside_vocabulary={},
        listed_not_launchable=(),
        creator_label_unmatched=(),
        advisories=(),
    )


def test_manual_reading_that_disagrees_with_live_readings_is_an_advisory() -> None:
    """Q8 (the dispatching seat, rul_367d8bd6): a manual reading that disagrees with two agreeing live pages stays served.

    The shipped behavior excluded the cell (`crosscheck_conflict` is `not_accepted` to the selector). Excluding a
    model an operator deliberately recorded with a named source is a gate nobody can pass unless someone re-records
    it (rul_becb4bed), and the cross-check's job -- catching a transient fetch fault -- is served by a loud note.
    Pass condition for the exclusion that is gone: none is needed. The cell stays accepted with its manual values;
    the advisory lasts until an operator re-records it or the live sources agree with it within tolerance
    (`test_a_manual_cell_renewed_by_live_sources_takes_the_default_window` in the discovery smoke is that path)."""
    state = _policy_state()
    verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "capability_score": 90, "cost_per_task_usd": 1.0})
    cell = DiscoveredCell("codex", "openai", "gpt-6-sol", "high", LeaderboardRow("GPT-6 Sol (high)", "GPT-6 Sol", "high", "openai", "40", "$1.00", "gpt-6-sol-high", "fixture"), None)
    report = reconcile_discovery(_typed(state), open_refresh_run(_typed(state), trigger="manual"), _live_run(cell, score=40.0, cost=1.0))
    held = _cell(read_cells(_typed(state)), "gpt-6-sol", "high")
    _check(report.counts["cells_conflicted"] == 0 and report.counts["cells_unchanged"] == 1, "a manual reading the live pages disagree with is counted unchanged, not conflicted")
    _check(held["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and held["capability_score"] == 90.0 and held["cost_per_task_usd"] == 1.0, "the manual values stay accepted and served")
    note = str(held["disagreement_note"])
    _check("manual versus live" in note and "(manual value kept)" in note, "the cell names the manual/live boundary and that the manual value was kept")
    _check(report.result_fields["manual_conflicts"] == ["codex/gpt-6-sol@high"], "the run result lists it under manual_conflicts")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 80, "billing_objective": "metered_usd"}, now=datetime.now(UTC))
    _check(picked["selected"]["model"] == "gpt-6-sol" and picked["selected"]["effort"] == "high", "the selector still serves the manual cell")
    _check(len(state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)) == 6, "the live readings are kept as evidence (2 manual + 4 live observations)")


def test_record_overwrites_and_reports_previous() -> None:
    state = _policy_state()
    seed_catalog(_typed(state), seed=load_seed_table())
    before = _cell(read_cells(_typed(state)), "gpt-5.6-sol", "high")
    result = verbs.record_model_capability_cell(
        _typed(state),
        {
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "effort": "high",
            "capability_score": 41,
            "cost_per_task_usd": 0.79,
            "source_ref": "register iev_test",
        },
    )
    after = _cell(read_cells(_typed(state)), "gpt-5.6-sol", "high")
    previous = result["previous"]
    _check(isinstance(previous, dict) and previous["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK and previous["capability_score"] == before["capability_score"], "previous reports the replaced values and acceptance")
    _check(after["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and after["capability_score"] == 41.0, "a recorded reading overwrites a pending seed cell as accepted")
    others = [row for row in read_cells(_typed(state)) if (row["model"], row["effort"]) != ("gpt-5.6-sol", "high")]
    _check(all(row["acceptance"] == CELL_ACCEPTANCE_PENDING_CROSSCHECK for row in others), "no other cell is touched")


def test_record_refusals() -> None:
    state = _policy_state()
    bad = {
        "unknown runtime": {**_GPT6_SOL_HIGH, "runtime": "gemini"},
        "effort outside the selector's ladder": {**_GPT6_SOL_HIGH, "effort": "ultra"},
        "score above 100": {**_GPT6_SOL_HIGH, "capability_score": 101},
        "missing score": {key: value for key, value in _GPT6_SOL_HIGH.items() if key != "capability_score"},
        "negative cost": {**_GPT6_SOL_HIGH, "cost_per_task_usd": -1},
        "NaN cost": {**_GPT6_SOL_HIGH, "cost_per_task_usd": nan},
        "infinite cost": {**_GPT6_SOL_HIGH, "cost_per_task_usd": inf},
        "blank source_ref": {**_GPT6_SOL_HIGH, "source_ref": "  "},
        "boolean score": {**_GPT6_SOL_HIGH, "capability_score": True},
        "zero staleness window": {**_GPT6_SOL_HIGH, "staleness_window_hours": 0},
    }
    for label, params in bad.items():
        _check(_code(lambda params=params: verbs.record_model_capability_cell(_typed(state), params)) == "parameter_invalid", f"refuses {label}")
    _check(read_cells(_typed(state)) == [], "a refused call writes nothing")


def test_recorded_rows_name_only_declared_columns() -> None:
    state = _policy_state()
    verbs.record_model_capability_cell(_typed(state), dict(_GPT6_SOL_HIGH))
    declared = {
        TABLE_MODEL_CAPABILITY_CELL: set(get_model_capability_cell_schema().columns),
        TABLE_MODEL_CAPABILITY_OBSERVATION: set(get_model_capability_observation_schema().columns),
        TABLE_MODEL_CAPABILITY_REFRESH_RUN: set(get_model_capability_refresh_run_schema().columns),
    }
    for table, columns in declared.items():
        rows = state.rows(AGENT_ROLE_BINDING_NAMESPACE, table)
        stray = {key for row in rows for key in row} - columns - _STANDARDIZER
        _check(bool(rows) and not stray, f"record verb {table}: rows written, no undeclared columns ({sorted(stray)})")


class _QueryLog(RealShapeState):
    """The real-shape fake, recording every query_ordered payload."""

    def __init__(self) -> None:
        super().__init__()
        self.queries: list[dict[str, object]] = []

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        self.queries.append(query)
        return super().query_ordered(namespace, query)


def test_filtered_read_pushes_filters_down_and_bounds_the_floor() -> None:
    state = _QueryLog()
    for model, score, cost in (("gpt-6-sol", 50, 10.0), ("gpt-6-luna", 40, 1.0)):
        verbs.record_model_capability_cell(_typed(state), {**_GPT6_SOL_HIGH, "model": model, "capability_score": score, "cost_per_task_usd": cost})
    state.queries.clear()
    rows = read_cells(_typed(state), runtime="codex", model="gpt-6-sol")
    _check([row["model"] for row in rows] == ["gpt-6-sol"] and rows[0]["relative_cost_multiplier"] == 10.0, "a filtered read returns only its rows, priced against the table-wide floor")
    filters = [query.get("filters") for query in state.queries]
    _check({"runtime": "codex", "model": "gpt-6-sol"} in filters and {} not in filters, "runtime and model filters are sent to the database; nothing reads the whole table")
    bounded = [query for query in state.queries if query.get("limit") == 1]
    _check(len(bounded) == 1 and cast(list[list[str]], bounded[0]["order_by"])[0] == ["cost_per_task_usd", "asc"], "the price floor is one bounded row, not a table scan")


def _seed_catalog_rows(state: RealShapeState, count: int) -> None:
    """Accepted cells written straight to the table, one model each, so the read is the only thing under test."""
    for index in range(count):
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "record": {
                    "provider": "anthropic",
                    "runtime": "claude_code",
                    "model": f"m-{index:04d}",
                    "effort": "high",
                    "capability_score": 50.0,
                    "cost_per_task_usd": 1.0 + index,
                    "currency": "USD",
                    "effort_supported": True,
                    "measured_at": _NOW.isoformat(),
                    "accepted_at": _NOW.isoformat(),
                    "acceptance": CELL_ACCEPTANCE_ACCEPTED,
                    "agreeing_observation_ids": [],
                    "disagreement_note": None,
                    "staleness_window_hours": 72,
                    "last_refresh_run_id": "run-seed",
                },
            },
        )


def test_read_pages_past_the_old_wall_in_full() -> None:
    state = _policy_state()
    _seed_catalog_rows(state, CATALOG_PAGE_SIZE + 30)
    read = read_cells_page(_typed(state))
    models = [row["model"] for row in read.rows]
    _check(len(models) == CATALOG_PAGE_SIZE + 30 and len(set(models)) == CATALOG_PAGE_SIZE + 30, "a catalog past the old 100-row wall reads in full, across pages")
    _check(read.first_unconsidered is None and not read.truncated, "a complete read reports no truncation")
    _check(models == sorted(models), "the paged read keeps the (runtime, model, effort) order")


def test_read_truncates_at_the_ceiling_and_never_raises() -> None:
    state = _policy_state()
    _seed_catalog_rows(state, CATALOG_READ_CEILING + 5)
    read = read_cells_page(_typed(state))
    _check(len(read.rows) == CATALOG_READ_CEILING, "the read returns exactly the ceiling's rows, first in order")
    _check(read.first_unconsidered == ("claude_code", "m-1000", "high") and read.truncated, "the truncation names the first row not considered")
    _check(len(read_cells(_typed(state))) == CATALOG_READ_CEILING, "the plain read returns the same truncated rows and does not raise")
    answer = verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=True, now=_NOW)
    _check(answer["total_cells"] == CATALOG_READ_CEILING and any("m-1000" in warning for warning in answer.get("warnings", [])), "the catalog answer carries a named truncation warning")
    complete = verbs.read_model_capability_catalog(_typed(_policy_state()), runtime=None, model=None, include_unaccepted=True, now=_NOW)
    _check("warnings" not in complete, "a complete catalog answer carries no truncation warning")


def _catalog_view(state: RealShapeState) -> dict[str, Any]:
    return verbs.read_model_capability_catalog(_typed(state), runtime=None, model=None, include_unaccepted=True, now=_NOW)


def test_live_window_lifecycle_and_fractional_storage() -> None:
    state = _empty_policy_state()
    created = _policy_change(state, "set", None, values={"staleness_window_hours": 0.5})
    seeded = seed_catalog(_typed(state), seed=load_seed_table())
    _check(all(row["staleness_window_hours"] is None for row in read_cells(_typed(state))), "fractional policy stores null, never a rounded cell window")
    _check(any("0.5 h" in text and "null" in text for text in seeded["freshness_window"]["advisories"]), "fractional storage advisory names exact policy value")
    _accept_all(state, measured_at=_NOW - timedelta(hours=1))
    first = _catalog_view(state)
    _check(first["freshness_window"]["hours"] == 0.5 and all(row["is_fresh"] is False for row in first["cells"]), "live catalog uses fractional window exactly")
    _check(first["freshness_window"]["policy_revision_id"] == created["provenance"]["revision_id"], "window provenance names exact policy revision")
    _check_correct_clear_retire_reopen(state)


def _check_correct_clear_retire_reopen(state: RealShapeState) -> None:
    corrected = _policy_change(state, "correct", 1, values={"staleness_window_hours": 2})
    second = _catalog_view(state)
    _check(all(row["is_fresh"] is True for row in second["cells"]) and second["freshness_window"]["hours"] == 2, "policy correction changes next catalog read without rewriting cells")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 38, "billing_objective": "metered_usd"}, now=_NOW)
    _check(picked["freshness_window"]["hours"] == 2 and picked["advisories"]["stale_reading_used"] == 0, "next selection reads corrected policy")
    explicit = verbs.select_dispatch_tier(_typed(state), {"required_score": 38, "billing_objective": "metered_usd", "max_staleness_hours": 0.25}, now=_NOW)
    _check(explicit["freshness_window"]["source"] == "request" and explicit["freshness_window"]["hours"] == 0.25 and explicit["advisories"]["stale_reading_used"] > 0, "explicit request wins without a cached policy ceiling")
    _policy_change(state, "clear", corrected["provenance"]["revision"], clear_fields=["staleness_window_hours"])
    cleared = verbs.select_dispatch_tier(_typed(state), {"required_score": 38, "billing_objective": "metered_usd"}, now=_NOW)
    _check(cleared["freshness_window"]["hours"] is None and cleared["advisories"]["unknown_freshness_window"] > 0, "clear changes next selection to unknown with accepted result")
    _check(any("cleared" in text for text in cleared["warnings"]), "cleared policy is an explicit advisory")
    _check_integral_and_retired(state)


def _check_integral_and_retired(state: RealShapeState) -> None:
    _policy_change(state, "correct", 3, values={"staleness_window_hours": 4})
    manual = verbs.record_model_capability_cell(_typed(state), _GPT6_SOL_HIGH)
    _check(manual["freshness_window"]["cell_hours"] == 4 and _cell(read_cells(_typed(state)), "gpt-6-sol", "high")["staleness_window_hours"] == 4, "integral policy persists exactly on manual write")
    _policy_change(state, "retire", 4)
    retired = _catalog_view(state)
    _check(retired["freshness_window"]["source"] == "observed_catalog" and retired["freshness_window"]["hours"] == 4 and any("retired" in text for text in retired["warnings"]), "retired policy falls through to observed value with advisory")
    _policy_change(state, "reopen", 5)
    _check(_catalog_view(state)["freshness_window"]["source"] == "policy", "reopened policy is used on next call")


def test_absent_policy_empty_catalog_and_other_identity() -> None:
    state = _empty_policy_state()
    policy.set_catalog_configuration(_typed(state), {"stable_id": "another-policy", "action": "set", "expected_revision": None, "reason": "not governing", "values": {"staleness_window_hours": 123}}, actor="catalog-smoke-operator", source=False)
    empty = _catalog_view(state)
    _check(empty["freshness_window"]["hours"] is None and empty["freshness_window"]["source"] == "unknown", "empty catalog and unrelated policy never invent a window")
    manual = verbs.record_model_capability_cell(_typed(state), _GPT6_SOL_HIGH)
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 40, "billing_objective": "metered_usd"}, now=datetime.now(UTC))
    _check(manual["freshness_window"]["hours"] is None and picked["selected"]["model"] == "gpt-6-sol" and picked["freshness_window"]["hours"] is None, "unconfigured policy still records and selects accepted facts")
    _check(_catalog_view(state)["cells"][0]["is_fresh"] is None and any("unknown" in text for text in picked["warnings"]), "unknown freshness stays null with advisory")
    _policy_change(state, "set", None, values={"staleness_window_hours": 0.5})
    cell = DiscoveredCell("codex", "openai", "gpt-6-sol", "high", LeaderboardRow("GPT-6 Sol (high)", "GPT-6 Sol", "high", "openai", "50", "$1.00", "gpt-6-sol-high", "fixture"), None)
    report = reconcile_discovery(_typed(state), open_refresh_run(_typed(state), trigger="manual"), _live_run(cell, score=50, cost=1))
    _check(report.result_fields["freshness_window"]["hours"] == 0.5 and _cell(read_cells(_typed(state)), "gpt-6-sol", "high")["staleness_window_hours"] is None, "reconcile retains exact fractional provenance with null cell storage")
    state.fail_next("query")
    try:
        _catalog_view(state)
    except Exception:
        _check(True, "provider read error remains loud rather than an absent policy")
    else:
        _check(False, "provider read error remains loud rather than an absent policy")


if __name__ == "__main__":
    test_live_window_lifecycle_and_fractional_storage()
    test_absent_policy_empty_catalog_and_other_identity()
    test_seed_writes_pending_never_accepted()
    test_seed_derives_relative_multipliers()
    test_seeded_records_name_only_declared_columns()
    test_selector_refuses_until_accepted_then_serves()
    test_aged_readings_still_select_and_a_tighter_window_changes_only_the_advisory()
    test_stale_selection_reports_readings_and_requests_one_refresh()
    test_refresh_request_is_deduplicated_against_recent_runs()
    test_receipts_verify_after_readings_cross_the_window()
    test_unreadable_quota_profile_is_a_warning_not_a_refusal()
    test_reseed_preserves_accepted_cells()
    test_read_verb_and_refusals()
    test_billing_objective_is_derived()
    test_provider_fault_is_loud()
    test_record_verb_accepts_and_serves_a_new_model()
    test_manual_relative_costs_follow_the_whole_table()
    test_manual_reading_that_disagrees_with_live_readings_is_an_advisory()
    test_record_overwrites_and_reports_previous()
    test_record_refusals()
    test_recorded_rows_name_only_declared_columns()
    test_filtered_read_pushes_filters_down_and_bounds_the_floor()
    test_read_pages_past_the_old_wall_in_full()
    test_read_truncates_at_the_ceiling_and_never_raises()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
