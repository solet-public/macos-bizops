"""Read-verb implementations — pure functions over an already-open connection.

Same shape as external_postgres_plugin's query_actions.py: take an
already-built connection and a ``params`` dict, return plain result dicts.
Export-path containment (``path_gate``, export_query) is injected.
Interactive reads are inline-only: an over-cap result raises
``ResultTooLargeError`` (A4 — no blob spill). Invalid params raise
``ValueError`` (-> snowflake.invalid_params); guard violations raise
``StatementGuardError``; driver errors propagate to the plugin's
``classify_snowflake_error``.

This module is S2-exempt (whole-file): its ``SHOW``/information-schema-style
introspection and the caller's own ``run_query``/``export_query`` text target
the operator-registered Snowflake account only (containment invariant #1 — no
account parameter on any verb). Introspection queries bind identifiers via
Snowflake's ``fully_qualified`` list results, never string-interpolating
caller-controlled filter values into a WHERE clause.
"""

from __future__ import annotations

import csv
import io
import json
import os
from collections.abc import Callable
from typing import Any

from .constants import (
    ERROR_RESULT_TOO_LARGE,
    EXPORT_ROW_CAP,
    INLINE_BYTE_CAP,
    INLINE_ROW_CAP_DEFAULT,
    MAX_ROWS_HARD_CAP,
)
from .statement_guard import assert_read_statement

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]


class ResultTooLargeError(RuntimeError):
    """An interactive read overflowed the inline caps (A4: fail loud, no spill)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_RESULT_TOO_LARGE


def run_query(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Run one read-only statement; return rows inline (fails loud over the caps)."""
    sql = _require_str(params, "sql")
    max_rows = _clamp_max_rows(params.get("max_rows"))
    assert_read_statement(sql)
    columns, rows = _execute_fetch(conn, sql, max_rows)
    return _shape_rows(columns, rows, max_rows)


