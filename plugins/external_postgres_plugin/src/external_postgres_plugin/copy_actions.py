"""``copy`` — PostgreSQL COPY between a table (or query) and a workspace file.

The vendor shape is ``COPY table [(column, …)] FROM STDIN [WITH (option, …)]
[WHERE condition]`` and ``COPY {table [(column, …)] | (query)} TO STDOUT [WITH
(option, …)]`` (PostgreSQL 17 COPY reference). The data streams over the
client connection (psycopg3 ``cursor.copy``), so the file lives in the
operator's workspace, never on the database host. The path must be absolute
and contained under an ``registered path addresses`` entry (the injected
``file_gate``). Server-side files and ``PROGRAM`` are a different vendor
privilege (``pg_read_server_files`` …) and go through ``run_statement``.

Every documented COPY option is a parameter, by its vendor name in lower case.
Values are composed with psycopg's ``sql.Identifier``/``sql.Literal`` (through
``connection``, the plugin's one driver module), never string interpolation.
Option-direction rules (``FORCE_QUOTE`` is COPY TO only, ``WHERE`` and
``FREEZE`` are COPY FROM only, …) are the server's to enforce: a misuse comes
back as the server's own error.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from .connection import pg_sql

# file_gate(path, param_name) -> realpath-resolved path, or raises
# PathRefusedError. Bound by the plugin to registered path addresses.
FileGate = Callable[[str, str], str]

OPERATION_FROM_FILE = "from_file"
OPERATION_TO_FILE = "to_file"
OPERATIONS = (OPERATION_FROM_FILE, OPERATION_TO_FILE)

_KEYWORD_CHOICES: dict[str, tuple[str, ...]] = {
    "format": ("text", "csv", "binary"),
    "on_error": ("stop", "ignore"),
    "log_verbosity": ("default", "verbose", "silent"),
}
_STRING_OPTIONS = ("delimiter", "null", "default", "quote", "escape", "encoding")
_BOOLEAN_OPTIONS = ("freeze",)
_COLUMN_OPTIONS = ("force_quote", "force_not_null", "force_null")
# HEADER takes a boolean or MATCH; a boolean arrives as True/False or "true"/"false".
_HEADER_CHOICES = ("true", "false", "match")
# Vendor option order, for a stable rendered statement.
COPY_OPTIONS = (
    "format",
    "freeze",
    "delimiter",
    "null",
    "default",
    "header",
    "quote",
    "escape",
    "force_quote",
    "force_not_null",
    "force_null",
    "on_error",
    "encoding",
    "log_verbosity",
)
_CHUNK_BYTES = 1 << 16


def run_copy(conn: Any, params: dict[str, Any], file_gate: FileGate) -> dict[str, Any]:
    """Dispatch ``operation`` to COPY FROM a file or COPY TO a file."""
    operation = params.get("operation")
    if operation == OPERATION_FROM_FILE:
        return _copy_from_file(conn, params, file_gate)
    if operation == OPERATION_TO_FILE:
        return _copy_to_file(conn, params, file_gate)
    raise ValueError(f"'operation' must be one of {list(OPERATIONS)}")


def build_copy_statement(params: dict[str, Any], direction: str) -> Any:
    """The composed COPY statement for ``direction`` ``FROM STDIN`` or ``TO STDOUT``."""
    options = _options_clause(params)
    statement = pg_sql.SQL("COPY {} {}").format(_copy_source(params, direction), pg_sql.SQL(direction))
    if options is not None:
        statement = pg_sql.SQL("{} WITH ({})").format(statement, options)
    where = params.get("where")
    if where is None:
        return statement
    if direction != "FROM STDIN":
        raise ValueError("'where' applies to operation from_file only (COPY FROM … WHERE)")
    if not isinstance(where, str) or not where.strip():
        raise ValueError("'where' must be a non-empty SQL condition")
    return pg_sql.SQL("{} WHERE {}").format(statement, pg_sql.SQL(where))


def _copy_source(params: dict[str, Any], direction: str) -> Any:
    """``schema.table [(columns)]``, or ``(query)`` for COPY TO a file."""
    query = params.get("query")
    if direction != "TO STDOUT" or query is None:
        return _table_source(params)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("'query' must be a non-empty string")
    if params.get("table") is not None or params.get("columns") is not None:
        raise ValueError("pass either 'query' or 'table' (with optional 'columns'), not both")
    return pg_sql.SQL("({})").format(pg_sql.SQL(query.strip().rstrip(";")))


def _copy_from_file(conn: Any, params: dict[str, Any], file_gate: FileGate) -> dict[str, Any]:
    path = file_gate(_require_str(params, "path"), "path")
    if not os.path.isfile(path):
        raise ValueError(f"'path' is not an existing file ({path})")
    statement = build_copy_statement(params, "FROM STDIN")
    notices: list[str] = []
    conn.add_notice_handler(lambda diag: notices.append(str(diag.message_primary)))
    sent = 0
    with conn.cursor() as cur:
        with cur.copy(statement) as copy, open(path, "rb") as handle:
            while chunk := handle.read(_CHUNK_BYTES):
                copy.write(chunk)
                sent += len(chunk)
        rowcount = cur.rowcount
    conn.commit()
    return {
        "operation": OPERATION_FROM_FILE,
        "path": path,
        "rowcount": rowcount,
        "bytes": sent,
        "notices": notices,
    }


def _copy_to_file(conn: Any, params: dict[str, Any], file_gate: FileGate) -> dict[str, Any]:
    path = file_gate(_require_str(params, "path"), "path")
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise ValueError(f"the parent directory of 'path' does not exist ({parent}); create it first")
    statement = build_copy_statement(params, "TO STDOUT")
    written = 0
    with conn.cursor() as cur:
        with cur.copy(statement) as copy, open(path, "wb") as handle:
            for chunk in copy:
                handle.write(chunk)
                written += len(chunk)
        rowcount = cur.rowcount
    conn.commit()
    return {"operation": OPERATION_TO_FILE, "path": path, "rowcount": rowcount, "bytes": written}


def _table_source(params: dict[str, Any]) -> Any:
    schema = _require_str(params, "schema")
    table = _require_str(params, "table")
    source = pg_sql.Identifier(schema, table)
    columns = params.get("columns")
    if columns is None:
        return source
    names = _column_list(columns, "columns")
    return pg_sql.SQL("{} ({})").format(source, pg_sql.SQL(", ").join(names))


def _options_clause(params: dict[str, Any]) -> Any | None:
    rendered = [part for name in COPY_OPTIONS if params.get(name) is not None for part in [_render_option(name, params[name])]]
    return pg_sql.SQL(", ").join(rendered) if rendered else None


def _render_option(name: str, value: Any) -> Any:
    """``KEYWORD value`` for one COPY option, validated by the option's kind."""
    if name in _KEYWORD_CHOICES:
        rendered = _keyword_value(name, value)
    elif name in _BOOLEAN_OPTIONS:
        rendered = _boolean_value(name, value)
    elif name == "header":
        rendered = _header_value(value)
    elif name in _STRING_OPTIONS:
        rendered = _string_value(name, value)
    else:
        rendered = _columns_value(name, value)
    return pg_sql.SQL("{} {}").format(pg_sql.SQL(name.upper()), rendered)


def _keyword_value(name: str, value: Any) -> Any:
    if value not in _KEYWORD_CHOICES[name]:
        raise ValueError(f"'{name}' must be one of {list(_KEYWORD_CHOICES[name])}")
    return pg_sql.SQL(str(value).upper())


def _boolean_value(name: str, value: Any) -> Any:
    if not isinstance(value, bool):
        raise ValueError(f"'{name}' must be a boolean")
    return pg_sql.SQL("TRUE" if value else "FALSE")


def _header_value(value: Any) -> Any:
    header = str(value).lower() if isinstance(value, (bool, str)) else ""
    if header not in _HEADER_CHOICES:
        raise ValueError("'header' must be true, false or \"match\"")
    return pg_sql.SQL(header.upper())


def _string_value(name: str, value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError(f"'{name}' must be a string")
    return pg_sql.Literal(value)


def _columns_value(name: str, value: Any) -> Any:
    if value in ("*", ["*"]):  # every column: FORCE_QUOTE * (vendor shape)
        return pg_sql.SQL("*")
    return pg_sql.SQL("({})").format(pg_sql.SQL(", ").join(_column_list(value, name)))


def _column_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"'{name}' must be a non-empty list of column names")
    return [pg_sql.Identifier(item) for item in value]


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value
