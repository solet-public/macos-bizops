#!/usr/bin/env python3
"""Statement SHAPE smoke tests for external_postgres_plugin.

Hermetic — no Postgres. The statement module splits and counts statements; it
never decides read versus write (USER ruling rul_4c99a30d: the registered
role's GRANTs are the only boundary).

RED-FIRST teeth:
  1. the read-leader allowlist is GONE — ``assert_read_statement`` and
     ``READ_LEADERS`` no longer exist, and every write/DDL leader passes the
     shape check (re-adding a leader refusal flips these to FAIL)
  2. single statement OK; two statements REFUSED, and the refusal points at
     run_script (neutralizing ``assert_single_statement`` flips this)
  3. a ';' inside a string or a dollar-quoted function body does NOT split
  4. empty / whitespace / comment-only REFUSED
  5. ``split_statements`` returns one entry per statement, trailing ';'
     stripped, empty fragments dropped
  6. ``leading_keyword`` sees the real leader past comments and parentheses

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_guard.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import constants, statement_guard  # noqa: E402
from external_postgres_plugin.statement_guard import (  # noqa: E402
    StatementGuardError,
    assert_single_statement,
    leading_keyword,
    split_statements,
)

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


def _single_ok(sql: str) -> bool:
    try:
        assert_single_statement(sql)
        return True
    except StatementGuardError:
        return False


def test_no_read_leader_allowlist() -> None:
    _assert("assert_read_statement removed", not hasattr(statement_guard, "assert_read_statement"))
    _assert("READ_LEADERS removed", not hasattr(constants, "READ_LEADERS"))
    for sql in (
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "MERGE INTO t USING s ON (t.id = s.id) WHEN MATCHED THEN DELETE",
        "CREATE TABLE t (a int)",
        "CREATE TEMP TABLE t (a int)",
        "DROP TABLE t",
        "ALTER TABLE t ADD COLUMN b int",
        "TRUNCATE t",
        "GRANT SELECT ON t TO r",
        "WITH d AS (DELETE FROM t RETURNING id) SELECT id FROM d",
    ):
        _assert(f"write/DDL leader passes the shape check: {sql!r}", _single_ok(sql), sql)


def test_single_vs_multi() -> None:
    _assert("single statement OK", _single_ok("SELECT * FROM t WHERE id = 1"))
    _assert("trailing semicolon OK", _single_ok("SELECT 1;"))
    try:
        assert_single_statement("SELECT 1; DELETE FROM t")
        refused, message = False, ""
    except StatementGuardError as exc:
        refused, message = exc.code == "external_pg.invalid_params", str(exc)
    _assert("two statements REFUSED as invalid_params", refused)
    _assert("the refusal points at run_script", "run_script" in message, message)


def test_quoting_does_not_split() -> None:
    _assert("';' inside a string does not split", _single_ok("SELECT ';' AS semi, id FROM t"))
    body = "CREATE FUNCTION f() RETURNS int AS $$ SELECT 1; SELECT 2; $$ LANGUAGE sql"
    _assert("';' inside a dollar-quoted body does not split", _single_ok(body))


def test_empty_refused() -> None:
    _assert("empty string REFUSED", not _single_ok("   "))
    _assert("comment-only REFUSED", not _single_ok("-- just a comment"))


def test_split_statements() -> None:
    script = "CREATE SCHEMA app;\nCREATE FUNCTION app.f() RETURNS int AS $$ SELECT 1; $$ LANGUAGE sql;\nINSERT INTO app.t VALUES (';');\n-- trailing comment only\n"
    parts = split_statements(script)
    _assert("three statements", len(parts) == 3, str(parts))
    _assert("trailing ';' stripped", parts[0] == "CREATE SCHEMA app", parts[0])
    _assert("function body kept whole", "SELECT 1; $$" in parts[1], parts[1])
    _assert("quoted ';' kept in its statement", parts[2] == "INSERT INTO app.t VALUES (';')")
    _assert("empty script splits to nothing", split_statements("  -- x\n") == [])


def test_leading_keyword() -> None:
    _assert("comment-led leader", leading_keyword("-- report\nDELETE FROM t") == "DELETE")
    _assert("parenthesized leader", leading_keyword("(SELECT 1)") == "SELECT")
    _assert("lower-case leader", leading_keyword("  begin") == "BEGIN")
    _assert("empty has no leader", leading_keyword("   ") == "")


def main() -> int:
    print("\nexternal_postgres_plugin statement-shape smoke tests")
    print("=" * 52)
    test_no_read_leader_allowlist()
    test_single_vs_multi()
    test_quoting_does_not_split()
    test_empty_refused()
    test_split_statements()
    test_leading_keyword()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All statement-guard smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
