"""Publisher source parsing and fetch for the model capability catalog (iss_d136ae29; iss_0e805222, iss_52fdb8e5; rul_473f772f).

The catalog learns which models exist from the providers' own lists (the
harness lists read by `model_capability_harness`), not from a table of ours.
This module takes score and cost from Artificial Analysis and joins them to
those lists. The documented Data API is the first source; its free-list pages
carry one scored variant per slug and effort. Model pages carry exact embedded
per-effort records for a cross-check and for a named fallback when the API is
unavailable. The leaderboard remains a legacy fallback for older layouts:

* the cross-model leaderboard (`/leaderboards/models`): one server-rendered
  `<table>` whose every row carries its own display name, any effort label, its
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

A detail URL can also serve the model family's page (every effort's URL does, since
2026-11-14T05:30Z), which shows the family's top-effort values: that is no reading of
the cell's own effort, so `parse_model_detail_html` returns it as `not_listed` and the
leaderboard row is accepted alone, as when the page is missing.

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

import json
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, cast

import httpx

from .dispatch_tier_selection import EFFORT_ORDER

SOURCE_LEADERBOARD_HTML: Final[str] = "artificial_analysis_leaderboard_html"
SOURCE_MODEL_DETAIL_HTML: Final[str] = "artificial_analysis_model_detail_html"
SOURCE_API: Final[str] = "artificial_analysis_api"
SOURCE_EMBEDDED: Final[str] = "artificial_analysis_embedded_model_record"

LEADERBOARD_PAGE_FLOOR: Final[int] = 40
"""Rows a leaderboard page must parse to count as a source (design G5). A
thinner page is a failed source: nothing is created or renewed from it."""

_LEADERBOARD_URL: Final[str] = "https://artificialanalysis.ai/leaderboards/models"
_MODEL_DETAIL_URL_TEMPLATE: Final[str] = "https://artificialanalysis.ai/models/{slug}"
_API_URL: Final[str] = "https://artificialanalysis.ai/api/v2/language/models/free"
_MAX_API_PAGES: Final[int] = 10
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
    """One parsed leaderboard row with an explicit effort from its label or name
    suffix, normalized to catalog spelling when present."""

    display_name: str
    base_name: str
    effort: str | None
    creator: str
    score_text: str
    cost_text: str
    detail_slug: str | None
    excerpt: str
    source_id: str = SOURCE_LEADERBOARD_HTML


@dataclass(frozen=True, slots=True)
class EffortVariant:
    """A scored publisher record with explicit, checked release and effort identity."""

    source_id: str
    release_slug: str
    base_name: str
    creator: str
    effort: str
    score: float
    cost: float
    excerpt: str

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.creator, self.release_slug, self.effort)

    def as_row(self) -> LeaderboardRow:
        return LeaderboardRow(
            display_name=f"{self.base_name} ({self.effort})",
            base_name=self.base_name,
            effort=self.effort,
            creator=self.creator,
            score_text=str(self.score),
            cost_text=str(self.cost),
            detail_slug=self.release_slug,
            excerpt=self.excerpt,
            source_id=self.source_id,
        )


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
# live page: all 126 rows of 2026-10-10 match, one per `<tr>`. The number of value columns
# between the cost and the action cell is not fixed (2026-11-12: the page dropped one column and
# 0 of 286 rows matched while the action-cell anchor stayed), so the count is open; the anchor
# is what the pattern pins.
_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"([A-Za-z0-9][^|]*?)\|(?:\d+[kKmM]|--)\|([A-Za-z][^|]*?)\|(\d+|--)\|(\$[\d.]+|--|\*)\|"
    r"(?:[^|]*\|)*?Model\|Providers\|",
)
# Since 2026-11-17 the creator is the logo's alt text and an optional
# effort label follows the name inside the first cell.
_LOGO_ROW_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\|([A-Za-z0-9][^|]*?)\|(?:[ \t]+\|([^|]+)\|)?(\d+|--)\|(\$[\d.]+|--|\*)\|"
    r"(?:[^|]*\|)*?Model\|Providers\|",
)
_TABLE_ROW: Final[re.Pattern[str]] = re.compile(r"<tr\b.*?</tr>", re.DOTALL | re.IGNORECASE)
_CREATOR_LOGO: Final[re.Pattern[str]] = re.compile(
    r'<img\b(?=[^>]*\ssrc="(?:/img/logos/|/_next/image\?url=%2Fimg%2Flogos%2F))[^>]*\salt="([^"]+)"',
)
_MODEL_LINK: Final[re.Pattern[str]] = re.compile(r'href="/models/([^"/?#]+)"[^>]*>\s*Model\b')
_EFFORT_SUFFIX: Final[re.Pattern[str]] = re.compile(r"\s*\(([a-z]+(?:-[a-z]+)?)(?: with fallback)?\)\s*$")
_LABEL_EFFORTS: Final[frozenset[str]] = frozenset((*EFFORT_ORDER, "reasoning", "ultra"))
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


def _label_effort(label: str) -> str | None:
    for part in label.split(","):
        token = part.strip().lower().removesuffix(" with fallback").replace("-", "_")
        if token in _LABEL_EFFORTS:
            return token
    return None


def parse_leaderboard_rows(html: str) -> list[LeaderboardRow]:
    """Every row the page's table lists, in page order, each with its own detail link."""
    rows: list[LeaderboardRow] = []
    for tr in _TABLE_ROW.finditer(_table_region(html)):
        segment = tr.group(0)
        flat = _strip_tags(segment)
        match = _LOGO_ROW_PATTERN.match(flat)
        label_effort: str | None = None
        if match is not None:
            logo = _CREATOR_LOGO.search(segment.partition("</td>")[0])
            if logo is None:
                continue
            display_name, label, score_text, cost_text = match.groups()
            creator = logo.group(1)
            label_effort = _label_effort(label or "")
        else:
            match = _ROW_PATTERN.search(flat)
            if match is None:
                continue
            display_name, creator, score_text, cost_text = match.groups()
        base_name, suffix_effort = split_effort(display_name)
        link = _MODEL_LINK.search(segment)
        rows.append(
            LeaderboardRow(
                display_name=display_name.strip(),
                base_name=base_name,
                effort=label_effort or suffix_effort,
                creator=creator.strip().lower(),
                score_text=score_text,
                cost_text=cost_text,
                detail_slug=link.group(1) if link else None,
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


def _reading(
    cell: DiscoveredCell,
    *,
    source_id: str,
    metric: str,
    status: str,
    fetched_at: str,
    excerpt: str,
    number: float | None = None,
    text: str | None = None,
) -> Reading:
    return Reading(
        source_id=source_id,
        provider=cell.provider,
        runtime=cell.runtime,
        model=cell.model,
        effort=cell.effort,
        metric=metric,
        value_number=number,
        value_text=text,
        raw_excerpt=excerpt[:512],
        fetch_status=status,
        fetched_at=fetched_at,
    )


def _metric_reading(cell: DiscoveredCell, *, source_id: str, metric: str, raw: str, excerpt: str, fetched_at: str) -> Reading:
    """One checked reading: a value that is not a number, or is out of range, is a
    `parse_error` reading that names why; it never becomes a value (design G6)."""
    try:
        value = float(raw.lstrip("$"))
    except ValueError:
        return _reading(
            cell,
            source_id=source_id,
            metric=metric,
            status="parse_error",
            fetched_at=fetched_at,
            excerpt=excerpt,
            text=f"not a number: {raw!r}",
        )
    if not _in_range(metric, value):
        return _reading(
            cell,
            source_id=source_id,
            metric=metric,
            status="parse_error",
            fetched_at=fetched_at,
            excerpt=excerpt,
            text=f"out of range: {raw!r}",
        )
    return _reading(
        cell,
        source_id=source_id,
        metric=metric,
        status="ok",
        fetched_at=fetched_at,
        excerpt=excerpt,
        number=value,
    )


def leaderboard_readings(cell: DiscoveredCell, *, fetched_at: str) -> list[Reading]:
    """The cell's own leaderboard row as readings: its score, and its cost where priced."""
    row = cell.row
    if row.source_id != SOURCE_LEADERBOARD_HTML:
        return []
    readings: list[Reading] = []
    if row.score_text != "--":
        readings.append(
            _metric_reading(
                cell,
                source_id=SOURCE_LEADERBOARD_HTML,
                metric=_SCORE_METRIC,
                raw=row.score_text,
                excerpt=row.excerpt,
                fetched_at=fetched_at,
            )
        )
    if row.cost_text not in ("--", "*"):
        readings.append(
            _metric_reading(
                cell,
                source_id=SOURCE_LEADERBOARD_HTML,
                metric=_COST_METRIC,
                raw=row.cost_text,
                excerpt=row.excerpt,
                fetched_at=fetched_at,
            )
        )
    return readings


_FLIGHT_PUSH: Final[re.Pattern[str]] = re.compile(r"self\.__next_f\.push\((\[.*?\])\)</script>", re.DOTALL)
_API_EFFORT_NAME: Final[re.Pattern[str]] = re.compile(r"^(.+?) \((low|medium|high|xhigh|max)(?:, [^)]*)?\)$", re.IGNORECASE)


def _checked_pair(score: object, cost: object) -> tuple[float, float] | None:
    if isinstance(score, bool) or isinstance(cost, bool) or not isinstance(score, (int, float)) or not isinstance(cost, (int, float)):
        return None
    try:
        values = float(score), float(cost)
    except OverflowError:
        return None
    return values if _in_range(_SCORE_METRIC, values[0]) and _in_range(_COST_METRIC, values[1]) else None


def _nonempty_strings(*values: object) -> bool:
    return all(isinstance(value, str) and bool(value) for value in values)


def _api_identity(record: Mapping[str, object]) -> tuple[str, str, str, str] | None:
    slug, name, creator = record.get("slug"), record.get("name"), record.get("model_creator")
    if not _nonempty_strings(slug, name) or not isinstance(creator, dict) or not _nonempty_strings(creator.get("name")):
        return None
    slug, name, creator_name = cast(str, slug), cast(str, name), cast(str, creator["name"])
    match = _API_EFFORT_NAME.fullmatch(name)
    if match is None:
        return None  # not a measured reasoning-effort variant
    base_name, effort = match.group(1), match.group(2).lower()
    release_slug = slug if effort == "max" else slug.removesuffix(f"-{effort}")
    if not release_slug or (effort != "max" and release_slug == slug):
        return None
    return release_slug, base_name, creator_name.lower(), effort


def _api_variant(record: object) -> tuple[EffortVariant | None, str | None]:
    if not isinstance(record, dict):
        return None, "Data API record is not an object"
    identity = _api_identity(record)
    if identity is None:
        name = record.get("name")
        if isinstance(name, str) and _API_EFFORT_NAME.fullmatch(name) is None:
            return None, None
        return None, "Data API record identity or effort slug changed"
    release_slug, base_name, creator_name, effort = identity
    slug = cast(str, record["slug"])
    evaluations = record.get("evaluations")
    index_cost = record.get("artificial_analysis_intelligence_index_cost")
    cost_per_task = index_cost.get("cost_per_task") if isinstance(index_cost, dict) else None
    if not isinstance(evaluations, dict) or not isinstance(cost_per_task, dict):
        return None, f"Data API variant {slug!r} has a changed metric shape"
    pair = _checked_pair(
        evaluations.get("artificial_analysis_intelligence_index"),
        cost_per_task.get("total_cost"),
    )
    if pair is None:
        return None, f"Data API variant {slug!r} has no valid score/cost pair"
    return EffortVariant(SOURCE_API, release_slug, base_name, creator_name, effort, *pair, slug), None


def _deduplicate_variants(records: Sequence[object], parse_record: Callable[[object], tuple[EffortVariant | None, str | None]]) -> tuple[list[EffortVariant], list[str]]:
    variants: list[EffortVariant] = []
    notes: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates: set[tuple[str, str, str]] = set()
    for record in records:
        variant, note = parse_record(record)
        if note is not None:
            notes.append(note)
        if variant is None:
            continue
        if variant.key in seen:
            notes.append(f"Data API duplicate variant {variant.key!r}; no duplicate reading")
            duplicates.add(variant.key)
            continue
        seen.add(variant.key)
        variants.append(variant)
    return [variant for variant in variants if variant.key not in duplicates], notes


def parse_api_variants(payload: object) -> tuple[list[EffortVariant], list[str]]:
    """Parse the measured free-list shape. Variant slug and name must agree on the effort."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return [], ["Data API shape changed: missing data list"]
    records = payload["data"]
    if len(records) > 1000:
        return [], ["Data API shape changed: page exceeds 1000 records"]
    return _deduplicate_variants(records, _api_variant)


def _flight_models_fragment(raw: str) -> str | None:
    try:
        pushed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(pushed, list) or len(pushed) < 2 or not isinstance(pushed[1], str):
        return None
    marker = '"initialModels":'
    return pushed[1].split(marker, 1)[1] if marker in pushed[1] else None


def _page_collection(html: str) -> tuple[list[object] | None, list[str]]:
    collections: list[list[object]] = []
    for match in _FLIGHT_PUSH.finditer(html):
        fragment = _flight_models_fragment(match.group(1))
        if fragment is None:
            continue
        try:
            collection, _ = json.JSONDecoder().raw_decode(fragment)
        except json.JSONDecodeError:
            return None, ["model page initialModels collection changed shape"]
        if isinstance(collection, list):
            collections.append(collection)
    if len(collections) != 1 or len(collections[0]) > 1000:
        return None, [f"model page expected one bounded initialModels collection, found {len(collections)}"]
    return collections[0], []


def _page_variant(record: object) -> tuple[EffortVariant | None, str | None]:
    if not isinstance(record, dict):
        return None, "model page variant is not an object"
    release, effort, creator = (
        record.get("release"),
        record.get("effort"),
        record.get("creator"),
    )
    if not isinstance(release, dict) or not isinstance(effort, dict) or not isinstance(creator, dict):
        return None, "model page variant identity changed shape"
    release_slug, base_name, effort_slug, creator_slug = (
        release.get("slug"),
        release.get("name"),
        effort.get("slug"),
        creator.get("slug"),
    )
    if not _nonempty_strings(release_slug, base_name, effort_slug, creator_slug):
        return None, "model page variant identity is incomplete"
    release_slug, base_name = cast(str, release_slug), cast(str, base_name)
    effort_slug, creator_slug = cast(str, effort_slug), cast(str, creator_slug)
    index_cost = record.get("intelligenceIndexCostPerTask")
    cost = index_cost.get("cost") if isinstance(index_cost, dict) else None
    pair = _checked_pair(record.get("intelligenceIndex"), cost.get("total") if isinstance(cost, dict) else None)
    if pair is None:
        return None, None
    return EffortVariant(
        SOURCE_EMBEDDED,
        release_slug,
        base_name,
        creator_slug.lower(),
        effort_slug,
        *pair,
        str(record.get("slug", "")),
    ), None


def parse_page_variants(html: str) -> tuple[list[EffortVariant], list[str]]:
    """Decode only the bounded Next/Flight initialModels collection on a model page."""
    if len(html) > 5_000_000:
        return [], ["model page exceeds bounded parser size"]
    if "initialModels" not in html:
        return [], []  # older, separately rendered detail layout
    collection, notes = _page_collection(html)
    if collection is None:
        return [], notes
    return _deduplicate_variants(collection, _page_variant)


def variant_readings(cell: DiscoveredCell, variant: EffortVariant, *, fetched_at: str) -> list[Reading]:
    return [
        _reading(
            cell,
            source_id=variant.source_id,
            metric=metric,
            status="ok",
            fetched_at=fetched_at,
            excerpt=f"{variant.excerpt}; attribution: artificialanalysis.ai",
            number=value,
        )
        for metric, value in ((_SCORE_METRIC, variant.score), (_COST_METRIC, variant.cost))
    ]


_DETAIL_SCORE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"Intelligence\|(?:Updated\|)?#\|\d+\| / \|\d+\|(\d+)\|Artificial Analysis Intellig",
)
_DETAIL_COST_PATTERN: Final[re.Pattern[str]] = re.compile(r"(\$[\d.]+)\|Cost per Intelligence Index [Tt]ask")

FAMILY_PAGE_NO_DETAIL: Final[str] = "family page: no per-effort detail"
"""The reason a detail reading carries when the page is the model family's, not the cell's own."""

# A model's own page is titled "<name> (<effort>) - Intelligence, Performance & Price Analysis"; the
# family page the same URL now serves is titled "<name> Models - Intelligence, Performance & Price
# Comparison" and shows the family's top-effort values for every effort. Measured on the catalog's own
# stored detail readings: the title is the first text segment (the `raw_excerpt` of each reading), the
# per-effort form up to the 2026-11-13T23:15Z refresh, the family form from the 2026-11-14T05:30Z one,
# for the max-effort cell's URL as well as every lower effort's. Only this positive match counts as a
# family page: a page of any other shape is read as it always was, so an unknown shape stays an
# advisory and never a refusal (rul_becb4bed).
_FAMILY_PAGE_TITLE: Final[re.Pattern[str]] = re.compile(r"(?:^|\|)[^|]+ Models - Intelligence, Performance (?:&amp;|&) Price Comparison")


def parse_model_detail_html(html: str, *, cell: DiscoveredCell, fetched_at: str) -> list[Reading]:
    """One cell's own detail page: same two metrics, independently rendered.
    Missing/unparseable fields come back as `parse_error` readings rather than
    being silently dropped, so a reconciler run can see the difference between
    "this source disagreed" and "this source's page changed shape".

    A family page is not this cell's detail page: its score and cost are the family's
    top-effort values, so it comes back as one `not_listed` reading naming that and no
    value. The reconciler then accepts the leaderboard alone, exactly as when the page is missing."""
    variants, notes = parse_page_variants(html)
    if variants:
        matched = [variant for variant in variants if variant.key == (cell.row.creator, cell.row.detail_slug, cell.effort)]
        if len(matched) == 1:
            return variant_readings(cell, matched[0], fetched_at=fetched_at)
        return [
            _reading(
                cell,
                source_id=SOURCE_MODEL_DETAIL_HTML,
                metric=_SCORE_METRIC,
                status="not_listed",
                fetched_at=fetched_at,
                excerpt=cell.row.excerpt,
                text=f"no embedded record for {cell.row.detail_slug}@{cell.effort}",
            )
        ]
    if '"initialModels"' in html and notes:
        return [
            _reading(
                cell,
                source_id=SOURCE_MODEL_DETAIL_HTML,
                metric=_SCORE_METRIC,
                status="parse_error",
                fetched_at=fetched_at,
                excerpt=cell.row.excerpt,
                text=notes[0],
            )
        ]
    text = _strip_tags(html)
    excerpt = text[:512]
    if _FAMILY_PAGE_TITLE.search(excerpt):
        return [
            _reading(
                cell,
                source_id=SOURCE_MODEL_DETAIL_HTML,
                metric=_SCORE_METRIC,
                status="not_listed",
                fetched_at=fetched_at,
                excerpt=excerpt,
                text=FAMILY_PAGE_NO_DETAIL,
            )
        ]
    score_match = _DETAIL_SCORE_PATTERN.search(text)
    if score_match:
        readings = [
            _metric_reading(
                cell,
                source_id=SOURCE_MODEL_DETAIL_HTML,
                metric=_SCORE_METRIC,
                raw=score_match.group(1),
                excerpt=excerpt,
                fetched_at=fetched_at,
            )
        ]
    else:
        readings = [
            _reading(
                cell,
                source_id=SOURCE_MODEL_DETAIL_HTML,
                metric=_SCORE_METRIC,
                status="parse_error",
                fetched_at=fetched_at,
                excerpt=excerpt,
                text="no Intelligence Index on the detail page",
            )
        ]
    cost_match = _DETAIL_COST_PATTERN.search(text)
    if cost_match:
        readings.append(
            _metric_reading(
                cell,
                source_id=SOURCE_MODEL_DETAIL_HTML,
                metric=_COST_METRIC,
                raw=cost_match.group(1),
                excerpt=excerpt,
                fetched_at=fetched_at,
            )
        )
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


def _api_response(client: httpx.Client, api_key: str, page: int) -> object:
    try:
        response = client.get(
            _API_URL,
            params={"page": page},
            headers={"x-api-key": api_key, "User-Agent": _USER_AGENT},
            timeout=_FETCH_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FetchError("http_error", f"Data API page {page}: {type(exc).__name__}") from None


def _bounded_int(value: object, lower: int, upper: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lower <= value <= upper


def _page_length_fits(payload: dict[str, object], page_size: int) -> bool:
    data = payload["data"]
    return isinstance(data, list) and len(data) <= page_size


def _api_page_has_more(payload: object, page: int) -> bool | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or not isinstance(payload.get("pagination"), dict):
        return None
    pagination = payload["pagination"]
    total_pages = pagination.get("total_pages")
    page_size = pagination.get("page_size")
    if not _bounded_int(total_pages, 1, _MAX_API_PAGES):
        return None
    if not _bounded_int(page_size, 1, 1000):
        return None
    total_pages, page_size = cast(int, total_pages), cast(int, page_size)
    has_more = pagination.get("has_more")
    if pagination.get("page") != page or not isinstance(has_more, bool):
        return None
    if has_more != (page < total_pages) or not _page_length_fits(payload, page_size):
        return None
    return has_more


def fetch_api_variants(client: httpx.Client, api_key: str) -> tuple[list[EffortVariant], list[str]]:
    """Read bounded free-list pages; the credential stays in the HTTP header only."""
    variants: list[EffortVariant] = []
    notes: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    duplicates: set[tuple[str, str, str]] = set()
    for page in range(1, _MAX_API_PAGES + 1):
        payload = _api_response(client, api_key, page)
        parsed, page_notes = parse_api_variants(payload)
        notes.extend(page_notes[:20])
        if len(page_notes) > 20:
            notes.append(f"Data API page {page}: {len(page_notes) - 20} further unscored or changed records")
        has_more = _api_page_has_more(payload, page)
        if has_more is None:
            return [], [f"Data API page {page} pagination changed; embedded records are the fallback"]
        for variant in parsed:
            if variant.key in seen:
                notes.append(f"Data API duplicate across pages: {variant.key!r}")
                duplicates.add(variant.key)
            else:
                seen.add(variant.key)
                variants.append(variant)
        if not has_more:
            return [variant for variant in variants if variant.key not in duplicates], notes
    return [], [f"Data API exceeds {_MAX_API_PAGES} page bound; embedded records are the fallback"]
