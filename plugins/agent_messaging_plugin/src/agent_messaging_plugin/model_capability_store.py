"""State-layer primitives for the model capability catalog (iss_48ea8171).

Three tables, one contract: a cell is READABLE by dispatch only while its
`acceptance` is `accepted` and its `measured_at` is inside the staleness
window. This module owns the writes that move a cell between those states
and the one read that hands the selector a catalog; it never decides which
cell wins — that is `dispatch_tier_selection`, which is pure.

Seeding is deliberately weak: a seed writes every cell as
`pending_crosscheck`, never `accepted`, and never downgrades a cell a real
refresh run already accepted. The hand-reconciled 2026-09-19 table is a
starting point, not evidence, and the store says so on every row it seeds.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records, require_updated

from .dispatch_tier_selection import EFFORT_ORDER, BillingObjective, CapabilityCell, CatalogWindow, catalog_staleness_window, resolve_freshness_window
from .model_capability_fetch import DiscoveredCell, DiscoveryRun, Reading
from .model_capability_harness import RUNTIME_ADAPTERS, HarnessError, adapter_for_runtime
from .model_capability_harness_pass import ACCEPTANCE_CHANGE_METRIC, refresh_outlook
from .model_capability_reconcile import (
    CellKey,
    CreationPlan,
    ObservationRef,
    ReconcileOutcome,
    RefreshReport,
    RunTally,
    build_report,
    cost_floor,
    flag_new_cell,
    group_readings,
    key_text,
    manual_disagreement_note,
    plan_creation,
    prior_value,
    processed_cells,
    reconcile_cells,
    unrenewable_stale,
)
from .schema import (
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    CELL_ACCEPTANCE_WITHDRAWN,
    CELL_ACCEPTANCES,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
    TABLE_MODEL_CAPABILITY_REFRESH_RUN,
)
from .usage_economics_profiles import (
    FlatRateQuotaProfile,
    UsageEconomicsProfileValidationError,
    load_usage_economics_profile_catalog,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_PLUGIN_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
SEED_PATH: Final[Path] = _PLUGIN_ROOT / "model_capability_seed.v1.json"
USAGE_ECONOMICS_PATH: Final[Path] = _PLUGIN_ROOT / "model_profiles" / "usage_economics.v1.json"
CATALOG_POLICY_ID: Final[str] = "catalog"
"""Governing configuration identity (seat ruling agm-_3fa65478)."""

CATALOG_PAGE_SIZE: Final[int] = 100
"""Rows per `query_ordered` page: the state layer's own page cap. A catalog
read walks the pages with the `after` cursor until a short page."""

CATALOG_READ_CEILING: Final[int] = 1000
"""Sanity ceiling on one catalog read (design 6.3). Past it the read returns the
first rows in (runtime, model, effort) order and says so; it never raises.
Discovery's live-row creation ceiling (design G4, 300) sits below it."""

_CELL_KEY: Final[tuple[str, str, str]] = ("runtime", "model", "effort")
_SEED_SOURCE_ID: Final[str] = "seed_table"


class CatalogError(Exception):
    """A catalog operation was refused. Carries a code for the verb layer."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class SeedCell:
    provider: str
    runtime: str
    model: str
    effort: str
    capability_score: float
    cost_per_task_usd: float | None


@dataclass(frozen=True, slots=True)
class SeedTable:
    seed_version: str
    measured_at: str
    fetch_method: str
    cells: tuple[SeedCell, ...]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str) or not raw:
        raise CatalogError("catalog_invalid", f"{field} must be an ISO-8601 timestamp.")
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CatalogError("catalog_invalid", f"{field} is not ISO-8601: {raw!r}.") from exc
    if value.tzinfo is None:
        raise CatalogError("catalog_invalid", f"{field} must be timezone-aware.")
    return value


def _seed_number(raw: object, field: str, *, optional: bool) -> float | None:
    if raw is None and optional:
        return None
    if not isinstance(raw, (int, float)) or isinstance(raw, bool) or raw < 0:
        raise CatalogError("seed_invalid", f"{field} must be a non-negative number.")
    return float(raw)


def _seed_text(raw: object, field: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise CatalogError("seed_invalid", f"{field} must be a non-empty string.")
    return raw.strip()


def _seed_cell(raw: object, index: int) -> SeedCell:
    if not isinstance(raw, dict):
        raise CatalogError("seed_invalid", f"cells[{index}] must be an object.")
    row: dict[str, Any] = raw
    effort = _seed_text(row.get("effort"), f"cells[{index}].effort")
    if effort not in EFFORT_ORDER:
        raise CatalogError("seed_invalid", f"cells[{index}].effort {effort!r} is not a known effort.")
    score = _seed_number(row.get("capability_score"), f"cells[{index}].capability_score", optional=False)
    if score is None:
        raise CatalogError("seed_invalid", f"cells[{index}].capability_score is required.")
    return SeedCell(
        provider=_seed_text(row.get("provider"), f"cells[{index}].provider"),
        runtime=_seed_text(row.get("runtime"), f"cells[{index}].runtime"),
        model=_seed_text(row.get("model"), f"cells[{index}].model"),
        effort=effort,
        capability_score=score,
        cost_per_task_usd=_seed_number(row.get("cost_per_task_usd"), f"cells[{index}].cost_per_task_usd", optional=True),
    )


def load_seed_table(path: Path = SEED_PATH) -> SeedTable:
    """Parse and validate the declarative seed; refuse rather than half-load."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError("seed_unavailable", f"seed table is unreadable: {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise CatalogError("seed_invalid", "seed table must be an object with schema_version=1.")
    root: dict[str, Any] = raw
    provenance = root.get("provenance")
    if not isinstance(provenance, dict):
        raise CatalogError("seed_invalid", "seed table requires a provenance object.")
    cells_raw = root.get("cells")
    if not isinstance(cells_raw, list) or not cells_raw:
        raise CatalogError("seed_invalid", "seed table requires a non-empty cells list.")
    cells = tuple(_seed_cell(row, index) for index, row in enumerate(cells_raw))
    keys = {(cell.runtime, cell.model, cell.effort) for cell in cells}
    if len(keys) != len(cells):
        raise CatalogError("seed_invalid", "seed table repeats a (runtime, model, effort) cell.")
    measured_at = _seed_text(root.get("measured_at"), "measured_at")
    _parse_stamp(measured_at, "measured_at")
    return SeedTable(
        seed_version=_seed_text(root.get("seed_version"), "seed_version"),
        measured_at=measured_at,
        fetch_method=_seed_text(provenance.get("fetch_method"), "provenance.fetch_method"),
        cells=cells,
    )


def open_refresh_run(state: StateManagementInterface, *, trigger: str) -> str:
    record: dict[str, Any] = {
        "started_at": _now_iso(),
        "finished_at": None,
        "trigger": trigger,
        "status": "running",
        "sources_ok": [],
        "sources_failed": [],
        "cells_accepted": 0,
        "cells_conflicted": 0,
        "cells_unchanged": 0,
        "note": None,
    }
    data = require_completed(
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_REFRESH_RUN, "record": record}),
        "open model capability refresh run",
    )
    result = data.get("result")
    run_id = result.get("generated_id") if isinstance(result, dict) else None
    if not isinstance(run_id, str) or not run_id:
        raise CatalogError("catalog_invalid", "refresh run insert returned no generated_id.")
    return run_id


