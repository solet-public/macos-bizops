#!/usr/bin/env python3
"""Delivery payloads stay opaque to action template resolution.

Result, error and job-completion delivery relay structured output that is
already produced by another action. Template or execution-context placeholder
resolution must never inspect those envelopes: literal ``<<RESULT>>`` prose is
data, not an instruction to the action processor.

Run:

    .venv/bin/python3 ananta/tests/core/actions/action_processor_opaque_relay_bypass_smoke.py
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.actions import action_processor as action_processor_module  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import QueuedAction  # noqa: E402
from ananta.core.state.job_completion_route import COMPLETION_DELIVERY_PROCESS_KEY  # noqa: E402

from agent_messaging_plugin.platform_surface import (  # noqa: E402
    _DELIVER_ERROR_PROCESS_KEY,
    _DELIVER_RESULT_PROCESS_KEY,
)

_NORMAL_PROCESS_KEY = "service_interface::knowledge_service::search"
_OPAQUE_PROCESS_KEYS = (_DELIVER_RESULT_PROCESS_KEY, _DELIVER_ERROR_PROCESS_KEY, COMPLETION_DELIVERY_PROCESS_KEY)
_FAILURES: list[str] = []


def _check(condition: bool, message: str) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {message}")
    if not condition:
        _FAILURES.append(message)


class _CountingStateService:
    """Small StateManagementInterface-shaped seam that counts global lookups."""

    def __init__(self) -> None:
        self.lookup_count = 0

    def list_key_values(self, *, namespace: str) -> dict[str, object]:
        self.lookup_count += 1
        return {
            "action_status": "completed",
            "data": {"values": [{"key": "GLOBAL_VALUE", "value": "global-value"}]},
        }


class _ExecutionContext:
    """A real placeholder-resolution collaborator with one resolvable key."""

    def has_placeholder(self, placeholder: str) -> bool:
        return placeholder == "<<CONTEXT_VALUE>>"

    def resolve_placeholder(self, placeholder: str) -> str:
        if placeholder != "<<CONTEXT_VALUE>>":
            raise AssertionError(f"unexpected placeholder: {placeholder}")
        return "context-value"


class _ExecutionContextManager:
    def __init__(self) -> None:
        self.context = _ExecutionContext()

    def get_context(self, flow_id: str) -> _ExecutionContext:
        if flow_id != "flow-opaque-relay-smoke":
            raise AssertionError(f"unexpected flow_id: {flow_id}")
        return self.context


def _processor(state_service: _CountingStateService) -> ActionProcessor:
    """Construct only the real ActionProcessor seams this smoke exercises."""
    processor = ActionProcessor.__new__(ActionProcessor)
    processor.state_service = state_service
    processor.execution_context_manager = _ExecutionContextManager()
    processor._app_home = "/tmp/action-processor-opaque-relay-smoke"
    return processor


@dataclass
class _OpaqueAction(QueuedAction):
    source_plugin: str | None = None


def _action(process_key: str, arguments: dict[str, object]) -> _OpaqueAction:
    return _OpaqueAction(
        id="ae-opaque-relay-smoke",
        process_key=process_key,
        parameters=json.dumps(arguments),
        notes="owned opaque delivery fixture",
        created_at="now",
        session_id="session-opaque-relay-smoke",
        flow_id="flow-opaque-relay-smoke",
        context_id=None,
        template_namespace="opaque-relay-smoke",
    )


def _resolve_arguments(processor: ActionProcessor, action: _OpaqueAction) -> dict[str, object]:
    prepared = processor._prepare_arguments(action)
    if prepared is None:
        raise AssertionError("fixture parameters must parse")
    return processor._resolve_placeholders(prepared, action)


def _opaque_payload(rows: int = 1, *, process_key: str = _DELIVER_RESULT_PROCESS_KEY) -> dict[str, object]:
    message = "The peer quoted literal <<RESULT>> and <<<GLOBAL_VALUE>>> in a relay; those bytes belong to the delivered result."
    payload = {"entries": [{"ordinal": row, "content": message, "metadata": {"kind": "peer_message"}} for row in range(rows)]}
    context = {"RESULT": "incorrect-local-substitution"}
    if process_key == COMPLETION_DELIVERY_PROCESS_KEY:
        return {"name": "owned-opaque-role", "job_id": "owned-opaque-job", "provider_name": "owned.operation", "status": "completed", "payload": payload, "plugin_context": context}
    return {
        "error_payload" if process_key == _DELIVER_ERROR_PROCESS_KEY else "result_payload": payload,
        "source_process_key": "service_interface::knowledge_service::search",
        "bridge_id": "bridge-opaque-relay-smoke",
        # Deliberately makes <<RESULT>> resolvable on the pre-fix path.
        "plugin_context": context,
    }


def test_opaque_keys_preserve_literal_payloads() -> None:
    """All delivery keys must bypass templates and placeholders."""
    for process_key in _OPAQUE_PROCESS_KEYS:
        state_service = _CountingStateService()
        processor = _processor(state_service)
        expected = _opaque_payload(process_key=process_key)
        actual = _resolve_arguments(processor, _action(process_key, expected))
        _check(
            actual == expected,
            f"{process_key} preserves literal <<RESULT>> / <<<GLOBAL_VALUE>>> payload bytes",
        )
        _check(
            state_service.lookup_count == 0,
            f"{process_key} performs zero global-template lookups",
        )


def test_large_opaque_payload_is_bounded_and_lookup_free() -> None:
    """A large relay cannot reparse itself per leaf or query template state."""
    state_service = _CountingStateService()
    processor = _processor(state_service)
    expected = _opaque_payload(rows=320)
    started_at = time.perf_counter()
    actual = _resolve_arguments(processor, _action(_DELIVER_RESULT_PROCESS_KEY, expected))
    elapsed_seconds = time.perf_counter() - started_at
    _check(actual == expected, "large deliver_result payload remains byte-for-byte unchanged")
    _check(
        elapsed_seconds < 0.5,
        f"large deliver_result resolution completes below 0.5s ({elapsed_seconds:.3f}s)",
    )
    _check(
        state_service.lookup_count == 0,
        "large deliver_result performs zero list_key_values/global-template lookups",
    )


def test_normal_process_still_resolves_templates_and_placeholders() -> None:
    """The bypass is exact-key scoped, not a global resolver disable."""
    state_service = _CountingStateService()
    processor = _processor(state_service)
    arguments: dict[str, object] = {
        "query": "local=<<LOCAL_VALUE>> global=<<<GLOBAL_VALUE>>> context=<<CONTEXT_VALUE>>",
        "plugin_context": {"LOCAL_VALUE": "local-value"},
    }
    actual = _resolve_arguments(processor, _action(_NORMAL_PROCESS_KEY, arguments))
    _check(
        actual["query"] == "local=local-value global=global-value context=context-value",
        "ordinary process still resolves local, global, and execution-context placeholders",
    )
    _check(
        state_service.lookup_count > 0,
        "ordinary process still reaches the global-template lookup path",
    )


def test_plugin_key_drift_is_detected() -> None:
    """Core literals and bridge-surface keys fail loudly if either side changes."""
    core_keys = getattr(action_processor_module, "_OPAQUE_RELAY_PROCESS_KEYS", frozenset())
    _check(
        core_keys == frozenset(_OPAQUE_PROCESS_KEYS),
        "core opaque-relay set equals bridge-delivery and terminal-completion constants",
    )


def main() -> int:
    print("ActionProcessor opaque bridge-relay bypass smoke")
    test_opaque_keys_preserve_literal_payloads()
    test_large_opaque_payload_is_bounded_and_lookup_free()
    test_normal_process_still_resolves_templates_and_placeholders()
    test_plugin_key_drift_is_detected()
    if _FAILURES:
        print(f"\nFAIL: {len(_FAILURES)} check(s) failed")
        return 1
    print("\nPASS: bridge relay payloads bypass resolution; ordinary actions still resolve")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
