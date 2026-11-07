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

import os
import sys
import tempfile
from base64 import b64decode
from dataclasses import asdict, dataclass
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
from _register_unit_double import fixture_assignment  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_control as session_lifecycle_control  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.lane_worktree_evidence import PsoletRetirementEvidenceReader, RetirementCustodyContext  # noqa: E402
from agent_messaging_plugin.lane_worktrees import LaneWorktree  # noqa: E402
from agent_messaging_plugin.model_capability_store import load_seed_table, seed_catalog  # noqa: E402
from agent_messaging_plugin.model_capability_verbs import select_dispatch_tier  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    CELL_ACCEPTANCE_ACCEPTED,
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    TABLE_MANAGED_SESSION,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_SESSION_TRANSITION,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
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
    from _register_unit_double import RegisterUnitDouble
    from managed_dispatch_smoke import _coordinator_actor, _seed_binding

    register = RegisterUnitDouble()
    register.add_unit()
    _seed_binding(state, role="Coordinator-Main", instance="agi-coordinator", session="ases-coordinator")
    plugin._dispatch_actor_from_state = lambda _context: _coordinator_actor()
    plugin._register_unit_client = lambda: register
    return plugin


def _params(**kwargs: object) -> dict[str, Any]:
    if "role_class" in kwargs:
        kwargs.update(spawned_by_role="Coordinator-Main", repository_root=str(REPO_ROOT))
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
    receipt = select_dispatch_tier(
        state,
        {"required_score": 38, "billing_objective": "metered_usd", "dispatch_kind": "infrastructure"},
    )["selection_receipt"]
    selected = receipt["selected"]
    return {
        "difficulty_score": receipt["required_score"],
        "selection_receipt": receipt,
        "agent_runtime": selected["runtime"],
        "model": selected["model"],
        "effort": selected["effort"],
    }


def test_phase_selector_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    _selection_params(state)
    plugin = _bare_plugin(state)
    selected = plugin.select_dispatch_tier(
        _params(required_score=50, dispatch_kind="test", scope_tags=["state_schema"], cost_tolerance=0),
        _state_with_context(),
    )
    cell = selected.get("data", {}).get("selected", {})
    _check(
        selected.get("action_status") == "completed" and (cell.get("runtime"), cell.get("model"), cell.get("effort")) == ("codex", "gpt-6-astra", "medium"),
        "public selector accepts test and selects Astra medium at score 50",
    )
    unknown = plugin.select_dispatch_tier(
        _params(required_score=50, dispatch_kind="unknown-phase"),
        _state_with_context(),
    )
    _check(
        unknown.get("action_status") == "completed" and unknown.get("data", {}).get("selected", {}).get("capability_score", 0) >= 50,
        "public selector accepts open nonblank phase provenance",
    )
    blank = plugin.select_dispatch_tier(
        _params(required_score=50, dispatch_kind=" "),
        _state_with_context(),
    )
    _check(
        blank.get("action_status") == "failed" and _error_code(blank) == "dispatch_kind_required",
        "public selector rejects blank phase provenance",
    )


def test_stale_selector_transport_queues_one_refresh_action() -> None:
    """rul_becb4bed: a public selection from stale readings succeeds and returns the refresh as a follow-on action."""
    state = cast("StateManagementInterface", RealShapeState())
    _selection_params(state)
    plugin = _bare_plugin(state)
    call = _params(required_score=50, dispatch_kind="test", cost_tolerance=0)
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
    result = plugin.spawn_session(
        _params(
            role_class="bogus",
            lane_id="lane-1",
            unit_id="unt_fixture-dispatch",
            work_class="read_only",
            budget_line="b1",
            dispatch_kind="infrastructure",
        ),
        _state_with_context(),
    )
    _check(
        result.get("action_status") == "failed" and _error_code(result) == "unknown_role_class",
        f"spawn_session transport maps VerbError to a failure result with the right code (got {result!r})",
    )

    selector_params = _selection_params(state)
    ok = plugin.spawn_session(
        _params(
            role_class="ephemeral",
            lane_id="lane-1",
            unit_id="unt_fixture-dispatch",
            work_class="read_only",
            budget_line="b1",
            host="operator",
            dispatch_kind="infrastructure",
            **selector_params,
        ),
        _state_with_context(),
    )
    _check(
        ok.get("action_status") == "failed" and _error_code(ok) == "host_cannot_spawn",
        "a valid-but-doomed spawn (operator host) surfaces host_cannot_spawn through the transport, not an unhandled exception",
    )

    request = plugin_module._spawn_session_request_from_params(  # noqa: SLF001
        {"role_class": "ephemeral", "lane_id": "lane-go", "unit_id": "unt-go", "work_class": "read_only", "budget_line": "b1", "first_go": "GO exact"},
        "operator",
    )
    _check(request.first_go == "GO exact", "spawn transport propagates optional first_go bytes into the typed request")


