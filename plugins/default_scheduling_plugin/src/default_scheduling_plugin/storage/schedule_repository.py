"""Scheduling-owned enumeration and revision-serialized lifecycle transactions."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import RLock
from typing import Any
from uuid import uuid4

from ananta.interfaces.state_management_interface import StateTransaction
from ananta.interfaces.state_service_protocol import StateServiceProtocol

from ..action_target import ScheduleConflictError
from ..binding_history import append_history, decoded_history, history_record, records_from, row_cursor
from ..models import ScheduleData

RELOAD_SAFE = True
_PAGE_SIZE = 100
_INTERNAL_ACTOR = "component:default_scheduling_plugin"


@dataclass
class ScheduleSnapshot:
    """An exhausted page walk is complete only at an unchanged registry epoch."""

    schedules: dict[str, ScheduleData] = field(default_factory=dict)
    complete: bool = False
    advisories: list[str] = field(default_factory=list)
    epoch_before: int | None = None
    epoch_after: int | None = None


class ScheduleRepository:
    """Use one typed transaction for current state, immutable history and epoch.

    Memory mode is explicit and shares row locks with its dispatch callbacks.
    Persistent failures propagate; they never select the memory store.
    """

    def __init__(self, state_service: StateServiceProtocol | None, namespace: str, logger: logging.Logger | None = None, *, in_memory: bool = False) -> None:
        self.state_service = state_service
        self.namespace = namespace
        self.logger = logger
        self.in_memory = in_memory
        self.memory_schedules: dict[str, ScheduleData] = {}
        self._history: list[dict[str, Any]] = []
        self._memory_epoch = 0
        self._memory_lock = RLock()
        self._row_locks: dict[str, RLock] = {}

    def _state(self) -> StateServiceProtocol:
        if self.state_service is None:
            raise RuntimeError("schedule_store_unavailable")
        return self.state_service

    def initialize_registry(self) -> None:
        """Called after schema installation; never reset an existing epoch."""
        if self.in_memory:
            return
        result = self._state().upsert_state(
            self.namespace,
            {
                "table": "registry_metadata",
                "record": {"key": "registry", "epoch": 0},
                "conflict_columns": ["key"],
                "on_conflict": "do_nothing",
            },
        )
        if result.get("action_status") != "completed":
            raise RuntimeError("registry_initialization_unavailable")

    @staticmethod
    def _deserialize_record(record: dict[str, Any]) -> dict[str, Any]:
        result = dict(record)
        for key in ("tags", "actions", "action_parameters"):
            if isinstance(result.get(key), str):
                result[key] = json.loads(result[key])
        return result

    def epoch(self) -> int:
        if self.in_memory:
            with self._memory_lock:
                return self._memory_epoch
        rows = records_from(self._state().read_state(self.namespace, {"table": "registry_metadata", "filters": {"key": "registry"}, "limit": 1}))
        if len(rows) != 1 or type(rows[0].get("epoch")) is not int:
            raise RuntimeError("registry_epoch_unavailable")
        return rows[0]["epoch"]

    def load_schedule(self, schedule_id: str) -> ScheduleData | None:
        if self.in_memory:
            with self._memory_lock:
                row = self.memory_schedules.get(schedule_id)
                return row.model_copy(deep=True) if row else None
        rows = records_from(self._state().read_state(self.namespace, {"table": "schedules", "filters": {"id": schedule_id, "is_deleted": 0}, "limit": 1}))
        return ScheduleData(**self._deserialize_record(rows[0])) if rows else None

    def _walk(self, snapshot: ScheduleSnapshot) -> None:
        after: list[object] | None = None
        previous: tuple[datetime, str] | None = None
        while True:
            query: dict[str, object] = {"table": "schedules", "filters": {}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": _PAGE_SIZE, "include_deleted": False}
            if after is not None:
                query["after"] = after
            rows = records_from(self._state().query_ordered(self.namespace, query))
            if len(rows) > _PAGE_SIZE:
                raise ValueError("oversized_page")
            for row in rows:
                cursor = row_cursor(row)
                if previous is not None and cursor <= previous:
                    raise ValueError("nonprogressing_cursor")
                previous = cursor
                schedule = ScheduleData(**self._deserialize_record(row))
                if not schedule.id:
                    raise ValueError("schedule_identity_missing")
                snapshot.schedules[schedule.id] = schedule
            if len(rows) < _PAGE_SIZE:
                return
            after = [rows[-1]["created_at"], rows[-1]["id"]]

    def load_all_schedules(self) -> ScheduleSnapshot:
        if self.in_memory:
            with self._memory_lock:
                return ScheduleSnapshot(deepcopy(self.memory_schedules), True, [], self._memory_epoch, self._memory_epoch)
        snapshot = ScheduleSnapshot()
        try:
            snapshot.epoch_before = self.epoch()
        except Exception as exc:
            snapshot.advisories.append(f"epoch_before_unavailable:{exc}")
        try:
            self._walk(snapshot)
        except Exception as exc:
            snapshot.advisories.append(f"page_failure:{exc}")
        try:
            snapshot.epoch_after = self.epoch()
        except Exception as exc:
            snapshot.advisories.append(f"epoch_after_unavailable:{exc}")
        if snapshot.epoch_before != snapshot.epoch_after:
            snapshot.advisories.append("concurrent_change")
        snapshot.complete = not snapshot.advisories and snapshot.epoch_before is not None
        return snapshot

    def _row_lock(self, schedule_id: str) -> RLock:
        with self._memory_lock:
            return self._row_locks.setdefault(schedule_id, RLock())

    def _memory_event(self, before: ScheduleData | None, after: ScheduleData | None, operation: str, actor: str, reason: str) -> None:
        event = history_record(before, after, operation, actor, reason)
        event.update(id="sbh-" + uuid4().hex, created_at=datetime.now(UTC).isoformat())
        self._history.append(event)
        self._memory_epoch += 1

    def save_schedule(self, schedule: ScheduleData, *, actor: str = _INTERNAL_ACTOR) -> str:
        if self.in_memory:
            saved = schedule.model_copy(deep=True)
            saved.id = saved.id or "sch-" + uuid4().hex
            with self._row_lock(saved.id), self._memory_lock:
                if saved.id in self.memory_schedules:
                    raise ScheduleConflictError("schedule_already_exists")
                self._memory_event(None, saved, "create", actor, "Created schedule")
                self.memory_schedules[saved.id] = saved
            return saved.id
        state = self._state()
        record = state.prepare_record(self.namespace, "schedules", schedule.model_dump())
        saved = ScheduleData.model_validate(record)
        with state.transactional() as transaction:
            schedule_id = transaction.write_state(self.namespace, {"table": "schedules", "record": record})
            saved.id = schedule_id
            append_history(state, transaction, self.namespace, None, saved, "create", actor, "Created schedule")
        return schedule_id

    def _read_in(self, transaction: StateTransaction, schedule_id: str) -> ScheduleData | None:
        rows = transaction.query_state(self.namespace, {"table": "schedules", "filters": {"id": schedule_id, "is_deleted": 0}, "limit": 1})
        return ScheduleData(**self._deserialize_record(rows[0])) if rows else None

    @staticmethod
    def _check_revision(current: ScheduleData | None, expected_revision: int) -> ScheduleData:
        if current is None:
            raise ScheduleConflictError("schedule_not_found")
        if type(expected_revision) is not int or current.revision != expected_revision:
            raise ScheduleConflictError("stale_revision")
        return current

    def _write_change(self, transaction: StateTransaction | None, before: ScheduleData, after: ScheduleData | None, operation: str, actor: str, reason: str) -> None:
        if after is not None:
            after.revision = before.revision + 1
        if transaction is None:
            with self._memory_lock:
                self._memory_event(before, after, operation, actor, reason)
                if after is None:
                    self.memory_schedules.pop(str(before.id), None)
                else:
                    self.memory_schedules[str(before.id)] = after.model_copy(deep=True)
            return
        filters = {"id": before.id, "revision": before.revision, "status": before.status}
        if after is None:
            affected = transaction.delete_records(self.namespace, {"table": "schedules", "filters": filters, "soft_delete": False})
        else:
            updates = {key: value for key, value in after.model_dump().items() if value != before.model_dump().get(key)}
            affected = transaction.update_state(self.namespace, {"table": "schedules", "filters": filters}, updates)
        if affected != 1:
            raise ScheduleConflictError("stale_revision")
        append_history(self._state(), transaction, self.namespace, before, after, operation, actor, reason)

    def mutate(self, schedule_id: str, expected_revision: int, transform: Callable[[ScheduleData], ScheduleData | None], *, operation: str, actor: str, reason: str) -> ScheduleData | None:
        if self.in_memory:
            with self._row_lock(schedule_id), self._memory_lock:
                before = self._check_revision(self.load_schedule(schedule_id), expected_revision)
                after = transform(before.model_copy(deep=True))
                history_length, epoch = len(self._history), self._memory_epoch
                try:
                    self._write_change(None, before, after, operation, actor, reason)
                except Exception:
                    self.memory_schedules[schedule_id] = before
                    del self._history[history_length:]
                    self._memory_epoch = epoch
                    raise
                return after
        with self._state().transactional() as transaction:
            before = self._check_revision(self._read_in(transaction, schedule_id), expected_revision)
            after = transform(before.model_copy(deep=True))
            self._write_change(transaction, before, after, operation, actor, reason)
            return after

    @contextmanager
    def dispatch_row(self, schedule_id: str) -> Generator[tuple[ScheduleData | None, StateTransaction | None]]:
        """Keep the authoritative row locked through synchronous queue submission."""
        if self.in_memory:
            with self._row_lock(schedule_id):
                current = self.load_schedule(schedule_id)
                yield (current if current and current.status == "scheduled" else None), None
            return
        while True:
            with self._state().transactional() as transaction:
                current = self._read_in(transaction, schedule_id)
                if current is None or current.status != "scheduled":
                    yield None, transaction
                    return
                affected = transaction.update_state(self.namespace, {"table": "schedules", "filters": {"id": schedule_id, "revision": current.revision, "status": "scheduled"}}, {"revision": current.revision})
                if affected == 1:
                    locked = self._read_in(transaction, schedule_id)
                    yield locked, transaction
                    return

    def finish_dispatch(self, before: ScheduleData, transaction: StateTransaction | None, success: bool, error: str | None) -> None:
        if success and before.type == "recurring":
            return
        after = None if success else before.model_copy(deep=True)
        if after is not None:
            after.status, after.error_message = "error", error
        self._write_change(transaction, before, after, "complete" if success else "error", _INTERNAL_ACTOR, f"Queue submission at revision {before.revision}: {error or 'completed'}")

    def update_schedule_status(self, schedule_id: str, status: str, error_message: str | None = None, *, expected_revision: int | None = None) -> bool:
        current = self.load_schedule(schedule_id)
        if current is None:
            return False

        def change(row: ScheduleData) -> ScheduleData:
            row.status, row.error_message = status, error_message
            return row

        self.mutate(schedule_id, current.revision if expected_revision is None else expected_revision, change, operation="status", actor=_INTERNAL_ACTOR, reason=f"Status changed to {status}")
        return True

    def delete_schedule(self, schedule_id: str, *, operation: str = "clear", actor: str = _INTERNAL_ACTOR, reason: str = "Schedule cleared") -> bool:
        while True:
            current = self.load_schedule(schedule_id)
            if current is None:
                return False
            try:
                self.mutate(schedule_id, current.revision, lambda _row: None, operation=operation, actor=actor, reason=reason)
                return True
            except ScheduleConflictError as exc:
                if exc.code not in ("stale_revision", "schedule_not_found"):
                    raise

    def delete_schedules_by_tag(self, tag: str) -> int:
        snapshot = self.load_all_schedules()
        if not snapshot.complete:
            if self.logger:
                self.logger.warning("Incomplete clear source; only observed IDs processed: %s", snapshot.advisories)
        return sum(self.delete_schedule(sid) for sid, row in snapshot.schedules.items() if tag in row.tags)

    def cleanup_completed_one_time_schedules(self) -> int:
        snapshot = self.load_all_schedules()
        if not snapshot.complete:
            if self.logger:
                self.logger.warning("Incomplete cleanup source; only observed IDs processed: %s", snapshot.advisories)
        return sum(self.delete_schedule(sid, operation="complete") for sid, row in snapshot.schedules.items() if row.type == "one_time" and row.status == "completed")

    def _history_rows(self, schedule_id: str, limit: int, after: list[object] | None) -> list[dict[str, Any]]:
        if self.in_memory:
            with self._memory_lock:
                rows = sorted((deepcopy(row) for row in self._history if row["schedule_id"] == schedule_id), key=row_cursor)
            if after is not None:
                cursor = row_cursor({"created_at": after[0], "id": after[1]})
                rows = [row for row in rows if row_cursor(row) > cursor]
            return rows[:limit]
        query: dict[str, object] = {"table": "binding_history", "filters": {"schedule_id": schedule_id}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": limit, "include_deleted": False}
        if after is not None:
            query["after"] = after
        return records_from(self._state().query_ordered(self.namespace, query))

    def history(self, schedule_id: str, limit: int = 100, after: list[object] | None = None) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("history_limit_must_be_1_to_100")
        if after is not None:
            if len(after) != 2:
                raise ValueError("malformed_history_cursor")
            row_cursor({"created_at": after[0], "id": after[1]})
        rows = self._history_rows(schedule_id, limit, after)
        return {"schedule_id": schedule_id, "events": [decoded_history(row) for row in rows], "next_cursor": [str(rows[-1]["created_at"]), rows[-1]["id"]] if len(rows) == limit else None}