def close_refresh_run(
    state: StateManagementInterface,
    run_id: str,
    *,
    counts: dict[str, int],
    sources_ok: list[str],
    note: str | None,
    sources_failed: list[str] | None = None,
) -> None:
    updates: dict[str, Any] = {
        "finished_at": _now_iso(),
        "status": "completed",
        "sources_ok": sources_ok,
        "sources_failed": sources_failed or [],
        "note": note,
        **counts,
    }
    require_completed(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_REFRESH_RUN, "filters": {"id": run_id}},
            updates,
        ),
        "close model capability refresh run",
    )


@dataclass(frozen=True, slots=True)
class CellRead:
    """The catalog rows of one read, and whether the read stopped at the ceiling.

    ``first_unconsidered`` is the (runtime, model, effort) key of the first row
    the read did not return; ``None`` means the rows are the whole filtered catalog.
    """

    rows: list[dict[str, Any]]
    first_unconsidered: tuple[str, str, str] | None

    @property
    def truncated(self) -> bool:
        return self.first_unconsidered is not None


def read_cells_page(
    state: StateManagementInterface,
    *,
    runtime: str | None = None,
    model: str | None = None,
    ceiling: int | None = CATALOG_READ_CEILING,
) -> CellRead:
    """Walk the filtered catalog in `CATALOG_PAGE_SIZE` pages up to `ceiling` rows (default `CATALOG_READ_CEILING`).

    The runtime and model filters run in the database. Each accepted row's
    relative_cost_multiplier is derived at read time against the catalog-wide
    price floor, which is one bounded query, so a cheaper cell recorded later
    rebases every other cell without rewriting stored rows. Past the ceiling the
    read stops and reports the first row it did not consider; it never raises.
    The refresh passes `ceiling=None` and walks every page: the rows it must see
    include withdrawn history, which it may neither duplicate nor reverse, and a
    ceiling there would hide cells from renewal with no way to clear it
    (no verb deletes a row; iss_75b6cb2c).
    """
    filters: dict[str, Any] = {}
    if runtime:
        filters["runtime"] = runtime
    if model:
        filters["model"] = model
    collected: list[dict[str, Any]] = []
    after: list[object] | None = None
    while True:
        query: dict[str, object] = {
            "table": TABLE_MODEL_CAPABILITY_CELL,
            "filters": filters,
            "order_by": [["runtime", "asc"], ["model", "asc"], ["effort", "asc"], ["id", "asc"]],
            "limit": CATALOG_PAGE_SIZE,
        }
        if after is not None:
            query["after"] = after
        page = [dict(row) for row in require_records(state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query))]
        collected.extend(page)
        if ceiling is not None and len(collected) > ceiling:
            beyond = collected[ceiling]
            del collected[ceiling:]
            _derive_current_relative_costs(collected, _acceptedcost_floor(state))
            return CellRead(collected, _cell_key(beyond))
        if len(page) < CATALOG_PAGE_SIZE:
            break
        last = page[-1]
        after = [last["runtime"], last["model"], last["effort"], last["id"]]
    _derive_current_relative_costs(collected, _acceptedcost_floor(state))
    return CellRead(collected, None)


def _cell_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["runtime"]), str(row["model"]), str(row["effort"]))


def read_cells(state: StateManagementInterface, *, runtime: str | None = None, model: str | None = None) -> list[dict[str, Any]]:
    """The rows of `read_cells_page`, for callers that do not show the truncation notice themselves.

    A truncated read is logged here at warning level; a caller that can carry a
    notice to its operator reads `read_cells_page` instead.
    """
    read = read_cells_page(state, runtime=runtime, model=model)
    if read.first_unconsidered is not None:
        logger.warning(
            "model capability catalog read truncated at %d rows; first row not considered: %s",
            CATALOG_READ_CEILING,
            "/".join(read.first_unconsidered),
        )
    return read.rows


def _acceptedcost_floor(state: StateManagementInterface) -> float | None:
    """The cheapest positive cost among accepted cells, read as one row."""
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"acceptance": CELL_ACCEPTANCE_ACCEPTED, "cost_per_task_usd": {"op": "gt", "value": 0}},
                "order_by": [["cost_per_task_usd", "asc"], ["id", "asc"]],
                "limit": 1,
            },
        ),
    )
    cost = rows[0].get("cost_per_task_usd") if rows else None
    if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not math.isfinite(cost):
        return None
    return float(cost)


def _relative_cost(cost: object, floor: float | None) -> float | None:
    if not isinstance(cost, (int, float)) or not math.isfinite(cost):
        return None
    if cost == 0:
        return 0.0
    return float(cost) / floor if floor is not None else None


def _derive_current_relative_costs(rows: list[dict[str, Any]], floor: float | None) -> None:
    for row in rows:
        if row.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED:
            row["relative_cost_multiplier"] = _relative_cost(row.get("cost_per_task_usd"), floor)


