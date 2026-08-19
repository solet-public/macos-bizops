"""GAU-15 — read a session's gauge SERIES and classify what it means.

`session_context_status` answers "what is this session's occupancy now".
This module answers the question that one row structurally cannot: **has this
gauge stopped, or was nobody ever driving the session?** Those are different
facts with different fixes, and until 2026-08-19 they reached a steward as the
same single alarm — measured that night across four live lanes, two of which
were merely idle and two of which were healthy.

The classification is deliberately built from TWO independent clocks that are
written by TWO different hooks on the same completed tool call:

* the gauge series (`session_context_status_history`, this plugin's store), and
* the lifecycle row's last `report_alive`, via
  `session_sweep.last_report_alive` — the ONE copy of that identity, imported
  rather than re-derived, because two copies of a liveness rule drift silently.

Reading them together is what separates the states. A session whose gauge
stalled while `report_alive` kept landing is WORKING AND GAUGE-DARK: the GAU-01
class. A session where both stalled together is nobody's incident — it is idle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .context_status_verbs import VerbError, resolve_status_row
from .session_context_status_store import (
    GAUGE_HISTORY_RETENTION,
    AmbiguousAgentSessionIdError,
    read_session_context_status_history,
)
from .session_lifecycle_store import SessionNotFoundError, read_managed_session
from .session_sweep import last_report_alive

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

SERIES_HEALTHY = "healthy"
SERIES_STOPPED = "stopped"
SERIES_IDLE = "idle"
SERIES_NEVER_STARTED = "never_started"
SERIES_UNDETERMINED = "undetermined"

SERIES_STATES = (
    SERIES_HEALTHY,
    SERIES_STOPPED,
    SERIES_IDLE,
    SERIES_NEVER_STARTED,
    SERIES_UNDETERMINED,
)

GAUGE_SERIES_STALL_S = 900.0
"""How long a series may go without a new row before it counts as STALLED.

