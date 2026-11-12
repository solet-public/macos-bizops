#!/usr/bin/env python3
"""End-to-end smoke for `refresh_model_capability_catalog` (iss_d136ae29; iss_0e805222, iss_52fdb8e5):
verb -> harness lists + fetch (mocked transport, real-shaped HTML fixtures) -> parse -> join ->
reconcile -> state write -> `select_dispatch_tier` can serve the result.

No live network: `httpx.MockTransport` answers every request from fixture HTML built from real
numbers captured in a live spot-check (see model_capability_fetch.py's module docstring), not
fabricated. The harness lists are real-shaped files in a temporary HOME: a Claude Code cache v2
file and a Codex `debug models` answer from a small executable standing in for the launching
binary. This proves the FULL pipeline wires together correctly; `model_capability_reconcile_smoke.py`
covers the reconciler's decision logic in isolation and `model_capability_discovery_smoke.py`
covers the discovery rules (join, caps, ceiling, degraded lists) exhaustively.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_refresh_smoke.py
"""

from __future__ import annotations

import json
import logging
import stat
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import httpx

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_fetch as fetch  # noqa: E402
from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.model_capability_store import read_cells  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_WITHDRAWN,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)  # on or after the newest usage_economics effective_at
_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# Real numbers, captured live 2026-09-21 (see model_capability_fetch.py's module docstring for the
# corroborating manual spot-check). The page needs 40 rows to count as a source, so 39 rows from a
# creator with no runtime stand around them.
_AI_ROWS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("Claude Sonnet 5 (max)", "1M", "Anthropic", "38", "$5.09", "claude-sonnet-5"),
    ("Claude Opus 5 (low)", "1M", "Anthropic", "39", "$1.10", "claude-opus-5-low"),
    ("GPT-6 Astra (max)", "1M", "OpenAI", "53", "$3.26", "gpt-6-astra"),
    ("Claude Fable 5.1 (max with fallback)", "1M", "Anthropic", "53", "$7.63", "claude-fable-5-1"),
)
_FILLER_ROWS: tuple[tuple[str, str, str, str, str, str], ...] = tuple((f"Filler Model {i:02d}", "128k", "FillerCo", "20", "$0.10", f"filler-{i:02d}") for i in range(39))

# The harness lists: Claude's cc cache, and what the launching Codex binary answers for `debug models`.
_CLAUDE_MODELS = ("claude-sonnet-5", "claude-opus-5", "claude-fable-5-1")
_CODEX_MODELS = ("gpt-6-astra", "gpt-6.1-sol")


def _row_html(row: Sequence[str]) -> str:
    name, context, creator, score, cost, slug = row
    return (
        f"<tr><td><div>{name}</div></td><td><div>{context}</div></td><td><div><span>{creator}</span></div></td><td><div>{score}</div></td><td><div>{cost}</div></td>"
        f'<td><div>56</div></td><td><div>49.25</div></td><td><div>58.11</div></td><td><div><a href="/models/{slug}">Model<svg></svg></a><a href="/models/{slug}/providers">Providers<svg></svg></a></div></td></tr>'
    )


def _leaderboard(rows: Sequence[Sequence[str]]) -> str:
    return "<html><body><table><tbody>" + "".join(_row_html(row) for row in (*rows, *_FILLER_ROWS)) + "</tbody></table></body></html>"


def _detail_fixture(score: str, cost: str) -> str:
    return ("<html><body>Model summary|Intelligence|Updated|#|45| / |202|" + score + "|Artificial Analysis Intelligence Index|blah|" + cost + "|Cost per Intelligence Index task|blah</body></html>").replace("|", "<span></span>")  # tag-strip collapses these right back to '|' delimiters


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/leaderboards/models":
        return httpx.Response(200, text=_leaderboard(_AI_ROWS))
    if path == "/models/gpt-6-astra":
        return httpx.Response(200, text=_detail_fixture("53", "$3.26"))  # agrees with the leaderboard -> accepted
    if path == "/models/claude-opus-5-low":
        return httpx.Response(200, text=_detail_fixture("50", "$1.10"))  # 11pt spread, past tolerance -> conflict
    if path == "/models/claude-sonnet-5":
        return httpx.Response(404, text="not found")  # fetch failure -> the leaderboard alone, with the reason
    return httpx.Response(200, text="<html><body>no data here</body></html>")  # parse_error -> the leaderboard alone, with the reason


