#!/usr/bin/env python3
"""Smoke for the discovery refresh (iss_0e805222, iss_52fdb8e5; rul_473f772f; design unit U4a).

The catalog learns which models exist from the harness lists and takes score and cost
from the leaderboard. Offline: a leaderboard page built in the live row shape from rows
captured on 2026-10-10 (the 11 roster misses and the families with no leaderboard row
included), Codex `debug models` stubs and a Claude cache v2 file written into a temporary
HOME, and `httpx.MockTransport` for detail pages.

Covers: the per-row parse with each row's own link, the effort token, creation from the
join and never from a leaderboard-only row, the creator filter, the vocabulary check, the
per-run cap and the live-row ceiling, the thin-page failed source, range checks, degraded
creation, unrenewable stale cells, alias candidates, provider from the adapter, a manual
cell renewed by live sources, and the refresh verb end to end.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/model_capability_discovery_smoke.py
"""

from __future__ import annotations

import json
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(_SRC))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_fetch as fetch  # noqa: E402
from agent_messaging_plugin import model_capability_store as store  # noqa: E402
from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.model_capability_harness import HarnessContext, ProcessResult  # noqa: E402
from agent_messaging_plugin.model_capability_store import read_cells  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_passed = 0
_failed: list[str] = []
_NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
_EFFORTS = ("low", "medium", "high", "xhigh", "max")

