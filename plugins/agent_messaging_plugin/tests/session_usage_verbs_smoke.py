#!/usr/bin/env python3
"""Offline conformance smoke fixtures for managed-session usage joins."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(ROOT / "ananta/src"),
    str(ROOT / "plugins/agent_messaging_plugin/src"),
    str(Path(__file__).resolve().parent),
]

from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import fixture_assignment  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    SESSION_HOST_HEADLESS,
    TABLE_MANAGED_SESSION,
    WORK_CLASS_READ_ONLY,
)
from agent_messaging_plugin.session_claude_mapping_store import (  # noqa: E402
    upsert_session_claude_mapping,
)
from agent_messaging_plugin.session_context_status_store import (  # noqa: E402
    upsert_session_context_status,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
)

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
PROFILE_PATH = ROOT / "plugins/agent_messaging_plugin/model_profiles/usage_economics.v1.json"


def _check(condition: object, name: str) -> None:
    if not condition:
        raise AssertionError(name)


def _module() -> Any:
    # Deliberately lazy: base-red must first fail on missing plugin metadata,
    # not on importing a production module that does not yet exist at base.
    from agent_messaging_plugin import session_usage_verbs

    return session_usage_verbs


def _state() -> tuple[StateManagementInterface, RealShapeState]:
    raw = RealShapeState()
    return cast("StateManagementInterface", raw), raw


def _spawn(
    state: StateManagementInterface,
    raw_state: RealShapeState,
    instance: str,
    *,
    unit: str = "unt-usage",
    runtime: str = "claude_code",
    model: str = "dispatch-model",
    effort: str = "medium",
    receipt: dict[str, Any] | None = None,
    difficulty: float = 47.0,
    created_at: str | None = None,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=instance,
            lane_id=f"lane-{instance}",
            assignment=fixture_assignment(unit),
            work_class=WORK_CLASS_READ_ONLY,
            budget_line=f"budget-{instance}",
            host=SESSION_HOST_HEADLESS,
            agent_runtime=runtime,
            model=model,
            effort=effort,
            difficulty_score=difficulty,
            selection_receipt=receipt or {},
        ),
    )
    if created_at is not None:
        row = next(row for row in raw_state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION) if row.get("agent_instance_id") == instance)
        row["created_at"] = created_at


def _mapping(state: StateManagementInterface, instance: str, runtime_id: str) -> None:
    upsert_session_claude_mapping(
        state,
        agent_instance_id=instance,
        claude_session_id=runtime_id,
        captured_at=NOW.isoformat(),
        capture_source="usage smoke fixture",
    )


def _context(
    state: StateManagementInterface,
    instance: str,
    runtime_id: str,
    *,
    runtime: str = "claude_code",
    provider: str = "anthropic",
    effort: str = "high",
) -> None:
    upsert_session_context_status(
        state,
        agent_instance_id=instance,
        claude_session_id=runtime_id,
        model="context-model",
        current_tokens=10,
        ceiling=100,
        measured_at=NOW.isoformat(),
        provider=provider,
        runtime=runtime,
        effort=effort,
    )


def _usage(
    *,
    input_tokens: int | None = 100,
    cached_input_tokens: int | None = 0,
    cache_write_input_tokens: int | None = 0,
    output_tokens: int | None = 50,
    reasoning_output_tokens: int | None = 0,
    tool_calls: int | None = 0,
) -> dict[str, Any]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "cache_write_input_tokens": cache_write_input_tokens,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "tool_calls": tool_calls,
        "breakdown": {},
    }


def _total(
    runtime_id: str,
    *,
    model: str = "claude-opus-5-5",
    usage: dict[str, Any] | None = None,
    status: str = "complete",
    meter: str = "supported",
    usage_event_count: int = 1,
    adapter: str = "claude_code",
    last_event_at: str = "2026-10-17T11:58:00+00:00",
    last_poll_at: str = "2026-10-17T11:59:00+00:00",
    advisories: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "runtime_session_id": runtime_id,
        "status": status,
        "meter": meter,
        "adapter": adapter,
        "by_model": {model: _usage()} if usage is None else {model: usage},
        "usage_event_count": usage_event_count,
        "last_event_at": last_event_at,
        "last_poll_at": last_poll_at,
        "sibling_rows_resolved": 0,
        "advisories": advisories or [],
    }


def _totals(rows: list[dict[str, Any]], requested: list[str] | None = None) -> dict[str, Any]:
    found = {str(row["runtime_session_id"]) for row in rows}
    missing = [item for item in requested or [] if item not in found]
    partial = any(row.get("status") != "complete" for row in rows)
    return {
        "status": "partial" if partial else "complete",
        "sessions": rows,
        "not_found": missing,
        "advisories": [],
    }


def _unit(
    state: StateManagementInterface | None,
    identity: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    unavailable: bool = False,
) -> dict[str, Any]:
    module = _module()
    if unavailable:
        reader = None
    else:

        def reader(*, runtime_session_ids: list[str]) -> dict[str, Any]:
            return _totals(rows, runtime_session_ids)

    return module.read_unit_usage(state, identity, totals_reader=reader, now=NOW)


def _process_factory(method: Any, key: str) -> ActionFactory:
    metadata = method._platform_process_metadata
    return ActionFactory(
        process_registry={
            "processes": {
                key: {
                    "parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()},
                },
            },
        },
        template_engine=SimpleNamespace(resolve_templates=lambda action_def, context: action_def),
        state_service=SimpleNamespace(
            generate_unique_string=lambda **_kwargs: {
                "action_status": "completed",
                "data": {"random_string": "usage-smoke"},
            },
        ),
        action_event_recorder=SimpleNamespace(store_action_event=lambda _action: "ae-usage-smoke"),
    )


def _submit(method: Any, key: str, arguments: dict[str, Any]) -> None:
    _process_factory(method, key).submit_action_definition(
        {
            "process_key": key,
            "arguments": arguments,
            "flow_id": "flow-usage-smoke",
            "result_processor_kind": None,
            "error_processor_kind": None,
        },
    )


def _assert_verb_registered() -> None:
    method = getattr(AgentMessagingPlugin, "read_unit_usage", None)
    metadata = getattr(method, "_platform_process_metadata", None)
    parameters = getattr(metadata, "parameters", {})
    _check(
        callable(method) and "unit_id" in parameters and "agent_instance_id" in parameters,
        "VERB_REGISTERED_ON_PLUGIN_WITH_DECLARED_PARAMETERS",
    )


def _assert_bridge_unit_arguments() -> None:
    key = "plugin::agent_messaging_plugin::read_unit_usage"
    method = AgentMessagingPlugin.read_unit_usage
    _submit(method, key, {"unit_id": "unt-usage"})
    _submit(method, key, {"agent_instance_id": "agi-usage"})
    try:
        _submit(method, key, {"xyzzy": "value"})
    except FrameworkError as exc:
        _check(exc.error_code == "action.unknown_arguments", "BRIDGE_ACCEPTS_READ_UNIT_USAGE_ARGUMENTS")
        return
    raise AssertionError("BRIDGE_ACCEPTS_READ_UNIT_USAGE_ARGUMENTS")


def _assert_edge_retryable() -> None:
    plugin = object.__new__(AgentMessagingPlugin)
    definitions = plugin.get_edge_process_definitions()
    for name in ("read_unit_usage", "read_session_usage_window"):
        definition = definitions[name]
        _check(
            definition.error_processor_template_customizations.retryable is True,
            "EDGE_PROCESS_DEFINITION_PRESENT_AND_RETRYABLE",
        )


def _assert_id_arguments() -> None:
    module = _module()
    state, _raw = _state()
    for params in ({}, {"unit_id": "unt-usage", "agent_instance_id": "agi-usage"}):
        try:
            module.read_unit_usage(state, params, totals_reader=lambda **_kw: {}, now=NOW)
        except module.UsageVerbError as exc:
            _check(exc.code == "parameter_invalid", "NEITHER_OR_BOTH_ID_REFUSED_BY_NAME")
        else:
            raise AssertionError("NEITHER_OR_BOTH_ID_REFUSED_BY_NAME")


def run_assertions(assertions: tuple[tuple[str, Callable[[], None]], ...]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=[name for name, _func in assertions])
    args = parser.parse_args()
    selected = [(name, function) for name, function in assertions if args.only in (None, name)]
    failures: list[tuple[str, str]] = []
    for name, function in selected:
        try:
            function()
        except Exception as exc:  # noqa: BLE001 — keep one smoke report per named contract
            failures.append((name, f"{type(exc).__name__}: {exc}"))
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {name}")
    if failures:
        print(f"FAILED {len(failures)}/{len(selected)} named assertions")
        return 1
    print(f"PASSED {len(selected)}/{len(selected)} named assertions")
    return 0


ASSERTIONS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("VERB_REGISTERED_ON_PLUGIN_WITH_DECLARED_PARAMETERS", _assert_verb_registered),
    ("BRIDGE_ACCEPTS_READ_UNIT_USAGE_ARGUMENTS", _assert_bridge_unit_arguments),
    ("EDGE_PROCESS_DEFINITION_PRESENT_AND_RETRYABLE", _assert_edge_retryable),
    ("NEITHER_OR_BOTH_ID_REFUSED_BY_NAME", _assert_id_arguments),
)


def main() -> int:
    return run_assertions(ASSERTIONS)


if __name__ == "__main__":
    raise SystemExit(main())
