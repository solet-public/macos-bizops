"""Sweep of the solet's own finished work folders.

The sweep reads this solet's ``work_folder`` rows and removes the directories of
the folders that are finished. A row is the only source of candidates; the
sweep never looks for folders on disk and never deletes on age.

What it does, in order:

1. Validate ``now`` (an aware datetime). An invalid value aborts the run with an
   advisory and deletes nothing. An applying run also aborts when this Python's
   ``shutil.rmtree`` cannot avoid symlink attacks.
2. Scan the ``harvested`` and ``due`` rows in (path, folder_id) order, or, given
   a ``folder_id``, look at that one row only. A ``due``
   row that is not regenerable is waiting for its harvest receipt: an advisory,
   never a removal. A ``due`` regenerable row is marked harvested first (it keeps
   nothing). A harvested row without custody (neither regenerable nor a receipt)
   is skipped with an advisory.
3. Re-check identity of each remaining row just before acting: ``lstat`` must
   show a real directory (not a symlink) whose (dev, ino) equals the recorded
   pair. An absent path is recorded deleted with reason ``absent``. A mismatch
   or a symlink is skipped with an advisory and never deleted.
4. Remove with ``shutil.rmtree`` (fd-based, it does not follow symlinks on the
   shipped Python 3.13) and record ``deleted``. A removal that raises is recorded
   as a ``removal_failed`` event and the folder stays harvested for the next run.

A dry run (the default) lists what it would do and writes nothing. At most
``limit`` folders are acted on per run; the rest are listed as ``deferred`` and
the next run takes them. A skipped folder does not count against the limit, so
a stuck row cannot starve the others. Every removal, every absent path and every
failed removal is an event. Unknown or unreadable data is an advisory, never a
refusal: the only refusals here are the two integrity aborts above.

One window is left by design: between the fresh read of a row and its removal a
creator could reopen it. The fresh read narrows that to the time of one
``lstat``; the design keeps no trash phase.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict

from ananta.services.lifecycle_management_service.work_folder_records import (
    Actor,
    Cursor,
    InvalidFieldError,
    Row,
    StaleVersionError,
    UnknownFolderError,
    WorkFolderStore,
    get,
    harvest_record,
    has_custody,
    list_folders,
    mark_deleted,
    record_removal_failed,
)
from ananta.services.lifecycle_management_service.work_folder_schema import (
    COL_DEV,
    COL_FOLDER_ID,
    COL_HARVEST_KIND,
    COL_INO,
    COL_PATH,
    COL_STATE,
    COL_VERSION,
    HARVEST_REGENERABLE,
    STATE_DUE,
    STATE_HARVESTED,
)

SWEEP_ACTOR = Actor(instance="work-folder-sweep")
DEFAULT_LIMIT = 200
_PAGE = 100

Advisory = dict[str, str]


class SweepResult(TypedDict):
    """What one sweep run found and did."""

    candidates: list[dict[str, str]]
    counts: dict[str, int]
    deferred: list[str]
    advisories: list[Advisory]
    aborted: bool


@dataclass(frozen=True, slots=True)
class _Skip:
    code: str
    message: str


_ABSENT = "absent"
_PRESENT = "present"


def _advisory(code: str, message: str, row: Row | None = None) -> Advisory:
    advisory = {"code": code, "message": message}
    if row is not None:
        advisory["folder_id"] = str(row[COL_FOLDER_ID])
        advisory["path"] = str(row[COL_PATH])
    return advisory


def _scan(store: WorkFolderStore, state: str) -> list[Row]:
    rows: list[Row] = []
    after: Cursor | None = None
    while True:
        page = list_folders(store, state=state, limit=_PAGE, after=after)
        rows.extend(page.rows)
        if page.next_after is None:
            return rows
        after = page.next_after


def _inspect(row: Row) -> str | _Skip:
    """Read-only identity check: ``_PRESENT``, ``_ABSENT`` or the reason to skip."""
    path = str(row[COL_PATH])
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return _ABSENT
    except OSError as error:
        return _Skip("inspect_failed", f"cannot inspect {path}: {error}")
    if not stat.S_ISDIR(info.st_mode):
        return _Skip("not_a_real_directory", f"{path} is no longer a real directory (symlink or file); left untouched")
    if (str(info.st_dev), str(info.st_ino)) != (str(row[COL_DEV]), str(row[COL_INO])):
        return _Skip("identity_changed", f"{path} is not the directory that was opened (dev/ino differ); left untouched")
    return _PRESENT


def _rows(store: WorkFolderStore, folder_id: str | None) -> list[Row]:
    """The rows to look at: every harvested and due row in order, or only the named row when it is harvested or due."""
    if folder_id is None:
        return sorted(
            [*_scan(store, STATE_HARVESTED), *_scan(store, STATE_DUE)],
            key=lambda row: (str(row[COL_PATH]), str(row[COL_FOLDER_ID])),
        )
    row = get(store, folder_id=folder_id)
    if row is None:
        raise UnknownFolderError(f"no work-folder record {folder_id!r}")
    return [row] if str(row[COL_STATE]) in (STATE_HARVESTED, STATE_DUE) else []


def _aborted(result: SweepResult, code: str, message: str) -> SweepResult:
    result["aborted"] = True
    result["advisories"].append(_advisory(code, message))
    return result


def _valid_now(now: object) -> datetime:
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise InvalidFieldError(f"now must be an aware datetime, got {now!r}")
    return now.astimezone(UTC)


def sweep(
    store: WorkFolderStore,
    *,
    apply: bool,
    now: datetime,
    limit: int = DEFAULT_LIMIT,
    actor: Actor = SWEEP_ACTOR,
    folder_id: str | None = None,
) -> SweepResult:
    """Run one sweep; a dry run (``apply`` False) lists and removes nothing.

    With ``folder_id`` the run looks at that one row only, with every check it
    has for the rest; an id with no record raises :class:`UnknownFolderError`.
    """
    if limit < 1:
        raise InvalidFieldError(f"limit must be at least 1, got {limit}")
    result: SweepResult = {
        "candidates": [],
        "counts": {"marked_harvested": 0, "deleted": 0, "absent": 0, "skipped": 0, "failed": 0},
        "deferred": [],
        "advisories": [],
        "aborted": False,
    }
    try:
        stamp = _valid_now(now)
    except InvalidFieldError as error:
        return _aborted(result, "invalid_now", f"{error}; nothing was listed or removed")
    if apply and not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        return _aborted(
            result,
            "rmtree_not_symlink_safe",
            "shutil.rmtree on this Python cannot avoid symlink attacks; nothing was removed",
        )
    acted = 0
    for row in _rows(store, folder_id):
        verdict = _classify(row, result)
        if verdict is None:
            continue
        if acted >= limit:
            result["deferred"].append(str(row[COL_FOLDER_ID]))
            continue
        acted += 1
        entry = {
            "folder_id": str(row[COL_FOLDER_ID]),
            "path": str(row[COL_PATH]),
            "state": str(row[COL_STATE]),
            "action": verdict,
        }
        result["candidates"].append(entry)
        if not apply:
            entry["outcome"] = "would_" + verdict
            continue
        entry["outcome"] = _act(store, row, result, stamp, actor)
    return result


def _classify(row: Row, result: SweepResult) -> str | None:
    """The action a row calls for, or ``None`` after recording why it is skipped."""
    state = row[COL_STATE]
    if state == STATE_DUE and row[COL_HARVEST_KIND] != HARVEST_REGENERABLE:
        result["advisories"].append(
            _advisory("awaiting_harvest_receipt", "closed but not harvested; it is kept until a harvest receipt is recorded", row),
        )
        return None
    if state == STATE_HARVESTED and not has_custody(row):
        result["counts"]["skipped"] += 1
        result["advisories"].append(
            _advisory("no_custody", "harvested without a receipt and not regenerable; it is kept", row),
        )
        return None
    found = _inspect(row)
    if isinstance(found, _Skip):
        result["counts"]["skipped"] += 1
        result["advisories"].append(_advisory(found.code, found.message, row))
        return None
    removal = "record_absent" if found == _ABSENT else "remove"
    return f"harvest_and_{removal}" if state == STATE_DUE else removal


def _act(store: WorkFolderStore, listed: Row, result: SweepResult, stamp: datetime, actor: Actor) -> str:
    """Carry out one classified action on fresh data; return its outcome."""
    folder_id = str(listed[COL_FOLDER_ID])
    try:
        row = listed
        if row[COL_STATE] == STATE_DUE:
            row = harvest_record(
                store,
                folder_id=folder_id,
                expected_version=int(str(row[COL_VERSION])),
                receipt=None,
                actor=actor,
                reason="regenerable folder: nothing to keep",
                recorded_at=stamp,
            )
            result["counts"]["marked_harvested"] += 1
        fresh = get(store, folder_id=folder_id)
        if fresh is None or fresh[COL_VERSION] != row[COL_VERSION] or fresh[COL_STATE] != STATE_HARVESTED:
            result["counts"]["skipped"] += 1
            result["advisories"].append(_advisory("row_moved", "the record changed after it was read; left for the next run", listed))
            return "skipped"
        found = _inspect(fresh)
        if isinstance(found, _Skip):
            result["counts"]["skipped"] += 1
            result["advisories"].append(_advisory(found.code, found.message, fresh))
            return "skipped"
        version = int(str(fresh[COL_VERSION]))
        if found == _ABSENT:
            mark_deleted(store, folder_id=folder_id, expected_version=version, reason="absent", actor=actor, recorded_at=stamp)
            result["counts"]["absent"] += 1
            return "absent"
        return _remove(store, fresh, version, result, stamp, actor)
    except StaleVersionError as error:
        result["counts"]["skipped"] += 1
        result["advisories"].append(_advisory("row_moved", f"{error}; left for the next run", listed))
        return "skipped"


def _remove(store: WorkFolderStore, row: Row, version: int, result: SweepResult, stamp: datetime, actor: Actor) -> str:
    folder_id = str(row[COL_FOLDER_ID])
    try:
        shutil.rmtree(str(row[COL_PATH]))
    except OSError as error:
        record_removal_failed(store, folder_id=folder_id, expected_version=version, error=str(error), actor=actor, recorded_at=stamp)
        result["counts"]["failed"] += 1
        result["advisories"].append(_advisory("removal_failed", f"removal did not finish: {error}", row))
        return "failed"
    mark_deleted(store, folder_id=folder_id, expected_version=version, reason="removed", actor=actor, recorded_at=stamp)
    result["counts"]["deleted"] += 1
    return "deleted"
