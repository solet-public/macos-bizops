"""maintenance-verbs M1 (workbench
2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3, shape (a)) — the
state-layer primitives over `session_context_status` (schema.py). ONE row per
`agent_instance_id`, always overwritten by the latest report — the reporting
hook's own cadence is the freshness bound, this table just holds the newest
value. Same decoupled-from-managed_session posture as
`session_claude_mapping_store.py`: no FK, keyed on a bare `agent_instance_id`
string, so a `host=operator` row (never present in `managed_session`) is
still representable here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from .schema import TABLE_SESSION_CONTEXT_STATUS

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_IS_DELETED = "is_deleted"
_CONFLICT_COLUMNS = ["agent_instance_id"]


def upsert_session_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    model: str,
    current_tokens: int,
    ceiling: int,
    measured_at: str,
) -> None:
    """Overwrite the single latest snapshot for `agent_instance_id`. Conflicts
    on `agent_instance_id` alone (unlike `session_claude_mapping`'s
    per-firing history triple) — this table is a cache, not a log."""
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_CONTEXT_STATUS,
                "record": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    "claude_session_id": claude_session_id,
                    "model": model,
                    "current_tokens": current_tokens,
                    "ceiling": ceiling,
                    "measured_at": measured_at,
                },
                "conflict_columns": _CONFLICT_COLUMNS,
            },
        ),
        "upsert session_context_status",
    )


def read_session_context_status(
    state: StateManagementInterface, agent_instance_id: str,
) -> dict[str, Any] | None:
    """The latest snapshot row for `agent_instance_id`, or `None` when no
    report has ever landed for it (a fresh session pre-first-tick, or a
    `host=operator` seat this pass has not been wired to report yet — see
    the M0 design doc's seat-wiring open item). Never raises on absence;
    callers translate `None` into their own fail-loud contract."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CONTEXT_STATUS,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    return records[0] if records else None


__all__ = [
    "read_session_context_status",
    "upsert_session_context_status",
]