# (display name, context, creator, score, cost, detail slug): the 46 Anthropic and OpenAI rows of the live
# leaderboard on 2026-10-10 and one row of each of 14 other creators (80 of the 126 rows have no runtime).
_LEADERBOARD_ROWS = (
    ('Claude Opus 5.5 (max with fallback)', '1M', 'Anthropic', '58', '$5.98', 'claude-opus-5-5'),
    ('Claude Sonnet 5.5 (max with fallback)', '1M', 'Anthropic', '56', '$5.46', 'claude-sonnet-5-5'),
    ('Claude Opus 5.5 (xhigh with fallback)', '1M', 'Anthropic', '56', '$3.46', 'claude-opus-5-5-xhigh'),
    ('Claude Opus 5.5 (high with fallback)', '1M', 'Anthropic', '54', '$1.82', 'claude-opus-5-5-high'),
    ('Claude Fable 5.1 (max with fallback)', '1M', 'Anthropic', '53', '$7.63', 'claude-fable-5-1'),
    ('Claude Fable 5.1 (xhigh with fallback)', '1M', 'Anthropic', '53', '$5.98', 'claude-fable-5-1-xhigh'),
    ('Claude Sonnet 5.5 (xhigh with fallback)', '1M', 'Anthropic', '52', '$2.01', 'claude-sonnet-5-5-xhigh'),
    ('Claude Opus 5.5 (medium with fallback)', '1M', 'Anthropic', '51', '$1.34', 'claude-opus-5-5-medium'),
    ('Claude Fable 5.1 (high with fallback)', '1M', 'Anthropic', '51', '$3.91', 'claude-fable-5-1-high'),
    ('Claude Fable 5.1 (medium with fallback)', '1M', 'Anthropic', '49', '$2.98', 'claude-fable-5-1-medium'),
    ('Claude Fable 5.1 (low with fallback)', '1M', 'Anthropic', '47', '$2.37', 'claude-fable-5-1-low'),
    ('Claude Sonnet 5.5 (high with fallback)', '1M', 'Anthropic', '47', '$0.88', 'claude-sonnet-5-5-high'),
    ('Claude Haiku 5.5 (max)', '1M', 'Anthropic', '43', '$0.21', 'claude-haiku-5-5'),
    ('Claude Opus 5.5 (low with fallback)', '1M', 'Anthropic', '42', '$0.55', 'claude-opus-5-5-low'),
    ('Claude Haiku 5.5 (xhigh)', '1M', 'Anthropic', '41', '$0.12', 'claude-haiku-5-5-xhigh'),
    ('Claude Sonnet 5.5 (medium with fallback)', '1M', 'Anthropic', '41', '$0.48', 'claude-sonnet-5-5-medium'),
    ('Claude Haiku 5.5 (high)', '1M', 'Anthropic', '38', '$0.08', 'claude-haiku-5-5-high'),
    ('Claude Sonnet 5.5 (low with fallback)', '1M', 'Anthropic', '36', '$0.35', 'claude-sonnet-5-5-low'),
    ('Claude Haiku 5.5 (medium)', '1M', 'Anthropic', '34', '$0.05', 'claude-haiku-5-5-medium'),
    ('Claude Haiku 5.5 (low)', '1M', 'Anthropic', '29', '$0.02', 'claude-haiku-5-5-low'),
    ('GPT-6 Astra (max)', '1M', 'OpenAI', '53', '$3.26', 'gpt-6-astra'),
    ('GPT-6 Astra (xhigh)', '1M', 'OpenAI', '52', '$2.31', 'gpt-6-astra-xhigh'),
    ('GPT-6.1 Sol (max)', '1M', 'OpenAI', '52', '$0.72', 'gpt-6-1-sol'),
    ('GPT-6.1 Sol (xhigh)', '1M', 'OpenAI', '51', '$0.39', 'gpt-6-1-sol-xhigh'),
    ('GPT-6 Astra (high)', '1M', 'OpenAI', '51', '$1.73', 'gpt-6-astra-high'),
    ('GPT-6.1 Sol (high)', '1M', 'OpenAI', '50', '$0.32', 'gpt-6-1-sol-high'),
    ('GPT-6 Astra (medium)', '1M', 'OpenAI', '50', '$1.54', 'gpt-6-astra-medium'),
    ('GPT-6.1 Sol (medium)', '1M', 'OpenAI', '48', '$0.21', 'gpt-6-1-sol-medium'),
    ('GPT-6 Astra (low)', '1M', 'OpenAI', '46', '$0.82', 'gpt-6-astra-low'),
    ('GPT-6.1 Sol (low)', '1M', 'OpenAI', '42', '$0.13', 'gpt-6-1-sol-low'),
    ('GPT-5.6 Terra (max)', '1M', 'OpenAI', '42', '$1.40', 'gpt-5-6-terra'),
    ('GPT-6 Luna (max)', '1M', 'OpenAI', '38', '$0.07', 'gpt-6-luna'),
    ('GPT-5.6 Terra (xhigh)', '1M', 'OpenAI', '38', '$0.63', 'gpt-5-6-terra-xhigh'),
    ('GPT-6 Luna (xhigh)', '1M', 'OpenAI', '35', '$0.04', 'gpt-6-luna-xhigh'),
    ('GPT-5.6 Terra (high)', '1M', 'OpenAI', '34', '$0.34', 'gpt-5-6-terra-high'),
    ('GPT-6 Luna (high)', '1M', 'OpenAI', '33', '$0.03', 'gpt-6-luna-high'),
    ('GPT-5.6 Terra (medium)', '1M', 'OpenAI', '30', '$0.18', 'gpt-5-6-terra-medium'),
    ('GPT-6 Luna (medium)', '1M', 'OpenAI', '30', '$0.02', 'gpt-6-luna-medium'),
    ('GPT-5.6 Terra (low)', '1M', 'OpenAI', '27', '$0.14', 'gpt-5-6-terra-low'),
    ('GPT-5.5 Instant (June 2026)', '400k', 'OpenAI', '26', '$0.69', 'gpt-5-5-instant-06-26'),
    ('GPT-6 Luna (low)', '1M', 'OpenAI', '22', '$0.0045', 'gpt-6-luna-low'),
    ('GPT-5.6 Terra (non-reasoning)', '1M', 'OpenAI', '21', '$0.14', 'gpt-5-6-terra-non-reasoning'),
    ('GPT-6 Luna (non-reasoning)', '1M', 'OpenAI', '18', '$0.01', 'gpt-6-luna-non-reasoning'),
    ('gpt-oss-120b (high)', '131k', 'OpenAI', '12', '$0.11', 'gpt-oss-120b'),
    ('gpt-oss-20b (high)', '131k', 'OpenAI', '9', '$0.01', 'gpt-oss-20b'),
    ('GPT-5.5 Pro (xhigh)', '922k', 'OpenAI', '--', '--', 'gpt-5-5-pro'),
    ('Gemini 4 Argon (high)', '1M', 'Google', '53', '$1.99', 'gemini-4-argon'),
    ('Muse Spark 1.3 (max)', '1M', 'Meta', '48', '$1.60', 'muse-spark-1-3'),
    ('Grok 4.7 (xhigh)', '500k', 'SpaceXAI', '46', '$3.74', 'grok-4-7'),
    ('MiMo-V2.6-Pro', '1M', 'Xiaomi', '46', '$0.13', 'mimo-v2-6-pro'),
    ('Qwen3.8 Max (0902)', '984k', 'Alibaba', '45', '$5.41', 'qwen3-8-max'),
    ('GLM-5.3 (max)', '1M', 'Z AI', '45', '$2.01', 'glm-5-3'),
    ('Step 5 Preview', '1M', 'StepFun', '44', '$1.03', 'step-5'),
    ('Ling 3.1 Flash', '1M', 'InclusionAI', '41', '$0.99', 'ling-3-1-flash'),
    ('DeepSeek V4.1 Flash (max)', '1M', 'DeepSeek', '39', '$0.27', 'deepseek-v4-1-flash'),
    ('Mistral Large 4 Preview', '524k', 'Mistral', '38', '$1.13', 'mistral-large-4'),
    ('JT-4.1 Flash 236B A21B', '262k', 'China Mobile', '34', '--', 'jt-4-1-flash-236b-a21b'),
    ('K2 Horizon 375B A23B', '524k', 'Institute of Foundation Models', '31', '$0.00', 'k2-horizon-375b-a23b'),
    ('MiniMax-M3', '1M', 'MiniMax', '29', '$0.51', 'minimax-m3'),
    ('Quasar 438B (max)', '1M', 'Multiverse Computing', '27', '$2.02', 'quasar-438b'),
)

