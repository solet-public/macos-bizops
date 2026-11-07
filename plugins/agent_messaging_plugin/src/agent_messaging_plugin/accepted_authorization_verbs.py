"""Accepted-authorization obligation verbs (wgr_d854aa6c, iss_b764f521).

Validation and lifecycle rules over ``accepted_authorization_store.py``. The
seat that ACCEPTS an authorization whose act waits on an external
precondition records it here at acceptance time. Nothing ingests messages
automatically, so an acceptance nobody records stays invisible to a
successor. Like the R1 refusal-queue verbs these are open to any caller:
``actor`` is reported provenance, never a grant or a role allowlist.

The queue is an obligation index. It is never proof that an authorization is
valid and never landing permission (a landing is authorized only by the
register's landing-authorization receipt). Recording a fire records the
supplied evidence and performs no act.

Lifecycle: ``accepted`` -> ``fired`` (terminal) | ``withdrawn`` | ``retired``;
``withdrawn`` / ``retired`` -> ``accepted`` only by ``reopen`` on a fresh
authorizing message. ``correct`` changes an accepted entry's act, predicate
or authorizing message (naming the authorizing message every time), and any
entry's metadata and notes; earlier values stay in the event history.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from .accepted_authorization_store import (
    Snapshot,
    apply_accepted_authorization_change,
    entry_ids_for_message,
    insert_accepted_authorization,
    list_authorization_events,
    read_accepted_authorizations,
    snapshot,
)
from .held_authorization_store import PAGE_LIMIT
from .schema import (
    AUTHORIZATION_ACCEPTED,
    AUTHORIZATION_FIRED,
    AUTHORIZATION_RETIRED,
    AUTHORIZATION_WITHDRAWN,
)
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

OPERATION_CORRECT: Final[str] = "correct"
OPERATION_FIRE: Final[str] = "fire"
OPERATION_WITHDRAW: Final[str] = "withdraw"
OPERATION_RETIRE: Final[str] = "retire"
OPERATION_REOPEN: Final[str] = "reopen"

REOPENABLE_STATES: Final[frozenset[str]] = frozenset({AUTHORIZATION_WITHDRAWN, AUTHORIZATION_RETIRED})
"""Fired is terminal: a distinct new act needs a new record, never a reopen."""

ACCEPTED_ENTRY_FIELDS: Final[tuple[str, ...]] = (
    "state",
    "version",
    "authorizing_message_id",
    "act",
    "predicate",
    "notes",
    "terminal_reason",
    "terminal_actor",
    "terminal_evidence",
)
"""The keys an accepted entry adds to the held-authorization entry shape."""

_RECORD_FIELDS: Final[tuple[str, ...]] = (
    "requesting_peer",
    "owed_by_role",
    "branch_or_request_ref",
    "authorizing_message_id",
    "act",
    "predicate",
)
_AUTHORITY_FIELDS: Final[tuple[str, ...]] = ("authorizing_message_id", "act", "predicate")
_METADATA_FIELDS: Final[tuple[str, ...]] = ("requesting_peer", "owed_by_role", "branch_or_request_ref")
_CHANGE_TEXT_FIELDS: Final[tuple[str, ...]] = (*_RECORD_FIELDS, "notes", "evidence")

# The optional arguments each operation accepts; anything else is refused
# rather than silently ignored.
_ALLOWED: Final[dict[str, frozenset[str]]] = {
    OPERATION_CORRECT: frozenset({*_RECORD_FIELDS, "notes", "clear_notes"}),
    OPERATION_FIRE: frozenset({"predicate", "evidence"}),
    OPERATION_WITHDRAW: frozenset({"evidence"}),
    OPERATION_RETIRE: frozenset({"evidence"}),
    OPERATION_REOPEN: frozenset({"authorizing_message_id", "predicate", "act"}),
}
_REQUIRED: Final[dict[str, tuple[str, ...]]] = {
    OPERATION_FIRE: ("predicate", "evidence"),
    OPERATION_REOPEN: ("authorizing_message_id", "predicate"),
}


@dataclass(frozen=True, slots=True)
class AuthorizationChange:
    """One requested change: the operation plus the arguments it supplied."""

    operation: str
    reason: str
    actor: str
    supplied: Mapping[str, str]
    clear_notes: bool = False

    def get(self, field: str) -> str | None:
        return self.supplied.get(field)


def _text(params: Mapping[str, object], field: str, *, verb: str) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip():
        raise VerbError("missing_argument", f"{verb} requires a non-empty {field}.")
    return value


def _optional_text(params: Mapping[str, object], field: str) -> str | None:
    """Absent stays absent; a supplied value must be non-blank text. A blank
    never means "clear": required fields cannot be cleared (withdraw the
    entry instead) and notes are cleared only by ``clear_notes``."""
    value = params.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise VerbError("invalid_argument", f"{field} must be non-blank text when supplied; omit it to leave it unchanged.")
    return value


def page_limit(raw: object) -> int:
    """A page size: 1 through ``PAGE_LIMIT``, defaulting to ``PAGE_LIMIT``."""
    if raw is None:
        return PAGE_LIMIT
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= PAGE_LIMIT:
        raise VerbError("invalid_argument", f"limit must be an integer from 1 through {PAGE_LIMIT}.")
    return raw


def page_cursor(raw: object) -> str | None:
    """An opaque continuation returned as ``next_cursor``, or absent for page one."""
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw.strip():
        raise VerbError("invalid_cursor", "cursor must be a next_cursor value from the previous page; omit it for the first page.")
    return raw


def accepted_entry_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """An accepted entry in the held-authorization entry shape.

    ``entry_kind`` is ``accepted``; the refusal-only keys (``reason`` and the
    ``retired_*`` stamps) are null because an accepted entry is not a
    refusal. Read ``state`` and the ``terminal_*`` fields instead."""
    current = snapshot(row)
    return {
        "entry_kind": "accepted",
        "entry_id": str(current["id"]),
        "requesting_peer": current["requesting_peer"],
        "owed_by_role": current["owed_by_role"],
        "branch_or_request_ref": current["branch_or_request_ref"],
        "reason": None,
        "created_at": str(row.get("created_at") or ""),
        "retired_at": None,
        "retired_reason": None,
        "retired_by": None,
        **{field: current[field] for field in ACCEPTED_ENTRY_FIELDS},
    }


def _event_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "event_id": str(row["id"]),
        "entry_id": str(row["entry_id"]),
        "revision": int(row["revision"]),
        "operation": row["operation"],
        "authorizing_message_id": row["authorizing_message_id"],
        "reason": row["reason"],
        "actor": row["actor"],
        "changes": row["changes"],
        "before": row.get("before_snapshot"),
        "after": row["after_snapshot"],
        "recorded_at": str(row.get("created_at") or ""),
    }


def record_accepted_authorization(
    state: StateManagementInterface,
    params: Mapping[str, object],
) -> dict[str, Any]:
    """Record one accepted, not-yet-fired authorization obligation.

    The accepting seat calls this when it accepts the authorization; the
    entry starts ``accepted`` at version 1 with a ``record`` event, written
    in one transaction. Not idempotent: after an unknown outcome read back
    with ``get_authorization_history`` (by ``authorizing_message_id``)
    before recording again.

    Errors: ``missing_argument`` for an empty required field;
    ``invalid_argument`` for blank ``notes``.
    """
    verb = "record_accepted_authorization"
    fields: dict[str, Any] = {field: _text(params, field, verb=verb) for field in _RECORD_FIELDS}
    reason = _text(params, "reason", verb=verb)
    actor = _text(params, "actor", verb=verb)
    fields["notes"] = _optional_text(params, "notes")
    after, event_id = insert_accepted_authorization(state, fields, reason=reason, actor=actor)
    return {
        "entry_id": after["id"],
        "status": "recorded",
        "state": after["state"],
        "version": after["version"],
        "event_id": event_id,
        "entry": after,
    }


def _clear_notes(params: Mapping[str, object]) -> bool:
    clear_notes = params.get("clear_notes")
    if clear_notes is None:
        return False
    if not isinstance(clear_notes, bool):
        raise VerbError("invalid_argument", "clear_notes must be a boolean when supplied.")
    return clear_notes


def _require_operation_arguments(operation: str, supplied: Mapping[str, str], *, clear_notes: bool) -> None:
    """Refuse an argument ``operation`` does not take, notes together with
    clear_notes, and a missing argument ``operation`` requires."""
    allowed = _ALLOWED[operation]
    foreign = sorted((set(supplied) | ({"clear_notes"} if clear_notes else set())) - allowed)
    if foreign:
        raise VerbError("invalid_argument", f"{operation} does not take {foreign}; it takes {sorted(allowed)}.")
    if clear_notes and "notes" in supplied:
        raise VerbError("invalid_argument", "supply notes to set or correct them, or clear_notes to clear them, not both.")
    missing = [field for field in _REQUIRED.get(operation, ()) if field not in supplied]
    if missing:
        raise VerbError("missing_argument", f"{operation} requires a non-empty {missing[0]}.")


def _change_request(params: Mapping[str, object]) -> AuthorizationChange:
    verb = "change_accepted_authorization"
    operation = _text(params, "operation", verb=verb)
    if operation not in _ALLOWED:
        raise VerbError("invalid_argument", f"operation must be one of {sorted(_ALLOWED)}; got {operation!r}.")
    texts = {field: _optional_text(params, field) for field in _CHANGE_TEXT_FIELDS}
    supplied = {field: value for field, value in texts.items() if value is not None}
    clear_notes = _clear_notes(params)
    _require_operation_arguments(operation, supplied, clear_notes=clear_notes)
    return AuthorizationChange(
        operation=operation,
        reason=_text(params, "reason", verb=verb),
        actor=_text(params, "actor", verb=verb),
        supplied=supplied,
        clear_notes=clear_notes,
    )


def _require_accepted(current: Snapshot, operation: str) -> None:
    if current["state"] != AUTHORIZATION_ACCEPTED:
        raise VerbError(
            "invalid_transition",
            f"{operation} needs state accepted; {current['id']} is {current['state']}.",
        )


def _decide_correct(change: AuthorizationChange, current: Snapshot) -> Snapshot:
    after = dict(current)
    for field in (*_AUTHORITY_FIELDS, *_METADATA_FIELDS):
        value = change.get(field)
        if value is not None:
            after[field] = value
    if any(after[field] != current[field] for field in _AUTHORITY_FIELDS):
        if current["state"] != AUTHORIZATION_ACCEPTED:
            raise VerbError(
                "invalid_transition",
                f"{current['id']} is {current['state']}: its act, predicate and authorizing message are history. Reopen a withdrawn or retired entry; a fired act needs a new record.",
            )
        if change.get("authorizing_message_id") is None:
            raise VerbError(
                "authorizing_message_required",
                "correcting the act or predicate names the authorizing message that reissued it.",
            )
    if change.clear_notes:
        after["notes"] = None
    elif change.get("notes") is not None:
        after["notes"] = change.get("notes")
    if after == current:
        raise VerbError("no_change", f"the correction changes nothing on {current['id']}.")
    return after


def _decide_fire(change: AuthorizationChange, current: Snapshot) -> Snapshot:
    _require_accepted(current, OPERATION_FIRE)
    current_predicate = current["predicate"]
    if change.get("predicate") != current_predicate:
        raise VerbError(
            "predicate_mismatch",
            "fire names the CURRENT predicate verbatim; a withdrawn or superseded predicate never fires.",
        )
    return {
        **current,
        "state": AUTHORIZATION_FIRED,
        "terminal_reason": change.reason,
        "terminal_actor": change.actor,
        "terminal_evidence": change.get("evidence"),
    }


def _decide_close(target: str, change: AuthorizationChange, current: Snapshot) -> Snapshot:
    _require_accepted(current, change.operation)
    return {
        **current,
        "state": target,
        "terminal_reason": change.reason,
        "terminal_actor": change.actor,
        "terminal_evidence": change.get("evidence"),
    }


def _decide_reopen(change: AuthorizationChange, current: Snapshot) -> Snapshot:
    if current["state"] not in REOPENABLE_STATES:
        raise VerbError(
            "invalid_transition",
            f"reopen needs state withdrawn or retired; {current['id']} is {current['state']}" + (" and a fired act is terminal: record a new entry for a new act." if current["state"] == AUTHORIZATION_FIRED else "."),
        )
    message = change.get("authorizing_message_id")
    if message == current["authorizing_message_id"]:
        raise VerbError(
            "authorization_not_fresh",
            "reopen needs a fresh authorizing message; the withdrawn or retired one cannot revive itself.",
        )
    return {
        **current,
        "state": AUTHORIZATION_ACCEPTED,
        "authorizing_message_id": message,
        "predicate": change.get("predicate"),
        "act": change.get("act") or current["act"],
        "terminal_reason": None,
        "terminal_actor": None,
        "terminal_evidence": None,
    }


_DECIDE: Final[dict[str, Callable[[AuthorizationChange, Snapshot], Snapshot]]] = {
    OPERATION_CORRECT: _decide_correct,
    OPERATION_FIRE: _decide_fire,
    OPERATION_WITHDRAW: partial(_decide_close, AUTHORIZATION_WITHDRAWN),
    OPERATION_RETIRE: partial(_decide_close, AUTHORIZATION_RETIRED),
    OPERATION_REOPEN: _decide_reopen,
}


def change_accepted_authorization(
    state: StateManagementInterface,
    params: Mapping[str, object],
) -> dict[str, Any]:
    """Apply one versioned lifecycle change to an accepted entry.

    ``operation`` is ``correct`` | ``fire`` | ``withdraw`` | ``retire`` |
    ``reopen``; ``expected_version`` is the version the caller read. The
    change and its event commit together or not at all; a writer whose
    version is stale is refused before or at the compare-and-set and writes
    nothing, so a retry of a change that already landed is refused
    ``stale_version`` instead of appending a duplicate event.

    Errors: ``missing_argument``, ``invalid_argument``, ``entry_not_found``,
    ``stale_version``, ``invalid_transition``, ``predicate_mismatch``,
    ``authorizing_message_required``, ``authorization_not_fresh``,
    ``no_change``.
    """
    entry_id = _text(params, "entry_id", verb="change_accepted_authorization")
    expected_version = params.get("expected_version")
    if isinstance(expected_version, bool) or not isinstance(expected_version, int) or expected_version < 1:
        raise VerbError("invalid_argument", "expected_version must be the entry's current version, an integer >= 1.")
    change = _change_request(params)
    after, event_id = apply_accepted_authorization_change(
        state,
        entry_id=entry_id,
        expected_version=expected_version,
        operation=change.operation,
        reason=change.reason,
        actor=change.actor,
        decide=partial(_DECIDE[change.operation], change),
    )
    return {
        "entry_id": entry_id,
        "operation": change.operation,
        "status": "changed",
        "state": after["state"],
        "version": after["version"],
        "event_id": event_id,
        "entry": after,
    }


def get_authorization_history(
    state: StateManagementInterface,
    params: Mapping[str, object],
) -> dict[str, Any]:
    """Ordered immutable history of accepted entries, by entry or by message.

    Exactly one of ``entry_id`` / ``authorizing_message_id``. A message id
    finds every entry it ever authorized, superseded or current, so an old
    message shows the correction or withdrawal that replaced it. ``current``
    is each entry's projection now: its ``predicate`` is the only one that
    may fire. Events page by ``limit`` and ``cursor``; follow
    ``next_cursor`` until it is null. An empty result means this queue has
    no record of the message, not that no authorization was ever given.

    Errors: ``invalid_argument``, ``entry_not_found``, ``invalid_cursor``.
    """
    entry_id = _optional_text(params, "entry_id")
    message = _optional_text(params, "authorizing_message_id")
    if (entry_id is None) == (message is None):
        raise VerbError("invalid_argument", "name exactly one of entry_id or authorizing_message_id.")
    limit = page_limit(params.get("limit"))
    cursor = page_cursor(params.get("cursor"))
    selector = {"entry_id": entry_id} if entry_id is not None else {"authorizing_message_id": message}
    entry_ids, current = _history_entries(state, entry_id=entry_id, message=message)
    events, next_cursor = _history_events(state, entry_ids, limit=limit, cursor=cursor)
    return {
        "selector": selector,
        "entry_ids": entry_ids,
        "current": [accepted_entry_view(row) for row in current],
        "events": [_event_view(row) for row in events],
        "count": len(events),
        "next_cursor": next_cursor,
    }


def _history_entries(
    state: StateManagementInterface,
    *,
    entry_id: str | None,
    message: str | None,
) -> tuple[list[str], list[dict[str, Any]]]:
    """The entries a history read covers and their current rows."""
    if entry_id is None:
        entry_ids = entry_ids_for_message(state, str(message))
        return entry_ids, read_accepted_authorizations(state, entry_ids)
    current = read_accepted_authorizations(state, [entry_id])
    if not current:
        raise VerbError("entry_not_found", f"no accepted authorization {entry_id!r}.")
    return [entry_id], current


def _history_events(
    state: StateManagementInterface,
    entry_ids: list[str],
    *,
    limit: int,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """One page of the entries' events and the cursor that continues it."""
    if not entry_ids:
        return [], None
    try:
        events = list_authorization_events(state, entry_ids, limit=limit, after_id=cursor)
        more = len(events) == limit and bool(list_authorization_events(state, entry_ids, limit=1, after_id=str(events[-1]["id"])))
    except LookupError as exc:
        raise VerbError("invalid_cursor", str(exc)) from exc
    return events, str(events[-1]["id"]) if more else None


__all__ = [
    "ACCEPTED_ENTRY_FIELDS",
    "OPERATION_CORRECT",
    "OPERATION_FIRE",
    "OPERATION_REOPEN",
    "OPERATION_RETIRE",
    "OPERATION_WITHDRAW",
    "REOPENABLE_STATES",
    "AuthorizationChange",
    "accepted_entry_view",
    "change_accepted_authorization",
    "get_authorization_history",
    "page_cursor",
    "page_limit",
    "record_accepted_authorization",
]
