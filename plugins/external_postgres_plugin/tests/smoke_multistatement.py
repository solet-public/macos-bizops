#!/usr/bin/env python3
"""LIVE single-statement + defense-in-depth smoke (§8.6) for external_postgres_plugin.

The single-statement parser (``sqlparse``) is the BELT — it bounds multi-result
/ SET-injection. The read-only CONNECTION is the boundary. This smoke proves
BOTH, and that the belt is not load-bearing for safety:

  1. the parser refuses a two-statement string BEFORE execute (belt)
  2. the parser admits exactly one statement
  3. DEFENSE-IN-DEPTH (LIVE): if the parser were bypassed and a two-statement
     string whose SECOND statement is a write is sent straight to psycopg, the
     read-only connection STILL refuses the write with SQLSTATE 25006 — the
     parser correctness gap never reaches the destruction boundary (rev-F reframe)
  4. two READS bypassing the parser both run (proving the parser is hygiene, not
     safety)

Same local ``epg_smoke_scratch`` fixture as smoke_readonly.py, and the same
LOUD-ON-UNREACHABLE contract (Coordinator-Day Q2): an unreachable fixture FAILS
LOUD (exit 1, unmissable banner), never a silent exit-0 skip — because run_smokes
only surfaces a smoke's output on FAIL.

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_multistatement.py

Exits 0 only when the live proof ran and passed; exits 1 on a real failure OR an
unreachable fixture.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402
from external_postgres_plugin.statement_guard import (  # noqa: E402
    StatementGuardError,
    assert_single_statement,
)

_SCRATCH_DB = "epg_smoke_scratch"
_HOST = "localhost"
_PORT = 5432
_USER = "dw"
_PROBE = "epg_probe"
_READ_ONLY_SQLSTATE = "25006"

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


def _dsn() -> ExternalDsn:
    return ExternalDsn(
        name="scratch", host=_HOST, port=_PORT, dbname=_SCRATCH_DB,
        user=_USER, password="", sslmode="disable",
    )


def _psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-U", _USER, "-d", _SCRATCH_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True, text=True, timeout=20,
    )


def _setup() -> bool:
    try:
        subprocess.run(["createdb", "-U", _USER, _SCRATCH_DB], capture_output=True, text=True, timeout=20)
        return _psql(
            f"DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id int); "
            f"INSERT INTO {_PROBE} VALUES (1), (2);"
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE};")
    except (OSError, subprocess.SubprocessError):
        pass


def _sqlstate(conn: Any, sql: str) -> str | None:
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.rollback()
        return None
    except Exception as exc:
        conn.rollback()
        return getattr(exc, "sqlstate", None)


def test_parser_belt() -> None:
    _assert("parser admits one statement", _single_ok("SELECT * FROM t WHERE id = 1"))
    _assert("parser refuses two statements", not _single_ok("SELECT 1; DELETE FROM t"))


def _run_live_checks() -> None:
    conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        # Bypass the parser: send the two-statement string straight to psycopg.
        state = _sqlstate(conn, f"SELECT 1; INSERT INTO {_PROBE} VALUES (99)")
        print(f"    observed SQLSTATE {state} for a parser-bypassing 2-stmt write")
        _assert("parser-bypass write refused at the connection (25006)", state == _READ_ONLY_SQLSTATE)

        # Two READS bypassing the parser both run — the parser is hygiene, not safety.
        two_reads_ok = _sqlstate(conn, "SELECT 1; SELECT 2") is None
        _assert("two reads bypassing the parser both run (no error)", two_reads_ok)
    finally:
        conn.close()


def _unreachable_fail(detail: str) -> int:
    # LOUD, non-zero exit (Coordinator-Day Q2 ruling): run_smokes only surfaces a
    # smoke's output on FAIL, so a silent exit-0 skip is invisible (GTE-09 rot).
    # Failing loud makes "the live defense-in-depth proof did NOT run" unmissable.
    print("=" * 70)
    print("SKIPPED-LIVE-DB-UNREACHABLE — FAILING LOUD (not a silent pass)")
    print(f"  {detail}")
    print("  The LIVE parser-bypass 25006 defense-in-depth proof DID NOT RUN.")
    print("  Bring up local Postgres (dw @ localhost:5432 + createdb/psql) and re-run.")
    print("=" * 70)
    return 1


def main() -> int:
    print("\nexternal_postgres_plugin LIVE single-statement / defense-in-depth smoke")
    print("=" * 68)
    test_parser_belt()
    if _failed:
        print("FAILED (belt):", _failed)
        return 1
    if not _setup():
        return _unreachable_fail("scratch Postgres fixture unreachable.")
    try:
        _run_live_checks()
    except Exception as exc:
        _teardown()
        return _unreachable_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All single-statement / defense-in-depth checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