def _existing_cell(state: StateManagementInterface, cell: SeedCell) -> dict[str, Any] | None:
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"runtime": cell.runtime, "model": cell.model, "effort": cell.effort},
                "order_by": [["updated_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return dict(rows[0]) if rows else None


def _record_seed_observations(state: StateManagementInterface, run_id: str, seed: SeedTable, cell: SeedCell) -> None:
    metrics: list[tuple[str, float | None]] = [("intelligence_index", cell.capability_score)]
    if cell.cost_per_task_usd is not None:
        metrics.append(("cost_per_task_usd", cell.cost_per_task_usd))
    for metric, value in metrics:
        record: dict[str, Any] = {
            "refresh_run_id": run_id,
            "source_id": _SEED_SOURCE_ID,
            "fetch_method": seed.fetch_method,
            "fetched_at": seed.measured_at,
            "provider": cell.provider,
            "runtime": cell.runtime,
            "model": cell.model,
            "effort": cell.effort,
            "metric": metric,
            "value_number": value,
            "value_text": None,
            "raw_excerpt": f"{seed.seed_version}: {cell.model}@{cell.effort} {metric}={value}",
            "fetch_status": "ok",
        }
        require_completed(
            state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
            "record seed observation",
        )


def _relative_multipliers(cells: tuple[SeedCell, ...]) -> dict[tuple[str, str, str], float | None]:
    """cost over the cheapest priced cell in the SEED — a seed-relative figure, re-derived by every real run."""
    priced = [cell.cost_per_task_usd for cell in cells if cell.cost_per_task_usd]
    floor = min(priced) if priced else None
    return {(cell.runtime, cell.model, cell.effort): (None if cell.cost_per_task_usd is None or floor is None else cell.cost_per_task_usd / floor) for cell in cells}


def _seed_cell_record(seed: SeedTable, cell: SeedCell, run_id: str, *, staleness_window_hours: int | None, relative: float | None) -> dict[str, Any]:
    return {
        "provider": cell.provider,
        "runtime": cell.runtime,
        "model": cell.model,
        "effort": cell.effort,
        "capability_score": cell.capability_score,
        "cost_per_task_usd": cell.cost_per_task_usd,
        "relative_cost_multiplier": relative,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": seed.measured_at,
        "accepted_at": None,
        "acceptance": CELL_ACCEPTANCE_PENDING_CROSSCHECK,
        "agreeing_observation_ids": [],
        "disagreement_note": "seeded from the hand-reconciled table; awaiting a cross-checked refresh run",
        "staleness_window_hours": staleness_window_hours,
        "last_refresh_run_id": run_id,
    }


def _write_policy_cell(state: StateManagementInterface, record: dict[str, Any]) -> None:
    # Local import keeps policy's reuse of catalog readback acyclic.
    from .model_cell_enablement import write_catalog_cell

    write_catalog_cell(state, record)


def seed_catalog(state: StateManagementInterface, *, seed: SeedTable, staleness_window_hours: float | None = None) -> dict[str, Any]:
    """Write the seed as `pending_crosscheck` cells; never touch an accepted cell.

    Idempotent: re-seeding rewrites pending cells with the same values and
    leaves accepted ones alone, so a seed can run at every start-up without
    ever undoing what a real refresh established.
    """
    window = resolve_catalog_window(state, read_cells(state), requested=staleness_window_hours)
    staleness_window_hours = window.cell_hours
    run_id = open_refresh_run(state, trigger="seed")
    relative = _relative_multipliers(seed.cells)
    seeded = 0
    preserved: list[str] = []
    for cell in seed.cells:
        existing = _existing_cell(state, cell)
        if existing is not None and existing.get("acceptance") == CELL_ACCEPTANCE_ACCEPTED:
            preserved.append(f"{cell.model}@{cell.effort}")
            continue
        _record_seed_observations(state, run_id, seed, cell)
        _write_policy_cell(
            state,
            _seed_cell_record(
                seed,
                cell,
                run_id,
                staleness_window_hours=staleness_window_hours,
                relative=relative[(cell.runtime, cell.model, cell.effort)],
            ),
        )
        seeded += 1
    counts = {"cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": len(preserved)}
    close_refresh_run(state, run_id, counts=counts, sources_ok=[_SEED_SOURCE_ID], note=f"seed {seed.seed_version}: {seeded} pending, {len(preserved)} accepted cells preserved")
    return {"run_id": run_id, "seed_version": seed.seed_version, "cells_seeded": seeded, "accepted_cells_preserved": preserved, "freshness_window": asdict(window)}


def _write_reading_observation(state: StateManagementInterface, run_id: str, reading: Reading) -> str:
    """One `model_capability_observation` row for one real fetched reading.
    Returns its generated id, so the reconciler's `agreeing_observation_ids`
    can cite it verbatim."""
    record: dict[str, Any] = {
        "refresh_run_id": run_id,
        "source_id": reading.source_id,
        "fetch_method": f"GET (parsed by model_capability_fetch, {reading.source_id})",
        "fetched_at": reading.fetched_at,
        "provider": reading.provider,
        "runtime": reading.runtime,
        "model": reading.model,
        "effort": reading.effort,
        "metric": reading.metric,
        "value_number": reading.value_number,
        "value_text": reading.value_text,
        "raw_excerpt": reading.raw_excerpt,
        "fetch_status": reading.fetch_status,
    }
    data = require_completed(
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
        "record model capability observation",
    )
    result = data.get("result")
    observation_id = result.get("generated_id") if isinstance(result, dict) else None
    if not isinstance(observation_id, str) or not observation_id:
        raise CatalogError("catalog_invalid", "observation insert returned no generated_id.")
    return observation_id


def _provider_for(runtime: str) -> str:
    """The provider of a runtime, from the adapter table; an unknown runtime raises, it never defaults to a provider."""
    try:
        return adapter_for_runtime(runtime).provider
    except HarnessError as exc:
        raise CatalogError("catalog_invalid", str(exc)) from exc


def _reconciled_cell_record(outcome: ReconcileOutcome, run_id: str, *, staleness_window_hours: int | None, relative: float | None) -> dict[str, Any]:
    runtime, model, effort = outcome.cell_key
    return {
        "provider": _provider_for(runtime),
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "capability_score": outcome.capability_score,
        "cost_per_task_usd": outcome.cost_per_task_usd,
        "relative_cost_multiplier": relative,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": outcome.measured_at,
        "accepted_at": _now_iso() if outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED else None,
        "acceptance": outcome.acceptance,
        "agreeing_observation_ids": outcome.agreeing_observation_ids,
        "disagreement_note": outcome.disagreement_note,
        "staleness_window_hours": staleness_window_hours,
        "last_refresh_run_id": run_id,
    }


def _existing_cell_by_key(state: StateManagementInterface, key: tuple[str, str, str]) -> dict[str, Any] | None:
    """`_existing_cell` keyed by (runtime, model, effort) rather than a `SeedCell`."""
    runtime, model, effort = key
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"runtime": runtime, "model": model, "effort": effort},
                "order_by": [["updated_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return dict(rows[0]) if rows else None


def _write_all_observations(state: StateManagementInterface, run_id: str, readings: list[Reading]) -> dict[tuple[str, str, str], list[ObservationRef]]:
    written: dict[tuple[str, str, str], list[ObservationRef]] = {}
    for reading in readings:
        observation_id = _write_reading_observation(state, run_id, reading)
        written.setdefault((reading.runtime, reading.model, reading.effort), []).append(
            ObservationRef(observation_id=observation_id, reading=reading),
        )
    return written


def _manual_source_for_cell(state: StateManagementInterface, existing: dict[str, Any]) -> bool:
    """Identify a direct recording by its persisted observation, not its run trigger."""
    ids = existing.get("agreeing_observation_ids")
    if not isinstance(ids, list) or not ids or not isinstance(ids[0], str):
        return False
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_OBSERVATION,
                "filters": {"id": ids[0]},
                "order_by": [["created_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    return bool(rows and rows[0].get("source_id") == _MANUAL_SOURCE_ID)


def _write_cell(
    state: StateManagementInterface,
    outcome: ReconcileOutcome,
    run_id: str,
    *,
    staleness_window_hours: int | None,
    floor: float | None,
    unverified_since: str | None = None,
) -> None:
    """Upsert one reconciled cell. `unverified_since` starts the harness-absence clock on a cell whose launch id no harness list confirmed."""
    relative = None if outcome.cost_per_task_usd is None or floor is None else outcome.cost_per_task_usd / floor
    record = _reconciled_cell_record(outcome, run_id, staleness_window_hours=staleness_window_hours, relative=relative)
    if unverified_since is not None:
        record["harness_unverified_since"] = unverified_since
    _write_policy_cell(state, record)


def _hold_withdrawn(state: StateManagementInterface, cell: DiscoveredCell, outcome: ReconcileOutcome, existing: dict[str, Any], run_id: str, tally: RunTally) -> str:
    """A refresh never changes the acceptance of a withdrawn cell (iss_71a21363, design mutant 13).

    The readings were recorded as observations already. When two sources agree, or one source
    stays inside tolerance of the retained value (`prior_value` bounds a withdrawn cell too),
    the cell's values are refreshed in place and it stays withdrawn; anything else leaves it
    untouched. Acceptance moves only by the harness pass's reinstatement of an automatic
    withdrawal or by `reinstate_model_capability_cell`. The write is predicated on the cell
    still being withdrawn, so a reinstatement that lands first is not overwritten."""
    tally.withdrawn_left.append(key_text(cell.key))
    if outcome.acceptance != CELL_ACCEPTANCE_ACCEPTED:  # a conflict, or a lone reading past tolerance (pending): the retained value stands
        return "unchanged"
    require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_CELL, "filters": {"id": existing["id"], "acceptance": CELL_ACCEPTANCE_WITHDRAWN}},
            {
                "capability_score": outcome.capability_score,
                "cost_per_task_usd": outcome.cost_per_task_usd,
                "measured_at": outcome.measured_at,
                "agreeing_observation_ids": outcome.agreeing_observation_ids,
                "last_refresh_run_id": run_id,
            },
        ),
    )
    return "unchanged"


def _hold_manual_value(state: StateManagementInterface, cell: DiscoveredCell, outcome: ReconcileOutcome, existing: dict[str, Any] | None, run_id: str, tally: RunTally) -> bool:
    """Q8: keep a manual cell's values when the live pages disagree, with a loud note; True when it did."""
    if existing is None or outcome.acceptance != CELL_ACCEPTANCE_ACCEPTED:
        return False
    prior = prior_value(existing)
    if prior is None or not _manual_source_for_cell(state, existing):
        return False
    advisory = manual_disagreement_note(prior, outcome)
    if advisory is None:
        return False
    require_completed(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_CELL, "filters": {"id": existing["id"]}},
            {"disagreement_note": advisory, "last_refresh_run_id": run_id},
        ),
        "note a manual versus live disagreement",
    )
    tally.manual_conflicts.append(key_text(cell.key))
    return True


