"""State transitions that preserve previously accepted model capability values."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_updated

from .model_capability_fetch import SOURCE_API, SOURCE_EMBEDDED, DiscoveredCell
from .model_capability_reconcile import ReconcileOutcome, RunTally, flag_new_cell, key_text, manual_disagreement_note, prior_value
from .schema import (
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_CROSSCHECK_CONFLICT,
    CELL_ACCEPTANCE_PENDING_CROSSCHECK,
    CELL_ACCEPTANCE_WITHDRAWN,
    TABLE_MODEL_CAPABILITY_CELL,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


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


def _hold_api_page_conflict(
    state: StateManagementInterface,
    cell: DiscoveredCell,
    outcome: ReconcileOutcome,
    existing: dict[str, Any] | None,
    run_id: str,
    tally: RunTally,
) -> bool:
    if existing is None or existing.get("acceptance") != CELL_ACCEPTANCE_ACCEPTED:
        return False
    note = outcome.disagreement_note
    if outcome.acceptance != CELL_ACCEPTANCE_CROSSCHECK_CONFLICT or note is None:
        return False
    if SOURCE_API not in note or SOURCE_EMBEDDED not in note:
        return False
    require_completed(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_CELL, "filters": {"id": existing["id"], "acceptance": CELL_ACCEPTANCE_ACCEPTED}},
            {"disagreement_note": note, "last_refresh_run_id": run_id},
        ),
        "record API/page conflict while retaining accepted values",
    )
    tally.held_conflicts.append(f"API/page disagreement for {key_text(cell.key)}: prior accepted score, cost and measured_at retained; {note}")
    return True


def _hold_manual_value(state: StateManagementInterface, cell: DiscoveredCell, outcome: ReconcileOutcome, existing: dict[str, Any] | None, run_id: str, tally: RunTally) -> bool:
    from . import model_capability_store as store

    """Q8: keep a manual cell's values when the live pages disagree, with a loud note; True when it did.

    The pages disagree with a manual reading in two ways, and both hold it: two agreeing pages
    whose value is past tolerance of it (an `accepted` outcome), and pages that disagree with
    each other (a `crosscheck_conflict` outcome). A conflict row would exclude a model an
    operator deliberately recorded until someone re-records it (rul_becb4bed); the note and
    `manual_conflicts` make the disagreement loud instead."""
    if existing is None or outcome.acceptance not in (CELL_ACCEPTANCE_ACCEPTED, CELL_ACCEPTANCE_CROSSCHECK_CONFLICT):
        return False
    prior = prior_value(existing)
    if prior is None or not store.manual_source_for_cell(state, existing):
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


def _write_settled_cell(
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
    from . import model_capability_store as store

    if outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED and (outcome.disagreement_note or "").startswith("single source:"):
        tally.single_source_notes.append(f"{key_text(cell.key)} renewed from {outcome.disagreement_note}")
    unverified_since: str | None = None
    if existing is None:
        outcome = flag_new_cell(outcome, cell)
        tally.created.append(key_text(cell.key))
        unverified_since = store._now_iso() if cell.derived_launch_id else None
    store.write_cell(state, outcome, run_id, staleness_window_hours=staleness_window_hours, floor=floor, unverified_since=unverified_since)
    return "accepted" if outcome.acceptance == CELL_ACCEPTANCE_ACCEPTED else "conflicted"


def settle_cell(
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
    is. An API/page conflict preserves a prior accepted cell and records an
    advisory. Legacy conflicts retain their transition; manual cells retain
    `_hold_manual_value`.
    A renewal resets the staleness window to
    `staleness_window_hours`, so a manual cell that live sources renew takes the current resolved window.
    A withdrawn cell is the exception: its acceptance never moves here (`_hold_withdrawn`)."""
    from . import model_capability_store as store

    if existing is not None and existing.get("acceptance") == CELL_ACCEPTANCE_WITHDRAWN:
        return _hold_withdrawn(state, cell, outcome, existing, run_id, tally)
    if outcome.single_source_unconfirmed:
        tally.unconfirmed.append(key_text(cell.key))
        return "unchanged"
    if outcome.acceptance == CELL_ACCEPTANCE_PENDING_CROSSCHECK or _hold_manual_value(state, cell, outcome, existing, run_id, tally):
        return "unchanged"
    if _hold_api_page_conflict(state, cell, outcome, existing, run_id, tally):
        return "conflicted"
    if outcome.acceptance not in (CELL_ACCEPTANCE_ACCEPTED, CELL_ACCEPTANCE_CROSSCHECK_CONFLICT):
        raise store.CatalogError("catalog_invalid", f"reconcile_cell produced an unexpected acceptance {outcome.acceptance!r}.")
    return _write_settled_cell(
        state,
        cell,
        outcome,
        existing,
        run_id,
        staleness_window_hours=staleness_window_hours,
        floor=floor,
        tally=tally,
    )
