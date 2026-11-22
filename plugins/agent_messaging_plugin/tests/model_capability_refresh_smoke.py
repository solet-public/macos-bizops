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
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
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
    TABLE_MODEL_CAPABILITY_OBSERVATION,
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


def _detail_fixture(score: str, cost: str, *, title: str = "") -> str:
    head = f"<head><title>{title}</title></head>" if title else ""
    return ("<html>" + head + "<body>Model summary|Intelligence|Updated|#|45| / |202|" + score + "|Artificial Analysis Intelligence Index|blah|" + cost + "|Cost per Intelligence Index task|blah</body></html>").replace("|", "<span></span>")  # tag-strip collapses these right back to '|' delimiters


def _own_page_fixture(name: str, score: str, cost: str) -> str:
    """A model's own detail page, titled as the live ones were up to 2026-11-13T23:15Z (recorded: 'Claude Sonnet 5.5 (high) - Intelligence, Performance & Price Analysis')."""
    return _detail_fixture(score, cost, title=f"{name} - Intelligence, Performance &amp; Price Analysis | Artificial Analysis")


def _family_fixture(family: str, score: str, cost: str) -> str:
    """The page every detail URL has served since 2026-11-14T05:30Z (recorded: 'Claude Sonnet 5.5 Models - Intelligence, Performance & Price Comparison'): the family's, with its top-effort values."""
    return _detail_fixture(score, cost, title=f"{family} Models - Intelligence, Performance &amp; Price Comparison | Artificial Analysis")


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
    _check(sonnet_note.startswith("single source: detail page unavailable (not_listed: the detail page was unavailable)"), "the cell note names its single-source fallback; the source-failure row above retains HTTP 404")
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
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 30}, now=_NOW)
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
        picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 50}, now=datetime.now(UTC) + timedelta(minutes=1))
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


def test_a_family_page_is_not_a_detail_reading() -> None:
    """The detail URL now serves the model family's page, whose score and cost are the family's top-effort values.
    Read as this cell's own page it disagrees with every lower effort's leaderboard row (a gate nobody can pass,
    rul_becb4bed); it is no reading at all, named as such, and a model's own page is still a reading."""
    row = fetch.LeaderboardRow("Claude Sonnet 5.5 (high)", "Claude Sonnet 5.5", "high", "anthropic", "42", "$1.64", "claude-sonnet-5-5-high", "fixture")
    cell = fetch.DiscoveredCell("claude_code", "anthropic", "claude-sonnet-5-5", "high", row, None)
    at = "2026-11-14T11:15:00+00:00"
    family = fetch.parse_model_detail_html(_family_fixture("Claude Sonnet 5.5", "55", "$10.02"), cell=cell, fetched_at=at)
    _check(len(family) == 1 and (family[0].metric, family[0].fetch_status, family[0].value_number) == ("intelligence_index", "not_listed", None), "a family page is one not_listed score reading carrying no value")
    _check(family[0].value_text == fetch.FAMILY_PAGE_NO_DETAIL == "family page: no per-effort detail", "and it names why")
    _check(not any(reading.metric == "cost_per_task_usd" for reading in family), "the family's top-effort cost is not read either")
    bare = fetch.parse_model_detail_html(_detail_fixture("55", "$10.02", title="Claude Sonnet 5.5 Models - Intelligence, Performance & Price Comparison | Artificial Analysis"), cell=cell, fetched_at=at)
    _check(len(bare) == 1 and bare[0].fetch_status == "not_listed", "the title is recognised with a bare ampersand as well")
    own = fetch.parse_model_detail_html(_own_page_fixture("Claude Sonnet 5.5 (high)", "42", "$1.64"), cell=cell, fetched_at=at)
    _check([(reading.metric, reading.fetch_status, reading.value_number) for reading in own] == [("intelligence_index", "ok", 42.0), ("cost_per_task_usd", "ok", 1.64)], "a model's own page, titled with its effort, is still read as both metrics")
    untitled = fetch.parse_model_detail_html(_detail_fixture("42", "$1.64"), cell=cell, fetched_at=at)
    _check(untitled[0].fetch_status == "ok" and untitled[0].value_number == 42.0, "a page of any other shape is read as it always was: only the recognised family title is refused")


