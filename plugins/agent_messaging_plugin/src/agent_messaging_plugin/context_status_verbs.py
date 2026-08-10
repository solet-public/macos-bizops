"""maintenance-verbs M1 (workbench
2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3; coordinator-seat ruling
on Q3, 2026-08-09) — `report_context_status` / `session_context_status`.

Shape (a), ratified: a hook (`rotation_due_watch.py`) already computes
current-context-window occupancy client-side every tick; `report_context_status`
is a plain state upsert of that measurement (no file/subprocess I/O in this
handler — the file read already happened in the caller, sanctioned ms-scale
state work per D0.3 §1), and `session_context_status` is a trivial state read
of the cached row. Neither verb resolves a transcript path or reads a file
itself; both are pure over `session_context_status_store`.

Fraction / rotation-due / per-prompt-carriage are derived at READ time from
the live `rotation_thresholds` constants, never stored — a future change to
`ROTATION_THRESHOLD_FRACTION` must never require a backfill of stored rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import rotation_thresholds
from .session_context_status_store import read_session_context_status, upsert_session_context_status
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

# The per-prompt carriage-cost heuristic named in the operator's 2026-08-09
# ruling (feedback_rotation_economics_and_context_gauge): a cached-context
# read bills roughly this fraction of base input, EVERY turn, before any new
# work. A declared constant, not a guess dressed as one — matches the
# operator's own "doesn't 800k context result in 80k tokens used per prompt?"
# arithmetic exactly (800_000 * 0.1 = 80_000).
CACHE_READ_COST_FRACTION = 0.1


def report_context_status(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    claude_session_id: str,
    model: str,
    current_tokens: int,
    ceiling: int,
    measured_at: str,
) -> dict[str, Any]:
    """Overwrite the caller's own latest context-status snapshot. The caller
    (today: `rotation_due_watch.py`, extended) already did the local
    transcript read and the `rotation_thresholds.resolve_ceiling` lookup —
    this verb trusts the reported `ceiling`/`current_tokens` rather than
    recomputing them, so it never touches a file itself.

    Errors: `missing_argument` (any of the five required fields empty/absent
    — fast-fail before any write), `negative_tokens` (a reported value that
    cannot be a real token count, catching a caller bug loud rather than
    silently caching garbage)."""
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "report_context_status requires a non-empty agent_instance_id.",
        )
    if not claude_session_id.strip():
        raise VerbError(
            "missing_argument", "report_context_status requires a non-empty claude_session_id.",
        )
    if not model.strip():
        raise VerbError("missing_argument", "report_context_status requires a non-empty model.")
    if not measured_at.strip():
        raise VerbError(
            "missing_argument", "report_context_status requires a non-empty measured_at.",
        )
    if current_tokens < 0 or ceiling <= 0:
        raise VerbError(
            "negative_tokens",
            f"report_context_status got current_tokens={current_tokens!r}, ceiling={ceiling!r} "
            "— neither can be non-positive for a real measurement.",
        )
    upsert_session_context_status(
        state,
        agent_instance_id=agent_instance_id,
        claude_session_id=claude_session_id,
        model=model,
        current_tokens=current_tokens,
        ceiling=ceiling,
        measured_at=measured_at,
    )
    return {"status": "recorded"}


def session_context_status(
    state: StateManagementInterface, *, agent_instance_id: str,
) -> dict[str, Any]:
    """Read the cached snapshot for `agent_instance_id`. `resolved=False`
    (never a raised `VerbError`) is the expected, stable shape for "no report
    has landed yet for this session" — e.g. a fresh session pre-first-tick,
    or (until the seat-wiring design note is acted on) any `host=operator`
    seat. Callers must treat `resolved=False` as a loud, honest gap, never
    estimate a number in its place (the standing repo rule against silently
    promoting an unknown into a fact)."""
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument", "session_context_status requires a non-empty agent_instance_id.",
        )
    row = read_session_context_status(state, agent_instance_id)
    if row is None:
        return {
            "resolved": False,
            "resolution_error": (
                f"no session_context_status report on file for {agent_instance_id!r} — "
                "either this session has not completed a reporting tick yet, or "
                "(for host=operator sessions) the seat-wiring design note has not "
                "been acted on yet."
            ),
            "agent_instance_id": agent_instance_id,
            "claude_session_id": "",
            "model": "",
            "current_tokens": 0,
            "ceiling": 0,
            "fraction": 0.0,
            "per_prompt_carriage_estimate_tokens": 0,
            "rotation_due": False,
            "measured_at": "",
        }
    current_tokens = int(row.get("current_tokens") or 0)
    ceiling = int(row.get("ceiling") or 0) or rotation_thresholds.DEFAULT_CONSERVATIVE_CEILING
    fraction = current_tokens / ceiling if ceiling else 0.0
    return {
        "resolved": True,
        "resolution_error": None,
        "agent_instance_id": agent_instance_id,
        "claude_session_id": str(row.get("claude_session_id") or ""),
        "model": str(row.get("model") or ""),
        "current_tokens": current_tokens,
        "ceiling": ceiling,
        "fraction": fraction,
        "per_prompt_carriage_estimate_tokens": round(current_tokens * CACHE_READ_COST_FRACTION),
        "rotation_due": fraction >= rotation_thresholds.ROTATION_THRESHOLD_FRACTION,
        "measured_at": str(row.get("measured_at") or ""),
    }


__all__ = [
    "CACHE_READ_COST_FRACTION",
    "report_context_status",
    "session_context_status",
]