# Rows added to the live ones for the cases the live page does not show today.
_ALIAS_ROW = ("Claude 4.5 Haiku", "200k", "Anthropic", "16", "$0.28", "claude-4-5-haiku")  # a bare name; joins by the one alias row
_DECOY_ROW = ("GPT-6 Astra (high)", "1M", "Decoy Labs", "99", "$0.01", "decoy-astra-high")  # an unmapped creator naming a real model
_ULTRA_ROW = ("GPT-6 Sol (ultra)", "1M", "OpenAI", "60", "$1.00", "gpt-6-sol-ultra")  # an effort outside the ladder
_FLEX_ROW = ("GPT Flex (non-reasoning)", "1M", "OpenAI", "20", "$0.05", "gpt-flex")  # a model that offers non_reasoning

# Codex `debug models` (0.159.0): (slug, visibility, efforts). Seven visible models, four hidden, `ultra` on five.
_CODEX_LIST: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("gpt-6.1-sol", "list", (*_EFFORTS, "ultra")),
    ("gpt-6-astra", "list", (*_EFFORTS, "ultra")),
    ("gpt-6-sol", "list", (*_EFFORTS, "ultra")),
    ("gpt-6-luna", "list", (*_EFFORTS, "ultra")),
    ("gpt-5.6-sol", "list", (*_EFFORTS, "ultra")),
    ("gpt-5.6-terra", "list", _EFFORTS),
    ("gpt-5.6-luna", "list", _EFFORTS),
    ("gpt-flex", "list", ("non_reasoning", "low")),
    ("memory4-dream", "hide", ("low",)),
    ("gpt-reserve", "hide", ("low",)),
)
# The Claude cc cache: (id, section, thinking type, efforts). The 4.5 Haiku entry has no effort axis.
_CLAUDE_LIST: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("claude-opus-5-5", "main", "effort", _EFFORTS),
    ("claude-fable-5-1", "main", "effort", _EFFORTS),
    ("claude-sonnet-5-5", "main", "effort", _EFFORTS),
    ("claude-haiku-5-5", "main", "effort", _EFFORTS),
    ("claude-haiku-4-5-20251001", "overflow", "none", ()),
    ("claude-sonnet-5", "overflow", "effort", _EFFORTS),
    ("claude-opus-5", "overflow", "effort", _EFFORTS),
)


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


# --------------------------------------------------------------------------- fixtures


def _row_html(row: Sequence[str | None]) -> str:
    name, context, creator, score, cost, slug = row
    link = "<span>Model</span><span>Providers</span>" if slug is None else f'<a class="x" href="/models/{slug}">Model<svg></svg></a><a class="x" href="/models/{slug}/providers">Providers<svg></svg></a>'
    return (
        f'<tr class="border-b"><td><div class="font-semibold">{name}</div></td><td><div class="text-center">{context}</div></td>'
        f'<td><div class="flex"><img alt="{creator}"/><span>{creator}</span></div></td><td><div class="text-center">{score}</div></td>'
        f'<td><div class="text-center">{cost}</div></td><td><div>56</div></td><td><div>49.25</div></td><td><div>58.11</div></td>'
        f"<td><div>{link}</div></td></tr>"
    )


def _page(rows: Sequence[Sequence[str | None]], *, header: bool = True) -> str:
    head = "<thead><tr><th>Model</th><th>Context</th></tr></thead>" if header else ""
    return "<html><head><script>var x = '<tr>';</script></head><body><table>" + head + "<tbody>" + "".join(_row_html(row) for row in rows) + "</tbody></table></body></html>"


def _full_rows() -> list[Sequence[str | None]]:
    return [_DECOY_ROW, *_LEADERBOARD_ROWS, _ALIAS_ROW, _ULTRA_ROW, _FLEX_ROW]


def _detail_page(score: str, cost: str) -> str:
    return ("<html><body>Intelligence|Updated|#|45| / |202|" + score + "|Artificial Analysis Intelligence Index|blah|" + cost + "|Cost per Intelligence Index task|blah</body></html>").replace("|", "<span></span>")


def _detail_pages(rows: Sequence[Sequence[str | None]]) -> dict[str, tuple[str, str]]:
    return {str(row[5]): (str(row[3]), str(row[4])) for row in rows if row[5] is not None and row[3] != "--" and row[4] not in ("--", "*")}