def test_list_and_status_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-z",
            lane_id="lane-z",
            assignment=fixture_assignment(),
            work_class="read_only",
            budget_line="b1",
            host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    listed = plugin.list_sessions(_params(lane_id="lane-z"), {})
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
            lane_id="lane-prompt",
            assignment=fixture_assignment(),
            work_class="read_only",
            budget_line="b1",
            host=host,
            agent_runtime=runtime,
        ),
    )
    cast("RealShapeState", state).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_SESSION)[0]["host_ref"] = "fixture-pane"
    driver = object.__new__(TmuxHostDriver)
    registry = {} if missing_driver else {("claude_code", "tmux"): driver}
    capture = {"side_effect": pane} if isinstance(pane, Exception) else {"return_value": pane}
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
    from managed_dispatch_smoke import _spec

    state = cast("StateManagementInterface", RealShapeState())
    plugin = _bare_plugin(state)
    for runtime, mode, warns in (("claude_code", "default", True), ("claude_code", "bypassPermissions", False), ("codex", "default", False)):
        raw = asdict(_spec(REPO_ROOT, agent_runtime=runtime, permission_mode=mode))
        with patch.object(plugin, "_build_session_lifecycle_policy_config", return_value=plugin_module._SessionLifecyclePolicyConfig()), patch.object(plugin_module, "lifecycle_dispatch_managed_work", return_value={}) as dispatch, patch.object(plugin_module.logger, "warning") as warning:
            result = plugin.dispatch_managed_work(_params(**raw), _state_with_context())
        _check(result.get("action_status") == "completed" and dispatch.call_args.args[1].permission_mode == mode, f"managed {runtime} launch retains explicit {mode}")
        _check(warning.called == warns and (not warns or "permission_mode='default'" in str(warning.call_args)), f"managed {runtime} {mode} reports advisory without changing launch")


def test_terminate_retire_directed_by_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-y",
            lane_id="lane-y",
            assignment=fixture_assignment(),
            work_class="read_only",
            budget_line="b1",
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
            lane_id="lane-w",
            assignment=fixture_assignment(),
            work_class="read_only",
            budget_line="b1",
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


def _threads_configured_custody(calls: list[RetirementCustodyContext | None], agent_instance_id: str) -> bool:
    """The plugin verb hands teardown its configured register CLI and the row's own Unit."""
    custody = calls[0] if len(calls) == 1 else None
    if custody is None or custody.assignment is None:
        return False
    return (custody.agent_instance_id, custody.assignment.unit_id, type(custody.register)) == (
        agent_instance_id,
        fixture_assignment().unit_id,
        PsoletRetirementEvidenceReader,
    )


def _custody_call(plugin: AgentMessagingPlugin, verb: str, request: dict[str, Any]) -> dict[str, Any]:
    return getattr(plugin, verb)(_params(**request), _state_with_context())


