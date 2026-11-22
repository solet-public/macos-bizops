"""Exercise the upgrade from legacy managed-session rows to durable origins."""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "ananta/src"), str(ROOT / "plugins/agent_messaging_plugin/src"), str(ROOT / "plugins/postgres_state_management_plugin/src"), str(Path(__file__).parent)]

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_completed, require_updated  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.state_service.bounded_read import PagedReadError  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402

from agent_messaging_plugin.gauge_canary import register_synthetic_session  # noqa: E402
from agent_messaging_plugin.gauge_canary_store import register_canary  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MANAGED_SESSION, get_managed_session_schema, get_session_lifecycle_schema_definition  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import read_managed_session  # noqa: E402
from agent_messaging_plugin.session_origin_backfill import SESSION_ORIGIN_MARKER, backfill_session_origins, read_session_origin_completion  # noqa: E402
from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface


def seed_legacy(state: StateManagementInterface, *, with_default: bool = False) -> None:
    modes = {
        "old-spawn": "worktree",
        "old-registration": "operator_existing_checkout",
        "old-path": "worktree",
        "old-canary": "synthetic_no_process",
        "old-ambiguous": "unrecorded",
        "old-generic-spawn": "synthetic_no_worktree",
    }
    for identity, mode in modes.items():
        record: dict[str, object] = {
            "agent_instance_id": identity,
            "host": "tmux",
            "workspace": "",
            "spawned_by_instance_id": "",
            "lifecycle_state": "live",
            "provisioning_mode": mode,
            "assignment": None,
            "brief_ref": "/old/brief" if identity == "old-path" else "",
        }
        if with_default:
            record["session_origin"] = "unknown"
        require_completed(state.write_state(NAMESPACE, {"table": TABLE_MANAGED_SESSION, "record": record}), "seed legacy session")


def check_origin_declaration() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    seed_legacy(state)
    for identity in ("old-spawn", "old-registration"):
        row = read_managed_session(state, identity)
        assert (row["spawned_by_instance_id"], row["workspace"], row["host"]) == ("", "", "tmux")
    origin = get_managed_session_schema().columns.get("session_origin")
    assert origin is not None, "managed_session has no session_origin for legacy spawned and registered tmux rows"
    assert origin.type == ColumnType.TEXT and origin.default == "unknown"


def checkpoint_value(state: StateManagementInterface) -> object:
    data = require_completed(state.get_key_value(NAMESPACE, SESSION_ORIGIN_MARKER), "read back history checkpoint")
    return data["value"] if data["found"] else None


def check_classification(state: StateManagementInterface) -> None:
    ambiguous = read_managed_session(state, "old-ambiguous")
    result = backfill_session_origins(state)
    assert result == {"status": "live_unknown", "updated": 5, "row_id": ambiguous["id"]}
    assert checkpoint_value(state) is None
    expected = {
        "old-spawn": "spawned",
        "old-registration": "registered",
        "old-path": "historical_read_only",
        "old-canary": "synthetic",
        "old-ambiguous": "unknown",
        "old-generic-spawn": "spawned",
    }
    actual = {identity: read_managed_session(state, identity)["session_origin"] for identity in expected}
    assert actual == expected, actual
    # An ambiguous terminal session can remain unknown. Ending it supplies no new origin evidence.
    query: dict[str, object] = {"table": TABLE_MANAGED_SESSION, "filters": {"id": ambiguous["id"]}}
    assert require_updated(state.update_state(NAMESPACE, query, {"lifecycle_state": "terminated"})) == 1
    assert backfill_session_origins(state) == {"status": "completed", "updated": 0}
    assert read_managed_session(state, "old-ambiguous")["session_origin"] == "unknown"
    assert checkpoint_value(state) == "history_scanned" and read_session_origin_completion(state) == {"status": "completed"}
    assert backfill_session_origins(state) == {"status": "already_done", "updated": 0}
    print(f"Legacy origins {actual}. Completion verified after the ambiguous row became terminal.")


def check_upgrade() -> None:
    """Boot the old declaration, write rows, then boot the updated declaration in the same database."""
    definition = get_session_lifecycle_schema_definition()
    table = definition.tables[TABLE_MANAGED_SESSION]
    old_table = replace(table, columns={key: col for key, col in table.columns.items() if key != "session_origin"})
    old = replace(definition, tables={**definition.tables, TABLE_MANAGED_SESSION: old_table})
    with ledger_fixture("messaging_write", schema="session_origin_" + uuid4().hex[:12]) as fixture:
        state = cast("StateManagementInterface", fixture.state)
        old_manager = SchemaManager(fixture.state, fixture.plugin)
        old_manager.initialize_schemas([CoreSchemaDefinitions.get_key_value_store_schema(), old])
        fixture.state.set_schema_manager(old_manager)
        seed_legacy(state)
        assert "session_origin" not in read_managed_session(state, "old-spawn")
        new_manager = SchemaManager(fixture.state, fixture.plugin)
        new_manager.initialize_schemas([CoreSchemaDefinitions.get_key_value_store_schema(), definition])
        fixture.state.set_schema_manager(new_manager)
        schema = require_completed(state.describe_schema(NAMESPACE), "describe upgraded database")
        tables = cast("dict[str, Any]", schema["tables"])
        column = tables[f"{NAMESPACE}__{TABLE_MANAGED_SESSION}"]["columns"]["session_origin"]
        assert column["type"] == "text" and column["default"] == "'unknown'::text"
        assert read_managed_session(state, "old-spawn")["session_origin"] == "unknown"
        check_classification(state)
        print(f"Private PostgreSQL upgraded column {column}.")


