"""The solet's work-folder verbs, as a mixin of the lifecycle service.

``open_work_folder``, ``read_work_folders``, ``transition_work_folder`` and
``sweep_work_folders`` make the work-folder store (:mod:`work_folder_records`) and
its sweep (:mod:`work_folder_sweep`) callable over the bridge, so a creator that
runs outside the solet, a mirror of the records and the scheduled sweep duty reach
the same records the solet keeps. Each verb is a thin envelope around one store
operation: arguments are checked, the store call is made, and the store's typed
refusal (``stale_version``, ``unknown_folder``, ``invalid_transition``,
``invalid_path``, ``invalid_field``) comes back as the error envelope's code,
never as an empty result. The code is the store error's own ``code``, so no
second table of codes exists here.

The store is the orchestrator's ``state_service``, reached the way the other core
services reach it. ``LifecycleManagementService`` lists this class before
``LifecycleManagementAPI`` so these concrete methods satisfy its abstract verbs.
The actor is recorded for the history and never used to refuse.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from ananta.core.domain.enums import ActionStatus
from ananta.services.lifecycle_management_service import work_folder_records as records
from ananta.services.lifecycle_management_service import work_folder_sweep
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
    COL_PATH,
    COL_PRIOR_VERSION,
    COL_PURPOSE,
    COL_RECORDED_AT,
    COL_STATE,
    COL_VERSION,
    HARVEST_REGENERABLE,
)

_ROW_COLUMNS = (
    COL_FOLDER_ID,
    COL_PATH,
    COL_STATE,
    COL_VERSION,
    COL_DEV,
    COL_INO,
    COL_PURPOSE,
    COL_BOUND_KIND,
    COL_BOUND_REF,
    COL_HARVEST_KIND,
    COL_OWNER,
    COL_HARVEST_RECEIPT,
)
_EVENT_COLUMNS = (
    COL_FOLDER_ID,
    COL_ORDINAL,
    COL_KIND,
    COL_PRIOR_VERSION,
    COL_ACTOR_INSTANCE,
    COL_ACTOR_SESSION,
    COL_ACTOR_MODEL,
    COL_ACTOR_EFFORT,
    COL_DETAIL,
    COL_RECORDED_AT,
)
_READ_MODES = ("show", "list", "history")
_DEFAULT_LIMIT = 50
_CURSOR_SEPARATOR = "|"
_MOVES: dict[str, Callable[..., records.Row]] = {
    "close": records.close,
    "reopen": records.reopen,
    "retire": records.retire,
    "restore": records.restore,
}
_ACTIONS = (*_MOVES, "correct", "harvest_record")

Envelope = dict[str, Any]


def _ok(data: dict[str, Any]) -> Envelope:
    return {"action_status": ActionStatus.COMPLETED.value, "data": data, "actions": []}


def _error(code: str, message: str) -> Envelope:
    """Error envelope that names the refusal code, never a silent empty result."""
    return {
        "action_status": ActionStatus.ERROR.value,
        "error": f"{code}: {message}",
        "data": {"error_code": code},
        "actions": [],
    }


def _refusal(error: records.WorkFolderError) -> Envelope:
    return _error(error.code, str(error))


def _actor(instance: str, session: str, model: str, effort: str) -> records.Actor:
    return records.Actor(instance=instance, session=session, model=model, effort=effort)


def _row(row: records.Row, advisories: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """Project a stored row to the contract fields, with the advisories this call has for the caller."""
    out: dict[str, Any] = {name: row[name] for name in _ROW_COLUMNS}
    out["advisories"] = [] if advisories is None else advisories
    return out


def _event(row: records.Row) -> dict[str, Any]:
    return {name: row[name] for name in _EVENT_COLUMNS}


def _identity_advisories(row: records.Row) -> list[dict[str, str]]:
    """One advisory when the directory at the recorded path is no longer the one recorded.

    The store returns a live row unchanged and leaves this comparison to the
    caller. The sweep keeps such a row, so the creator is told now instead of
    finding the folder kept later; the record itself is not rewritten.
    """
    path = str(row[COL_PATH])
    try:
        info = os.lstat(path)
        found: tuple[str, str] | None = (str(info.st_dev), str(info.st_ino))
    except OSError:
        found = None
    if found == (str(row[COL_DEV]), str(row[COL_INO])):
        return []
    message = f"{path} is not the directory that was recorded (dev and ino differ, or it cannot be read); the record is unchanged and the sweep will keep the directory"
    return [{"code": "identity_changed", "message": message, "folder_id": str(row[COL_FOLDER_ID]), "path": path}]


def _split_bound(bound: str) -> tuple[str, str] | Envelope:
    kind, separator, reference = bound.partition(":")
    if not separator or not kind or not reference:
        return _error("invalid_argument", f"bound must be 'kind:ref', got {bound!r}")
    return kind, reference


def _encode_cursor(cursor: records.Cursor | None) -> str | None:
    """The opaque ``next`` token: the store cursor joined by one separator."""
    return None if cursor is None else f"{cursor[0]}{_CURSOR_SEPARATOR}{cursor[1]}"


def _parse_cursor(after: str | None, *, ordinal: bool) -> records.Cursor | None | Envelope:
    """Decode an ``after`` token; a history cursor leads with an ordinal, a list cursor with a path.

    The folder id is the last part and never holds the separator, so a path that does is still split correctly.
    """
    if after is None:
        return None
    first, separator, folder_id = after.rpartition(_CURSOR_SEPARATOR)
    if not separator or not first or not folder_id or (ordinal and not first.isdecimal()):
        return _error("invalid_argument", f"after must be a 'next' value from a previous page, got {after!r}")
    return (int(first) if ordinal else first, folder_id)


def _parse_receipt(receipt_json: str | None) -> dict[str, object] | None:
    """The harvest receipt as a mapping, or ``None`` when none was given; anything else is ``invalid_field``."""
    if receipt_json is None:
        return None
    try:
        parsed: Any = json.loads(receipt_json)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise records.InvalidFieldError("receipt_json must be a JSON object")
    receipt: dict[str, object] = dict(parsed)
    return receipt


def _page_envelope(page: records.RowPage, project: Callable[[records.Row], dict[str, Any]]) -> Envelope:
    return _ok({"folders": [project(row) for row in page.rows], "next": _encode_cursor(page.next_after)})


def _read_one(store: records.WorkFolderStore, folder_id: str | None) -> Envelope:
    if not folder_id:
        return _error("invalid_argument", "folder_id is required for mode 'show'")
    row = records.get(store, folder_id=folder_id)
    if row is None:
        raise records.UnknownFolderError(f"no work-folder record {folder_id!r}")
    return _ok({"folders": [_row(row)], "next": None})


def _read_history(store: records.WorkFolderStore, folder_id: str | None, limit: int, after: str | None) -> Envelope:
    if not folder_id:
        return _error("invalid_argument", "folder_id is required for mode 'history'")
    cursor = _parse_cursor(after, ordinal=True)
    if isinstance(cursor, dict):
        return cursor
    return _page_envelope(records.history(store, folder_id=folder_id, limit=limit, after=cursor), _event)


def _read_list(store: records.WorkFolderStore, parent_dir: str | None, bound: str | None, state_filter: str | None, limit: int, after: str | None) -> Envelope:
    pair: tuple[str, str] | None = None
    if bound is not None:
        split = _split_bound(bound)
        if isinstance(split, dict):
            return split
        pair = split
    cursor = _parse_cursor(after, ordinal=False)
    if isinstance(cursor, dict):
        return cursor
    page = records.list_folders(
        store,
        parent_dir=parent_dir,
        bound_kind=None if pair is None else pair[0],
        bound_ref=None if pair is None else pair[1],
        state=state_filter,
        limit=limit,
        after=cursor,
    )
    return _page_envelope(page, _row)


class WorkFolderVerbs:
    """Concrete work-folder verbs for ``LifecycleManagementService``."""

    _orchestrator: Any

    def open_work_folder(
        self,
        path: str,
        purpose: str,
        bound: str,
        harvest: str = HARVEST_REGENERABLE,
        owner: str = "",
        agent_instance_id: str = "",
        agent_session_id: str = "",
        actor_model: str = "",
        actor_effort: str = "",
    ) -> Envelope:
        """Record an existing directory as a work folder; idempotent for a live path."""
        store = self._work_folder_store()
        if isinstance(store, dict):
            return store
        pair = _split_bound(bound)
        if isinstance(pair, dict):
            return pair
        try:
            row, created = records.open_folder(
                store,
                path=path,
                purpose=purpose,
                bound_kind=pair[0],
                bound_ref=pair[1],
                harvest_kind=harvest,
                owner=owner,
                actor=_actor(agent_instance_id, agent_session_id, actor_model, actor_effort),
            )
        except records.WorkFolderError as error:
            return _refusal(error)
        return _ok(_row(row, [] if created else _identity_advisories(row)))

    def read_work_folders(
        self,
        mode: str,
        folder_id: str | None = None,
        parent_dir: str | None = None,
        bound: str | None = None,
        state_filter: str | None = None,
        limit: int = _DEFAULT_LIMIT,
        after: str | None = None,
    ) -> Envelope:
        """Read work folders: one by id, a filtered page of them, or the event history of one."""
        if mode not in _READ_MODES:
            return _error("invalid_argument", f"mode must be one of {list(_READ_MODES)}, got {mode!r}")
        store = self._work_folder_store()
        if isinstance(store, dict):
            return store
        try:
            if mode == "show":
                return _read_one(store, folder_id)
            if mode == "history":
                return _read_history(store, folder_id, limit, after)
            return _read_list(store, parent_dir, bound, state_filter, limit, after)
        except records.WorkFolderError as error:
            return _refusal(error)

    def transition_work_folder(
        self,
        folder_id: str,
        action: str,
        expected_version: int,
        reason: str = "",
        harvest_kind: str | None = None,
        owner: str | None = None,
        receipt_json: str | None = None,
        agent_instance_id: str = "",
        agent_session_id: str = "",
        actor_model: str = "",
        actor_effort: str = "",
    ) -> Envelope:
        """Move one work folder through its life, or correct its record, by compare-and-set on its version."""
        if action not in _ACTIONS:
            return _error("invalid_argument", f"action must be one of {list(_ACTIONS)}, got {action!r}")
        store = self._work_folder_store()
        if isinstance(store, dict):
            return store
        actor = _actor(agent_instance_id, agent_session_id, actor_model, actor_effort)
        try:
            if action == "correct":
                row = records.correct(store, folder_id=folder_id, expected_version=expected_version, actor=actor, harvest_kind=harvest_kind, owner=owner, reason=reason)
            elif action == "harvest_record":
                row = records.harvest_record(store, folder_id=folder_id, expected_version=expected_version, receipt=_parse_receipt(receipt_json), actor=actor, reason=reason)
            else:
                row = _MOVES[action](store, folder_id=folder_id, expected_version=expected_version, actor=actor, reason=reason)
        except records.WorkFolderError as error:
            return _refusal(error)
        return _ok(_row(row))

    def sweep_work_folders(self, apply: bool = False, limit: int = work_folder_sweep.DEFAULT_LIMIT, folder_id: str | None = None) -> Envelope:
        """Sweep the finished work folders, or only the one named by folder_id; a dry run (the default) lists and removes nothing."""
        store = self._work_folder_store()
        if isinstance(store, dict):
            return store
        try:
            result = work_folder_sweep.sweep(store, apply=apply, now=datetime.now(UTC), limit=limit, folder_id=folder_id)
        except records.WorkFolderError as error:
            return _refusal(error)
        return _ok(dict(result))

    def _work_folder_store(self) -> records.WorkFolderStore | Envelope:
        """The state service as the work-folder store, or an error envelope."""
        state = self._orchestrator.get_service("state_service")
        if not isinstance(state, records.WorkFolderStore):
            return _error("state_service_unavailable", "the state service is not available to record, read or sweep work folders")
        return state