def _custody_boundary(plugin: AgentMessagingPlugin, verb: str, arguments: dict[str, Any]) -> None:
    """Use actual metadata and the real ActionFactory/PlatformSurface validator."""
    from ananta.core.actions.action_factory import ActionFactory
    from ananta.core.actions.action_metadata import ActionMetadata
    from process_call_unknown_arguments_smoke import (
        _definition,
        _Recorder,
        _StateService,
        _surface,
        _TemplateEngine,
    )

    from agent_messaging_plugin.platform_surface import BridgeError

    key = f"plugin::agent_messaging_plugin::{verb}"
    metadata = cast("ActionMetadata", getattr(AgentMessagingPlugin, verb)._platform_process_metadata)
    definition = plugin.get_edge_process_definitions()[verb]
    assert definition.name == verb and definition.result_processor_template_customizations is not None
    assert definition.error_processor_template_customizations is not None
    registry = {"processes": {key: {"parameters": {name: item.to_dict() for name, item in metadata.parameters.items()}}}}
    recorder = _Recorder()
    factory = ActionFactory(
        process_registry=registry,
        template_engine=_TemplateEngine(),
        state_service=_StateService(),
        action_event_recorder=recorder,
    )
    try:
        factory.submit_action_definition(_definition(key, arguments))
    except Exception as exc:
        raise AssertionError("all custody declared inputs must pass real ActionFactory") from exc
    surface, _flows = _surface(factory)
    try:
        surface.process_call(key, arguments, trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"}, deliver_to_bridge=False)
    except BridgeError as exc:
        raise AssertionError("all custody declared inputs must pass real PlatformSurface") from exc
    try:
        surface.process_call(key, arguments | {"source_path": "/arbitrary/source"}, trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"}, deliver_to_bridge=False)
    except BridgeError as exc:
        assert exc.code == "action.unknown_arguments" and "source_path" in str(exc), exc
    else:
        raise AssertionError("arbitrary custody paths must fail declared-input validation")
    print(f"  PASS  {verb}: real declared-input boundaries reached")


def _custody_identity_tests(plugin: AgentMessagingPlugin, request: dict[str, Any], packet_id: str) -> None:
    from agent_messaging_plugin.managed_dispatch import DispatchActor

    read = {key: request[key] for key in ("agent_instance_id", "unit_id", "repository_id", "assignment_sha256")}
    for field in ("unit_id", "repository_id", "assignment_sha256"):
        for verb, params in (("preserve_lane_evidence", request), ("read_lane_evidence", read | {"packet_id": packet_id})):
            response = _custody_call(plugin, verb, params | {field: "wrong"})
            assert _error_code(response) == "custody_identity_mismatch", f"wrong {field} must refuse"
    actor = plugin._dispatch_actor_from_state
    plugin._dispatch_actor_from_state = lambda _state: DispatchActor("agi-foreign", "ases-foreign", "live_peer_binding")
    try:
        for verb, params in (("preserve_lane_evidence", request), ("read_lane_evidence", read | {"packet_id": packet_id})):
            response = _custody_call(plugin, verb, params)
            assert _error_code(response) == "coordinator_authority_denied", f"wrong-session custody request must refuse: {response!r}"
    finally:
        plugin._dispatch_actor_from_state = actor
    print("  PASS  wrong-session capture/read authorization reached")


def _custody_history_tests(plugin: AgentMessagingPlugin, request: dict[str, Any], packet_id: str) -> None:
    read = {key: request[key] for key in ("agent_instance_id", "unit_id", "repository_id", "assignment_sha256")}
    original = _custody_call(plugin, "read_lane_evidence", read | {"packet_id": packet_id})
    successor = _custody_call(plugin, "preserve_lane_evidence", request | {"predecessor_packet_id": packet_id, "disposition": "superseded", "rationale": "successor correction"})
    assert successor.get("action_status") == "completed", successor
    new_id = successor["data"]["packet_id"]
    assert new_id != packet_id and successor["data"]["provenance"]["predecessor"] == packet_id
    old = _custody_call(plugin, "read_lane_evidence", read | {"packet_id": packet_id})
    assert old.get("data") == original.get("data"), f"original-history read must remain equal after successor capture: {old!r}"
    newer = _custody_call(plugin, "read_lane_evidence", read | {"packet_id": new_id})
    assert newer["data"]["provenance"]["disposition"] == "superseded"
    cleared = _custody_call(plugin, "preserve_lane_evidence", request | {"rationale": "new packet without predecessor"})
    assert cleared["data"]["provenance"]["predecessor"] is None
    register = plugin._register_unit_client()
    register.events[:] = [(request["unit_id"], {"corrected": True, "packet_id": "not-custody"})]
    corrected = _custody_call(plugin, "read_lane_evidence", read | {"packet_id": packet_id})
    assert corrected["data"] == original["data"], "corrected audit is never custody proof"
    register.events.clear()
    after_withdrawal = _custody_call(plugin, "read_lane_evidence", read | {"packet_id": packet_id})
    assert after_withdrawal["data"] == original["data"], "audit correction/withdrawal cannot change byte-custody verification"
    print("  PASS  original/successor/clear and audit-independent reads reached")


def _custody_payload_tests(plugin: AgentMessagingPlugin, request: dict[str, Any], packet_id: str) -> None:
    read = {key: request[key] for key in ("agent_instance_id", "unit_id", "repository_id", "assignment_sha256")}
    snapshot = _custody_call(plugin, "read_lane_evidence", read)["data"]
    assert snapshot["snapshot_only"] and not snapshot["custody_verified"] and not snapshot["deletion_authority"]
    assert snapshot["fingerprint"] == request["expected_fingerprint"] and "payload_base64" not in snapshot
    payload_args = read | {"packet_id": packet_id, "payload_path": "notes/deep/data.bin", "payload_layer": "worktree"}
    selected = _custody_call(plugin, "read_lane_evidence", payload_args)
    assert b64decode(selected["data"]["payload_base64"]) == b"\x00\xff\x10binary\x00"
    assert selected["data"]["selected_payload_bytes"] == 10
    large = _custody_call(plugin, "read_lane_evidence", payload_args | {"payload_path": "notes/large.bin"})["data"]
    assert large["selected_payload_bytes"] == 65537 and "payload_base64" not in large and large["advisories"]
    bad_selection = _custody_call(plugin, "read_lane_evidence", payload_args | {"packet_id": ""})
    assert _error_code(bad_selection) == "custody_payload_selection_invalid"
    _custody_boundary(plugin, "read_lane_evidence", payload_args)
    _custody_boundary(plugin, "preserve_lane_evidence", request | {"predecessor_packet_id": packet_id})
    print("  PASS  bound snapshot and explicit bounded payload reads reached")


def _custody_audit_test(plugin: AgentMessagingPlugin, request: dict[str, Any]) -> None:
    from agent_messaging_plugin.register_unit_client import RegisterUnitError

    register = plugin._register_unit_client()
    with patch.object(register, "record_unit_event", side_effect=RegisterUnitError("audit_down", "injected audit failure")):
        response = _custody_call(plugin, "preserve_lane_evidence", request | {"rationale": "audit down capture"})
    assert response.get("action_status") == "completed", f"audit-down capture must succeed with advisory: {response!r}"
    assert any("custody_audit_unavailable" in value for value in response["data"]["advisories"])
    with patch.object(plugin, "_register_unit_client", side_effect=RegisterUnitError("audit_missing", "injected optional metadata failure")):
        missing = _custody_call(plugin, "preserve_lane_evidence", request | {"rationale": "audit client absent"})
    assert missing.get("action_status") == "completed" and missing["data"]["advisories"]
    print("  PASS  successful local capture with audit-down advisory reached")


def _custody_public_requests(plugin: AgentMessagingPlugin, request: dict[str, Any], audit: Any) -> None:
    method = getattr(plugin, "preserve_lane_evidence", None)
    response = method(_params(**request), _state_with_context()) if method else {"action_status": "failed", "error": {"code": "capability_missing"}}
    result = {"success": response.get("action_status") == "completed" and response.get("error") is None, "response": response}
    assert result.get("success") is True, "preserve capability must succeed"
    assert audit.call_args is not None
    assert audit.call_args.args[2].agent_instance_id == "agi-coordinator"
    assert audit.call_args.args[2].agent_session_id == "ases-coordinator"
    again = _custody_call(plugin, "preserve_lane_evidence", request)
    assert again["data"]["packet_id"] == response["data"]["packet_id"]
    _custody_identity_tests(plugin, request, response["data"]["packet_id"])
    _custody_history_tests(plugin, request, response["data"]["packet_id"])
    _custody_payload_tests(plugin, request, response["data"]["packet_id"])
    _custody_audit_test(plugin, request)


def test_preserve_read() -> None:
    """Real managed identity and Part 1 fixture precede the public capability assertion."""
    from lane_worktrees_smoke import _custody_lane, _environment, _index_file, _tree_state

    from agent_messaging_plugin.lane_worktree_preservation import LaneCustodyContext, snapshot_lane_custody

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = _environment(root)
        repo, lane = _custody_lane(root, environment, "public-custody")
        (lane.path / "notes/large.bin").write_bytes(b"x" * 65537)
        sentinel = lane.path.parent / "outside-target"
        sentinel.write_bytes(b"protected sentinel, never opened")
        before = _tree_state(lane.path), _index_file(lane, environment)
        state = cast("StateManagementInterface", RealShapeState())
        plugin = _bare_plugin(state)
        assignment = fixture_assignment()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-public-custody",
                lane_id="public-custody",
                assignment=assignment,
                work_class="read_only",
                budget_line="fixture",
                host="operator",
                local_name="public-custody",
                spawned_by_role="Coordinator-Main",
                spawned_by_instance_id="agi-coordinator",
                lane_repo_root=str(repo),
            ),
        )
        context = LaneCustodyContext(repo, "public-custody", "agi-public-custody", assignment, "agi-coordinator")
        snapshot = snapshot_lane_custody(context, environment=environment)
        assert snapshot.success, snapshot
        assert snapshot.snapshot is not None, snapshot
        assert snapshot.snapshot.complete, snapshot
        request = {
            "agent_instance_id": "agi-public-custody",
            "unit_id": assignment.unit_id,
            "repository_id": assignment.repository_id,
            "assignment_sha256": assignment.sha256,
            "expected_fingerprint": snapshot.snapshot.fingerprint,
            "disposition": "preserved",
            "rationale": "public standalone custody fixture",
        }
        original_open = os.open

        def guarded_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            assert str(path) not in {str(sentinel), "outside-target"}, "protected sentinel must never be opened"
            return original_open(path, flags, *args, **kwargs)

        register = plugin._register_unit_client()
        with (
            patch.dict("os.environ", environment, clear=True),
            patch.object(os, "open", guarded_open),
            patch.object(lifecycle_verbs, "terminate_session") as terminate,
            patch.object(lifecycle_verbs, "remove_lane_worktree") as remove,
            patch.object(register, "record_unit_event", wraps=register.record_unit_event) as audit,
        ):
            _custody_public_requests(plugin, request, audit)
            assert lane.path.exists(), "preserve keeps the original lane"
            assert terminate.call_count == remove.call_count == 0, f"preserve/read must make zero host or removal calls: {terminate.call_count}/{remove.call_count}"
        assert (_tree_state(lane.path), _index_file(lane, environment)) == before, "public capture/read leave original bytes/index untouched"
        print("  PASS  standalone preserve/read zero host/removal calls reached")


