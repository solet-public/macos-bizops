"""State-interface-backed accepted-authorization obligations (wgr_d854aa6c).

Persistence primitives over ``accepted_authorization`` (the current
projection) and ``accepted_authorization_event`` (its append-only history),
both declared in ``schema.py``. :func:`insert_accepted_authorization` writes
the projection and its ``record`` event in one typed transaction.
:func:`apply_accepted_authorization_change` reads the current row, lets the
caller's ``decide`` compute the next snapshot, compare-and-sets the row on
``id`` + ``version`` + prior ``state`` and appends the event in the same
transaction, so a stale writer or a failed event append changes nothing.

The lifecycle rules live in ``accepted_authorization_verbs.py``. Nothing here
performs an act or authorizes a landing, and nothing deletes a row: events are
only appended and read. Reads are ordered, bounded pages with an explicit
continuation, never a silently capped prefix.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE

from .held_authorization_store import CREATION_ORDER, PAGE_LIMIT, ordered_page, role_peer_filters
from .register_unit_client import VerbError
from .schema import (
    ACCEPTED_AUTHORIZATION_EVENT_ID_PREFIX,
    ACCEPTED_AUTHORIZATION_ID_PREFIX,
    AUTHORIZATION_ACCEPTED,
    TABLE_ACCEPTED_AUTHORIZATION,
    TABLE_ACCEPTED_AUTHORIZATION_EVENT,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface, StateTransaction

SNAPSHOT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "requesting_peer",
    "owed_by_role",
    "branch_or_request_ref",
    "authorizing_message_id",
    "act",
    "predicate",
    "state",
    "version",
    "notes",
    "terminal_reason",
    "terminal_actor",
    "terminal_evidence",
)
"""The projection's own fields: what an event's before/after snapshot holds."""
EVENT_ORDER: Final[tuple[str, str, str]] = ("entry_id", "revision", "id")

Snapshot = dict[str, Any]
Decide = Callable[[Snapshot], Snapshot]


def snapshot(row: Mapping[str, Any]) -> Snapshot:
    """The projection fields of one stored row, ``version`` as an int."""
    taken = {field: row.get(field) for field in SNAPSHOT_FIELDS}
    taken["version"] = int(row["version"])
    return taken


def field_changes(before: Snapshot | None, after: Snapshot) -> dict[str, str]:
    """Each projection field this event changed -> ``set`` | ``correct`` | ``clear``."""
    changes: dict[str, str] = {}
    for field in SNAPSHOT_FIELDS:
        if field in {"id", "version"}:
            continue
        old = None if before is None else before.get(field)
        new = after.get(field)
        if old == new:
            continue
        changes[field] = "set" if old is None else "clear" if new is None else "correct"
    return changes


def _append_event(
    txn: StateTransaction,
    *,
    operation: str,
    before: Snapshot | None,
    after: Snapshot,
    reason: str,
    actor: str,
) -> str:
    """Append the event for ``after``'s revision inside ``txn``; raises on failure."""
    revision = int(after["version"])
    return txn.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ACCEPTED_AUTHORIZATION_EVENT,
            "record": {
                "id": f"{ACCEPTED_AUTHORIZATION_EVENT_ID_PREFIX}_{uuid.uuid4().hex}",
                "namespace": AGENT_ROLE_BINDING_NAMESPACE,
                "external_id": f"accepted-authorization-event:{after['id']}:{revision}",
                "entry_id": after["id"],
                "revision": revision,
                "operation": operation,
                "authorizing_message_id": after["authorizing_message_id"],
                "reason": reason,
                "actor": actor,
                "changes": field_changes(before, after),
                "before_snapshot": None if before is None else dict(before),
                "after_snapshot": dict(after),
            },
        },
    )


def insert_accepted_authorization(
    state: StateManagementInterface,
    fields: Mapping[str, Any],
    *,
    reason: str,
    actor: str,
) -> tuple[Snapshot, str]:
    """Insert one accepted entry (version 1) and its ``record`` event in one
    transaction. Returns the stored snapshot and the event id.

    A fresh INSERT every call, so a blind retry after an unknown outcome
    would record a second entry: the caller reads back instead."""
    after = snapshot(
        {
            **{field: fields.get(field) for field in SNAPSHOT_FIELDS},
            "id": f"{ACCEPTED_AUTHORIZATION_ID_PREFIX}_{uuid.uuid4().hex}",
            "state": AUTHORIZATION_ACCEPTED,
            "version": 1,
        },
    )
    with state.transactional() as txn:
        txn.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ACCEPTED_AUTHORIZATION,
                "record": {**after, "namespace": AGENT_ROLE_BINDING_NAMESPACE},
            },
        )
        event_id = _append_event(txn, operation="record", before=None, after=after, reason=reason, actor=actor)
    return after, event_id


