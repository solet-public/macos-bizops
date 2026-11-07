"""Runtime-neutral fleet stall detection keyed on PRODUCED WORK, not liveness.

``seat_idle_watcher`` is the fleet's only other autonomous stall net, and it
is Claude-only and single-seat by construction: it globs
``~/.claude/sessions/*.json`` for ONE ``--role-tag``, requires a
``sessionId`` from that file, and keys its idle clock on that file saying
``idle``.  A Codex lane is invisible to it, a second seat is invisible to it,
and -- because the signal is liveness rather than output -- a fleet that is
alive and busy-looking while producing nothing is not a stall to it at all.
``iss_c4fbe548`` found that watcher dead for about 25 days; ``iss_ebc1ce9c``
records the whole fleet idle for 3 h 40 min with two ready queued Units while
nothing detected it and the Operator found it by hand.

This sweep answers the other question.  Its predicate is output, over a
window:

    STALLED  iff  work was ready and owned in the register
                  AND the actors that own it produced nothing in the window.

Both halves are register facts read through the existing read-only CLI port,
with that port's own paging and named-refusal conventions:

* **ready and owned** -- ``psolet db unit who-has-work`` rows whose
  ``coverage`` is ``active-owned``.  That is the register's own name for a
  live, owned, unexpired Unit someone is supposed to be moving, which is
  exactly the "work is available and owned" half of the predicate.  It is the
  right fact because it is the register's own answer to "who is supposed to
  be working", not a derived guess at readiness.
* **produced** -- ``psolet db activity produced-work --actor <actor>
  --since <iso>`` rows for each distinct owning actor.  Those rows are the
  register-recorded artifacts an actor actually created (Unit state events,
  read-backs, candidate revisions, landings, issue events).  It is the right
  fact because it is what the fleet *made*, which is the thing a stall is the
  absence of; an actor can look alive, hold a lease, and emit none of them.

Nothing here keys on a liveness signal, on ``~/.claude/sessions``, on
``claude_session_id``, or on a session count.  Sessions are enumerated
runtime-neutrally from the ``managed_session`` ledger through
:func:`session_lifecycle_store.list_managed_sessions` and carried into the
report with their ``agent_runtime``, so a Codex-only fleet is covered
identically to a Claude one -- but the verdict never depends on which runtime
is present, which is what makes the neutrality measurable rather than
asserted.

**This sweep REPORTS and nothing else.**  It never drives, clears, compacts,
terminates or spawns a session, and it imports no module that could.  Waking
a seat on a stall verdict would make a detector into an actor on live
sessions; that is a separate decision and a separate plan
(``prb_351c9641``, ``prb_4deb89fb``).

**This sweep REFUSES NOTHING** (``rul_becb4bed``).  Its one conditional
branch is the verdict, and when a read did not complete it emits an explicit
advisory and NO verdict -- neither stalled nor not-stalled.  An unreadable
register is never read as "nothing produced", because that would manufacture
a false stall out of a failure to measure.

**Its own death is visible.**  Every completed run -- including a run that
could only emit an advisory -- records
:data:`LAST_COMPLETED_RUN_KEY` in the shared ``key_value_store`` under this
plugin's namespace, and :func:`read_sweep_status` reads it back with the age
of that run and an explicit ``silent`` advisory once the age passes
:data:`SILENCE_THRESHOLD_SECONDS`.  A reader notices silence because the age
keeps growing while the verdict stops changing.  That is a pass condition,
not a nicety: the predecessor net was dead for 25 days unnoticed precisely
because nothing published its last run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.llm.agent_messaging.state_results import require_completed
from ananta.services.state_service.bounded_read import PagedReadError

from .register_unit_client import PsoletRegisterUnitClient, RegisterUnitError
from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
)
from .session_hosts import DEFAULT_AGENT_RUNTIME

STATE_NAMESPACE: Final[str] = "agent_messaging_plugin"
"""``key_value_store`` is one shared table across every plugin, so the
sweep's own run record is namespaced under this plugin's name rather than
left on an unqualified key that another plugin could collide with."""

LAST_COMPLETED_RUN_KEY: Final[str] = "fleet_stall_sweep:last_completed_run"
RUN_RECORD_SCHEMA: Final[str] = "fleet-stall-sweep-run/v1"

DEFAULT_WINDOW_SECONDS: Final[int] = 1800
"""Thirty minutes.  ``iss_ebc1ce9c``'s measured incident ran 3 h 40 min
before a human noticed; a window this size reports it on the first scheduled
run after the first half hour, and is long enough that an ordinary lane's gap
between two register writes does not read as a stall."""

DEFAULT_SCHEDULE_CRON: Final[str] = "*/15 * * * *"
DEFAULT_SCHEDULE_INTERVAL_SECONDS: Final[int] = 900
DEFAULT_SCHEDULE_TAG: Final[str] = "fleet_stall_sweep"
DEFAULT_SCHEDULE_LABEL: Final[str] = "Work-keyed fleet stall sweep"

SILENCE_THRESHOLD_SECONDS: Final[int] = 3 * DEFAULT_SCHEDULE_INTERVAL_SECONDS
"""Three missed fires.  One missed fire is a restart or a slow tick; three
consecutive misses is the sweep not running, which is the fact
``iss_c4fbe548`` went 25 days without anyone being able to see."""

VERDICT_STALLED: Final[str] = "stalled"
VERDICT_NOT_STALLED: Final[str] = "not_stalled"
VERDICT_UNMEASURED: Final[str] = "unmeasured"
"""No verdict was computed because a read did not complete.  Deliberately a
third value rather than a default to ``not_stalled``: a sweep that cannot
measure must say so, not report calm."""

COVERAGE_ACTIVE_OWNED: Final[str] = "active-owned"

ACTIVE_LIFECYCLE_STATES: Final[tuple[str, ...]] = (
    LIFECYCLE_SPAWNING,
    LIFECYCLE_LIVE,
    LIFECYCLE_IDLE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
)
"""Every non-terminal managed-session state, regardless of runtime.  Taken
whole rather than narrowed to ``live``: a parked or overdue Codex lane is
still part of the fleet this report describes."""

_PAGE_LIMIT: Final[int] = 100
_PAGE_CEILING: Final[int] = 50


class FleetStallSweepError(RuntimeError):
    """A malformed configured input the sweep cannot measure around."""


@dataclass(frozen=True, slots=True)
class FleetSession:
    """One managed session as the ledger records it, runtime carried through."""

    agent_instance_id: str
    agent_session_id: str
    agent_runtime: str
    lifecycle_state: str
    local_name: str

    def to_dict(self) -> dict[str, str]:
        return {
            "agent_instance_id": self.agent_instance_id,
            "agent_session_id": self.agent_session_id,
            "agent_runtime": self.agent_runtime,
            "lifecycle_state": self.lifecycle_state,
            "local_name": self.local_name,
        }


@dataclass(frozen=True, slots=True)
class ReadyOwnedUnit:
    """One register Unit that is live, owned and unexpired: work to be moved."""

    unit_id: str
    unit_key: str
    actor: str
    state: str

    def to_dict(self) -> dict[str, str]:
        return {"unit_id": self.unit_id, "unit_key": self.unit_key, "actor": self.actor, "state": self.state}


@dataclass(frozen=True, slots=True)
class ProducedWorkItem:
    """One register artifact an actor created inside the window."""

    actor: str
    event_kind: str
    event_id: str
    occurred_at: str
    unit_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "actor": self.actor,
            "event_kind": self.event_kind,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "unit_id": self.unit_id,
        }


class FleetStallRegisterReader(Protocol):
    """The two register reads the predicate needs, and nothing else."""

    def read_ready_owned_work(self) -> list[ReadyOwnedUnit]: ...
    def read_produced_work(self, actor: str, since: datetime) -> list[ProducedWorkItem]: ...


def _text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    return value if isinstance(value, str) else ""


def _rows(value: object, label: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise RegisterUnitError("unit_register_invalid", f"psolet {label} did not print a list.")
    return [row for row in value if isinstance(row, Mapping)]


def _cli_json(stdout: bytes, label: str) -> object:
    try:
        return json.loads(stdout)
    except (ValueError, UnicodeError) as exc:
        raise RegisterUnitError("unit_register_invalid", f"psolet {label} printed invalid JSON.") from exc


class PsoletFleetStallReader(PsoletRegisterUnitClient):
    """The configured register CLI's timeout-bounded boundary, plus the two
    fleet-wide reads this sweep keys on.

    Both listings are read to their end within the inherited bound; a listing
    that does not end, or prints anything but its documented shape, is a
    named refusal the caller turns into an advisory, because an incomplete
    read proves nothing.  Neither read writes anything.
    """

    def read_ready_owned_work(self) -> list[ReadyOwnedUnit]:
        """Every live, owned, unexpired Unit, following ``who-has-work``'s cursor."""
        units: list[ReadyOwnedUnit] = []
        cursor = ""
        for _ in range(_PAGE_CEILING):
            command = ("db", "unit", "who-has-work", "--limit", str(_PAGE_LIMIT))
            after = ("--after", cursor) if cursor else ()
            payload = _cli_json(self._run(*command, *after).stdout, "unit who-has-work")
            if not isinstance(payload, Mapping):
                raise RegisterUnitError("unit_register_invalid", "psolet unit who-has-work did not print an object.")
            units.extend(_ready_owned_rows(_rows(payload.get("rows"), "unit who-has-work")))
            if not payload.get("truncated"):
                return units
            cursor = _text(payload, "next_cursor")
            if not cursor:
                raise RegisterUnitError("unit_register_invalid", "Truncated who-has-work page has no continuation cursor.")
        raise RegisterUnitError("ready_owned_truncated", f"who-has-work exceeds {_PAGE_CEILING} pages.")

    def read_produced_work(self, actor: str, since: datetime) -> list[ProducedWorkItem]:
        """One actor's register-recorded artifacts since ``since``, in full."""
        command = ("db", "activity", "produced-work", "--actor", actor, "--since", _iso(since))
        rows = _rows(_cli_json(self._run(*command).stdout, "activity produced-work"), "activity produced-work")
        return [
            ProducedWorkItem(
                actor=actor,
                event_kind=_text(row, "event_kind"),
                event_id=_text(row, "id"),
                occurred_at=_text(row, "occurred_at"),
                unit_id=_text(row, "unit_id"),
            )
            for row in rows
        ]