@dataclass(frozen=True, slots=True)
class PacketLane:
    """A real custody lane behind a managed row its coordinator may preserve and retire.

    Register custody reads are unconfigured, so only the packet or local Git
    custody can permit removal; every call runs under the contained Git
    environment and no host process exists.
    """

    repo: Path
    lane: LaneWorktree
    plugin: AgentMessagingPlugin
    state: StateManagementInterface
    environment: dict[str, str]
    identity: dict[str, str]

    def call(self, verb: str, **request: Any) -> dict[str, Any]:
        with patch.dict("os.environ", self.environment, clear=True):
            return getattr(self.plugin, verb)(_params(**(self.identity | request)), _state_with_context())

    def fingerprint(self) -> str:
        return str(self.call("read_lane_evidence")["data"]["fingerprint"])

    def preserve(self, **request: Any) -> dict[str, Any]:
        fields = {"expected_fingerprint": self.fingerprint(), "disposition": "preserved", "rationale": "fixture evidence"}
        return self.call("preserve_lane_evidence", **(fields | request))

    def lifecycle(self) -> str:
        return str(read_managed_session(self.state, self.identity["agent_instance_id"])["lifecycle_state"])

    def disposition(self) -> object:
        return read_managed_session(self.state, self.identity["agent_instance_id"]).get("worktree_disposition")

    def tip(self) -> str:
        from lane_worktrees_smoke import _run_git

        return _run_git(self.repo, "rev-parse", f"refs/heads/{self.lane.branch}", environment=self.environment).strip()


