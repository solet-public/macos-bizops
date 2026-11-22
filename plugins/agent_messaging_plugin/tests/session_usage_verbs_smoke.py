#!/usr/bin/env python3
"""Offline conformance smoke fixtures for managed-session usage joins."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping
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
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    SESSION_HOST_HEADLESS,
    TABLE_MANAGED_SESSION,
    TABLE_MODEL_CAPABILITY_CELL,
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
    label: str = "lbl-usage",
    runtime: str = "claude_code",
    model: str = "dispatch-model",
    effort: str = "medium",
    created_at: str | None = None,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=instance,
            session_origin="spawned",
            host=SESSION_HOST_HEADLESS,
            agent_runtime=runtime,
            model=model,
            effort=effort,
            label=label,
            workspace=f"/work/{instance}",
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


def _context(  # pyright: ignore[reportUnusedFunction]
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

    def reader(*, runtime_session_ids: list[str]) -> dict[str, Any]:
        return _totals(rows, runtime_session_ids)

    return module.read_label_usage(state, identity, totals_reader=None if unavailable else reader, now=NOW)


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
        template_engine=cast(Any, SimpleNamespace(resolve_templates=lambda action_def, context: action_def)),
        state_service=cast(
            Any,
            SimpleNamespace(
                generate_unique_string=lambda **_kwargs: {
                    "action_status": "completed",
                    "data": {"random_string": "usage-smoke"},
                },
            ),
        ),
        action_event_recorder=cast(Any, SimpleNamespace(store_action_event=lambda _action: "ae-usage-smoke")),
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
    method = getattr(AgentMessagingPlugin, "read_label_usage", None)
    metadata = getattr(method, "_platform_process_metadata", None)
    parameters = getattr(metadata, "parameters", {})
    _check(
        callable(method) and set(parameters) == {"label", "agent_instance_id"},
        "VERB_REGISTERED_ON_PLUGIN_WITH_DECLARED_PARAMETERS",
    )


def _assert_bridge_label_arguments() -> None:
    key = "plugin::agent_messaging_plugin::read_label_usage"
    method = AgentMessagingPlugin.read_label_usage
    _submit(method, key, {"label": "lbl-usage"})
    _submit(method, key, {"agent_instance_id": "agi-usage"})
    retired_filter = "_".join(("unit", "id"))
    for refused in ({retired_filter: "retired-input"}, {"xyzzy": "value"}):
        try:
            _submit(method, key, refused)
        except FrameworkError as exc:
            _check(exc.error_code == "action.unknown_arguments", "BRIDGE_ACCEPTS_LABEL_AND_INSTANCE_AND_REFUSES_UNDECLARED_ARGUMENTS")
        else:
            raise AssertionError("BRIDGE_ACCEPTS_LABEL_AND_INSTANCE_AND_REFUSES_UNDECLARED_ARGUMENTS")


def _assert_edge_retryable() -> None:
    plugin = object.__new__(AgentMessagingPlugin)
    definitions = plugin.get_edge_process_definitions()
    for name in ("read_label_usage", "read_session_usage_window"):
        customizations = definitions[name].error_processor_template_customizations
        _check(
            customizations is not None and customizations.retryable is True,
            "EDGE_PROCESS_DEFINITION_PRESENT_AND_RETRYABLE",
        )


def _assert_id_arguments() -> None:
    module = _module()
    state, _raw = _state()
    retired_filter = "_".join(("unit", "id"))
    for params in ({}, {"label": "lbl-usage", "agent_instance_id": "agi-usage"}, {retired_filter: "retired-input"}):
        try:
            module.read_label_usage(state, params, totals_reader=lambda **_kw: {}, now=NOW)
        except module.UsageVerbError as exc:
            _check(exc.code == "parameter_invalid", "NEITHER_OR_BOTH_ID_REFUSED_BY_NAME")
        else:
            raise AssertionError("NEITHER_OR_BOTH_ID_REFUSED_BY_NAME")


def _label_session_matches(session: Mapping[str, Any], runtime_id: str, input_tokens: int, output_tokens: int) -> bool:
    observed = session.get("observed", [])
    if len(observed) != 1:
        return False
    usage = observed[0].get("usage", {})
    return session.get("runtime_session_ids") == [runtime_id] and usage.get("input_tokens") == input_tokens and usage.get("output_tokens") == output_tokens


def _assert_label_read_totals() -> None:
    """Read exact-label totals for three distinct sessions and omit other labels."""
    state, raw = _state()
    instances = ["agi-alpha-author", "agi-alpha-reviewer", "agi-alpha-planner"]
    expected: dict[str, tuple[str, int, int]] = {}
    totals: list[dict[str, Any]] = []
    for index, instance in enumerate(instances):
        runtime_id = f"runtime-alpha-{index}"
        input_tokens, output_tokens = (index + 1) * 10, (index + 1) * 5
        _spawn(state, raw, instance, label="alpha")
        _mapping(state, instance, runtime_id)
        expected[instance] = (runtime_id, input_tokens, output_tokens)
        totals.append(_total(runtime_id, usage=_usage(input_tokens=input_tokens, output_tokens=output_tokens)))
    _spawn(state, raw, "agi-beta-other", label="beta")
    _mapping(state, "agi-beta-other", "runtime-beta-0")
    totals.append(_total("runtime-beta-0", usage=_usage(input_tokens=999, output_tokens=999)))

    answer = _unit(state, {"label": "alpha"}, totals)
    sessions = {row["agent_instance_id"]: row for row in answer["sessions"]}
    joined = answer["coverage"] == "full" and set(sessions) == set(instances) and all(_label_session_matches(sessions[instance], *want) for instance, want in expected.items())
    _check(joined, "LABEL_READ_RETURNS_THE_LABELLED_SESSIONS_PER_SESSION_TOTALS")

    unmatched = _unit(state, {"label": "alpha "}, totals)
    _check(
        unmatched["coverage"] == "none" and unmatched["sessions"] == [] and "managed_session_not_found" in unmatched["notes"]["advisories"],
        "LABEL_MATCHED_EXACTLY_AS_STORED",
    )


def _assert_estimate_uses_observed_model() -> None:
    """Price the observed session when its model and effort differ from the request."""
    state, raw = _state()
    _spawn(state, raw, "agi-priced", label="priced", model="claude-sonnet-5", effort="high")
    _mapping(state, "agi-priced", "runtime-priced")
    _context(state, "agi-priced", "runtime-priced", effort="medium")
    raw.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL).extend(
        [
            {"id": "cell-requested", "runtime": "claude_code", "model": "claude-sonnet-5", "effort": "high", "cost_per_task_usd": 0.23, "acceptance": "accepted"},
            {"id": "cell-observed", "runtime": "claude_code", "model": "claude-opus-5-5", "effort": "medium", "cost_per_task_usd": 0.41, "acceptance": "accepted"},
        ],
    )
    answer = _unit(state, {"label": "priced"}, [_total("runtime-priced", model="claude-opus-5-5")])
    _check(
        answer["estimate"] == {"usd": 0.41, "basis": "catalog_lookup", "cell": ["claude_code", "claude-opus-5-5", "medium"]},
        "ESTIMATE_COMES_FROM_THE_OBSERVED_MODEL_AND_EFFORT",
    )


def _assert_retired_result_field_is_absent() -> None:
    """The retired result field stays absent in both populated and empty answers."""
    state, raw = _state()
    _spawn(state, raw, "agi-scored", label="scored")
    _mapping(state, "agi-scored", "runtime-scored")
    found = _unit(state, {"label": "scored"}, [_total("runtime-scored")])
    missing = _unit(state, {"label": "no-such-label"}, [])
    no_state = _unit(None, {"label": "scored"}, [])
    retired_field = "_".join(("difficulty", "score"))
    _check(
        all(retired_field not in answer for answer in (found, missing, no_state)),
        "USAGE_RESULT_OMITS_RETIRED_FIELD",
    )


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
    ("BRIDGE_ACCEPTS_LABEL_AND_INSTANCE_AND_REFUSES_UNDECLARED_ARGUMENTS", _assert_bridge_label_arguments),
    ("EDGE_PROCESS_DEFINITION_PRESENT_AND_RETRYABLE", _assert_edge_retryable),
    ("NEITHER_OR_BOTH_ID_REFUSED_BY_NAME", _assert_id_arguments),
    ("LABEL_READ_RETURNS_THE_LABELLED_SESSIONS_PER_SESSION_TOTALS", _assert_label_read_totals),
    ("ESTIMATE_COMES_FROM_THE_OBSERVED_MODEL_AND_EFFORT", _assert_estimate_uses_observed_model),
    ("USAGE_RESULT_OMITS_RETIRED_FIELD", _assert_retired_result_field_is_absent),
)


def main() -> int:
    return run_assertions(ASSERTIONS)


if __name__ == "__main__":
    raise SystemExit(main())
