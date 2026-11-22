"""Backfill session origins. Current completion combines a history checkpoint with live rows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_updated

from .schema import TABLE_MANAGED_SESSION
from .session_lifecycle_store import iter_managed_sessions, iter_managed_sessions_unbounded
from .session_limits import LIVE_STATES

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


SESSION_ORIGIN_MARKER = "managed_session_origin_backfill_v1_complete"
_HISTORY_SCANNED = "history_scanned"
_LEGACY_ORIGINS = {
    "worktree": "spawned",
    "synthetic_no_worktree": "spawned",
    "operator_existing_checkout": "registered",
    "synthetic_no_process": "synthetic",
}


def _backfill_row(state: StateManagementInterface, row: dict[str, Any]) -> int:
    previous = row["session_origin"]
    if previous not in (None, "unknown"):
        return 0
    if row.get("brief_ref") and row["assignment"] is None:
        origin = "historical_read_only"
    else:
        origin = _LEGACY_ORIGINS.get(row["provisioning_mode"])
    if origin is None:
        return 0
    filters = {"id": row["id"], "session_origin": {"op": "is_null"} if previous is None else previous}
    updated = require_updated(state.update_state(NAMESPACE, {"table": TABLE_MANAGED_SESSION, "filters": filters}, {"session_origin": origin}))
    if updated != 1:
        raise RuntimeError(f"Session origin backfill conflict on managed_session {row['id']!r}: expected one unknown origin, updated {updated}.")
    return updated


def _live_origin_status(state: StateManagementInterface) -> dict[str, object]:
    # The installed column is NOT NULL DEFAULT 'unknown'. Handle legacy NULLs
    # first so the final snapshot covers every new writer, including old binaries.
    for unknown in ({"op": "is_null"}, "unknown"):
        row = next(iter_managed_sessions(state, {"lifecycle_state": list(LIVE_STATES), "session_origin": unknown}), None)
        if row is not None:
            return {"status": "live_unknown", "row_id": row["id"]}
    return {"status": "completed"}


def _history_checkpoint(state: StateManagementInterface) -> object:
    data = require_completed(state.get_key_value(NAMESPACE, SESSION_ORIGIN_MARKER), "read session origin history checkpoint")
    return data["value"] if data["found"] else None


def _record_history_scan(state: StateManagementInterface) -> None:
    require_completed(state.set_key_value(NAMESPACE, SESSION_ORIGIN_MARKER, _HISTORY_SCANNED), "record session origin history scan")


def read_session_origin_completion(state: StateManagementInterface) -> dict[str, object]:
    """Read current readiness. A stored checkpoint (including the old True value) only attests to the history scan."""
    checkpoint = _history_checkpoint(state)
    if checkpoint is not True and checkpoint != _HISTORY_SCANNED:
        return {"status": "not_scanned"}
    return _live_origin_status(state)


def backfill_session_origins(state: StateManagementInterface) -> dict[str, object]:
    """Resume classification when a writer has created a live unknown since the last scan."""
    checkpoint = _history_checkpoint(state)
    if checkpoint is True:
        _record_history_scan(state)
        checkpoint = _HISTORY_SCANNED
    if checkpoint == _HISTORY_SCANNED and _live_origin_status(state)["status"] == "completed":
        return {"status": "already_done", "updated": 0}

    rows = iter_managed_sessions_unbounded(state, reason="One-time classification of existing sessions, including retired history.", include_deleted=True)
    updated = sum(_backfill_row(state, row) for row in rows)
    status = _live_origin_status(state)
    if status["status"] == "completed" and checkpoint != _HISTORY_SCANNED:
        _record_history_scan(state)
        status = read_session_origin_completion(state)
    return {**status, "updated": updated}