def packet_lane(root: Path, name: str, *, unique_state: bool = True) -> PacketLane:
    """Provision ``name`` with every kind of custody state, or with none, behind a live managed row."""
    from lane_worktrees_smoke import _custody_lane, _environment, _lane_at_base

    environment = _environment(root)
    repo, lane = _custody_lane(root, environment, name) if unique_state else _lane_at_base(root, environment, name)
    state = cast("StateManagementInterface", RealShapeState())
    plugin = _bare_plugin(state)
    plugin._retirement_evidence_reader = lambda: None  # type: ignore[method-assign]
    assignment = fixture_assignment()
    agent_instance_id = f"agi-{name}"
    spec = ManagedSessionSpec(
        agent_instance_id=agent_instance_id,
        lane_id=name,
        assignment=assignment,
        work_class="production_mutation",
        budget_line="fixture",
        host="operator",
        local_name=name,
        spawned_by_role="Coordinator-Main",
        spawned_by_instance_id="agi-coordinator",
        lane_repo_root=str(repo),
    )
    insert_managed_session(state, spec)
    transition_lifecycle_state(state, agent_instance_id=agent_instance_id, from_state="spawning", to_state=LIFECYCLE_LIVE, directed_by="operator:none")
    identity = {"agent_instance_id": agent_instance_id, "unit_id": assignment.unit_id, "repository_id": assignment.repository_id, "assignment_sha256": assignment.sha256}
    return PacketLane(repo, lane, plugin, state, environment, identity)


