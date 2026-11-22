"""The one writer of a session end, and the verbs' logic that calls it.

A host's notice that a session closed is a trigger, never proof: a person's
session of the same name may exist again, and a hand seat's host session can
outlive its harness. A notice therefore ends a record only after the exact host
check says gone; a check that cannot be made (unknown) leaves the record exactly
as it is and is counted in the result. A session's own report of its end needs
no host check: the session is the authority on its own end, and the report never
acts on the host.

An ended record says why: ``ended: <what was observed>``, and carries one typed
reason (:mod:`session_end_reason`). A host notice and the start check cannot say
why a host session went away, so they store ``unknown``; a session's own report
may say ``finished`` or ``failed``. Every end goes through
:func:`end_session_record`, which is the only caller of the lifecycle write here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.core.runtime.port_manager import get_runtime_dir

# session_lifecycle_control and session_lifecycle_verbs import each other. Entering
# through verbs is the order that resolves, so a caller may import this module first.
from . import session_lifecycle_verbs  # noqa: F401  # pyright: ignore[reportUnusedImport]
from .host_end_notice import HostObservation, observe_tmux_session
from .schema import LIFECYCLE_TERMINATED
from .session_end_reason import FAILED, FINISHED, UNKNOWN, end_reason_of
from .session_hosts import DEFAULT_AGENT_RUNTIME, resolve_host_driver
from .session_lifecycle_control import _fire_session_terminal_dependencies  # pyright: ignore[reportPrivateUsage]  # noqa: PLC2701
from .session_lifecycle_store import (
    _OBSERVATION_FIELDS,  # pyright: ignore[reportPrivateUsage]  # noqa: PLC2701
    SessionNotFoundError,
    StaleLifecycleStateError,
    list_managed_sessions,
    read_managed_session,
    transition_lifecycle_state,
)
from .session_list import _LIVE_STATES  # pyright: ignore[reportPrivateUsage]  # noqa: PLC2701
from .session_runtime_files import remove_files_without_live_session

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from ananta.interfaces.state_management_interface import StateManagementInterface

_HOSTS_WITH_NOTICE = ("tmux", "headless")
# What a session may say about its own end; the other reasons are the solet's to record.
_SELF_REPORTED_OUTCOMES = (FINISHED, FAILED)


class SessionEndError(Exception):
    """A refused end request: ``parameter_invalid`` or ``session_not_found``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def observe_host(row: Mapping[str, Any], *, tmux: Sequence[str] | None = None) -> HostObservation:
    """The exact check of one record's host session, as far as one exists."""
    host = str(row.get("host") or "")
    host_ref = str(row.get("host_ref") or "")
    if not host_ref:
        return HostObservation("unknown", "no host reference recorded")
    if host == "tmux":
        return observe_tmux_session(host_ref, tmux=tmux)
    if host == "headless":
        return _observe_headless(row, host_ref)
    return HostObservation("unknown", f"host {host} has no exact check")


def _observe_headless(row: Mapping[str, Any], host_ref: str) -> HostObservation:
    try:
        driver, _host = resolve_host_driver("headless", str(row.get("agent_runtime") or DEFAULT_AGENT_RUNTIME))
        alive = driver.alive(host_ref)
    except Exception as exc:  # noqa: BLE001 — a probe fault is unknown, never gone
        return HostObservation("unknown", f"{type(exc).__name__}: {exc}")
    if alive is True:
        return HostObservation("present", f"headless process {host_ref} is running")
    if alive is False:
        return HostObservation("gone", f"headless process {host_ref} is not running")
    return HostObservation("unknown", "driver.alive returned no boolean observation")


def end_session_record(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    *,
    why: str,
    observation: HostObservation | None,
    directed_by: str,
    reason_kind: str = UNKNOWN,
) -> bool:
    """End one record under the exact snapshot it was read in.

    False means the record changed since ``row`` was read (the compare-and-set
    lost): nothing was written. True means it is terminated with ``reason_kind``
    as its typed reason, its audit row is written and every armed
    ``session_terminal`` edge waiting on it has fired.
    """
    now = datetime.now(UTC).isoformat()
    agent_instance_id = str(row["agent_instance_id"])
    recorded: dict[str, object] | None = None
    if observation is not None:
        recorded = {
            "host_liveness": "dead",
            "host_liveness_detail": observation.detail,
            "host_liveness_observed_at": now,
            "last_reconciled_at": now,
        }
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=str(row["lifecycle_state"]),
            to_state=LIFECYCLE_TERMINATED,
            directed_by=directed_by,
            reason=f"ended: {why}",
            recorded_fields=recorded,
            expected_observation={key: row.get(key) for key in _OBSERVATION_FIELDS},
            end_reason=reason_kind,
        )
    except StaleLifecycleStateError:
        return False
    _fire_session_terminal_dependencies(state, agent_instance_id=agent_instance_id, fired_at=now)
    return True


def end_records_if_gone(
    state: StateManagementInterface,
    rows: Sequence[Mapping[str, Any]],
    *,
    cause: str,
    directed_by: str,
    tmux: Sequence[str] | None = None,
    reason_kind: str = UNKNOWN,
) -> dict[str, Any]:
    """Check each distinct host session once; end the records whose host is gone.

    A present host leaves its records; an unknown one leaves them too and is
    listed with the detail. Ids are in row order. Every record ended here gets
    ``reason_kind`` as its typed reason.
    """
    observed: dict[tuple[str, str], HostObservation] = {}
    ended: list[str] = []
    present: list[str] = []
    unknown: list[dict[str, str]] = []
    changed_meanwhile: list[str] = []
    for row in rows:
        agent_instance_id = str(row["agent_instance_id"])
        key = (str(row.get("host") or ""), str(row.get("host_ref") or ""))
        if key not in observed:
            observed[key] = observe_host(row, tmux=tmux)
        observation = observed[key]
        if observation.state == "gone":
            ended_now = end_session_record(
                state,
                row,
                why=f"{cause}; {observation.detail}",
                observation=observation,
                directed_by=directed_by,
                reason_kind=reason_kind,
            )
            (ended if ended_now else changed_meanwhile).append(agent_instance_id)
        elif observation.state == "present":
            present.append(agent_instance_id)
        else:
            unknown.append({"agent_instance_id": agent_instance_id, "detail": observation.detail})
    return {
        "checked": len(rows),
        "ended": ended,
        "present": present,
        "unknown": unknown,
        "changed_meanwhile": changed_meanwhile,
    }


