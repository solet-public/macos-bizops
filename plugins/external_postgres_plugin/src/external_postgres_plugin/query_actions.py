"""Verb implementations — pure functions over an already-built psycopg connection.

Same shape as g_suite's action modules: take an already-built connection and a
``params`` dict, return plain result dicts. Export-path containment
(``path_gate``) is injected. ``run_query``, ``export_query``, and (when its
statement produces a result set) ``run_statement`` ALWAYS write their result to
the caller-supplied ``output_tsv_path`` and return a handle only — never rows
inline, at any size (business-data limits + data-export migration, 2026-08-02;
the former inline-return/``INLINE_BYTE_CAP`` branch is deleted, not lowered).
The effective row ceiling is DEFAULT_ROW_LIMIT unless the caller supplies BOTH
``acknowledge_default_limit_override=true`` and an explicit ``row_limit`` (up
to the verb's hard cap) — see ``_resolve_effective_limit``. Invalid params
raise ``ValueError`` (-> external_pg.invalid_params); guard violations raise
``StatementGuardError``; psycopg errors propagate to the plugin's
``classify_pg_error``.

No verb here classifies a statement as a read or a write (USER ruling
rul_4c99a30d; operator ruling 2026-08-09 + Amendment 1, "vendor RBAC is the
control plane"). ``assert_single_statement`` is a SHAPE check (exactly one
statement); what the statement may do is decided entirely by the server-side
Postgres GRANTs on the registered credential. ``run_query`` and
``export_query`` commit after the TSV is written, so a data-modifying CTE does
what it says; ``run_statement`` commits per call, or runs in autocommit mode
for commands that refuse a transaction block.

This module is S2-exempt (whole-file): its ``information_schema`` SELECTs and the
caller's own ``run_query``/``export_query``/``run_statement`` text target the
FOREIGN database only (containment invariant #1 — every verb takes a connection
NAME, never a DSN; the connection is built solely from ``external_pg::*``
entries). Introspection queries bind their filters with ``%s`` — never string
interpolation of caller values.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Callable
from typing import Any

from .constants import (
    DEFAULT_ROW_LIMIT,
    EXPORT_ROW_CAP,
    MAX_ROWS_HARD_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
)
from .statement_guard import assert_single_statement

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# PathRefusedError. Injected by the plugin, which binds the operator's
# registered path addresses config (ananta.file_locations.containment.assert_path_allowed).
PathGate = Callable[[str], str]

_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast")


def run_query(conn: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Run one statement; write its rows to output_tsv_path, commit, return a handle.

    Defaults to DEFAULT_ROW_LIMIT rows; an acknowledged override may request
    up to MAX_ROWS_HARD_CAP. For pulls beyond that ceiling, use export_query
    (up to EXPORT_ROW_CAP with the same override).
    """
    effective_limit = _resolve_effective_limit(
        params,
        default=DEFAULT_ROW_LIMIT,
        hard_cap=MAX_ROWS_HARD_CAP,
        verb="run_query",
    )
    sql = _require_str(params, "sql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    assert_single_statement(sql)
    return _execute_and_write_tsv(conn, sql, effective_limit, path_gate, output_tsv_path)


def run_statement(conn: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Run ONE statement; the server's GRANTs decide what it can do.

    Single-statement contract (:func:`assert_single_statement` — SHAPE only; no
    statement-leader classification exists on this path, by design — see the
    module docstring). ``autocommit=true`` runs the statement outside a
    transaction block, for the commands the server refuses inside one (VACUUM,
    CREATE DATABASE, CREATE INDEX CONCURRENTLY, ALTER SYSTEM); the statement is
    then committed by the server as it runs, so the rollback below cannot undo
    it and the caller must pass output_tsv_path if it returns rows. Commit
    semantics are explicit and per-call: a statement with no result set (the
    common INSERT/UPDATE/DELETE/DDL case) commits and returns ``rowcount``
    inline. A statement that DOES produce a result set (a RETURNING clause) is
    routed through the SAME always-TSV export path as run_query — rows are never
    returned inline, at any size — so ``output_tsv_path`` is required in that
    case; its absence rolls the transaction back rather than silently discarding
    the returned rows while still committing the write. Which branch fires is
    decided by the driver's own ``cur.description`` AFTER executing, never by
    inspecting the SQL text beforehand — the same principle as the read-leader
    guard's absence here: this verb classifies nothing about the statement,
    ever.
    """
    sql = _require_str(params, "sql")
    assert_single_statement(sql)
    resolved_path = _optional_output_path(params, path_gate)
    effective_limit = _resolve_effective_limit(
        params,
        default=DEFAULT_ROW_LIMIT,
        hard_cap=MAX_ROWS_HARD_CAP,
        verb="run_statement",
    )
    autocommit = _apply_autocommit(conn, params)
    with conn.cursor() as cur:
        cur.execute(sql)
        if cur.description is None:
            rowcount = cur.rowcount
            conn.commit()
            return {"rowcount": rowcount, "has_result_set": False, "autocommit": autocommit}
        if resolved_path is None:
            conn.rollback()
            raise _missing_output_path(autocommit)
        columns = [d.name for d in cur.description]
        rows = cur.fetchmany(effective_limit)
        rowcount = cur.rowcount
    row_lists = [_row_to_list(r) for r in rows]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    conn.commit()
    return {
        "rowcount": rowcount,
        "has_result_set": True,
        "autocommit": autocommit,
        "path": resolved_path,
        "row_count": len(row_lists),
        "columns": columns,
        "truncated": len(row_lists) >= effective_limit,
    }


def _optional_output_path(params: dict[str, Any], path_gate: PathGate) -> str | None:
    """run_statement's optional TSV path: gated and its directory checked BEFORE executing."""
    output_tsv_path = params.get("output_tsv_path")
    if not isinstance(output_tsv_path, str) or not output_tsv_path.strip():
        return None
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(f"the parent directory of output_tsv_path does not exist ({parent_dir}); create it first — this verb writes one file, it does not create directories")
    return resolved_path


def _apply_autocommit(conn: Any, params: dict[str, Any]) -> bool:
    """Put the connection in autocommit mode when asked (before any statement runs)."""
    autocommit = params.get("autocommit", False)
    if not isinstance(autocommit, bool):
        raise ValueError("'autocommit' must be a boolean")
    if autocommit:
        conn.autocommit = True
    return autocommit


def _missing_output_path(autocommit: bool) -> ValueError:
    committed_note = " The statement already ran in autocommit mode, so the server has committed it." if autocommit else " The statement was rolled back."
    return ValueError("the statement produced a result set (e.g. a RETURNING clause) but no output_tsv_path was supplied; rows are never returned inline — pass output_tsv_path to receive them as a TSV, the same always-export contract as run_query." + committed_note)


def list_schemas(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List non-system schemas."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name <> ALL(%s) ORDER BY schema_name",
            (list(_SYSTEM_SCHEMAS),),
        )
        rows = cur.fetchall()
    return {"schemas": [_as_str(r[0]) for r in rows]}


def list_tables(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List tables/views in a schema (bound-parameter filter)."""
    schema = _require_str(params, "schema")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = %s ORDER BY table_name",
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
            "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
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
        cur.execute("SELECT version(), current_user, current_setting('transaction_read_only')")
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
    """Export one statement's rows as a workspace TSV, then commit — the N>>500 route.

    Same caller-supplied-path destination and override mechanism as
    run_query; only the override's hard cap differs (EXPORT_ROW_CAP, not
    MAX_ROWS_HARD_CAP). Absent the override this also defaults to
    DEFAULT_ROW_LIMIT — for the common small/default case, run_query has an
    identical interface with a lower ceiling. The gate runs BEFORE the
    statement executes, so a refused path never sends the query.
    """
    effective_limit = _resolve_effective_limit(
        params,
        default=DEFAULT_ROW_LIMIT,
        hard_cap=EXPORT_ROW_CAP,
        verb="export_query",
    )
    sql = _require_str(params, "sql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    assert_single_statement(sql)
    return _execute_and_write_tsv(conn, sql, effective_limit, path_gate, output_tsv_path)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_effective_limit(
    params: dict[str, Any],
    *,
    default: int,
    hard_cap: int,
    verb: str,
) -> int:
    """Resolve the effective fetch ceiling from the §5 override pair.

    Absent (or ``acknowledge_default_limit_override`` not exactly ``True``)
    with no ``row_limit``: returns ``default``. Both must be given together —
    the override flag alone, or ``row_limit`` alone, fails loud rather than
    silently honoring half. A ``row_limit`` above ``hard_cap`` is refused,
    never silently clamped back down.
    """
    override = params.get(PARAM_ACKNOWLEDGE_OVERRIDE)
    row_limit = params.get(PARAM_ROW_LIMIT)
    override_present = override is True
    row_limit_present = row_limit is not None
    if override_present != row_limit_present:
        raise ValueError(f"{verb}: '{PARAM_ACKNOWLEDGE_OVERRIDE}' and '{PARAM_ROW_LIMIT}' must be given together — got {PARAM_ACKNOWLEDGE_OVERRIDE}={override!r}, {PARAM_ROW_LIMIT}={row_limit!r}")
    if not override_present:
        return default
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}' must be a positive integer")
    if row_limit > hard_cap:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}'={row_limit} exceeds the hard cap of {hard_cap}; refusing rather than silently clamping")
    return row_limit


def _execute_and_write_tsv(
    conn: Any,
    sql: str,
    cap: int,
    path_gate: PathGate,
    output_tsv_path: str,
) -> dict[str, Any]:
    """Admit the path, fetch up to ``cap`` rows, write a TSV, commit, return a handle.

    The containment gate runs BEFORE the statement executes, so a refused
    path never sends the query. ``fetchmany(cap)`` bounds the fetch at the
    cursor/network level — never fetch-everything-then-truncate. The commit
    comes AFTER the file is written: a statement that writes (a
    data-modifying CTE) is never committed while its rows are lost.
    """
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(f"the parent directory of output_tsv_path does not exist ({parent_dir}); create it first — this verb writes one file, it does not create directories")
    columns, rows = _execute_fetch(conn, sql, cap)
    row_lists = [_row_to_list(r) for r in rows]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    conn.commit()
    return {
        "path": resolved_path,
        "row_count": len(row_lists),
        "columns": columns,
        "truncated": len(row_lists) >= cap,
    }


def _execute_fetch(conn: Any, sql: str, cap: int) -> tuple[list[str], list[tuple[Any, ...]]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchmany(cap) if columns else []
    return columns, rows


def _row_to_list(row: tuple[Any, ...]) -> list[Any]:
    return [_jsonable(val) for val in row]


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


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None
