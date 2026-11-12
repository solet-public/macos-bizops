#!/usr/bin/env python3
"""Private live PostgreSQL proof of run_script (one transaction, savepoints).

Run under quality_gates/provision_test_pg.py; the private manifest variable
selects this smoke's independent database on the owned loopback cluster.
No fixture returns 77; provisioned setup or body failures return nonzero.
Proves a migration with a dollar-quoted function body commits, on_error
rollback names the failing statement and commits nothing, on_error continue
keeps the good statements, commit=false is a dry run, and a command the server
refuses in a transaction block reports its SQLSTATE. The dynamic OS-user
assertions remain exercised. See quality_gates/private_postgres.md.
"""

from __future__ import annotations

import getpass
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
REPO_ROOT = _ROOT
sys.path.insert(0, str(REPO_ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import script_actions  # noqa: E402
from external_postgres_plugin.app_config import ExternalDsn  # noqa: E402
from external_postgres_plugin.connection import connect  # noqa: E402
from external_postgres_plugin.statement_guard import (  # noqa: E402
    StatementGuardError,
    assert_single_statement,
)

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
_PROBE = "epg_probe"

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


def _configure_fixture() -> None:
    global _HOST, _PORT, _SCRATCH_DB
    config = load_fixture("external_multistatement")
    if config.user != _USER:
        raise FixtureIntegrityError("fixture user differs from the current OS user")
    _HOST, _PORT, _SCRATCH_DB = config.host, config.port, config.database  # pyright: ignore[reportConstantRedefinition] -- fixture endpoint bound once per run


def _dsn() -> ExternalDsn:
    return ExternalDsn(
        name="scratch",
        host=_HOST,
        port=_PORT,
        dbname=_SCRATCH_DB,
        user=_USER,
        password="",
        sslmode="disable",
    )


def _psql(sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(pg_binary("psql")), "-X", *load_fixture("external_multistatement").endpoint_args(), "-d", _SCRATCH_DB, "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=without_libpq(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )


def _setup() -> bool:
    try:
        subprocess.run([str(pg_binary("createdb")), *load_fixture("external_multistatement").endpoint_args(), "--maintenance-db=postgres", _SCRATCH_DB], env=without_libpq(os.environ), capture_output=True, text=True, timeout=20)
        return _psql(f"DROP SCHEMA IF EXISTS epg_app CASCADE; DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id int); INSERT INTO {_PROBE} VALUES (1), (2);").returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE}; DROP SCHEMA IF EXISTS epg_app CASCADE;")
    except (OSError, subprocess.SubprocessError):
        pass


def _scalar(sql: str) -> str | None:
    result = subprocess.run(
        [str(pg_binary("psql")), "-X", *load_fixture("external_multistatement").endpoint_args(), "-d", _SCRATCH_DB, "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=without_libpq(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def test_parser_belt() -> None:
    _assert("parser admits one statement", _single_ok("SELECT * FROM t WHERE id = 1"))
    _assert("parser refuses two statements on the single-statement verbs", not _single_ok("SELECT 1; DELETE FROM t"))


def _script(sql: str, **params: Any) -> dict[str, Any]:
    conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        return script_actions.run_script(conn, {"sql": sql, **params})
    finally:
        conn.close()


def _run_live_checks() -> None:
    # (1) A migration in one transaction: schema, table, function with a
    # dollar-quoted body containing ';', and rows — all committed.
    migration = "CREATE SCHEMA epg_app;\nCREATE TABLE epg_app.item (id int PRIMARY KEY, name text NOT NULL);\nCREATE FUNCTION epg_app.item_count() RETURNS bigint AS $$ SELECT count(*) FROM epg_app.item; $$ LANGUAGE sql;\nINSERT INTO epg_app.item VALUES (1, 'a'), (2, 'b');\nSELECT epg_app.item_count();\n"
    result = _script(migration)
    print(f"    observed run_script result = {result}")
    _assert("five statements ran", result["statement_count"] == 5 and result["failed_count"] == 0)
    _assert("command tag of the insert", result["statements"][3]["command_tag"] == "INSERT 0 2")
    _assert("the SELECT reports a result set without rows", result["statements"][4]["has_result_set"] is True)
    _assert("migration committed (separate connection)", _scalar("SELECT epg_app.item_count()") == "2")

    # (2) on_error=rollback: the failing statement is named and NOTHING commits.
    failed_message = ""
    try:
        _script("INSERT INTO epg_app.item VALUES (3, 'c');\nINSERT INTO epg_app.item VALUES (1, 'dup');\n")
    except script_actions.ScriptStatementError as exc:
        failed_message = str(exc)
    print(f"    observed failure = {failed_message}")
    _assert("failure names statement 2 of 2", "statement 2 of 2" in failed_message)
    _assert("failure carries SQLSTATE 23505", "SQLSTATE 23505" in failed_message)
    _assert("statement 1 was rolled back too", _scalar("SELECT count(*) FROM epg_app.item") == "2")

    # (3) on_error=continue: only the failing statement rolls back to its savepoint.
    cont = _script(
        "INSERT INTO epg_app.item VALUES (3, 'c');\nINSERT INTO epg_app.item VALUES (1, 'dup');\nINSERT INTO epg_app.item VALUES (4, 'd');\n",
        on_error="continue",
    )
    _assert("continue: two succeeded, one failed", (cont["succeeded_count"], cont["failed_count"]) == (2, 1))
    _assert("continue: the good rows committed", _scalar("SELECT count(*) FROM epg_app.item") == "4")

    # (4) commit=false: a dry run leaves nothing behind.
    dry = _script("DELETE FROM epg_app.item;", commit=False)
    _assert("dry run reports not committed", dry["committed"] is False)
    _assert("dry run left the rows", _scalar("SELECT count(*) FROM epg_app.item") == "4")

    # (5) A command the server refuses in a transaction block fails with its SQLSTATE.
    vacuum_message = ""
    try:
        _script("VACUUM epg_app.item;")
    except script_actions.ScriptStatementError as exc:
        vacuum_message = str(exc)
    _assert("VACUUM in a script reports SQLSTATE 25001", "SQLSTATE 25001" in vacuum_message, vacuum_message)


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
    the first-seen value). Mirrors
    ``bootstrap_stdlib_only_smoke.py:_load_bootstrap_module_fresh``.
    """
    _fresh_load_counter[0] += 1
    module_name = f"_smoke_multistatement_fresh_{_fresh_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(module_name, __file__)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not build an import spec for {__file__}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _check_user_is_dynamic_getuser() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-31): ``_USER``
    must resolve to ``getpass.getuser()`` at import time, NOT a hardcoded
    operator username. Proven the same way ``bootstrap_stdlib_only_smoke.py``
    proves ``_ADMIN_ROLE`` (:99-117): load this module fresh with
    ``getpass.getuser`` patched to a sentinel — a dynamically-sourced constant
    picks the sentinel up; a hardcoded literal would ignore the patch (RED).
    Offline; never touches Postgres.
    """
    sentinel = "smoke_epg_user_sentinel_not_a_real_user"
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
    """Companion to the dynamic-getuser proof above: even with ``_USER``
    correctly derived, a literal could still leak elsewhere in this file's
    prose. Composed from two concatenated halves (see
    ``_OPERATOR_USERNAME_TOKEN``) so this guard's own source never contains
    the contiguous token it hunts for. Word-bounded so it does not collide
    with an unrelated substring.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(_OPERATOR_USERNAME_TOKEN)}(?![A-Za-z0-9_])")
    _assert(
        "source carries no bare operator-username token",
        pattern.search(source) is None,
    )


def main() -> int:
    print("\nexternal_postgres_plugin LIVE run_script smoke")
    print("=" * 46)
    _check_user_is_dynamic_getuser()
    _check_source_carries_no_operator_username()
    test_parser_belt()
    if _failed:
        print("FAILED (belt):", _failed)
        return 1
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    _configure_fixture()
    if not _setup():
        return _mid_run_fail("provisioned private fixture setup failed")
    try:
        _run_live_checks()
    except Exception as exc:
        _teardown()
        return _mid_run_fail(f"live fixture failed mid-run: {exc}")
    _teardown()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All run_script checks passed.")
    print("PRIVATE_PG_BODY external_multistatement")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