def completed(response: dict[str, Any]) -> bool:
    return response.get("action_status") == "completed" and response.get("error") is None


def _reconstruct(packet_lane: PacketLane, packet_id: str) -> tuple[Any, Any]:
    """Re-add the lane from its retained branch and rebuild index then worktree from the packet alone."""
    from lane_worktrees_smoke import _index_state, _rebuild_index, _rebuild_worktree, _run_git, _tree_state

    from agent_messaging_plugin import lane_worktree_preservation as engine

    name = packet_lane.identity["agent_instance_id"].removeprefix("agi-")
    context = engine.LaneCustodyContext(packet_lane.repo, name, packet_lane.identity["agent_instance_id"], fixture_assignment())
    packet = engine.read_lane_custody(context, packet_id, environment=packet_lane.environment).packet
    assert packet is not None, "the packet must verify after its lane is gone"
    _run_git(packet_lane.repo, "worktree", "add", str(packet_lane.lane.path), packet_lane.lane.branch, environment=packet_lane.environment)
    _rebuild_index(engine, context, packet_lane.lane, packet, packet_lane.environment)
    _rebuild_worktree(engine, context, packet_lane.lane, packet, packet_lane.environment)
    return _tree_state(packet_lane.lane.path), _index_state(packet_lane.lane, packet_lane.environment)


