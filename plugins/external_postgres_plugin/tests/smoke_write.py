#!/usr/bin/env python3
"""Private live PostgreSQL write behavioral proof.

Run under quality_gates/provision_test_pg.py; the private manifest variable
selects this smoke's independent database on the owned loopback cluster.
No fixture returns 77; provisioned setup or body failures return nonzero.
The original SQL behavior, read/write probes and dynamic OS-user assertions
remain exercised. See quality_gates/private_postgres.md.
"""

from __future__ import annotations

import getpass
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
REPO_ROOT = _ROOT
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import query_actions  # noqa: E402
from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402
from external_postgres_plugin.statement_guard import StatementGuardError  # noqa: E402

from quality_gates.private_test_pg import (  # noqa: E402
    MANIFEST_ENV,
    FixtureIntegrityError,
    load_fixture,
    pg_binary,
    skip_fixture,
    without_libpq,
)

_SCRATCH_DB = ""
_HOST = "127.0.0.1"
_PORT = 0
_USER = getpass.getuser()
_SSLMODE = "disable"
_PROBE = "epg_write_probe"

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


def _configure_fixture() -> None:
    global _HOST, _PORT, _SCRATCH_DB
    config = load_fixture("external_write")
    if config.user != _USER:
        raise FixtureIntegrityError("fixture user differs from the current OS user")
    _HOST, _PORT, _SCRATCH_DB = config.host, config.port, config.database


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