def list_databases(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List databases visible to the current role."""
    with conn.cursor() as cur:
        cur.execute("SHOW DATABASES")
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    return {"databases": [{"name": _cell(r, name_idx)} for r in rows]}


def list_schemas(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List schemas in a database (identifier passed via Snowflake's own IN clause)."""
    database = _require_str(params, "database")
    with conn.cursor() as cur:
        cur.execute(f'SHOW SCHEMAS IN DATABASE "{_quote_ident(database)}"')
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    return {"schemas": [_cell(r, name_idx) for r in rows]}


def list_tables(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List tables in a database.schema."""
    database = _require_str(params, "database")
    schema = _require_str(params, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f'SHOW TABLES IN SCHEMA "{_quote_ident(database)}"."{_quote_ident(schema)}"'
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    kind_idx = _column_index(columns, "kind")
    return {
        "tables": [
            {"name": _cell(r, name_idx), "kind": _cell(r, kind_idx)} for r in rows
        ]
    }


def describe_table(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Describe a table's columns."""
    database = _require_str(params, "database")
    schema = _require_str(params, "schema")
    table = _require_str(params, "table")
    with conn.cursor() as cur:
        cur.execute(
            f'DESCRIBE TABLE "{_quote_ident(database)}"."{_quote_ident(schema)}".'
            f'"{_quote_ident(table)}"'
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    type_idx = _column_index(columns, "type")
    null_idx = _column_index(columns, "null?")
    default_idx = _column_index(columns, "default")
    return {
        "columns": [
            {
                "name": _cell(r, name_idx),
                "type": _cell(r, type_idx),
                "nullable": _cell(r, null_idx) == "Y",
                "default": _cell(r, default_idx) or None,
            }
            for r in rows
        ]
    }


def test_connection(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Confirm the connection: account, user, role, warehouse, server version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE(), CURRENT_VERSION()"
        )
        row = cur.fetchone()
    return {
        "ok": True,
        "account": _as_str(row[0]) if row else "",
        "user": _as_str(row[1]) if row else "",
        "role": _as_str(row[2]) if row else "",
        "warehouse": _as_str(row[3]) if row else "",
        "version": _as_str(row[4]) if row else "",
    }


def export_query(conn: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Export a read-only query's full result (up to EXPORT_ROW_CAP) as a workspace TSV.

    The full result set lands as ONE tab-separated file at the caller's
    absolute ``output_tsv_path`` — admitted by the injected containment gate,
    never platform blob storage (A2, operator-ruled 2026-07-15). The gate runs
    BEFORE the statement executes, so a refused path never sends the query.
    """
    sql = _require_str(params, "sql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
            "create it first — this verb writes one file, it does not create directories"
        )
    assert_read_statement(sql)
    columns, rows = _execute_fetch(conn, sql, EXPORT_ROW_CAP)
    row_lists = [_row_to_list(r) for r in rows]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    return {
        "path": resolved_path,
        "row_count": len(row_lists),
        "columns": columns,
        "truncated": len(row_lists) >= EXPORT_ROW_CAP,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _execute_fetch(conn: Any, sql: str, cap: int) -> tuple[list[str], list[tuple[Any, ...]]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(cap) if columns else []
    return columns, rows


def _shape_rows(
    columns: list[str], rows: list[tuple[Any, ...]], max_rows: int
) -> dict[str, Any]:
    # rows are list-of-lists PARALLEL to columns (NOT dicts) so duplicate column
    # names never collapse and lose a value.
    row_lists = [_row_to_list(r) for r in rows]
    payload = _to_json(columns, row_lists)
    # The row bound is the CALLER'S effective max_rows (already clamped to the
    # hard cap) — comparing against the 200 default here made every legal
    # max_rows=201..1000 request fail loud after a successful fetch (live,
    # 2026-07-16). The row arm survives only as a belt against a driver that
    # over-returns fetchmany(cap); the byte cap is the absolute guardrail.
    if len(row_lists) > max_rows or len(payload) > INLINE_BYTE_CAP:
        # A4 (2026-07-16): FAIL LOUD, never spill to platform blob storage.
        raise ResultTooLargeError(
            f"the result is too large to return inline ({len(row_lists)} rows / "
            f"{len(payload)} bytes vs caps {max_rows} rows / "
            f"{INLINE_BYTE_CAP} bytes); narrow the query (LIMIT / max_rows) for an "
            "interactive answer, or use export_query with an absolute "
            "output_tsv_path to land the full result set as a workspace TSV file"
        )
    return {
        "columns": columns,
        "rows": row_lists,
        "row_count": len(row_lists),
        "spilled": False,
    }


def _row_to_list(row: tuple[Any, ...]) -> list[Any]:
    return [_jsonable(val) for val in row]


def _to_json(columns: list[str], row_lists: list[list[Any]]) -> bytes:
    return json.dumps(
        {"columns": columns, "rows": row_lists}, default=str, ensure_ascii=False
    ).encode("utf-8")


def _to_tsv(columns: list[str], row_lists: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(row_lists)
    return buffer.getvalue().encode("utf-8")


def _jsonable(value: Any) -> Any:
    """Coerce a warehouse value to a JSON-safe form; non-primitives become their str repr."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _clamp_max_rows(raw: Any) -> int:
    if not isinstance(raw, int) or isinstance(raw, bool):
        return INLINE_ROW_CAP_DEFAULT
    return max(1, min(MAX_ROWS_HARD_CAP, raw))


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _column_index(columns: list[str], name: str) -> int:
    lowered = [c.lower() for c in columns]
    try:
        return lowered.index(name.lower())
    except ValueError:
        return -1


def _cell(row: tuple[Any, ...], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    value = row[idx]
    return _as_str(value)


def _quote_ident(identifier: str) -> str:
    """Escape a double-quote inside a caller-supplied Snowflake identifier.

    Identifiers here come from typed params (database/schema/table), never
    from the free-text ``sql``/``run_query`` surface — this only prevents an
    identifier containing ``"`` from breaking out of its quoting.
    """
    return identifier.replace('"', '""')