def _client(rows: Sequence[Sequence[str | None]], *, page: str | None = None, details: Mapping[str, tuple[str, str] | int] | None = None) -> httpx.Client:
    """A mock transport that serves the leaderboard and, per slug, an agreeing detail page unless `details` overrides it (an int is a status)."""
    pages = dict(_detail_pages(rows))
    overrides = dict(details or {})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/leaderboards/models":
            return httpx.Response(200, text=page if page is not None else _page(rows))
        slug = path.removeprefix("/models/")
        answer = overrides.get(slug, pages.get(slug))
        if isinstance(answer, int):
            return httpx.Response(answer, text="not found")
        if answer is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=_detail_page(*answer))

    return httpx.Client(transport=httpx.MockTransport(handler))


def _codex_payload(rows: Sequence[tuple[str, str, tuple[str, ...]]]) -> dict[str, object]:
    return {"models": [{"slug": slug, "display_name": slug, "visibility": visibility, "supported_reasoning_levels": [{"effort": effort, "description": effort} for effort in efforts]} for slug, visibility, efforts in rows]}


def _write_claude_cache(home: Path, entries: Sequence[tuple[str, str, str, tuple[str, ...]]] = _CLAUDE_LIST) -> None:
    models: list[dict[str, object]] = []
    for model_id, section, kind, efforts in entries:
        thinking: dict[str, object] = {"type": kind}
        if kind == "effort":
            thinking["effort_options"] = [{"id": effort, "name": effort.title()} for effort in efforts]
        models.append({"id": model_id, "name": model_id, "section": section, "thinking": thinking})
    directory = home / ".claude" / "cache" / "model-catalog"
    directory.mkdir(parents=True, exist_ok=True)
    payload = {"version": 2, "fetchedAt": int(_NOW.timestamp() * 1000), "staleAt": int((_NOW + timedelta(hours=1)).timestamp() * 1000), "catalog": {"surface": "cc", "config": {"id": "cfg", "models": models}, "state": {}}}
    (directory / "org-hash-cc.json").write_text(json.dumps(payload), encoding="utf-8")


def _fake_binary(root: Path) -> Path:
    binary = root / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    binary.chmod(0o755)
    return binary


class _CodexStub:
    """Answers `--version` and `debug models` from a fixture; never starts a process."""

    def __init__(self, rows: Sequence[tuple[str, str, tuple[str, ...]]] | None) -> None:
        self.rows = rows

    def __call__(self, argv: Sequence[str], timeout_seconds: float) -> ProcessResult:
        if tuple(argv[1:]) == ("--version",):
            return ProcessResult(0, "codex-cli 0.159.0\n", "")
        if self.rows is None:
            return ProcessResult(1, "", "boom")
        return ProcessResult(0, json.dumps(_codex_payload(self.rows)), "")


def _context(home: Path, *, codex: Sequence[tuple[str, str, tuple[str, ...]]] | None = _CODEX_LIST, claude: bool = True, claude_entries: Sequence[tuple[str, str, str, tuple[str, ...]]] = _CLAUDE_LIST) -> HarnessContext:
    if claude:
        _write_claude_cache(home, claude_entries)
    return HarnessContext(home=home, codex_binary=_fake_binary(home), run=_CodexStub(codex), now=_NOW)


def _discover(rows: Sequence[Sequence[str | None]] | None = None, *, page: str | None = None, details: Mapping[str, tuple[str, str] | int] | None = None, codex: Sequence[tuple[str, str, tuple[str, ...]]] | None = _CODEX_LIST, claude: bool = True, claude_entries: Sequence[tuple[str, str, str, tuple[str, ...]]] = _CLAUDE_LIST, catalog: Mapping[str, Sequence[str]] | None = None) -> fetch.DiscoveryRun:
    used = _full_rows() if rows is None else list(rows)
    with tempfile.TemporaryDirectory() as tmp, _client(used, page=page, details=details) as client:
        return fetch.discover_readings(client, _context(Path(tmp), codex=codex, claude=claude, claude_entries=claude_entries), catalog_models=dict(catalog or {}))


def _keys(run: fetch.DiscoveryRun) -> set[tuple[str, str, str]]:
    return {cell.key for cell in run.cells}


def _refresh(state: RealShapeState, run: fetch.DiscoveryRun) -> store.RefreshReport:
    return store.reconcile_discovery(_typed(state), store.open_refresh_run(_typed(state), trigger="manual"), run, now=_NOW)


def _cell(state: RealShapeState, model: str, effort: str) -> dict[str, Any]:
    return next(row for row in read_cells(_typed(state)) if row["model"] == model and row["effort"] == effort)



# --------------------------------------------------------------------------- store-level helpers


