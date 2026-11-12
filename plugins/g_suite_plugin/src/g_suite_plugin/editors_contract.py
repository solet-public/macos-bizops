"""Dispatch-time contract for the Docs, Sheets and Slides typed processes.

Every editor verb is checked here before its background job is queued:

- Credential query parameters (``key``, ``access_token``, ``oauth_token``) are
  refused. The selected connected account supplies authentication; a supplied
  value is never echoed, queued or sent, whatever its length.
- Path and query arguments are checked against the pinned discovery parameter
  schemas (types, enums, patterns, ranges), including the API-wide parameters.
- The JSON ``body`` is checked against the method's discovery request schema:
  every field must be a declared vendor field of the right JSON type and enum,
  and each batchUpdate ``Request`` must set exactly one request kind (Google's
  ``oneof``). Bodies are passed through unchanged; nothing is invented.

Pass condition for every refusal here: the caller supplies vendor-documented
arguments. The shipped component is the discovery document packaged with the
plugin, read on every dispatch. ``smoke_editors_transcripts`` exercises the pass
path for every method.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .workspace_actions import request_arguments
from .workspace_registry import WORKSPACE_METHODS, discovery_document

EDITOR_PRODUCTS: tuple[str, ...] = ("docs", "sheets", "slides")
CREDENTIAL_PARAMETERS: tuple[str, ...] = ("access_token", "key", "oauth_token")
# Google's batchUpdate request element is a proto oneof: exactly one kind per entry.
UNION_SCHEMAS: frozenset[str] = frozenset({"Request"})
_INTEGER_RANGES: dict[str, tuple[int, int]] = {"int32": (-(2**31), 2**31 - 1), "uint32": (0, 2**32 - 1)}


def editor_verbs() -> list[str]:
    return [verb for verb, (product, _) in WORKSPACE_METHODS.items() if product in EDITOR_PRODUCTS]


def refuse_credential_parameters(verb: str, params: dict[str, Any]) -> None:
    supplied = [name for name in CREDENTIAL_PARAMETERS if name in params]
    if supplied:
        raise ValueError(f"{verb}: {', '.join(supplied)} cannot be passed; the connected Google account authenticates every call. Select it with 'account' (email or alias), or connect one with start_interface then connect_account.")


def _type_error(path: str, expected: str) -> ValueError:
    return ValueError(f"{path} must be {expected}")


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# JSON scalar type -> (accepts the value?, what the caller must send).
_SCALARS: dict[str, tuple[Callable[[Any], bool], str]] = {
    "string": (lambda value: isinstance(value, str), "a string"),
    "boolean": (lambda value: isinstance(value, bool), "true or false"),
    "integer": (_is_integer, "an integer"),
    "number": (lambda value: isinstance(value, (int, float)) and not isinstance(value, bool), "a number"),
}


def _validate_scalar(value: Any, schema: dict[str, Any], path: str) -> None:
    accepts, expected = _SCALARS.get(schema.get("type", ""), (lambda _: True, ""))
    if not accepts(value):
        suffix = f" ({schema['format']})" if "format" in schema else ""
        raise _type_error(path, expected + suffix)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}")
    low, high = _INTEGER_RANGES.get(schema.get("format", ""), (value, value))
    if not low <= value <= high:
        raise ValueError(f"{path} is outside the {schema['format']} range")


def _field_schema(key: str, schema: dict[str, Any], path: str) -> dict[str, Any] | None:
    """The schema of one object member; None for a free-form object."""
    properties: dict[str, Any] = schema.get("properties", {})
    if key in properties:
        return properties[key]
    if schema.get("additionalProperties") is not None:
        return schema["additionalProperties"]
    if "properties" in schema:
        raise ValueError(f"{path}.{key} is not a field of {schema.get('id', 'this object')}; use the vendor field names in the discovery schema")
    return None


def _validate_object(value: Any, schema: dict[str, Any], schemas: dict[str, Any], path: str) -> None:
    if not isinstance(value, dict):
        raise _type_error(path, f"a JSON object ({schema.get('id', 'object')})")
    for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
        member = _field_schema(str(key), schema, path)  # pyright: ignore[reportUnknownArgumentType]
        if member is not None:
            validate_value(item, member, schemas, f"{path}.{key}")
    kinds = [key for key, item in value.items() if item is not None]  # pyright: ignore[reportUnknownVariableType]
    if schema.get("id") in UNION_SCHEMAS and len(kinds) != 1:  # pyright: ignore[reportUnknownArgumentType]
        raise ValueError(f"{path} must set exactly one request kind, got {kinds or 'none'}")


def validate_value(value: Any, schema: dict[str, Any], schemas: dict[str, Any], path: str) -> None:
    """Check ``value`` against a discovery schema node; JSON null clears a field."""
    if value is None:
        return
    if "$ref" in schema:
        validate_value(value, schemas[schema["$ref"]], schemas, path)
        return
    kind = schema.get("type")
    if kind == "object":
        _validate_object(value, schema, schemas, path)
    elif kind == "array":
        if not isinstance(value, list):
            raise _type_error(path, "an array")
        for index, item in enumerate(value):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            validate_value(item, schema.get("items", {"type": "any"}), schemas, f"{path}[{index}]")
    elif kind != "any":
        _validate_scalar(value, schema, path)


def validate_body(verb: str, body: Any) -> None:
    product, method = WORKSPACE_METHODS[verb]
    if "request" not in method:
        if body is not None:
            raise ValueError(f"{verb} takes no request body")
        return
    if body is None:
        return
    schemas: dict[str, Any] = discovery_document(product)["schemas"]
    validate_value(body, {"$ref": method["request"]["$ref"]}, schemas, "body")


def check_editor_call(verb: str, params: dict[str, Any]) -> None:
    """Refuse an invalid editor call before a job is queued (ValueError names the rule)."""
    refuse_credential_parameters(verb, params)
    request_arguments(verb, params)
    validate_body(verb, params.get("body"))
