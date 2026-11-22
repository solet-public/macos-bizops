"""Store operations for the solet's own work-folder records.

A creator opens a scratch folder, closes it when its work ends, records what it
kept, and a sweep removes what is finished. These operations record that over
the state interface, with no outside service.

States and the only moves between them (anything else is ``invalid_transition``):

==========  =========================  ========================================
from        to                         operation
==========  =========================  ========================================
(none)      open                       :func:`open_folder`
open        due                        :func:`close`
due         harvested                  :func:`harvest_record` (a receipt; none
                                       needed when the folder is regenerable)
due or      open                       :func:`reopen` (clears the current
harvested                              receipt; the history keeps it)
open, due   retired                    :func:`retire` (a retired folder is kept
or harvested                           by the sweep)
retired     open                       :func:`restore`
harvested   deleted                    :func:`mark_deleted` (the sweep)
==========  =========================  ========================================

``deleted`` is terminal: opening the same path again makes a new row.

Lifecycle inventory (every row of ``work_folder`` and its events):

- create: :func:`open_folder` — idempotent: a path whose row is not deleted
  returns that row and writes nothing.
- read: :func:`get`, :func:`list_folders`, :func:`history` (current and
  historical, bounded pages).
- update or correct: :func:`correct` (harvest_kind, owner, purpose),
  :func:`close`, :func:`harvest_record`.
- reverse: :func:`reopen`, :func:`restore`.
- terminal: :func:`retire`, :func:`mark_deleted`; :func:`record_removal_failed`
  records a removal that did not finish and leaves the state alone.
- stale writes: every change takes ``expected_version``, is a compare-and-set
  on ``version`` and appends exactly one event whose ordinal is the new
  version. A stale version raises :class:`StaleVersionError` and writes nothing.

The actor is recorded, never an authority: no operation here refuses by who is
asking. The row change and its event are two state writes, not one transaction;
the version/ordinal equality makes a missing event visible to a reader.

The store dependency is structural (:class:`WorkFolderStore`), so the real
``StateManagementInterface``, the release run's state service and the offline
smoke fake all satisfy it.
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol, runtime_checkable
from uuid import uuid4

from ananta.core.domain.timestamps import to_naive_utc
from ananta.error_handling import FrameworkError
from ananta.llm.agent_messaging.state_results import (
    is_completed,
    require_completed,
    require_records,
    require_updated,
)
from ananta.services.lifecycle_management_service.work_folder_schema import (
    COL_ACTOR_EFFORT,
    COL_ACTOR_INSTANCE,
    COL_ACTOR_MODEL,
    COL_ACTOR_SESSION,
    COL_BOUND_KIND,
    COL_BOUND_REF,
    COL_DETAIL,
    COL_DEV,
    COL_FOLDER_ID,
    COL_HARVEST_KIND,
    COL_HARVEST_RECEIPT,
    COL_INO,
    COL_KIND,
    COL_ORDINAL,
    COL_OWNER,
    COL_PARENT_DIR,
    COL_PATH,
    COL_PRIOR_VERSION,
    COL_PURPOSE,
    COL_RECORDED_AT,
    COL_STATE,
    COL_VERSION,
    HARVEST_KINDS,
    HARVEST_REGENERABLE,
    KIND_CLOSED,
    KIND_CORRECTED,
    KIND_DELETED,
    KIND_HARVESTED,
    KIND_OPENED,
    KIND_REMOVAL_FAILED,
    KIND_REOPENED,
    KIND_RESTORED,
    KIND_RETIRED,
    STATE_DELETED,
    STATE_DUE,
    STATE_HARVESTED,
    STATE_OPEN,
    STATE_RETIRED,
    STATES,
    TABLE_WORK_FOLDER,
    TABLE_WORK_FOLDER_EVENT,
    WORK_FOLDER_NAMESPACE,
    get_work_folder_schema,
)

Row = dict[str, object]
Cursor = tuple[str | int, str]

# The state interface caps an ordered page at 100 rows.
MAX_PAGE_LIMIT = 100

_FOLDER_ID_PREFIX = get_work_folder_schema().tables[TABLE_WORK_FOLDER].id_prefix


class WorkFolderError(FrameworkError):
    """Base of the typed work-folder errors; ``code`` is the stable error name."""

    code: ClassVar[str] = "work_folder_error"


class StaleVersionError(WorkFolderError):
    """``expected_version`` no longer matches the row; nothing was written."""

    code: ClassVar[str] = "stale_version"


class UnknownFolderError(WorkFolderError):
    """No work-folder record has that id."""

    code: ClassVar[str] = "unknown_folder"


class InvalidTransitionError(WorkFolderError):
    """The move is not in the transition table, or its precondition is not met."""

    code: ClassVar[str] = "invalid_transition"


class InvalidPathError(WorkFolderError):
    """The path is not an absolute path of an existing real directory below the root."""

    code: ClassVar[str] = "invalid_path"


class InvalidFieldError(WorkFolderError):
    """A field value is outside its allowed set or range (harvest kind, receipt, limit, state)."""

    code: ClassVar[str] = "invalid_field"


@dataclass(frozen=True, slots=True)
class Actor:
    """Who caused a write. Recorded for the history, never used to refuse."""

    instance: str = ""
    session: str = ""
    model: str = ""
    effort: str = ""


@dataclass(frozen=True, slots=True)
class RowPage:
    """One page of rows; ``next_after`` continues it, or is None on the last page."""

    rows: list[Row]
    next_after: Cursor | None


@runtime_checkable
class WorkFolderStore(Protocol):
    """The state-interface surface the work-folder store needs."""

    # Positional-only: the real interface and the offline fake name these
    # parameters differently, and the store never passes them by keyword.
    def write_state(self, namespace: str, data: dict[str, object], /) -> object: ...

    def query_state(self, namespace: str, query: dict[str, object], /) -> object: ...

    def query_ordered(self, namespace: str, query: dict[str, object], /) -> object: ...

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
        /,
    ) -> object: ...


def require_work_folder_store(state: object) -> WorkFolderStore:
    """The state surface for the work-folder store; fail LOUD if absent or wrong.

    The record is how the solet knows what it may remove, so a state service that
    cannot hold it must stop the caller, not silently skip the record.
    """
    if not isinstance(state, WorkFolderStore):
        raise FrameworkError(
            f"the work-folder store requires a state service exposing write_state/query_state/query_ordered/update_state; got {type(state).__name__}",
        )
    return state


def _utc_text(value: object) -> str:
    """One fixed spelling of a UTC instant, so text order is time order."""
    return to_naive_utc(value).isoformat(timespec="microseconds")


def _json_text(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def has_custody(row: Mapping[str, object]) -> bool:
    """True when the folder may be removed: it is regenerable or holds a receipt.

    The one home of the rule "never removed without its receipt"; the sweep and
    :func:`mark_deleted` both ask it.
    """
    return row.get(COL_HARVEST_KIND) == HARVEST_REGENERABLE or str(row.get(COL_HARVEST_RECEIPT, "")) != ""


def get(store: WorkFolderStore, *, folder_id: str) -> Row | None:
    """Read one work-folder record by id, or ``None``."""
    rows = require_records(
        store.query_state(
            WORK_FOLDER_NAMESPACE,
            {"table": TABLE_WORK_FOLDER, "filters": {COL_FOLDER_ID: folder_id}},
        ),
    )
    return rows[0] if rows else None


def _load(store: WorkFolderStore, folder_id: str) -> Row:
    row = get(store, folder_id=folder_id)
    if row is None:
        raise UnknownFolderError(f"no work-folder record {folder_id!r}")
    return row


def _live_row_for_path(store: WorkFolderStore, path: str) -> Row | None:
    rows = require_records(
        store.query_state(
            WORK_FOLDER_NAMESPACE,
            {"table": TABLE_WORK_FOLDER, "filters": {COL_PATH: path}},
        ),
    )
    live = [row for row in rows if row.get(COL_STATE) != STATE_DELETED]
    return live[0] if live else None


def _check_harvest_kind(value: str) -> None:
    if value not in HARVEST_KINDS:
        raise InvalidFieldError(f"harvest_kind must be one of {HARVEST_KINDS}, got {value!r}")


def _check_limit(limit: int) -> None:
    if not 1 <= limit <= MAX_PAGE_LIMIT:
        raise InvalidFieldError(f"limit must be between 1 and {MAX_PAGE_LIMIT}, got {limit}")


def _canonical_directory(path: str) -> tuple[str, os.stat_result]:
    """The canonical real path of an existing directory, and its fresh lstat."""
    if not isinstance(path, str) or not os.path.isabs(path):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise InvalidPathError(f"path must be absolute, got {path!r}")
    real = os.path.realpath(path)
    try:
        info = os.lstat(real)
    except OSError as error:
        raise InvalidPathError(f"{real!r} is not an existing directory: {error}") from error
    if not stat.S_ISDIR(info.st_mode):
        raise InvalidPathError(f"{real!r} is not a directory")
    if os.path.dirname(real) == real:
        raise InvalidPathError("the filesystem root is not a work folder")
    return real, info


def _guard(row: Row, expected_version: int, allowed_from: frozenset[str] | None) -> None:
    """Stale first (the caller's view is old), then the transition table."""
    if row[COL_VERSION] != expected_version:
        raise StaleVersionError(
            f"work folder {row[COL_FOLDER_ID]} is at version {row[COL_VERSION]}, not {expected_version}; re-read it and retry",
        )
    state = row[COL_STATE]
    if state == STATE_DELETED:
        raise InvalidTransitionError(f"work folder {row[COL_FOLDER_ID]} is deleted, which is terminal")
    if allowed_from is not None and state not in allowed_from:
        raise InvalidTransitionError(
            f"work folder {row[COL_FOLDER_ID]} is {state}; this move needs {sorted(allowed_from)}",
        )


def _append_event(
    store: WorkFolderStore,
    *,
    folder_id: str,
    ordinal: int,
    kind: str,
    detail: Mapping[str, object],
    actor: Actor,
    recorded_at: datetime | None,
) -> None:
    require_completed(
        store.write_state(
            WORK_FOLDER_NAMESPACE,
            {
                "table": TABLE_WORK_FOLDER_EVENT,
                "record": {
                    COL_FOLDER_ID: folder_id,
                    COL_ORDINAL: ordinal,
                    COL_KIND: kind,
                    COL_PRIOR_VERSION: ordinal - 1,
                    COL_ACTOR_INSTANCE: actor.instance,
                    COL_ACTOR_SESSION: actor.session,
                    COL_ACTOR_MODEL: actor.model,
                    COL_ACTOR_EFFORT: actor.effort,
                    COL_DETAIL: _json_text(detail),
                    COL_RECORDED_AT: _utc_text(datetime.now(UTC) if recorded_at is None else recorded_at),
                },
            },
        ),
        f"append work folder event {kind}",
    )


def _commit(
    store: WorkFolderStore,
    row: Row,
    *,
    kind: str,
    updates: Mapping[str, object],
    detail: Mapping[str, object],
    actor: Actor,
    recorded_at: datetime | None,
) -> Row:
    """Compare-and-set the row to its next version, append the event, read back."""
    folder_id = str(row[COL_FOLDER_ID])
    expected = int(str(row[COL_VERSION]))
    changed = require_updated(
        store.update_state(
            WORK_FOLDER_NAMESPACE,
            {
                "table": TABLE_WORK_FOLDER,
                "filters": {COL_FOLDER_ID: folder_id, COL_VERSION: expected},
            },
            {**updates, COL_VERSION: expected + 1},
        ),
    )
    if changed != 1:
        raise StaleVersionError(f"work folder {folder_id} is no longer at version {expected}; re-read it and retry")
    _append_event(
        store,
        folder_id=folder_id,
        ordinal=expected + 1,
        kind=kind,
        detail=detail,
        actor=actor,
        recorded_at=recorded_at,
    )
    moved = get(store, folder_id=folder_id)
    if moved is None:
        raise WorkFolderError(f"work folder {folder_id} vanished after a change")
    return moved


def _move(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    allowed_from: frozenset[str],
    kind: str,
    new_state: str | None,
    extra_updates: Mapping[str, object],
    detail: Mapping[str, object],
    actor: Actor,
    recorded_at: datetime | None,
) -> Row:
    row = _load(store, folder_id)
    _guard(row, expected_version, allowed_from)
    updates: dict[str, object] = dict(extra_updates)
    if new_state is not None:
        updates[COL_STATE] = new_state
    return _commit(store, row, kind=kind, updates=updates, detail=detail, actor=actor, recorded_at=recorded_at)


def open_folder(
    store: WorkFolderStore,
    *,
    path: str,
    purpose: str,
    bound_kind: str,
    bound_ref: str,
    harvest_kind: str,
    owner: str,
    actor: Actor,
    recorded_at: datetime | None = None,
) -> tuple[Row, bool]:
    """Record an existing directory as a work folder; return ``(row, created)``.

    Idempotent on the canonical path: a path whose row is not deleted returns
    that row unchanged with ``created`` False and writes nothing (the caller
    compares ``dev`` and ``ino`` with the directory, and reopens a due or
    harvested row). The path must be absolute and name an existing real
    directory that is not the filesystem root; a symlinked spelling resolves to
    its target, which is what is recorded.
    """
    _check_harvest_kind(harvest_kind)
    real, info = _canonical_directory(path)
    existing = _live_row_for_path(store, real)
    if existing is not None:
        return existing, False
    folder_id = f"{_FOLDER_ID_PREFIX}-{uuid4().hex}"
    written = store.write_state(
        WORK_FOLDER_NAMESPACE,
        {
            "table": TABLE_WORK_FOLDER,
            "record": {
                COL_FOLDER_ID: folder_id,
                COL_PATH: real,
                COL_PARENT_DIR: os.path.dirname(real),
                COL_DEV: str(info.st_dev),
                COL_INO: str(info.st_ino),
                COL_PURPOSE: purpose,
                COL_BOUND_KIND: bound_kind,
                COL_BOUND_REF: bound_ref,
                COL_HARVEST_KIND: harvest_kind,
                COL_OWNER: owner,
                COL_STATE: STATE_OPEN,
                COL_HARVEST_RECEIPT: "",
                COL_VERSION: 1,
            },
        },
    )
    if not is_completed(written):
        # A concurrent opener won the unique live path; that row is the answer.
        # Anything else is a real write failure and stays loud.
        raced = _live_row_for_path(store, real)
        if raced is None:
            require_completed(written, "open work folder")
        else:
            return raced, False
    _append_event(
        store,
        folder_id=folder_id,
        ordinal=1,
        kind=KIND_OPENED,
        detail={
            "bound": [bound_kind, bound_ref],
            "harvest_kind": harvest_kind,
            "owner": owner,
            "purpose": purpose,
        },
        actor=actor,
        recorded_at=recorded_at,
    )
    return _load(store, folder_id), True


def correct(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    actor: Actor,
    harvest_kind: str | None = None,
    owner: str | None = None,
    purpose: str | None = None,
    reason: str = "",
    recorded_at: datetime | None = None,
) -> Row:
    """Correct harvest_kind, owner and purpose of a folder that is not deleted.

    A field left ``None`` is unchanged. A call that changes nothing raises
    :class:`InvalidFieldError`. The event keeps every changed field's old and
    new value.
    """
    if harvest_kind is not None:
        _check_harvest_kind(harvest_kind)
    row = _load(store, folder_id)
    _guard(row, expected_version, None)
    wanted = {COL_HARVEST_KIND: harvest_kind, COL_OWNER: owner, COL_PURPOSE: purpose}
    changes = {column: value for column, value in wanted.items() if value is not None and value != row[column]}
    if not changes:
        raise InvalidFieldError(f"correcting work folder {folder_id} would change nothing")
    return _commit(
        store,
        row,
        kind=KIND_CORRECTED,
        updates=changes,
        detail={"changes": {column: [row[column], value] for column, value in changes.items()}, "reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def close(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    actor: Actor,
    reason: str = "",
    recorded_at: datetime | None = None,
) -> Row:
    """The creator's work is over: open becomes due."""
    return _move(
        store,
        folder_id=folder_id,
        expected_version=expected_version,
        allowed_from=frozenset({STATE_OPEN}),
        kind=KIND_CLOSED,
        new_state=STATE_DUE,
        extra_updates={},
        detail={"reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def harvest_record(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    receipt: Mapping[str, object] | None,
    actor: Actor,
    reason: str = "",
    recorded_at: datetime | None = None,
) -> Row:
    """Record what was kept: due becomes harvested.

    ``receipt`` is a non-empty mapping, stored as JSON text. It may be ``None``
    only for a regenerable folder, which keeps nothing; any other folder
    without a receipt raises :class:`InvalidFieldError`.
    """
    row = _load(store, folder_id)
    _guard(row, expected_version, frozenset({STATE_DUE}))
    if receipt is None:
        if row[COL_HARVEST_KIND] != HARVEST_REGENERABLE:
            raise InvalidFieldError(
                f"work folder {folder_id} is {row[COL_HARVEST_KIND]}; harvesting it needs a receipt",
            )
        receipt_text = ""
    else:
        if not receipt:
            raise InvalidFieldError("a harvest receipt must not be empty")
        try:
            receipt_text = json.dumps(dict(receipt), sort_keys=True)
        except (TypeError, ValueError) as error:
            raise InvalidFieldError(f"the harvest receipt is not JSON-serializable: {error}") from error
    return _commit(
        store,
        row,
        kind=KIND_HARVESTED,
        updates={COL_STATE: STATE_HARVESTED, COL_HARVEST_RECEIPT: receipt_text},
        detail={"receipt": None if receipt is None else dict(receipt), "reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def reopen(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    actor: Actor,
    reason: str = "",
    recorded_at: datetime | None = None,
) -> Row:
    """More work arrived: due or harvested becomes open and the current receipt is cleared.

    The harvested event keeps the receipt, so the next close needs a fresh harvest.
    """
    return _move(
        store,
        folder_id=folder_id,
        expected_version=expected_version,
        allowed_from=frozenset({STATE_DUE, STATE_HARVESTED}),
        kind=KIND_REOPENED,
        new_state=STATE_OPEN,
        extra_updates={COL_HARVEST_RECEIPT: ""},
        detail={"reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def retire(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    actor: Actor,
    reason: str = "",
    recorded_at: datetime | None = None,
) -> Row:
    """Keep the folder: open, due or harvested becomes retired and the sweep skips it."""
    return _move(
        store,
        folder_id=folder_id,
        expected_version=expected_version,
        allowed_from=frozenset({STATE_OPEN, STATE_DUE, STATE_HARVESTED}),
        kind=KIND_RETIRED,
        new_state=STATE_RETIRED,
        extra_updates={},
        detail={"reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def restore(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    actor: Actor,
    reason: str = "",
    recorded_at: datetime | None = None,
) -> Row:
    """Take a retired folder back into use: retired becomes open."""
    return _move(
        store,
        folder_id=folder_id,
        expected_version=expected_version,
        allowed_from=frozenset({STATE_RETIRED}),
        kind=KIND_RESTORED,
        new_state=STATE_OPEN,
        extra_updates={},
        detail={"reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def mark_deleted(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    reason: str,
    actor: Actor,
    recorded_at: datetime | None = None,
) -> Row:
    """Record that the folder is gone (``reason`` removed or absent): harvested becomes deleted.

    Meant for the sweep. Refuses a folder that has no custody (neither
    regenerable nor a receipt), so a folder is never recorded as removed
    without its receipt.
    """
    row = _load(store, folder_id)
    _guard(row, expected_version, frozenset({STATE_HARVESTED}))
    if not has_custody(row):
        raise InvalidTransitionError(f"work folder {folder_id} has no harvest receipt and is not regenerable")
    return _commit(
        store,
        row,
        kind=KIND_DELETED,
        updates={COL_STATE: STATE_DELETED},
        detail={"reason": reason},
        actor=actor,
        recorded_at=recorded_at,
    )


def record_removal_failed(
    store: WorkFolderStore,
    *,
    folder_id: str,
    expected_version: int,
    error: str,
    actor: Actor,
    recorded_at: datetime | None = None,
) -> Row:
    """Record a removal that did not finish; the state stays harvested, the version moves on."""
    return _move(
        store,
        folder_id=folder_id,
        expected_version=expected_version,
        allowed_from=frozenset({STATE_HARVESTED}),
        kind=KIND_REMOVAL_FAILED,
        new_state=None,
        extra_updates={},
        detail={"error": error},
        actor=actor,
        recorded_at=recorded_at,
    )


def _page(fetched: list[Row], limit: int, first_column: str) -> RowPage:
    """Trim the ``limit + 1`` fetched rows to a page and its continuation cursor."""
    rows = fetched[:limit]
    if len(fetched) <= limit:
        return RowPage(rows=rows, next_after=None)
    last = rows[-1]
    first = last[first_column]
    return RowPage(
        rows=rows,
        next_after=(first if isinstance(first, int) else str(first), str(last[COL_FOLDER_ID])),
    )


def _state_filter(state: str | Sequence[str]) -> str | list[str]:
    """One known state, or a non-empty list of known states, as a query filter value."""
    wanted = [state] if isinstance(state, str) else list(state)
    if not wanted or any(value not in STATES for value in wanted):
        raise InvalidFieldError(f"state must be one or more of {STATES}, got {state!r}")
    return state if isinstance(state, str) else wanted


def list_folders(
    store: WorkFolderStore,
    *,
    parent_dir: str | None = None,
    bound_kind: str | None = None,
    bound_ref: str | None = None,
    state: str | Sequence[str] | None = None,
    limit: int = MAX_PAGE_LIMIT,
    after: Cursor | None = None,
) -> RowPage:
    """Work folders ordered by (path, folder_id), one bounded page at a time.

    Filters are exact: ``parent_dir``, the bound pair, and ``state`` (one state
    or a set). ``after`` is the ``next_after`` of the previous page.
    """
    _check_limit(limit)
    exact = {COL_PARENT_DIR: parent_dir, COL_BOUND_KIND: bound_kind, COL_BOUND_REF: bound_ref}
    filters: dict[str, object] = {column: value for column, value in exact.items() if value is not None}
    if state is not None:
        filters[COL_STATE] = _state_filter(state)
    query: dict[str, object] = {
        "table": TABLE_WORK_FOLDER,
        "filters": filters,
        "order_by": [[COL_PATH, "asc"], [COL_FOLDER_ID, "asc"]],
        "limit": limit + 1,
        "unbounded": True,
    }
    if after is not None:
        query["after"] = list(after)
    fetched = require_records(store.query_ordered(WORK_FOLDER_NAMESPACE, query))
    return _page(fetched, limit, COL_PATH)


def history(
    store: WorkFolderStore,
    *,
    folder_id: str,
    limit: int = MAX_PAGE_LIMIT,
    after: Cursor | None = None,
) -> RowPage:
    """Events of one folder, oldest first, one bounded page at a time.

    ``after`` is the ``next_after`` of the previous page. An unknown folder
    raises :class:`UnknownFolderError`.
    """
    _check_limit(limit)
    _load(store, folder_id)
    query: dict[str, object] = {
        "table": TABLE_WORK_FOLDER_EVENT,
        "filters": {COL_FOLDER_ID: folder_id},
        "order_by": [[COL_ORDINAL, "asc"], [COL_FOLDER_ID, "asc"]],
        "limit": limit + 1,
        "unbounded": True,
    }
    if after is not None:
        query["after"] = list(after)
    fetched = require_records(store.query_ordered(WORK_FOLDER_NAMESPACE, query))
    return _page(fetched, limit, COL_ORDINAL)
