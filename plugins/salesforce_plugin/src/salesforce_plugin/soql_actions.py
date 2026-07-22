"""SOQL query verb — a pure function over a ``SalesforceCliExecutor``.

This module is S2-exempt (whole-file): SOQL strings like
``SELECT Id, Name FROM Account`` match the gate's S2 verb+FROM heuristic even
though they target the FOREIGN Salesforce org (containment invariant #1 — no
org/domain parameter on any verb; the client is built solely from the
"salesforce_org" address-book entry). See
knowledge_base/01_salesforce_overview.md §3 for the full gate-treatment note.

The query is written to a tempfile and run via `sf data query --file`
(file-based — the operator's proven work-script pattern, and it keeps SOQL
text, which can carry business-sensitive filter values, out of the process
argv `ps` would otherwise expose).

No manual pagination: `sf data query` runs on jsforce with `autoFetch: true`
internally (verified by reading the CLI's own source,
``@salesforce/plugin-data/lib/commands/data/query.js``) — it already collects
every page up to a fetch cap before returning. This module passes
``SF_ORG_MAX_QUERY_LIMIT`` as a subprocess env override so the CLI stops
fetching at ``max_records`` server-side (never the unbounded default), then
slices the returned list to ``max_records`` again client-side as
defense-in-depth.
"""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections.abc import Callable
from typing import Any

from .client import SalesforceCliExecutor
from .constants import (
    ERROR_RESULT_TOO_LARGE,
    INLINE_BYTE_CAP,
    SOQL_DEFAULT_MAX_RECORDS,
    SOQL_EXPORT_ROW_CAP,
    SOQL_MAX_RECORDS_CAP,
)

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]


class ResultTooLargeError(RuntimeError):
    """An interactive read overflowed the inline caps (A4: fail loud, no spill)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_RESULT_TOO_LARGE


def fetch_org_id(executor: SalesforceCliExecutor) -> str:
    """Fetch the org's own Id via a minimal query — used by test_connection.

    Kept here (not in plugin.py) so this module stays the ONE place composing
    SOQL-shaped literal strings — the S2 gate exemption boundary (see
    knowledge_base/01_salesforce_overview.md §3).
    """
    result = _run_soql(executor, "SELECT Id FROM Organization LIMIT 1", max_records=1)
    records = result.get("records") if isinstance(result, dict) else None
    if isinstance(records, list) and records and isinstance(records[0], dict):
        return str(records[0].get("Id") or "")
    return ""


def soql_query(executor: SalesforceCliExecutor, params: dict[str, Any]) -> dict[str, Any]:
    """Run a SOQL query, capped at max_records (server-side env cap + client-side slice)."""
    query = _require_str(params, "query")
    max_records = _clamp(params.get("max_records"), SOQL_DEFAULT_MAX_RECORDS, SOQL_MAX_RECORDS_CAP)
    result = _run_soql(executor, query, max_records)
    records = _strip_attributes(result.get("records") or [] if isinstance(result, dict) else [])
    total_size = _as_int(result.get("totalSize") if isinstance(result, dict) else None, default=len(records))
    records = records[:max_records]
    return _query_envelope(records, total_size)


def export_soql(
    executor: SalesforceCliExecutor, params: dict[str, Any], path_gate: PathGate
) -> dict[str, Any]:
    """Export a SOQL query's full result (up to SOQL_EXPORT_ROW_CAP) as a workspace TSV.

    Mirrors the A2 contract on external_postgres/snowflake: the full result
    set lands as ONE tab-separated file at the caller's absolute
    ``output_tsv_path`` — admitted by the injected containment gate, never
    platform blob storage. The gate runs BEFORE the query is sent.
    """
    query = _require_str(params, "query")
    output_tsv_path = _require_str(params, "output_tsv_path")
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
            "create it first — this verb writes one file, it does not create directories"
        )
    result = _run_soql(executor, query, SOQL_EXPORT_ROW_CAP)
    records = _strip_attributes(result.get("records") or [] if isinstance(result, dict) else [])
    total_size = _as_int(result.get("totalSize") if isinstance(result, dict) else None, default=len(records))
    # Salesforce's own incompleteness signal: done=false means more data
    # remains server-side even when totalSize equals the fetched count
    # (Codex Wave-3 review, A3-1) — never report such an export as complete.
    done_false = isinstance(result, dict) and result.get("done") is False
    columns = _ordered_columns(records)
    row_lists = [[_cell_value(record.get(column)) for column in columns] for record in records]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    return {
        "path": resolved_path,
        "columns": columns,
        "row_count": len(row_lists),
        "total_size": total_size,
        "truncated": (
            done_false
            or len(row_lists) >= SOQL_EXPORT_ROW_CAP
            or total_size > len(row_lists)
        ),
    }


def _run_soql(executor: SalesforceCliExecutor, query: str, max_records: int) -> Any:
    fd, path = tempfile.mkstemp(suffix=".soql", prefix="salesforce_plugin_query_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(query)
        return executor.run_json(
            ["data", "query", "--file", path],
            env_overrides={"SF_ORG_MAX_QUERY_LIMIT": str(max_records)},
        )
    finally:
        os.unlink(path)


def _query_envelope(
    records: list[dict[str, Any]],
    total_size: int,
) -> dict[str, Any]:
    """Return records inline; FAIL LOUD over the byte cap (A4 — no blob spill)."""
    payload = json.dumps(records, default=str).encode("utf-8")
    if len(payload) > INLINE_BYTE_CAP:
        raise ResultTooLargeError(
            f"the result is too large to return inline ({len(payload)} bytes > "
            f"{INLINE_BYTE_CAP}-byte cap across {len(records)} records); narrow the "
            "query (add a WHERE/LIMIT or lower max_records) for an interactive "
            "answer, or use export_soql with an absolute output_tsv_path to land "
            "the full result set as a workspace TSV file"
        )
    return {"records": records, "total_size": total_size, "row_count": len(records), "spilled": False}


def _ordered_columns(records: list[dict[str, Any]]) -> list[str]:
    """Union of record keys in first-appearance order (SOQL SELECT order)."""
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    return columns


def _cell_value(value: Any) -> Any:
    """Coerce a record value to a TSV-safe form; nested objects become JSON text."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return json.dumps(value, default=str, ensure_ascii=False)


def _to_tsv(columns: list[str], row_lists: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(row_lists)
    return buffer.getvalue().encode("utf-8")


def _strip_attributes(records: list[Any]) -> list[dict[str, Any]]:
    """Drop Salesforce's injected ``attributes`` metadata key from each record."""
    out: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        out.append({k: v for k, v in record.items() if k != "attributes"})
    return out


def _as_int(value: Any, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _clamp(value: Any, default: int, cap: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(1, min(cap, value))
