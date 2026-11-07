#!/usr/bin/env python3
"""Registered metadata and result-shape checks for role-read receipt actions."""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions.action_metadata import ParameterType  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import (  # noqa: E402
    InvocationSchemaGenerator,
)
from ananta.core.process_registry.plugin_process_scanner import (  # noqa: E402
    PluginProcessScanner,
)
from ananta.core.process_registry.plugin_registration_validator import (  # noqa: E402
    PluginRegistrationValidator,
)
from ananta.core.process_registry.service_interface_metadata_generator import (  # noqa: E402
    ServiceInterfaceMetadataGenerator,
)
from ananta.llm.agent_messaging.role_read_models import (  # noqa: E402
    RoleReadAck,
    RoleReadReceiptResult,
)
from ananta.llm.agent_messaging.service import AgentMessagingError  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

_SESSION_ID = "ases-role-read-schema"
_INSTANCE_ID = "agi-role-read-schema"
_PLUGIN_NAME = "agent_messaging_plugin"
_RECEIPT_ACTIONS = frozenset({
    "peer_ack_role_read_page",
    "peer_role_read_receipts",
})


class _PluginManager:
    def __init__(self, plugin: AgentMessagingPlugin) -> None:
        self.plugins = {_PLUGIN_NAME: plugin}
        self.orchestrator_ref = None


class _Registry:
    def resolve_by_agent_session_id(self, agent_session_id: str) -> BridgeBinding:
        assert agent_session_id == _SESSION_ID
        return BridgeBinding(
            bridge_id="agc-schema",
            agent_id="codex",
            agent_instance_id=_INSTANCE_ID,
            session_label="schema-smoke",
            parent_pid=1,
            agent_session_id=_SESSION_ID,
        )


class _Service:
    def __init__(self, *, reject: bool = False) -> None:
        self.reject = reject

    def acknowledge_role_read_page(
        self, *, agent_session_id: str, agent_instance_id: str, token: str,
    ) -> RoleReadAck:
        assert agent_session_id == _SESSION_ID
        assert agent_instance_id == _INSTANCE_ID
        assert token == "page-token"
        if self.reject:
            raise AgentMessagingError("receipt page rejected")
        return RoleReadAck(status="acknowledged", receipt_count=2)

    def role_read_receipts(
        self, *, agent_instance_id: str, candidates: list[tuple[str, str]],
    ) -> tuple[RoleReadReceiptResult, ...]:
        assert agent_instance_id == _INSTANCE_ID
        assert candidates in ([], [("Coordinator-Main", "arr-example")])
        if self.reject:
            raise AgentMessagingError("receipt lookup rejected")
        if not candidates:
            return ()
        return (RoleReadReceiptResult(
            recipient_key="Coordinator-Main", role_row_id="arr-example", served=True,
        ),)


def _plugin(*, reject: bool) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._peer_registry = cast(Any, _Registry())  # noqa: SLF001
    plugin._service = cast(Any, _Service(reject=reject))  # noqa: SLF001
    return plugin


def _assert_action_result(
    result: dict[str, Any], *, expected_status: str, method_name: str,
    registered_schema: Any,
) -> None:
    assert set(result) == {"action_status", "data", "actions", "error", "timestamp"}
    assert result["action_status"] == expected_status
    schema = registered_schema
    assert schema is not None, f"{method_name} must declare return_value_schema"
    if expected_status == "completed":
        assert set(result["data"]) == set(schema.properties)
        assert result["error"] is None
    else:
        assert result["error"]["code"] in schema.error_conditions
    assert len(schema.examples) == 2
    assert {example["action_status"] for example in schema.examples} == {
        "completed", "failed",
    }


def _is_platform_process(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "platform_process"
        for decorator in node.decorator_list
    )


def _is_receipt_action_name(name: str) -> bool:
    return any(part in name for part in ("receipt", "role_read", "role_ack"))