def _ready_owned_rows(rows: Iterable[Mapping[str, Any]]) -> list[ReadyOwnedUnit]:
    return [
        ReadyOwnedUnit(
            unit_id=_text(row, "unit_id"),
            unit_key=_text(row, "unit_key"),
            actor=_text(row, "actor"),
            state=_text(row, "state"),
        )
        for row in rows
        if _text(row, "coverage") == COVERAGE_ACTIVE_OWNED and _text(row, "actor")
    ]


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def read_fleet_sessions(state: StateManagementInterface) -> list[FleetSession]:
    """Every non-terminal managed session, of every runtime.

    Imported at call time for the same reason ``lane_worktrees`` does it:
    ``session_lifecycle_store`` is a heavier module than this one needs at
    import.  Raises :class:`PagedReadError` on an incomplete read rather than
    returning a short roster that would read like an empty fleet.
    """
    from .session_lifecycle_store import list_managed_sessions

    rows = list_managed_sessions(state, {"lifecycle_state": list(ACTIVE_LIFECYCLE_STATES)})
    return [
        FleetSession(
            agent_instance_id=_text(row, "agent_instance_id"),
            agent_session_id=_text(row, "agent_session_id"),
            agent_runtime=_text(row, "agent_runtime") or DEFAULT_AGENT_RUNTIME,
            lifecycle_state=_text(row, "lifecycle_state"),
            local_name=_text(row, "local_name"),
        )
        for row in rows
    ]


