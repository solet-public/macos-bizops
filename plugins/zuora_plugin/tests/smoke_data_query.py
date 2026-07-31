#!/usr/bin/env python3
"""Data Query + bulk export smoke tests for zuora_plugin.

Hermetic — a faked client with ``post``/``get`` returning canned
``httpx.Response`` objects, no live tenant.

Exercises:
  1. data_query — inline records + row_count/spilled shape
  2. data_query — max_rows clamped to the 1000 hard cap
  3. data_query spills to a blob when the result exceeds the byte cap
  4. bulk_export — csv default, row_count/format/truncated shape
  5. bulk_export — json format
  6. bulk_export — rejects an unsupported format
  7. bulk_export flags truncated at the row cap
  8. A non-2xx response raises ZuoraResponseError carrying is_query correctly

Run:
    HOMUNCULUS_NAME=<name> .venv/bin/python3 \
        plugins/zuora_plugin/tests/smoke_data_query.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "zuora_plugin" / "src"))

from zuora_plugin import billing_actions  # noqa: E402
from zuora_plugin.billing_actions import ZuoraResponseError  # noqa: E402
from zuora_plugin.constants import BULK_EXPORT_ROW_CAP, INLINE_BYTE_CAP  # noqa: E402
from zuora_plugin.errors import ZuoraServiceError  # noqa: E402
from zuora_plugin.plugin import ZuoraPlugin  # noqa: E402

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


def _fake_client(records: list[dict[str, Any]], *, status: int = 200) -> Any:
    response = httpx.Response(status, json={"records": records}, request=httpx.Request("POST", "https://fake/v1/query"))
    client = MagicMock()
    client.post.return_value = response
    return client


def _fake_blob_writer() -> tuple[Any, list[tuple[bytes, str, str]]]:
    calls: list[tuple[bytes, str, str]] = []

    def _writer(content: bytes, filename: str, mime: str) -> str:
        calls.append((content, filename, mime))
        return f"blob-{len(calls)}"

    return _writer, calls


def _no_blob(content: bytes, filename: str, mime: str) -> str:  # pragma: no cover
    raise AssertionError("blob writer should not be called for an inline result")


def test_data_query_inline() -> None:
    client = _fake_client([{"Id": "1", "Name": "A"}, {"Id": "2", "Name": "B"}])
    result = billing_actions.data_query(client, {"zoql": "SELECT Id, Name FROM Account"}, _no_blob)
    _assert("records carried", result["records"] == [{"Id": "1", "Name": "A"}, {"Id": "2", "Name": "B"}])
    _assert("row_count matches", result["row_count"] == 2)
    _assert("not spilled", result["spilled"] is False)


def test_data_query_max_rows_clamp() -> None:
    records = [{"Id": str(i)} for i in range(10)]
    client = _fake_client(records)
    result = billing_actions.data_query(client, {"zoql": "SELECT Id FROM Account", "max_rows": 3}, _no_blob)
    _assert("max_rows clamps the returned set", result["row_count"] == 3)


def test_data_query_spills_over_byte_cap() -> None:
    big_value = "x" * 5000
    records = [{"Id": str(i), "Blob": big_value} for i in range(100)]
    client = _fake_client(records)
    writer, calls = _fake_blob_writer()
    result = billing_actions.data_query(client, {"zoql": "SELECT Id, Blob FROM Account", "max_rows": 1000}, writer)
    _assert("spilled True over byte cap", result["spilled"] is True)
    _assert("result_blob_key present", "result_blob_key" in result)
    _assert("blob writer called once", len(calls) == 1)


def test_bulk_export_csv_default() -> None:
    client = _fake_client([{"Id": "1", "Name": "A"}])
    writer, calls = _fake_blob_writer()
    result = billing_actions.bulk_export(client, {"zoql": "SELECT Id, Name FROM Account"}, writer)
    _assert("export defaults to csv", result["format"] == "csv")
    _assert("export row_count", result["row_count"] == 1)
    _assert("export not truncated", result["truncated"] is False)
    content, _filename, mime = calls[0]
    _assert("csv mime type", mime == "text/csv")
    _assert("csv content has header", "Id" in content.decode("utf-8").splitlines()[0])


def test_bulk_export_json_format() -> None:
    client = _fake_client([{"Id": "1"}, {"Id": "2"}])
    writer, calls = _fake_blob_writer()
    result = billing_actions.bulk_export(client, {"zoql": "SELECT Id FROM Account", "format": "json"}, writer)
    _assert("export json format", result["format"] == "json")
    _, _, mime = calls[0]
    _assert("json mime type", mime == "application/json")


def test_bulk_export_rejects_bad_format() -> None:
    client = _fake_client([{"Id": "1"}])
    writer, _ = _fake_blob_writer()
    raised = False
    try:
        billing_actions.bulk_export(client, {"zoql": "SELECT Id FROM Account", "format": "xml"}, writer)
    except ValueError:
        raised = True
    _assert("unsupported format rejected", raised)


def test_bulk_export_flags_truncation() -> None:
    records = [{"Id": str(i)} for i in range(BULK_EXPORT_ROW_CAP)]
    client = _fake_client(records)
    writer, _ = _fake_blob_writer()
    result = billing_actions.bulk_export(client, {"zoql": "SELECT Id FROM Account"}, writer)
    _assert("truncated flagged at the export row cap", result["truncated"] is True)


def test_non_2xx_raises_with_is_query_flag() -> None:
    client = _fake_client([], status=400)
    raised: ZuoraResponseError | None = None
    try:
        billing_actions.data_query(client, {"zoql": "SELECT bogus"}, _no_blob)
    except ZuoraResponseError as exc:
        raised = exc
    _assert("non-2xx raises ZuoraResponseError", raised is not None)
    _assert("is_query True for data_query", raised is not None and raised.is_query is True)


# ---------------------------------------------------------------------------
# Blob spill service resolution
# ---------------------------------------------------------------------------


def test_store_blob_resolves_service_at_point_of_use() -> None:
    """§20.1 regression: blob_storage_service is constructed AFTER plugin
    readiness, so readiness-time resolution cached None forever and every
    spill hard-failed; the fix resolves lazily at first use."""
    plugin = ZuoraPlugin()
    blob_service = MagicMock()
    blob_service.store_blob.return_value = {
        "action_status": "completed",
        "data": {"blob_id": "blob-dq-1"},
    }
    orch = MagicMock()
    orch.get_service.return_value = blob_service
    plugin.orchestrator_ref = orch
    blob_id = plugin._store_blob(b"x" * 64, "data_query_results.json", "application/json")
    _assert("spill succeeds via point-of-use resolution", blob_id == "blob-dq-1")
    plugin._store_blob(b"y", "again.json", "application/json")
    _assert(
        "one get_service call across two spills (cached)",
        orch.get_service.call_count == 1,
        str(orch.get_service.call_count),
    )


def test_store_blob_unavailable_error_is_self_describing() -> None:
    plugin = ZuoraPlugin()
    orch = MagicMock()
    orch.get_service.return_value = None
    plugin.orchestrator_ref = orch
    raised: ZuoraServiceError | None = None
    try:
        plugin._store_blob(b"z" * 67890, "data_query_results.json", "application/json")
    except ZuoraServiceError as exc:
        raised = exc
    _assert("unavailable blob storage raises the typed error", raised is not None)
    message = str(raised)
    _assert("error names the observed payload size", "67890" in message, message)
    _assert("error names the inline cap", str(INLINE_BYTE_CAP) in message, message)


def main() -> int:
    print("\nzuora_plugin Data Query + bulk export smoke tests")
    print("=" * 47)
    test_data_query_inline()
    test_data_query_max_rows_clamp()
    test_data_query_spills_over_byte_cap()
    test_bulk_export_csv_default()
    test_bulk_export_json_format()
    test_bulk_export_rejects_bad_format()
    test_bulk_export_flags_truncation()
    test_non_2xx_raises_with_is_query_flag()
    test_store_blob_resolves_service_at_point_of_use()
    test_store_blob_unavailable_error_is_self_describing()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Data Query + bulk export smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