def _retire_packet_refusals(lane: PacketLane, packet_id: str, fingerprint: str) -> None:
    """Ambiguous, incomplete, foreign and unauthorized requests refuse before any host or removal."""
    from agent_messaging_plugin.managed_dispatch import DispatchActor

    cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("custody_selection_ambiguous", {"packet_id": packet_id, "expected_fingerprint": fingerprint, "preserve_evidence": True}),
        ("custody_selection_ambiguous", {"packet_id": packet_id, "expected_fingerprint": fingerprint, "disposition": "superseded"}),
        ("custody_selection_invalid", {"packet_id": packet_id}),
        ("custody_selection_invalid", {"preserve_evidence": True, "expected_fingerprint": fingerprint, "disposition": "preserved"}),
        ("custody_selection_invalid", {"disposition": "superseded", "rationale": "superseded without a packet"}),
        ("custody_identity_mismatch", {"packet_id": packet_id, "expected_fingerprint": fingerprint, "unit_id": "unt_wrong"}),
    )
    for code, request in cases:
        assert _error_code(lane.call("retire_session", **request)) == code, (code, request)
    actor = lane.plugin._dispatch_actor_from_state
    lane.plugin._dispatch_actor_from_state = lambda _state: DispatchActor("agi-foreign", "ases-foreign", "live_peer_binding")  # type: ignore[method-assign]
    try:
        assert _error_code(lane.call("retire_session", packet_id=packet_id, expected_fingerprint=fingerprint)) == "coordinator_authority_denied"
    finally:
        lane.plugin._dispatch_actor_from_state = actor  # type: ignore[method-assign]
    assert (lane.lane.path.exists(), lane.lifecycle()) == (True, LIFECYCLE_LIVE), "a refused packet request retains the lane and its host"


def test_preserve_read_retire_roundtrip() -> None:
    """Preserve, read and retire a superseded lane with no manual diff, copy or delete.

    Red on the reviewed base: retire_session ignores the packet, the r12
    predicate refuses the unique WIP and the structured response fails the
    success assertion; nothing raises on import or arguments.
    """
    from lane_worktrees_smoke import _index_state, _tree_state

    with tempfile.TemporaryDirectory(prefix="retire-packet-") as temporary:
        lane = packet_lane(Path(temporary).resolve(), "retire-packet")
        before, tip = (_tree_state(lane.lane.path), _index_state(lane.lane, lane.environment)), lane.tip()
        preserved = lane.preserve(disposition="superseded", rationale="superseded by the delivered candidate")
        assert completed(preserved), f"Part 2 preserve must already succeed: {preserved!r}"
        packet_id, fingerprint = preserved["data"]["packet_id"], preserved["data"]["fingerprint"]
        assert lane.call("read_lane_evidence", packet_id=packet_id)["data"]["custody_verified"] is True
        response = lane.call("retire_session", packet_id=packet_id, expected_fingerprint=fingerprint)
        retire_result = {"success": completed(response), "response": response}
        assert retire_result.get("success") is True, "verified custody must permit retirement"
        assert response["data"]["custody_packet"] == {"packet_id": packet_id, "fingerprint": fingerprint, "captured": False, "worktree_disposition": "removed"}, response
        assert (lane.lane.path.exists(), lane.lifecycle(), lane.disposition(), lane.tip()) == (False, LIFECYCLE_RETIRED, "removed", tip)
        assert any(event.get("packet_id") == packet_id for _, event in lane.plugin._register_unit_client().events), "audit links the packet"
        assert lane.call("read_lane_evidence", packet_id=packet_id)["data"]["custody_verified"] is True, "packet stays readable outside the lane"
        assert _reconstruct(lane, packet_id) == before, "the packet alone reconstructs exact worktree bytes and index"
        _custody_boundary(lane.plugin, "retire_session", lane.identity | {"packet_id": packet_id, "expected_fingerprint": fingerprint})
        print("  PASS  explicit preserve/read/retire roundtrip reached")


def test_retire_packet_refusals() -> None:
    """Every malformed, foreign or unauthorized packet request leaves the host and lane alone."""
    with tempfile.TemporaryDirectory(prefix="retire-refusals-") as temporary:
        lane = packet_lane(Path(temporary).resolve(), "retire-refusals")
        preserved = lane.preserve()
        assert completed(preserved), preserved
        with patch.object(session_lifecycle_control, "_terminate_host", wraps=session_lifecycle_control._terminate_host) as host:
            _retire_packet_refusals(lane, preserved["data"]["packet_id"], preserved["data"]["fingerprint"])
        assert host.call_count == 0, "a refused packet request never reaches the host"
        print("  PASS  ambiguous/incomplete/foreign/unauthorized retire requests reached")


