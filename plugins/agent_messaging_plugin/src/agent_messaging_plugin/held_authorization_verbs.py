"""R1 held-authorization queue verbs (2026-08-17, seat GO ruling).

Validation layer over ``held_authorization_store.py``. Per the ruling: the
three verbs are OPEN, not enforced to a specific caller — capability first,
lockdown only after usage data (no usage exists yet for this queue), and a
hand-rolled "is this Git-Controller?" predicate here would be a reimplemented
control the platform's identity model already makes unnecessary. The
CONVENTION — Git-Controller records at refusal time and retires on receiving
the matching first-party authorization; the ``owed_by_role`` holder may also
retire directly — is documented on each verb, not mechanically enforced.

``list_held_authorizations`` also reports accepted-but-unfired authorization
obligations (``accepted_authorization_verbs.py``), each marked by
``entry_kind``; acceptance is never inferred from a refusal's reason text.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from functools import partial
from typing import TYPE_CHECKING, Any, Final

from .accepted_authorization_store import list_accepted_authorizations
from .accepted_authorization_verbs import (
    ACCEPTED_ENTRY_FIELDS,
    accepted_entry_view,
    page_cursor,
    page_limit,
)
from .held_authorization_store import (
    list_held_authorizations as _list_held_authorizations,
)
from .held_authorization_store import (
    record_held_authorization as _record_held_authorization,
)
from .held_authorization_store import (
    retire_held_authorization as _retire_held_authorization,
)
from .session_lifecycle_verbs import VerbError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_REFUSAL: Final[str] = "refusal"
_ACCEPTED: Final[str] = "accepted"


def record_held_authorization(
    state: StateManagementInterface,
    *,
    requesting_peer: str,
    owed_by_role: str,
    branch_or_request_ref: str,
    reason: str,
) -> dict[str, Any]:
    """Record one open held-authorization entry.

    Convention: Git-Controller calls this at the moment it refuses a peer's
    citation of an authorization it cannot verify first-party in its own
    inbox (the ``git-controller-commit`` skill's pre-authorization
    exception, Step 0). Not the requesting peer — a peer that forgets to
    record an obligation would equally forget to enqueue one; this call
    exists precisely so the entry does not depend on any seat's memory.
    ``created_at`` is the platform's own auto-timestamp on insert — the
    refusal IS the insert, so no caller-supplied time is needed or accepted.

    Errors: ``missing_argument`` for any empty required field, fast-fail
    before any write.
    """
    for field_name, value in (
        ("requesting_peer", requesting_peer),
        ("owed_by_role", owed_by_role),
        ("branch_or_request_ref", branch_or_request_ref),
        ("reason", reason),
    ):
        if not value.strip():
            raise VerbError(
                "missing_argument",
                f"record_held_authorization requires a non-empty {field_name}.",
            )
    entry_id = _record_held_authorization(
        state,
        requesting_peer=requesting_peer,
        owed_by_role=owed_by_role,
        branch_or_request_ref=branch_or_request_ref,
        reason=reason,
    )
    return {"entry_id": entry_id, "status": "recorded"}


def _refusal_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "entry_kind": _REFUSAL,
        "entry_id": str(row.get("id") or ""),
        "requesting_peer": str(row.get("requesting_peer") or ""),
        "owed_by_role": str(row.get("owed_by_role") or ""),
        "branch_or_request_ref": str(row.get("branch_or_request_ref") or ""),
        "reason": str(row.get("reason") or ""),
        "created_at": str(row.get("created_at") or ""),
        "retired_at": row.get("retired_at"),
        "retired_reason": row.get("retired_reason"),
        "retired_by": row.get("retired_by"),
        **dict.fromkeys(ACCEPTED_ENTRY_FIELDS),
    }


def _parse_cursor(raw: object) -> tuple[str, str | None]:
    """``refusal:<id>`` continues the refusal rows, ``accepted:<id>`` the
    accepted rows, ``accepted:`` starts them; no cursor starts at refusals."""
    cursor = page_cursor(raw)
    if cursor is None:
        return _REFUSAL, None
    kind, separator, after_id = cursor.partition(":")
    if not separator or kind not in {_REFUSAL, _ACCEPTED} or (kind == _REFUSAL and not after_id):
        raise VerbError("invalid_cursor", f"cursor {cursor!r} is not a next_cursor this verb returned.")
    return kind, after_id or None


def list_held_authorizations(
    state: StateManagementInterface,
    *,
    owed_by_role: str | None = None,
    requesting_peer: str | None = None,
    include_retired: bool = False,
    limit: object = None,
    cursor: object = None,
) -> dict[str, Any]:
    """The "what is blocked on <role>?" answer — read-only, callable by any
    session with no memory of a prior seat. One page of open refusals
    (``retired_at`` unset), then accepted-but-unfired obligations (``state``
    accepted), each marked by ``entry_kind``. ``include_retired`` adds
    retired refusals and fired, withdrawn or retired accepted entries.
    ``next_cursor`` is null only on the last page: a reader concludes
    nothing is outstanding only after paging until it is null. Each entry's
    ``created_at`` is returned as-is so a caller can judge staleness itself —
    this verb never filters or flags age; the queue has no silent TTL by
    design. An entry is an obligation, never proof of authority."""
    size = page_limit(limit)
    kind, after_id = _parse_cursor(cursor)
    refusal_rows = partial(
        _list_held_authorizations,
        state,
        owed_by_role=owed_by_role,
        requesting_peer=requesting_peer,
        include_retired=include_retired,
    )
    accepted_rows = partial(
        list_accepted_authorizations,
        state,
        owed_by_role=owed_by_role,
        requesting_peer=requesting_peer,
        include_terminal=include_retired,
    )
    entries: list[dict[str, Any]] = []
    try:
        if kind == _REFUSAL:
            entries, next_cursor = _domain_page(refusal_rows, _REFUSAL, _refusal_entry, size=size, after_id=after_id)
            if next_cursor is not None:
                return {"entries": entries, "count": len(entries), "next_cursor": next_cursor}
            after_id = None
        accepted, next_cursor = _domain_page(accepted_rows, _ACCEPTED, accepted_entry_view, size=size - len(entries), after_id=after_id)
    except LookupError as exc:
        raise VerbError("invalid_cursor", str(exc)) from exc
    entries.extend(accepted)
    return {"entries": entries, "count": len(entries), "next_cursor": next_cursor}


def _domain_page(
    rows: Callable[..., list[dict[str, Any]]],
    kind: str,
    view: Callable[[Mapping[str, Any]], dict[str, Any]],
    *,
    size: int,
    after_id: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Up to ``size`` entries of one domain after ``after_id`` and the cursor
    that continues it, or ``None`` once the domain is exhausted. With no
    room left (``size`` 0) the cursor only says whether the domain starts on
    the next page."""
    if size == 0:
        return [], f"{kind}:" if rows(limit=1, after_id=after_id) else None
    page = rows(limit=size, after_id=after_id)
    more = len(page) == size and bool(rows(limit=1, after_id=str(page[-1]["id"])))
    return [view(row) for row in page], f"{kind}:{page[-1]['id']}" if more else None


