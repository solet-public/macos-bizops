"""Key-value round-trip case table, shared by every smoke that models the store.

``run_cases`` works against any object with ``set_key_value``, ``get_key_value``
and ``list_key_values`` (the real plugin, ``StateService``, or a fake), so a
store and the fakes that stand in for it are held to the same contract: a read
returns the value as written.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Protocol, cast

VALUE_CASES: tuple[tuple[str, object], ...] = (
    ("object_with_nested_list", {"name": "alpha", "items": [1, 2, {"deep": True}], "none": None}),
    ("list", ["a", 1, 2.5, None, {"k": "v"}]),
    ("string", "plain text"),
    ("string_holding_json_text", json.dumps({"inner": [1, 2]})),
    ("string_five", "5"),
    ("int", 42),
    ("float", 2.5),
    ("true", True),
    ("false", False),
    ("none", None),
)


class KeyValueStore(Protocol):
    def set_key_value(self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None) -> Mapping[str, object]: ...

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> Mapping[str, object]: ...

    def list_key_values(self, namespace: str | None = None, scope: str | None = None, pattern: str | None = None) -> Mapping[str, object]: ...


def _data(envelope: Mapping[str, object]) -> dict[str, object]:
    data = envelope.get("data")
    return cast(dict[str, object], data) if isinstance(data, dict) else {}


def _completed(envelope: Mapping[str, object]) -> bool:
    return envelope.get("action_status") == "completed"


Report = Callable[[object, str], None]


def _check_round_trips(store: KeyValueStore, report: Report, namespace: str) -> None:
    for label, value in VALUE_CASES:
        key = f"case_{label}"
        written = store.set_key_value(namespace, key, value)
        read = _data(store.get_key_value(namespace, key))
        read_back = read.get("value")
        report(
            _completed(written) and read.get("found") is True and read_back == value and type(read_back) is type(value),
            f"{label}: set then get reads the value as written",
        )


def _check_absent_key(store: KeyValueStore, report: Report, namespace: str) -> None:
    absent = _data(store.get_key_value(namespace, "never_written"))
    report(absent.get("found") is False and absent.get("value") is None, "missing key reads found False and value None")


def _check_scopes(store: KeyValueStore, report: Report, namespace: str) -> None:
    store.set_key_value(namespace, "scoped", {"scope": "global"}, "GLOBAL")
    store.set_key_value(namespace, "scoped", {"scope": "session"}, "SESSION")
    global_read = _data(store.get_key_value(namespace, "scoped", "GLOBAL"))
    session_read = _data(store.get_key_value(namespace, "scoped", "SESSION"))
    report(
        global_read.get("value") == {"scope": "global"} and session_read.get("value") == {"scope": "session"},
        "the same key under GLOBAL and SESSION stays separate",
    )


def _check_list_agrees(store: KeyValueStore, report: Report, namespace: str) -> None:
    rows = cast(list[dict[str, object]], _data(store.list_key_values(namespace)).get("values") or [])
    by_key = {str(row["key"]): row["value"] for row in rows if row.get("scope") == "GLOBAL"}
    expected = {f"case_{label}": value for label, value in VALUE_CASES}
    agrees = all(key in by_key and by_key[key] == value and type(by_key[key]) is type(value) for key, value in expected.items())
    report(agrees, "list_key_values returns the same decoded values as get")


def _check_unserializable_refused(store: KeyValueStore, report: Report, namespace: str) -> None:
    refused = store.set_key_value(namespace, "not_serializable", cast(object, {1, 2}))
    left_behind = _data(store.get_key_value(namespace, "not_serializable"))
    report(not _completed(refused) and left_behind.get("found") is False, "a set value is refused and leaves no row")


def run_cases(store: KeyValueStore, report: Report, namespace: str) -> None:
    """Check every case on ``store`` under ``namespace``; ``report(condition, label)`` records each."""
    for check in (_check_round_trips, _check_absent_key, _check_scopes, _check_list_agrees, _check_unserializable_refused):
        check(store, report, namespace)
