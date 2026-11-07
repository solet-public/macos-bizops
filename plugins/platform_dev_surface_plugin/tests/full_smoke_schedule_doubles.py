"""Test doubles for full smoke scheduling and register readback."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from default_scheduling_plugin.plugin import SchedulingPlugin
from platform_dev_surface_plugin.quality.battery_register import BatteryRegister


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
        if self.corrupt_tag in row["tags"]:
            row.update(deepcopy(self.created_override))
        self.rows.append(row)
        if self.duplicate and self.corrupt_tag in row["tags"]:
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


class RegisterDouble:
    def __init__(self) -> None:
        self.busy = False
        self.problem: dict[str, str] | None = None
        self.filings = 0
        self.lose_response = False
        self.available = True

    def controller_busy(self) -> dict[str, Any]:
        return {"busy": self.busy}

    def landings(self, _since: str | None) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError("register attribution unavailable")
        return {"landings": [{"landing_id": "lnd_fixture"}], "change_ids": ["chg_fixture"]}

    def reconcile(self, _marker: str) -> dict[str, str] | None:
        if not self.available:
            raise RuntimeError("register unavailable")
        return self.problem

    def file_problem(self, _marker: str, _detail: str) -> dict[str, str]:
        self.filings += 1
        self.problem = {"issue_id": "iss_fixture", "problem_id": "prb_fixture"}
        if self.lose_response:
            self.lose_response = False
            raise RuntimeError("commit succeeded; response lost")
        return self.problem

    def attribute_problem(self, _problem: dict[str, str], _marker: str, _detail: str, changes: list[str]) -> dict[str, Any]:
        return {"issue_evidence": "iev_fixture", "change_evidence": [{"change_id": change, "event_id": "cev_fixture"} for change in changes]}


class NativeRegisterFixture(BatteryRegister):
    """Only the native command boundary is replaced; real paging/guards run."""

    def __init__(self) -> None:
        super().__init__("fixture-solet", "fixture-instance")
        self.calls: list[tuple[str, ...]] = []
        self.change_written = False
        self.linked = False
        self.issue_exists = True

    def _call(self, *arguments: str) -> Any:
        self.calls.append(arguments)
        prefix = arguments[:3]
        fixed = self._fixed_response(prefix)
        if fixed is not None:
            return fixed
        if arguments[:1] == ("filed",):
            self.issue_exists = True
            return {"issue_id": "iss_fixture", "problem_id": "prb_fixture"}
        if prefix == ("db", "change", "events"):
            return self._change_events_response(arguments)
        if prefix == ("db", "change", "event"):
            self.change_written = True
            return {"event_id": "cev_fixture"}
        if arguments[:4] == ("db", "problem", "relation", "show"):
            return self._relation_response()
        if arguments[:4] == ("db", "problem", "relation", "add"):
            self.linked = True
            return {"relation_id": "pfr_fixture", "version": 4}
        raise AssertionError(f"unexpected native call: {arguments}")

    def _fixed_response(self, prefix: tuple[str, ...]) -> Any:
        fixed = {
            ("db", "landing-queue", "show"): {"entries": [{"unit_id": "unt_admitted", "eligible": True}]},
            ("db", "issue", "list"): {"items": [{"issue_id": "iss_fixture", "title": "failure campaign-marker"}] if self.issue_exists else []},
            ("db", "issue", "show"): {"history_version": 3, "parent_state": {"parent_kind": "problem", "parent_id": "prb_fixture", "state": "open"}},
            ("db", "problem", "show"): {"parent": {"id": "prb_fixture", "lifecycle_state": "open"}, "version": 3},
            ("db", "issue", "events"): {"records": [{"detail": "campaign-marker detail"}]},
        }
        return fixed.get(prefix)

    def _change_events_response(self, arguments: tuple[str, ...]) -> dict[str, Any]:
        if "--cursor" not in arguments:
            return {"records": [{"detail": "older unrelated event"}], "frontier_sequence": 2, "next_cursor": "second-page"}
        return {"records": [{"detail": "campaign-marker detail"}] if self.change_written else [], "frontier_sequence": 2, "next_cursor": None}

    def _relation_response(self) -> dict[str, Any]:
        return {"as_of_version": 4 if self.linked else 3, "next_cursor": None, "records": [{"change_id": "chg_fixture", "active": True, "kind": "investigated_by", "relation_key": "full-smoke:chg_fixture"}] if self.linked else []}
