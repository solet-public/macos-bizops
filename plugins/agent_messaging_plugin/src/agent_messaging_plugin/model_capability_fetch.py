"""Live fetch and discovery for the model capability catalog (iss_d136ae29; iss_0e805222, iss_52fdb8e5; rul_473f772f).

The catalog learns which models exist from the providers' own lists (the
harness lists read by `model_capability_harness`), not from a table of ours.
This module takes score and cost from Artificial Analysis and joins them to
those lists:

* the cross-model leaderboard (`/leaderboards/models`): one server-rendered
  `<table>` whose every row carries its own effort-suffixed display name, its
  creator, an Intelligence Index score, a cost per task and a link to the
  model's own detail page. A plain HTTP GET plus a deterministic per-`<tr>`
  parse sees what a browser would; no headless browser is needed.
* each joined model's own detail page (`/models/<slug>`, the slug taken from
  the row's own link): a second, separately rendered artifact whose agreement
  is the corroboration the reconciler in `model_capability_reconcile.py`
  asks for.

Both pages come from one publisher, so they are not evidence of a different
organization's measurement; they can diverge (a leaderboard refresh that has
not reached a detail page, a transient parse fault on one), which is what the
cross-check protects against.

What the harness decides, and what the leaderboard decides:

* existence, the exact launch id and the offered efforts: the harness list.
  A leaderboard row whose creator has no runtime, or whose model no harness
  list offers, is listed and counted but never becomes a cell.
* score and cost: the leaderboard row, corroborated by its detail page.
* provider and runtime: the adapter table in `model_capability_harness`.
* an unreadable or empty harness list is the documented degraded mode: rows
  are joined to the launch ids the catalog already holds, and an unmatched row
  becomes a flagged cell only when the creator's id-shape rule accepts its
  derived launch id.

No model is named as a rule here. A new model needs no code change; the one
reviewed alias row lives in `model_capability_harness.JOIN_ALIASES`.

Fetch is separated from parse everywhere so tests exercise the parser against
canned HTML fixtures with zero network access; `fetch_*_html` and
`discover_readings`' detail fetches are the only code here that touches the
network.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import httpx

from .dispatch_tier_selection import EFFORT_ORDER
from .model_capability_harness import (
    NON_REASONING_EFFORT,
    RUNTIME_ADAPTERS,
    STATUS_DEGRADED,
    STATUS_OK,
    HarnessContext,
    HarnessList,
    HarnessModel,
    RuntimeAdapter,
    join_models,
)

SOURCE_LEADERBOARD_HTML: Final[str] = "artificial_analysis_leaderboard_html"
SOURCE_MODEL_DETAIL_HTML: Final[str] = "artificial_analysis_model_detail_html"

LEADERBOARD_PAGE_FLOOR: Final[int] = 40
"""Rows a leaderboard page must parse to count as a source (design G5). A
thinner page is a failed source: nothing is created or renewed from it."""

_LEADERBOARD_URL: Final[str] = "https://artificialanalysis.ai/leaderboards/models"
_MODEL_DETAIL_URL_TEMPLATE: Final[str] = "https://artificialanalysis.ai/models/{slug}"
_USER_AGENT: Final[str] = "Mozilla/5.0 (compatible; model-capability-refresh/1.0)"
_FETCH_TIMEOUT_SECONDS: Final[float] = 20.0
_SCORE_METRIC: Final[str] = "intelligence_index"
_COST_METRIC: Final[str] = "cost_per_task_usd"
_CATALOG_VISIBILITY: Final[str] = "catalog"
_UNMAPPED_CREATORS_REPORTED: Final[int] = 10
_NOT_LAUNCHABLE_REPORTED: Final[int] = 20


class FetchError(Exception):
    """A fetch failed. `status` matches the observation table's `fetch_status` enum."""

    def __init__(self, status: str, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Reading:
    """One (provider, runtime, model, effort) x metric observation, not yet written."""

    source_id: str
    provider: str
    runtime: str
    model: str
    effort: str
    metric: str
    value_number: float | None
    value_text: str | None
    raw_excerpt: str
    fetch_status: str
    fetched_at: str


@dataclass(frozen=True, slots=True)
class LeaderboardRow:
    """One parsed leaderboard row. `effort` is the lowercase suffix token mapped to
    the catalog spelling (`non-reasoning` -> `non_reasoning`), or None for a bare name
    or a name whose parenthesis is not an effort (for example `(June 2026)`)."""

    display_name: str
    base_name: str
    effort: str | None
    creator: str
    score_text: str
    cost_text: str
    detail_slug: str | None
    excerpt: str


@dataclass(frozen=True, slots=True)
class DiscoveredCell:
    """A (runtime, launch id, effort) the harness offers and the leaderboard scores.

    `creation_note` is the flag a newly created cell carries when the harness
    list could not fully vouch for it (a degraded list, or a launch id derived
    from the leaderboard name); the store applies it only when it creates the cell.
    `derived_launch_id` is True when the runtime's harness list was unusable, so the
    launch id was not confirmed by any harness list: creating such a cell needs two
    agreeing sources (design 1.7) and starts its harness-absence clock."""

    runtime: str
    provider: str
    model: str
    effort: str
    row: LeaderboardRow
    creation_note: str | None
    derived_launch_id: bool = False

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.runtime, self.model, self.effort)