def _has_return_schema(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    return any(
        isinstance(decorator, ast.Call)
        and isinstance(decorator.func, ast.Name)
        and decorator.func.id == "platform_process"
        and any(keyword.arg == "return_value_schema" for keyword in decorator.keywords)
        for decorator in node.decorator_list
    )


def _ast_receipt_decorators() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    source_path = (
        REPO_ROOT / "plugins" / _PLUGIN_NAME / "src" /
        Path(AgentMessagingPlugin.__module__.replace(".", "/") + ".py")
    )
    module = ast.parse(
        source_path.read_text(encoding="utf-8"), filename=str(source_path),
    )
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in ast.walk(module):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_platform_process(node) or not _is_receipt_action_name(node.name):
            continue
        assert node.name not in found, f"duplicate platform process decorator: {node.name}"
        found[node.name] = node
    assert set(found) == _RECEIPT_ACTIONS, (
        "AST census of role-read receipt siblings changed: "
        f"expected {sorted(_RECEIPT_ACTIONS)}, got {sorted(found)}"
    )
    for name, node in found.items():
        assert _has_return_schema(node), (
            f"{name} source decorator must declare return_value_schema"
        )
    return found


def _registered_schemas() -> dict[str, Any]:
    plugin = AgentMessagingPlugin()
    schema_generator = InvocationSchemaGenerator()
    scanner = PluginProcessScanner(
        plugin_manager=cast(Any, _PluginManager(plugin)),
        validator=PluginRegistrationValidator(),
        metadata_generator=ServiceInterfaceMetadataGenerator(),
        schema_generator=schema_generator,
    )
    registry: dict[str, object] = {"processes": {}}
    scanner.scan_and_register(registry)
    processes = registry["processes"]
    assert isinstance(processes, dict)
    registered: dict[str, Any] = {}
    for name in _RECEIPT_ACTIONS:
        process_key = f"plugin::{_PLUGIN_NAME}::{name}"
        assert process_key in processes, f"normal scanner did not register {process_key}"
        entry = processes[process_key]
        assert isinstance(entry, dict)
        registered[name] = entry["return_value_schema"]
    return registered


def _assert_property_types(schemas: dict[str, Any]) -> None:
    expected = {
        "peer_ack_role_read_page": {
            "status": ParameterType.STRING,
            "receipt_count": ParameterType.INTEGER,
        },
        "peer_role_read_receipts": {
            "agent_instance_id": ParameterType.STRING,
            "results": ParameterType.LIST,
        },
    }
    for action_name, properties in expected.items():
        schema = schemas[action_name]
        assert schema is not None, f"{action_name} must declare return_value_schema"
        assert set(schema.properties) == set(properties)
        for property_name, parameter_type in properties.items():
            assert schema.properties[property_name].type is parameter_type, (
                f"{action_name}.{property_name} must be {parameter_type.value}"
            )


def main() -> int:
    ast_receipt_actions = _ast_receipt_decorators()
    schemas = _registered_schemas()
    _assert_property_types(schemas)
    assert set(ast_receipt_actions) == set(schemas)

    ack_success = _plugin(reject=False).peer_ack_role_read_page(
        {"agent_session_id": _SESSION_ID, "page_token": "page-token"}, {},
    )
    ack_failure = _plugin(reject=True).peer_ack_role_read_page(
        {"agent_session_id": _SESSION_ID, "page_token": "page-token"}, {},
    )
    receipts_success = _plugin(reject=False).peer_role_read_receipts(
        {"agent_session_id": _SESSION_ID, "candidates": [{
            "recipient_key": "Coordinator-Main", "role_row_id": "arr-example",
        }]}, {},
    )
    receipts_failure = _plugin(reject=True).peer_role_read_receipts(
        {"agent_session_id": _SESSION_ID, "candidates": [{
            "recipient_key": "Coordinator-Main", "role_row_id": "arr-example",
        }]}, {},
    )
    receipts_empty = _plugin(reject=False).peer_role_read_receipts(
        {"agent_session_id": _SESSION_ID, "candidates": []}, {},
    )

    _assert_action_result(
        ack_success, expected_status="completed", method_name="peer_ack_role_read_page",
        registered_schema=schemas["peer_ack_role_read_page"],
    )
    _assert_action_result(
        ack_failure, expected_status="failed", method_name="peer_ack_role_read_page",
        registered_schema=schemas["peer_ack_role_read_page"],
    )
    _assert_action_result(
        receipts_success, expected_status="completed", method_name="peer_role_read_receipts",
        registered_schema=schemas["peer_role_read_receipts"],
    )
    _assert_action_result(
        receipts_failure, expected_status="failed", method_name="peer_role_read_receipts",
        registered_schema=schemas["peer_role_read_receipts"],
    )
    assert receipts_empty["action_status"] == "completed"
    assert receipts_empty["data"] == {"agent_instance_id": _INSTANCE_ID, "results": []}
    print("role_read_receipt_schema_smoke: both registered schemas match success/failure results")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