def _preload(state: RealShapeState, model: str, effort: str, *, acceptance: str = CELL_ACCEPTANCE_ACCEPTED, runtime: str = "codex", score: float = 30.0, measured_at: str | None = None, window: int = 72) -> None:
    provider = "openai" if runtime == "codex" else "anthropic"
    record: dict[str, Any] = {
        "provider": provider, "runtime": runtime, "model": model, "effort": effort, "capability_score": score, "cost_per_task_usd": 1.0, "relative_cost_multiplier": None,
        "currency": "USD", "effort_supported": True, "measured_at": measured_at or _NOW.isoformat(), "accepted_at": _NOW.isoformat(), "acceptance": acceptance,
        "agreeing_observation_ids": [], "disagreement_note": None, "staleness_window_hours": window, "last_refresh_run_id": None,
    }
    state.upsert_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_CELL, "record": record, "conflict_columns": ["runtime", "model", "effort"]})


# --------------------------------------------------------------------------- parse


def test_per_row_parse_uses_the_rows_own_link_and_effort_token() -> None:
    rows = fetch.parse_leaderboard_rows(_page(_full_rows()))
    by_name = {row.display_name: row for row in rows}
    _check(len(rows) == len(_full_rows()), f"every row of the table parses, in page order (got {len(rows)})")
    opus = by_name["Claude Opus 5.5 (max with fallback)"]
    _check((opus.base_name, opus.effort, opus.creator, opus.detail_slug) == ("Claude Opus 5.5", "max", "anthropic", "claude-opus-5-5"), "a row carries its base name, effort, lowercased creator and its own detail slug")
    _check(by_name["Claude Opus 5.5 (xhigh with fallback)"].detail_slug == "claude-opus-5-5-xhigh", "the slug is the row's link, not a slug rule")
    _check(by_name["GPT-6 Luna (non-reasoning)"].effort == "non_reasoning", "the non-reasoning token maps to the catalog spelling")
    instant = by_name["GPT-5.5 Instant (June 2026)"]
    _check(instant.effort is None and instant.base_name == instant.display_name, "a parenthesis that is not an effort word stays in the base name")
    _check(by_name["Claude 4.5 Haiku"].effort is None, "a bare name has no effort token")
    _check(by_name["GPT-5.5 Pro (xhigh)"].score_text == "--", "a row with no score is parsed and keeps its placeholder")
    for name in ("gpt-oss-120b (high)", "Claude Haiku 5.5 (low)", "GPT-6 Luna (non-reasoning)"):
        _check(name in by_name, f"the live roster miss {name!r} parses")
    no_link = fetch.parse_leaderboard_rows(_page([("Foo Bar (high)", "1M", "OpenAI", "40", "$1.00", None)]))
    _check(len(no_link) == 1 and no_link[0].detail_slug is None, "a row with no Model link parses with no detail slug")


# --------------------------------------------------------------------------- creation from the join


def test_harness_model_with_a_leaderboard_row_becomes_a_cell() -> None:
    run = _discover()
    keys = _keys(run)
    _check(all(("codex", "gpt-6.1-sol", effort) in keys for effort in _EFFORTS), "a dotted Codex launch id joins and keeps its dot, all five efforts")
    _check(all(("claude_code", "claude-haiku-5-5", effort) in keys for effort in _EFFORTS), "a Claude model on the list becomes five cells with no code naming it")
    _check(("claude_code", "claude-haiku-4-5-20251001", "non_reasoning") in keys, "the one alias row joins 'Claude 4.5 Haiku' to its dated launch id as non_reasoning")
    _check(len(keys) == 42, f"42 cells in all: 21 Claude, 21 Codex (got {len(keys)})")
    _check({cell.provider for cell in run.cells if cell.runtime == "claude_code"} == {"anthropic"} and {cell.provider for cell in run.cells if cell.runtime == "codex"} == {"openai"}, "provider comes from the runtime adapter")
    _check(all(c.creation_note is None for c in run.cells), "an ok harness list needs no flag on a new cell")
    _check(run.sources_ok == (fetch.SOURCE_LEADERBOARD_HTML, fetch.SOURCE_MODEL_DETAIL_HTML) and not run.failures, "both sources fetched cleanly")


def test_a_leaderboard_only_row_is_not_a_cell() -> None:
    run = _discover()
    keys = _keys(run)
    _check(("codex", "gpt-oss-120b", "high") not in keys and ("codex", "gpt-oss-20b", "high") not in keys, "a leaderboard row no harness model matches creates no cell")
    _check(not any("pro" in cell.model or "instant" in cell.model for cell in run.cells), "'GPT-5.5 Pro' and 'GPT-5.5 Instant' create no cell")
    listed = " | ".join(run.listed_not_launchable)
    _check("GPT-5.5 Pro (xhigh)" in listed and "gpt-oss-120b (high)" in listed, "they are named as listed but not launchable")
    _check(not {"gpt-6-sol", "gpt-5.6-sol", "gpt-5.6-luna"} & {cell.model for cell in run.cells}, "harness models with no leaderboard row create no cell either")
    _check(not any(cell.model in ("memory4-dream", "gpt-reserve") for cell in run.cells), "hidden Codex models are never new cells")


