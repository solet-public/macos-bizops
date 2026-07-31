#!/usr/bin/env python3
"""LIVE read-only enforcement smoke (§8.5) for external_postgres_plugin.

This is the LOAD-BEARING proof: the psycopg3 connection read-only characteristic
is the developer-proof write-stopper, not the belt guards. It needs a REAL
Postgres (MagicMock cannot prove SQLSTATE 25006), so it runs against a local
SCRATCH database — NEVER the platform's own DB.

FIXTURE (documented): a local Homebrew Postgres, database ``epg_smoke_scratch``,
connected as ``dw`` (the passwordless trust SUPERUSER) over ``localhost:5432``,
``sslmode=disable``. Connecting as the superuser is deliberate — it proves that
even an OVER-PRIVILEGED registered credential cannot write through this tool
(the read-only characteristic binds regardless of role). Setup/teardown of the
probe table go through ``psql`` (a read-WRITE path) so this smoke never needs a
raw driver import.

LOUD-ON-UNREACHABLE (Coordinator-Day Q2 ruling, GTE-09): if Postgres/``dw``/
``psql`` are unavailable the smoke FAILS LOUD (exit 1) with an unmissable
SKIPPED-LIVE-DB-UNREACHABLE banner — NOT a silent exit-0 skip. run_smokes only
surfaces a smoke's output on FAIL, so a silent skip would be invisible in the
suite (the GTE-09 rot); failing loud makes "the 25006 proof did not run"
unmissable. The scratch cluster is the same 24/7 instance the platform needs, so
this path is pathological. It runs FOR REAL wherever the scratch DB is reachable.

Proves:
  1. conn.read_only is True
  2. test_connection echoes the RESOLVED host it actually connected to and it
     equals the host we connected with (F1 — Rev-A R-D2 "surfaces resolved host")
  3. SHOW transaction_read_only == 'on' in the SAME transaction as the user query
     (the exact Codex BLOCKER: proves the CURRENT txn is read-only, not just
     default_transaction_read_only)
  4. EXPLAIN / SHOW / SELECT succeed
  5. INSERT/UPDATE/DELETE/CREATE/CREATE TEMP/EXPLAIN ANALYZE <write> all refused
     with SQLSTATE 25006
  6. a write as the VERY FIRST statement on a fresh connection is refused — no
     write-capable window (the rev-E point)

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_readonly.py

Exits 0 only when the live proof actually ran and passed; exits 1 on a real
failure OR when the fixture is unreachable (loud, never silent).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import query_actions  # noqa: E402
from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402

_SCRATCH_DB = "epg_smoke_scratch"
_HOST = "localhost"
_PORT = 5432
_USER = "dw"
_SSLMODE = "disable"
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


def _dsn() -> ExternalDsn:
    return ExternalDsn(
        name="scratch",
        host=_HOST,
        port=_PORT,
        dbname=_SCRATCH_DB,
        user=_USER,
        password="",
        sslmode=_SSLMODE,
    )


def _psql(sql: str, dbname: str = _SCRATCH_DB) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["psql", "-U", _USER, "-d", dbname, "-v", "ON_ERROR_STOP=1", "-c", sql],
        capture_output=True,
        text=True,
        timeout=20,
    )


def _setup() -> bool:
    """Create the scratch DB + seed the probe table via psql (read-write path)."""
    try:
        subprocess.run(
            ["createdb", "-U", _USER, _SCRATCH_DB], capture_output=True, text=True, timeout=20
        )  # ignore failure — the DB may already exist
        seed = _psql(
            f"DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id int); "
            f"INSERT INTO {_PROBE} VALUES (1), (2);"
        )
        return seed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE};")
    except (OSError, subprocess.SubprocessError):
        pass


def _sqlstate_of_write(conn: Any, sql: str) -> str | None:
    """Attempt a write; return its SQLSTATE (rolling the txn back), or None if it slipped."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.rollback()
        return None
    except Exception as exc:  # a real psycopg error carries .sqlstate
        conn.rollback()
        return getattr(exc, "sqlstate", None)