def _typed(fake: RealShapeState) -> StateManagementInterface:
    return cast("StateManagementInterface", fake)


def _write_harness(home: Path) -> Path:
    """Claude's cache under HOME and a Codex binary that lists `_CODEX_MODELS`; returns the binary path."""
    entries = [{"id": model, "name": model, "section": "main", "thinking": {"type": "effort", "effort_options": [{"id": effort, "name": effort} for effort in _EFFORTS]}} for model in _CLAUDE_MODELS]
    directory = home / ".claude" / "cache" / "model-catalog"
    directory.mkdir(parents=True)
    stamp = int(datetime.now(UTC).timestamp() * 1000)
    (directory / "org-hash-cc.json").write_text(json.dumps({"version": 2, "fetchedAt": stamp, "staleAt": stamp + 3_600_000, "catalog": {"surface": "cc", "config": {"models": entries}}}), encoding="utf-8")
    listing = {"models": [{"slug": slug, "visibility": "list", "supported_reasoning_levels": [{"effort": effort} for effort in _EFFORTS]} for slug in _CODEX_MODELS]}
    binary = home / "bin" / "codex"
    binary.parent.mkdir()
    Path(str(binary) + ".models.json").write_text(json.dumps(listing), encoding="utf-8")
    binary.write_text('#!/bin/sh\ncase "$1" in\n  --version) echo "codex-cli 0.159.0";;\n  debug) cat "$0.models.json";;\nesac\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def _refresh(state: RealShapeState, handler: Any, *, trigger: str = "manual") -> dict[str, Any]:  # noqa: ANN401 -- an httpx.MockTransport handler
    """The real refresh verb over `handler`, with hermetic harness lists (also how the platform-health guard smoke drives it)."""
    with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        home = Path(tmp)
        binary = _write_harness(home)
        return verbs.refresh_model_capability_catalog(_typed(state), trigger=trigger, client=client, codex_binary=binary, home=home)


def _find_cell(cells: list[dict[str, Any]], model: str, effort: str) -> dict[str, Any]:
    return next(c for c in cells if c["model"] == model and c["effort"] == effort)


def _check_run_level_reporting(result: dict[str, Any]) -> None:
    _check(result["trigger"] == "manual", "run records its own trigger")
    _check("artificial_analysis_leaderboard_html" in result["sources_ok"], "leaderboard source reported ok")
    _check(any("claude-sonnet-5" in f for f in result["sources_failed"]), "the 404'd detail fetch is named in sources_failed, not silently dropped")
    _check(result["cells_created"] == 4 and result["deferred"] == [] and result["creation_capped"] is False, "the four scored cells the harness offers are created, none deferred")


def _check_astra_accepted(cells: list[dict[str, Any]]) -> None:
    astra_max = _find_cell(cells, "gpt-6-astra", "max")
    _check(astra_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "gpt-6-astra max: 2 agreeing sources -> accepted")
    _check(astra_max["capability_score"] == 53.0 and astra_max["provider"] == "openai", "accepted score is the agreed value; provider comes from the Codex adapter")
    _check(len(astra_max["agreeing_observation_ids"]) == 4, "agreeing_observation_ids names all 4 corroborating readings (score x2 + cost x2)")


def _check_opus_conflict(cells: list[dict[str, Any]]) -> None:
    opus_low = _find_cell(cells, "claude-opus-5", "low")
    _check(opus_low["acceptance"] == CELL_ACCEPTANCE_CROSSCHECK_CONFLICT, "opus-5 low: 39 vs 50 exceeds tolerance -> crosscheck_conflict")
    disagreement_note = opus_low["disagreement_note"]
    _check(isinstance(disagreement_note, str) and "intelligence_index" in disagreement_note, "conflict note names the disagreeing metric")


def _check_single_source_cells(cells: list[dict[str, Any]]) -> None:
    sonnet_max = _find_cell(cells, "claude-sonnet-5", "max")
    fable_max = _find_cell(cells, "claude-fable-5-1", "max")
    sonnet_note = str(sonnet_max["disagreement_note"])
    _check(sonnet_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and sonnet_max["capability_score"] == 38.0, "sonnet-5 max: detail fetch 404'd -> the leaderboard alone is accepted, no longer pending forever")
    _check(sonnet_note.startswith("single source: detail page unavailable (http_error") and "404" in sonnet_note, "and the note says it is a single source and why")
    _check(fable_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and "parse_error" in str(fable_max["disagreement_note"]), "fable-5.1 max: a detail page that does not parse is the same single-source case, named")
    _check(sonnet_max["provider"] == "anthropic", "provider comes from the Claude Code adapter")
    _check(not [c for c in cells if c["acceptance"] == "pending_crosscheck"], "the refresh writes no pending_crosscheck cell")


def _check_run_row_counts(state: RealShapeState) -> None:
    runs = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)
    run_row = next(r for r in runs if r["trigger"] == "manual")
    _check(run_row["cells_accepted"] == 3, f"run row's cells_accepted matches (got {run_row['cells_accepted']})")
    _check(run_row["cells_conflicted"] == 1, f"run row's cells_conflicted matches (got {run_row['cells_conflicted']})")
    _check(any("claude-sonnet-5" in f for f in run_row["sources_failed"]), "run row's own sources_failed column carries the failure, not just the verb's return value")


