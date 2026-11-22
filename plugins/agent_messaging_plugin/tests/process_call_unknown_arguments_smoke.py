#!/usr/bin/env python3
"""Regression guard for rejecting undeclared process-call arguments offline.

Exercises the real ActionFactory submission boundary and the real bridge wrapper
for one service_interface verb and one plugin verb.  The action processor uses
the same validator before dispatching service-interface actions, preventing its
former filter from silently dropping a bypassed queued argument.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

if TYPE_CHECKING:
    from ananta.core.actions.action_factory import StateService
    from ananta.core.actions.action_processor import QueuedActionProtocol

    from agent_messaging_plugin.bridge_sessions import BridgeSessionManager

from ananta.core.actions.action_factory import (  # noqa: E402
    UNKNOWN_ARGUMENTS_ERROR_CODE,
    ActionFactory,
)
from ananta.core.actions.action_metadata import ActionMetadata  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402

from agent_messaging_plugin.http_routes import _bridge_error_response  # noqa: E402
from agent_messaging_plugin.models import BridgeSessionState  # noqa: E402
from agent_messaging_plugin.platform_surface import (  # noqa: E402
    BridgeError,
    PlatformSurface,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402

_SERVICE_KEY = "service_interface::knowledge_service::search"
_PLUGIN_KEY = "plugin::agent_messaging_plugin::peer_identity"
_SESSION_START_KEYS = ("plugin::agent_messaging_plugin::spawn_session",)
_FLOW_ID = "flow-unknown-arguments-smoke"
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


class _TemplateEngine:
    def resolve_templates(
        self,
        action_def: dict[str, object],
        context: dict[str, object],
    ) -> dict[str, object]:
        del context
        return action_def


class _StateService:
    def generate_unique_string(self, length: int, encoding: str) -> dict[str, object]:
        return {
            "action_status": "completed",
            "data": {"random_string": "smoke"[:length]},
        }


class _Recorder:
    def __init__(self) -> None:
        self.actions: list[dict[str, object]] = []

    def store_action_event(self, action: dict[str, object]) -> str:
        self.actions.append(action)
        return f"ae-smoke-{len(self.actions)}"


class _FlowManager:
    def __init__(self) -> None:
        self.failed: list[tuple[str, str]] = []

    def create_flow(self, **_kwargs: object) -> str:
        return _FLOW_ID

    def update_flow_status(self, flow_id: str, status: str) -> None:
        self.failed.append((flow_id, status))


class _CompilationContextBuilder:
    def build_context(self, **_kwargs: object) -> dict[str, object]:
        return {}


class _BridgeManager:
    def __init__(self, bridge: BridgeSessionState) -> None:
        self._bridge = bridge

    def get(self, bridge_id: str) -> BridgeSessionState | None:
        return self._bridge if bridge_id == self._bridge.bridge_id else None


def _registry() -> dict[str, object]:
    session_processes: dict[str, object] = {}
    for process_key in _SESSION_START_KEYS:
        method = getattr(AgentMessagingPlugin, f"{process_key.rsplit('::', 1)[1]}_action")
        metadata = cast(ActionMetadata, getattr(method, "_platform_process_metadata"))  # noqa: B009
        session_processes[process_key] = {
            "parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()},
        }
    return {
        "processes": {
            **session_processes,
            _SERVICE_KEY: {
                "parameters": {
                    "query": {"required": True},
                    "top_k": {"required": False},
                },
            },
            _PLUGIN_KEY: {"parameters": {}},
        },
    }


def _factory(
    registry: dict[str, object] | None = None,
    recorder: _Recorder | None = None,
) -> tuple[ActionFactory, _Recorder]:
    recorder = recorder if recorder is not None else _Recorder()
    return (
        ActionFactory(
            process_registry=registry if registry is not None else _registry(),
            template_engine=_TemplateEngine(),
            state_service=cast("StateService", _StateService()),
            action_event_recorder=recorder,
        ),
        recorder,
    )


def _definition(process_key: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "process_key": process_key,
        "arguments": arguments,
        "flow_id": _FLOW_ID,
        "result_processor_kind": None,
        "error_processor_kind": None,
    }


def _surface(factory: ActionFactory) -> tuple[PlatformSurface, _FlowManager]:
    bridge = BridgeSessionState(bridge_id="agc-unknown-arguments", session_id="sess-smoke")
    flows = _FlowManager()
    return (
        PlatformSurface(
            action_factory=factory,
            flow_manager=flows,
            compilation_context_builder=_CompilationContextBuilder(),
            bridge_manager=cast("BridgeSessionManager", _BridgeManager(bridge)),
            export_policy=ProcessExportPolicy(
                allow_patterns=("service_interface::*", "plugin::*"),
            ),
        ),
        flows,
    )


def _assert_core_unknown(process_key: str, arguments: dict[str, object]) -> None:
    factory, _recorder = _factory()
    try:
        factory.submit_action_definition(_definition(process_key, arguments))
    except FrameworkError as exc:
        _check(exc.error_code == UNKNOWN_ARGUMENTS_ERROR_CODE, f"{process_key} core code")
        _check("xyzzy" in str(exc), f"{process_key} core error names offending key")
        _check(
            "Declared arguments" in str(exc),
            f"{process_key} core error names declared set",
        )
    else:
        _check(False, f"{process_key} core rejects undeclared argument")


def _assert_bridge_unknown(process_key: str, arguments: dict[str, object]) -> None:
    factory, _recorder = _factory()
    surface, flows = _surface(factory)
    try:
        surface.process_call(
            process_key,
            arguments,
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        _check(exc.code == UNKNOWN_ARGUMENTS_ERROR_CODE, f"{process_key} bridge preserves code")
        _check("xyzzy" in str(exc), f"{process_key} bridge error names offending key")
        _check(
            "Declared arguments" in str(exc),
            f"{process_key} bridge error names declared set",
        )
        _check(flows.failed == [(_FLOW_ID, "failed")], f"{process_key} bridge fails its flow")
    else:
        _check(False, f"{process_key} bridge rejects undeclared argument")


def test_unknown_arguments_refused_on_both_families_and_surfaces() -> None:
    cases: tuple[tuple[str, dict[str, object]], ...] = (
        (_SERVICE_KEY, {"query": "needle", "xyzzy": "bad"}),
        (_PLUGIN_KEY, {"xyzzy": "bad"}),
    )
    for process_key, arguments in cases:
        _assert_core_unknown(process_key, arguments)
        _assert_bridge_unknown(process_key, arguments)


def test_service_processor_rejects_a_bypassed_undeclared_argument() -> None:
    processor = object.__new__(ActionProcessor)
    action = cast("QueuedActionProtocol", SimpleNamespace(process_key=_SERVICE_KEY))
    try:
        processor._filter_and_inject_arguments(
            {"query": "needle", "xyzzy": "bad"},
            {"query": {"required": True}},
            action,
        )
    except FrameworkError as exc:
        _check(
            exc.error_code == UNKNOWN_ARGUMENTS_ERROR_CODE,
            "service processor rejects a bypassed undeclared argument with the action code",
        )
    else:
        _check(False, "service processor never silently drops a bypassed undeclared argument")


def _assert_bridge_argument_error(
    process_key: str,
    arguments: dict[str, object],
    expected_code: str,
) -> None:
    factory, recorder = _factory()
    surface, flows = _surface(factory)
    label = f"{process_key} {arguments!r}"
    try:
        surface.process_call(
            process_key,
            arguments,
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        _check(exc.code == expected_code, f"{label} preserves {expected_code}")
    else:
        _check(False, f"{label} is refused")
    _check(flows.failed == [(_FLOW_ID, "failed")], f"{label} fails its flow")
    _check(not recorder.actions, f"{label} never queues a session-start action")


def test_retired_session_arguments_survive_the_public_bridge_boundary() -> None:
    for process_key in _SESSION_START_KEYS:
        for field in ("ttl_seconds", "expires_at"):
            for value in (None, 0, "", "not-a-lifetime", {"malformed": True}):
                _assert_bridge_argument_error(
                    process_key,
                    {field: value},
                    "retired_session_ttl_argument",
                )
        _assert_bridge_argument_error(
            process_key,
            {"unrelated": 1},
            "action.unknown_arguments",
        )
    for field in ("ttl_seconds", "expires_at"):
        _assert_bridge_argument_error(
            _PLUGIN_KEY,
            {field: None},
            "action.unknown_arguments",
        )


def test_declared_arguments_still_pass_and_missing_required_keeps_its_code() -> None:
    factory, recorder = _factory()
    factory.submit_action_definition(_definition(_SERVICE_KEY, {"query": "needle", "top_k": 1}))
    factory.submit_action_definition(_definition(_PLUGIN_KEY, {}))
    _check(len(recorder.actions) == 2, "declared service and plugin arguments still submit")

    try:
        factory.submit_action_definition(_definition(_SERVICE_KEY, {"top_k": 1}))
    except FrameworkError as exc:
        _check(
            exc.error_code == "action.missing_required_arguments",
            "missing required argument retains its existing core code",
        )
    else:
        _check(False, "missing required argument is refused at core boundary")

    surface, _flows = _surface(factory)
    try:
        surface.process_call(
            _SERVICE_KEY,
            {"top_k": 1},
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "ags-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        _check(
            exc.code == "bridge.process_call_failed",
            "missing required argument retains its existing bridge code",
        )
    else:
        _check(False, "missing required argument is refused at bridge boundary")


_UNKNOWN_PROCESS_KEY = "plugin::agent_messaging_plugin::no_such_process"  # wint:negative-fixture: deliberately unregistered, the refusal under test
_BROKEN_ENTRY_KEY = "plugin::agent_messaging_plugin::broken_entry"  # wint:negative-fixture: deliberately a malformed registry entry, never a real process


def _refusal_over_http(
    process_key: str,
    arguments: dict[str, object],
    registry: dict[str, object] | None = None,
) -> tuple[BridgeError, int, dict[str, object]]:
    """Dispatch through the real surface, then render the refusal as the route does."""
    factory, _recorder = _factory(registry)
    surface, _flows = _surface(factory)
    try:
        surface.process_call(
            process_key,
            arguments,
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        response = _bridge_error_response(exc)
        body = json.loads(bytes(response.body))
        return exc, response.status_code, body
    raise AssertionError(f"{process_key} was not refused")


def test_a_callers_mistake_is_a_4xx_and_the_body_is_unchanged() -> None:
    """The unknown process / missing argument answer 4xx under the SAME code and message.

    Mutation: map ``bridge.process_call_failed`` to 404 in the table, or drop the
    status, and the 500 / body assertions below turn red.
    """
    exc, status, body = _refusal_over_http(_UNKNOWN_PROCESS_KEY, {})
    _check(status == 404, f"unknown process is a 404 (got {status})")
    _check(
        body
        == {
            "code": "bridge.process_call_failed",
            "message": f"Process '{_UNKNOWN_PROCESS_KEY}' not found or malformed in registry",
        },
        f"unknown process body keeps its code and message (got {body})",
    )
    _check(exc.code == "bridge.process_call_failed", "unknown process keeps the bridge code")

    exc, status, body = _refusal_over_http(_SERVICE_KEY, {"top_k": 1})
    _check(status == 400, f"missing required argument is a 400 (got {status})")
    _check(
        body["code"] == "bridge.process_call_failed" and "Missing required arguments" in str(body["message"]),
        f"missing required argument body keeps its code and message (got {body})",
    )

    exc, status, body = _refusal_over_http(_PLUGIN_KEY, {"message": "wrong name"})
    _check(status == 400, f"unknown argument is a 400 (got {status})")
    _check(body["code"] == UNKNOWN_ARGUMENTS_ERROR_CODE, f"unknown argument keeps its own code (got {body['code']})")
    _check(exc.status is None, "unknown argument relies on the table, not a carried status")


def test_true_server_faults_stay_500() -> None:
    """A malformed registry or a fault inside submission is not the caller's mistake.

    Mutation: answer every ACTION_PROCESS_NOT_FOUND with 404 (skip the registry
    check) and the malformed-entry assertion turns red.
    """
    registry = _registry()
    processes = cast("dict[str, object]", registry["processes"])
    processes[_BROKEN_ENTRY_KEY] = "not-a-process-definition"
    _exc, status, body = _refusal_over_http(_BROKEN_ENTRY_KEY, {}, registry)
    _check(status == 500, f"a malformed registry entry is a server fault, 500 (got {status})")
    _check(body["code"] == "bridge.process_call_failed", "malformed entry keeps the bridge code")

    _exc, status, _body = _refusal_over_http(_UNKNOWN_PROCESS_KEY, {}, {"processes": ["not", "a", "dict"]})
    _check(status == 500, f"a registry whose processes is not a dict is a server fault, 500 (got {status})")

    class _FailingRecorder(_Recorder):
        def store_action_event(self, action: dict[str, object]) -> str:
            raise RuntimeError("action store unavailable")

    factory, _recorder = _factory(recorder=_FailingRecorder())
    surface, _flows = _surface(factory)
    try:
        surface.process_call(
            _PLUGIN_KEY,
            {},
            trigger_data={"bridge_id": "agc-unknown-arguments", "session_id": "sess-smoke"},
            deliver_to_bridge=False,
        )
    except BridgeError as exc:
        _check(_bridge_error_response(exc).status_code == 500, "a fault inside submission stays a 500")
    else:
        _check(False, "a fault inside submission is refused")


def main() -> int:
    logging.getLogger("agent_messaging_plugin.platform_surface").setLevel(logging.CRITICAL)
    print("process-call undeclared argument refusal smoke")
    test_unknown_arguments_refused_on_both_families_and_surfaces()
    test_service_processor_rejects_a_bypassed_undeclared_argument()
    test_retired_session_arguments_survive_the_public_bridge_boundary()
    test_declared_arguments_still_pass_and_missing_required_keeps_its_code()
    test_a_callers_mistake_is_a_4xx_and_the_body_is_unchanged()
    test_true_server_faults_stay_500()
    if _failed:
        print(f"\nFAIL: {len(_failed)} check(s) failed")
        return 1
    print(f"\nPASS: {_passed} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