def test_unmapped_creator_rows_create_nothing() -> None:
    run = _discover()
    astra_high = next(cell for cell in run.cells if cell.key == ("codex", "gpt-6-astra", "high"))
    _check(astra_high.row.score_text == "51" and astra_high.row.creator == "openai", "a decoy row from an unmapped creator never stands in for the real model's row")
    counts = dict(run.unmapped_creators)
    _check("decoy labs" in counts and "google" in counts and "openai" not in counts, "unmapped creators are counted per creator")
    _check(len(run.unmapped_creators) <= 10, "the report names at most the top ten")
    _check(not any(cell.row.creator not in ("anthropic", "openai") for cell in run.cells), "no cell comes from a creator without a runtime")


def test_unknown_effort_never_becomes_a_cell() -> None:
    run = _discover()
    _check(not any(cell.effort == "ultra" for cell in run.cells), "a row at an effort outside the ladder creates no cell even where the harness offers it")
    _check(set(run.efforts_outside_vocabulary) == {"gpt-6.1-sol", "gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"}, "the five models that offer ultra are reported")
    _check(all(efforts == ("ultra",) for efforts in run.efforts_outside_vocabulary.values()), "the report names the effort")


def test_non_reasoning_maps_and_an_unoffered_effort_is_not_launchable() -> None:
    run = _discover()
    flex = [cell for cell in run.cells if cell.model == "gpt-flex"]
    _check([cell.effort for cell in flex] == ["non_reasoning"], "a model that offers non_reasoning gets a non_reasoning cell from its '(non-reasoning)' row")
    _check(("codex", "gpt-6-luna", "non_reasoning") not in _keys(run) and ("codex", "gpt-5.6-terra", "non_reasoning") not in _keys(run), "a leaderboard effort the harness does not offer is not launchable")
    _check(any("GPT-6 Luna (non-reasoning)" in name for name in run.listed_not_launchable), "and is listed as such")
    haiku = next(cell for cell in run.cells if cell.model == "claude-haiku-4-5-20251001")
    _check(haiku.effort == "non_reasoning", "a bare name joins as non_reasoning only on a model with no effort axis")


def test_alias_candidates_are_reported_and_never_joined() -> None:
    entries = (*_CLAUDE_LIST, ("claude-nova-4", "overflow", "effort", _EFFORTS))
    rows = [*_LEADERBOARD_ROWS, ("Claude 4 Nova (high)", "1M", "Anthropic", "44", "$1.10", "claude-4-nova-high")]
    run = _discover(rows, claude_entries=entries)
    _check({"runtime": "claude_code", "name": "Claude 4 Nova", "model_id": "claude-nova-4"} in [dict(item) for item in run.alias_candidates], "a name and a launch id with equal sorted tokens are an alias candidate")
    _check(not any(cell.model == "claude-nova-4" for cell in run.cells), "the candidate is reported only; nothing is joined by a guess")


def test_a_renamed_creator_label_is_reported_and_changes_no_cell() -> None:
    rows = [(*row[:2], "OpenAI Inc" if row[2] == "OpenAI" else row[2], *row[3:]) for row in _LEADERBOARD_ROWS]
    run = _discover(rows)
    _check(run.creator_label_unmatched == ("codex",), "a page that parses plenty of rows but none for a mapped creator is reported loudly")
    _check(not any(cell.runtime == "codex" for cell in run.cells) and any(cell.runtime == "claude_code" for cell in run.cells), "Codex creates nothing and Claude is unaffected")


# --------------------------------------------------------------------------- the failed page and the range checks


def test_a_thin_page_is_a_failed_source() -> None:
    run = _discover(list(_LEADERBOARD_ROWS[:20]))
    _check(not run.cells and not run.readings and not run.leaderboard_ok, "a page that parses fewer than 40 rows creates and renews nothing")
    _check(any(source == fetch.SOURCE_LEADERBOARD_HTML and "thin_page" in reason for source, reason in run.failures), "the failure is named in sources_failed")
    _check(fetch.SOURCE_LEADERBOARD_HTML not in run.sources_ok, "and the leaderboard is not reported as an ok source")


def test_a_healthy_page_creates_cells() -> None:
    state = RealShapeState()
    report = _refresh(state, _discover())
    _check(report.result_fields["cells_created"] == 30 and len(report.result_fields["deferred"]) == 12, "a healthy page creates cells, 30 now and the other 12 deferred")


def _oor_page() -> tuple[list[tuple[str, str, str, str, str, str]], tuple[tuple[str, str, tuple[str, ...]], ...]]:
    rows = [*_LEADERBOARD_ROWS, ("Broken Score (high)", "1M", "OpenAI", "250", "$0.10", "broken-score"), ("Junk Cost (high)", "1M", "OpenAI", "40", "$1.2.3", "junk-cost")]
    return rows, (*_CODEX_LIST, ("broken-score", "list", ("high",)), ("junk-cost", "list", ("high",)))