def _settle_cell(
    state: StateManagementInterface,
    cell: DiscoveredCell,
    outcome: ReconcileOutcome,
    existing: dict[str, Any] | None,
    run_id: str,
    *,
    staleness_window_hours: int | None,
    floor: float | None,
    tally: RunTally,
) -> str:
    """Write one reconciled outcome and return 'accepted' | 'conflicted' | 'unchanged'.

    The refresh never writes a `pending_crosscheck` cell (design 4.3): a single-source
    reading that moved past tolerance, or any pending outcome, leaves the prior cell as it
    is. A genuine `crosscheck_conflict` DOES overwrite a prior accepted cell (real
    disagreement is real signal). A renewal resets the staleness window to
    `staleness_window_hours`, so a manual cell that live sources renew takes the current resolved window.
    A withdrawn cell is the exception: its acceptance never moves here (`_hold_withdrawn`)."""
    if existing is not None and existing.get("acceptance") == CELL_ACCEPTANCE_WITHDRAWN:
        return _hold_withdrawn(state, cell, outcome, existing, run_id, tally)
    if outcome.single_source_unconfirmed:
        tally.unconfirmed.append(key_text(cell.key))
        return "unchanged"
    if outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK or _hold_manual_value(state, cell, outcome, existing, run_id, tally):
        return "unchanged"
    if outcome.acceptance not in (CELL_ACCEPTANCE_ACCEPTED, CELL_ACCEPTANCE_CROSSCHECK_CONFLICT):
        raise CatalogError("catalog_invalid", f"reconcile_cell produced an unexpected acceptance {outcome.acceptance!r}.")
    unverified_since: str | None = None
    if existing is None:
        outcome = flag_new_cell(outcome, cell)
        tally.created.append(key_text(cell.key))
        unverified_since = _now_iso() if cell.derived_launch_id else None  # the clock withdraws a derived id no `ok` list ever confirms (design 1.7)
    _write_cell(state, outcome, run_id, staleness_window_hours=staleness_window_hours, floor=floor, unverified_since=unverified_since)
    return "accepted" if outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED else "conflicted"


def _apply_outcomes(
    state: StateManagementInterface,
    processed: Sequence[DiscoveredCell],
    outcomes: Mapping[CellKey, ReconcileOutcome],
    by_key: Mapping[CellKey, dict[str, Any]],
    run_id: str,
    *,
    staleness_window_hours: int | None,
) -> RunTally:
    floor = cost_floor(dict(outcomes))
    tally = RunTally()
    for cell in processed:
        outcome = outcomes.get(cell.key)
        if outcome is not None:
            tally.counts[_settle_cell(state, cell, outcome, by_key.get(cell.key), run_id, staleness_window_hours=staleness_window_hours, floor=floor, tally=tally)] += 1
    return tally


def _creation_plan(run: DiscoveryRun, catalog: CellRead, by_key: Mapping[CellKey, dict[str, Any]], readings_by_key: Mapping[CellKey, Sequence[Reading]]) -> CreationPlan:
    live = sum(1 for row in catalog.rows if row.get("acceptance") != CELL_ACCEPTANCE_WITHDRAWN)
    return plan_creation(run.cells, by_key, live_rows=live, readings_by_key=readings_by_key)


