"""Read-verb implementations — pure functions over a read-only psycopg connection.

Same shape as g_suite's action modules: take an already-built (read-only)
connection and a ``params`` dict, return plain result dicts. Export-path
containment (``path_gate``, export_query) is injected. Interactive reads are
inline-only: an over-cap result raises ``ResultTooLargeError`` (A4 — no blob
spill). Invalid params raise ``ValueError`` (-> external_pg.invalid_params);
guard violations raise ``StatementGuardError``; psycopg errors propagate to
the plugin's ``classify_pg_error``.

This module is S2-exempt (whole-file): its ``information_schema`` SELECTs and the
caller's own ``run_query``/``export_query`` text target the FOREIGN database only
(containment invariant #1 — every verb takes a connection NAME, never a DSN; the
connection is built solely from ``external_pg::*`` entries). Introspection queries
bind their filters with ``%s`` — never string interpolation of caller values.
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
from .statement_guard import assert_read_statement, assert_single_statement

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]


class ResultTooLargeError(RuntimeError):
    """An interactive read overflowed the inline caps (A4: fail loud, no spill)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_RESULT_TOO_LARGE

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def run_query(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Run one read-only statement; return rows inline (fails loud over the caps)."""
    sql = _require_str(params, "sql")
    max_rows = _clamp_max_rows(params.get("max_rows"))
    assert_single_statement(sql)
    assert_read_statement(sql)
    columns, rows = _execute_fetch(conn, sql, max_rows)
    return _shape_rows(columns, rows, max_rows)


def list_schemas(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List non-system schemas."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name <> ALL(%s) ORDER BY schema_name",
            (list(_SYSTEM_SCHEMAS),),
        )
        rows = cur.fetchall()
    return {"schemas": [_as_str(r[0]) for r in rows]}


def list_tables(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List tables/views in a schema (bound-parameter filter)."""
    schema = _require_str(params, "schema")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = %s ORDER BY table_name",
            (schema,),
        )
        rows = cur.fetchall()
    return {"tables": [{"name": _as_str(r[0]), "kind": _as_str(r[1])} for r in rows]}


def describe_table(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Describe a table's columns (bound-parameter filters)."""
    schema = _require_str(params, "schema")
    table = _require_str(params, "table")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable, column_default "
            "FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position",
            (schema, table),
        )
        rows = cur.fetchall()
    return {
        "columns": [
            {
                "name": _as_str(r[0]),
                "type": _as_str(r[1]),
                "nullable": _as_str(r[2]) == "YES",
                "default": _as_optional_str(r[3]),
            }
            for r in rows
        ]
    }


def test_connection(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Confirm a connection: resolved host, server version, current role, read-only flag.

    ``host`` is the RESOLVED host libpq actually connected to (``conn.info.host``,
    the same string passed to ``connect(host=dsn.host or None)`` for a TCP target),
    so the operator can confirm the connection points where they expect and catch a
    mis-registration (§8.2, Rev-A R-D2). This is a SUCCESS-path echo of the
    operator's own registered connection metadata on a deny-listed internal-only
    verb — the §2.4 topology-safety rule governs ERROR paths only.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version(), current_user, current_setting('transaction_read_only')"
        )
        row = cur.fetchone()
    version = _as_str(row[0]) if row else ""
    user = _as_str(row[1]) if row else ""
    read_only = bool(row) and _as_str(row[2]) == "on"
    return {
        "ok": True,
        "host": _as_str(conn.info.host),
        "server_version": version,
        "current_user": user,
        "read_only": read_only,
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
    assert_single_statement(sql)
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
        columns = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchmany(cap) if columns else []
    return columns, rows


def _shape_rows(
    columns: list[str], rows: list[tuple[Any, ...]], max_rows: int
) -> dict[str, Any]:
    # rows are list-of-lists PARALLEL to columns (NOT dicts) so duplicate column
    # names — routine in JOINs, e.g. SELECT a.id, b.id — never collapse and lose
    # a value. Column order + duplicates are preserved faithfully.
    row_lists = [_row_to_list(r) for r in rows]
    payload = _to_json(columns, row_lists)
    # The row bound is the CALLER'S effective max_rows (already clamped to the
    # hard cap) — comparing against the 200 default here made every legal
    # max_rows=201..1000 request fail loud after a successful fetch (found live
    # on the snowflake sibling, 2026-07-16; same copy-pasted arm here). The row
    # arm survives only as a belt against a driver that over-returns
    # fetchmany(cap); the byte cap is the absolute guardrail.
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
    """Coerce a DB value to a JSON-safe form; non-primitives become their str repr."""
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
    return value if isinstance(value, str) else ""


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