def retire_held_authorization(
    state: StateManagementInterface,
    *,
    entry_id: str,
    retired_reason: str,
    retired_by: str,
    retired_at: str,
) -> dict[str, Any]:
    """Retire one entry.

    Convention: Git-Controller retires an entry on receiving the matching
    first-party authorization; the ``owed_by_role`` holder may also retire
    directly (e.g. ``retired_reason='superseded'`` or ``'withdrawn'``) when
    the work is abandoned. No silent TTL anywhere in this queue — every
    retirement is an explicit call with a reason, never a timer.

    Errors: ``missing_argument`` for any empty required field.
    ``entry_not_found_or_already_retired`` when the ``retired_at IS NULL``
    predicate matches no row — either ``entry_id`` does not exist or the
    entry was already retired (both are ``False`` from the store; the
    caller cannot need to distinguish the two, since either way there is
    nothing further for THIS call to do).
    """
    for field_name, value in (
        ("entry_id", entry_id),
        ("retired_reason", retired_reason),
        ("retired_by", retired_by),
        ("retired_at", retired_at),
    ):
        if not value.strip():
            raise VerbError(
                "missing_argument",
                f"retire_held_authorization requires a non-empty {field_name}.",
            )
    retired = _retire_held_authorization(
        state,
        entry_id=entry_id,
        retired_reason=retired_reason,
        retired_by=retired_by,
        retired_at=retired_at,
    )
    if not retired:
        raise VerbError(
            "entry_not_found_or_already_retired",
            f"retire_held_authorization found no open entry {entry_id!r} — it either does not exist or was already retired.",
        )
    return {"entry_id": entry_id, "status": "retired"}


__all__ = [
    "list_held_authorizations",
    "record_held_authorization",
    "retire_held_authorization",
]