@dataclass(frozen=True, slots=True)
class DiscoveryRun:
    """Everything one discovery pass gathered, before any state is written."""

    readings: tuple[Reading, ...]
    failures: tuple[tuple[str, str], ...]
    sources_ok: tuple[str, ...]
    cells: tuple[DiscoveredCell, ...]
    leaderboard_ok: bool
    leaderboard_rows: int
    harness: Mapping[str, Mapping[str, object]]
    harness_model_ids: Mapping[str, tuple[str, ...]]
    alias_candidates: tuple[Mapping[str, str], ...]
    unmapped_creators: tuple[tuple[str, int], ...]
    efforts_outside_vocabulary: Mapping[str, tuple[str, ...]]
    listed_not_launchable: tuple[str, ...]
    creator_label_unmatched: tuple[str, ...]
    advisories: tuple[str, ...]


# --------------------------------------------------------------------------- the leaderboard

# One leaderboard row's flattened-text shape: "{Display Name}|{Context Window}|{Creator}|{Intelligence
# Index}|${Cost}|..." terminated by the row's own "Model|Providers" action cell. Verified against the
# live page: all 126 rows of 2026-10-10 match, one per `<tr>`.
_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z0-9][^|]*?)\|(?:\d+[kKmM]|--)\|([A-Za-z][^|]*?)\|(\d+|--)\|(\$[\d.]+|--|\*)\|"
    r"[^|]*\|[^|]*\|[^|]*\|Model\|Providers\|",
)
_TABLE_ROW: Final[re.Pattern[str]] = re.compile(r"<tr\b.*?</tr>", re.DOTALL | re.IGNORECASE)
_MODEL_LINK: Final[re.Pattern[str]] = re.compile(r'href="/models/([^"/?#]+)"[^>]*>\s*Model\b')
_EFFORT_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\s*\(([a-z]+(?:-[a-z]+)?)(?: with fallback)?\)\s*$")
_SCRIPT_OR_STYLE: Final[re.Pattern[str]] = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _strip_tags(html: str) -> str:
    """Tag-strip to a `|`-delimited flat text. Bundled `<script>`/`<style>`
    content is dropped bodily first -- these pages ship megabytes of JS
    around a few KB of real table data, and running the row/field regexes
    over that JS text (rather than just past its tags) is slow enough to
    look like a hang."""
    without_scripts = _SCRIPT_OR_STYLE.sub("", html)
    text = re.sub(r"<[^>]+>", "|", without_scripts)
    return re.sub(r"\|+", "|", text)


def _table_region(html: str) -> str:
    """The first `<table>...</table>` block only. The leaderboard page ships
    several megabytes of unrelated bundled JS in `<script>` tags around the
    one real data table; scoping to the table first keeps the parse to the
    ~1MB of actual row content and is linear."""
    start = html.find("<table")
    if start == -1:
        return ""
    end = html.find("</table>", start)
    return html[start : end + len("</table>")] if end != -1 else html[start:]


