"""``activity`` and ``statistics`` — PostgreSQL's administrative views and functions.

Vendor surface (PostgreSQL 17 "Monitoring Database Activity" and "System
Administration Functions"):

- ``activity``: ``list`` reads ``pg_stat_activity``; ``locks`` reads
  ``pg_locks`` with ``pg_blocking_pids()``; ``cancel`` calls
  ``pg_cancel_backend(pid)``; ``terminate`` calls
  ``pg_terminate_backend(pid, timeout)``.
- ``statistics``: ``database_sizes`` (``pg_database_size``), ``relation_sizes``
  (``pg_total_relation_size`` / ``pg_relation_size`` / ``pg_indexes_size``),
  ``table_stats`` (``pg_stat_user_tables``), ``index_stats``
  (``pg_stat_user_indexes``), ``reset`` (``pg_stat_reset()``) and
  ``reset_table`` (``pg_stat_reset_single_table_counters``).

What each operation may see or do is the role's own privilege (for example
``pg_read_all_stats``, ``pg_signal_backend``). List operations page with
``limit`` (1..LIST_LIMIT_MAX, default LIST_LIMIT_DEFAULT) and ``offset``,
fetch one row past the page, and return ``next_offset`` when more rows exist.
Every caller value is a bound ``%s`` parameter. These rows are server metadata
(sessions, locks, sizes, counters), returned inline as one object per row with
the vendor's column names.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .constants import LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX

_ACTIVITY_LIST = (
    "SELECT pid, datname, usename, application_name, client_addr::text AS client_addr, "
    "backend_type, state, wait_event_type, wait_event, backend_start, xact_start, "
    "query_start, state_change, backend_xid::text AS backend_xid, "
    "backend_xmin::text AS backend_xmin, query "
    "FROM pg_stat_activity WHERE (%s::text IS NULL OR state = %s::text) "
    "AND (%s::text IS NULL OR datname = %s::text) "
    "ORDER BY pid LIMIT %s OFFSET %s"
)
_ACTIVITY_LOCKS = (
    "SELECT l.pid, l.locktype, l.database, l.relation::regclass::text AS relation, "
    "l.page, l.tuple, l.virtualxid, l.transactionid::text AS transactionid, l.mode, "
    "l.granted, l.fastpath, l.waitstart, pg_blocking_pids(l.pid) AS blocked_by "
    "FROM pg_locks l WHERE (%s::boolean IS NULL OR l.granted = %s::boolean) "
    "ORDER BY l.pid, l.locktype, l.mode LIMIT %s OFFSET %s"
)
_CANCEL = "SELECT pg_cancel_backend(%s) AS signalled"
_TERMINATE = "SELECT pg_terminate_backend(%s, %s) AS signalled"
_DATABASE_SIZES = "SELECT datname, CASE WHEN has_database_privilege(datname, 'CONNECT') THEN pg_database_size(datname) END AS size_bytes FROM pg_database WHERE NOT datistemplate ORDER BY size_bytes DESC NULLS LAST, datname LIMIT %s OFFSET %s"
_RELATION_SIZES = (
    "SELECT n.nspname AS schema, c.relname AS relation, c.relkind::text AS kind, "
    "pg_total_relation_size(c.oid) AS total_bytes, pg_relation_size(c.oid) AS table_bytes, "
    "pg_indexes_size(c.oid) AS index_bytes, c.reltuples::bigint AS estimated_rows "
    "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'm', 'p') "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%%' AND (%s::text IS NULL OR n.nspname = %s::text) "
    "ORDER BY total_bytes DESC, schema, relation LIMIT %s OFFSET %s"
)
_TABLE_STATS = (
    "SELECT schemaname AS schema, relname AS relation, seq_scan, seq_tup_read, idx_scan, "
    "idx_tup_fetch, n_tup_ins, n_tup_upd, n_tup_del, n_tup_hot_upd, n_live_tup, n_dead_tup, "
    "n_mod_since_analyze, last_vacuum, last_autovacuum, last_analyze, last_autoanalyze, "
    "vacuum_count, autovacuum_count, analyze_count, autoanalyze_count "
    "FROM pg_stat_user_tables WHERE (%s::text IS NULL OR schemaname = %s::text) "
    "ORDER BY schemaname, relname LIMIT %s OFFSET %s"
)
_INDEX_STATS = "SELECT schemaname AS schema, relname AS relation, indexrelname AS index, idx_scan, idx_tup_read, idx_tup_fetch, pg_relation_size(indexrelid) AS index_bytes FROM pg_stat_user_indexes WHERE (%s::text IS NULL OR schemaname = %s::text) ORDER BY schemaname, relname, indexrelname LIMIT %s OFFSET %s"
_RESET = "SELECT pg_stat_reset()"
_RESET_TABLE = "SELECT pg_stat_reset_single_table_counters(r) FROM to_regclass(quote_ident(%s) || '.' || quote_ident(%s)) AS r WHERE r IS NOT NULL"

ACTIVITY_OPERATIONS = ("list", "locks", "cancel", "terminate")
STATISTICS_OPERATIONS = (
    "database_sizes",
    "relation_sizes",
    "table_stats",
    "index_stats",
    "reset",
    "reset_table",
)


def run_activity(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """``activity``: sessions, locks, and the cancel/terminate signals."""
    return _dispatch(conn, params, _ACTIVITY_HANDLERS, ACTIVITY_OPERATIONS)


def run_statistics(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """``statistics``: sizes, table and index counters, and the counter resets."""
    return _dispatch(conn, params, _STATISTICS_HANDLERS, STATISTICS_OPERATIONS)


def _dispatch(
    conn: Any,
    params: dict[str, Any],
    handlers: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]],
    operations: tuple[str, ...],
) -> dict[str, Any]:
    operation = params.get("operation")
    handler = handlers.get(operation) if isinstance(operation, str) else None
    if handler is None:
        raise ValueError(f"'operation' must be one of {list(operations)}")
    return {"operation": operation, **handler(conn, params)}


def _activity_list(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    state = _optional_str(params, "state")
    database = _optional_str(params, "database")
    return _page(conn, params, _ACTIVITY_LIST, (state, state, database, database))


def _activity_locks(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    granted = params.get("granted")
    if granted is not None and not isinstance(granted, bool):
        raise ValueError("'granted' must be a boolean when given")
    return _page(conn, params, _ACTIVITY_LOCKS, (granted, granted))


def _activity_cancel(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    pid = _require_int(params, "pid", minimum=1)
    return {"pid": pid, "signalled": _scalar(conn, _CANCEL, (pid,)) is True}


def _activity_terminate(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    pid = _require_int(params, "pid", minimum=1)
    timeout_ms = params.get("timeout_ms", 0)
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms < 0:
        raise ValueError("'timeout_ms' must be a non-negative integer (0 = do not wait)")
    signalled = _scalar(conn, _TERMINATE, (pid, timeout_ms)) is True
    return {"pid": pid, "timeout_ms": timeout_ms, "signalled": signalled}


def _database_sizes(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    return _page(conn, params, _DATABASE_SIZES, ())


def _relation_sizes(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    schema = _optional_str(params, "schema")
    return _page(conn, params, _RELATION_SIZES, (schema, schema))


def _table_stats(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    schema = _optional_str(params, "schema")
    return _page(conn, params, _TABLE_STATS, (schema, schema))


def _index_stats(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    schema = _optional_str(params, "schema")
    return _page(conn, params, _INDEX_STATS, (schema, schema))


def _reset(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute(_RESET)
    conn.commit()
    return {"reset": True}


def _reset_table(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    schema = _require_str(params, "schema")
    table = _require_str(params, "table")
    with conn.cursor() as cur:
        cur.execute(_RESET_TABLE, (schema, table))
        found = cur.fetchone() is not None
    if not found:
        raise ValueError(f"relation {schema}.{table} does not exist")
    conn.commit()
    return {"reset": True, "schema": schema, "table": table}


_ACTIVITY_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "list": _activity_list,
    "locks": _activity_locks,
    "cancel": _activity_cancel,
    "terminate": _activity_terminate,
}
_STATISTICS_HANDLERS: dict[str, Callable[[Any, dict[str, Any]], dict[str, Any]]] = {
    "database_sizes": _database_sizes,
    "relation_sizes": _relation_sizes,
    "table_stats": _table_stats,
    "index_stats": _index_stats,
    "reset": _reset,
    "reset_table": _reset_table,
}


def _page(
    conn: Any,
    params: dict[str, Any],
    query: str,
    filters: tuple[Any, ...],
) -> dict[str, Any]:
    """Run a paged reader: ``limit`` + 1 rows, ``next_offset`` when one more exists."""
    limit = params.get("limit", LIST_LIMIT_DEFAULT)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= LIST_LIMIT_MAX:
        raise ValueError(f"'limit' must be an integer from 1 to {LIST_LIMIT_MAX}")
    offset = _require_int(params, "offset", minimum=0, default=0)
    with conn.cursor() as cur:
        cur.execute(query, (*filters, limit + 1, offset))
        columns = [d.name for d in cur.description] if cur.description else []
        rows = cur.fetchall()
    has_more = len(rows) > limit
    records = [dict(zip(columns, (_jsonable(v) for v in row), strict=True)) for row in rows[:limit]]
    return {
        "rows": records,
        "returned": len(records),
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if has_more else None,
    }


def _scalar(conn: Any, query: str, args: tuple[Any, ...]) -> Any:
    with conn.cursor() as cur:
        cur.execute(query, args)
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return str(value)


def _optional_str(params: dict[str, Any], key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' must be a non-empty string when given")
    return value


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _require_int(
    params: dict[str, Any],
    key: str,
    *,
    minimum: int,
    default: int | None = None,
) -> int:
    value = params.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"'{key}' must be an integer >= {minimum}")
    return value
