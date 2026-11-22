"""Join publisher score and cost readings to harness-offered model efforts.

The publisher parsers and network entry points live in model_capability_sources;
this module owns the bounded discovery pass and launch-id join.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

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
from .model_capability_sources import (
    _CATALOG_VISIBILITY,
    _NOT_LAUNCHABLE_REPORTED,
    _SCORE_METRIC,
    _UNMAPPED_CREATORS_REPORTED,
    FAMILY_PAGE_NO_DETAIL,
    LEADERBOARD_PAGE_FLOOR,
    SOURCE_API,
    SOURCE_LEADERBOARD_HTML,
    SOURCE_MODEL_DETAIL_HTML,
    DiscoveredCell,
    DiscoveryRun,
    EffortVariant,
    FetchError,
    LeaderboardRow,
    Reading,
    _reading,
    fetch_api_variants,
    fetch_leaderboard_html,
    fetch_model_detail_html,
    leaderboard_readings,
    parse_leaderboard_rows,
    parse_model_detail_html,
    parse_page_variants,
    split_effort,
    variant_readings,
)
from .model_capability_sources import (
    SOURCE_EMBEDDED as SOURCE_EMBEDDED,
)
from .model_capability_sources import (
    parse_api_variants as parse_api_variants,
)

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


def _add_cell(
    tally: _RuntimeTally,
    adapter: RuntimeAdapter,
    row: LeaderboardRow,
    model_id: str,
    effort: str,
    note: str | None,
    *,
    derived: bool,
) -> None:
    if effort not in EFFORT_ORDER:  # design G2: an effort outside the ladder is reported, never a cell
        tally.outside_vocabulary.setdefault(model_id, set()).add(effort)
        return
    cell = DiscoveredCell(adapter.runtime, adapter.provider, model_id, effort, row, note, derived)
    if any(existing.key == cell.key for existing in tally.cells):
        tally.advisories.append(f"two leaderboard rows name {adapter.runtime}/{model_id}@{effort}; the first was used ({row.display_name!r} was ignored)")
        return
    tally.cells.append(cell)


def _label_cells(
    tally: _RuntimeTally,
    adapter: RuntimeAdapter,
    harness_list: HarnessList,
    label: str,
    label_rows: Sequence[LeaderboardRow],
    models: Sequence[HarnessModel],
) -> None:
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
        _add_cell(
            tally,
            adapter,
            row,
            model_id,
            effort,
            _creation_note(adapter, harness_list, derived=derived_mode),
            derived=derived_mode,
        )


def _row_without_model(
    tally: _RuntimeTally,
    adapter: RuntimeAdapter,
    harness_list: HarnessList,
    row: LeaderboardRow,
    *,
    derived_mode: bool,
) -> None:
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
        _add_cell(
            tally,
            adapter,
            row,
            model_id,
            effort,
            _creation_note(adapter, harness_list, derived=True),
            derived=True,
        )


def _note_outside_vocabulary(tally: _RuntimeTally, models: Sequence[HarnessModel]) -> None:
    """Harness efforts outside the ladder are reported per model (design 2.4); they never become cells."""
    for model in models:
        outside = [effort for effort in model.efforts if effort not in EFFORT_ORDER]
        if outside:
            tally.outside_vocabulary.setdefault(model.model_id, set()).update(outside)


def _runtime_cells(
    adapter: RuntimeAdapter,
    harness_list: HarnessList,
    rows: Sequence[LeaderboardRow],
    catalog_ids: Sequence[str],
) -> _RuntimeTally:
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
        "status": harness_list.status,
        "source": harness_list.source,
        "detail": harness_list.detail,
        "client_version": harness_list.client_version,
        "fetched_at": harness_list.fetched_at,
        "models_offered": len(harness_list.offered_models),
        "advisories": list(harness_list.advisories),
    }


def _read_page(client: httpx.Client, failures: list[tuple[str, str]]) -> list[LeaderboardRow]:
    try:
        rows = parse_leaderboard_rows(fetch_leaderboard_html(client))
    except FetchError as exc:
        failures.append((SOURCE_LEADERBOARD_HTML, f"{exc.status}: {exc.message}"))
        return []
    if len(rows) < LEADERBOARD_PAGE_FLOOR:
        failures.append(
            (
                SOURCE_LEADERBOARD_HTML,
                f"thin_page: {len(rows)} rows parsed, at least {LEADERBOARD_PAGE_FLOOR} are required; nothing was created or renewed from it",
            )
        )
        return []
    return rows


def _merge_embedded_variant(
    variant: EffortVariant,
    identities: set[tuple[str, str | None]],
    variants: dict[tuple[str, str, str], EffortVariant],
    conflicted: set[tuple[str, str, str]],
    notes: list[str],
) -> None:
    if (variant.creator, variant.release_slug) not in identities or variant.key in conflicted:
        return
    previous = variants.get(variant.key)
    if previous is not None:
        if (previous.score, previous.cost) != (variant.score, variant.cost):
            notes.append(f"conflicting embedded records for {variant.key!r}; neither will renew")
            variants.pop(variant.key)
            conflicted.add(variant.key)
        return
    variants[variant.key] = variant


def _read_detail_pages(client: httpx.Client, cells: Sequence[DiscoveredCell], failures: list[tuple[str, str]]) -> tuple[dict[str, str], dict[tuple[str, str, str], EffortVariant], set[tuple[str, str, str]], list[str]]:
    """Fetch each joined release once; retain only records for joined release identities."""
    pages: dict[str, str] = {}
    variants: dict[tuple[str, str, str], EffortVariant] = {}
    notes: list[str] = []
    conflicted: set[tuple[str, str, str]] = set()
    identities = {(cell.row.creator, cell.row.detail_slug) for cell in cells}
    for slug in sorted({cell.row.detail_slug for cell in cells if cell.row.detail_slug is not None}):
        try:
            html = fetch_model_detail_html(client, slug)
        except FetchError as exc:
            failures.append((f"{SOURCE_MODEL_DETAIL_HTML}:{slug}", f"{exc.status}: {exc.message}"))
            continue
        pages[slug] = html
        parsed, page_notes = parse_page_variants(html)
        notes.extend(f"{slug}: {note}" for note in page_notes)
        for variant in parsed:
            _merge_embedded_variant(variant, identities, variants, conflicted, notes)
    return pages, variants, conflicted, notes


def _page_readings(
    cell: DiscoveredCell,
    identity: tuple[str, str, str] | None,
    fetched_at: str,
    pages: Mapping[str, str],
    page_variants: Mapping[tuple[str, str, str], EffortVariant],
    conflicted: set[tuple[str, str, str]],
) -> list[Reading]:
    embedded = page_variants.get(identity) if identity is not None else None
    if embedded is not None:
        return variant_readings(cell, embedded, fetched_at=fetched_at)
    if identity in conflicted:
        return []
    slug = cell.row.detail_slug
    if slug is not None and slug in pages:
        return parse_model_detail_html(pages[slug], cell=cell, fetched_at=fetched_at)
    reason = "the leaderboard row has no detail link" if slug is None else "the detail page was unavailable"
    return [_reading(cell, source_id=SOURCE_MODEL_DETAIL_HTML, metric=_SCORE_METRIC, status="not_listed", fetched_at=fetched_at, excerpt=cell.row.excerpt, text=reason)]


def _gather_readings(
    cells: Sequence[DiscoveredCell],
    *,
    fetched_at: str,
    pages: Mapping[str, str],
    api_variants: Mapping[tuple[str, str, str], EffortVariant],
    page_variants: Mapping[tuple[str, str, str], EffortVariant],
    conflicted: set[tuple[str, str, str]],
) -> list[Reading]:
    readings: list[Reading] = []
    for cell in cells:
        readings.extend(leaderboard_readings(cell, fetched_at=fetched_at))
        slug = cell.row.detail_slug
        identity = (cell.row.creator, slug, cell.effort) if slug is not None else None
        api = api_variants.get(identity) if identity is not None else None
        if api is not None:
            readings.extend(variant_readings(cell, api, fetched_at=fetched_at))
        readings.extend(_page_readings(cell, identity, fetched_at, pages, page_variants, conflicted))
    return readings


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


def _source_advisories(failures: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(f"{source}: {reason}" for source, reason in failures)


def _read_harnesses(context: HarnessContext) -> dict[str, HarnessList]:
    return {runtime: adapter.read_harness(context) for runtime, adapter in RUNTIME_ADAPTERS.items()}


def _runtime_tallies(
    harness_lists: Mapping[str, HarnessList],
    rows: Sequence[LeaderboardRow],
    catalog_models: Mapping[str, Sequence[str]],
) -> dict[str, _RuntimeTally]:
    return {runtime: _runtime_cells(adapter, harness_lists[runtime], rows, catalog_models.get(runtime, ())) for runtime, adapter in RUNTIME_ADAPTERS.items()}


def _listed_ids(harness_lists: Mapping[str, HarnessList]) -> dict[str, tuple[str, ...]]:
    """Every launch id a usable harness list names, hidden ones included: the 'harness-listed' set."""
    return {runtime: tuple(model.model_id for model in harness_list.models) if _usable(harness_list) else () for runtime, harness_list in harness_lists.items()}


def _read_api(client: httpx.Client, api_key: str | None, failures: list[tuple[str, str]]) -> tuple[list[EffortVariant], list[str]]:
    if api_key is None:
        return [], ["Data API key unavailable; embedded model records are the fallback"]
    try:
        return fetch_api_variants(client, api_key)
    except FetchError as exc:
        failures.append((SOURCE_API, f"{exc.status}: {exc.message}"))
        return [], ["Data API unavailable; embedded model records are the fallback"]


def _discovery_run(
    *,
    readings: list[Reading],
    cells: list[DiscoveredCell],
    failures: list[tuple[str, str]],
    rows: list[LeaderboardRow],
    pages: Mapping[str, str],
    api_variants: list[EffortVariant],
    harness_lists: Mapping[str, HarnessList],
    tallies: Mapping[str, _RuntimeTally],
    api_notes: list[str],
    page_notes: list[str],
    leaderboard_advisories: tuple[str, ...],
) -> DiscoveryRun:
    sources = (
        (SOURCE_API, bool(api_variants)),
        (SOURCE_LEADERBOARD_HTML, bool(rows)),
        (SOURCE_MODEL_DETAIL_HTML, bool(pages)),
    )
    return DiscoveryRun(
        readings=tuple(readings),
        failures=tuple(failures),
        sources_ok=tuple(source for source, present in sources if present),
        cells=tuple(cells),
        leaderboard_ok=bool(rows),
        leaderboard_rows=len(rows),
        harness={runtime: _harness_summary(harness_list) for runtime, harness_list in harness_lists.items()},
        harness_model_ids=_listed_ids(harness_lists),
        alias_candidates=tuple(candidate for tally in tallies.values() for candidate in tally.alias_candidates),
        unmapped_creators=_unmapped_creators(rows),
        efforts_outside_vocabulary=_merged_outside_vocabulary(tallies),
        listed_not_launchable=tuple(name for tally in tallies.values() for name in tally.not_launchable)[:_NOT_LAUNCHABLE_REPORTED],
        creator_label_unmatched=_unmatched_labels(rows),
        advisories=(*leaderboard_advisories, *api_notes, *page_notes, *_advisories(tallies, harness_lists)),
    )


def discover_readings(
    client: httpx.Client,
    context: HarnessContext,
    *,
    catalog_models: Mapping[str, Sequence[str]],
    api_key: str | None = None,
) -> DiscoveryRun:
    """Read the harness lists and the leaderboard, join them, and gather every reading.

    `catalog_models` is runtime -> the launch ids the catalog already holds; it is
    what rows are joined to when a runtime's harness list cannot be read (design 1.7).
    A per-model detail failure never aborts the run: one bad page must not blank
    out every other cell's otherwise-good leaderboard reading."""
    fetched_at = context.now.isoformat()
    failures: list[tuple[str, str]] = []
    harness_lists = _read_harnesses(context)
    api_variants, api_notes = _read_api(client, api_key, failures)
    rows = _read_page(client, failures)
    leaderboard_advisories = _source_advisories([item for item in failures if item[0] == SOURCE_LEADERBOARD_HTML])
    api_rows = [variant.as_row() for variant in api_variants]
    initial_tallies = _runtime_tallies(harness_lists, [*rows, *api_rows], catalog_models)
    initial_cells = [cell for tally in initial_tallies.values() for cell in tally.cells]
    pages, page_variants, conflicted, page_notes = _read_detail_pages(client, initial_cells, failures)
    page_rows = [variant.as_row() for variant in page_variants.values()]
    tallies = _runtime_tallies(harness_lists, [*rows, *api_rows, *page_rows], catalog_models)
    cells = sorted((cell for tally in tallies.values() for cell in tally.cells), key=lambda cell: cell.key)
    api_by_key = {variant.key: variant for variant in api_variants}
    readings = _gather_readings(
        cells,
        fetched_at=fetched_at,
        pages=pages,
        api_variants=api_by_key,
        page_variants=page_variants,
        conflicted=conflicted,
    )
    return _discovery_run(
        readings=readings,
        cells=cells,
        failures=failures,
        rows=rows,
        pages=pages,
        api_variants=api_variants,
        harness_lists=harness_lists,
        tallies=tallies,
        api_notes=api_notes,
        page_notes=page_notes,
        leaderboard_advisories=leaderboard_advisories,
    )


__all__ = [
    "EffortVariant",
    "FAMILY_PAGE_NO_DETAIL",
    "LEADERBOARD_PAGE_FLOOR",
    "SOURCE_API",
    "SOURCE_EMBEDDED",
    "SOURCE_LEADERBOARD_HTML",
    "SOURCE_MODEL_DETAIL_HTML",
    "DiscoveredCell",
    "DiscoveryRun",
    "FetchError",
    "LeaderboardRow",
    "Reading",
    "discover_readings",
    "fetch_api_variants",
    "fetch_leaderboard_html",
    "fetch_model_detail_html",
    "leaderboard_readings",
    "parse_api_variants",
    "parse_leaderboard_rows",
    "parse_model_detail_html",
    "parse_page_variants",
    "split_effort",
    "variant_readings",
]
