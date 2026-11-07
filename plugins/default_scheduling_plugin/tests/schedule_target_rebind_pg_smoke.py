#!/usr/bin/env python3
"""Manual private-PG proof of schedule CAS, atomic history and dispatch ordering.

Run only through the pinned native private-PG provisioner. Missing fixture is
exit 77 (UNMEASURED). Both independent connections use the owning state plugin;
this module never opens a driver connection or executes SQL.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event, Thread
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT / "ananta/src", ROOT / "plugins/default_scheduling_plugin/src", ROOT / "plugins/agent_messaging_plugin/src", ROOT / "plugins/postgres_state_management_plugin/src", Path(__file__).parent):
    sys.path.insert(0, str(source))

import default_scheduling_plugin  # noqa: E402
import postgres_state_management_plugin  # noqa: E402
from ananta.interfaces.state_management_interface import StateTransaction  # noqa: E402
from ananta.services.plugin_schema_service.serialization import to_json  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from default_scheduling_plugin.models import ScheduleData  # noqa: E402
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402
from default_scheduling_plugin.scheduling.scheduler_manager import SchedulerManager  # noqa: E402
from default_scheduling_plugin.storage.schedule_repository import ScheduleRepository  # noqa: E402
from postgres_state_management_plugin.plugin import PostgresStatePlugin  # noqa: E402
from schedule_target_rebind_smoke import TEXT, Recorder, check, rebind, schedule  # noqa: E402

import agent_messaging_plugin  # noqa: E402
import ananta  # noqa: E402


def _private_dsn() -> dict[str, str]:
    raw = os.environ.get("PROJECT_SOLET_DESK_TEST_DSN")
    if not raw:
        print("UNMEASURED: private PostgreSQL fixture is missing")
        print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
        raise SystemExit(77)
    dsn = dict(word.split("=", 1) for word in shlex.split(raw))
    assert dsn.get("host") == "127.0.0.1" and int(dsn.get("port", "5432")) != 5432 and dsn.get("dbname") == "project_solet_desk_test", "private_fixture_required"
    assert Path.cwd().resolve() == ROOT, "private_fixture_cwd"
    for module in (ananta, default_scheduling_plugin, agent_messaging_plugin, postgres_state_management_plugin):
        check(Path(str(module.__file__)).resolve().is_relative_to(ROOT), "source_provenance_" + module.__name__)
    return dsn


class Fixture:
    def __init__(self) -> None:
        self.owners: list[PostgresStatePlugin] = []
        self.plugins: list[SchedulingPlugin] = []
        self.states: list[StateService] = []
        self.repos: list[ScheduleRepository] = []

    def open(self) -> None:
        dsn = _private_dsn()
        physical_schema = "scheduling_test_" + uuid4().hex[:12]
        definitions = SchedulingPlugin().get_schema_definitions()
        schemas = {definition.namespace: definition for definition in definitions}
        for index in range(2):
            owner = PostgresStatePlugin()
            self.owners.append(owner)
            owner.set_vault_service(SimpleNamespace(retrieve=lambda **_kw: {"action_status": "completed", "data": {"value": dsn.get("password", "")}}))  # pyright: ignore[reportArgumentType]
            config = {"host": dsn["host"], "port": int(dsn["port"]), "database": dsn["dbname"], "user": dsn["user"], "pg_schema": physical_schema, "pool_size": 3}
            with patch.dict(os.environ, {"SOLET_NAME": "scheduling-fixture"}):
                owner._initialize(config)  # pyright: ignore[reportArgumentType]
            check(asyncio.run(owner.start_services())["action_status"] == "completed", f"private_state_owner_{index}")  # pyright: ignore[reportTypedDictNotRequiredAccess]
            if index == 0:
                for definition in definitions:
                    installed = owner.install_plugin_schema(definition.namespace, to_json(definition))
                    check(installed["status"] in ("installed", "installed_and_adopted", "adopted"), "declared_schema_installed")
            state = StateService(plugin_manager=SimpleNamespace(get_plugin=lambda _name, current=owner: current), state_plugin_name="postgres_state_management_plugin")  # pyright: ignore[reportArgumentType]
            state.set_schema_manager(SimpleNamespace(get_schema=lambda namespace: schemas[namespace]))  # pyright: ignore[reportArgumentType]
            self.states.append(state)
            plugin = SchedulingPlugin()
            plugin.logger.disabled = True
            plugin.set_state_service(state)
            plugin._job_tracker = None
            self.plugins.append(plugin)
            assert plugin._repository is not None
            self.repos.append(plugin._repository)
        self.repos[0].initialize_registry()

    def close(self) -> None:
        for owner in reversed(self.owners):
            asyncio.run(owner.stop_services())


class HistoryFault:
    def __init__(self, transaction: StateTransaction) -> None:
        self.transaction = transaction

    def __getattr__(self, key: str) -> Any:
        return getattr(self.transaction, key)

    def write_state(self, namespace: str, data: dict[str, object]) -> str:
        if data["table"] == "binding_history":
            raise RuntimeError("injected_history_failure")
        return self.transaction.write_state(namespace, data)


def check_atomic(fixture: Fixture) -> None:
    plugin, other = fixture.plugins
    repo, second = fixture.repos
    sid = repo.save_schedule(schedule())
    before = repo.load_schedule(sid)
    assert before is not None
    check(rebind(plugin, sid, 0)["action_status"] == "completed", "rebind_current_revision_passes")
    current = second.load_schedule(sid)
    assert current is not None
    expected = before.model_dump()
    expected["actions"][0]["parameters"]["name"] = "new"
    expected["revision"] = 1
    check(current.model_dump() == expected, "rebind_preserves_action_text")
    baseline = (current.model_dump(), repo.history(sid), repo.epoch())
    check(rebind(other, sid, 0)["action_status"] == "error" and (repo.load_schedule(sid).model_dump(), repo.history(sid), repo.epoch()) == baseline, "stale_revision_no_writes")  # pyright: ignore[reportOptionalMemberAccess]
    original = fixture.states[0].transactional

    @contextmanager
    def fail_history() -> Generator[StateTransaction]:
        with original() as transaction:
            yield cast(StateTransaction, HistoryFault(transaction))

    with patch.object(fixture.states[0], "transactional", fail_history):
        check(rebind(plugin, sid, 1, "uncommitted")["action_status"] == "error", "history_failure_reached")
    check((repo.load_schedule(sid).model_dump(), repo.history(sid), repo.epoch()) == baseline, "history_failure_rolls_back")  # pyright: ignore[reportOptionalMemberAccess]
    check(rebind(plugin, sid, 1)["action_status"] == "completed" and rebind(other, sid, 2, "old")["action_status"] == "completed", "correction_and_reversal")
    check(len(repo.history(sid)["events"]) == 4, "rebind_appends_history")
    plugin.clear_scheduled_action({"schedule_id": sid}, {})
    check(second.load_schedule(sid) is None and repo.history(sid)["events"][-1]["operation"] == "clear", "clear_retains_history")
    check(rebind(other, sid, 3)["action_status"] == "error", "no_resurrection")


def check_old_snapshot(fixture: Fixture) -> None:
    plugin, other = fixture.plugins
    repo = fixture.repos[0]
    sid = repo.save_schedule(schedule())
    old = repo.load_schedule(sid)
    assert old is not None
    recorder = Recorder()
    plugin.set_action_factory(recorder)  # type: ignore[arg-type]
    read, release = Event(), Event()
    original = repo._read_in
    calls = 0

    def pause_read(transaction: StateTransaction, schedule_id: str) -> ScheduleData | None:
        nonlocal calls
        row = original(transaction, schedule_id)
        calls += 1
        if calls == 1:
            read.set()
            assert release.wait(10), "snapshot_barrier_released"
        return row

    failures: list[BaseException] = []

    def fire() -> None:
        try:
            plugin._execute_action(sid, old.model_dump())
        except BaseException as exc:
            failures.append(exc)

    with patch.object(repo, "_read_in", pause_read):
        thread = Thread(target=fire)
        thread.start()
        check(read.wait(5), "old_snapshot_before_lock_reached")
        check(rebind(other, sid, 0, "after-snapshot")["action_status"] == "completed", "snapshot_rebind_committed")
        release.set()
        thread.join(10)
    check(not failures and not thread.is_alive() and calls >= 3 and recorder.actions[-1]["arguments"]["name"] == "after-snapshot", "old_snapshot_rechecks_revision")
    plugin._execute_action(sid, old.model_dump())
    check(recorder.actions[-1]["arguments"]["name"] == "after-snapshot", "prior_callback_uses_current_target")
    other.clear_scheduled_action({"schedule_id": sid}, {})
    count = len(recorder.actions)
    plugin._execute_action(sid, old.model_dump())
    check(len(recorder.actions) == count, "callback_after_clear_no_submit")


def check_held_lock(fixture: Fixture, *, one_time: bool) -> None:
    plugin, other = fixture.plugins
    repo = fixture.repos[0]
    sid = repo.save_schedule(schedule(kind="one_time" if one_time else "recurring"))
    recorder = Recorder()
    entered, release, finished, started = Event(), Event(), Event(), Event()
    recorder.entered, recorder.release = entered, release
    plugin.set_action_factory(recorder)  # type: ignore[arg-type]
    failures: list[BaseException] = []
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            plugin._execute_action(sid, {})
        except BaseException as exc:
            failures.append(exc)

    def update() -> None:
        started.set()
        result.update(rebind(other, sid, 0, "after-boundary"))
        finished.set()

    dispatch = Thread(target=fire)
    dispatch.start()
    check(entered.wait(5), "queue_submitted_before_completion_barrier")
    updater = Thread(target=update)
    updater.start()
    check(started.wait(5) and not finished.wait(0.1), "row_lock_serializes_independent_connections")
    release.set()
    dispatch.join(10)
    updater.join(10)
    check(not failures and not dispatch.is_alive() and not updater.is_alive(), "held_lock_threads_completed")
    check(recorder.actions[-1]["arguments"]["name"] == "old", "queued_before_boundary_is_preserved")
    if one_time:
        check(repo.load_schedule(sid) is None and result["action_status"] == "error" and repo.history(sid)["events"][-1]["operation"] == "complete", "complete_rebind_race")
        check(repo.history(sid)["events"][-1]["after_status"] == "completed", "completion_retains_history")
    else:
        check(result["action_status"] == "completed", "held_lock_rebind_committed")
        recorder.entered = recorder.release = None
        plugin._execute_action(sid, {})
        check(recorder.actions[-1]["arguments"]["name"] == "after-boundary", "callback_after_rebind_current")
        other.clear_scheduled_action({"schedule_id": sid}, {})


def check_clear_race(fixture: Fixture) -> None:
    plugin, other = fixture.plugins
    repo = fixture.repos[0]
    sid = repo.save_schedule(schedule())
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def clear() -> None:
        barrier.wait(5)
        outcomes.append(plugin.clear_scheduled_action({"schedule_id": sid}, {}))

    def change() -> None:
        barrier.wait(5)
        outcomes.append(rebind(other, sid, 0))

    workers = [Thread(target=clear), Thread(target=change)]
    for worker in workers:
        worker.start()
    barrier.wait(5)
    for worker in workers:
        worker.join(10)
    events = repo.history(sid)["events"]
    check(len(outcomes) == 2 and repo.load_schedule(sid) is None and sum(event["operation"] == "clear" for event in events) == 1, "clear_rebind_race")


def check_restore_and_pages(fixture: Fixture) -> None:
    plugin, other = fixture.plugins
    repo, second = fixture.repos
    sid = repo.save_schedule(schedule())
    repo.update_schedule_status(sid, "error", "fixture error")
    rebind(plugin, sid, 1)
    plugin._scheduler_manager = SchedulerManager()
    plugin._scheduler_manager.initialize()
    result = plugin.recover_scheduled_action({"schedule_id": sid, "expected_revision": 2, "reason": "explicit recovery"}, {})
    check(result["action_status"] == "completed" and result["data"]["effective"], "recover_error_current_revision_passes")
    history = repo.history(sid)
    epoch = repo.epoch()
    retry = plugin.recover_scheduled_action({"schedule_id": sid, "expected_revision": 3, "reason": "retry"}, {})
    check(retry["data"]["outcome"] == "already_scheduled" and retry["data"]["effective"] and repo.history(sid) == history and repo.epoch() == epoch and len(plugin._scheduler_manager.get_jobs()) == 1, "recovery_idempotent_retry")
    other._scheduler_manager = SchedulerManager()
    other._scheduler_manager.initialize()
    other._load_persisted_schedules()
    current = second.load_schedule(sid)
    check(len(other._scheduler_manager.get_jobs()) == 1 and current is not None and current.error_message is None and current.actions[0].parameters["content"] == TEXT, "recovered_error_restores")
    other.clear_scheduled_actions_by_tag({"tag": "fixture"}, {})
    check(repo.history(sid)["events"][-1]["operation"] == "clear", "clear_tag_retains_history")
    _check_pg_pages(fixture)


def _check_pg_pages(fixture: Fixture) -> None:
    plugin, repo = fixture.plugins[0], fixture.repos[0]
    prepare = fixture.states[0].prepare_record

    def same_timestamp(namespace: str, table_name: str, record: dict[str, Any]) -> dict[str, Any]:
        prepared = prepare(namespace, table_name, record)
        if table_name == "schedules":
            prepared["created_at"] = datetime(2026, 1, 1, tzinfo=UTC)
        return prepared

    ids: list[str] = []
    with patch.object(fixture.states[0], "prepare_record", same_timestamp):
        for count in (99, 100, 101):
            while len(ids) < count:
                ids.append(repo.save_schedule(schedule(f"pg-page-{len(ids):04d}")))
            observed = plugin.list_schedules({}, {})["data"]
            check(observed["complete"] and observed["count"] == count and {row["schedule_id"] for row in observed["schedules"]} == set(ids), f"private_complete_registry_{count}")
            check(observed["checked_epochs"]["persistent_before"] == observed["checked_epochs"]["persistent_after"], "completeness_epoch_stable")
    _check_pg_changes(fixture, ids)


def _check_pg_changes(fixture: Fixture, ids: list[str]) -> None:
    plugin, other = fixture.plugins
    repo, second = fixture.repos
    query = fixture.states[0].query_ordered

    def concurrent_page(namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        result = query(namespace, data)
        if data["table"] == "schedules":
            current = second.load_schedule(ids[0])
            assert current is not None
            check(rebind(other, ids[0], current.revision)["action_status"] == "completed", "page_mutation_committed")
        return result  # pyright: ignore[reportReturnType]

    with patch.object(fixture.states[0], "query_ordered", concurrent_page):
        changed = plugin.list_schedules({}, {})["data"]
    check(not changed["complete"] and changed["count"] == 101 and any("concurrent_change" in advisory for advisory in changed["advisories"]), "concurrent_change_not_complete")
    plugin.clear_scheduled_actions_by_tag({"tag": "fixture"}, {})
    check(plugin.list_schedules({}, {})["data"]["complete"] and repo.load_all_schedules().schedules == {}, "private_empty_registry_complete")


def main() -> int:
    fixture = Fixture()
    try:
        fixture.open()
        check_atomic(fixture)
        check_old_snapshot(fixture)
        check_held_lock(fixture, one_time=False)
        check_held_lock(fixture, one_time=True)
        check_clear_race(fixture)
        check_restore_and_pages(fixture)
        return 0
    finally:
        fixture.close()


if __name__ == "__main__":
    raise SystemExit(main())
