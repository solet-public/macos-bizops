#!/usr/bin/env python3
"""Private live PostgreSQL write proof and the build-1 acceptance walk.

Run under quality_gates/provision_test_pg.py; the private manifest variable
selects this smoke's independent database on the owned loopback cluster.
No fixture returns 77; provisioned setup or body failures return nonzero.
Proves run_statement commits (visible from a separate connection), then walks
the build-1 acceptance path: create a schema, migrate it (run_script), bulk-load
a CSV (copy), dump it (pg_dump), restore the archive into a second database
(pg_restore; plain SQL restores are refused), and read activity, locks and
statistics. The
dynamic OS-user assertions remain exercised. See quality_gates/private_postgres.md.
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

from external_postgres_plugin import (  # noqa: E402
    admin_actions,
    client_tools,
    copy_actions,
    query_actions,
    script_actions,
)
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
    _HOST, _PORT, _SCRATCH_DB = config.host, config.port, config.database  # pyright: ignore[reportConstantRedefinition] -- fixture endpoint bound once per run


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


def _psql_on(dbname: str, sql: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(pg_binary("psql")), "-X", *load_fixture("external_write").endpoint_args(), "-d", dbname, "-v", "ON_ERROR_STOP=1", "-c", sql],
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
        seed = _psql(f"DROP SCHEMA IF EXISTS epg_walk CASCADE; DROP TABLE IF EXISTS {_PROBE}; CREATE TABLE {_PROBE}(id serial PRIMARY KEY, val int);")
        _psql_on("postgres", f"DROP DATABASE IF EXISTS {_restore_db()}")
        restore = _psql_on("postgres", f"CREATE DATABASE {_restore_db()}")
        return seed.returncode == 0 and restore.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _teardown() -> None:
    try:
        _psql(f"DROP TABLE IF EXISTS {_PROBE}; DROP SCHEMA IF EXISTS epg_walk CASCADE;")
        _psql_on("postgres", f"DROP DATABASE IF EXISTS {_restore_db()}")
    except (OSError, subprocess.SubprocessError):
        pass


def _passthrough_gate(path: str) -> str:
    return path


def _run_live_checks(workspace: Path) -> None:
    write_conn = connect(_dsn(), statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        _assert("the connection carries no read-only characteristic (None)", write_conn.read_only is None)

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

    _acceptance_walk(workspace)


def _file_gate(path: str, _param: str) -> str:
    return path


def _restore_db() -> str:
    return f"{_SCRATCH_DB}_restore"


def _walk_scalar(sql: str, dbname: str) -> str | None:
    result = subprocess.run(
        [str(pg_binary("psql")), "-X", *load_fixture("external_write").endpoint_args(), "-d", dbname, "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=without_libpq(os.environ),
        capture_output=True,
        text=True,
        timeout=20,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _on(dsn: ExternalDsn, action: Any) -> Any:
    conn = connect(dsn, statement_timeout_ms=30_000, platform_pg_port=_PORT)
    try:
        return action(conn)
    finally:
        conn.close()


def _acceptance_walk(workspace: Path) -> None:
    """Build-1 acceptance: create a schema, migrate it, bulk-load a CSV, dump it,
    restore it into a second database, and read the activity/statistics views."""
    source = _dsn()
    target = ExternalDsn(
        name="restore",
        host=_HOST,
        port=_PORT,
        dbname=_restore_db(),
        user=_USER,
        password="",
        sslmode=_SSLMODE,
    )
    settings = client_tools.ToolSettings(bin_dir=str(pg_binary("psql").parent), timeout_seconds=120, platform_pg_port=5432)

    created = _on(source, lambda c: script_actions.run_script(c, {"sql": ("CREATE SCHEMA epg_walk;\nCREATE TABLE epg_walk.customer (id int PRIMARY KEY, name text NOT NULL);\n")}))
    _assert("walk: schema created", created["failed_count"] == 0 and created["committed"] is True)
    migrated = _on(source, lambda c: script_actions.run_script(c, {"sql": ("ALTER TABLE epg_walk.customer ADD COLUMN email text;\nCREATE INDEX customer_email_idx ON epg_walk.customer (email);\nCREATE FUNCTION epg_walk.digits(t text) RETURNS text LANGUAGE sql AS $$ SELECT regexp_replace(t, '\\D', '', 'g') $$;\n")}))
    _assert("walk: migration applied", migrated["statement_count"] == 3 and migrated["failed_count"] == 0)

    csv_path = workspace / "customers.csv"
    csv_path.write_text("id,name,email\n1,Alice,alice@example.com\n2,Grace,grace@example.com\n3,Ed\\sger,\n", encoding="utf-8")
    loaded = _on(
        source,
        lambda c: copy_actions.run_copy(
            c,
            {
                "operation": "from_file",
                "path": str(csv_path),
                "schema": "epg_walk",
                "table": "customer",
                "columns": ["id", "name", "email"],
                "format": "csv",
                "header": True,
            },
            _file_gate,
        ),
    )
    print(f"    observed copy from_file = {loaded}")
    _assert("walk: COPY loaded 3 rows", loaded["rowcount"] == 3)
    _assert("walk: rows committed", _walk_scalar("SELECT count(*) FROM epg_walk.customer", _SCRATCH_DB) == "3")
    exported = _on(
        source,
        lambda c: copy_actions.run_copy(
            c,
            {
                "operation": "to_file",
                "path": str(workspace / "export.csv"),
                "query": "SELECT id, name FROM epg_walk.customer ORDER BY id",
                "format": "csv",
                "header": True,
            },
            _file_gate,
        ),
    )
    _assert("walk: COPY to_file wrote 3 rows", exported["rowcount"] == 3)
    _assert("walk: exported CSV content", (workspace / "export.csv").read_text(encoding="utf-8").splitlines()[1] == "1,Alice")

    _walk_dump_restore(source, target, settings, workspace)
    _walk_admin(source)


def _walk_dump_restore(source: ExternalDsn, target: ExternalDsn, settings: client_tools.ToolSettings, workspace: Path) -> None:
    """Dump custom and restore it into the second database; plain SQL restores are refused."""
    dumped = client_tools.dump_database(
        source,
        {"output_path": str(workspace / "walk.dump"), "format": "custom", "schema": ["epg_walk"]},
        _file_gate,
        settings,
    )
    print(f"    observed dump = {dumped}")
    _assert("walk: custom dump written", dumped["bytes"] > 0 and dumped["format"] == "custom")
    restored = client_tools.restore_database(
        target,
        {"input_path": str(workspace / "walk.dump"), "exit_on_error": True, "single_transaction": True},
        _file_gate,
        settings,
    )
    print(f"    observed restore = {restored}")
    _assert("walk: archive restored through pg_restore", restored["tool"] == "pg_restore" and restored["format"] == "custom")
    _assert("walk: restored rows in the second database", _walk_scalar("SELECT count(*) FROM epg_walk.customer", _restore_db()) == "3")
    _assert("walk: restored index", _walk_scalar("SELECT count(*) FROM pg_indexes WHERE indexname = 'customer_email_idx'", _restore_db()) == "1")

    _assert("walk: backslash data survived pg_restore", _walk_scalar("SELECT name FROM epg_walk.customer WHERE id = 3", _restore_db()) == "Ed\\sger")
    _assert("walk: dollar-quoted body with a backslash survived", _walk_scalar("SELECT epg_walk.digits('a1b2')", _restore_db()) == "12")

    # A plain-SQL dump is written (pg_dump's default format) but never replayed: restore
    # takes archives only, so no psql meta-command in a plain file can run on this host.
    plain = client_tools.dump_database(source, {"output_path": str(workspace / "walk.sql"), "schema": ["epg_walk"]}, _file_gate, settings)
    _assert("walk: plain dump written", plain["format"] == "plain" and plain["bytes"] > 0)
    marker = workspace / "meta_command_ran"
    evil = workspace / "evil.sql"
    evil.write_text(f"SELECT 1;\n\\! touch {marker}\n", encoding="utf-8")
    for path in (workspace / "walk.sql", evil):
        code = ""
        try:
            client_tools.restore_database(target, {"input_path": str(path)}, _file_gate, settings)
        except client_tools.ClientToolError as exc:
            code = exc.code
        _assert(f"walk: plain SQL restore refused ({path.name})", code == "external_pg.plain_sql_restore_refused", code)
    _assert("walk: the shell meta-command in the refused file never ran", not marker.exists())


def _walk_admin(source: ExternalDsn) -> None:
    """Read activity, locks and statistics; reset one table; VACUUM via autocommit."""
    activity = _on(source, lambda c: admin_actions.run_activity(c, {"operation": "list", "database": _SCRATCH_DB, "limit": 1}))
    _assert("walk: activity lists this session", activity["returned"] == 1 and "pid" in activity["rows"][0])
    locks = _on(source, lambda c: admin_actions.run_activity(c, {"operation": "locks", "granted": True}))
    _assert("walk: locks readable", locks["returned"] >= 1 and "blocked_by" in locks["rows"][0])
    cancelled = _on(source, lambda c: admin_actions.run_activity(c, {"operation": "cancel", "pid": 1}))
    _assert("walk: cancel of a non-backend pid is not signalled", cancelled["signalled"] is False)
    sizes = _on(source, lambda c: admin_actions.run_statistics(c, {"operation": "relation_sizes", "schema": "epg_walk"}))
    _assert("walk: relation size of the loaded table", sizes["rows"][0]["relation"] == "customer" and sizes["rows"][0]["total_bytes"] > 0)
    databases = _on(source, lambda c: admin_actions.run_statistics(c, {"operation": "database_sizes"}))
    _assert("walk: database sizes include the scratch database", any(r["datname"] == _SCRATCH_DB for r in databases["rows"]))
    reset = _on(source, lambda c: admin_actions.run_statistics(c, {"operation": "reset_table", "schema": "epg_walk", "table": "customer"}))
    _assert("walk: one table's counters reset", reset["reset"] is True)
    vacuumed = _on(
        source,
        lambda c: query_actions.run_statement(
            c,
            {"sql": "VACUUM (ANALYZE) epg_walk.customer", "autocommit": True},
            _passthrough_gate,
        ),
    )
    _assert("walk: VACUUM runs through run_statement autocommit", vacuumed == {"rowcount": -1, "has_result_set": False, "autocommit": True})
    _assert(
        "walk: the VACUUM's ANALYZE is recorded",
        _walk_scalar("SELECT last_analyze IS NOT NULL FROM pg_stat_user_tables WHERE relname = 'customer'", _SCRATCH_DB) == "t",
    )


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
    print("\nexternal_postgres_plugin LIVE write + build-1 acceptance smoke")
    print("=" * 62)
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
    print("All LIVE write and acceptance-walk checks passed.")
    print("PRIVATE_PG_BODY external_write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