def reconcile_discovery(
    state: StateManagementInterface,
    run_id: str,
    run: DiscoveryRun,
    *,
    staleness_window_hours: float | None = None,
    now: datetime | None = None,
) -> RefreshReport:
    """Write the readings of every cell this run may touch, reconcile each, and upsert the cell table.

    Existing cells are always processed. New cells are created subject to the per-run
    cap and the live-row ceiling (`plan_creation`); a cell with no usable leaderboard
    reading is neither created nor renewed and never becomes `pending_crosscheck`
    (design 4.3). Mirrors `seed_catalog`'s "never silently undo an already-accepted
    cell on weaker evidence" rule: a genuine `crosscheck_conflict` DOES overwrite a
    prior accepted cell, but a thin single-source reading never does."""
    catalog = read_cells_page(state, ceiling=None)
    window = resolve_catalog_window(state, catalog.rows, requested=staleness_window_hours)
    by_key = {_cell_key(row): row for row in catalog.rows}
    readings_by_key = group_readings(run.readings)
    plan = _creation_plan(run, catalog, by_key, readings_by_key)
    processed = processed_cells(run, by_key, plan)
    written = _write_all_observations(state, run_id, [reading for cell in processed for reading in readings_by_key.get(cell.key, ())])
    outcomes = reconcile_cells(processed, written, by_key)
    tally = _apply_outcomes(state, processed, outcomes, by_key, run_id, staleness_window_hours=window.cell_hours)
    unrenewable = unrenewable_stale(run, by_key, {cell.key for cell in run.cells if cell.key in outcomes}, now=now or datetime.now(UTC))
    report = build_report(run, plan, tally, unrenewable=unrenewable)
    report.result_fields["freshness_window"] = asdict(window)
    report.result_fields["advisories"].extend(window.advisories)
    return report


_MANUAL_SOURCE_ID: Final[str] = "manual_record"
_RUNTIME_PROVIDERS: Final[Mapping[str, str]] = MappingProxyType({runtime: adapter.provider for runtime, adapter in RUNTIME_ADAPTERS.items()})
"""A view of `RUNTIME_ADAPTERS`, the one table that names a runtime's provider."""
KNOWN_RUNTIMES: Final[tuple[str, ...]] = tuple(sorted(RUNTIME_ADAPTERS))
_MAX_EXCERPT: Final[int] = 512


@dataclass(frozen=True, slots=True)
class ManualCell:
    """One caller-supplied catalog reading (operator ruling rul_9e7a67ba:
    anyone with better information can update the table directly)."""

    runtime: str
    model: str
    effort: str
    capability_score: float
    cost_per_task_usd: float | None
    source_ref: str
    note: str | None


def _validate_manual_cell(cell: ManualCell) -> None:
    if cell.runtime not in _RUNTIME_PROVIDERS:
        raise CatalogError("parameter_invalid", f"runtime must be one of {sorted(_RUNTIME_PROVIDERS)}; got {cell.runtime!r}.")
    if not cell.model.strip():
        raise CatalogError("parameter_invalid", "model must be a non-blank canonical model id.")
    if cell.effort not in EFFORT_ORDER:
        raise CatalogError("parameter_invalid", f"effort must be one of {list(EFFORT_ORDER)}; got {cell.effort!r}.")
    if not math.isfinite(cell.capability_score) or not 0 <= cell.capability_score <= 100:
        raise CatalogError("parameter_invalid", f"capability_score is on the 0-100 Intelligence Index scale; got {cell.capability_score}.")
    if cell.cost_per_task_usd is not None and (not math.isfinite(cell.cost_per_task_usd) or cell.cost_per_task_usd < 0):
        raise CatalogError("parameter_invalid", f"cost_per_task_usd must be non-negative; got {cell.cost_per_task_usd}.")
    if not cell.source_ref.strip():
        raise CatalogError("parameter_invalid", "source_ref is required: name where the reading came from (a URL or a document).")


def _record_manual_observations(state: StateManagementInterface, run_id: str, cell: ManualCell, *, recorded_at: str) -> list[str]:
    metrics: list[tuple[str, float]] = [("intelligence_index", cell.capability_score)]
    if cell.cost_per_task_usd is not None:
        metrics.append(("cost_per_task_usd", cell.cost_per_task_usd))
    excerpt = (f"{cell.source_ref} | {cell.note}" if cell.note else cell.source_ref)[:_MAX_EXCERPT]
    observation_ids: list[str] = []
    for metric, value in metrics:
        record: dict[str, Any] = {
            "refresh_run_id": run_id,
            "source_id": _MANUAL_SOURCE_ID,
            "fetch_method": f"recorded directly via record_model_capability_cell; source: {cell.source_ref}"[:_MAX_EXCERPT],
            "fetched_at": recorded_at,
            "provider": _RUNTIME_PROVIDERS[cell.runtime],
            "runtime": cell.runtime,
            "model": cell.model,
            "effort": cell.effort,
            "metric": metric,
            "value_number": value,
            "value_text": None,
            "raw_excerpt": excerpt,
            "fetch_status": "ok",
        }
        data = require_completed(
            state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
            "record manual model capability observation",
        )
        result = data.get("result")
        observation_id = result.get("generated_id") if isinstance(result, dict) else None
        if not isinstance(observation_id, str) or not observation_id:
            raise CatalogError("catalog_invalid", "observation insert returned no generated_id.")
        observation_ids.append(observation_id)
    return observation_ids


