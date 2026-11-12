"""JSON-compatible outward forms of native keys and values (no scanning)."""

from __future__ import annotations

import json
from datetime import date, time
from decimal import Decimal
from typing import Any


def normalize_key(value: Any) -> str:
    normalized = normalize_outward(value)
    if isinstance(normalized, str):
        return normalized
    if isinstance(normalized, (list, dict)):
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
    return str(normalized)


def _outward_scalar(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "hex:" + bytes(value).hex()
    if isinstance(value, BaseException):
        return value.args[0] if len(value.args) == 1 else value.args
    if isinstance(value, (Decimal, date, time)):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def normalize_outward(value: Any) -> Any:
    """Make native keys/values JSON-compatible.

    Binary uses a fixed hex representation, so even embedded invalid UTF-8
    has a stable spelling. Tuple keys use compact JSON, never Python repr.
    """
    if isinstance(value, BaseException):
        return normalize_outward(_outward_scalar(value))
    if isinstance(value, dict):
        return {normalize_key(key): normalize_outward(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_outward(item) for item in value]
    return _outward_scalar(value)


def render(value: Any) -> str:
    """Normalize, then serialize: text stays text, anything else is compact JSON."""
    clean = normalize_outward(value)
    return clean if isinstance(clean, str) else json.dumps(clean, ensure_ascii=False, separators=(",", ":"))