def split_effort(display_name: str) -> tuple[str, str | None]:
    """(base name, effort token) of a display name. A suffix that is not a lowercase
    word (`(June 2026)` has digits) is not an effort: the whole string is the base
    name and fails to join, which is the correct outcome for such a row."""
    match = _EFFORT_SUFFIX.search(display_name)
    if match is None:
        return display_name.strip(), None
    return display_name[: match.start()].strip(), match.group(1).replace("-", "_")


def parse_leaderboard_rows(html: str) -> list[LeaderboardRow]:
    """Every row the page's table lists, in page order, each with its own detail link."""
    rows: list[LeaderboardRow] = []
    for tr in _TABLE_ROW.finditer(_table_region(html)):
        segment = tr.group(0)
        match = _ROW_PATTERN.search(_strip_tags(segment))
        if match is None:
            continue
        display_name, creator, score_text, cost_text = match.groups()
        base_name, effort = split_effort(display_name.strip())
        link = _MODEL_LINK.search(segment)
        rows.append(
            LeaderboardRow(
                display_name=display_name.strip(), base_name=base_name, effort=effort, creator=creator.strip().lower(),
                score_text=score_text, cost_text=cost_text, detail_slug=link.group(1) if link else None,
                excerpt=match.group(0)[:512],
            ),
        )
    return rows


# --------------------------------------------------------------------------- readings


def _in_range(metric: str, value: float) -> bool:
    """Score is on the 0-100 Intelligence Index scale; cost is finite and not negative (design G6)."""
    if not math.isfinite(value):
        return False
    return 0.0 <= value <= 100.0 if metric == _SCORE_METRIC else value >= 0.0


def _reading(cell: DiscoveredCell, *, source_id: str, metric: str, status: str, fetched_at: str, excerpt: str, number: float | None = None, text: str | None = None) -> Reading:
    return Reading(
        source_id=source_id, provider=cell.provider, runtime=cell.runtime, model=cell.model, effort=cell.effort,
        metric=metric, value_number=number, value_text=text, raw_excerpt=excerpt[:512], fetch_status=status, fetched_at=fetched_at,
    )


def _metric_reading(cell: DiscoveredCell, *, source_id: str, metric: str, raw: str, excerpt: str, fetched_at: str) -> Reading:
    """One checked reading: a value that is not a number, or is out of range, is a
    `parse_error` reading that names why; it never becomes a value (design G6)."""
    try:
        value = float(raw.lstrip("$"))
    except ValueError:
        return _reading(cell, source_id=source_id, metric=metric, status="parse_error", fetched_at=fetched_at, excerpt=excerpt, text=f"not a number: {raw!r}")
    if not _in_range(metric, value):
        return _reading(cell, source_id=source_id, metric=metric, status="parse_error", fetched_at=fetched_at, excerpt=excerpt, text=f"out of range: {raw!r}")
    return _reading(cell, source_id=source_id, metric=metric, status="ok", fetched_at=fetched_at, excerpt=excerpt, number=value)


def leaderboard_readings(cell: DiscoveredCell, *, fetched_at: str) -> list[Reading]:
    """The cell's own leaderboard row as readings: its score, and its cost where priced."""
    row = cell.row
    readings: list[Reading] = []
    if row.score_text != "--":
        readings.append(_metric_reading(cell, source_id=SOURCE_LEADERBOARD_HTML, metric=_SCORE_METRIC, raw=row.score_text, excerpt=row.excerpt, fetched_at=fetched_at))
    if row.cost_text not in ("--", "*"):
        readings.append(_metric_reading(cell, source_id=SOURCE_LEADERBOARD_HTML, metric=_COST_METRIC, raw=row.cost_text, excerpt=row.excerpt, fetched_at=fetched_at))
    return readings


