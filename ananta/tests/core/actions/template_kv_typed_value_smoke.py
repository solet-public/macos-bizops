#!/usr/bin/env python3
"""Template-variable readers render typed key-value values.

The store hands back a stored object, list, number or boolean as that value,
not only as text. Both readers of <<<VAR>>> substitution must render it the way
template_functions renders a variable: dict or list as JSON, None as empty text
(absent for the engine), everything else through str().

Run:

    .venv/bin/python3 ananta/tests/core/actions/template_kv_typed_value_smoke.py
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import QueuedAction  # noqa: E402
from ananta.platform.new_template_engine import NewTemplateEngine  # noqa: E402

_FAILURES: list[str] = []

_CASES: list[tuple[object, str]] = [
    (5, "5"),
    (2.5, "2.5"),
    (True, "True"),
    ({"a": 1}, json.dumps({"a": 1})),
    ([1, 2], json.dumps([1, 2])),
    ("text", "text"),
]


def _check(condition: bool, message: str) -> None:
    print(f"  [{'PASS' if condition else 'FAIL'}] {message}")
    if not condition:
        _FAILURES.append(message)


class _GetStateService:
    """get_key_value seam returning one completed envelope."""

    def __init__(self, value: object) -> None:
        self.value = value

    def get_key_value(self, *, namespace: str, key: str, scope: str) -> dict[str, object]:
        return {"action_status": "completed", "data": {"value": self.value}}


class _ListStateService:
    """list_key_values seam returning one record for key V."""

    def __init__(self, value: object) -> None:
        self.value = value

    def list_key_values(self, *, namespace: str) -> dict[str, object]:
        return {"action_status": "completed", "data": {"values": [{"key": "V", "value": self.value}]}}


@dataclass
class _Action(QueuedAction):
    source_plugin: str | None = None


def _action() -> _Action:
    return _Action(
        id="ae-template-kv-typed-smoke",
        process_key="service_interface::knowledge_service::search",
        parameters="{}",
        notes="typed key-value fixture",
        created_at="now",
        session_id="session-template-kv-typed-smoke",
        flow_id="flow-template-kv-typed-smoke",
        context_id=None,
        template_namespace="template-kv-typed-smoke",
    )


def test_engine_reader() -> None:
    for value, expected in _CASES:
        engine = NewTemplateEngine(metadata_registry=cast(Any, object()), state_service=cast(Any, _GetStateService(value)))
        actual = engine._query_key_value_store("V", {})
        _check(actual == expected, f"engine reads stored {value!r} as {expected!r} (got {actual!r})")
    engine = NewTemplateEngine(metadata_registry=cast(Any, object()), state_service=cast(Any, _GetStateService(None)))
    _check(engine._query_key_value_store("V", {}) is None, "engine reads a stored None as absent")


def test_processor_reader() -> None:
    for value, expected in _CASES:
        processor = ActionProcessor.__new__(ActionProcessor)
        processor.state_service = cast(Any, _ListStateService(value))
        actual = processor._apply_global_variables("<<<V>>>", _action())
        _check(actual == expected, f"processor renders <<<V>>> for stored {value!r} as {expected!r} (got {actual!r})")
    processor = ActionProcessor.__new__(ActionProcessor)
    processor.state_service = cast(Any, _ListStateService(None))
    actual = processor._apply_global_variables("[<<<V>>>]", _action())
    _check(actual == "[]", f"processor renders a stored None as empty text (got {actual!r})")


def main() -> int:
    print("Template key-value typed value smoke")
    test_engine_reader()
    test_processor_reader()
    if _FAILURES:
        print(f"\nFAIL: {len(_FAILURES)} check(s) failed")
        return 1
    print("\nPASS: both template readers render typed key-value values")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