def test_refresh_accepts_conflicts_and_names_single_sources() -> None:
    state = RealShapeState()
    result = _refresh(state, _handler)
    _check_run_level_reporting(result)
    cells = read_cells(_typed(state))
    _check_astra_accepted(cells)
    _check_opus_conflict(cells)
    _check_single_source_cells(cells)
    _check_run_row_counts(state)


def test_accepted_cell_is_immediately_servable() -> None:
    """Closes the loop this whole fix exists for: a real refresh run's accepted cell is exactly what unblocks `select_dispatch_tier`."""
    state = RealShapeState()
    _refresh(state, _handler)
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 30, "billing_objective": "metered_usd"}, now=_NOW)
    _check(picked["selected"]["model"] == "gpt-6-astra" and picked["selected"]["effort"] == "max", "the cheapest cell the refresh accepted is what the selector now serves")


def test_second_refresh_never_downgrades_a_prior_acceptance_on_thin_evidence() -> None:
    state = RealShapeState()
    _refresh(state, _handler)

    def thin_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/leaderboards/models":
            return httpx.Response(200, text=_leaderboard([(*row[:3], "60", *row[4:]) if row[5] == "gpt-6-astra" else row for row in _AI_ROWS]))  # astra now 60: 7 points from the prior 53
        return httpx.Response(404, text="not found")  # every detail page now fails

    result = _refresh(state, thin_handler)
    astra_max = _find_cell(read_cells(_typed(state)), "gpt-6-astra", "max")
    _check(astra_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and astra_max["capability_score"] == 53.0, "a single-source reading 7 points from the prior accepted value keeps the prior")
    _check(result["single_source_unconfirmed"] == ["codex/gpt-6-astra@max"], "and is reported as single_source_unconfirmed")
    _check(result["cells_unchanged"] >= 1, "the preserved cell is counted as unchanged, not silently dropped from the report")
    near = _refresh(state, lambda request: httpx.Response(200, text=_leaderboard(_AI_ROWS)) if request.url.path == "/leaderboards/models" else httpx.Response(404, text="not found"))
    renewed = _find_cell(read_cells(_typed(state)), "gpt-6-astra", "max")
    _check(renewed["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and "single source" in str(renewed["disagreement_note"]), "a single-source reading within tolerance renews the cell and says so")
    _check(near["single_source_unconfirmed"] == [] and renewed["agreeing_observation_ids"] != astra_max["agreeing_observation_ids"], "with new observations cited")


def test_a_new_model_is_selectable_with_no_code_change() -> None:
    """GPT-6.1 Sol is on the Codex list and on the leaderboard: it becomes selectable by discovery alone (iss_91f14c81 needed a roster edit)."""
    leaderboard = [("GPT-6.1 Sol (high)", "1M", "OpenAI", "50", "$0.32", "gpt-6-1-sol-high")]

    def handler(request: httpx.Request, *, agree: bool) -> httpx.Response:
        if request.url.path == "/leaderboards/models":
            return httpx.Response(200, text=_leaderboard(leaderboard))
        if request.url.path == "/models/gpt-6-1-sol-high" and agree:
            return httpx.Response(200, text=_detail_fixture("50", "$0.32"))
        return httpx.Response(404, text="missing independent observation")

    for agree in (True, False):
        state = RealShapeState()
        _refresh(state, lambda request, agree=agree: handler(request, agree=agree))
        cells = read_cells(_typed(state))
        high = _find_cell(cells, "gpt-6.1-sol", "high")
        _check(len(cells) == 1 and high["capability_score"] == 50.0 and high["cost_per_task_usd"] == 0.32, "only the pair the leaderboard scores becomes a cell; its launch id keeps the harness's dot")
        if agree:
            _check(high["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and len(high["agreeing_observation_ids"]) == 4, "two agreeing artifacts accept the exact score and cost")
        else:
            _check(high["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and len(high["agreeing_observation_ids"]) == 2 and "single source" in str(high["disagreement_note"]), "one artifact accepts with a note naming the missing corroboration")
        picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 50, "billing_objective": "metered_usd"}, now=datetime.now(UTC) + timedelta(minutes=1))
        _check((picked["selected"]["model"], picked["selected"]["effort"]) == ("gpt-6.1-sol", "high"), "the accepted fresh cell is selectable under the existing provider policy")


def test_detail_score_parses_with_and_without_updated_token() -> None:
    """iss_6cb8fe86: the live page dropped the Updated token; the score must
    parse either way, and a page with no score must stay a parse_error."""
    row = fetch.LeaderboardRow("Claude Sonnet 5.5 (high)", "Claude Sonnet 5.5", "high", "anthropic", "47", "$1.50", "claude-sonnet-5-5-high", "fixture")
    cell = fetch.DiscoveredCell("claude_code", "anthropic", "claude-sonnet-5-5", "high", row, None)
    tail = "|Artificial Analysis Intelligence Index|blah|$1.50|Cost per Intelligence Index task|blah"

    def score_reading(page_text: str) -> fetch.Reading:
        html = ("<html><body>" + page_text + "</body></html>").replace("|", "<span></span>")
        readings = fetch.parse_model_detail_html(html, cell=cell, fetched_at="2026-10-09T00:00:00+00:00")
        return next(reading for reading in readings if reading.metric == "intelligence_index")

    with_token = score_reading("Intelligence|Updated|#|45| / |202|47" + tail)
    no_token = score_reading("Intelligence|#|28| / |226|47" + tail)
    _check(with_token.fetch_status == "ok" and with_token.value_number == 47.0, "detail page with the Updated token gives the score")
    _check(no_token.fetch_status == "ok" and no_token.value_number == 47.0, "detail page without the Updated token gives the score")
    missing = score_reading("Intelligence|#|28| / |226|Artificial Analysis Intelligence Index|blah|$1.50|Cost per Intelligence Index task")
    _check(missing.fetch_status == "parse_error" and missing.value_number is None, "a page with no score is parse_error, not a zero")
    words_only = score_reading("Artificial Analysis Intelligence Index of 47 is the Intelligence score")
    _check(words_only.fetch_status == "parse_error", "the pattern does not match loose text containing the words")
    differing = score_reading("Intelligence|#|28| / |226|51" + tail)
    _check(differing.fetch_status == "ok" and differing.value_number == 51.0, "a differing score is read as-is for the reconciler to compare")
    out_of_range = score_reading("Intelligence|#|28| / |226|250" + tail)
    _check(out_of_range.fetch_status == "parse_error" and out_of_range.value_number is None, "a score outside 0-100 is a parse_error, not a value")


def _seed_cell(state: RealShapeState, model: str, effort: str, *, acceptance: str = CELL_ACCEPTANCE_ACCEPTED, clock: str | None = None) -> None:
    now = datetime.now(UTC).isoformat()
    record: dict[str, Any] = {
        "provider": "anthropic",
        "runtime": "claude_code",
        "model": model,
        "effort": effort,
        "capability_score": 30.0,
        "cost_per_task_usd": 1.0,
        "relative_cost_multiplier": None,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": now,
        "accepted_at": now,
        "acceptance": acceptance,
        "agreeing_observation_ids": [],
        "disagreement_note": None,
        "staleness_window_hours": 72,
        "last_refresh_run_id": "run-before",
        "harness_unverified_since": clock,
    }
    state.upsert_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_CELL, "record": record, "conflict_columns": ["runtime", "model", "effort"]})


def test_the_refresh_runs_the_harness_pass_with_the_real_lists() -> None:
    """The real verb over the real list readers: the Claude cache lists three models, the stub Codex binary two."""
    state = RealShapeState()
    old = (datetime.now(UTC) - timedelta(hours=100)).isoformat()
    _seed_cell(state, "claude-gone", "high")  # no harness list names it: the clock starts
    _seed_cell(state, "claude-gone-old", "high", clock=old)  # absent for longer than the window: withdrawn
    _seed_cell(state, "claude-opus-5", "medium", acceptance=CELL_ACCEPTANCE_WITHDRAWN, clock=old)  # listed again: the refresh's own withdrawal is reversed
    _seed_cell(state, "claude-sonnet-5", "max", acceptance=CELL_ACCEPTANCE_WITHDRAWN)  # withdrawn by a person; the leaderboard scores it
    result = _refresh(state, _handler)
    rows = {(row["model"], row["effort"]): row for row in read_cells(_typed(state))}
    _check(result["harness_clock_started"] == ["claude_code/claude-gone@high"] and rows[("claude-gone", "high")]["harness_unverified_since"] is not None, "an unlisted model's clock starts and the cell keeps serving")
    _check(result["auto_withdrawn"] == ["claude_code/claude-gone-old@high"] and rows[("claude-gone-old", "high")]["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN, "after one window the refresh withdraws it")
    _check(result["auto_reinstated"] == ["claude_code/claude-opus-5@medium"] and rows[("claude-opus-5", "medium")]["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "a relisted model's automatic withdrawal is reversed")
    _check(rows[("claude-sonnet-5", "max")]["acceptance"] == CELL_ACCEPTANCE_WITHDRAWN and result["withdrawn_left"] == ["claude_code/claude-sonnet-5@max"], "a withdrawal made by a person survives a refresh whose readings reach the cell")
    _check(any("claude-gone" in line and "does not list" in line for line in result["advisories"]), "the absent models are named in the advisories")
    _check("withdrawn: the harness no longer lists them" in str(state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)[-1]["note"]), "the run row's note carries the harness pass")
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 30, "billing_objective": "metered_usd"}, now=datetime.now(UTC) + timedelta(minutes=1))
    _check(picked["excluded"]["not_accepted"] >= 2, "the selector counts the withdrawn cells as not_accepted")


def test_the_registered_result_schema_declares_every_field_the_verb_returns() -> None:
    from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: PLC0415 -- the plugin import is heavy; only this test needs it

    declared = set(AgentMessagingPlugin.refresh_model_capability_catalog._platform_process_metadata.return_value_schema.properties)  # type: ignore[attr-defined]  # noqa: SLF001
    returned = {"job_id", "status"}
    _check(declared == returned, f"the registered return_value_schema declares exactly the fields the verb returns (declared-only {sorted(declared - returned)}, returned-only {sorted(returned - declared)})")


def test_the_roster_is_gone() -> None:
    names = ("_ROSTER", "_RosterEntry", "roster_cells", "fetch_all_readings", "_detail_slug", "_parse_effort")
    _check(not [name for name in names if hasattr(fetch, name)], "no hand-kept roster, slug rule or roster enumeration remains in the fetch module")


def _operation_fixture(plugin: Any) -> tuple[Any, Any]:
    root = Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(root / "ananta/tests/core/actions"))
    from ananta.core.state.async_job_manager import AsyncJobManager
    from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph
    from ananta.services.job_service.service import JobService
    from async_job_fixture_support import JobState  # pyright: ignore[reportMissingImports]

    class Ledger(JobState):  # pyright: ignore[reportUntypedBaseClass]
        def write_state(self, namespace: str, data: dict[str, Any], calling_service: str | None = None, calling_namespace: str | None = None) -> dict[str, Any]:
            record = {"created_at": datetime.now(UTC).isoformat(), **data["record"]}
            return super().write_state(namespace, {**data, "record": record}, calling_service, calling_namespace)

    ledger = Ledger()
    ledger.rows["flows"] = [{"id": "smoke-flow", "status": "active", "trigger_type": "bridge_process_call"}]
    manager = AsyncJobManager(ledger, FlowRuntimeGraph(ledger))
    plugin.logger = logging.getLogger(plugin.name)
    if plugin.orchestrator_ref is None:
        plugin.orchestrator_ref = SimpleNamespace(async_job_manager=manager)
    else:
        plugin.orchestrator_ref.async_job_manager = manager
    return manager, JobService(ledger, manager)


def _wait_operation(plugin: Any, service: Any, job_id: str, verb: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:
        job = service.get_job(job_id)["data"]["result"]["job"]
        if job["status"] in {"completed", "error"}:
            break
        assert time.monotonic() < deadline, f"{verb}: worker did not finish"
        time.sleep(0.005)
    worker = plugin._operation_jobs._thread
    if worker is not None:
        worker.join(2)
        assert not worker.is_alive()
    return job


def _assert_operation_outcome(job: dict[str, Any], verb: str, expected: dict[str, Any] | None, value: Any, failure: bool) -> None:
    assert job["status"] == ("error" if failure else "completed"), job
    if expected is not None:
        assert job["result"].get("data", {}) == expected, f"{verb}: original_result_payload"
    if failure:
        if isinstance(value, dict):
            assert job["error"] == value["error"], f"{verb}: original_error_payload"
        else:
            assert "controlled failure" in str(job["error"]), f"{verb}: original_error_payload"
    else:
        assert job["error"] is None


def prove_queued_operation(plugin: Any, verb: str, params: dict[str, Any], boundary: Any, attribute: str, value: Any, expected: dict[str, Any] | None, *, failure: bool = False) -> dict[str, Any]:
    """Real platform dispatch and job retrieval, with only the work boundary replaced."""
    from ananta.core.actions.action_processor import ActionProcessor
    from ananta.core.plugins.plugin_manager import PluginManager

    manager, service = _operation_fixture(plugin)
    touched: list[int] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        touched.append(threading.get_ident())
        time.sleep(0.1)
        if isinstance(value, Exception):
            raise value
        return value(*args, **kwargs) if callable(value) else value

    action = SimpleNamespace(id="queued-operation", process_key=f"plugin::{plugin.name}::{verb}", session_id="smoke-session", flow_id="smoke-flow", context_id=None, source_plugin=None, flow_token_id=None)
    processor = ActionProcessor(PluginManager())
    with patch.object(boundary, attribute, side_effect=slow):
        started = time.perf_counter()
        receipt: Any = processor._execute_plugin_method(plugin, verb, params, cast(Any, action))  # pyright: ignore[reportPrivateUsage]
        elapsed = time.perf_counter() - started
        assert "job_id" in receipt.get("data", {}), f"{verb}: queued_receipt_before_work_completion: {receipt}"
        assert elapsed < 0.05, f"{verb}: fast_return_bound: {elapsed}"
        assert receipt["data"]["status"] == "queued"
        job_id = receipt["data"]["job_id"]
        request = manager.get_job_payload(job_id, "request")["data"]["payload"]
        assert request["parameters"] == params, f"{verb}: durable_original_request"
        job = _wait_operation(plugin, service, job_id, verb)
    assert touched and threading.get_ident() not in touched, f"{verb}: work_must_run_off_caller"
    _assert_operation_outcome(job, verb, expected, value, failure)
    print(f"  PASS  {plugin.name}.{verb}: fast receipt, persisted request, worker, job_service result/error")
    return job


def test_born_async_fleet_messaging_verbs() -> None:
    from agent_messaging_plugin import plugin as module

    report = {"verdict": "not_stalled", "ready_and_owned_count": 2, "produced_count": 1, "advisories": []}
    for verb, attribute, params in (
        ("refresh_model_capability_catalog", "catalog_refresh", {"trigger": "manual"}),
        ("seed_model_capability_catalog", "catalog_seed", {}),
        ("sweep_fleet_stall", "run_fleet_stall_sweep", {}),
        ("sweep_fleet_stall_cron", "run_fleet_stall_sweep", {}),
    ):
        for failure in (False, True):
            plugin = module.AgentMessagingPlugin()
            error = module.CatalogError("controlled_failure", "controlled failure") if "catalog" in verb else module.FleetStallSweepError("controlled failure")
            with patch.object(plugin, "_get_state_service", return_value=object()), patch.object(plugin, "_fleet_stall_register_reader", return_value=object()):
                prove_queued_operation(plugin, verb, params, module, attribute, error if failure else report, {} if failure else report, failure=failure)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def main() -> int:
    print("model_capability_refresh_smoke")
    test_born_async_fleet_messaging_verbs()
    test_the_roster_is_gone()
    test_detail_score_parses_with_and_without_updated_token()
    test_refresh_accepts_conflicts_and_names_single_sources()
    test_accepted_cell_is_immediately_servable()
    test_second_refresh_never_downgrades_a_prior_acceptance_on_thin_evidence()
    test_a_new_model_is_selectable_with_no_code_change()
    test_the_refresh_runs_the_harness_pass_with_the_real_lists()
    test_the_registered_result_schema_declares_every_field_the_verb_returns()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
