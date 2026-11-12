#!/usr/bin/env python3
"""Fake owner-store proof for schedule targets, completeness and serialized lifecycle.

No live service, database, host or session access. Private PostgreSQL contention
is independently exercised by schedule_target_rebind_pg_smoke.py.
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, RLock, Thread
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/default_scheduling_plugin/src"):
    sys.path.insert(0, str(source))

import default_scheduling_plugin  # noqa: E402
from ananta.interfaces.state_management_interface import StateTransaction  # noqa: E402
from ananta.interfaces.state_service_protocol import StateServiceProtocol  # noqa: E402
from default_scheduling_plugin.binding_history import row_cursor  # noqa: E402
from default_scheduling_plugin.models import ActionData, ScheduleData  # noqa: E402
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402
from default_scheduling_plugin.scheduling.scheduler_manager import SchedulerManager  # noqa: E402
from default_scheduling_plugin.storage.schedule_repository import ScheduleRepository  # noqa: E402

import ananta  # noqa: E402

NAMESPACE = "default_scheduling_plugin"
TEXT = "  original \u03bb\nsecond line\r\n"
STAMP = datetime(2026, 1, 1, tzinfo=UTC)


def check(value: object, label: str) -> None:
    assert value, label
    print("PASS", label, flush=True)


class FakeTransaction:
    def __init__(self, owner: FakeState) -> None:
        self.owner = owner

    def query_state(self, namespace: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        return self.owner.rows(namespace, query)

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        assert namespace == NAMESPACE, "reads_owner_only"
        if data["table"] == "binding_history" and self.owner.fail_history:
            raise RuntimeError("injected_history_failure")
        row = deepcopy(data["record"])
        self.owner.tables[data["table"]][row["id"]] = row
        return row["id"]

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> int:
        rows = self.owner.rows(namespace, query)
        for row in rows:
            self.owner.tables[query["table"]][row["id"]].update(deepcopy(updates))
        return len(rows)

    def delete_records(self, namespace: str, query: dict[str, Any]) -> int:
        rows = self.owner.rows(namespace, query)
        for row in rows:
            del self.owner.tables[query["table"]][row["id"]]
        return len(rows)

    def count(self, namespace: str, data: dict[str, Any]) -> int:
        return len(self.owner.rows(namespace, data))

    def increment_and_return(self, namespace: str, data: dict[str, Any]) -> int:
        row = self.owner.rows(namespace, data)[0]
        value = row[data["column"]] + data.get("by", 1)
        self.owner.tables[data["table"]][row["id"]][data["column"]] = value
        return value


class FakeState:
    """Typed fake transaction surface, with rollback and an explicit owner lock."""

    def __init__(self) -> None:
        self.tables: dict[str, dict[str, dict[str, Any]]] = {"schedules": {}, "binding_history": {}, "registry_metadata": {}}
        self.lock = RLock()
        self.serial = 0
        self.fail_history = False
        self.page_fault: str | None = None
        self.pages = 0
        self.bump_epoch = False
        self.namespaces: list[str] = []

    def rows(self, namespace: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        self.namespaces.append(namespace)
        assert namespace == NAMESPACE, "reads_owner_only"
        return [deepcopy(row) for row in self.tables[query["table"]].values() if all(row.get(key, 0 if key == "is_deleted" else None) == value for key, value in query.get("filters", {}).items())]

    def prepare_record(self, namespace: str, table_name: str, record: dict[str, Any]) -> dict[str, Any]:
        assert namespace == NAMESPACE, "reads_owner_only"
        self.serial += 1
        return {**deepcopy(record), "id": record.get("id") or f"{table_name}-{self.serial:08d}", "created_at": STAMP, "is_deleted": 0}

    def upsert_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            if not self.tables[data["table"]]:
                row = self.prepare_record(namespace, data["table"], data["record"])
                self.tables[data["table"]][row["id"]] = row
        return {"action_status": "completed", "data": {}}

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            return {"action_status": "completed", "data": {"records": self.rows(namespace, query)}}

    def _page_rows(self, namespace: str, data: dict[str, Any]) -> list[dict[str, Any]]:
        rows = sorted(self.rows(namespace, data), key=row_cursor)
        if not data.get("include_deleted", False):
            rows = [row for row in rows if not row.get("is_deleted")]
        if data.get("after") and self.page_fault != "cycle":
            cursor = row_cursor({"created_at": data["after"][0], "id": data["after"][1]})
            rows = [row for row in rows if row_cursor(row) > cursor]
        return rows[: data["limit"]]

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.pages += 1
            if data["table"] == "schedules" and self.page_fault == "late" and data.get("after"):
                return {"action_status": "error", "error": "injected_late_page"}
            rows = self._page_rows(namespace, data)
            if self.page_fault == "malformed" and rows:
                rows[-1].pop("created_at", None)
            if self.bump_epoch:
                for row in self.tables["registry_metadata"].values():
                    row["epoch"] += 1
            return {"action_status": "completed", "data": {"records": rows}}

    @contextmanager
    def transactional(self) -> Generator[StateTransaction]:
        with self.lock:
            before = deepcopy(self.tables)
            try:
                yield cast(StateTransaction, FakeTransaction(self))
            except BaseException:
                self.tables = before
                raise


def schedule(sid: str | None = None, *, kind: str = "recurring") -> ScheduleData:
    return ScheduleData(
        id=sid,
        action_name="",
        status="scheduled",
        error_message=None,
        label="preserve label",
        type=kind,
        tags=["keep", "fixture"],
        actions=[ActionData(name="plugin::agent_messaging_plugin::peer_send_by_name", parameters={"name": "old", "content": TEXT, "nested": {"untouched": [None, "\u03b2"]}}, result_processor={"process_key": "fixture::done", "arguments": {"text": "stay"}}, result_processor_kind="deterministic_continuation")],
        cron_expression="0 * * * *" if kind == "recurring" else None,
        run_at="2099-01-01T00:00:00+00:00" if kind == "one_time" else None,
        session_id="context-only",
        flow_id="flow-fixture",
    )


def fixture(*, memory: bool = False) -> tuple[SchedulingPlugin, ScheduleRepository, FakeState | None]:
    plugin = SchedulingPlugin()
    plugin.logger.disabled = True
    if memory:
        plugin.configure_memory_store()
        return plugin, plugin._memory_repository, None
    state = FakeState()
    plugin.set_state_service(cast(StateServiceProtocol, state))
    plugin._job_tracker = None
    assert plugin._repository is not None
    plugin._repository.initialize_registry()
    return plugin, plugin._repository, state


def rebind(plugin: SchedulingPlugin, sid: str, revision: int, value: str = "new") -> dict[str, Any]:
    return plugin.rebind_scheduled_action({"schedule_id": sid, "action_index": 0, "target_kind": "role", "target_value": value, "expected_revision": revision, "reason": "fixture correction", "actor": "spoofed"}, {})


class Recorder:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []
        self.entered: Event | None = None
        self.release: Event | None = None

    def submit_action_definition(self, definition: dict[str, Any], context: dict[str, Any] | None = None) -> str:
        self.actions.append(deepcopy(definition))
        if self.entered is not None and self.release is not None:
            self.entered.set()
            assert self.release.wait(5), "queue_barrier_released"
        return "fake-action"


def check_rebind() -> None:
    plugin, repo, state = fixture()
    sid = repo.save_schedule(schedule())
    before = repo.load_schedule(sid)
    assert before is not None and state is not None
    result = rebind(plugin, sid, 0)
    check(result.get("action_status") == "completed" and result["data"]["revision"] == 1, "rebind_current_revision_passes")
    after = repo.load_schedule(sid)
    assert after is not None
    expected = before.model_dump()
    expected["actions"][0]["parameters"]["name"] = "new"
    expected["revision"] = 1
    check(after.model_dump() == expected and after.actions[0].parameters["content"] == TEXT, "rebind_preserves_action_text")
    events = repo.history(sid)["events"]
    check([event["operation"] for event in events] == ["create", "rebind"] and events[-1]["actor"] == "attribution_unavailable", "rebind_appends_history")
    frozen = deepcopy(state.tables)
    stale = rebind(plugin, sid, 0, "stale")
    check(stale.get("action_status") == "error" and state.tables == frozen, "stale_revision_no_writes")
    _check_corrections(plugin, repo, state, sid, frozen)


def _check_corrections(plugin: SchedulingPlugin, repo: ScheduleRepository, state: FakeState, sid: str, frozen: dict[str, Any]) -> None:
    state.fail_history = True
    failed = rebind(plugin, sid, 1, "uncommitted")
    check(failed.get("action_status") == "error" and state.tables == frozen, "history_failure_rolls_back")
    state.fail_history = False
    check(rebind(plugin, sid, 1)["action_status"] == "completed", "same_target_correction")
    check(rebind(plugin, sid, 2, "old")["action_status"] == "completed", "rebind_reversal")
    check(cast(ScheduleData, repo.load_schedule(sid)).actions[0].parameters["name"] == "old", "reversal_readback")
    page = repo.history(sid, 2)
    check(len(page["events"]) == 2 and len(repo.history(sid, 2, page["next_cursor"])["events"]) == 2, "history_pagination")
    cleared = plugin.clear_scheduled_action({"schedule_id": sid}, {})
    check(cleared["data"]["cancelled"] and repo.load_schedule(sid) is None and repo.history(sid)["events"][-1]["operation"] == "clear", "clear_retains_history")
    check(rebind(plugin, sid, 3)["action_status"] == "error", "no_resurrection")
    ids = [repo.save_schedule(schedule()) for _ in range(2)]
    cleared = plugin.clear_scheduled_actions_by_tag({"tag": "fixture"}, {})
    check(cleared["data"]["cleared_count"] == 2 and all(repo.history(item)["events"][-1]["operation"] == "clear" for item in ids), "clear_tag_retains_history")


def _check_page_faults(plugin: SchedulingPlugin, state: FakeState) -> None:
    state.page_fault = "late"
    partial = plugin.list_schedules({}, {})["data"]
    check(not partial["complete"] and partial["count"] >= 100 and any("page_failure" in item for item in partial["advisories"]), "page_failure_not_empty_success")
    for fault in ("malformed", "cycle"):
        state.page_fault = fault
        check(not plugin.list_schedules({}, {})["data"]["complete"], "invalid_cursor_not_complete_" + fault)
    state.page_fault = None
    state.bump_epoch = True
    check(not plugin.list_schedules({}, {})["data"]["complete"], "concurrent_change_not_complete")


def _check_page_count(count: int) -> None:
    plugin, repo, state = fixture()
    assert state is not None
    for index in range(count):
        repo.save_schedule(schedule(f"sch-{index:04d}"))
    deleted = schedule("sch-deleted").model_dump()
    state.tables["schedules"]["sch-deleted"] = {**deleted, "created_at": STAMP, "is_deleted": 1}
    result = plugin.list_schedules({}, {})["data"]
    check(set(state.namespaces) == {NAMESPACE}, "reads_owner_only")
    ids = [row["schedule_id"] for row in result["schedules"]]
    check(result["complete"] and len(ids) == count and len(set(ids)) == count, f"complete_registry_{count}")
    check(state.pages == (1 if count == 99 else 2), f"terminal_page_{count}")
    check(result["checked_epochs"]["persistent_before"] == result["checked_epochs"]["persistent_after"], "completeness_epoch_stable")
    _check_page_filters(plugin, state, count)


def _check_page_filters(plugin: SchedulingPlugin, state: FakeState, count: int) -> None:
    check(plugin.get_schedules_by_tag({"tag": "fixture"}, {})["data"]["count"] == count and plugin.list_schedules({"status": "error"}, {})["data"]["count"] == 0, "filters_after_complete_enumeration")
    check(set(state.namespaces) == {NAMESPACE}, "reads_owner_only")
    plugin.configure_memory_store()
    plugin._save_schedule_in_memory(schedule("memory-only"))
    check(plugin.list_schedules({}, {})["data"]["count"] == count + 1, "memory_only_rows_visible")
    if count == 101:
        _check_page_faults(plugin, state)


def check_pages() -> None:
    for count in (99, 100, 101):
        _check_page_count(count)
    empty, _, _ = fixture()
    check(empty.list_schedules({}, {})["data"]["complete"], "empty_registry_complete")


def check_recovery() -> None:
    plugin, repo, _ = fixture()
    sid = repo.save_schedule(schedule())
    repo.update_schedule_status(sid, "error", "fixture failure")
    check(rebind(plugin, sid, 1)["data"]["status"] == "error", "rebind_never_recovers_implicitly")
    params = {"schedule_id": sid, "expected_revision": 2, "reason": "explicit recovery"}
    result = plugin.recover_scheduled_action(params, {})["data"]
    check(result["revision"] == 3 and result["status"] == "scheduled", "recover_error_current_revision_passes")
    check(result["activation_status"] == "pending" and not result["effective"], "activation_failure_disclosed")
    manager = SchedulerManager()
    manager.initialize()
    plugin._scheduler_manager = manager
    params["expected_revision"] = 3
    count = len(repo.history(sid)["events"])
    retry = plugin.recover_scheduled_action(params, {})["data"]
    check(retry["outcome"] == "already_scheduled" and retry["effective"] and len(manager.get_jobs()) == 1, "recovery_idempotent_retry")
    plugin.recover_scheduled_action(params, {})
    check(len(manager.get_jobs()) == 1 and len(repo.history(sid)["events"]) == count, "recovery_no_duplicate_history_or_job")
    plugin._scheduler_manager = SchedulerManager()
    plugin._scheduler_manager.initialize()
    plugin._load_persisted_schedules()
    check(len(plugin._scheduler_manager.get_jobs()) == 1 and cast(ScheduleData, repo.load_schedule(sid)).actions[0].parameters["content"] == TEXT, "recovered_error_restores")


def _check_dispatch(memory: bool) -> None:
    plugin, repo, _ = fixture(memory=memory)
    sid = repo.save_schedule(schedule())
    old = cast(ScheduleData, repo.load_schedule(sid)).model_dump()
    recorder = Recorder()
    plugin.set_action_factory(recorder)  # type: ignore[arg-type]
    check(rebind(plugin, sid, 0)["action_status"] == "completed", "callback_rebind_setup")
    plugin._execute_action(sid, old)
    check(recorder.actions[-1]["arguments"]["name"] == "new", "prior_callback_uses_current_target")
    before = len(recorder.actions)
    plugin.clear_scheduled_action({"schedule_id": sid}, {})
    plugin._execute_action(sid, old)
    check(len(recorder.actions) == before, "callback_after_clear_no_submit")
    one = repo.save_schedule(schedule(kind="one_time"))
    plugin._execute_action(one, {})
    check(repo.load_schedule(one) is None and repo.history(one)["events"][-1]["operation"] == "complete", "completion_retains_history")
    check(plugin.recover_scheduled_action({"schedule_id": one, "expected_revision": 1, "reason": "invalid reopen"}, {})["action_status"] == "error", "terminal_cannot_recover")
    sid = repo.save_schedule(schedule())
    entered, release, rebound = Event(), Event(), Event()
    recorder.entered, recorder.release = entered, release
    errors: list[BaseException] = []

    def fire() -> None:
        try:
            plugin._execute_action(sid, {})
        except BaseException as exc:
            errors.append(exc)

    def revise() -> None:
        rebind(plugin, sid, 0, "after-boundary")
        rebound.set()

    firing = Thread(target=fire)
    firing.start()
    check(entered.wait(5), "dispatch_held_lock_reached")
    revising = Thread(target=revise)
    revising.start()
    check(not rebound.wait(0.05), "rebind_waits_for_submission")
    release.set()
    firing.join(5)
    revising.join(5)
    check(not errors and rebound.is_set() and recorder.actions[-1]["arguments"]["name"] == "old", "queued_before_boundary_is_preserved")
    recorder.entered = recorder.release = None
    plugin._execute_action(sid, {})
    check(recorder.actions[-1]["arguments"]["name"] == "after-boundary", "post_boundary_dispatch_current")


def check_dispatch() -> None:
    _check_dispatch(False)
    _check_dispatch(True)


def _check_races(memory: bool) -> None:
    plugin, repo, _ = fixture(memory=memory)
    sid = repo.save_schedule(schedule())
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def clear() -> None:
        barrier.wait(5)
        outcomes.append(plugin.clear_scheduled_action({"schedule_id": sid}, {}))

    def revise() -> None:
        barrier.wait(5)
        outcomes.append(rebind(plugin, sid, 0))

    workers = [Thread(target=clear), Thread(target=revise)]
    for worker in workers:
        worker.start()
    barrier.wait(5)
    for worker in workers:
        worker.join(5)
    check(len(outcomes) == 2 and repo.load_schedule(sid) is None and sum(event["operation"] == "clear" for event in repo.history(sid)["events"]) == 1, "clear_rebind_race")
    sid = repo.save_schedule(schedule(kind="one_time"))
    recorder = Recorder()
    entered, release, finished = Event(), Event(), Event()
    recorder.entered, recorder.release = entered, release
    plugin.set_action_factory(recorder)  # type: ignore[arg-type]
    result: dict[str, Any] = {}

    def complete() -> None:
        plugin._execute_action(sid, {})

    def change() -> None:
        result.update(rebind(plugin, sid, 0))
        finished.set()

    completing = Thread(target=complete)
    completing.start()
    check(entered.wait(5), "completion_lock_reached")
    changing = Thread(target=change)
    changing.start()
    check(not finished.wait(0.05), "completion_rebind_waits")
    release.set()
    completing.join(5)
    changing.join(5)
    check(finished.is_set() and result["action_status"] == "error" and repo.load_schedule(sid) is None, "complete_rebind_race")


def check_races() -> None:
    _check_races(False)
    _check_races(True)
    plugin, repo, _ = fixture()
    sid = repo.save_schedule(schedule())
    old = repo.load_schedule(sid)
    assert old is not None
    rebind(plugin, sid, 0)
    original = repo._read_in
    reads = 0

    def stale_read(transaction: StateTransaction, key: str) -> ScheduleData | None:
        nonlocal reads
        reads += 1
        return old if reads == 1 else original(transaction, key)

    recorder = Recorder()
    plugin.set_action_factory(recorder)  # type: ignore[arg-type]
    with patch.object(repo, "_read_in", stale_read):
        plugin._execute_action(sid, old.model_dump())
    check(reads >= 3 and recorder.actions[-1]["arguments"]["name"] == "new", "old_snapshot_rechecks_revision")


def check_shapes() -> None:
    from ananta.services.scheduling_service import SchedulingService
    from ananta.services.scheduling_service.interfaces.public import SchedulingServiceAPI
    from default_scheduling_plugin.action_target import action_definitions, targets_for

    plugin, repo, state = fixture()
    value = schedule()
    value.actions += [
        ActionData(name="plugin::agent_messaging_plugin::send_peer_message", parameters={"peer_id": "fixture", "peer_agent_instance_id": "i"}, result_processor=None, result_processor_kind=None),
        ActionData(name="plugin::agent_messaging_plugin::send_peer_message", parameters={"peer_id": "fixture", "peer_agent_session_id": "s"}, result_processor=None, result_processor_kind=None),
        ActionData(name="plugin::agent_messaging_plugin::drive_session", parameters={"agent_instance_id": "d", "text": TEXT}, result_processor=None, result_processor_kind=None),
        ActionData(name="plugin::agent_messaging_plugin::send_peer_message", parameters={"peer_agent_instance_id": "i", "peer_agent_session_id": "s"}, result_processor=None, result_processor_kind=None),
        ActionData(name="service_interface::knowledge_service::search", parameters={"query": "not a destination"}, result_processor=None, result_processor_kind=None),
    ]
    sid = repo.save_schedule(value)
    check([target["kind"] for target in targets_for(value)] == ["role", "instance", "unknown", "instance", "ambiguous", "process"], "declared_destination_contracts")
    rebind(plugin, sid, 0)
    current = repo.load_schedule(sid)
    assert current is not None
    check(current.actions[1:] == value.actions[1:], "other_actions_unchanged")
    legacy = schedule()
    legacy.action_name, legacy.action_parameters, legacy.actions = legacy.actions[0].name, legacy.actions[0].parameters, []
    legacy_id = repo.save_schedule(legacy)
    check(rebind(plugin, legacy_id, 0)["action_status"] == "completed", "legacy_destination_rebind")
    legacy_after = repo.load_schedule(legacy_id)
    assert legacy_after is not None
    check(not legacy_after.actions and action_definitions(legacy_after)[0]["arguments"]["content"] == TEXT, "legacy_semantics_preserved")
    assert state is not None
    preexisting = state.prepare_record(NAMESPACE, "schedules", schedule("preexisting").model_dump())
    state.tables["schedules"]["preexisting"] = preexisting
    rebind(plugin, "preexisting", 0)
    check([event["operation"] for event in repo.history("preexisting")["events"]] == ["observed_baseline", "rebind"], "observed_baseline_not_invented_creator")
    service = SchedulingService.__new__(SchedulingService)
    service._ensure_ready = lambda: plugin  # type: ignore[method-assign,return-value]
    check(cast(dict[str, Any], service.list_schedules())["data"]["count"] == 3, "public_list_owner_forwarding")
    for name in ("list_schedules", "get_schedules_by_tag", "rebind_scheduled_action", "get_schedule_binding_history", "recover_scheduled_action"):
        public = getattr(SchedulingServiceAPI, name)._service_interface_metadata
        handler_name = {"list_schedules": "list_schedules_action", "get_schedules_by_tag": "get_schedules_by_tag_action"}.get(name, name)
        edge = getattr(SchedulingPlugin, handler_name)._platform_process_metadata
        check(public.is_discoverable and set(public.parameters) - {"state"} == set(edge.parameters) and name in plugin.get_edge_process_definitions(), "public_registration_" + name)


def main() -> int:
    for module in (ananta, default_scheduling_plugin):
        check(Path(str(module.__file__)).resolve().is_relative_to(ROOT), "source_provenance_" + module.__name__)
    check_rebind()
    check_pages()
    check_recovery()
    check_dispatch()
    check_races()
    check_shapes()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