def classify_stall(
    ready_owned: Sequence[ReadyOwnedUnit],
    produced: Sequence[ProducedWorkItem],
) -> str:
    """The predicate itself, pure: output over a window, never liveness.

    An empty ``ready_owned`` is ``not_stalled`` however idle the fleet looks
    -- an idle fleet with no work to do is not a stall, and reporting one
    would make the signal useless the moment the queue drains.
    """
    if not ready_owned:
        return VERDICT_NOT_STALLED
    return VERDICT_NOT_STALLED if produced else VERDICT_STALLED


def _runtime_counts(sessions: Sequence[FleetSession]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for session in sessions:
        counts[session.agent_runtime] = counts.get(session.agent_runtime, 0) + 1
    return counts


def _read_sessions_or_advise(state: StateManagementInterface) -> tuple[list[FleetSession], str]:
    try:
        return read_fleet_sessions(state), ""
    except PagedReadError as exc:
        return [], f"managed_session listing did not complete: {exc}"


def _read_ready_or_advise(reader: FleetStallRegisterReader) -> tuple[list[ReadyOwnedUnit], str]:
    try:
        return reader.read_ready_owned_work(), ""
    except RegisterUnitError as exc:
        return [], f"ready-and-owned register read did not complete ({exc.code}): {exc.detail}"


def _read_produced_or_advise(
    reader: FleetStallRegisterReader,
    actors: Sequence[str],
    since: datetime,
) -> tuple[list[ProducedWorkItem], str]:
    produced: list[ProducedWorkItem] = []
    for actor in actors:
        try:
            produced.extend(reader.read_produced_work(actor, since))
        except RegisterUnitError as exc:
            return [], f"produced-work register read for {actor} did not complete ({exc.code}): {exc.detail}"
    return produced, ""


def _window_seconds(value: object) -> int:
    if value is None or value == "":
        return DEFAULT_WINDOW_SECONDS
    try:
        seconds = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError) as exc:
        raise FleetStallSweepError(f"window_seconds must be a positive integer, got {value!r}") from exc
    if seconds <= 0:
        raise FleetStallSweepError(f"window_seconds must be a positive integer, got {seconds}")
    return seconds