def _leaderboard_readings_of(run: fetch.DiscoveryRun, model: str, metric: str) -> list[fetch.Reading]:
    return [r for r in run.readings if (r.model, r.source_id, r.metric) == (model, fetch.SOURCE_LEADERBOARD_HTML, metric)]


def test_out_of_range_reading_is_rejected() -> None:
    rows, codex = _oor_page()
    run = _discover(rows, codex=codex)
    broken = _leaderboard_readings_of(run, "broken-score", "intelligence_index")
    _check([(r.fetch_status, r.value_number) for r in broken] == [("parse_error", None)], "a score of 250 is a parse_error reading, not a value")
    _check("out of range" in str(broken[0].value_text), "and says why")
    junk = _leaderboard_readings_of(run, "junk-cost", "cost_per_task_usd")
    _check([(r.fetch_status, r.value_number) for r in junk] == [("parse_error", None)], "a cost that is not a number is a parse_error reading and does not crash the parse")
    _check("not a number" in str(junk[0].value_text), "and says why")


def test_out_of_range_reading_leaves_the_prior_value() -> None:
    rows, codex = _oor_page()
    state = RealShapeState()
    _preload(state, "broken-score", "high", score=30.0)
    _refresh(state, _discover(rows, codex=codex))
    _check(_cell(state, "broken-score", "high")["capability_score"] == 30.0, "the cell keeps its prior value when the reading is rejected")
    _check(not any(row["model"] == "broken-score" and row["effort"] != "high" for row in read_cells(_typed(state))), "and nothing else is written for it")


def test_degraded_mode_creates_a_flagged_cell_for_a_well_formed_id() -> None:
    run = _discover(claude=False)
    derived = [cell for cell in run.cells if cell.runtime == "claude_code"]
    _check(("claude_code", "claude-haiku-5-5", "high") in {cell.key for cell in derived}, "when the Claude list is unreadable, a leaderboard row creates a cell under the id derived from its name")
    _check(all(cell.creation_note and cell.creation_note.startswith("launch id derived from the leaderboard name; claude_code harness list unreadable") for cell in derived), "every such cell is flagged with why")
    _check(run.harness["claude_code"]["status"] == "unreadable" and run.harness["codex"]["status"] == "ok", "the result names each runtime's list status")
    _check(any("leaderboard-only creation applies" in note for note in run.advisories), "and says loudly that leaderboard-only creation applies")
    state = RealShapeState()
    _refresh(state, run)
    _check("launch id derived" in str(_cell(state, "claude-haiku-5-5", "high")["disagreement_note"]), "the flag is written on the new cell")
    _check(_cell(state, "claude-haiku-5-5", "high")["acceptance"] == CELL_ACCEPTANCE_ACCEPTED, "the cell is accepted on the two sources, not held back")


def test_derived_cells_are_marked_and_start_their_clock() -> None:
    run = _discover(claude=False)
    _check(all(cell.derived_launch_id for cell in run.cells if cell.runtime == "claude_code"), "each cell of an unreadable list is marked derived_launch_id, so creation needs two agreeing sources")
    _check(not any(cell.derived_launch_id for cell in run.cells if cell.runtime == "codex"), "a cell whose launch id an ok list vouches for is not marked derived")
    state = RealShapeState()
    _refresh(state, run)
    _check(_cell(state, "claude-haiku-5-5", "high").get("harness_unverified_since") is not None, "and the new cell's harness-absence clock is running from its creation")


def test_degraded_mode_rejects_a_malformed_id() -> None:
    rows = [*_LEADERBOARD_ROWS, ("Solo (high)", "1M", "Anthropic", "40", "$1.00", "solo")]
    run = _discover(rows, claude=False)
    _check(not any(cell.model == "solo" for cell in run.cells), "a derived launch id the creator's id shape refuses creates no cell")
    _check(any("not well formed" in note and "'solo'" in note for note in run.advisories), "and the refusal is named")


def test_degraded_mode_renews_known_models_and_flags_only_creation() -> None:
    state = RealShapeState()
    _preload(state, "claude-opus-5-5", "max", runtime="claude_code", score=40.0)
    run = _discover(claude=False, catalog={"claude_code": ["claude-opus-5-5"]})
    _refresh(state, run)
    renewed = _cell(state, "claude-opus-5-5", "max")
    _check(renewed["capability_score"] == 58.0 and renewed["disagreement_note"] is None, "a cell the catalog already holds is renewed from the leaderboard and not flagged")


