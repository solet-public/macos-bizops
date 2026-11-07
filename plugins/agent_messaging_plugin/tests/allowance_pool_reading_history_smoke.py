#!/usr/bin/env python3
"""Allowance history lifecycle and real PostgreSQL state-interface keyset contract."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "ananta/src"), str(ROOT / "plugins/agent_messaging_plugin/src"), str(Path(__file__).parent)]
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402

NOW = datetime(2026, 10, 16, 6, tzinfo=UTC)
PROFILE = "anthropic-max-20x-2026-08-22"
PROFILE_PATH = ROOT / "plugins/agent_messaging_plugin/model_profiles/usage_economics.v1.json"


def check(condition: object, name: str) -> None:
    assert condition, name
    print("PASS", name)


def parameters(**changes: Any) -> dict[str, Any]:
    return {"profile_id": PROFILE, "pool_id": "rolling-five-hour", "consumed": 12.5, "as_of": (NOW - timedelta(minutes=5)).isoformat(), "next_reset_at": (NOW + timedelta(hours=2)).isoformat(), "source": "fixture operator reading", "recorded_by": "fixture", **changes}


def submit(method: Any, key: str, arguments: dict[str, Any]) -> None:
    metadata = getattr(method, "_platform_process_metadata", None) or method._service_interface_metadata
    factory = ActionFactory(
        process_registry={"processes": {key: {"parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()}}}},
        template_engine=SimpleNamespace(resolve_templates=lambda action_def, context: action_def),
        state_service=SimpleNamespace(generate_unique_string=lambda **kw: {"action_status": "completed", "data": {"random_string": "fixture"}}),
        action_event_recorder=SimpleNamespace(store_action_event=lambda action: "ae-fixture"),
    )
    factory.submit_action_definition({"process_key": key, "arguments": arguments, "flow_id": "flow-history", "result_processor_kind": None, "error_processor_kind": None})


def history(state: Any, **params: Any) -> dict[str, Any]:
    from agent_messaging_plugin import allowance_pool_readings as readings

    return readings.read_allowance_pool_readings(state, {"history": True, **params}, now=NOW)


def record(state: Any, index: int = 0, **params: Any) -> dict[str, Any]:
    from agent_messaging_plugin import allowance_pool_readings as readings

    return readings.record_allowance_pool_reading(state, parameters(**params), profile_path=PROFILE_PATH, now=NOW + timedelta(seconds=index))


_FIXTURES: list[Any] = []


def new_state() -> Any:
    sys.path.insert(0, str(ROOT / "ananta/tests/llm/session_ledger"))
    from ananta.services.plugin_schema_service.serialization import to_json
    from session_usage_totals_smoke import Fixture

    fixture = Fixture()
    _FIXTURES.append(fixture)
    from agent_messaging_plugin import schema

    definition = schema.get_session_lifecycle_schema_definition()
    fixture.schemas[definition.namespace] = definition
    fixture.plugin.install_plugin_schema(definition.namespace, to_json(definition))
    return fixture.state


def lifecycle(state: Any) -> None:
    from agent_messaging_plugin import allowance_pool_readings as readings

    first = record(state)
    second = record(state, 1, consumed=25)
    rows = history(state)["history"]
    check(first["history_appended"] and second["history_appended"] and len(rows) == 2 and rows[0]["consumed"] == 12.5 and rows[1]["consumed"] == 25, "HISTORY_ROW_ON_RECORD")
    cleared = readings.retract_allowance_pool_reading(state, parameters())
    check(cleared["history_appended"] and history(state)["history"][-1]["operation"] == "retract" and len(history(state)["history"]) == 3, "HISTORY_ROW_ON_RETRACT_WITH_OPERATION")
    rows = history(state)["history"]
    check(rows == sorted(rows, key=lambda row: (row["recorded_at"], row["id"])), "HISTORY_ORDERED_OLDEST_FIRST")


def cursor(state: Any) -> None:
    from agent_messaging_plugin.model_capability_store import CatalogError

    rows = history(state)["history"]
    page = history(state, limit=1)
    continuation = history(state, limit=2, after_id=page["next_cursor"])
    try:
        history(state, after_id="missing")
    except CatalogError as exc:
        assert exc.code == "parameter_invalid"
    else:
        raise AssertionError("unknown cursor accepted")
    check(page["history"] + continuation["history"] == rows, "HISTORY_PAGED_WITH_CURSOR")


def page_contract(state: Any, limit: int) -> None:
    from ananta.llm.agent_messaging.state_results import StateOperationError
    from ananta.services.state_service.ordered_query import OrderedQueryError

    try:
        page = history(state, limit=limit)
        tail = history(state, limit=limit, after_id=page["next_cursor"])
    except (OrderedQueryError, StateOperationError) as exc:
        raise AssertionError(f"HISTORY_PAGE_LIMIT_100_101_200: primitive page rejected at {limit}") from exc
    ids = [row["id"] for row in page["history"] + tail["history"]]
    assert len(page["history"]) == limit and len(ids) == min(205, limit * 2) and len(set(ids)) == len(ids)


def pages() -> None:
    from agent_messaging_plugin.model_capability_store import CatalogError

    many = new_state()
    for index in range(205):
        record(many, index)
    original = many.query_ordered
    limits: list[int] = []

    def bounded(namespace: str, query: dict[str, Any]) -> Any:
        limits.append(query["limit"])
        return original(namespace, query)

    with patch.object(many, "query_ordered", side_effect=bounded):
        for limit in (100, 101, 200):
            page_contract(many, limit)
        for invalid in (0, 201):
            try:
                history(many, limit=invalid)
            except CatalogError as exc:
                assert exc.code == "parameter_invalid"
            else:
                raise AssertionError("invalid history limit accepted")
    check(max(limits) <= 100, "HISTORY_PAGE_LIMIT_100_101_200")


def append_failure() -> None:
    from agent_messaging_plugin import allowance_pool_readings as readings
    from agent_messaging_plugin import schema

    broken = new_state()
    original_write = broken.write_state

    def failing(namespace: str, data: dict[str, Any], **kwargs: Any) -> Any:
        if data["table"] == schema.TABLE_ALLOWANCE_POOL_READING_HISTORY:
            raise RuntimeError("fixture history append failure")
        return original_write(namespace, data, **kwargs)

    with patch.object(broken, "write_state", side_effect=failing):
        result = record(broken)
    check(result["history_appended"] is False and "fixture history append failure" in result["history_error"] and len(readings.read_pool_readings(broken)) == 1, "HISTORY_APPEND_FAILURE_KEEPS_READING")


def latest(state: Any) -> None:
    from agent_messaging_plugin import allowance_pool_readings as readings

    restored = record(state, 2)
    current = readings.read_pool_readings(state)
    expected = {"readings": [row.to_payload(NOW) for row in current], "current_readings": sum(row.is_current(NOW) for row in current), "as_of": NOW.isoformat()}
    check(restored["history_appended"] and readings.read_allowance_pool_readings(state, {}, now=NOW) == expected and len(history(state)["history"]) == 4, "LATEST_READING_UNCHANGED_BEHAVIOUR")


def bridge() -> None:
    from agent_messaging_plugin.plugin import AgentMessagingPlugin

    key = "plugin::agent_messaging_plugin::read_allowance_pool_readings"
    try:
        submit(AgentMessagingPlugin.read_allowance_pool_readings, key, {"history": True, "after_id": "cursor", "limit": 101})
    except FrameworkError as exc:
        raise AssertionError("BRIDGE_ACCEPTS_HISTORY_ARGUMENTS") from exc
    check(True, "BRIDGE_ACCEPTS_HISTORY_ARGUMENTS")
    try:
        submit(AgentMessagingPlugin.read_allowance_pool_readings, key, {"xyzzy": True})
    except FrameworkError as exc:
        check(exc.error_code == "action.unknown_arguments", "BRIDGE_STILL_REFUSES_UNDECLARED_ARGUMENT")
    else:
        raise AssertionError("BRIDGE_STILL_REFUSES_UNDECLARED_ARGUMENT")


def main() -> None:
    try:
        from agent_messaging_plugin import schema
    except ModuleNotFoundError as exc:
        print(f"SKIP optional plugin unavailable: {exc.name}")
        raise SystemExit(77) from exc
    check(schema.get_session_lifecycle_schema_definition().version == "1.16.0", "SCHEMA_VERSION_BUMPED")
    state = new_state()
    lifecycle(state)
    cursor(state)
    pages()
    append_failure()
    latest(state)
    bridge()


if __name__ == "__main__":
    try:
        main()
    finally:
        for fixture in _FIXTURES:
            fixture.close()