_FAMILY_PAGE_ROWS: tuple[tuple[str, str, str, str, str, str], ...] = (
    ("Claude Sonnet 5 (max)", "1M", "Anthropic", "38", "$5.09", "claude-sonnet-5"),
    ("Claude Opus 5 (low)", "1M", "Anthropic", "39", "$1.10", "claude-opus-5-low"),
)


def _family_page_handler(request: httpx.Request) -> httpx.Response:
    """A non-max effort's URL serves the family page, a max effort's URL serves its own agreeing page."""
    path = request.url.path
    if path == "/leaderboards/models":
        return httpx.Response(200, text=_leaderboard(_FAMILY_PAGE_ROWS))
    if path == "/models/claude-opus-5-low":
        return httpx.Response(200, text=_family_fixture("Claude Opus 5", "55", "$10.02"))  # 16 points above its row: a conflict if it were read
    if path == "/models/claude-sonnet-5":
        return httpx.Response(200, text=_own_page_fixture("Claude Sonnet 5 (max)", "38", "$5.09"))
    return httpx.Response(404, text="not found")


def _check_family_page_cell(cells: list[dict[str, Any]]) -> None:
    opus_low = _find_cell(cells, "claude-opus-5", "low")
    note = str(opus_low["disagreement_note"])
    _check(opus_low["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and opus_low["capability_score"] == 39.0 and opus_low["cost_per_task_usd"] == 1.10, "a non-max cell whose detail URL serves the family page is accepted with its own leaderboard values")
    _check(note.startswith("single source: detail page unavailable (") and fetch.FAMILY_PAGE_NO_DETAIL in note, "and the note says it is a single source and that the page was the family's")
    _check(len(opus_low["agreeing_observation_ids"]) == 2, "it cites only the leaderboard's score and cost")


def _check_own_page_cell(cells: list[dict[str, Any]]) -> None:
    sonnet_max = _find_cell(cells, "claude-sonnet-5", "max")
    _check(sonnet_max["acceptance"] == CELL_ACCEPTANCE_ACCEPTED and sonnet_max["capability_score"] == 38.0, "a max cell with a real agreeing detail page is accepted with the agreed score")
    _check(len(sonnet_max["agreeing_observation_ids"]) == 4 and not sonnet_max["disagreement_note"], "and still takes the two-source path: four readings, no single-source note")


def _check_family_page_run(state: RealShapeState, result: dict[str, Any]) -> None:
    _check(result["cells_accepted"] == 2 and result["cells_conflicted"] == 0 and result["sources_failed"] == [], "the run counts both accepted, none conflicted, and no source failed (the family page was fetched fine)")
    observations = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_OBSERVATION)
    family = [row for row in observations if row["source_id"] == fetch.SOURCE_MODEL_DETAIL_HTML and row["model"] == "claude-opus-5"]
    _check(len(family) == 1 and (family[0]["fetch_status"], family[0]["value_number"], family[0]["value_text"]) == ("not_listed", None, fetch.FAMILY_PAGE_NO_DETAIL), "the family page is kept as evidence, named, with no value")


def test_a_family_page_leaves_the_leaderboard_alone_and_a_models_own_page_still_corroborates() -> None:
    """The refresh end to end: a non-max effort's URL serves the family page (the family's top-effort values), the max
    effort's URL serves its own agreeing page. The non-max cell is accepted from its own leaderboard row with the
    single-source note; the max cell still takes the two-source path."""
    state = RealShapeState()
    result = _refresh(state, _family_page_handler)
    cells = read_cells(_typed(state))
    _check_family_page_cell(cells)
    _check_own_page_cell(cells)
    _check_family_page_run(state, result)


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
    picked = verbs.select_dispatch_tier(_typed(state), {"required_score": 30}, now=datetime.now(UTC) + timedelta(minutes=1))
    _check(picked["excluded"]["not_accepted"] >= 2, "the selector counts the withdrawn cells as not_accepted")


def test_the_registered_result_schema_declares_every_field_the_verb_returns() -> None:
    from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: PLC0415 -- the plugin import is heavy; only this test needs it

    declared = set(AgentMessagingPlugin.refresh_model_capability_catalog._platform_process_metadata.return_value_schema.properties)  # type: ignore[attr-defined]  # noqa: SLF001
    returned = {"job_id", "status"}
    _check(declared == returned, f"the registered return_value_schema declares exactly the fields the verb returns (declared-only {sorted(declared - returned)}, returned-only {sorted(returned - declared)})")


def test_the_roster_is_gone() -> None:
    names = ("_ROSTER", "_RosterEntry", "roster_cells", "fetch_all_readings", "_detail_slug", "_parse_effort")
    _check(not [name for name in names if hasattr(fetch, name)], "no hand-kept roster, slug rule or roster enumeration remains in the fetch module")


def _operation_fixture(plugin: Any) -> tuple[Any, Any, Any]:
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

    ledger = cast(Any, Ledger())
    ledger.rows["flows"] = [{"id": "smoke-flow", "status": "active", "trigger_type": "bridge_process_call"}]
    manager = AsyncJobManager(ledger, FlowRuntimeGraph(ledger))
    plugin.logger = logging.getLogger(plugin.name)
    if plugin.orchestrator_ref is None:
        plugin.orchestrator_ref = SimpleNamespace(async_job_manager=manager)
    else:
        plugin.orchestrator_ref.async_job_manager = manager
    return manager, JobService(ledger, manager), ledger


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


def _assert_queued_before_work_completion(receipt: Any, work_completed: threading.Event, verb: str) -> None:
    assert "job_id" in receipt.get("data", {}), f"{verb}: queued_receipt_before_work_completion: {receipt}"
    assert not work_completed.is_set(), f"{verb}: queued_receipt_after_work_completion: {receipt}"


def _assert_one_persisted_request(ledger: Any, job_id: str, verb: str) -> None:
    with ledger.lock:
        requests = [row for row in ledger.rows.get("job_payload", []) if row["payload_type"] == "request"]
    assert len(requests) == 1 and requests[0]["job_id"] == job_id, f"{verb}: exactly_one_persisted_request_while_parked: {requests!r}"


def _negative_control_inline_work() -> None:
    """Run the real dispatch path with work executed inline before its receipt."""
    from agent_messaging_plugin import plugin as module

    plugin = module.AgentMessagingPlugin()
    with patch.object(plugin, "_get_state_service", return_value=object()):
        prove_queued_operation(plugin, "refresh_model_capability_catalog", {"trigger": "manual"}, module, "catalog_refresh", {"status": "ok"}, {"status": "ok"}, inline_work=True)


def prove_queued_operation(plugin: Any, verb: str, params: dict[str, Any], boundary: Any, attribute: str, value: Any, expected: dict[str, Any] | None, *, failure: bool = False, inline_work: bool = False) -> dict[str, Any]:
    """Real platform dispatch and job retrieval, with only the work boundary replaced."""
    from ananta.core.actions.action_processor import ActionProcessor
    from ananta.core.plugins.plugin_manager import PluginManager

    manager, service, ledger = _operation_fixture(plugin)
    touched: list[int] = []
    entered = threading.Event()
    release = threading.Event()
    completed = threading.Event()
    caller_thread: list[int] = []

    def slow(*args: Any, **kwargs: Any) -> Any:
        touched.append(threading.get_ident())
        entered.set()
        assert release.wait(5), f"{verb}: worker_gate_timeout: touched={touched!r} release={release.is_set()}"
        try:
            if isinstance(value, Exception):
                raise value
            return value(*args, **kwargs) if callable(value) else value
        finally:
            completed.set()

    action = SimpleNamespace(id="queued-operation", process_key=f"plugin::{plugin.name}::{verb}", session_id="smoke-session", flow_id="smoke-flow", context_id=None, source_plugin=None, flow_token_id=None)
    processor = ActionProcessor(PluginManager())

    def submit() -> Any:
        caller_thread.append(threading.get_ident())
        return processor._execute_plugin_method(plugin, verb, params, cast(Any, action))  # pyright: ignore[reportPrivateUsage]

    def inline_submit(_plugin: Any, action_name: str, parameters: dict[str, Any], state: dict[str, Any], operation: Any) -> dict[str, Any]:
        receipt = manager.create_job(
            plugin_name=_plugin.name,
            action_name=action_name,
            request_data={"notes": f"{_plugin.name}.{action_name}", "parameters": parameters},
            job_metadata={"session_id": state["session_id"], "flow_id": state["flow_id"]},
        )
        assert receipt["action_status"] == "completed", receipt
        release.set()
        operation(parameters)
        return {"data": {"job_id": receipt["data"]["job_id"], "status": "queued"}}

    with patch.object(boundary, attribute, side_effect=slow), patch.object(plugin._operation_jobs, "submit", side_effect=inline_submit) if inline_work else nullcontext():
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="queued-operation-caller") as executor:
            started = time.perf_counter()
            future = executor.submit(submit)
            assert entered.wait(5), f"{verb}: worker_did_not_enter: touched={touched!r}"
            try:
                receipt = future.result(timeout=5)
                elapsed = time.perf_counter() - started
                _assert_queued_before_work_completion(receipt, completed, verb)
                assert receipt["data"]["status"] == "queued"
                job_id = receipt["data"]["job_id"]
                _assert_one_persisted_request(ledger, job_id, verb)
                request = manager.get_job_payload(job_id, "request")["data"]["payload"]
                assert request["parameters"] == params, f"{verb}: durable_original_request"
                state = service.get_job(job_id)["data"]["result"]["job"]
                assert state["status"] not in {"completed", "error"}, f"{verb}: terminal_before_release: {state}"
                result_read = manager.get_job_payload(job_id, "result")
                assert result_read.get("action_status") != "completed", f"{verb}: result_before_release: {result_read}"
            finally:
                release.set()
            job = _wait_operation(plugin, service, job_id, verb)
    assert touched and caller_thread and caller_thread[0] not in touched, f"{verb}: work_must_run_off_caller"
    _assert_operation_outcome(job, verb, expected, value, failure)
    print(f"  PASS  {plugin.name}.{verb}: queued receipt before parked work, persisted request, worker, job_service result/error (elapsed={elapsed:.6f}s)")
    return job


def test_born_async_fleet_messaging_verbs() -> None:
    from agent_messaging_plugin import plugin as module

    report = {"status": "ok", "advisories": []}
    for verb, attribute, params in (
        ("refresh_model_capability_catalog", "catalog_refresh", {"trigger": "manual"}),
        ("seed_model_capability_catalog", "catalog_seed", {}),
    ):
        for failure in (False, True):
            plugin = module.AgentMessagingPlugin()
            error = module.CatalogError("controlled_failure", "controlled failure")
            with patch.object(plugin, "_get_state_service", return_value=object()):
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
    if "--negative-control" in sys.argv:
        _negative_control_inline_work()
        return 0
    print("model_capability_refresh_smoke")
    test_born_async_fleet_messaging_verbs()
    test_the_roster_is_gone()
    test_detail_score_parses_with_and_without_updated_token()
    test_a_family_page_is_not_a_detail_reading()
    test_a_family_page_leaves_the_leaderboard_alone_and_a_models_own_page_still_corroborates()
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