def _run_live_checks() -> None:
    conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        _assert("conn.read_only is True", conn.read_only is True)

        # LIVE test_connection: the resolved host echoed back must equal the host
        # we actually connected with (F1 — Rev-A R-D2 "test_connection surfaces
        # the resolved host so the operator can confirm/catch a mis-registration").
        tc = query_actions.test_connection(conn, {})
        conn.rollback()
        print(f"    observed test_connection host = {tc.get('host')!r} (connected with {_HOST!r})")
        _assert("test_connection returns a host field", "host" in tc)
        _assert("resolved host equals the connected host", tc.get("host") == _HOST, str(tc.get("host")))
        _assert("test_connection read_only True", tc.get("read_only") is True)

        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            row = cur.fetchone()
            show_val = row[0] if row else None
            cur.execute(f"SELECT count(*) FROM {_PROBE}")
            count_row = cur.fetchone()
        conn.rollback()
        print(f"    observed SHOW transaction_read_only = {show_val!r} (same txn as the SELECT)")
        _assert("SHOW transaction_read_only == 'on' in the user query's txn", show_val == "on")

        with conn.cursor() as cur:
            cur.execute("EXPLAIN SELECT 1")
            explained = cur.fetchall()
        conn.rollback()
        _assert("EXPLAIN succeeds", len(explained) > 0)
        _assert("SELECT on the probe table returned the seeded rows", count_row is not None and count_row[0] == 2)

        writes = (
            "CREATE TEMP TABLE t_probe (a int)",
            f"INSERT INTO {_PROBE} VALUES (99)",
            f"UPDATE {_PROBE} SET id = 0",
            f"DELETE FROM {_PROBE}",
            "CREATE TABLE t_probe2 (a int)",
            f"EXPLAIN ANALYZE INSERT INTO {_PROBE} VALUES (99)",
        )
        for sql in writes:
            state = _sqlstate_of_write(conn, sql)
            print(f"    observed SQLSTATE {state} for: {sql}")
            _assert(f"write refused with 25006: {sql!r}", state == _READ_ONLY_SQLSTATE)
    finally:
        conn.close()

    # First-statement-is-a-write on a FRESH connection: no write-capable window.
    fresh = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        first_state = _sqlstate_of_write(fresh, "CREATE TABLE t_first (a int)")
        print(f"    observed SQLSTATE {first_state} for the FIRST statement (a write) on a fresh conn")
        _assert("first-statement write refused (no window)", first_state == _READ_ONLY_SQLSTATE)
    finally:
        fresh.close()


def _unreachable_fail(detail: str) -> int:
    # LOUD, non-zero exit (Coordinator-Day Q2 ruling): run_smokes ONLY surfaces a
    # smoke's output on FAIL, so a silent exit-0 skip is invisible in the suite —
    # exactly the GTE-09 rot. Failing loud makes "the live 25006 proof did NOT run"
    # unmissable in run_smokes output. The scratch cluster is the same 24/7 Homebrew
    # instance the platform needs, so this path is pathological, not routine.
    print("=" * 70)
    print("SKIPPED-LIVE-DB-UNREACHABLE — FAILING LOUD (not a silent pass)")
    print(f"  {detail}")
    print("  The LIVE read-only 25006 proof DID NOT RUN. Bring up local Postgres")
    print("  (dw @ localhost:5432 + createdb/psql) and re-run.")
    print("=" * 70)
    return 1


def main() -> int:
    print("\nexternal_postgres_plugin LIVE read-only smoke")
    print("=" * 44)
    if not _setup():
        return _unreachable_fail(
            "scratch Postgres fixture unreachable (createdb/psql/dw/localhost:5432)."
        )
    try:
        _run_live_checks()
    except Exception as exc:  # fixture failed mid-run -> loud FAIL, not a silent skip
        _teardown()
        return _unreachable_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All LIVE read-only checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
