"""Test doubles for full smoke scheduling."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from default_scheduling_plugin.plugin import SchedulingPlugin


class CapturingScheduler:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.creates = 0
        self.complete: bool | None = True
        self.created_override: dict[str, Any] = {}
        self.corrupt_tag: str | None = None
        self.duplicate = False
        self.clears: list[str] = []
        self.incomplete_after_create = False
        self.report_wrong_id = False

    def get_schedules_by_tag(self, tag: str) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"complete": self.complete, "schedules": deepcopy([row for row in self.rows if tag in row["tags"]])}}

    def create_cron_schedule(self, **kwargs: Any) -> dict[str, Any]:
        self.creates += 1
        row = {**kwargs, **kwargs["state"], "schedule_id": str(self.creates), "status": "scheduled"}
        if self.corrupt_tag is not None and self.corrupt_tag in row["tags"]:
            row.update(deepcopy(self.created_override))
        self.rows.append(row)
        if self.duplicate and self.corrupt_tag is not None and self.corrupt_tag in row["tags"]:
            self.rows.append({**deepcopy(row), "schedule_id": "duplicate-" + row["schedule_id"]})
        if self.incomplete_after_create:
            self.complete = False
        return {"data": {"schedule_id": "wrong-id" if self.report_wrong_id else row["schedule_id"]}}

    def clear_scheduled_action(self, schedule_id: str, **_kwargs: Any) -> dict[str, Any]:
        self.clears.append(schedule_id)
        self.rows = [row for row in self.rows if row["schedule_id"] != schedule_id]
        return {"data": {"cleared_count": 1}}


class MemoryScheduler:
    """Real scheduler operations and canonical projection; explicit memory storage."""

    def __init__(self) -> None:
        self.plugin = SchedulingPlugin()
        self.plugin.configure_memory_store()

    def get_schedules_by_tag(self, tag: str) -> dict[str, Any]:
        return self.plugin.get_schedules_by_tag({"tag": tag}, {})

    def create_cron_schedule(self, *, state: dict[str, Any], **params: Any) -> dict[str, Any]:
        return self.plugin.create_cron_schedule(params, state)

    def clear_scheduled_action(self, *, state: dict[str, Any], **params: Any) -> dict[str, Any]:
        return self.plugin.clear_scheduled_action(params, state)
