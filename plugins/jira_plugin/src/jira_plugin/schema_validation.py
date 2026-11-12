"""Validate the pinned vendor request schemas without closing open JSON objects."""

from __future__ import annotations

from typing import Any


def resolve(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        return schemas[schema["$ref"].rsplit("/", 1)[-1]]
    return schema


def validate(value: Any, schema: dict[str, Any], label: str, schemas: dict[str, Any]) -> None:
    schema = resolve(schema, schemas)
    if value is None and schema.get("nullable"):
        return
    _alternatives(value, schema, label, schemas)
    _type(value, schema, label)
    if isinstance(value, dict):
        _object(value, schema, label, schemas)
    if isinstance(value, list):
        for item in value:
            validate(item, schema.get("items", {}), label + "[]", schemas)
    _ranges(value, schema, label)


def _alternatives(value: Any, schema: dict[str, Any], label: str, schemas: dict[str, Any]) -> None:
    for part in schema.get("allOf", []):
        validate(value, part, label, schemas)
    alternatives = schema.get("anyOf", schema.get("oneOf", []))
    if not alternatives:
        return
    for part in alternatives:
        try:
            validate(value, part, label, schemas)
            return
        except ValueError:
            continue
    raise ValueError(f"{label} does not match a documented vendor shape")


def _type(value: Any, schema: dict[str, Any], label: str) -> None:
    kind = schema.get("type")
    matches = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        "number": type(value) in (float, int),
        "boolean": type(value) is bool,
    }
    if kind in matches and not matches[kind]:
        raise ValueError(f"{label} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{label} is not a documented enum value")


def _object(value: dict[str, Any], schema: dict[str, Any], label: str, schemas: dict[str, Any]) -> None:
    properties = schema.get("properties", {})
    required = [k for k in schema.get("required", []) if not resolve(properties.get(k, {}), schemas).get("readOnly")]
    if any(k not in value for k in required):
        raise ValueError(f"{label} requires fields: {', '.join(required)}")
    for key, item in value.items():
        if key in properties:
            validate(item, properties[key], f"{label}.{key}", schemas)
        elif schema.get("additionalProperties") is False:
            raise ValueError(f"{label} has an undocumented field")


def _ranges(value: Any, schema: dict[str, Any], label: str) -> None:
    limits: list[tuple[str, str, Any]] = []
    if type(value) in (int, float):
        limits.append(("minimum", "maximum", value))
    if isinstance(value, list):
        limits.append(("minItems", "maxItems", len(value)))
    if isinstance(value, str):
        limits.append(("minLength", "maxLength", len(value)))
    for low, high, measured in limits:
        if low in schema and measured < schema[low]:
            raise ValueError(f"{label} is below the documented minimum")
        if high in schema and measured > schema[high]:
            raise ValueError(f"{label} exceeds the documented maximum")