def test_one_call_preserve_retire() -> None:
    """One retire call preserves, read-verifies and retires; the packet then reads back outside the lane."""
    with tempfile.TemporaryDirectory(prefix="retire-one-call-") as temporary:
        lane = packet_lane(Path(temporary).resolve(), "retire-one-call")
        request = {"preserve_evidence": True, "expected_fingerprint": lane.fingerprint(), "disposition": "superseded", "rationale": "one-call supersession"}
        _custody_boundary(lane.plugin, "retire_session", lane.identity | request)
        response = lane.call("retire_session", **request)
        assert completed(response), f"one-call preservation must permit retirement: {response!r}"
        packet = response["data"]["custody_packet"]
        assert (packet["captured"], packet["worktree_disposition"], lane.lane.path.exists()) == (True, "removed", False), packet
        read = lane.call("read_lane_evidence", packet_id=packet["packet_id"], payload_path="notes/deep/data.bin", payload_layer="worktree")
        assert b64decode(read["data"]["payload_base64"]) == b"\x00\xff\x10binary\x00"
        assert read["data"]["provenance"]["disposition"] == "superseded"
        print("  PASS  one-call preserve/retire reached")


def main() -> int:
    test_preserve_read()
    test_preserve_read_retire_roundtrip()
    test_retire_packet_refusals()
    test_one_call_preserve_retire()
    fixture_temp = tempfile.TemporaryDirectory()
    fixture_root = Path(fixture_temp.name)
    original_provision = lifecycle_verbs._provision_spawn_worktree  # noqa: SLF001
    original_retire = lifecycle_verbs._retire_lane_worktree  # noqa: SLF001
    original_remove = lifecycle_verbs.remove_lane_worktree  # noqa: SLF001
    provisioning_calls: list[tuple[str, str, lifecycle_verbs.LaneWorktree]] = []
    retirement_calls: list[dict[str, object]] = []
    custody_calls: list[RetirementCustodyContext | None] = []

    def fixture_provision(
        state: StateManagementInterface,
        *,
        role_name: str,
        agent_instance_id: str,
        repository_root: str,
    ) -> lifecycle_verbs.LaneWorktree:
        del state, repository_root
        worktree = lifecycle_verbs.LaneWorktree(
            repo_root=fixture_root,
            root=fixture_root,
            path=fixture_root,
            branch=f"fixture/{role_name}/{agent_instance_id}",
        )
        provisioning_calls.append((role_name, agent_instance_id, worktree))
        return worktree

    def fixture_retire(row: dict[str, object], custody: RetirementCustodyContext | None = None) -> None:
        retirement_calls.append(row)
        custody_calls.append(custody)

    lifecycle_verbs._provision_spawn_worktree = fixture_provision  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs._retire_lane_worktree = fixture_retire  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.remove_lane_worktree = lambda worktree: None  # type: ignore[assignment]  # noqa: SLF001
    try:
        test_spawn_session_transport()
        test_phase_selector_transport()
        test_stale_selector_transport_queues_one_refresh_action()
        test_list_and_status_transport()
        test_claude_permission_prompt_state()
        test_quoted_prompt_phrase_is_not_blocked()
        test_permission_observation_and_advisory()
        test_terminate_retire_directed_by_transport()
        test_report_alive_transport()
        _check(
            len(provisioning_calls) == 1 and provisioning_calls[0][0] == "lane-1" and provisioning_calls[0][2].repo_root == fixture_root and provisioning_calls[0][2].path == fixture_root and provisioning_calls[0][2].branch == f"fixture/lane-1/{provisioning_calls[0][1]}",
            "transport spawn invokes provisioning with the lane-derived branch",
        )
        _check(
            len(retirement_calls) == 1 and retirement_calls[0].get("agent_instance_id") == "agi-y",
            "transport retirement invokes teardown for the retiring session",
        )
        _check(
            _threads_configured_custody(custody_calls, "agi-y"),
            "transport retirement threads the configured register client and the session's own assignment",
        )
    finally:
        lifecycle_verbs._provision_spawn_worktree = original_provision  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._retire_lane_worktree = original_retire  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.remove_lane_worktree = original_remove  # type: ignore[assignment]  # noqa: SLF001
        fixture_temp.cleanup()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