def _psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(pg_binary("psql")), "-X", *load_fixture("external_write").endpoint_args(), "-d", _SCRATCH_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=without_libpq(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _psql_scalar(sql: str) -> str | None:
    result = subprocess.run(
        [str(pg_binary("psql")), "-X", *load_fixture("external_write").endpoint_args(), "-d", _SCRATCH_DB, "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=without_libpq(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _setup() -> bool:
    try:
        subprocess.run([str(pg_binary("createdb")), *load_fixture("external_write").endpoint_args(), "--maintenance-db=postgres", _SCRATCH_DB], env=without_libpq(os.environ), capture_output=True, text=True, timeout=20)  # ignore failure — the DB may already exist
        seed = _psql(f"DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id serial PRIMARY KEY, val int);")
        return seed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE};")
    except (OSError, subprocess.SubprocessError):
        pass


def _passthrough_gate(path: str) -> str:
    return path


def _run_live_checks(workspace: Path) -> None:
    write_conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT, read_only=False)
    try:
        _assert("conn.read_only is False for a write connection", write_conn.read_only is False)

        # (2) no-RETURNING write: commits, visible from a FRESH separate connection.
        result = query_actions.run_statement(
            write_conn,
            {"sql": f"INSERT INTO {_PROBE}(val) VALUES (42)"},
            _passthrough_gate,
        )
        print(f"    observed run_statement (no RETURNING) result = {result}")
        _assert("no-RETURNING has_result_set False", result.get("has_result_set") is False)
        _assert("no-RETURNING rowcount == 1", result.get("rowcount") == 1)
        count = _psql_scalar(f"SELECT count(*) FROM {_PROBE} WHERE val = 42")
        print(f"    observed count via a SEPARATE psql connection = {count!r}")
        _assert("the write is visible from a fresh separate connection (real commit)", count == "1")

        # (3) RETURNING write: real driver-populated result set, real TSV, real commit.
        out_path = str(workspace / "returning.tsv")
        result2 = query_actions.run_statement(
            write_conn,
            {
                "sql": f"INSERT INTO {_PROBE}(val) VALUES (99) RETURNING id, val",
                "output_tsv_path": out_path,
            },
            _passthrough_gate,
        )
        print(f"    observed run_statement (RETURNING) result = {result2}")
        _assert("RETURNING has_result_set True", result2.get("has_result_set") is True)
        _assert("RETURNING row_count == 1", result2.get("row_count") == 1)
        lines = Path(out_path).read_text(encoding="utf-8").splitlines()
        _assert("RETURNING TSV header", lines[0] == "id\tval")
        _assert("RETURNING TSV carries the written value", any(line.endswith("\t99") for line in lines[1:]))
        count2 = _psql_scalar(f"SELECT count(*) FROM {_PROBE} WHERE val = 99")
        _assert("the RETURNING write is also visible from a fresh separate connection", count2 == "1")

        # (5) shape guard vs. access-control guard, live.
        two_stmt_refused = False
        try:
            query_actions.run_statement(
                write_conn,
                {"sql": f"UPDATE {_PROBE} SET val = 1; UPDATE {_PROBE} SET val = 2"},
                _passthrough_gate,
            )
        except StatementGuardError:
            two_stmt_refused = True
        _assert("two-statement run_statement refused (shape guard)", two_stmt_refused)

        delete_refused = False
        try:
            query_actions.run_statement(write_conn, {"sql": f"DELETE FROM {_PROBE} WHERE val = 42"}, _passthrough_gate)
        except StatementGuardError:
            delete_refused = True
        _assert("a DELETE is NOT refused by run_statement (no access-control guard on this path)", not delete_refused)
        remaining = _psql_scalar(f"SELECT count(*) FROM {_PROBE} WHERE val = 42")
        _assert("the live DELETE actually removed the row", remaining == "0")
    finally:
        write_conn.close()

    # (4) the write reversal is verb-scoped: a read_only=True connection on the
    # SAME scratch DB, in the SAME run, still cannot write.
    read_conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        _assert("conn.read_only is True for the DEFAULT (unaffected) connection", read_conn.read_only is True)
        refused_state: str | None = None
        try:
            with read_conn.cursor() as cur:
                cur.execute(f"INSERT INTO {_PROBE}(val) VALUES (7)")
            read_conn.rollback()
        except Exception as exc:  # a real psycopg error carries .sqlstate
            read_conn.rollback()
            refused_state = getattr(exc, "sqlstate", None)
        print(f"    observed SQLSTATE {refused_state} for a write on the default read-only connection")
        _assert(
            "the default (read_only=True) connection still refuses a write — reversal is verb-scoped",
            refused_state == "25006",
        )
    finally:
        read_conn.close()


def _mid_run_fail(detail: str) -> int:
    print("=" * 70)
    print("LIVE-DB FIXTURE FAILED MID-RUN")
    print(f"  {detail}")
    print("=" * 70)
    return 1


_fresh_load_counter = [0]


def _load_module_fresh() -> Any:
    """Load a FRESH copy of this same file (unique module name, never cached)
    so a test can observe its module-level ``_USER`` under a patched
    ``getpass.getuser`` (a normal import would reuse ``sys.modules`` and freeze
    the first-seen value). Mirrors smoke_readonly.py's identical helper.
    """
    _fresh_load_counter[0] += 1
    module_name = f"_smoke_write_fresh_{_fresh_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(module_name, __file__)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build an import spec for {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _check_user_is_dynamic_getuser() -> None:
    """RED-FIRST: ``_USER`` must resolve to ``getpass.getuser()`` at import
    time, NOT a hardcoded operator username — same proof as
    smoke_readonly.py's identical check, independently re-run here since this
    file has its own separate ``_USER`` binding a regression there wouldn't
    catch.
    """
    sentinel = "smoke_epg_write_user_sentinel_not_a_real_user"
    with patch.object(getpass, "getuser", return_value=sentinel):
        fresh = _load_module_fresh()
    _assert(
        "_USER is dynamically sourced from getpass.getuser() (not a hardcoded username)",
        fresh._USER == sentinel,
        f"got {fresh._USER!r}; a hardcoded user would ignore the patched getuser()",
    )
    real_user = getpass.getuser()
    _assert(
        "the resolved _USER equals getpass.getuser() and is non-empty",
        bool(real_user) and _USER == real_user,
        f"got {_USER!r} vs getpass.getuser()={real_user!r}",
    )


_OPERATOR_USERNAME_TOKEN = "d" + "w"


def _check_source_carries_no_operator_username() -> None:
    """Companion to the dynamic-getuser proof above — even with ``_USER``
    correctly derived, a literal could still leak elsewhere in this file's
    prose. Same construction as smoke_readonly.py's identical check.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _assert(
        "source carries no bare operator-username token",
        pattern.search(source) is None,
    )


def main() -> int:
    print("\nexternal_postgres_plugin LIVE write-capability smoke")
    print("=" * 53)
    _check_user_is_dynamic_getuser()
    _check_source_carries_no_operator_username()
    if _failed:
        print()
        print(f"Results: {_passed} passed, {len(_failed)} failed")
        print("FAILED:", _failed)
        return 1
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    _configure_fixture()
    if not _setup():
        return _mid_run_fail("provisioned private fixture setup failed")
    try:
        with tempfile.TemporaryDirectory(prefix="epg_write_smoke_") as workspace:
            _run_live_checks(Path(workspace))
    except Exception as exc:  # fixture failed mid-run -> loud FAIL, not a silent skip
        _teardown()
        return _mid_run_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All LIVE write-capability checks passed.")
    print("PRIVATE_PG_BODY external_write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
