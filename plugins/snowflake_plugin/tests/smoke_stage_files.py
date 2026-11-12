"""Offline connector-shaped stage transcripts; no Snowflake or platform DB.

PUT/GET result columns follow connector/file_transfer_agent.py; COPY results
follow the SQL reference. The fake executes file movement from the actual SQL
operands, so containment and byte equality exercise the production path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import NonCallableMagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta/src"))
import csv  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from collections.abc import Iterator  # noqa: E402
from datetime import UTC, datetime  # noqa: E402
from decimal import Decimal  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Any  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

from ananta.file_locations.containment import PathRefusedError, assert_path_allowed, derive_allowed_roots  # noqa: E402
from ananta.file_locations.resolution import PathAddress, SoletTree  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/snowflake_plugin/src"))

from snowflake_plugin import async_jobs, completion_templates, connection, stage_files, stage_paths, stage_sql  # noqa: E402
from snowflake_plugin.plugin import SnowflakePlugin  # noqa: E402

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


def arguments(operation: str, **extra: Any) -> dict[str, Any]:
    return {"operation": operation, "stage_location": "@DB.PUBLIC.FILES/prefix/", **extra}


class VendorError(Exception):
    def __init__(self, message: str, *, errno: int = 1003, sqlstate: str = "42000") -> None:
        super().__init__(message)
        self.msg = message
        self.errno = errno
        self.sqlstate = sqlstate


class Cursor:
    """Independent tiny stage server, returning real-shaped connector rows."""

    def __init__(self) -> None:
        self.stage: dict[str, bytes] = {}
        self.description: list[tuple[str]] = []
        self.rows: list[tuple[Any, ...]] = []
        self.commands: list[str] = []
        self.sfqid = "01b9a111-0000-1111-0000-000000000001"
        self.failure: Exception | None = None
        self.forced_rows: list[tuple[Any, ...]] | None = None

    def __enter__(self) -> Cursor:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def execute(self, command: str) -> None:
        self.commands.append(command)
        if command.startswith("ALTER SESSION"):
            return
        if self.failure is not None:
            raise self.failure
        literals = [part.replace("''", "'").replace("\\\\", "\\") for part in re.findall(r"'((?:[^']|'')*)'", command)]
        name = command.split()[0]
        handlers = {"PUT": self._put, "GET": self._get, "LIST": self._list, "REMOVE": self._remove}
        if name in handlers:
            columns = handlers[name](command, literals)
        elif command.startswith("COPY FILES"):
            columns = ["file"]
            self.rows = [("ARCHIVE/payload.bin",)]
        else:
            columns = self._copy(command)
        self.description = [(column,) for column in columns]
        if self.forced_rows is not None:
            self.description = [("file" if command.startswith("COPY FILES") else "MESSAGE",)]
            self.rows = self.forced_rows

    def _copy(self, command: str) -> list[str]:
        if "VALIDATION_MODE" in command:
            self.rows = [(1,), (2,)]
            return ["ID"]
        if command.startswith("COPY INTO '"):
            self.rows = [(2, 24, 20)]
            return ["rows_unloaded", "input_bytes", "output_bytes"]
        self.rows = [("prefix/payload.csv", "LOADED", 2, 2, 1, 0, None, None, None, None)]
        return ["file", "status", "rows_parsed", "rows_loaded", "error_limit", "errors_seen", "first_error", "first_error_line", "first_error_character", "first_error_column_name"]

    def _put(self, command: str, literals: list[str]) -> list[str]:
        source = Path(literals[0].removeprefix("file://"))
        status = "UPLOADED" if source.name not in self.stage or "OVERWRITE = TRUE" in command else "SKIPPED"
        if status == "UPLOADED":
            self.stage[source.name] = source.read_bytes()
        self.rows = [(source.name, source.name, source.stat().st_size, len(self.stage[source.name]), "NONE", "NONE", status, "")]
        return ["source", "target", "source_size", "target_size", "source_compression", "target_compression", "status", "message"]

    def _get(self, _command: str, literals: list[str]) -> list[str]:
        destination = Path(literals[1].removeprefix("file://"))
        self.rows = []
        for name, data in self.stage.items():
            (destination / name).write_bytes(data)
            self.rows.append((name, len(data), "DOWNLOADED", ""))
        return ["file", "size", "status", "message"]

    def _list(self, _command: str, _literals: list[str]) -> list[str]:
        self.rows = [("FILES/prefix/" + name, len(data), "18c70b338f19", "Fri, 07 Nov 2026 10:00:00 GMT") for name, data in self.stage.items()]
        return ["name", "size", "md5", "last_modified"]

    def _remove(self, _command: str, _literals: list[str]) -> list[str]:
        self.rows = [("FILES/prefix/" + name, "removed") for name in self.stage]
        self.stage.clear()
        return ["name", "result"]

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def __iter__(self) -> Iterator[tuple[Any, ...]]:
        return iter(self.rows)


class Fixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.allowed = self.directory / "workspace"
        self.allowed.mkdir()
        self.plugin = SnowflakePlugin()
        self.plugin._config = MagicMock(return_value={})
        _bind_locations(self.plugin, [str(self.allowed)])  # type: ignore[method-assign]
        loader = MagicMock()
        loader.resolve.return_value.private_key_der = b"fixture-private-key"
        self.plugin._app_config_loader = loader
        self.cursor = Cursor()
        self.conn = MagicMock()
        self.conn.cursor.return_value = self.cursor
        self.connect = self.enterContext(patch.object(connection, "connect", return_value=self.conn))

    def run_operation(self, operation: str, **params: Any) -> dict[str, Any]:
        outcome = stage_files.run_stage_files(self.plugin, arguments(operation, **params))
        self.assertEqual(outcome["action_status"], "completed", outcome)
        return outcome["data"]


class Lifecycle(Fixture):
    def test_upload_download_correct_remove_recreate_with_byte_equality(self) -> None:
        source = self.allowed / "payload.bin"
        source.write_bytes(bytes(range(256)) + b"\x00\xff\n")
        download = self.allowed / "downloads"
        download.mkdir()
        put = self.run_operation("put", local_path=str(source), options={"AUTO_COMPRESS": False, "SOURCE_COMPRESSION": "NONE", "PARALLEL": 1, "OVERWRITE": True})
        self.assertEqual(put["results"][0]["rows"][0]["status"], "UPLOADED")
        get = self.run_operation("get", local_path=str(download), options={"PARALLEL": 99, "PATTERN": ".*bin"})
        self.assertEqual(Path(get["paths"][0]).read_bytes(), source.read_bytes())
        source.write_bytes(b"corrected")
        skipped = self.run_operation("put", local_path=str(source), options={"OVERWRITE": False})
        self.assertEqual(skipped["results"][0]["rows"][0]["status"], "SKIPPED")
        self.run_operation("put", local_path=str(source), options={"OVERWRITE": True})
        self.run_operation("get", local_path=str(download))
        self.assertEqual((download / source.name).read_bytes(), b"corrected")
        self.assertEqual(self.run_operation("list", options={"PATTERN": ".*"})["row_count"], 1)
        self.assertEqual(self.run_operation("remove", options={"PATTERN": ".*"})["rows"][0]["result"], "removed")
        self.assertEqual(self.run_operation("list")["rows"], [])
        self.run_operation("put", local_path=str(source))
        self.assertEqual(self.cursor.stage[source.name], b"corrected")
        self.assertEqual(self.conn.close.call_count, self.connect.call_count)

    def test_wildcards_are_materialized_and_complete_listing_is_not_truncated(self) -> None:
        for name in ("a.csv", "b.csv"):
            (self.allowed / name).write_text(name)
        result = self.run_operation("put", local_path=str(self.allowed / "?.csv"))
        self.assertEqual(len(result["results"]), 2)
        self.assertFalse(any("?.csv" in command for command in self.cursor.commands))
        self.assertEqual(len(self.run_operation("list")["rows"]), 2)

    def test_get_replaces_destination_symlink_without_writing_its_target(self) -> None:
        outside = self.directory / "outside"
        outside.write_bytes(b"protected")
        (self.allowed / "payload.bin").symlink_to(outside)
        self.cursor.stage["payload.bin"] = b"downloaded"
        self.run_operation("get", local_path=str(self.allowed))
        self.assertEqual(outside.read_bytes(), b"protected")
        self.assertFalse((self.allowed / "payload.bin").is_symlink())
        self.assertEqual((self.allowed / "payload.bin").read_bytes(), b"downloaded")

    def test_refuse_escapes_and_malformed_roots_before_connect(self) -> None:
        outside = self.directory / "outside.csv"
        outside.write_bytes(b"private")
        (self.allowed / "link.csv").symlink_to(outside)
        for operation, path in [("put", "relative.csv"), ("put", str(self.allowed / ".." / "outside.csv")), ("put", str(self.allowed / "*.csv")), ("get", str(self.directory)), ("get", str(outside))]:
            with self.subTest(operation=operation, path=path):
                outcome = stage_files.run_stage_files(self.plugin, arguments(operation, local_path=path))
                self.assertEqual(outcome["action_status"], "error")
                self.connect.assert_not_called()
        for roots in ([], ["relative"]):
            with self.assertRaises(PathRefusedError):
                stage_paths.contained(str(self.allowed), lambda path, roots=roots: _checked_path(path, roots, suffix=None))
        with self.assertRaises(ValueError):
            stage_paths.upload_paths(str(self.allowed / "absent*"), self.plugin._local_path_gate)

    def test_download_publication_refuses_symlink_from_vendor(self) -> None:
        scratch = self.allowed / "scratch"
        scratch.mkdir()
        (scratch / "bad").symlink_to(self.directory)
        with self.assertRaises(PathRefusedError):
            stage_paths.publish_downloads(scratch, self.allowed)

    def test_failed_file_status_is_visible_even_when_job_completed(self) -> None:
        self.cursor.forced_rows = [("failure detail",)]
        # Exercise a real-shaped GET per-file error separately from exceptions.
        self.cursor.stage["file"] = b"bytes"
        original = self.cursor.execute

        def fail_file(command: str) -> None:
            original(command)
            self.cursor.description = [("file",), ("size",), ("status",), ("message",)]
            self.cursor.rows = [("file", 0, "ERROR", "download failed")]

        with patch.object(self.cursor, "execute", side_effect=fail_file):
            result = self.run_operation("get", local_path=str(self.allowed))
        self.assertFalse(result["vendor_success"])
        self.assertEqual(result["rows"][0]["status"], "ERROR")


class ReturnedRows(Fixture):
    def test_vendor_status_rows_are_returned_as_received(self) -> None:
        original = self.cursor.execute

        def echo(command: str) -> None:
            original(command)
            self.cursor.description = [("file",), ("size",), ("status",), ("nested",)]
            self.cursor.rows = [("payload.bin", Decimal("123.456"), "DOWNLOADED", {"echo": [1]})]

        with patch.object(self.cursor, "execute", side_effect=echo):
            result = self.run_operation("list")
        self.assertEqual(result["rows"][0], {"file": "payload.bin", "size": "123.456", "status": "DOWNLOADED", "nested": {"echo": [1]}})
        for status, success in (("UPLOADED", True), ("ERROR", False)):
            self.cursor.rows = [(status,)]
            result = stage_files._metadata_result(self.cursor, ["status"], "qid")
            self.assertIs(result["vendor_success"], success)


class CopyAndErrors(Fixture):
    def test_native_validation_cells_are_normalized_in_the_tsv(self) -> None:
        output = self.allowed / "native.tsv"
        moment = datetime(2026, 11, 7, 12, 30, 1)
        decimal = Decimal("123.456")
        self.cursor.forced_rows = [(decimal, moment, None, 'tab\tquote"\nline', b"\x00\xff")]
        self.run_operation("copy_into_table", table="T", output_tsv_path=str(output))
        with output.open(newline="") as stream:
            self.assertEqual(list(csv.reader(stream, delimiter="\t"))[1], [str(decimal), str(moment), "", 'tab\tquote"\nline', "hex:00ff"])

    def test_copy_load_unload_and_validation_always_write_tsv(self) -> None:
        for operation, extra in [("copy_into_table", {"table": "DB.PUBLIC.DATA", "columns": ["ID", '"Mixed Case"']}), ("copy_into_location", {"query": "SELECT ID FROM DB.PUBLIC.DATA", "partition_by": "TO_VARCHAR(CURRENT_DATE())"})]:
            path = self.allowed / f"{operation}.tsv"
            result = self.run_operation(operation, output_tsv_path=str(path), options={"FILE_FORMAT": {"TYPE": "CSV", "FIELD_DELIMITER": ","}}, **extra)
            self.assertNotIn("rows", result)
            self.assertEqual(result["row_count"], 1)
            self.assertIn("LOADED" if operation == "copy_into_table" else "rows_unloaded", path.read_text())
        validation = self.allowed / "validation.tsv"
        result = self.run_operation("copy_into_table", table="T", output_tsv_path=str(validation), options={"VALIDATION_MODE": "RETURN_2_ROWS"})
        self.assertNotIn("rows", result)
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(validation.read_text(), "ID\n1\n2\n")
        result = self.run_operation("copy_files", source_location="@DB.PUBLIC.OTHER/", options={"FILES": ["payload.bin"], "PATTERN": ".*", "DETAILED_OUTPUT": True})
        self.assertEqual(result["rows"], [{"file": "ARCHIVE/payload.bin"}])
        outcome = stage_files.run_stage_files(self.plugin, arguments("copy_into_table", table="DATA"))
        self.assertEqual(outcome["action_status"], "error")

    def test_vendor_auth_permission_and_syntax_errors_are_typed_and_closed(self) -> None:
        for errno, state, code in [(390144, "08001", "snowflake.auth_failed"), (2036, "42501", "snowflake.permission_denied"), (1003, "42000", "snowflake.api_error")]:
            self.cursor.failure = VendorError("SQL compilation error: malformed COPY", errno=errno, sqlstate=state)
            outcome = stage_files.run_stage_files(self.plugin, arguments("remove"))
            self.assertEqual(outcome["error"]["code"], code)
        self.assertEqual(self.conn.close.call_count, 3)


class SqlContract(unittest.TestCase):
    def test_copy_locations_cover_documented_protocols_and_stage_forms(self) -> None:
        locations = ["@DB.PUBLIC.FILES/path/", "@%TABLE/path/", "@~/path/"]
        locations += [protocol + "://bucket/path/" for protocol in ("s3", "s3china", "s3gov", "gcs", "azure")]
        for operation in ("copy_into_table", "copy_into_location"):
            for location in locations:
                with self.subTest(operation=operation, location=location):
                    command = stage_sql.build_command(arguments(operation, stage_location=location, table="T"))
                    self.assertIn(stage_sql.literal(location), command)
        command = stage_sql.build_command(arguments("copy_files", stage_location="@DB.PUBLIC.TARGET/", source_location="@DB.PUBLIC.SOURCE/"))
        self.assertIn("INTO '@DB.PUBLIC.TARGET/' FROM '@DB.PUBLIC.SOURCE/'", command)
        for url in ("@SOURCE/file.txt", "https://account.snowflakecomputing.com/api/files/query/file"):
            query = "SELECT " + stage_sql.literal(url)
            command = stage_sql.build_command(arguments("copy_files", query=query))
            self.assertIn("FROM (" + query + ")", command)

    def test_all_transfer_options_and_sql_string_escaping(self) -> None:
        for compression in ("AUTO_DETECT", "GZIP", "BZ2", "BROTLI", "ZSTD", "DEFLATE", "RAW_DEFLATE", "NONE"):
            sql = stage_sql.build_command(arguments("put", local_path="/tmp/file's name.csv", options={"PARALLEL": 99, "AUTO_COMPRESS": True, "SOURCE_COMPRESSION": compression, "OVERWRITE": False}))
            self.assertIn("file''s name.csv", sql)
            self.assertIn(f"SOURCE_COMPRESSION = '{compression}'", sql)
        self.assertEqual(stage_sql.literal("a\\b'c"), "'a\\\\b''c'")
        for operation, options in [("put", {"PARALLEL": 0}), ("put", {"PARALLEL": True}), ("put", {"OVERWRITE": "TRUE"}), ("put", {"SOURCE_COMPRESSION": "FAKE"}), ("get", {"AUTO_COMPRESS": True}), ("get", {"PATTERN": 1}), ("list", {"PATTERN": []}), ("remove", {"OVERWRITE": True})]:
            with self.subTest(operation=operation, options=options), self.assertRaises(ValueError):
                stage_sql.build_command(arguments(operation, local_path="/tmp/file", options=options))

    def test_copy_accepts_all_vendor_option_families_without_name_allowlist(self) -> None:
        # Every load/unload top-level option in the current references; nested
        # format/auth options use the same unrestricted value contract.
        names = "ON_ERROR SIZE_LIMIT PURGE RETURN_FAILED_ONLY MATCH_BY_COLUMN_NAME ENFORCE_LENGTH TRUNCATECOLUMNS FORCE LOAD_UNCERTAIN_FILES CLUSTER_AT_INGEST_TIME INCLUDE_METADATA LOAD_MODE ADD_FILES_COPY_BY_NAME OVERWRITE SINGLE MAX_FILE_SIZE INCLUDE_QUERY_ID DETAILED_OUTPUT HEADER FILES PATTERN FILE_FORMAT COPY_OPTIONS VALIDATION_MODE".split()
        for name in names:
            sql = stage_sql.build_command(arguments("copy_into_table", table="DB.PUBLIC.DATA", options={name.lower(): "vendor-value"}))
            self.assertIn(f"{name} = 'vendor-value'", sql)
        sql = stage_sql.build_command(
            arguments(
                "copy_into_location",
                table="T",
                stage_location="s3://bucket/path/",
                options={
                    "STORAGE_INTEGRATION": {"sql": '"My integration"'},
                    "CREDENTIALS": {"AWS_KEY_ID": "id", "AWS_SECRET_KEY": "secret", "AWS_TOKEN": "token"},
                    "ENCRYPTION": {"TYPE": "AWS_SSE_KMS", "KMS_KEY_ID": "id"},
                    "FILE_FORMAT": {"TYPE": "CSV", "NULL_IF": ["NULL", ""], "SKIP_HEADER": 1, "TRIM_SPACE": True},
                    "INCLUDE_METADATA": {"sql": "(filename = METADATA$FILENAME, rownum = METADATA$FILE_ROW_NUMBER)"},
                },
            )
        )
        self.assertLess(sql.index("CREDENTIALS"), sql.index(" FROM "))
        self.assertIn("NULL_IF = ('NULL', '')", sql)
        self.assertIn("INCLUDE_METADATA = (filename = METADATA$FILENAME, rownum = METADATA$FILE_ROW_NUMBER)", sql)
        transformed = stage_sql.build_command(arguments("copy_into_table", table="T", query="SELECT $1 FROM @S", options={"FORCE": True}))
        self.assertIn("FROM (SELECT $1 FROM @S)", transformed)
        queried = stage_sql.build_command(arguments("copy_files", query="SELECT 's3://bucket/a', 'b'"))
        self.assertIn("FROM (SELECT 's3://bucket/a', 'b')", queried)


class Registration(Fixture):
    def test_three_artifacts_async_dispatch_worker_and_completion_contract(self) -> None:
        from ananta.core.plugins.action_discovery import discover_actions

        actions = {action.name: action for action in discover_actions(self.plugin)}
        self.assertIn("stage_files", actions)
        self.assertIn("options", actions["stage_files"].parameters)
        edges = self.plugin.get_edge_process_definitions()
        doc = json.loads((ROOT / "plugins/snowflake_plugin/knowledge_base/processes/stage_files.json").read_text())
        self.assertEqual(doc["process_key"], "plugin::snowflake_plugin::stage_files")
        self.assertEqual(edges["stage_files"].result_processor_template_customizations.result_type, "snowflake_stage_files_result")  # type: ignore[union-attr]
        self.assertTrue(200 <= len(doc["embedding_description"]) <= 400)
        params = arguments("list")
        with patch.object(async_jobs, "create_job", return_value={"action_status": "completed", "data": {"job_id": "job", "status": "queued"}}) as create:
            result = self.plugin.stage_files(params, {"session_id": "s", "flow_id": "f"})
        self.assertEqual(result["data"]["status"], "queued")
        self.connect.assert_not_called()
        self.assertEqual(create.call_args.kwargs["action_name"], "stage_files")
        manager = MagicMock()
        manager.get_job_payload.return_value = {"action_status": "completed", "data": {"payload": params}}
        async_jobs._process_job(self.plugin, manager, "job", "stage_files")
        self.assertEqual(manager.update_job.call_args.args[1]["status"], "completed")
        self.assertEqual(manager.update_job.call_args.args[1]["result"]["operation"], "list")
        handlers = completion_templates.build_completion_handlers("stage_files")
        self.assertIn("partial effects", json.dumps(handlers))
        self.assertEqual(self.plugin.stage_files(arguments("invalid"), {})["error"]["code"], "snowflake.invalid_params")


if __name__ == "__main__":
    unittest.main()