def record_completed_run(state: StateManagementInterface, record: Mapping[str, Any]) -> None:
    """Publish this completed run, so the sweep's own silence is observable."""
    require_completed(
        state.set_key_value(STATE_NAMESPACE, LAST_COMPLETED_RUN_KEY, json.dumps(dict(record), sort_keys=True)),
        "fleet_stall_sweep record completed run",
    )


def read_completed_run(state: StateManagementInterface) -> dict[str, Any] | None:
    """The last completed run, or ``None`` when none has ever been recorded."""
    data = require_completed(
        state.get_key_value(STATE_NAMESPACE, LAST_COMPLETED_RUN_KEY),
        "fleet_stall_sweep read completed run",
    )
    if not data.get("found"):
        return None
    try:
        value = json.loads(str(data.get("value") or ""))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def clear_completed_run(state: StateManagementInterface) -> dict[str, Any]:
    """Retire the recorded run, returning what was retired.

    The record is a single latest-run row, overwritten by every run, so
    retention is bounded at one and correction is the next run's write.  This
    is that row's terminal action, and it is reversible only in the sense
    every run is: the next scheduled fire publishes a fresh record.

    The record it removed is read first and returned, so a caller that clears
    a run is told exactly what it discarded rather than being left to find
    out from the absence.  Clearing a record that was never there is reported
    as ``cleared: false`` and is not an error: a terminal action on an absent
    row has already reached the state it was asked for.
    """
    previous = read_completed_run(state)
    require_completed(
        state.delete_key_value(STATE_NAMESPACE, LAST_COMPLETED_RUN_KEY),
        "fleet_stall_sweep clear completed run",
    )
    return {
        "cleared": previous is not None,
        "cleared_run": previous,
        "advisories": ([] if previous is not None else ["no completed run was recorded, so nothing was cleared; the key is absent either way"]),
    }


def run_fleet_stall_sweep(
    state: StateManagementInterface,
    reader: FleetStallRegisterReader,
    *,
    now: datetime,
    trigger: str,
    window_seconds: object = None,
) -> dict[str, Any]:
    """One sweep: read, classify, publish the run, report.  No action taken."""
    window = _window_seconds(window_seconds)
    since = datetime.fromtimestamp(now.timestamp() - window, tz=UTC)
    sessions, session_advisory = _read_sessions_or_advise(state)
    ready_owned, ready_advisory = _read_ready_or_advise(reader)
    actors = sorted({unit.actor for unit in ready_owned})
    produced, produced_advisory = ([], "") if ready_advisory or not actors else _read_produced_or_advise(reader, actors, since)
    advisories = [note for note in (session_advisory, ready_advisory, produced_advisory) if note]
    verdict = VERDICT_UNMEASURED if advisories else classify_stall(ready_owned, produced)
    report = {
        "schema": RUN_RECORD_SCHEMA,
        "trigger": trigger,
        "completed_at": _iso(now),
        "window_seconds": window,
        "window_since": _iso(since),
        "verdict": verdict,
        "ready_and_owned_count": len(ready_owned),
        "produced_count": len(produced),
        "owning_actors": actors,
        "session_count": len(sessions),
        "runtime_counts": _runtime_counts(sessions),
        "advisories": advisories,
    }
    record_completed_run(state, report)
    return {
        **report,
        "ready_and_owned": [unit.to_dict() for unit in ready_owned],
        "produced": [item.to_dict() for item in produced],
        "sessions": [session.to_dict() for session in sessions],
    }


