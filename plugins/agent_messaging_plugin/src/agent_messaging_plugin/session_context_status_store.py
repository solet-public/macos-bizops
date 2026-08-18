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


def _as_flag(value: bool | None) -> int | None:
    """``True``/``False`` -> 1/0, and ``None`` STAYS ``None``.

    Deliberately not ``int(bool(value))``: that maps ``None`` to 0, collapsing
    "not reported" into "cache is warm" — the exact distinction these columns
    exist to preserve.
    """
    return None if value is None else int(value)


def upsert_session_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    model: str,
    current_tokens: int,
    ceiling: int,
    measured_at: str,
    cache_read_tokens: int | None = None,
    cache_cold: bool | None = None,
    cache_overage_signature: bool | None = None,
    reporter_surface: str | None = None,
    reporter_generation: int | None = None,
    agent_session_id: str | None = None,
) -> None:
    """Overwrite the single latest snapshot for `agent_instance_id`. Conflicts
    on `agent_instance_id` alone (unlike `session_claude_mapping`'s
    per-firing history triple) — this table is a cache, not a log.

    The three cache fields default to ``None`` = NOT REPORTED, which is a
    third state and never a synonym for "warm". A reporter predating the
    2026-08-16 cache-state widening sends none of them, and a reader must be
    able to tell that apart from a reporter that looked and found the cache
    live — otherwise every un-upgraded hook silently asserts a warm cache.

    ``reporter_surface``/``reporter_generation`` say WHICH COPY of the
    reporting hook produced this row, because more than one copy can be
    registered on the same event at once. Latest write wins here — that is
    what "a cache, not a log" means — so without them a row cannot be
    attributed to a reporter at all, and a field missing because a STALE
    copy served the tick is indistinguishable from one missing because the
    verbs are undeployed. Both default to ``None`` = pre-attribution
    reporter, which is itself a positive finding about that row.

    ``agent_session_id`` is the ROUTING JOIN (2026-08-18). This table keys on
    the reporting session's LEDGER id, while a watcher-held worker's live
    binding keys on its WATCH id, so a consumer holding a row here could not
    find that session's bridge at all. Storing the stable session id lets
    ``peer_registry.resolve_by_agent_session_id`` reverse-resolve the binding
    whichever id that binding is keyed on. ``None`` = NOT REPORTED (a reporter
    predating the column), and a caller must not read it as `this session has
    no bridge` -- the same tri-state discipline ``_as_flag`` enforces above,
    for the same reason: those are different facts with different fixes.
    """
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
                    "cache_read_tokens": cache_read_tokens,
                    "cache_cold": _as_flag(cache_cold),
                    "cache_overage_signature": _as_flag(cache_overage_signature),
                    "reporter_surface": reporter_surface,
                    "reporter_generation": reporter_generation,
                    "agent_session_id": agent_session_id,
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


def list_session_context_statuses(
    state: StateManagementInterface,
) -> list[dict[str, Any]]:
    """Every live gauge snapshot, one row per `agent_instance_id`.

    THE SCAN THE L4c SELF-NOTICE LEG NEEDS, and the reason it can reach a seat
    at all. Its two sibling legs enumerate `managed_session` and then look the
    gauge up per row, which structurally excludes every `host=operator` session
    -- a ledger a seat is never in. This table has no FK to that ledger (see
    the module docstring), so scanning it directly is the only enumeration that
    includes the sessions whose rotation decision is the expensive one.

    Returns rows as stored, in whatever order the state layer yields them: the
    caller decides what counts as notable, exactly as
    `_rotation_due_row` does for the managed-session path. No filtering happens
    here beyond `is_deleted`, so a future consumer asking a different question
    of the same rows does not have to defeat this function's opinion first.
    """
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_CONTEXT_STATUS,
            "filters": {_COL_IS_DELETED: 0},
        },
    )
    return require_records(result)


__all__ = [
    "list_session_context_statuses",
    "read_session_context_status",
    "upsert_session_context_status",
]