_DETAIL_SCORE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Intelligence\|(?:Updated\|)?#\|\d+\| / \|\d+\|(\d+)\|Artificial Analysis Intellig",
)
_DETAIL_COST_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\$[\d.]+)\|Cost per Intelligence Index [Tt]ask")


def parse_model_detail_html(html: str, *, cell: DiscoveredCell, fetched_at: str) -> list[Reading]:
    """One cell's own detail page: same two metrics, independently rendered.
    Missing/unparseable fields come back as `parse_error` readings rather than
    being silently dropped, so a reconciler run can see the difference between
    "this source disagreed" and "this source's page changed shape"."""
    text = _strip_tags(html)
    excerpt = text[:512]
    score_match = _DETAIL_SCORE_PATTERN.search(text)
    if score_match:
        readings = [_metric_reading(cell, source_id=SOURCE_MODEL_DETAIL_HTML, metric=_SCORE_METRIC, raw=score_match.group(1), excerpt=excerpt, fetched_at=fetched_at)]
    else:
        readings = [_reading(cell, source_id=SOURCE_MODEL_DETAIL_HTML, metric=_SCORE_METRIC, status="parse_error", fetched_at=fetched_at, excerpt=excerpt, text="no Intelligence Index on the detail page")]
    cost_match = _DETAIL_COST_PATTERN.search(text)
    if cost_match:
        readings.append(_metric_reading(cell, source_id=SOURCE_MODEL_DETAIL_HTML, metric=_COST_METRIC, raw=cost_match.group(1), excerpt=excerpt, fetched_at=fetched_at))
    return readings


