#!/usr/bin/env python3
"""Smoke for the discovery refresh's store rules (iss_0e805222, iss_52fdb8e5; rul_473f772f; design unit U4a).

The decisions `reconcile_discovery` makes over what the discovery pass gathered, with the
gathering replaced by hand-built cells and readings (`model_capability_discovery_smoke.py`
covers the gathering itself): the per-run creation cap that defers instead of dropping, the
live-row ceiling that counts live rows only and never stops a refresh of existing cells or the
manual record verb, a failed page that makes no model "unrenewable", harness-listed models the
leaderboard no longer scores (kept accepted, named), a manual cell renewed by live sources taking
the default window, and an unknown runtime never defaulting to a provider.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_discovery_store_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_fetch as fetch  # noqa: E402
from agent_messaging_plugin import model_capability_store as store  # noqa: E402
from agent_messaging_plugin.model_capability_store import CatalogError, read_cells  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_WITHDRAWN,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
_POLICY_WINDOW_HOURS = 72  # Explicit refresh fixture data; no production default.
_EFFORTS = ("low", "medium", "high", "xhigh", "max")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _typed(fake: RealShapeState) -> StateManagementInterface:
    return cast("StateManagementInterface", fake)


def _refresh(state: RealShapeState, run: fetch.DiscoveryRun) -> store.RefreshReport:
    return store.reconcile_discovery(_typed(state), store.open_refresh_run(_typed(state), trigger="manual"), run, now=_NOW, staleness_window_hours=_POLICY_WINDOW_HOURS)


def _cell(state: RealShapeState, model: str, effort: str) -> dict[str, Any]:
    return next(row for row in read_cells(_typed(state)) if row["model"] == model and row["effort"] == effort)


# --------------------------------------------------------------------------- hand-built discovery runs


def _synthetic_row(index: int, *, creator: str = "OpenAI") -> fetch.LeaderboardRow:
    name = f"Syn Model {index:03d}"
    return fetch.LeaderboardRow(f"{name} (high)", name, "high", creator.lower(), "40", "$1.00", f"syn-{index:03d}", f"{name}|1M|{creator}|40|$1.00|a|b|c|Model|Providers|")


def _synthetic_cells(count: int) -> list[fetch.DiscoveredCell]:
    return [fetch.DiscoveredCell("codex", "openai", f"gpt-syn-{index:03d}", "high", _synthetic_row(index), None) for index in range(count)]


def _agreeing_readings(cell: fetch.DiscoveredCell, *, score: float = 40.0, cost: float = 1.0) -> list[fetch.Reading]:
    return [fetch.Reading(source, cell.provider, cell.runtime, cell.model, cell.effort, metric, value, None, "fixture", "ok", _NOW.isoformat()) for source in (fetch.SOURCE_LEADERBOARD_HTML, fetch.SOURCE_MODEL_DETAIL_HTML) for metric, value in (("intelligence_index", score), ("cost_per_task_usd", cost))]


def _synthetic_run(cells: Sequence[fetch.DiscoveredCell], *, score: float = 40.0, cost: float = 1.0, leaderboard_ok: bool = True, harness_models: dict[str, Sequence[str]] | None = None) -> fetch.DiscoveryRun:
    ordered = sorted(cells, key=lambda cell: cell.key)
    return fetch.DiscoveryRun(
        readings=tuple(reading for cell in ordered for reading in _agreeing_readings(cell, score=score, cost=cost)),
        failures=(),
        sources_ok=(fetch.SOURCE_LEADERBOARD_HTML, fetch.SOURCE_MODEL_DETAIL_HTML),
        cells=tuple(ordered),
        leaderboard_ok=leaderboard_ok,
        leaderboard_rows=100 if leaderboard_ok else 0,
        harness={},
        harness_model_ids={runtime: tuple(models) for runtime, models in (harness_models or {}).items()},
        alias_candidates=(),
        unmapped_creators=(),
        efforts_outside_vocabulary={},
        listed_not_launchable=(),
        creator_label_unmatched=(),
        advisories=(),
    )


def _preload(state: RealShapeState, model: str, effort: str, *, acceptance: str = CELL_ACCEPTANCE_ACCEPTED, runtime: str = "codex", score: float = 30.0, measured_at: str | None = None, window: int = 72) -> None:
    provider = "openai" if runtime == "codex" else "anthropic"
    record: dict[str, Any] = {
        "provider": provider,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "capability_score": score,
        "cost_per_task_usd": 1.0,
        "relative_cost_multiplier": None,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": measured_at or _NOW.isoformat(),
        "accepted_at": _NOW.isoformat(),
        "acceptance": acceptance,
        "agreeing_observation_ids": [],
        "disagreement_note": None,
        "staleness_window_hours": window,
        "last_refresh_run_id": None,
    }
    state.upsert_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_CELL, "record": record, "conflict_columns": ["runtime", "model", "effort"]})


# --------------------------------------------------------------------------- the rules


def test_creation_cap_defers_and_drains() -> None:
    state = RealShapeState()
    run = _synthetic_run(_synthetic_cells(70))
    created: list[int] = []
    deferred: list[int] = []
    capped: list[bool] = []
    for _ in range(3):
        report = _refresh(state, run)
        created.append(report.result_fields["cells_created"])
        deferred.append(len(report.result_fields["deferred"]))
        capped.append(report.result_fields["creation_capped"])
    _check(created == [30, 30, 10], f"70 new cells are created 30, 30, 10 over three runs (got {created})")
    _check(deferred == [40, 10, 0] and capped == [True, True, False], f"the rest are deferred, not dropped, and the run says it was capped (got {deferred}, {capped})")
    _check(len(read_cells(_typed(state))) == 70, "all 70 cells exist after the third run")
    first = _refresh(RealShapeState(), run).result_fields["deferred"]
    _check(first[0] == "codex/gpt-syn-030@high" and first[-1] == "codex/gpt-syn-069@high", "the deferred names are the cells after the first 30, in key order")


def test_the_ceiling_counts_live_rows_only() -> None:
    state = RealShapeState()
    for index in range(290):
        _preload(state, f"gpt-old-{index:03d}", "high")
    for index in range(20):
        _preload(state, f"gpt-gone-{index:03d}", "high", acceptance=CELL_ACCEPTANCE_WITHDRAWN)
    report = _refresh(state, _synthetic_run(_synthetic_cells(20)))
    _check(report.result_fields["cells_created"] == 10, f"290 live rows leave room for 10 more; 20 withdrawn rows are history and do not count (created {report.result_fields['cells_created']})")
    _check(report.result_fields["creation_suspended"] is True and report.result_fields["creation_capped"] is False and len(report.result_fields["deferred"]) == 10, "the ceiling, not the cap, binds and the other 10 are deferred")


def test_ceiling_suspends_creation_but_not_refresh_or_manual_record() -> None:
    state = RealShapeState()
    for index in range(299):
        _preload(state, f"gpt-old-{index:03d}", "high")
    _preload(state, "gpt-syn-000", "high", score=30.0)
    run = _synthetic_run(_synthetic_cells(6), score=44.0)
    report = _refresh(state, run)
    _check(report.result_fields["cells_created"] == 0 and report.result_fields["creation_suspended"] is True, "at 300 live rows no new cell is created")
    _check(_cell(state, "gpt-syn-000", "high")["capability_score"] == 44.0, "an existing cell is still renewed")
    store.record_manual_cell(_typed(state), store.ManualCell("codex", "gpt-brand-new", "high", 50.0, 1.0, "operator note", None))
    _check(_cell(state, "gpt-brand-new", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "the manual record verb is not subject to the ceiling")


def test_a_manual_cell_renewed_by_live_sources_takes_the_explicit_window() -> None:
    state = RealShapeState()
    store.record_manual_cell(_typed(state), store.ManualCell("claude_code", "claude-haiku-5-5", "high", 38.0, 0.08, "operator reading", None), staleness_window_hours=720)
    _check(_cell(state, "claude-haiku-5-5", "high")["staleness_window_hours"] == 720, "the manual cell starts with its long window")
    cell = fetch.DiscoveredCell("claude_code", "anthropic", "claude-haiku-5-5", "high", _synthetic_row(0, creator="Anthropic"), None)
    report = _refresh(state, _synthetic_run([cell], score=38.0, cost=0.08))
    renewed = _cell(state, "claude-haiku-5-5", "high")
    _check(renewed["staleness_window_hours"] == _POLICY_WINDOW_HOURS, "once live sources renew it the window is the explicit fixture value")
    observations = {row["id"]: row["source_id"] for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)}
    _check(renewed["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and all(observations[i] != "manual_record" for i in renewed["agreeing_observation_ids"]), "it now cites the live observations, and the manual ones stay in history")
    _check(report.result_fields["manual_conflicts"] == [], "an agreeing manual reading is not a conflict")


def test_an_unknown_runtime_raises() -> None:
    outcome = store.ReconcileOutcome(("gemini", "x-1", "high"), CELL_ACCEPTANCE_ACCEPTED, 40.0, 1.0, _NOW.isoformat(), [], None)
    try:
        store._reconciled_cell_record(outcome, "run", staleness_window_hours=72, relative=None)  # noqa: SLF001 -- pins the provider rule
    except CatalogError as exc:
        _check(exc.code == "catalog_invalid" and "gemini" in exc.message, "an unknown runtime raises instead of defaulting to a provider")
    else:
        _check(False, "an unknown runtime raises instead of defaulting to a provider")
    _check(set(store.KNOWN_RUNTIMES) == {"claude_code", "codex"}, "the known runtimes are the adapter table's")


def test_a_harness_listed_unscored_model_keeps_its_score_and_is_never_withdrawn() -> None:
    state = RealShapeState()
    old = (_NOW - timedelta(days=10)).isoformat()
    for effort in _EFFORTS:
        _preload(state, "claude-sonnet-5", effort, runtime="claude_code", score=33.0, measured_at=old)
    _preload(state, "gpt-6-sol", "max", score=41.0, measured_at=old)
    _preload(state, "claude-haiku-4.5", "non_reasoning", runtime="claude_code", score=16.0, measured_at=old)
    _preload(state, "gpt-6-astra", "max", score=50.0, measured_at=old)
    astra = fetch.DiscoveredCell("codex", "openai", "gpt-6-astra", "max", _synthetic_row(0), None)
    run = _synthetic_run([astra], score=53.0, harness_models={"claude_code": ["claude-sonnet-5", "claude-opus-5-5"], "codex": ["gpt-6-sol", "gpt-6-astra"]})
    report = _refresh(state, run)
    stale = {(item["runtime"], item["model"]): item for item in report.result_fields["unrenewable_stale"]}
    _check(set(stale) == {("claude_code", "claude-sonnet-5"), ("codex", "gpt-6-sol")}, "harness-listed models with no leaderboard reading are named, grouped per model; the renewed model is not")
    _check(stale[("claude_code", "claude-sonnet-5")]["efforts"] == sorted(_EFFORTS) and stale[("claude_code", "claude-sonnet-5")]["age_hours"] == 240.0, "with their efforts and the age of the last reading")
    _check(("claude_code", "claude-haiku-4.5") not in stale, "a model no harness list offers is not 'unrenewable'; its absence is the harness-pass clock's business, not this run's")
    kept = [_cell(state, "claude-sonnet-5", effort) for effort in _EFFORTS] + [_cell(state, "gpt-6-sol", "max"), _cell(state, "claude-haiku-4.5", "non_reasoning")]
    _check(all(row["acceptance"] == CELL_ACCEPTANCE_ACCEPTED for row in kept), "every one of them stays accepted")
    _check([row["capability_score"] for row in kept] == [33.0] * 5 + [41.0, 16.0], "and keeps its last score")
    _check(_cell(state, "gpt-6-astra", "max")["capability_score"] == 53.0, "while the model the leaderboard still scores is renewed")
    _check("claude_code/claude-sonnet-5" in str(report.note) and "no leaderboard row" in str(report.note), "the run note names them")


def test_a_failed_page_says_nothing_about_the_models() -> None:
    state = RealShapeState()
    _preload(state, "gpt-6-sol", "high")
    report = _refresh(state, _synthetic_run([], leaderboard_ok=False, harness_models={"codex": ["gpt-6-sol"]}))
    _check(report.result_fields["unrenewable_stale"] == [], "a failed page renews nothing and says nothing about the models, so no model is called unrenewable")


def main() -> int:
    print("model_capability_discovery_store_smoke")
    test_creation_cap_defers_and_drains()
    test_the_ceiling_counts_live_rows_only()
    test_ceiling_suspends_creation_but_not_refresh_or_manual_record()
    test_a_failed_page_says_nothing_about_the_models()
    test_a_harness_listed_unscored_model_keeps_its_score_and_is_never_withdrawn()
    test_a_manual_cell_renewed_by_live_sources_takes_the_explicit_window()
    test_an_unknown_runtime_raises()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
