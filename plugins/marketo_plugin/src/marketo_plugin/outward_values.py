"""JSON-safe outward forms of native keys and values (no scanning)."""

from __future__ import annotations

import json
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any


def normalize_value(value: Any) -> Any:
    """Produce the final JSON-safe value, decoding binary text."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {outward_text(key): normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [normalize_value(item) for item in value]
    return _normalize_scalar(value)


def _normalize_scalar(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, BaseException):
        return normalize_value(value.args[0] if len(value.args) == 1 else value.args)
    return str(value)


def outward_text(value: Any) -> str:
    """Stringify only normalized values, including composite dictionary keys."""
    normalized = normalize_value(value)
    return normalized if isinstance(normalized, str) else json.dumps(normalized, ensure_ascii=False)