def read_sweep_status(state: StateManagementInterface, *, now: datetime) -> dict[str, Any]:
    """The sweep's own last completed run, its age, and a silence advisory."""
    record = read_completed_run(state)
    if record is None:
        return {
            "last_completed_run": None,
            "seconds_since_last_completed_run": None,
            "silent": False,
            "silence_threshold_seconds": SILENCE_THRESHOLD_SECONDS,
            "advisories": ["no completed run has been recorded yet; the sweep may never have run"],
        }
    age = _age_seconds(record, now=now)
    silent = age is not None and age > SILENCE_THRESHOLD_SECONDS
    advisories = [f"the sweep last completed {age:.0f}s ago, past its {SILENCE_THRESHOLD_SECONDS}s silence threshold"] if silent else []
    if age is None:
        advisories.append("the recorded run carries no readable completed_at; its age cannot be measured")
    return {
        "last_completed_run": record,
        "seconds_since_last_completed_run": age,
        "silent": silent,
        "silence_threshold_seconds": SILENCE_THRESHOLD_SECONDS,
        "advisories": advisories,
    }


def _age_seconds(record: Mapping[str, Any], *, now: datetime) -> float | None:
    stamp = record.get("completed_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        completed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, now.timestamp() - completed.timestamp())


def ensure_sweep_schedule(
    scheduling_service: object,
    *,
    cron_expression: str = DEFAULT_SCHEDULE_CRON,
    tag: str = DEFAULT_SCHEDULE_TAG,
    label: str = DEFAULT_SCHEDULE_LABEL,
) -> dict[str, Any]:
    """Idempotently ensure the sweep's cron exists on the platform's own scheduler.

    ``scheduling_service`` is the service binding, whose
    ``get_schedules_by_tag``/``create_cron_schedule`` take the friendly
    keyword shape.  Nothing here refuses: the sweep needs no configured
    target, so there is no configuration under which the installed cron would
    be inert.  The cron's action is a LITERAL ``EDGE_SINK`` process key with
    no ``result_processor_kind`` -- the canonical cron-action shape the
    whole-tree gate resolves, and the one that keeps a report-only detector
    off the inference path on every fire.
    """
    schedule_state = {"flow_id": _SWEEP_FLOW_ID, "session_id": _SWEEP_SESSION_ID}
    existing = _unwrap_response(
        scheduling_service.get_schedules_by_tag(tag=tag, state=schedule_state),  # type: ignore[attr-defined]
        action="get_schedules_by_tag",
    )
    if existing.get("schedules"):
        return {"status": "already_present", "tag": tag, "cron_expression": cron_expression}
    created = _unwrap_response(
        scheduling_service.create_cron_schedule(  # type: ignore[attr-defined]
            cron_expression=cron_expression,
            actions=[{"process_key": "plugin::agent_messaging_plugin::sweep_fleet_stall_cron", "arguments": {}}],
            label=label,
            tags=[tag],
            state=schedule_state,
        ),
        action="create_cron_schedule",
    )
    return {
        "status": "created",
        "tag": tag,
        "cron_expression": cron_expression,
        "schedule_id": created.get("schedule_id"),
    }


_SWEEP_FLOW_ID: Final[str] = "flow-fleet-stall-sweep"
_SWEEP_SESSION_ID: Final[str] = "sess-fleet-stall-sweep"
"""System-owned identity for the cron's own schedule row -- there is no
caller; this is a scheduler-fired system check.  Its own pair, not another
cron's, so a fire-time audit trail can tell this schedule from any other."""


def _unwrap_response(result: object, *, action: str) -> dict[str, Any]:
    """Unwrap the scheduling plugin's ``build_response`` shape, raising on an
    error status rather than reading it as "no existing schedule found"."""
    if not isinstance(result, dict):
        raise FleetStallSweepError(f"{action}: expected a dict response, got {type(result).__name__}")
    status = result.get("action_status")
    if status != "completed":
        raise FleetStallSweepError(f"{action}: {result.get('error')!r} (action_status={status!r})")
    data = result.get("data")
    return data if isinstance(data, dict) else {}
