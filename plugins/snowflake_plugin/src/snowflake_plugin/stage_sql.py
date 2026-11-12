"""Connector stage commands, following Snowflake's SQL command references.

PUT/GET have deliberately closed option shapes because their local paths must
pass containment. COPY accepts vendor option names without a versioned name
allowlist: values are SQL literals, nested option objects, or explicit
``{"sql": expression}`` values for identifiers/expressions. Snowflake validates
COPY's option combinations and the registered role authorizes all commands.
"""

from __future__ import annotations

import math
import re
from typing import Any

OPERATIONS = ("put", "get", "list", "remove", "copy_into_table", "copy_into_location", "copy_files")
_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_IDENTIFIER = re.compile(r'(?:[A-Za-z_][A-Za-z_0-9$]*|"(?:[^"]|"")+")(?:\.(?:[A-Za-z_][A-Za-z_0-9$]*|"(?:[^"]|"")+"))*')
_COMPRESSION = frozenset({"AUTO_DETECT", "GZIP", "BZ2", "BROTLI", "ZSTD", "DEFLATE", "RAW_DEFLATE", "NONE"})


def text(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise ValueError(f"{key} must be a nonempty string without NUL")
    return value


def literal(value: str) -> str:
    """Quote SQL strings including backslashes, which Snowflake interprets."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def identifier(value: str) -> str:
    if not _IDENTIFIER.fullmatch(value):
        raise ValueError("table/column must be a SQL identifier (double-quote special names)")
    return value


def location(value: str) -> str:
    if not value.startswith(("@", "s3://", "s3china://", "s3gov://", "gcs://", "azure://")) or "\x00" in value:
        raise ValueError("location must start with @, s3://, s3china://, s3gov://, gcs:// or azure://")
    return literal(value)


def _option_value(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).upper()
    if isinstance(value, (int, float)) and math.isfinite(value):
        return str(value)
    if isinstance(value, str):
        return literal(value)
    return _compound_value(value)


def _compound_value(value: Any) -> str:
    if isinstance(value, list) and value:
        return "(" + ", ".join(_option_value(item) for item in value) + ")"
    if isinstance(value, dict) and value:
        if set(value) == {"sql"}:
            return text(value, "sql")
        return "(" + options(value) + ")"
    raise ValueError("options need strings, finite numbers, booleans, nonempty lists/objects or {sql: expression}")


def options(values: dict[str, Any]) -> str:
    pieces: list[str] = []
    for key, value in values.items():
        if not _NAME.fullmatch(key):
            raise ValueError("option names must be SQL keyword names")
        pieces.append(f"{key.upper()} = {_option_value(value)}")
    return " ".join(pieces)


def _parallel(value: Any) -> bool:
    return type(value) is int and 1 <= value <= 99


def _compression(value: Any) -> bool:
    return isinstance(value, str) and value in _COMPRESSION


def _boolean(value: Any) -> bool:
    return isinstance(value, bool)


def _string(value: Any) -> bool:
    return isinstance(value, str)


def _transfer_options(operation: str, values: dict[str, Any]) -> str:
    validators = (
        {"PARALLEL": _parallel, "PATTERN": _string}
        if operation == "get"
        else {
            "PARALLEL": _parallel,
            "AUTO_COMPRESS": _boolean,
            "OVERWRITE": _boolean,
            "SOURCE_COMPRESSION": _compression,
        }
    )
    for key, value in values.items():
        if key not in validators or not validators[key](value):
            raise ValueError(f"invalid {operation} option {key}")
    return options(values)


def _inputs(params: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    operation = text(params, "operation")
    if operation not in OPERATIONS:
        raise ValueError(f"operation must be one of {OPERATIONS}")
    raw = params.get("options", {})
    if not isinstance(raw, dict):
        raise ValueError("options must be an object")
    values = {key.upper(): value for key, value in raw.items() if isinstance(key, str)}
    if len(values) != len(raw):
        raise ValueError("options require distinct SQL keyword names")
    return operation, location(text(params, "stage_location")), values


def build_command(params: dict[str, Any], *, local_path: str | None = None) -> str:
    """Build one command; local_path is admitted by stage_paths, never a URI input."""
    operation, stage, values = _inputs(params)
    if operation in {"put", "get"}:
        if not text(params, "stage_location").startswith("@"):
            raise ValueError("PUT/GET require an internal @stage")
        local = literal("file://" + (local_path if local_path is not None else text(params, "local_path")))
        operands = f"{local} {stage}" if operation == "put" else f"{stage} {local}"
        return f"{operation.upper()} {operands} {_transfer_options(operation, values)}".rstrip()
    if operation in {"list", "remove"}:
        if set(values) - {"PATTERN"} or ("PATTERN" in values and not isinstance(values["PATTERN"], str)):
            raise ValueError("LIST/REMOVE accept only the string PATTERN option")
        return f"{operation.upper()} {stage} {options(values)}".rstrip()
    builders = {"copy_into_table": _copy_into_table, "copy_into_location": _copy_into_location, "copy_files": _copy_files}
    return builders[operation](params, stage, values)


def _query(params: dict[str, Any]) -> str | None:
    return "(" + text(params, "query") + ")" if params.get("query") is not None else None


def _auth(values: dict[str, Any]) -> str:
    return options({key: values.pop(key) for key in ("STORAGE_INTEGRATION", "CREDENTIALS", "ENCRYPTION") if key in values})


def _target(params: dict[str, Any]) -> str:
    target = identifier(text(params, "table"))
    columns = params.get("columns")
    if columns is not None:
        if not isinstance(columns, list) or not columns or not all(isinstance(item, str) for item in columns):
            raise ValueError("columns must be a nonempty list of identifiers")
        target += " (" + ", ".join(identifier(item) for item in columns) + ")"
    return target


def _copy_into_table(params: dict[str, Any], stage: str, values: dict[str, Any]) -> str:
    target = _target(params)
    auth = _auth(values)
    query = _query(params)
    if query and auth:
        raise ValueError("put external authentication inside the COPY transformation query")
    source = query or (stage + " " + auth).rstrip()
    return f"COPY INTO {target} FROM {source} {options(values)}".rstrip()


def _copy_files(params: dict[str, Any], stage: str, values: dict[str, Any]) -> str:
    source = _query(params) or location(text(params, "source_location"))
    return f"COPY FILES INTO {stage} FROM {source} {options(values)}".rstrip()


def _copy_into_location(params: dict[str, Any], stage: str, values: dict[str, Any]) -> str:
    auth = _auth(values)
    source = _query(params) or identifier(text(params, "table"))
    partition = " PARTITION BY " + text(params, "partition_by") if "partition_by" in params else ""
    return f"COPY INTO {stage} {auth} FROM {source}{partition} {options(values)}".rstrip()
