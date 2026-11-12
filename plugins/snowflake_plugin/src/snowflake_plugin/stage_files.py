"""Typed stage-file execution on the existing D0.3 worker, never dispatch I/O.

Transfer/list/remove results are vendor file-status rows, not business rows.
COPY INTO results (including RETURN_ROWS validation) always go to a workspace
TSV. No command is retried and a failure does not undo already moved data.
"""

from __future__ import annotations

import csv
import logging
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from . import connection, stage_paths, stage_sql
from .constants import ERROR_NOT_CONFIGURED
from .outward_values import normalize_key, normalize_outward, render

if TYPE_CHECKING:
    from .plugin import SnowflakePlugin


def _roots(plugin: SnowflakePlugin) -> Callable[[str], str]:
    return plugin._local_path_gate


def _execute(conn: Any, params: dict[str, Any], *, local_path: str | None = None, output: str | None = None) -> dict[str, Any]:
    with conn.cursor() as cursor:
        cursor.execute(stage_sql.build_command(params, local_path=local_path))
        columns = [column[0] for column in (cursor.description or [])]
        query_id = getattr(cursor, "sfqid", None)
        if output is not None:
            return _write_copy(cursor, columns, query_id, output)
        return _metadata_result(cursor, columns, query_id)


def _copy_cell(value: Any) -> str:
    return "" if value is None else render(value)


def _write_copy(cursor: Any, columns: list[Any], query_id: Any, output: str) -> dict[str, Any]:
    count = 0
    with Path(output).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, delimiter="\t", lineterminator="\n")
        headers = [normalize_key(column) for column in columns]
        writer.writerow(headers)
        for row in cursor:
            writer.writerow([_copy_cell(value) for value in row])
            count += 1
    return normalize_outward({"query_id": query_id, "columns": headers, "path": output, "row_count": count})


def _metadata_result(cursor: Any, columns: list[Any], query_id: Any) -> dict[str, Any]:
    rows = [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]
    statuses = [str(row[key]).upper() for row in rows for key in row if str(key).lower() == "status"]
    success = all(status in {"UPLOADED", "DOWNLOADED", "SKIPPED", "LOADED"} for status in statuses) if statuses else None
    return normalize_outward({"query_id": query_id, "rows": rows, "row_count": len(rows), "vendor_success": success})


def _transfer(conn: Any, params: dict[str, Any], roots: Callable[[str], str]) -> dict[str, Any]:
    path = stage_sql.text(params, "local_path")
    if params["operation"] == "put":
        files = stage_paths.upload_paths(path, roots)
        return {"results": [_execute(conn, params, local_path=str(file)) for file in files]}
    destination = stage_paths.download_directory(path, roots)
    # The connector never sees the final destination: even a pre-existing
    # symlink there cannot redirect its file writes outside the admitted root.
    with TemporaryDirectory(prefix=".snowflake-get-", dir=destination) as temporary:
        result = _execute(conn, params, local_path=temporary)
        result["paths"] = normalize_outward(stage_paths.publish_downloads(Path(temporary), destination))
    return result


def _prepare(plugin: SnowflakePlugin, params: dict[str, Any]) -> tuple[str, Callable[[str], str], str | None]:
    operation = params["operation"]
    roots = _roots(plugin)
    if operation == "put":
        stage_paths.upload_paths(stage_sql.text(params, "local_path"), roots)
    elif operation == "get":
        stage_paths.download_directory(stage_sql.text(params, "local_path"), roots)
    output = plugin._export_path_gate(stage_sql.text(params, "output_tsv_path")) if operation in {"copy_into_table", "copy_into_location"} else None
    if output is not None and not Path(output).parent.is_dir():
        raise ValueError("output_tsv_path parent must exist")
    return operation, roots, output


def run_stage_files(plugin: SnowflakePlugin, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve one account and perform exactly one caller-requested operation."""
    conn: Any = None
    try:
        stage_sql.build_command(params)
        if plugin._app_config_loader is None:
            return plugin._error(ERROR_NOT_CONFIGURED, "snowflake_plugin is not ready")
        operation, roots, output = _prepare(plugin, params)
        config = plugin._app_config_loader.resolve()
        conn = connection.connect(config, login_timeout_seconds=plugin._login_timeout_seconds())
        connection.apply_session_hardening(conn, statement_timeout_seconds=plugin._statement_timeout_seconds())
        if operation in {"put", "get"}:
            data = _transfer(conn, params, roots)
        else:
            data = _execute(conn, params, output=output)
        return plugin._success({"operation": render(operation), **data})
    except Exception as exc:
        code, message = plugin._classify_run_error(exc, "stage_files")
        return plugin._error(code, render(exc if isinstance(exc, ValueError) else message))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                logging.getLogger(__name__).warning("Snowflake stage connection close failed; operation outcome retained")