Deliberately the same number as ``session_sweep.GAUGE_STALE_LAG_S``, and
deliberately not imported from it: these are two independent judgements that
happen to agree today (a detector's alarm threshold, and a reader's "is this
series still moving"). Binding them together would make a future tightening of
one silently retune the other. DERIVATION, NOT MEASUREMENT: 7.5x the reporting
hook's 120s throttle. A live control on 2026-08-18 measured healthy lags up to
+178.8s, which is the only reason this is not smaller -- the tidy argument for
a tighter bound has already been tried and measured false.
"""


def _parse(value: object) -> datetime | None:
    """An ISO timestamp as an AWARE datetime, or ``None`` if unreadable.

    Naive input is READ AS UTC rather than rejected: the DATETIME columns this
    module reads drop their offset on the way in, so every stored stamp comes
    back naive, and refusing them would make this classifier permanently
    undetermined against its own store.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _lifecycle_tick(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[datetime | None, bool]:
    """``(last report_alive, lifecycle row was readable)``.

    The bool matters on its own: "no lifecycle row" and "a lifecycle row that
    carries no report_by window" both yield ``None`` for the timestamp and are
    NOT the same finding, and a classifier that collapsed them would report a
    session it could not find as one that never ticked.
    """
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError:
        return (None, False)
    return (last_report_alive(row), True)


def classify_gauge_series(
    *,
    newest_recorded_at: datetime | None,
    last_alive: datetime | None,
    lifecycle_readable: bool,
    now: datetime,
) -> tuple[str, str]:
    """``(state, why)`` for one session's gauge series.

    THE TRUTH TABLE, and every branch names the evidence it used rather than
    the conclusion it prefers:

    * no series at all -> NEVER_STARTED. Nothing has ever been written for this
      session; whether that is a fault depends on whether it was ever driven,
      which the caller can see in ``last_alive``.
    * series moving inside the stall bound -> HEALTHY.
    * series stalled, ``report_alive`` still advancing -> STOPPED. The session
      completes tool calls and its gauge does not. This is GAU-01.
    * series stalled, ``report_alive`` stalled with it -> IDLE. Nobody is
      driving this session. A normal fleet state and NOT an incident -- the
      alarm that treats it as one is what trains stewards to ignore alarms.
    * anything unreadable -> UNDETERMINED, said plainly. A classifier that
      guesses here is worse than one that abstains, because its guess is
      indistinguishable from a measurement.
    """
    if newest_recorded_at is None:
        return (
            SERIES_NEVER_STARTED,
            "no history row has ever been written for this session",
        )
    series_age_s = (now - newest_recorded_at).total_seconds()
    if series_age_s <= GAUGE_SERIES_STALL_S:
        return (
            SERIES_HEALTHY,
            f"the newest history row is {series_age_s:,.0f}s old, inside the "
            f"{GAUGE_SERIES_STALL_S:,.0f}s stall bound",
        )
    if not lifecycle_readable:
        return (
            SERIES_UNDETERMINED,
            f"the series has been stalled {series_age_s:,.0f}s and no "
            "managed_session row could be read, so whether this session is "
            "still working is not established here",
        )
    if last_alive is None:
        return (
            SERIES_UNDETERMINED,
            f"the series has been stalled {series_age_s:,.0f}s and the "
            "lifecycle row carries no report_by window, so its last "
            "report_alive is not derivable -- absence of the WINDOW is not "
            "evidence of absence of a TICK",
        )
    alive_age_s = (now - last_alive).total_seconds()
    if alive_age_s <= GAUGE_SERIES_STALL_S:
        return (
            SERIES_STOPPED,
            f"the series has been stalled {series_age_s:,.0f}s while "
            f"report_alive landed {alive_age_s:,.0f}s ago -- this session works "
            "and its gauge is dark",
        )
    return (
        SERIES_IDLE,
        f"the series has been stalled {series_age_s:,.0f}s and report_alive "
        f"is {alive_age_s:,.0f}s old -- both clocks stopped together, so "
        "nobody is driving this session; it is not faulty",
    )


def _entry(row: dict[str, Any]) -> dict[str, Any]:
    """One history row as the verb publishes it.

    A PROJECTION, not the raw row: the stored record also carries standardizer
    bookkeeping (``id``, ``namespace``, ``is_deleted``, ...) that no consumer
    should learn to depend on. Every published field is copied as stored --
    nothing is defaulted, and a NULL stays NULL, because on this table a NULL
    is a positive finding about the REPORTER rather than a missing value.
    """
    return {
        "recorded_at": str(row.get("recorded_at") or ""),
        "measured_at": str(row.get("measured_at") or ""),
        "reading_at": row.get("reading_at"),
        "current_tokens": row.get("current_tokens"),
        "ceiling": row.get("ceiling"),
        "model": str(row.get("model") or ""),
        "claude_session_id": str(row.get("claude_session_id") or ""),
        "agent_session_id": row.get("agent_session_id"),
        "cache_read_tokens": row.get("cache_read_tokens"),
        "cache_cold": row.get("cache_cold"),
        "cache_overage_signature": row.get("cache_overage_signature"),
        "reporter_surface": row.get("reporter_surface"),
        "reporter_generation": row.get("reporter_generation"),
    }


def _rotations(entries: list[dict[str, Any]]) -> int:
    """How many `/clear` boundaries this page spans.

    A ROTATION IS A SPLICE, not a gap: the instance id survives a `/clear` and
    the Claude session id does not, so consecutive rows carrying different
    `claude_session_id` values are two contexts' readings in one series.
    Counting them is what stops a reader treating the 487,777 -> 253,247 step
    of the original GAU-01 incident as a measurement rather than a new session.
    """
    seen = [e["claude_session_id"] for e in entries if e["claude_session_id"]]
    return sum(1 for a, b in zip(seen, seen[1:], strict=False) if a != b)


def _series_identity(
    state: StateManagementInterface, agent_instance_id: str,
) -> tuple[str, str]:
    """``(the id the series is keyed on, how it was reached)``.

    Delegates to ``resolve_status_row`` -- the ONE copy of the GAU-07 join --
    and falls back to the id as given when no cache row resolves, so a session
    that has a series and somehow no cache row is still readable rather than
    silently empty.
    """
    try:
        row, id_resolution = resolve_status_row(state, agent_instance_id)
    except AmbiguousAgentSessionIdError as exc:
        raise VerbError("ambiguous_agent_session_id", str(exc)) from exc
    keyed_id = str(row.get("agent_instance_id") or "") if row else ""
    return (keyed_id or agent_instance_id, id_resolution)


def _gap_reason(agent_instance_id: str) -> str:
    """The honest-gap sentence, which must say WHICH lookups were tried.

    "No series on file" alone would leave a reader unable to tell a genuine
    reporting gap from the GAU-07 lookup miss that made healthy watcher-held
    sessions read as gauge-less for a day.
    """
    return (
        f"no session_context_status history on file for {agent_instance_id!r}. "
        "This id was ALSO resolved through the peer binding (the watch-id "
        "join) and still matched no series."
    )


def session_context_status_history(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    limit: int = GAUGE_HISTORY_RETENTION,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The bounded gauge series for one session, newest first, classified.

    ``resolved=False`` is the honest-gap shape, never a raised error, and it
    carries the SAME key set as a resolved read so a caller cannot ``KeyError``
    its way through a legitimate gap -- the same contract
    ``session_context_status`` already holds.

    ``truncated`` is published rather than implied. A reader asking "when did
    this stop" is asking about the OLDEST row it can see, and a silently capped
    page answers that with a boundary the reader picked by accident.
    """
    if not agent_instance_id.strip():
        raise VerbError(
            "missing_argument",
            "session_context_status_history requires a non-empty agent_instance_id.",
        )
    clock = now or datetime.now(UTC)
    series_id, id_resolution = _series_identity(state, agent_instance_id)
    rows, truncated = read_session_context_status_history(state, series_id, limit=limit)
    entries = [_entry(r) for r in rows]
    newest = _parse(entries[0]["recorded_at"]) if entries else None
    last_alive, lifecycle_readable = _lifecycle_tick(state, series_id)
    series_state, why = classify_gauge_series(
        newest_recorded_at=newest,
        last_alive=last_alive,
        lifecycle_readable=lifecycle_readable,
        now=clock,
    )
    return {
        "resolved": bool(entries),
        "resolution_error": _gap_reason(agent_instance_id) if not entries else None,
        "agent_instance_id": series_id,
        "queried_agent_instance_id": agent_instance_id,
        "id_resolution": id_resolution,
        "series_state": series_state,
        "series_state_reason": why,
        "last_report_alive": last_alive.isoformat() if last_alive else None,
        "entries": entries,
        "returned": len(entries),
        "truncated": truncated,
        "retention": GAUGE_HISTORY_RETENTION,
        "rotation_boundaries": _rotations(entries),
    }


__all__ = [
    "GAUGE_SERIES_STALL_S",
    "SERIES_HEALTHY",
    "SERIES_IDLE",
    "SERIES_NEVER_STARTED",
    "SERIES_STATES",
    "SERIES_STOPPED",
    "SERIES_UNDETERMINED",
    "classify_gauge_series",
    "session_context_status_history",
]