def _sweep_session_files(state: StateManagementInterface) -> dict[str, int]:
    """Remove the runtime files of every instance with no record still live, and count what happened."""
    live = {str(row["agent_instance_id"]) for row in list_managed_sessions(state, {"lifecycle_state": list(_LIVE_STATES)})}
    removal = remove_files_without_live_session(get_runtime_dir(), live)
    return {"removed": len(removal.removed), "kept_locked": len(removal.kept_locked), "failed": len(removal.failed)}


def check_session_records(
    state: StateManagementInterface,
    *,
    directed_by: str,
    tmux: Sequence[str] | None = None,
    sweep_session_files: bool = False,
) -> dict[str, Any]:
    """Check every non-ended record once when this solet starts.

    With ``sweep_session_files``, once the gone records are ended, the runtime folder is
    swept too: the watch and wake files of every instance with no record still live go
    (a family whose watcher holds its lock stays), and ``session_files`` counts them.
    Off by default, so only the live start check ever reads the folder.
    """
    rows = list_managed_sessions(state, {"lifecycle_state": list(_LIVE_STATES)})
    result = end_records_if_gone(
        state,
        rows,
        cause="the solet's start check found its host session gone",
        directed_by=directed_by,
        tmux=tmux,
    )
    result["counts"] = {
        "checked": result["checked"],
        "ended": len(result["ended"]),
        "present": len(result["present"]),
        "unknown": len(result["unknown"]),
        "changed_meanwhile": len(result["changed_meanwhile"]),
    }
    details: dict[str, int] = {}
    for unknown in result["unknown"]:
        detail = str(unknown["detail"])
        details[detail] = details.get(detail, 0) + 1
    result["unknown_by_detail"] = details
    if sweep_session_files:
        result["session_files"] = _sweep_session_files(state)
    return result


def on_host_session_closed(
    state: StateManagementInterface,
    *,
    host: object,
    host_ref: object,
    directed_by: str,
    tmux: Sequence[str] | None = None,
    reason_kind: str = UNKNOWN,
) -> dict[str, Any]:
    """A host reported that its session ``host_ref`` closed: end the records
    that name it, if the exact check agrees. The notice cannot say why, so a
    caller that knows passes ``reason_kind``; otherwise the reason is unknown."""
    if not isinstance(host, str) or host not in _HOSTS_WITH_NOTICE:
        raise SessionEndError("parameter_invalid", f"host must be one of {', '.join(_HOSTS_WITH_NOTICE)}")
    if not isinstance(host_ref, str) or not host_ref.strip():
        raise SessionEndError("parameter_invalid", "host_ref must be a non-blank string")
    rows = list_managed_sessions(
        state,
        {"host": host, "host_ref": host_ref, "lifecycle_state": list(_LIVE_STATES)},
    )
    outcome = end_records_if_gone(
        state,
        rows,
        cause=f"{host} reported host session {host_ref} closed",
        directed_by=directed_by,
        tmux=tmux,
        reason_kind=reason_kind,
    )
    return {"host": host, "host_ref": host_ref, **outcome}


def _self_reported_reason(outcome: object) -> str:
    """The typed reason for a session's own outcome: none given is unknown."""
    if outcome is None:
        return UNKNOWN
    if isinstance(outcome, str) and outcome in _SELF_REPORTED_OUTCOMES:
        return outcome
    raise SessionEndError("parameter_invalid", f"outcome must be {' or '.join(_SELF_REPORTED_OUTCOMES)} when given")


def end_reported_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: object,
    reason: object,
    directed_by: str,
    outcome: object = None,
) -> dict[str, Any]:
    """A session reported its own end: record it, with no host check.

    ``outcome`` is the session's own typed result, ``finished`` or ``failed``;
    none given records ``unknown`` and any other value is refused before anything
    is written. The result carries the record's ``end_reason`` as read back.
    """
    if not isinstance(agent_instance_id, str) or not agent_instance_id.strip():
        raise SessionEndError("parameter_invalid", "agent_instance_id must be a non-blank string")
    reason_kind = _self_reported_reason(outcome)
    instance = agent_instance_id.strip()
    try:
        row = read_managed_session(state, instance)
    except SessionNotFoundError as exc:
        raise SessionEndError("session_not_found", f"no managed session {instance!r}") from exc
    if row["lifecycle_state"] not in _LIVE_STATES:
        return {
            "agent_instance_id": instance,
            "ended": False,
            "already_ended": True,
            "lifecycle_state": row["lifecycle_state"],
            "end_reason": end_reason_of(row),
        }
    said = reason.strip() if isinstance(reason, str) else ""
    ended = end_session_record(
        state,
        row,
        why=f"the session reported its own end ({said or 'no reason given'})",
        observation=None,
        directed_by=directed_by,
        reason_kind=reason_kind,
    )
    after = read_managed_session(state, instance)
    return {
        "agent_instance_id": instance,
        "ended": ended,
        "already_ended": False,
        "lifecycle_state": after["lifecycle_state"],
        "end_reason": end_reason_of(after),
    }
