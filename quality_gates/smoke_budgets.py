"""Finite smoke budgets derived from reviewed timing evidence, never live load."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path, PurePosixPath
from typing import cast

_MULTIPLIER = 3
_ROUND_UP_SECONDS = 15
_MAXIMUM_SECONDS = 900
_POLICY = {
    "schema_version": 1,
    "multiplier": _MULTIPLIER,
    "round_up_seconds": _ROUND_UP_SECONDS,
    "maximum_seconds": _MAXIMUM_SECONDS,
}
_ROW_KEYS = {
    "entry",
    "base_seconds",
    "observed_seconds",
    "base_commit_sha",
    "base_receipt_sha256",
    "observation_sha256",
}


def _object(value: object, keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError(f"smoke budget record must contain exactly {sorted(keys)}")
    return cast(dict[str, object], value)


def _seconds(value: object) -> float:
    if type(value) not in {int, float}:
        raise ValueError("smoke budget timing must be a finite positive number")
    number = float(cast(float, value))
    if not math.isfinite(number) or number <= 0:
        raise ValueError("smoke budget timing must be a finite positive number")
    return number


def _digest(value: object, length: int) -> None:
    if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is None:
        raise ValueError(f"smoke budget evidence needs a {length}-digit lowercase digest")


def _entry(value: object) -> str:
    if not isinstance(value, str) or not value.endswith(".py"):
        raise ValueError("smoke budget entry must be a relative Python path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or str(path) != value:
        raise ValueError(f"unsafe smoke budget entry: {value!r}")
    return value


def _budget(value: object) -> tuple[str, int]:
    row = _object(value, _ROW_KEYS)
    entry = _entry(row["entry"])
    _digest(row["base_commit_sha"], 40)
    _digest(row["base_receipt_sha256"], 64)
    _digest(row["observation_sha256"], 64)
    measured = max(_seconds(row["base_seconds"]), _seconds(row["observed_seconds"]))
    if measured > _MAXIMUM_SECONDS / _MULTIPLIER:
        raise ValueError(f"{entry}: measured work needs splitting; derived budget exceeds 900s")
    return entry, max(120, math.ceil(measured * _MULTIPLIER / _ROUND_UP_SECONDS) * _ROUND_UP_SECONDS)


def load_budgets(path: Path) -> dict[str, int]:
    """Load exact entries; absent/excluded smoke files need not exist in a seed."""
    value: object = json.loads(path.read_text(encoding="utf-8"))
    document = _object(value, {*_POLICY, "measurements"})
    for key, expected in _POLICY.items():
        if type(document[key]) is not int or document[key] != expected:
            raise ValueError(f"smoke budget policy {key} must be {expected}")
    rows = document["measurements"]
    if not isinstance(rows, list) or not rows:
        raise ValueError("smoke budget measurements must be a nonempty list")
    result: dict[str, int] = {}
    for value in cast(list[object], rows):
        entry, seconds = _budget(value)
        if entry in result:
            raise ValueError(f"duplicate smoke budget entry: {entry}")
        result[entry] = seconds
    return result
