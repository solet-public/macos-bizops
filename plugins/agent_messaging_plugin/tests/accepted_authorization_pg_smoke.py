#!/usr/bin/env python3
"""Private-PostgreSQL proof of accepted-authorization schema, CAS and history.

Run only under the shipped provisioner
(``project-solet/scripts/provision_test_pg.py --exec``); a missing fixture
exits 77 (UNMEASURED), which is never a pass. Two independent state services
each own a connection pool through the PostgreSQL state plugin; the schema is
applied by that plugin's declarative lifecycle. This module never opens a
driver connection or executes SQL, and never touches a live database.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Thread
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
for _source in (_ROOT / "ananta/src", _ROOT / "plugins/agent_messaging_plugin/src", _ROOT / "plugins/postgres_state_management_plugin/src", Path(__file__).resolve().parent):
    sys.path.insert(0, str(_source))

import postgres_state_management_plugin  # noqa: E402
from accepted_authorization_smoke import PREDICATE_A, PREDICATE_B, PREDICATE_C, _ProbedState, _refused  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402
from ananta.services.plugin_schema_service.serialization import to_json  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from postgres_state_management_plugin.plugin import PostgresStatePlugin  # noqa: E402

import agent_messaging_plugin  # noqa: E402
import ananta  # noqa: E402
from agent_messaging_plugin import accepted_authorization_verbs as verbs  # noqa: E402
from agent_messaging_plugin.held_authorization_store import record_held_authorization  # noqa: E402
from agent_messaging_plugin.held_authorization_verbs import list_held_authorizations  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_ACCEPTED_AUTHORIZATION,
    TABLE_ACCEPTED_AUTHORIZATION_EVENT,
    get_accepted_authorization_schema_definition,
    get_session_lifecycle_schema_definition,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _private_dsn() -> dict[str, str]:
    raw = os.environ.get("PROJECT_SOLET_DESK_TEST_DSN")
    if not raw:
        print("UNMEASURED: private PostgreSQL fixture is missing; run under project-solet/scripts/provision_test_pg.py --exec")
        print('BORN_CLONE_SKIP_WITNESS={"reason":"local_postgres_fixture_unreachable"}')
        raise SystemExit(77)
    dsn = dict(word.split("=", 1) for word in shlex.split(raw))
    assert dsn.get("host") == "127.0.0.1" and int(dsn.get("port", "5432")) != 5432 and dsn.get("dbname") == "project_solet_desk_test", "private_fixture_required"
    for module in (ananta, agent_messaging_plugin, postgres_state_management_plugin):
        _check(Path(str(module.__file__)).resolve().is_relative_to(_ROOT), "lane_source_provenance_" + module.__name__)
    return dsn


class Fixture:
    """Two state services over one private schema, each with its own pool."""

    def __init__(self) -> None:
        self.owners: list[PostgresStatePlugin] = []
        self.states: list[StateService] = []
        self.installs: list[str] = []

    def open(self) -> None:
        dsn = _private_dsn()
        physical_schema = "accepted_authorization_test_" + uuid4().hex[:12]
        lifecycle = get_session_lifecycle_schema_definition()
        accepted = get_accepted_authorization_schema_definition()
        merged = replace(lifecycle, tables={**lifecycle.tables, **accepted.tables})
        for index in range(2):
            owner = PostgresStatePlugin()
            self.owners.append(owner)
            owner.set_vault_service(SimpleNamespace(retrieve=lambda **_kw: {"action_status": "completed", "data": {"value": dsn.get("password", "")}}))  # pyright: ignore[reportArgumentType]
            config = {"host": dsn["host"], "port": int(dsn["port"]), "database": dsn["dbname"], "user": dsn["user"], "pg_schema": physical_schema, "pool_size": 3}
            with patch.dict(os.environ, {"SOLET_NAME": "accepted-authorization-fixture"}):
                owner._initialize(config)  # pyright: ignore[reportArgumentType]  # noqa: SLF001
            _check(asyncio.run(owner.start_services())["action_status"] == "completed", f"private_state_owner_{index}")  # pyright: ignore[reportTypedDictNotRequiredAccess]
            if index == 0:
                for definition in (lifecycle, accepted, lifecycle, accepted):
                    self.installs.append(str(owner.install_plugin_schema(definition.namespace, to_json(definition))["status"]))
            state = StateService(plugin_manager=SimpleNamespace(get_plugin=lambda _name, current=owner: current), state_plugin_name="postgres_state_management_plugin")  # pyright: ignore[reportArgumentType]
            state.set_schema_manager(SimpleNamespace(get_schema=lambda _namespace: merged))  # pyright: ignore[reportArgumentType]
            self.states.append(state)

    def close(self) -> None:
        for owner in reversed(self.owners):
            asyncio.run(owner.stop_services())


def _record(state: StateService, message: str, predicate: str = PREDICATE_A, **extra: str) -> dict[str, Any]:
    params: dict[str, object] = {
        "requesting_peer": "Coordinator-Main",
        "owed_by_role": "Git-Controller",
        "branch_or_request_ref": "seed-clone/fixture",
        "authorizing_message_id": message,
        "act": "seed-clone",
        "predicate": predicate,
        "reason": "standing authorization accepted at intake",
        "actor": "Git-Controller",
        **extra,
    }
    return verbs.record_accepted_authorization(state, params)  # pyright: ignore[reportArgumentType]


def _change(state: Any, entry_id: str, version: int, operation: str, **extra: object) -> dict[str, Any]:
    params: dict[str, object] = {"entry_id": entry_id, "expected_version": version, "operation": operation, "reason": f"{operation} on private PG", "actor": "Coordinator-Main", **extra}
    return verbs.change_accepted_authorization(state, params)


def _snapshot(state: StateService, entry_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Projection and events of one entry, read through ``state``'s own pool."""
    history = verbs.get_authorization_history(state, {"entry_id": entry_id})  # pyright: ignore[reportArgumentType]
    return history["current"], history["events"]