def apply_accepted_authorization_change(
    state: StateManagementInterface,
    *,
    entry_id: str,
    expected_version: int,
    operation: str,
    reason: str,
    actor: str,
    decide: Decide,
) -> tuple[Snapshot, str]:
    """Apply one versioned change. Returns the new snapshot and the event id.

    Inside one transaction: read the row, refuse ``entry_not_found`` or a
    ``stale_version`` the caller did not expect, let ``decide`` (which may
    raise its own refusal) compute the next fields, compare-and-set on
    ``id`` + ``expected_version`` + the prior ``state``, then append the
    event. A writer that lost the race between read and write matches no
    row and is refused ``stale_version``; any raise rolls the whole change
    back, so the projection never moves without its event."""
    with state.transactional() as txn:
        rows = txn.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_ACCEPTED_AUTHORIZATION, "filters": {"id": entry_id}, "limit": 1},
        )
        if not rows:
            raise VerbError("entry_not_found", f"no accepted authorization {entry_id!r}.")
        before = snapshot(rows[0])
        if before["version"] != expected_version:
            raise VerbError(
                "stale_version",
                f"{entry_id} is at version {before['version']}, not {expected_version}; read it back and decide again.",
            )
        after = {**decide(before), "id": entry_id, "version": expected_version + 1}
        updated = txn.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ACCEPTED_AUTHORIZATION,
                "filters": {"id": entry_id, "version": expected_version, "state": before["state"]},
            },
            {field: after[field] for field in SNAPSHOT_FIELDS if field != "id"},
        )
        if updated != 1:
            raise VerbError(
                "stale_version",
                f"{entry_id} changed after it was read at version {expected_version}; read it back and decide again.",
            )
        event_id = _append_event(txn, operation=operation, before=before, after=after, reason=reason, actor=actor)
    return after, event_id


def list_accepted_authorizations(
    state: StateManagementInterface,
    *,
    owed_by_role: str | None = None,
    requesting_peer: str | None = None,
    include_terminal: bool = False,
    limit: int = PAGE_LIMIT,
    after_id: str | None = None,
) -> list[dict[str, Any]]:
    """One page of accepted entries, oldest first. Pending (``state``
    accepted) by default; ``include_terminal`` adds fired, withdrawn and
    retired entries. Filters compose with AND."""
    filters = role_peer_filters(owed_by_role=owed_by_role, requesting_peer=requesting_peer)
    if not include_terminal:
        filters["state"] = AUTHORIZATION_ACCEPTED
    return ordered_page(
        state,
        TABLE_ACCEPTED_AUTHORIZATION,
        filters,
        CREATION_ORDER,
        limit=limit,
        after_id=after_id,
    )


def read_accepted_authorizations(
    state: StateManagementInterface,
    entry_ids: list[str],
) -> list[dict[str, Any]]:
    """Every current row among ``entry_ids``, oldest first, read page by page."""
    rows: list[dict[str, Any]] = []
    after_id: str | None = None
    while entry_ids:
        page = ordered_page(
            state,
            TABLE_ACCEPTED_AUTHORIZATION,
            {"id": list(entry_ids)},
            CREATION_ORDER,
            limit=PAGE_LIMIT,
            after_id=after_id,
        )
        rows.extend(page)
        if len(page) < PAGE_LIMIT:
            break
        after_id = str(page[-1]["id"])
    return rows


def entry_ids_for_message(state: StateManagementInterface, authorizing_message_id: str) -> list[str]:
    """Every entry a message ever authorized, in event order.

    Each record, correction or reopen event carries the authorizing message
    it put in force, so a superseded message still finds its entry."""
    entry_ids: list[str] = []
    after_id: str | None = None
    while True:
        page = ordered_page(
            state,
            TABLE_ACCEPTED_AUTHORIZATION_EVENT,
            {"authorizing_message_id": authorizing_message_id},
            EVENT_ORDER,
            limit=PAGE_LIMIT,
            after_id=after_id,
        )
        entry_ids.extend(str(row["entry_id"]) for row in page if str(row["entry_id"]) not in entry_ids)
        if len(page) < PAGE_LIMIT:
            return entry_ids
        after_id = str(page[-1]["id"])


def list_authorization_events(
    state: StateManagementInterface,
    entry_ids: list[str],
    *,
    limit: int,
    after_id: str | None = None,
) -> list[dict[str, Any]]:
    """One page of the events of ``entry_ids``, by entry then revision."""
    return ordered_page(
        state,
        TABLE_ACCEPTED_AUTHORIZATION_EVENT,
        {"entry_id": list(entry_ids)},
        EVENT_ORDER,
        limit=limit,
        after_id=after_id,
    )


__all__ = [
    "EVENT_ORDER",
    "SNAPSHOT_FIELDS",
    "Decide",
    "Snapshot",
    "apply_accepted_authorization_change",
    "entry_ids_for_message",
    "field_changes",
    "insert_accepted_authorization",
    "list_accepted_authorizations",
    "list_authorization_events",
    "read_accepted_authorizations",
    "snapshot",
]