def check_page_fault_and_retry() -> None:
    """A malformed second page with tied timestamps leaves the first page durable."""
    fake = RealShapeState()
    fake.now_iso = lambda: "2026-01-02T03:04:05"
    state = cast("StateManagementInterface", fake)
    for index in range(101):
        record = {
            "id": f"mgs-{index:03}",
            "agent_instance_id": f"agi-{index}",
            "session_origin": "unknown",
            "brief_ref": "",
            "assignment": None,
            "provisioning_mode": "worktree",
            "lifecycle_state": "live",
        }
        require_completed(state.write_state(NAMESPACE, {"table": TABLE_MANAGED_SESSION, "record": record}), "seed tied row")
    read_page = fake.query_ordered

    def incomplete_second_page(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        assert query["limit"] <= 100
        if "after" in query:
            assert query["after"] == [fake.now_iso(), "mgs-099"]
            return {"action_status": "completed", "data": {}}
        return read_page(namespace, query)

    with patch.object(fake, "query_ordered", side_effect=incomplete_second_page), TestCase().assertRaisesRegex(PagedReadError, "data.records is not a list"):
        backfill_session_origins(state)
    assert checkpoint_value(state) is None
    rows = fake.rows(NAMESPACE, TABLE_MANAGED_SESSION)
    assert sum(row["session_origin"] == "spawned" for row in rows) == 100
    assert backfill_session_origins(state) == {"status": "completed", "updated": 1}
    assert all(row["session_origin"] == "spawned" for row in fake.rows(NAMESPACE, TABLE_MANAGED_SESSION))
    assert read_session_origin_completion(state) == {"status": "completed"}


def check_compare_and_set() -> None:
    fake = RealShapeState()
    state = cast("StateManagementInterface", fake)
    record = {
        "agent_instance_id": "agi-race",
        "session_origin": None,
        "brief_ref": "/bound",
        "assignment": {"bound": True},
        "provisioning_mode": "worktree",
        "lifecycle_state": "parked",
    }
    require_completed(state.write_state(NAMESPACE, {"table": TABLE_MANAGED_SESSION, "record": record}), "seed nullable origin")
    update = fake.update_state
    row_id = read_managed_session(state, "agi-race")["id"]

    def win_race(namespace: str, query: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
        assert query["filters"] == {"id": row_id, "session_origin": {"op": "is_null"}}
        assert changes == {"session_origin": "spawned"}
        assert require_updated(update(namespace, {"table": TABLE_MANAGED_SESSION, "filters": {"id": row_id}}, {"session_origin": "registered"})) == 1
        return update(namespace, query, changes)

    with patch.object(fake, "update_state", side_effect=win_race), TestCase().assertRaisesRegex(RuntimeError, "backfill conflict"):
        backfill_session_origins(state)
    assert checkpoint_value(state) is None
    assert backfill_session_origins(state) == {"status": "completed", "updated": 0}
    assert read_managed_session(state, "agi-race")["session_origin"] == "registered"


def check_synthetic_writer() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    register_canary(state, agent_instance_id="agi-amber", purpose="origin evidence", registered_by="steward")
    register_synthetic_session(state, agent_instance_id="agi-amber", label="amber", spawned_by_instance_id="steward", directed_by="steward", report_by_seconds=60)
    assert read_managed_session(state, "agi-amber")["session_origin"] == "synthetic"


def check_stale_completion() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    assert backfill_session_origins(state) == {"status": "completed", "updated": 0}
    seed_legacy(state, with_default=True)
    assert read_session_origin_completion(state)["status"] == "live_unknown"
    require_completed(state.set_key_value(NAMESPACE, SESSION_ORIGIN_MARKER, True), "seed old boolean marker")
    assert read_session_origin_completion(state)["status"] == "live_unknown"
    ambiguous = read_managed_session(state, "old-ambiguous")
    assert backfill_session_origins(state) == {"status": "live_unknown", "updated": 5, "row_id": ambiguous["id"]}
    assert checkpoint_value(state) == "history_scanned"
    assert read_session_origin_completion(state) == {"status": "live_unknown", "row_id": ambiguous["id"]}
    assert read_managed_session(state, "old-registration")["session_origin"] == "registered"
    query: dict[str, object] = {"table": TABLE_MANAGED_SESSION, "filters": {"id": ambiguous["id"]}}
    assert require_updated(state.update_state(NAMESPACE, query, {"lifecycle_state": "terminated"})) == 1
    assert read_session_origin_completion(state) == {"status": "completed"}
    print("A stale boolean marker was revalidated and upgraded.")


def check_clean_restart() -> None:
    fake = RealShapeState()
    state = cast("StateManagementInterface", fake)
    assert backfill_session_origins(state) == {"status": "completed", "updated": 0}
    with patch.object(fake, "set_key_value", wraps=fake.set_key_value) as write_checkpoint, patch.object(fake, "update_state", wraps=fake.update_state) as write_session:
        assert backfill_session_origins(state) == {"status": "already_done", "updated": 0}
        write_checkpoint.assert_not_called()
        write_session.assert_not_called()
    print("The clean restart made zero writes.")


if __name__ == "__main__":
    check_origin_declaration()
    upgraded = cast("StateManagementInterface", RealShapeState())
    seed_legacy(upgraded, with_default=True)
    check_classification(upgraded)
    check_page_fault_and_retry()
    check_compare_and_set()
    check_synthetic_writer()
    check_stale_completion()
    check_clean_restart()
    if not os.environ.get(MANIFEST_ENV):
        raise SystemExit(skip_fixture())
    check_upgrade()