def record_manual_cell(
    state: StateManagementInterface,
    cell: ManualCell,
    *,
    staleness_window_hours: float | None = None,
) -> dict[str, Any]:
    """Write one caller-supplied reading as an ACCEPTED cell, with its source.

    Operator ruling rul_9e7a67ba (2026-09-22): models change daily, and anyone
    who gets better information must be able to update the catalog directly.
    The reading is kept as evidence exactly like a fetched one: its own
    refresh-run row (trigger=manual) and one observation per metric whose
    fetch_method names the caller's source. It overwrites any prior value for
    the same (runtime, model, effort), whatever that cell's acceptance was.
    A later refresh whose live sources disagree with it leaves the manual
    values served and writes a loud `manual versus live disagreement` note (and the
    run result's `manual_conflicts`), so a bad manual reading surfaces without
    excluding a model an operator deliberately recorded. When the live sources
    agree with it, the refresh renews the cell and its window follows the current policy or observed catalog.
    """
    _validate_manual_cell(cell)
    window = resolve_catalog_window(state, read_cells(state), requested=staleness_window_hours)
    staleness_window_hours = window.cell_hours
    key = (cell.runtime, cell.model, cell.effort)
    previous = _existing_cell_by_key(state, key)
    run_id = open_refresh_run(state, trigger="manual")
    recorded_at = _now_iso()
    observation_ids = _record_manual_observations(state, run_id, cell, recorded_at=recorded_at)
    record: dict[str, Any] = {
        "provider": _RUNTIME_PROVIDERS[cell.runtime],
        "runtime": cell.runtime,
        "model": cell.model,
        "effort": cell.effort,
        "capability_score": cell.capability_score,
        "cost_per_task_usd": cell.cost_per_task_usd,
        "relative_cost_multiplier": None,  # derived at read time by read_cells against the live price floor
        "currency": "USD",
        "effort_supported": True,
        "measured_at": recorded_at,
        "accepted_at": recorded_at,
        "acceptance": CELL_ACCEPTANCE_ACCEPTED,
        "agreeing_observation_ids": observation_ids,
        "disagreement_note": None,
        "staleness_window_hours": staleness_window_hours,
        "last_refresh_run_id": run_id,
    }
    _write_policy_cell(state, record)
    close_refresh_run(
        state,
        run_id,
        counts={"cells_accepted": 1, "cells_conflicted": 0, "cells_unchanged": 0},
        sources_ok=[_MANUAL_SOURCE_ID],
        note=f"record_model_capability_cell {cell.model}@{cell.effort}; source: {cell.source_ref}"[:_MAX_EXCERPT],
    )
    return {
        "run_id": run_id,
        "runtime": cell.runtime,
        "model": cell.model,
        "effort": cell.effort,
        "capability_score": cell.capability_score,
        "cost_per_task_usd": cell.cost_per_task_usd,
        "acceptance": CELL_ACCEPTANCE_ACCEPTED,
        "observation_ids": observation_ids,
        "freshness_window": asdict(window),
        "previous": None
        if previous is None
        else {
            "capability_score": previous.get("capability_score"),
            "cost_per_task_usd": previous.get("cost_per_task_usd"),
            "acceptance": previous.get("acceptance"),
            "measured_at": previous.get("measured_at"),
        },
    }


_LIFECYCLE_SOURCE_ID: Final[str] = "manual_lifecycle"
_WITHDRAW_VERB: Final[str] = "withdraw_model_capability_cell"
_REINSTATE_VERB: Final[str] = "reinstate_model_capability_cell"
HISTORY_DEFAULT_LIMIT: Final[int] = CATALOG_PAGE_SIZE
"""Observations per history page when the caller names no limit; the state layer's own page cap bounds it."""


@dataclass(frozen=True, slots=True)
class CellTransition:
    """One caller-requested move of a cell between `accepted` and `withdrawn`.

    ``effort`` ``None`` means every effort of the model on the runtime.
    ``expected_acceptance`` is the premise the caller read before asking: the
    move is refused, not forced, when a cell no longer holds it. ``actor`` is
    recorded as evidence (a coordination label, not a security control).
    """

    runtime: str
    model: str
    effort: str | None
    reason: str
    expected_acceptance: str
    actor: str


def _validate_transition_fields(request: CellTransition) -> None:
    if request.runtime not in _RUNTIME_PROVIDERS:
        raise CatalogError("parameter_invalid", f"runtime must be one of {sorted(_RUNTIME_PROVIDERS)}; got {request.runtime!r}.")
    if not request.model.strip():
        raise CatalogError("parameter_invalid", "model must be a non-blank canonical model id.")
    if request.effort is not None and not request.effort.strip():
        raise CatalogError("parameter_invalid", "effort must be non-blank when given; omit it to act on every effort of the model.")
    if not request.reason.strip():
        raise CatalogError("parameter_invalid", "reason is required: say why the cell is being moved, so the history explains it.")
    if len(request.reason) > _MAX_EXCERPT:
        raise CatalogError("parameter_invalid", f"reason must be at most {_MAX_EXCERPT} characters; got {len(request.reason)}.")
    if not request.actor.strip():
        raise CatalogError("parameter_invalid", "actor is required: name the session or role making the change.")


def _validate_transition(request: CellTransition, *, verb: str) -> None:
    _validate_transition_fields(request)
    if request.expected_acceptance not in CELL_ACCEPTANCES:
        raise CatalogError("parameter_invalid", f"expected_acceptance must be one of {sorted(CELL_ACCEPTANCES)}; got {request.expected_acceptance!r}.")
    already = request.expected_acceptance == CELL_ACCEPTANCE_WITHDRAWN
    if verb == _WITHDRAW_VERB and already:
        raise CatalogError("parameter_invalid", "expected_acceptance is withdrawn, so there is nothing to withdraw; the cell is already withdrawn.")
    if verb == _REINSTATE_VERB and not already:
        raise CatalogError("parameter_invalid", "reinstate reopens a withdrawn cell, so expected_acceptance must be withdrawn.")


def _transition_targets(state: StateManagementInterface, request: CellTransition) -> list[dict[str, Any]]:
    """The cells a transition acts on, or `cell_not_found` when there are none."""
    if request.effort is None:
        rows = read_cells(state, runtime=request.runtime, model=request.model)
    else:
        found = _existing_cell_by_key(state, (request.runtime, request.model, request.effort))
        rows = [] if found is None else [found]
    if not rows:
        named = f"{request.runtime}/{request.model}" + ("" if request.effort is None else f"@{request.effort}")
        raise CatalogError("cell_not_found", f"no catalog cell for {named}.")
    return rows


def _require_premise(rows: list[dict[str, Any]], request: CellTransition) -> None:
    """Refuse the whole request, before any write, when any target no longer holds the caller's premise."""
    drifted = [f"{row['effort']}={row.get('acceptance')}" for row in rows if row.get("acceptance") != request.expected_acceptance]
    if drifted:
        raise CatalogError(
            "stale_cell_state",
            f"expected acceptance {request.expected_acceptance!r} but the catalog now holds {', '.join(drifted)}; read the cell again and decide again.",
        )


def _require_retained_scores(rows: list[dict[str, Any]]) -> None:
    unscored = [str(row["effort"]) for row in rows if row.get("capability_score") is None]
    if unscored:
        raise CatalogError(
            "parameter_invalid",
            f"{', '.join(unscored)} has no retained capability_score, so there is nothing to reinstate; record the cell instead (record_model_capability_cell).",
        )


def _transition_updates(row: dict[str, Any], request: CellTransition, *, target: str, run_id: str, stamp: str) -> dict[str, Any]:
    """The columns a transition rewrites; the score, cost and measured_at are always kept."""
    if target == CELL_ACCEPTANCE_WITHDRAWN:
        note: str | None = f"withdrawn {stamp}: {request.reason} (by {request.actor})"
        accepted_at = row.get("accepted_at")
    else:
        note = None
        accepted_at = row.get("accepted_at") or stamp
    updates: dict[str, Any] = {
        "acceptance": target,
        "accepted_at": accepted_at,
        "disagreement_note": note,
        "last_refresh_run_id": run_id,
    }
    if target == CELL_ACCEPTANCE_WITHDRAWN:
        updates["harness_unverified_since"] = None  # a null clock marks a person's withdrawal: a refresh never reverses it (design 3.3)
    return updates