def fetch_leaderboard_html(client: httpx.Client) -> str:
    try:
        response = client.get(_LEADERBOARD_URL, headers={"User-Agent": _USER_AGENT}, timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FetchError("http_error", f"{_LEADERBOARD_URL}: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise FetchError("http_error", f"{_LEADERBOARD_URL}: {exc}") from exc
    return response.text


def fetch_model_detail_html(client: httpx.Client, slug: str) -> str:
    url = _MODEL_DETAIL_URL_TEMPLATE.format(slug=slug)
    try:
        response = client.get(url, headers={"User-Agent": _USER_AGENT}, timeout=_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise FetchError("http_error", f"{url}: HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise FetchError("http_error", f"{url}: {exc}") from exc
    return response.text


def _detail_readings(client: httpx.Client, cell: DiscoveredCell, *, fetched_at: str, failures: list[tuple[str, str]]) -> tuple[list[Reading], bool]:
    """The cell's detail-page readings, and whether the page was fetched at all.

    A page that cannot be had is still a reading, with the reason in `value_text`
    (`http_error`, or `not_listed` for a row with no detail link): the reconciler
    reads that reason into the single-source note instead of leaving the cell pending."""
    label = f"{SOURCE_MODEL_DETAIL_HTML}:{cell.model}@{cell.effort}"
    if cell.row.detail_slug is None:
        reason = "the leaderboard row has no detail link"
        return [_reading(cell, source_id=SOURCE_MODEL_DETAIL_HTML, metric=_SCORE_METRIC, status="not_listed", fetched_at=fetched_at, excerpt=cell.row.excerpt, text=reason)], False
    try:
        html = fetch_model_detail_html(client, cell.row.detail_slug)
    except FetchError as exc:
        failures.append((label, f"{exc.status}: {exc.message}"))
        return [_reading(cell, source_id=SOURCE_MODEL_DETAIL_HTML, metric=_SCORE_METRIC, status=exc.status, fetched_at=fetched_at, excerpt=exc.message, text=exc.message[:512])], False
    return parse_model_detail_html(html, cell=cell, fetched_at=fetched_at), True


# --------------------------------------------------------------------------- discovery


@dataclass(slots=True)
class _RuntimeTally:
    cells: list[DiscoveredCell] = field(default_factory=lambda: list[DiscoveredCell]())
    alias_candidates: list[Mapping[str, str]] = field(default_factory=lambda: list[Mapping[str, str]]())
    not_launchable: list[str] = field(default_factory=lambda: list[str]())
    advisories: list[str] = field(default_factory=lambda: list[str]())
    outside_vocabulary: dict[str, set[str]] = field(default_factory=lambda: dict[str, set[str]]())


def _usable(harness_list: HarnessList) -> bool:
    return harness_list.status in (STATUS_OK, STATUS_DEGRADED) and bool(harness_list.offered_models)


def _row_effort(row: LeaderboardRow, model: HarnessModel | None) -> str | None:
    """The effort a row stands for on a model. A bare name is `non_reasoning` only
    where the model has no effort axis (or the harness could not say); otherwise it is unparsed."""
    if row.effort is not None:
        return row.effort
    if model is None or model.visibility == _CATALOG_VISIBILITY:
        return NON_REASONING_EFFORT
    return NON_REASONING_EFFORT if model.efforts == (NON_REASONING_EFFORT,) else None


def _launchable(model: HarnessModel | None, effort: str) -> bool:
    if model is None or model.visibility == _CATALOG_VISIBILITY:
        return True  # the harness could not list efforts; the derived-mode flag says so
    return effort in model.efforts


def _creation_note(adapter: RuntimeAdapter, harness_list: HarnessList, *, derived: bool) -> str | None:
    if derived:
        return f"launch id derived from the leaderboard name; {adapter.runtime} harness list {harness_list.status}: {harness_list.detail}"
    if harness_list.status == STATUS_DEGRADED:
        return f"created from a degraded {adapter.runtime} harness list: {harness_list.detail}"
    return None


def _add_cell(tally: _RuntimeTally, adapter: RuntimeAdapter, row: LeaderboardRow, model_id: str, effort: str, note: str | None, *, derived: bool) -> None:
    if effort not in EFFORT_ORDER:  # design G2: an effort outside the ladder is reported, never a cell
        tally.outside_vocabulary.setdefault(model_id, set()).add(effort)
        return
    cell = DiscoveredCell(adapter.runtime, adapter.provider, model_id, effort, row, note, derived)
    if any(existing.key == cell.key for existing in tally.cells):
        tally.advisories.append(f"two leaderboard rows name {adapter.runtime}/{model_id}@{effort}; the first was used ({row.display_name!r} was ignored)")
        return
    tally.cells.append(cell)


def _label_cells(tally: _RuntimeTally, adapter: RuntimeAdapter, harness_list: HarnessList, label: str, label_rows: Sequence[LeaderboardRow], models: Sequence[HarnessModel]) -> None:
    by_id = {model.model_id: model for model in models}
    names = [row.base_name for row in label_rows]
    joined = join_models(label, names, models)
    tally.alias_candidates.extend({"runtime": adapter.runtime, "name": item.name, "model_id": item.model_id} for item in joined.alias_candidates)
    tally.advisories.extend(f"join ambiguous for key {item.key!r}: names {list(item.names)} and models {list(item.model_ids)}; none was joined" for item in joined.ambiguous)
    derived_mode = not _usable(harness_list)
    for row in label_rows:
        model_id = joined.joined.get(row.base_name)
        if model_id is None:
            _row_without_model(tally, adapter, harness_list, row, derived_mode=derived_mode)
            continue
        model = by_id.get(model_id)
        effort = _row_effort(row, model)
        if effort is None or not _launchable(model, effort):
            tally.not_launchable.append(f"{adapter.runtime}/{model_id}: {row.display_name}")
            continue
        _add_cell(tally, adapter, row, model_id, effort, _creation_note(adapter, harness_list, derived=derived_mode), derived=derived_mode)


def _row_without_model(tally: _RuntimeTally, adapter: RuntimeAdapter, harness_list: HarnessList, row: LeaderboardRow, *, derived_mode: bool) -> None:
    """A row no model was joined to: listed but not launchable, or (degraded mode only) a flagged derived cell."""
    if not derived_mode:
        tally.not_launchable.append(f"{adapter.runtime}: {row.display_name} (no harness model)")
        return
    model_id = adapter.derive_id(row.base_name)
    if not adapter.id_shape.match(model_id):
        tally.advisories.append(f"{adapter.runtime} harness list {harness_list.status}: the derived launch id {model_id!r} for {row.display_name!r} is not well formed, so no cell was created")
        return
    effort = _row_effort(row, None)
    if effort is not None:
        _add_cell(tally, adapter, row, model_id, effort, _creation_note(adapter, harness_list, derived=True), derived=True)


def _note_outside_vocabulary(tally: _RuntimeTally, models: Sequence[HarnessModel]) -> None:
    """Harness efforts outside the ladder are reported per model (design 2.4); they never become cells."""
    for model in models:
        outside = [effort for effort in model.efforts if effort not in EFFORT_ORDER]
        if outside:
            tally.outside_vocabulary.setdefault(model.model_id, set()).update(outside)


def _runtime_cells(adapter: RuntimeAdapter, harness_list: HarnessList, rows: Sequence[LeaderboardRow], catalog_ids: Sequence[str]) -> _RuntimeTally:
    tally = _RuntimeTally()
    if _usable(harness_list):
        models: Sequence[HarnessModel] = harness_list.offered_models
        _note_outside_vocabulary(tally, models)
    else:
        models = tuple(HarnessModel(model_id, (), True, _CATALOG_VISIBILITY) for model_id in catalog_ids)
        tally.advisories.append(f"{adapter.runtime} harness list is {harness_list.status} ({harness_list.detail}); leaderboard-only creation applies, and every cell it creates is flagged unverified")
    for label in sorted(adapter.creator_labels):
        label_rows = [row for row in rows if row.creator == label]
        if label_rows:
            _label_cells(tally, adapter, harness_list, label, label_rows, models)
    return tally


def _harness_summary(harness_list: HarnessList) -> dict[str, object]:
    return {
        "status": harness_list.status, "source": harness_list.source, "detail": harness_list.detail,
        "client_version": harness_list.client_version, "fetched_at": harness_list.fetched_at,
        "models_offered": len(harness_list.offered_models), "advisories": list(harness_list.advisories),
    }


def _read_page(client: httpx.Client, failures: list[tuple[str, str]]) -> list[LeaderboardRow]:
    try:
        rows = parse_leaderboard_rows(fetch_leaderboard_html(client))
    except FetchError as exc:
        failures.append((SOURCE_LEADERBOARD_HTML, f"{exc.status}: {exc.message}"))
        return []
    if len(rows) < LEADERBOARD_PAGE_FLOOR:
        failures.append((SOURCE_LEADERBOARD_HTML, f"thin_page: {len(rows)} rows parsed, at least {LEADERBOARD_PAGE_FLOOR} are required; nothing was created or renewed from it"))
        return []
    return rows


def _gather_readings(client: httpx.Client, cells: Sequence[DiscoveredCell], *, fetched_at: str, failures: list[tuple[str, str]]) -> tuple[list[Reading], bool]:
    """Every cell's leaderboard and detail readings, and whether any detail page was fetched."""
    readings: list[Reading] = []
    detail_fetched = False
    for cell in cells:
        readings.extend(leaderboard_readings(cell, fetched_at=fetched_at))
        detail, fetched = _detail_readings(client, cell, fetched_at=fetched_at, failures=failures)
        readings.extend(detail)
        detail_fetched = detail_fetched or fetched
    return readings, detail_fetched


def _unmapped_creators(rows: Sequence[LeaderboardRow]) -> tuple[tuple[str, int], ...]:
    mapped = {label for adapter in RUNTIME_ADAPTERS.values() for label in adapter.creator_labels}
    return tuple(Counter(row.creator for row in rows if row.creator not in mapped).most_common(_UNMAPPED_CREATORS_REPORTED))


def _unmatched_labels(rows: Sequence[LeaderboardRow]) -> tuple[str, ...]:
    """Runtimes whose creator label matched no row of a page that otherwise parsed (design 2.3)."""
    return tuple(runtime for runtime, adapter in RUNTIME_ADAPTERS.items() if rows and not any(row.creator in adapter.creator_labels for row in rows))


def _merged_outside_vocabulary(tallies: Mapping[str, _RuntimeTally]) -> dict[str, tuple[str, ...]]:
    return {model: tuple(sorted(efforts)) for tally in tallies.values() for model, efforts in sorted(tally.outside_vocabulary.items())}


def _advisories(tallies: Mapping[str, _RuntimeTally], harness_lists: Mapping[str, HarnessList]) -> tuple[str, ...]:
    notes = [note for tally in tallies.values() for note in tally.advisories]
    notes.extend(f"{runtime} harness: {note}" for runtime, harness_list in harness_lists.items() for note in harness_list.advisories)
    return tuple(notes)


def _read_harnesses(context: HarnessContext) -> dict[str, HarnessList]:
    return {runtime: adapter.read_harness(context) for runtime, adapter in RUNTIME_ADAPTERS.items()}


def _runtime_tallies(harness_lists: Mapping[str, HarnessList], rows: Sequence[LeaderboardRow], catalog_models: Mapping[str, Sequence[str]]) -> dict[str, _RuntimeTally]:
    return {runtime: _runtime_cells(adapter, harness_lists[runtime], rows, catalog_models.get(runtime, ())) for runtime, adapter in RUNTIME_ADAPTERS.items()}


def _listed_ids(harness_lists: Mapping[str, HarnessList]) -> dict[str, tuple[str, ...]]:
    """Every launch id a usable harness list names, hidden ones included: the 'harness-listed' set."""
    return {runtime: tuple(model.model_id for model in harness_list.models) if _usable(harness_list) else () for runtime, harness_list in harness_lists.items()}


def discover_readings(client: httpx.Client, context: HarnessContext, *, catalog_models: Mapping[str, Sequence[str]]) -> DiscoveryRun:
    """Read the harness lists and the leaderboard, join them, and gather every reading.

    `catalog_models` is runtime -> the launch ids the catalog already holds; it is
    what rows are joined to when a runtime's harness list cannot be read (design 1.7).
    A per-model detail failure never aborts the run: one bad page must not blank
    out every other cell's otherwise-good leaderboard reading."""
    fetched_at = context.now.isoformat()
    failures: list[tuple[str, str]] = []
    rows = _read_page(client, failures)
    harness_lists = _read_harnesses(context)
    tallies = _runtime_tallies(harness_lists, rows, catalog_models)
    cells = sorted((cell for tally in tallies.values() for cell in tally.cells), key=lambda cell: cell.key)
    readings, detail_fetched = _gather_readings(client, cells, fetched_at=fetched_at, failures=failures)
    sources = ((SOURCE_LEADERBOARD_HTML, bool(rows)), (SOURCE_MODEL_DETAIL_HTML, detail_fetched))
    return DiscoveryRun(
        readings=tuple(readings), failures=tuple(failures), sources_ok=tuple(source for source, present in sources if present),
        cells=tuple(cells), leaderboard_ok=bool(rows), leaderboard_rows=len(rows),
        harness={runtime: _harness_summary(harness_list) for runtime, harness_list in harness_lists.items()}, harness_model_ids=_listed_ids(harness_lists),
        alias_candidates=tuple(candidate for tally in tallies.values() for candidate in tally.alias_candidates),
        unmapped_creators=_unmapped_creators(rows), efforts_outside_vocabulary=_merged_outside_vocabulary(tallies),
        listed_not_launchable=tuple(name for tally in tallies.values() for name in tally.not_launchable)[:_NOT_LAUNCHABLE_REPORTED],
        creator_label_unmatched=_unmatched_labels(rows), advisories=_advisories(tallies, harness_lists),
    )


__all__ = [
    "LEADERBOARD_PAGE_FLOOR",
    "SOURCE_LEADERBOARD_HTML",
    "SOURCE_MODEL_DETAIL_HTML",
    "DiscoveredCell",
    "DiscoveryRun",
    "FetchError",
    "LeaderboardRow",
    "Reading",
    "discover_readings",
    "fetch_leaderboard_html",
    "fetch_model_detail_html",
    "leaderboard_readings",
    "parse_leaderboard_rows",
    "parse_model_detail_html",
    "split_effort",
]