def test_codex_cache_list_flags_new_cells_as_degraded() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        cache = home / ".codex" / "models_cache.json"
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({"fetched_at": _NOW.isoformat(), "client_version": "0.156.0", "models": _codex_payload(_CODEX_LIST)["models"]}), encoding="utf-8")
        _write_claude_cache(home)
        binary_not_supplied = Path("<codex binary not configured>")
        with _client(_full_rows()) as client:
            run = fetch.discover_readings(client, HarnessContext(home=home, codex_binary=binary_not_supplied, run=_CodexStub(None), now=_NOW), catalog_models={})
    _check(run.harness["codex"]["status"] == "degraded" and "never resolved from PATH" in str(run.harness["codex"]["detail"]), "no binary supplied: the cache file is read as a degraded list and the detail says why")
    codex_cells = [cell for cell in run.cells if cell.runtime == "codex"]
    _check(len(codex_cells) == 21 and all(str(cell.creation_note).startswith("created from a degraded codex harness list") for cell in codex_cells), "its models still create cells, each flagged")
    _check(not any(cell.creation_note for cell in run.cells if cell.runtime == "claude_code"), "the Claude cells, from an ok list, carry no flag")
    _check(not any(cell.derived_launch_id for cell in run.cells), "a cache-file Codex list names real launch ids, so none of its cells is derived")


# --------------------------------------------------------------------------- cells that cannot be renewed


# --------------------------------------------------------------------------- manual cells and the adapter table


# --------------------------------------------------------------------------- the verb end to end


def _fake_codex(root: Path) -> Path:
    binary = root / "bin" / "codex"
    binary.parent.mkdir(parents=True, exist_ok=True)
    Path(str(binary) + ".models.json").write_text(json.dumps(_codex_payload(_CODEX_LIST)), encoding="utf-8")
    binary.write_text('#!/bin/sh\ncase "$1" in\n  --version) echo "codex-cli 0.159.0";;\n  debug) cat "$0.models.json";;\nesac\n', encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    return binary


def test_the_refresh_verb_end_to_end() -> None:
    state = RealShapeState()
    with tempfile.TemporaryDirectory() as tmp, _client(_full_rows()) as client:
        home = Path(tmp)
        _write_claude_cache(home)
        result = verbs.refresh_model_capability_catalog(_typed(state), trigger="manual", client=client, codex_binary=_fake_codex(home), home=home)
    _check(result["harness"]["codex"]["status"] == "ok" and result["harness"]["claude_code"]["status"] == "ok", "both harness lists are read and their statuses are in the result")
    _check(result["cells_created"] == 30 and result["creation_capped"] is True and len(result["deferred"]) == 12, "the result carries cells_created, creation_capped and deferred")
    _check(result["cells_accepted"] == 30 and result["sources_failed"] == [], "the created cells are accepted on two agreeing sources")
    _check({"unrenewable_stale", "alias_candidates", "unmapped_creators", "efforts_outside_vocabulary", "manual_conflicts", "advisories"} <= set(result), "and the other discovery fields")
    run_row = state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN)[0]
    _check(run_row["status"] == "completed" and run_row["cells_accepted"] == 30 and "deferred" in str(run_row["note"]), "the run row records the counts and a note")
    with tempfile.TemporaryDirectory() as tmp, _client(_full_rows()) as client:
        home = Path(tmp)
        _write_claude_cache(home)
        second = verbs.refresh_model_capability_catalog(_typed(state), trigger="manual", client=client, home=home)
    _check(second["harness"]["codex"]["status"] == "unreadable" and "never resolved from PATH" in second["harness"]["codex"]["detail"], "with no binary supplied and no cache file the Codex list is unreadable, said loudly, and nothing is refused")
    _check(second["cells_created"] >= 1, "and the refresh still ran and created flagged cells")


def main() -> int:
    print("model_capability_discovery_smoke")
    test_per_row_parse_uses_the_rows_own_link_and_effort_token()
    test_harness_model_with_a_leaderboard_row_becomes_a_cell()
    test_a_leaderboard_only_row_is_not_a_cell()
    test_unmapped_creator_rows_create_nothing()
    test_unknown_effort_never_becomes_a_cell()
    test_non_reasoning_maps_and_an_unoffered_effort_is_not_launchable()
    test_alias_candidates_are_reported_and_never_joined()
    test_a_renamed_creator_label_is_reported_and_changes_no_cell()
    test_a_thin_page_is_a_failed_source()
    test_a_healthy_page_creates_cells()
    test_out_of_range_reading_is_rejected()
    test_out_of_range_reading_leaves_the_prior_value()
    test_degraded_mode_creates_a_flagged_cell_for_a_well_formed_id()
    test_derived_cells_are_marked_and_start_their_clock()
    test_degraded_mode_rejects_a_malformed_id()
    test_degraded_mode_renews_known_models_and_flags_only_creation()
    test_codex_cache_list_flags_new_cells_as_degraded()
    test_the_refresh_verb_end_to_end()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