def _write_transition_observation(
    state: StateManagementInterface,
    run_id: str,
    row: dict[str, Any],
    request: CellTransition,
    *,
    verb: str,
    target: str,
    stamp: str,
) -> str:
    record: dict[str, Any] = {
        "refresh_run_id": run_id,
        "source_id": _LIFECYCLE_SOURCE_ID,
        "fetch_method": f"{verb} called by {request.actor}"[:_MAX_EXCERPT],
        "fetched_at": stamp,
        "provider": _RUNTIME_PROVIDERS[request.runtime],
        "runtime": request.runtime,
        "model": row["model"],
        "effort": row["effort"],
        "metric": ACCEPTANCE_CHANGE_METRIC,
        "value_number": None,
        "value_text": f"{row['acceptance']}->{target}: manual",
        "raw_excerpt": request.reason,
        "fetch_status": "ok",
    }
    data = require_completed(
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
        "record model capability acceptance change",
    )
    result = data.get("result")
    observation_id = result.get("generated_id") if isinstance(result, dict) else None
    if not isinstance(observation_id, str) or not observation_id:
        raise CatalogError("catalog_invalid", "observation insert returned no generated_id.")
    return observation_id


def _move_cell(
    state: StateManagementInterface,
    row: dict[str, Any],
    request: CellTransition,
    *,
    verb: str,
    target: str,
    run_id: str,
    stamp: str,
) -> dict[str, Any]:
    """One predicated update on id + acceptance + updated_at, then its evidence row.

    Zero rows affected means another writer moved the cell after it was read,
    so the premise is gone: `stale_cell_state`, nothing forced. The cell is
    written first and the observation second, so the history never records a
    move the cell does not hold; a fault between the two is loud, not hidden.
    """
    updated_at = row.get("updated_at")
    if updated_at is None:
        raise CatalogError("catalog_invalid", f"cell {row['id']} has no updated_at to guard the write with.")
    affected = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"id": row["id"], "acceptance": request.expected_acceptance, "updated_at": updated_at},
            },
            _transition_updates(row, request, target=target, run_id=run_id, stamp=stamp),
        ),
    )
    if affected == 0:
        raise CatalogError(
            "stale_cell_state",
            f"{row['runtime']}/{row['model']}@{row['effort']} changed after it was read and was not moved. Read the cell again and decide again.",
        )
    observation_id = _write_transition_observation(state, run_id, row, request, verb=verb, target=target, stamp=stamp)
    return {
        "effort": row["effort"],
        "from_acceptance": row["acceptance"],
        "to_acceptance": target,
        "capability_score": row.get("capability_score"),
        "cost_per_task_usd": row.get("cost_per_task_usd"),
        "measured_at": row.get("measured_at"),
        "observation_id": observation_id,
    }


