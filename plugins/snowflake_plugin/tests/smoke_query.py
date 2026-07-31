#!/usr/bin/env python3
"""Query-verb + classification smoke tests for snowflake_plugin.

Hermetic — a faked Snowflake connection/cursor, no live warehouse. Red-first:
every check asserts REAL behavior in query_actions / connection.classify_
snowflake_error / the EDGE parity.

Exercises:
  1. run_query — inline row/column shape + row_count
  2. run_query — refuses a write-leader statement (guard)
  3. run_query — max_rows clamped to the 1000 hard cap (fetchmany arg)
  4. list_databases — name extraction from a SHOW DATABASES row
  5. list_schemas / list_tables / describe_table — shape + identifier escaping
  6. test_connection — account/user/role/warehouse/version shape
  7. TOPOLOGY-LEAK (SECURITY): auth/timeout/warehouse-suspended classes
     classify to a GENERIC message that NEVER contains the driver exception's
     account/user marker; object-not-found keeps driver detail
  8. plugin-level: a driver auth fault surfaces a generic error, marker absent
  9. EDGE parity: validate_edge_process_provider raises nothing

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/snowflake_plugin/tests/smoke_query.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "snowflake_plugin" / "src"))

from snowflake_plugin import connection, query_actions  # noqa: E402
from snowflake_plugin.app_config import SnowflakeAccountConfig  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402
from snowflake_plugin.statement_guard import StatementGuardError  # noqa: E402

_ACCOUNT_MARKER = "SECRET-ACCOUNT-9.9.9.9"
_USER_MARKER = "SECRET-USER-marker"

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _fake_conn(columns: list[str] | None, rows: list[tuple[Any, ...]]) -> tuple[Any, Any]:
    cur = MagicMock()
    cur.description = [(c,) for c in columns] if columns else None
    cur.fetchmany.return_value = rows
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    ctx.__exit__.return_value = False
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur



class _FakeDriverError(Exception):
    def __init__(self, errno: int, message: str, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.errno = errno
        self.sqlstate = sqlstate
        self.msg = message


def test_run_query_inline() -> None:
    conn, _ = _fake_conn(["id", "name"], [(1, "alice"), (2, "bob")])
    result = query_actions.run_query(conn, {"sql": "SELECT id, name FROM t"})
    _assert("inline columns carried", result["columns"] == ["id", "name"])
    _assert("inline rows as parallel lists", result["rows"] == [[1, "alice"], [2, "bob"]])
    _assert("inline row_count", result["row_count"] == 2)
    _assert("inline not spilled", result["spilled"] is False)


def test_run_query_guard() -> None:
    conn, _ = _fake_conn(["id"], [(1,)])
    write = False
    try:
        query_actions.run_query(conn, {"sql": "DELETE FROM t"})
    except StatementGuardError:
        write = True
    _assert("write-leader run_query refused", write)


def test_run_query_max_rows_clamp() -> None:
    conn, cur = _fake_conn(["id"], [(1,)])
    query_actions.run_query(conn, {"sql": "SELECT id FROM t", "max_rows": 999999})
    _assert("max_rows clamped to 1000 hard cap", cur.fetchmany.call_args.args[0] == 1000)


def test_list_databases() -> None:
    conn, _ = _fake_conn(["created_on", "name", "is_default"], [("2026-01-01", "ANALYTICS", "N")])
    result = query_actions.list_databases(conn, {})
    _assert("databases shape", result["databases"] == [{"name": "ANALYTICS"}])


def test_list_schemas() -> None:
    conn, _ = _fake_conn(["created_on", "name"], [("2026-01-01", "PUBLIC")])
    result = query_actions.list_schemas(conn, {"database": "ANALYTICS"})
    _assert("schemas shape", result["schemas"] == ["PUBLIC"])


def test_list_tables() -> None:
    conn, _ = _fake_conn(["created_on", "name", "kind"], [("2026-01-01", "ORDERS", "TABLE")])
    result = query_actions.list_tables(conn, {"database": "ANALYTICS", "schema": "PUBLIC"})
    _assert("tables shape", result["tables"] == [{"name": "ORDERS", "kind": "TABLE"}])


def test_describe_table() -> None:
    conn, _ = _fake_conn(
        ["name", "type", "kind", "null?", "default"],
        [("id", "NUMBER(38,0)", "COLUMN", "N", None), ("label", "VARCHAR", "COLUMN", "Y", "''")],
    )
    cols = query_actions.describe_table(conn, {"database": "d", "schema": "s", "table": "t"})["columns"]
    _assert("column name/type", cols[0]["name"] == "id" and cols[0]["type"] == "NUMBER(38,0)")
    _assert("N -> nullable False", cols[0]["nullable"] is False)
    _assert("Y -> nullable True", cols[1]["nullable"] is True)


def test_test_connection() -> None:
    conn, _ = _fake_conn(
        ["account", "user", "role", "warehouse", "version"],
        [("myorg-acct", "OPERATOR_USER", "EXAMPLE_READONLY", "WH_XS", "8.1.0")],
    )
    result = query_actions.test_connection(conn, {})
    _assert("ok True", result["ok"] is True)
    _assert("account carried", result["account"] == "myorg-acct")
    _assert("role carried", result["role"] == "EXAMPLE_READONLY")


def test_classify_topology_leak() -> None:
    auth_err = _FakeDriverError(390144, f"JWT rejected for account {_ACCOUNT_MARKER} user {_USER_MARKER}")
    code, message = connection.classify_snowflake_error(auth_err)
    _assert("auth-class -> auth_failed", code == "snowflake.auth_failed")
    _assert("auth message hides account marker", _ACCOUNT_MARKER not in message, message)
    _assert("auth message hides user marker", _USER_MARKER not in message, message)

    timeout_err = _FakeDriverError(604, f"query timed out on account {_ACCOUNT_MARKER}")
    code, message = connection.classify_snowflake_error(timeout_err)
    _assert("timeout-class -> timeout", code == "snowflake.timeout")
    _assert("timeout message hides account marker", _ACCOUNT_MARKER not in message, message)

    suspended_err = _FakeDriverError(606, f"warehouse suspended for {_ACCOUNT_MARKER}")
    code, message = connection.classify_snowflake_error(suspended_err)
    _assert("warehouse-suspended-class -> warehouse_suspended", code == "snowflake.warehouse_suspended")
    _assert("warehouse message hides account marker", _ACCOUNT_MARKER not in message, message)

    not_found_err = _FakeDriverError(2003, "SQL compilation error: Table 'FOO' does not exist")
    code, message = connection.classify_snowflake_error(not_found_err)
    _assert("not-found carries driver detail", "FOO" in message, message)


def test_plugin_level_generic_error() -> None:
    plugin = SnowflakePlugin()
    plugin.initialize({})  # bind config_provider (defaults apply); unbound = config fault
    plugin._app_config_loader = MagicMock()
    plugin._app_config_loader.resolve.return_value = SnowflakeAccountConfig(
        account="a", user="u", warehouse="w", database="d", schema="s", role="r",
        auth_method="key_pair", private_key_der=b"fake",
    )
    original = connection.connect

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise _FakeDriverError(390144, f"JWT rejected for {_USER_MARKER}@{_ACCOUNT_MARKER}")

    connection.connect = _boom  # type: ignore[assignment]
    try:
        result = plugin.run_query({"sql": "SELECT 1"}, {})
    finally:
        connection.connect = original  # type: ignore[assignment]
    _assert("plugin surfaces an error status", result["action_status"] == "error")
    _assert("plugin error code is auth_failed", result["error"]["code"] == "snowflake.auth_failed")
    _assert(
        "plugin error message hides account+user markers",
        _ACCOUNT_MARKER not in result["error"]["message"] and _USER_MARKER not in result["error"]["message"],
        result["error"]["message"],
    )


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = SnowflakePlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider(
            "snowflake_plugin", plugin, actions
        )
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 7 verbs discovered", len(actions) == 7)


def main() -> int:
    print("\nsnowflake_plugin query-verb smoke tests")
    print("=" * 47)
    test_run_query_inline()
    test_run_query_guard()
    test_run_query_max_rows_clamp()
    test_list_databases()
    test_list_schemas()
    test_list_tables()
    test_describe_table()
    test_test_connection()
    test_classify_topology_leak()
    test_plugin_level_generic_error()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All query-verb smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
