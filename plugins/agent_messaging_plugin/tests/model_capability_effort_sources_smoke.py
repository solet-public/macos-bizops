"""Measured Artificial Analysis API and embedded model page effort-source smoke."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import model_capability_discovery_smoke as base

from agent_messaging_plugin import model_capability_fetch as fetch
from agent_messaging_plugin import model_capability_reconcile as reconcile
from agent_messaging_plugin.schema import CELL_ACCEPTANCE_ACCEPTED

_check = base._check
_NOW = base._NOW
_EFFORTS = base._EFFORTS
_LEADERBOARD_ROWS = base._LEADERBOARD_ROWS
_CODEX_LIST = base._CODEX_LIST
_page = base._page
_context = base._context
_keys = base._keys
_preload = base._preload
_refresh = base._refresh
_cell = base._cell
RealShapeState = base.RealShapeState


def _check_measured_fixture() -> tuple[str, dict[str, Any], list[fetch.EffortVariant], list[fetch.EffortVariant]]:
    fixtures = Path(__file__).with_name("fixtures")
    page = (fixtures / "aa_model_detail_astra-for-law-6-2_2026-11-17T2125Z.html").read_text(encoding="utf-8")
    api_payload = json.loads((fixtures / "aa_language_models_free_per_effort.json").read_text(encoding="utf-8"))
    page_variants, page_notes = fetch.parse_page_variants(page)
    api_variants, api_notes = fetch.parse_api_variants(api_payload)
    _check_fixture_readings(page_variants, page_notes, api_variants, api_notes)
    return page, api_payload, page_variants, api_variants


def _below_top_count(variants: list[fetch.EffortVariant]) -> int:
    counts: dict[str, int] = {}
    for variant in variants:
        counts[variant.release_slug] = counts.get(variant.release_slug, 0) + 1
    return sum(count - 1 for count in counts.values())


def _check_fixture_readings(page_variants: list[fetch.EffortVariant], page_notes: list[str], api_variants: list[fetch.EffortVariant], api_notes: list[str]) -> None:
    _check(
        len(page_variants) == 27 and not page_notes and len({item.key for item in page_variants}) == 27,
        "saved page has 27 distinct scored effort records",
    )
    _check(
        len({item.release_slug for item in page_variants}) == 9 and _below_top_count(page_variants) == 18,
        "27 records span nine releases, 18 below each release's top effort",
    )
    _check(
        len(api_variants) == 15 and not api_notes,
        "trimmed measured API response has 15 exact variant score/cost pairs",
    )


def _check_api_readings(api_payload: dict[str, Any], api_variants: list[fetch.EffortVariant]) -> None:
    typed_variant: fetch.EffortVariant = api_variants[0]
    _check(typed_variant.effort in _EFFORTS, "API parser returns typed per-effort records")
    api_cell = fetch.DiscoveredCell("codex", "openai", "gpt-6.2-astra", "medium", api_variants[0].as_row(), None)
    _check(len(fetch.variant_readings(api_cell, api_variants[0], fetched_at=_NOW.isoformat())) == 2, "one API variant emits score and cost readings")
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=api_payload))) as api_client:
        fetched_variants, fetched_notes = fetch.fetch_api_variants(api_client, "fixture-key")
    _check(len(fetched_variants) == 15 and not fetched_notes, "bounded Data API fetch decodes the measured response")


def _check_page_readings(page: str, page_variants: list[fetch.EffortVariant]) -> None:
    detail_readings = []
    for variant in page_variants:
        cell = fetch.DiscoveredCell("codex", variant.creator, variant.release_slug, variant.effort, variant.as_row(), None)
        detail_readings.extend(fetch.parse_model_detail_html(page, cell=cell, fetched_at=_NOW.isoformat()))
    _check(
        len(detail_readings) == 54 and all(item.fetch_status == "ok" for item in detail_readings),
        "detail parser yields 27 score and 27 cost readings from the saved page",
    )


def _check_precision(page_variants: list[fetch.EffortVariant], api_variants: list[fetch.EffortVariant]) -> None:
    page_by_key = {item.key: item for item in page_variants}
    api_by_key = {item.key: item for item in api_variants}
    expected = (
        ("gpt-6-2-astra", "medium", 56.9986252138657, 1.6176877362867854),
        ("gpt-6-2-astra", "high", 60.0882280338247, 2.073874957362806),
        ("gpt-6-2-astra", "xhigh", 61.5602325371388, 2.5211433764120375),
        ("gpt-6-2-sol", "medium", 52.6777305876603, 0.40605590960076604),
        ("gpt-6-2-sol", "high", 54.8898658241942, 0.563644652221075),
        ("gpt-6-2-sol", "xhigh", 55.7351067246438, 0.6479676730055945),
    )
    for release, effort, score, cost in expected:
        row = page_by_key[("openai", release, effort)]
        _check(
            (row.score, row.cost) == (score, cost),
            f"{release}@{effort} retains full page precision",
        )
        _check(
            ("openai", release, effort) in api_by_key,
            f"{release}@{effort} has a measured API counterpart",
        )
    _check(
        all(("openai", release, "low") not in page_by_key for release in ("gpt-6-2-astra", "gpt-6-2-sol")),
        "page has no invented low record for either GPT-6.2 release",
    )
    _check(
        all(item.effort != "ultra" for item in (*page_variants, *api_variants)),
        "neither source invents ultra",
    )


def _check_joined_efforts(page: str, api_payload: dict[str, Any], *, api_astra: bool = True) -> fetch.DiscoveryRun:
    rows = [
        *_LEADERBOARD_ROWS[:40],
        ("GPT-6.2 Astra (max)", "1M", "OpenAI", "62", "$4.00", "gpt-6-2-astra"),
        ("GPT-6.2 Astra (medium)", "1M", "OpenAI", "-", "-", "gpt-6-2-astra"),
        ("GPT-6.2 Sol (max)", "1M", "OpenAI", "57", "$1.01", "gpt-6-2-sol"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/language/models/free":
            return httpx.Response(200, json=api_payload)
        if request.url.path == "/leaderboards/models":
            return httpx.Response(200, text=_page(rows))
        if request.url.path in (
            "/models/gpt-6-2-astra",
            "/models/gpt-6-2-sol",
            "/models/claude-opus-5-5",
        ):
            return httpx.Response(200, text=page)
        return httpx.Response(404, text="not found")

    codex = (
        *_CODEX_LIST,
        ("gpt-6.2-astra", "list", (*_EFFORTS, "ultra")),
        ("gpt-6.2-sol", "list", (*_EFFORTS, "ultra")),
    )
    with (
        tempfile.TemporaryDirectory() as tmp,
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
    ):
        run = fetch.discover_readings(client, _context(Path(tmp), codex=codex), catalog_models={}, api_key="fixture-key")
    _check_joined_results(run, api_astra)
    return run


def _check_joined_results(run: fetch.DiscoveryRun, api_astra: bool) -> None:
    keys = _keys(run)
    _check(
        all(("codex", launch, effort) in keys for launch in ("gpt-6.2-astra", "gpt-6.2-sol") for effort in _EFFORTS),
        "API and page variants map by joined harness launch id plus exact offered effort",
    )
    _check(
        ("codex", "gpt-6.2-astra", "ultra") not in keys,
        "unoffered vocabulary cannot create an ultra cell",
    )
    _check_astra_sources(run, api_astra)


def _check_astra_sources(run: fetch.DiscoveryRun, api_astra: bool) -> None:
    astra = [item for item in run.readings if item.model == "gpt-6.2-astra" and item.effort == "medium" and item.fetch_status == "ok"]
    _check(
        {item.source_id for item in astra} == ({fetch.SOURCE_API, fetch.SOURCE_EMBEDDED} if api_astra else {fetch.SOURCE_EMBEDDED}) and len(astra) == (4 if api_astra else 2),
        "Astra medium carries API and embedded score/cost observations",
    )


def _check_source_reconciliation(run: fetch.DiscoveryRun) -> list[reconcile.ObservationRef]:
    astra = [item for item in run.readings if item.model == "gpt-6.2-astra" and item.effort == "medium" and item.fetch_status == "ok"]
    refs = [reconcile.ObservationRef(f"obs{i}", item) for i, item in enumerate(astra)]
    agreed = reconcile.reconcile_cell(("codex", "gpt-6.2-astra", "medium"), refs)
    _check(
        agreed is not None and agreed.capability_score == 57 and agreed.cost_per_task_usd == 1.6177,
        "agreeing sources renew from the API's exact published pair",
    )
    return refs


def _check_page_only(refs: list[reconcile.ObservationRef]) -> None:
    page_only = reconcile.reconcile_cell(
        ("codex", "gpt-6.2-astra", "medium"),
        [ref for ref in refs if ref.reading.source_id == fetch.SOURCE_EMBEDDED],
        prior=reconcile.PriorValue(57.0, 1.618),
    )
    _check(
        page_only is not None and page_only.acceptance == CELL_ACCEPTANCE_ACCEPTED and page_only.capability_score == 56.9986252138657 and "single source" in str(page_only.disagreement_note),
        "embedded-only exact effort renews within prior tolerance with provenance",
    )


def _check_api_only(run: fetch.DiscoveryRun) -> None:
    low_readings = [item for item in run.readings if item.model == "gpt-6.2-astra" and item.effort == "low" and item.fetch_status == "ok"]
    api_only = reconcile.reconcile_cell(
        ("codex", "gpt-6.2-astra", "low"),
        [reconcile.ObservationRef(f"low{i}", item) for i, item in enumerate(low_readings)],
    )
    _check(
        api_only is not None and api_only.acceptance == CELL_ACCEPTANCE_ACCEPTED and api_only.capability_score == 45.7 and "single source" in str(api_only.disagreement_note),
        "API-only low effort renews without inventing a page record",
    )


def _check_reconcile_conflict(refs: list[reconcile.ObservationRef]) -> None:
    conflict = [reconcile.ObservationRef(ref.observation_id, replace(ref.reading, value_number=40.0)) if ref.reading.source_id == fetch.SOURCE_EMBEDDED and ref.reading.metric == "intelligence_index" else ref for ref in refs]
    disputed = reconcile.reconcile_cell(("codex", "gpt-6.2-astra", "medium"), conflict)
    _check(
        disputed is not None and disputed.acceptance == "crosscheck_conflict" and disputed.capability_score is None,
        "API/page disagreement preserves the prior cell",
    )


def _check_store_conflict(run: fetch.DiscoveryRun) -> None:
    conflicted_readings = tuple(replace(item, value_number=40.0) if item.model == "gpt-6.2-astra" and item.effort == "medium" and item.source_id == fetch.SOURCE_EMBEDDED and item.metric == "intelligence_index" else item for item in run.readings)
    conflict_run = replace(run, readings=conflicted_readings)
    conflict_state = RealShapeState()
    conflict_stamp = (_NOW - timedelta(days=2)).isoformat()
    _preload(conflict_state, "gpt-6.2-astra", "medium", score=57.0, measured_at=conflict_stamp)
    conflict_report = _refresh(conflict_state, conflict_run)
    held = _cell(conflict_state, "gpt-6.2-astra", "medium")
    _check(
        (held["capability_score"], held["cost_per_task_usd"], held["measured_at"], held["acceptance"]) == (57.0, 1.0, conflict_stamp, CELL_ACCEPTANCE_ACCEPTED),
        "store keeps the last accepted non-manual value on API/page conflict",
    )
    _check(any("disagreement" in note and "gpt-6.2-astra" in note for note in conflict_report.result_fields["advisories"]), "store reports a held API/page conflict in advisories")
    _check(
        "fixture-key" not in json.dumps([run.failures, run.advisories, [item.raw_excerpt for item in run.readings]]),
        "API credential does not enter run output or observation excerpts",
    )


def _check_absence(run: fetch.DiscoveryRun) -> None:
    missing = replace(
        run,
        readings=tuple(item for item in run.readings if not (item.model == "gpt-6.2-astra" and item.effort == "low")),
    )
    state = RealShapeState()
    old_stamp = (_NOW - timedelta(days=5)).isoformat()
    _preload(state, "gpt-6.2-astra", "low", score=40.0, measured_at=old_stamp)
    missing_report = _refresh(state, missing)
    old_cell = _cell(state, "gpt-6.2-astra", "low")
    _check(
        (old_cell["capability_score"], old_cell["cost_per_task_usd"], old_cell["measured_at"]) == (40.0, 1.0, old_stamp),
        "missing effort leaves prior score, cost and measured_at intact",
    )
    _check(
        any("UNRENEWABLE" in note and "gpt-6.2-astra" in note and "low" in note for note in missing_report.result_fields["advisories"]),
        "missing effort produces a loud historical-cell advisory",
    )


def _check_changed_shape(api_payload: dict[str, Any]) -> None:
    changed = {
        **api_payload,
        "data": [{**record, "evaluations": {}} if record["slug"] == "gpt-6-2-astra-medium" else record for record in api_payload["data"]],
    }
    parsed, notes = fetch.parse_api_variants(changed)
    _check(
        ("openai", "gpt-6-2-astra", "medium") not in {item.key for item in parsed} and any("no valid score" in note for note in notes),
        "changed API metric shape leaves the page as a named fallback",
    )


def test_measured_api_and_embedded_efforts() -> None:
    page, api_payload, page_variants, api_variants = _check_measured_fixture()
    _check_api_readings(api_payload, api_variants)
    _check_page_readings(page, page_variants)
    _check_precision(page_variants, api_variants)
    run = _check_joined_efforts(page, api_payload)
    refs = _check_source_reconciliation(run)
    _check_page_only(refs)
    _check_api_only(run)
    _check_reconcile_conflict(refs)
    _check_store_conflict(run)
    _check_absence(run)
    _check_changed_shape(api_payload)


def _conflicting_page_handler(page: str, altered: str, rows: list[tuple[str, ...]]) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/leaderboards/models":
            return httpx.Response(200, text=_page(rows))
        if request.url.path == "/models/gpt-6-2-astra":
            return httpx.Response(200, text=page)
        if request.url.path == "/models/gpt-6-2-sol":
            return httpx.Response(200, text=altered)
        return httpx.Response(404)

    return handler


def _conflict_cells(variants: list[fetch.EffortVariant]) -> list[fetch.DiscoveredCell]:
    astra = next(v for v in variants if v.release_slug == "gpt-6-2-astra" and v.effort == "medium")
    sol = next(v for v in variants if v.release_slug == "gpt-6-2-sol" and v.effort == "medium")
    return [fetch.DiscoveredCell("codex", v.creator, v.release_slug.replace("-6-2-", "-6.2-"), v.effort, v.as_row(), None) for v in (astra, sol)]


def test_conflicting_pages_do_not_renew_prior() -> None:
    page, _, variants, _ = _check_measured_fixture()
    altered = page.replace("56.9986252138657", "50.9986252138657")
    assert altered != page
    rows = [*_LEADERBOARD_ROWS[:40], ("GPT-6.2 Astra (max)", "1M", "OpenAI", "62", "$4.00", "gpt-6-2-astra"), ("GPT-6.2 Astra (medium)", "1M", "OpenAI", "-", "-", "gpt-6-2-astra"), ("GPT-6.2 Sol (max)", "1M", "OpenAI", "57", "$1.01", "gpt-6-2-sol")]

    handler = _conflicting_page_handler(page, altered, rows)

    codex = (*_CODEX_LIST, ("gpt-6.2-astra", "list", _EFFORTS), ("gpt-6.2-sol", "list", _EFFORTS))
    with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client:
        run = fetch.discover_readings(client, _context(Path(tmp), codex=codex), catalog_models={}, api_key=None)
        cells = _conflict_cells(variants)
        pages, parsed, conflicted, notes = fetch._read_detail_pages(client, cells, [])
        readings = fetch._gather_readings(cells, fetched_at=_NOW.isoformat(), pages=pages, api_variants={}, page_variants=parsed, conflicted=conflicted)
    assert any("conflicting embedded" in note for note in notes)
    run = replace(run, cells=(*run.cells, *cells), readings=(*run.readings, *readings))
    state = RealShapeState()
    old_stamp = (_NOW - timedelta(days=2)).isoformat()
    _preload(state, "gpt-6.2-astra", "medium", score=57.0, cost=1.6177, measured_at=old_stamp)
    _refresh(state, run)
    assert _cell(state, "gpt-6.2-astra", "medium")["measured_at"] == old_stamp


def test_overflow_record_does_not_abort_refresh() -> None:
    page, payload, _, _ = _check_measured_fixture()
    record = next(item for item in payload["data"] if item["slug"] == "gpt-6-2-astra-medium")
    record["evaluations"]["artificial_analysis_intelligence_index"] = 10**309
    run = _check_joined_efforts(page, payload, api_astra=False)
    assert any("no valid score/cost pair" in note for note in run.advisories)
    state = RealShapeState()
    old_stamp = (_NOW - timedelta(days=2)).isoformat()
    _preload(state, "gpt-6.2-astra", "medium", score=57.0, cost=1.6177, measured_at=old_stamp)
    _preload(state, "gpt-6.2-sol", "medium", score=52.7, cost=0.406, measured_at=old_stamp)
    _refresh(state, run)
    assert _cell(state, "gpt-6.2-astra", "medium")["measured_at"] != old_stamp
    assert _cell(state, "gpt-6.2-sol", "medium")["measured_at"] != old_stamp


def main() -> int:
    print("model_capability_effort_sources_smoke")
    test_measured_api_and_embedded_efforts()
    test_conflicting_pages_do_not_renew_prior()
    test_overflow_record_does_not_abort_refresh()
    print(f"{base._passed} passed, {len(base._failed)} failed")
    return 1 if base._failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