def _transition_cells(
    state: StateManagementInterface,
    request: CellTransition,
    *,
    verb: str,
    target: str,
    now: datetime,
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate, read, check the premise, then move every target under one manual run row."""
    _validate_transition(request, verb=verb)
    rows = _transition_targets(state, request)
    _require_premise(rows, request)
    if target == CELL_ACCEPTANCE_ACCEPTED:
        _require_retained_scores(rows)
    run_id = open_refresh_run(state, trigger="manual")
    stamp = now.isoformat()
    moved: list[dict[str, Any]] = []
    try:
        for row in rows:
            moved.append(_move_cell(state, row, request, verb=verb, target=target, run_id=run_id, stamp=stamp))
    except CatalogError as exc:
        done = [str(cell["effort"]) for cell in moved]
        left = [str(row["effort"]) for row in rows[len(moved) :]]
        detail = f"{exc.message} This request had already moved {done or 'no effort'}; it did not move {left}. The moved efforts stay moved: reverse them with the opposite verb if that is wanted."
        close_refresh_run(
            state,
            run_id,
            counts={"cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": 0},
            sources_ok=[],
            note=f"{verb} stopped on {exc.code}; moved {done}, not moved {left}: {exc.message}"[:_MAX_EXCERPT],
        )
        raise CatalogError(exc.code, detail) from exc
    close_refresh_run(
        state,
        run_id,
        counts={"cells_accepted": len(moved) if target == CELL_ACCEPTANCE_ACCEPTED else 0, "cells_conflicted": 0, "cells_unchanged": 0},
        sources_ok=[_LIFECYCLE_SOURCE_ID],
        note=f"{verb} {request.runtime}/{request.model} {[cell['effort'] for cell in moved]} by {request.actor}: {request.reason}"[:_MAX_EXCERPT],
    )
    return run_id, rows, moved


def withdraw_cells(state: StateManagementInterface, request: CellTransition, *, now: datetime | None = None) -> dict[str, Any]:
    """Take a cell (or every effort of a model) out of dispatch, keeping its score.

    Operator ruling rul_2bff72b8 (full lifecycle verbs): the retire half of the
    cell lifecycle. A withdrawn row keeps its score, cost and measured_at, so
    it still reaches the selector and is excluded there as `not_accepted`.
    `reinstate_cells` is the reversal.
    """
    run_id, _rows, moved = _transition_cells(
        state,
        request,
        verb=_WITHDRAW_VERB,
        target=CELL_ACCEPTANCE_WITHDRAWN,
        now=now or datetime.now(UTC),
    )
    return {
        "run_id": run_id,
        "runtime": request.runtime,
        "model": request.model,
        "acceptance": CELL_ACCEPTANCE_WITHDRAWN,
        "cells": moved,
        "reason": request.reason,
        "actor": request.actor,
        "reversal": _REINSTATE_VERB,
    }


def reinstate_cells(state: StateManagementInterface, request: CellTransition, *, now: datetime | None = None) -> dict[str, Any]:
    """Reopen a withdrawn cell (or every effort of a model) as accepted with its retained values.

    Refused when a target has no retained score: there is then nothing to
    reopen and the cell must be recorded instead. The answer says per cell
    whether the next refresh would withdraw it again.
    """
    moment = now or datetime.now(UTC)
    run_id, rows, moved = _transition_cells(state, request, verb=_REINSTATE_VERB, target=CELL_ACCEPTANCE_ACCEPTED, now=moment)
    for cell, row in zip(moved, rows, strict=True):
        cell["refresh_outlook"] = refresh_outlook(row, now=moment)
    return {
        "run_id": run_id,
        "runtime": request.runtime,
        "model": request.model,
        "acceptance": CELL_ACCEPTANCE_ACCEPTED,
        "cells": moved,
        "reason": request.reason,
        "actor": request.actor,
        "reversal": _WITHDRAW_VERB,
    }


def _history_provenance(state: StateManagementInterface, current: dict[str, Any] | None) -> str | None:
    from .model_cell_enablement import decision, read_policy

    return None if current is None else decision(read_policy(state), current)[1]


def read_cell_history(
    state: StateManagementInterface,
    *,
    runtime: str,
    model: str,
    effort: str,
    limit: int = HISTORY_DEFAULT_LIMIT,
    after: list[object] | None = None,
) -> dict[str, Any]:
    """One page of a cell's observations by `fetched_at`, oldest first, with the cell as it stands.

    `after` is the `next_after` cursor of the previous page. `next_after` is
    null when the page was short, which means the history is exhausted; a full
    page may be followed by an empty one.
    """
    if runtime not in _RUNTIME_PROVIDERS:
        raise CatalogError("parameter_invalid", f"runtime must be one of {sorted(_RUNTIME_PROVIDERS)}; got {runtime!r}.")
    if not model.strip() or not effort.strip():
        raise CatalogError("parameter_invalid", "model and effort are required and must be non-blank.")
    if not 1 <= limit <= CATALOG_PAGE_SIZE:
        raise CatalogError("parameter_invalid", f"limit must be between 1 and {CATALOG_PAGE_SIZE}; got {limit}.")
    query: dict[str, object] = {
        "table": TABLE_MODEL_CAPABILITY_OBSERVATION,
        "filters": {"runtime": runtime, "model": model, "effort": effort},
        "order_by": [["fetched_at", "asc"], ["id", "asc"]],
        "limit": limit,
    }
    if after is not None:
        query["after"] = after
    page = [dict(row) for row in require_records(state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query))]
    current = _existing_cell_by_key(state, (runtime, model, effort))
    provenance = _history_provenance(state, current)
    return {
        "enabled_provenance": provenance,
        "runtime": runtime,
        "model": model,
        "effort": effort,
        "current": None
        if current is None
        else {
            key: current.get(key)
            for key in (
                "acceptance",
                "capability_score",
                "cost_per_task_usd",
                "measured_at",
                "accepted_at",
                "disagreement_note",
                "staleness_window_hours",
                "last_refresh_run_id",
                "updated_at",
                "enabled",
            )
        },
        "observations": page,
        "next_after": [page[-1]["fetched_at"], page[-1]["id"]] if len(page) == limit else None,
    }


def _optional_float(raw: object) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise CatalogError("catalog_invalid", f"numeric catalog column holds {raw!r}.")
    return float(raw)


def _enabled(row: dict[str, Any]) -> bool:
    value = row.get("enabled", True)
    if not isinstance(value, bool):
        raise CatalogError("catalog_invalid", "enabled must be a boolean.")
    return value


def selection_catalog(
    rows: list[dict[str, Any]],
    weight_for: Callable[[str, str], float] | None = None,
) -> tuple[CapabilityCell, ...]:
    """Every scored cell as the pure selector wants it; unscored rows are dropped here, counted by the verb.

    ``weight_for(runtime, model)`` is the plan's declared dispatch weight; without it every cell weighs 1.0.
    """
    cells: list[CapabilityCell] = []
    for row in rows:
        score = _optional_float(row.get("capability_score"))
        if score is None:
            continue
        measured_raw = row.get("measured_at")
        measured_at = _parse_stamp(measured_raw, "measured_at") if measured_raw else datetime.min.replace(tzinfo=UTC)
        runtime = str(row.get("runtime", ""))
        model = str(row.get("model", ""))
        cells.append(
            CapabilityCell(
                runtime=runtime,
                model=model,
                effort=str(row.get("effort", "")),
                capability_score=score,
                cost_per_task_usd=_optional_float(row.get("cost_per_task_usd")),
                relative_cost_multiplier=_optional_float(row.get("relative_cost_multiplier")),
                measured_at=measured_at,
                acceptance=str(row.get("acceptance", "")),
                plan_weight=1.0 if weight_for is None else weight_for(runtime, model),
                enabled=_enabled(row),
            ),
        )
    return tuple(cells)


def resolve_catalog_window(state: StateManagementInterface, cells: Sequence[dict[str, Any]], *, requested: object = None) -> CatalogWindow:
    """Read governing policy per use, leaving pure annotation to freshness helpers."""
    from .model_catalog_policy import read_catalog_policy

    if requested is not None:
        return resolve_freshness_window(cells, requested=requested)
    policy = read_catalog_policy(state, CATALOG_POLICY_ID)
    if policy is None:
        return resolve_freshness_window(cells, policy_status="unconfigured")
    return resolve_freshness_window(cells, policy_hours=policy.staleness_window_hours, policy_status=policy.provenance.lifecycle_state, policy_revision_id=policy.provenance.revision_id)


def default_billing_objective(runtime: str | None, *, now: datetime, path: Path = USAGE_ECONOMICS_PATH) -> BillingObjective:
    """`relative` when a current flat-rate plan covers the runtime, else `metered_usd`.

    Derived from the declared economics profiles rather than assumed: a solet
    on metered billing gets dollars by default, and only a declared
    subscription turns that into a quota proxy. With no runtime named, any
    current flat-rate plan selects `relative`.
    """
    try:
        catalog = load_usage_economics_profile_catalog(path, as_of=now)
    except UsageEconomicsProfileValidationError as exc:
        raise CatalogError("usage_economics_invalid", f"usage economics profiles unreadable: {exc}") from exc
    for profile in catalog.profiles:
        if not isinstance(profile, FlatRateQuotaProfile) or profile.refresh_status != "current":
            continue
        if runtime is None or runtime in profile.included_runtimes:
            return "relative"
    return "metered_usd"


__all__ = [
    "CATALOG_POLICY_ID",
    "CatalogWindow",
    "resolve_catalog_window",
    "KNOWN_RUNTIMES",
    "CATALOG_PAGE_SIZE",
    "CATALOG_READ_CEILING",
    "HISTORY_DEFAULT_LIMIT",
    "SEED_PATH",
    "CatalogError",
    "CellRead",
    "CellTransition",
    "ManualCell",
    "SeedCell",
    "SeedTable",
    "catalog_staleness_window",
    "close_refresh_run",
    "default_billing_objective",
    "load_seed_table",
    "open_refresh_run",
    "read_cell_history",
    "read_cells",
    "read_cells_page",
    "reconcile_discovery",
    "record_manual_cell",
    "reinstate_cells",
    "seed_catalog",
    "selection_catalog",
    "withdraw_cells",
]