def check_schema(fixture: Fixture) -> None:
    _check(fixture.installs == ["installed", "updated", "no_op", "no_op"], "declared_schema_adds_tables_then_plans_no_change")
    for table in (TABLE_ACCEPTED_AUTHORIZATION, TABLE_ACCEPTED_AUTHORIZATION_EVENT):
        rows = require_records(fixture.states[1].query_ordered(AGENT_ROLE_BINDING_NAMESPACE, {"table": table, "filters": {}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": 1}))
        _check(rows == [], f"declared_table_readable_{table}")


def check_successor_lifecycle(fixture: Fixture) -> str:
    accepter, successor = fixture.states
    entry_id = str(_record(accepter, "arm-pg-A")["entry_id"])
    listed = list_held_authorizations(successor, owed_by_role="Git-Controller")  # pyright: ignore[reportArgumentType]
    _check([(e["entry_kind"], e["state"], e["authorizing_message_id"], e["predicate"]) for e in listed["entries"]] == [("accepted", "accepted", "arm-pg-A", PREDICATE_A)], "pg_accepted_unfired_visible_to_successor")
    _change(accepter, entry_id, 1, "correct", predicate=PREDICATE_B, authorizing_message_id="arm-pg-B")
    _change(successor, entry_id, 2, "correct", predicate=PREDICATE_C, authorizing_message_id="arm-pg-C")
    by_old = verbs.get_authorization_history(successor, {"authorizing_message_id": "arm-pg-A"})  # pyright: ignore[reportArgumentType]
    _check(
        [event["after"]["predicate"] for event in by_old["events"]] == [PREDICATE_A, PREDICATE_B, PREDICATE_C] and by_old["current"][0]["predicate"] == PREDICATE_C and by_old["events"][1]["before"]["authorizing_message_id"] == "arm-pg-A",
        "pg_correction_retains_old_predicates",
    )
    baseline = _snapshot(successor, entry_id)
    code, _other = _refused(lambda: _change(accepter, entry_id, 3, "fire", predicate=PREDICATE_A, evidence="withdrawn"))
    _check(code == "predicate_mismatch" and _snapshot(successor, entry_id) == baseline, "pg_withdrawn_predicate_cannot_fire")
    return entry_id


def check_stale_and_rollback(fixture: Fixture, entry_id: str) -> None:
    accepter, successor = fixture.states
    stale_row = dict(require_records(accepter.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_ACCEPTED_AUTHORIZATION, "filters": {"id": entry_id}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": 1}))[0])
    _change(successor, entry_id, 3, "correct", notes="moved on by the other connection")
    baseline = _snapshot(successor, entry_id)
    probed = _ProbedState(accepter, stale_row=stale_row)
    code, other = _refused(lambda: _change(probed, entry_id, 3, "correct", notes="written from a stale read"))
    print(f"        reach: stale read version 3, CAS matches={probed.probe.cas_matches}, refusal={code}, other={other}")
    _check(code == "stale_version" and other is None and probed.probe.cas_matches == [0] and _snapshot(successor, entry_id) == baseline, "pg_stale_write_preserves_projection_and_history")
    failing = _ProbedState(accepter, fail_table=TABLE_ACCEPTED_AUTHORIZATION_EVENT)
    code, other = _refused(lambda: _change(failing, entry_id, 4, "withdraw"))
    print(f"        reach: CAS matches={failing.probe.cas_matches}, failed event writes={failing.probe.failed_writes}, raised={other or code}")
    _check(failing.probe.cas_matches == [1] and other == "RuntimeError" and _snapshot(successor, entry_id) == baseline, "pg_event_failure_rolls_back_projection")


def _race(fixture: Fixture, entry_id: str) -> dict[str, tuple[str | None, str | None]]:
    """Fire on one connection and withdraw on the other, both from version 1,
    each held after its read until the other has read too."""
    accepter, successor = fixture.states
    barrier = Barrier(2, timeout=10)
    outcomes: dict[str, tuple[str | None, str | None]] = {}

    def run(name: str, state: StateService, operation: str, **extra: object) -> None:
        probed = _ProbedState(state, read_barrier=barrier)
        outcomes[name] = _refused(lambda: _change(probed, entry_id, 1, operation, **extra))

    workers = [
        Thread(target=run, args=("fire", accepter, "fire"), kwargs={"predicate": PREDICATE_A, "evidence": "fired by connection one"}),
        Thread(target=run, args=("withdraw", successor, "withdraw"), kwargs={"evidence": "withdrawn by connection two"}),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    return outcomes


def check_concurrent_terminal(fixture: Fixture) -> None:
    accepter = fixture.states[0]
    entry_id = str(_record(accepter, "arm-pg-race")["entry_id"])
    outcomes = _race(fixture, entry_id)
    current, events = _snapshot(accepter, entry_id)
    winner = next((name for name, outcome in outcomes.items() if outcome == (None, None)), "none")
    expected = {"fire": ("fired", "fired by connection one"), "withdraw": ("withdrawn", "withdrawn by connection two")}
    print(f"        reach: outcomes={outcomes}")
    _check(
        (sorted(outcomes.values(), key=str), [event["operation"] for event in events], (current[0]["state"], current[0]["terminal_evidence"])) == (sorted([(None, None), ("stale_version", None)], key=str), ["record", winner], expected.get(winner)),
        "pg_concurrent_terminal_has_one_winner",
    )


def check_pages(fixture: Fixture) -> None:
    accepter, successor = fixture.states
    for index in range(2):
        record_held_authorization(accepter, requesting_peer=f"lane-{index}", owed_by_role="Pager", branch_or_request_ref=f"b{index}", reason="refused")  # pyright: ignore[reportArgumentType]
    for index in range(101):
        _record(accepter, f"arm-pg-page-{index}", owed_by_role="Pager")
    seen: list[str] = []
    sizes: list[int] = []
    cursor: str | None = None
    while True:
        page = list_held_authorizations(successor, owed_by_role="Pager", cursor=cursor)  # pyright: ignore[reportArgumentType]
        seen.extend(entry["entry_id"] for entry in page["entries"])
        sizes.append(page["count"])
        cursor = page["next_cursor"]
        if cursor is None or len(sizes) > 5:
            break
    _check(sizes == [100, 3] and len(set(seen)) == 103, "pg_pages_continue_past_one_hundred")


def main() -> int:
    fixture = Fixture()
    try:
        fixture.open()
        check_schema(fixture)
        entry_id = check_successor_lifecycle(fixture)
        check_stale_and_rollback(fixture, entry_id)
        check_concurrent_terminal(fixture)
        check_pages(fixture)
    finally:
        fixture.close()
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
