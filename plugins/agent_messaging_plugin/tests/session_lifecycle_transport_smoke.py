#!/usr/bin/env python3
"""Transport-shim smoke: the D1 L1 verbs as actually reached through
``AgentMessagingPlugin`` methods (not just the pure functions in
``session_lifecycle_verbs.py``, which ``session_lifecycle_verbs_smoke.py``
already covers). Verifies the ``params``/``state`` extraction, the
``VerbError`` -> ``_failure_result`` mapping, and that ``call_context`` flows
into ``directed_by`` on the session_transition audit trail.

Builds a BARE plugin instance (``object.__new__`` — the full
``AgentMessagingPlugin.__init__`` wants an orchestrator/config this smoke has
no business standing up) and monkeypatches ``_get_state_service`` to hand
back a real ``RealShapeState`` — the same technique other unit smokes in
this suite use to reach an EDGE method's body directly. This does NOT
exercise the platform's process registry, `process_search`, or a live
`process_call` (no unresolved `<<FIELD>>` check) — that layer needs a
running solet and a restart to pick up the new processes; NOT verified
here, and this slice's commit request must say so explicitly.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_lifecycle_transport_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.model_capability_store import load_seed_table, seed_catalog  # noqa: E402
from agent_messaging_plugin.model_capability_verbs import select_dispatch_tier  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    TABLE_MANAGED_SESSION,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_SESSION_TRANSITION,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
)
from agent_messaging_plugin.tmux_adapter import TmuxHostDriver  # noqa: E402

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


def _bare_plugin(state: StateManagementInterface) -> AgentMessagingPlugin:
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    from _register_unit_double import coordinator_actor, seed_binding

    seed_binding(state, role="Coordinator-Main", instance="agi-coordinator", session="ases-coordinator")
    plugin._dispatch_actor_from_state = lambda _context: coordinator_actor()
    return plugin


def _params(**kwargs: object) -> dict[str, Any]:
    return {"parameters": kwargs}


def _state_with_context() -> dict[str, Any]:
    return {"call_context": CallContext.for_operator()}


def _error_code(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    return error.get("code") if isinstance(error, dict) else None


def _selection_params(state: StateManagementInterface) -> dict[str, object]:
    """Seed a fresh accepted catalog and produce the receipt public spawn requires."""
    seed_catalog(state, seed=load_seed_table(), staleness_window_hours=72)
    measured_at = datetime.now(UTC).isoformat()
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row.update(
            acceptance=CELL_ACCEPTANCE_ACCEPTED,
            accepted_at=measured_at,
            measured_at=measured_at,
            last_refresh_run_id="session-lifecycle-transport-fixture",
        )
    selection = select_dispatch_tier(
        state,
        {"required_score": 38, "billing_objective": "metered_usd"},
    )
    selected = selection["selected"]
    return {
        "required_score": selection["required_score"],
        "agent_runtime": selected["runtime"],
        "model": selected["model"],
        "effort": selected["effort"],
    }


def test_phase_selector_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    _selection_params(state)
    plugin = _bare_plugin(state)
    selected = plugin.select_dispatch_tier(
        _params(required_score=50, cost_tolerance=0),
        _state_with_context(),
    )
    cell = selected.get("data", {}).get("selected", {})
    _check(
        selected.get("action_status") == "completed" and (cell.get("runtime"), cell.get("model"), cell.get("effort")) == ("codex", "gpt-6-astra", "medium"),
        "public selector chooses Astra medium at score 50",
    )


def test_stale_selector_transport_queues_one_refresh_action() -> None:
    """rul_becb4bed: a public selection from stale readings succeeds and returns the refresh as a follow-on action."""
    state = cast("StateManagementInterface", RealShapeState())
    _selection_params(state)
    plugin = _bare_plugin(state)
    call = _params(required_score=50, cost_tolerance=0)
    fresh = plugin.select_dispatch_tier(call, _state_with_context())
    _check(fresh.get("action_status") == "completed" and fresh.get("actions") == [], "fresh readings: no follow-on action")
    for row in cast("RealShapeState", state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["measured_at"] = "2020-01-01T00:00:00+00:00"
    stale = plugin.select_dispatch_tier(call, _state_with_context())
    data = stale.get("data", {})
    _check(
        stale.get("action_status") == "completed" and data.get("selected") == fresh.get("data", {}).get("selected") | {"measured_at": "2020-01-01T00:00:00+00:00"} and data.get("stale_readings"),
        "all-stale catalog: the public selector still completes with the same cell and lists the stale readings",
    )
    _check(
        stale.get("actions")
        == [
            {
                "process_key": "plugin::agent_messaging_plugin::refresh_model_capability_catalog",
                "arguments": {"trigger": "selector_stale"},
            }
        ],
        "the refresh rides out as the result's returned action",
    )
    _check("refresh_request" not in data, "the request is lifted out of the data payload into actions")


def test_spawn_session_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    plugin = _bare_plugin(state)
    generic = {"agent_runtime": "claude_code", "model": "fixture-model", "effort": "high", "first_message": "fixture first message"}
    result = plugin.spawn_session(
        _params(workspace="relative/dir", **generic),
        _state_with_context(),
    )
    _check(
        result.get("action_status") == "failed" and _error_code(result) == "generic_workspace_invalid",
        f"spawn_session transport maps VerbError to a failure result with the right code (got {result!r})",
    )

    ok = plugin.spawn_session(
        _params(workspace=str(REPO_ROOT), host="operator", **generic),
        _state_with_context(),
    )
    _check(
        ok.get("action_status") == "failed" and _error_code(ok) == "host_cannot_spawn",
        "a valid-but-doomed spawn (operator host) surfaces host_cannot_spawn through the transport, not an unhandled exception",
    )

    request = plugin_module._spawn_session_request_from_params(  # noqa: SLF001
        {**generic, "workspace": str(REPO_ROOT), "label": "label-transport"},
        {"call_context": CallContext.for_operator()},
        plugin_module._SessionLifecyclePolicyConfig(),  # noqa: SLF001
    )
    _check(request.label == "label-transport" and request.workspace == str(REPO_ROOT), "spawn transport carries the label and workspace into the typed request")


def test_list_and_status_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-z",
            label="lane-z",
            host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    listed = plugin.list_sessions(_params(label="lane-z"), {})
    _check(
        listed.get("action_status") == "completed" and len(listed["data"]["sessions"]) == 1,
        f"list_sessions transport returns the envelope-wrapped sessions list (got {listed!r})",
    )

    unfiltered = plugin.list_sessions(_params(), {})
    _check(
        unfiltered.get("action_status") == "failed" and _error_code(unfiltered) == "filter_required",
        "list_sessions transport maps an omitted filter to filter_required",
    )

    status = plugin.session_status(_params(agent_instance_id="agi-z"), {})
    _check(
        status.get("action_status") == "completed" and status["data"]["agent_instance_id"] == "agi-z",
        "session_status transport returns the ledger row",
    )

    missing = plugin.session_status(_params(agent_instance_id="agi-none"), {})
    _check(
        missing.get("action_status") == "failed" and _error_code(missing) == "session_not_found",
        "session_status(unknown) transport surfaces session_not_found",
    )


def _permission_status(
    pane: str | Exception,
    *,
    runtime: str = "claude_code",
    host: str = "tmux",
    missing_driver: bool = False,
) -> dict[str, Any]:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-prompt",
            label="lane-prompt",
            host=host,
            agent_runtime=runtime,
        ),
    )
    cast("RealShapeState", state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION)[0]["host_ref"] = "fixture-pane"
    driver = object.__new__(TmuxHostDriver)
    registry = {} if missing_driver else {("claude_code", "tmux"): driver}
    capture: dict[str, Any] = {"side_effect": pane} if isinstance(pane, Exception) else {"return_value": pane}
    with (
        patch.dict(session_hosts._REGISTRY, registry, clear=True),
        patch.object(  # noqa: SLF001
            TmuxHostDriver,
            "_capture_pane_text",
            **capture,
        ),
    ):
        result = _bare_plugin(state).session_status(_params(agent_instance_id="agi-prompt"), {})
    result["persisted_row"] = read_managed_session(state, "agi-prompt")
    return result


def test_claude_permission_prompt_state() -> None:
    status = _permission_status("Do you want to proceed?\n❯ 1. Yes\n  2. No")
    _check(
        isinstance(status.get("data"), dict) and status["data"].get("permission_prompt_state") == "blocked",
        "session_status: visible Claude permission prompt reports blocked",
    )


def test_quoted_prompt_phrase_is_not_blocked() -> None:
    quoted = "the brief says Do you want to proceed? is the prompt\n  1. Yes\n  2. No"
    no_options = "Do you want to proceed?\nthe agent is still working"
    scrolled = "Do you want to proceed?\n❯ 1. Yes\n  2. No" + "".join(f"\nlater output {n}" for n in range(20))
    for pane, label in (
        (quoted, "quoted_phrase_must_not_read_blocked"),
        (no_options, "prompt_without_options_not_blocked"),
        (scrolled, "scrolled_prompt_not_blocked"),
    ):
        status = _permission_status(pane)
        _check(status["data"].get("permission_prompt_state") == "not_detected", label)


def test_permission_observation_and_advisory() -> None:
    for pane, expected in (("Ready for input", "not_detected"), ("", "unknown"), ("   \n", "unknown"), (OSError("capture unavailable"), "unknown")):
        status = _permission_status(pane)
        _check(status["data"].get("permission_prompt_state") == expected, f"permission observation {pane!r} reports {expected}")
        _check("permission_prompt_state" not in status["persisted_row"] and status["persisted_row"]["lifecycle_state"] == "spawning", "permission observation never writes a lifecycle or prompt state")
    _check(_permission_status("ready", missing_driver=True)["data"].get("permission_prompt_state") == "unknown", "missing capture driver reports unknown")
    _check(_permission_status("Do you want to proceed?", runtime="codex")["data"].get("permission_prompt_state") == "not_applicable", "non-Claude runtime is not applicable")
    _check(_permission_status("Do you want to proceed?", host="operator")["data"].get("permission_prompt_state") == "not_applicable", "non-tmux host is not applicable")


def test_terminate_retire_directed_by_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-y",
            label="lane-y",
            host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    terminated = plugin.terminate_session(
        _params(agent_instance_id="agi-y"),
        _state_with_context(),
    )
    _check(
        terminated.get("action_status") == "completed" and terminated["data"]["already_terminal"] is False,
        "terminate_session transport lands the transition",
    )

    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_TRANSITION, "filters": {"agent_instance_id": "agi-y"}},
    )
    rows = require_records(result)
    _check(
        len(rows) == 1 and rows[0]["directed_by"] == "operator",
        f"call_context flows through format_directed_by into the audit trail's directed_by column (got {rows[0].get('directed_by') if rows else None!r})",
    )

    retired = plugin.retire_session(_params(agent_instance_id="agi-y"), _state_with_context())
    _check(
        retired.get("action_status") == "completed" and retired["data"]["already_retired"] is False,
        "retire_session transport lands terminated -> retired",
    )


def test_report_alive_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-w",
            label="lane-w",
            host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    bad_status = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="bogus"),
        _state_with_context(),
    )
    _check(
        bad_status.get("action_status") == "failed" and _error_code(bad_status) == "unknown_status",
        "report_alive transport surfaces unknown_status through the failure envelope",
    )
    before = read_managed_session(state, "agi-w").get("report_by")
    working = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="working"),
        _state_with_context(),
    )
    after = read_managed_session(state, "agi-w")
    _check(
        working.get("action_status") == "completed" and working["data"]["lifecycle_state"] == "live",
        "the plugin report_alive process path promotes a spawned row to live",
    )
    _check(
        after["report_by"] != before,
        "the plugin report_alive process path re-arms report_by",
    )
    first_heartbeat = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="heartbeat"),
        _state_with_context(),
    )
    after_first_heartbeat = read_managed_session(state, "agi-w")
    _check(
        first_heartbeat.get("action_status") == "completed" and after_first_heartbeat["last_heartbeat_at"] is not None,
        "a healthy heartbeat writes a distinct liveness reading through the transport path",
    )
    _check(
        after_first_heartbeat["heartbeat_failure_first_at"] is None,
        "a healthy heartbeat writes typed NULL for its absent failure timestamp",
    )
    failing_heartbeat = plugin.report_alive(
        _params(
            agent_instance_id="agi-w",
            status="heartbeat",
            heartbeat_failures_since_last=1,
            heartbeat_failure_first_at="2026-09-04T00:00:00+00:00",
            heartbeat_failure_last_reason="bridge timeout",
        ),
        _state_with_context(),
    )
    after_failing_heartbeat = read_managed_session(state, "agi-w")
    _check(
        failing_heartbeat.get("action_status") == "completed" and after_failing_heartbeat["heartbeat_failure_first_at"] == "2026-09-04T00:00:00+00:00",
        "a failing heartbeat records its failure episode before the healthy recovery",
    )
    recovered_heartbeat = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="heartbeat"),
        _state_with_context(),
    )
    after_recovery = read_managed_session(state, "agi-w")
    _check(
        recovered_heartbeat.get("action_status") == "completed" and after_recovery["last_heartbeat_at"] != after_failing_heartbeat["last_heartbeat_at"],
        "a healthy recovery advances the heartbeat row rather than reusing the failing value",
    )
    _check(
        after_recovery["heartbeat_failure_first_at"] is None and after_recovery["heartbeat_failure_last_reason"] == "",
        "a healthy recovery clears the frozen failure episode with typed NULL",
    )
    before_empty_attempt = dict(after_recovery)
    empty_timestamp = plugin.report_alive(
        _params(
            agent_instance_id="agi-w",
            status="heartbeat",
            heartbeat_failure_first_at="",
        ),
        _state_with_context(),
    )
    _check(
        empty_timestamp.get("action_status") == "failed" and _error_code(empty_timestamp) == "invalid_heartbeat_failure_first_at",
        "an explicit empty timestamp fails loud instead of returning a false success",
    )
    _check(
        read_managed_session(state, "agi-w")["last_heartbeat_at"] == before_empty_attempt["last_heartbeat_at"],
        "the rejected empty timestamp does not advance the heartbeat row",
    )
    state.fail_next("update")
    rejected_write = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="heartbeat"),
        _state_with_context(),
    )
    _check(
        rejected_write.get("action_status") == "failed" and _error_code(rejected_write) == "heartbeat_write_failed" and "injected update failure" in str(rejected_write.get("error")),
        "a state-layer write rejection reaches the caller as a failed report_alive result",
    )


def completed(response: dict[str, Any]) -> bool:
    return response.get("action_status") == "completed" and response.get("error") is None


def main() -> int:
    test_spawn_session_transport()
    test_phase_selector_transport()
    test_stale_selector_transport_queues_one_refresh_action()
    test_list_and_status_transport()
    test_claude_permission_prompt_state()
    test_quoted_prompt_phrase_is_not_blocked()
    test_permission_observation_and_advisory()
    test_terminate_retire_directed_by_transport()
    test_report_alive_transport()
    print(f"PASSED: {_passed}; FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
