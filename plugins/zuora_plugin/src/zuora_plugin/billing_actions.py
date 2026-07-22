"""Zuora verb implementations — pure functions over a built ``ZuoraClient``.

Each function takes an already-built :class:`http_client.ZuoraClient` and a
``params`` dict, returning a plain result dict. Blob I/O is injected
(``blob_writer``) for the two spilling verbs. Invalid parameters raise
``ValueError`` (mapped to ``zuora.invalid_params``); non-2xx responses raise
``ZuoraResponseError`` (carrying the response for the plugin's classifier).

No delete verb exists (v1 scope, matching the 2026-06-20 design + the
umbrella wave's read/write posture register): billing records are
voided/cancelled through Zuora's own workflow, not deleted through this tool.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable
from typing import Any

import httpx

from .constants import (
    BULK_EXPORT_ROW_CAP,
    DATA_QUERY_DEFAULT_MAX_ROWS,
    DATA_QUERY_MAX_ROWS_CAP,
    DATA_QUERY_SPILL_FILENAME,
    DEFAULT_EXPORT_FORMAT,
    EXPORT_FORMAT_CSV,
    EXPORT_FORMATS,
    INLINE_BYTE_CAP,
    MIME_CSV,
    MIME_JSON,
    SUPPORTED_OBJECT_TYPES,
)

# blob_writer(content, filename, mime_type) -> blob_id (the returned *_blob_key)
BlobWriter = Callable[[bytes, str, str], str]


class ZuoraResponseError(Exception):
    """Carries a non-2xx ``httpx.Response`` for the plugin's classifier to map."""

    def __init__(self, response: httpx.Response, *, is_query: bool) -> None:
        super().__init__(f"Zuora request failed with status {response.status_code}")
        self.response = response
        self.is_query = is_query


def data_query(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Run a ZOQL query via the synchronous Query API; return rows inline or spill."""
    zoql = _require_str(params, "zoql")
    max_rows = _clamp(params.get("max_rows"), DATA_QUERY_DEFAULT_MAX_ROWS, DATA_QUERY_MAX_ROWS_CAP)
    response = client.post("/v1/query", json={"queryString": zoql})
    _raise_for_status(response, is_query=True)
    payload = response.json()
    records = payload.get("records") or []
    records = records[:max_rows] if isinstance(records, list) else []
    return _query_envelope(records, blob_writer)


def get_object(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one object by type + id via the Object API."""
    object_type = _require_object_type(params)
    object_id = _require_str(params, "id")
    response = client.get(f"/v1/object/{object_type}/{object_id}")
    _raise_for_status(response, is_query=False)
    return {"object": response.json()}


def create_object(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create an object of the given type with the given fields."""
    object_type = _require_object_type(params)
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    response = client.post(f"/v1/object/{object_type}", json=fields)
    _raise_for_status(response, is_query=False)
    payload = response.json()
    return {
        "id": _as_str(payload.get("Id")),
        "success": bool(payload.get("Success", True)),
    }


def update_object(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Update an existing object by type + id with a non-empty fields object."""
    object_type = _require_object_type(params)
    object_id = _require_str(params, "id")
    fields = params.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise ValueError("'fields' is required and must be a non-empty object")
    response = client.put(f"/v1/object/{object_type}/{object_id}", json=fields)
    _raise_for_status(response, is_query=False)
    payload = response.json()
    return {"success": bool(payload.get("Success", True))}


def list_subscriptions(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List an account's subscriptions."""
    account_id = _require_str(params, "account_id")
    response = client.get(f"/v1/subscriptions/accounts/{account_id}")
    _raise_for_status(response, is_query=False)
    payload = response.json()
    subscriptions = payload.get("subscriptions")
    return {"subscriptions": subscriptions if isinstance(subscriptions, list) else []}


def get_invoice(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Fetch one invoice by id."""
    invoice_id = _require_str(params, "id")
    response = client.get(f"/v1/invoices/{invoice_id}")
    _raise_for_status(response, is_query=False)
    return {"invoice": response.json()}


def list_invoices(client: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List an account's invoices."""
    account_id = _require_str(params, "account_id")
    response = client.get(f"/v1/invoices/accounts/{account_id}")
    _raise_for_status(response, is_query=False)
    payload = response.json()
    invoices = payload.get("invoices")
    return {"invoices": invoices if isinstance(invoices, list) else []}


def bulk_export(client: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Run a ZOQL query and export the full result (up to BULK_EXPORT_ROW_CAP) to a blob."""
    zoql = _require_str(params, "zoql")
    fmt = _as_str(params.get("format")) or DEFAULT_EXPORT_FORMAT
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"unsupported export format '{fmt}'; choose one of {sorted(EXPORT_FORMATS)}")
    response = client.post("/v1/query", json={"queryString": zoql})
    _raise_for_status(response, is_query=True)
    payload = response.json()
    records = payload.get("records") or []
    records = records[:BULK_EXPORT_ROW_CAP] if isinstance(records, list) else []
    if fmt == EXPORT_FORMAT_CSV:
        content, mime = _to_csv(records), MIME_CSV
    else:
        content, mime = _to_json(records), MIME_JSON
    blob_key = blob_writer(content, f"zuora_export.{fmt}", mime)
    return {
        "result_blob_key": blob_key,
        "row_count": len(records),
        "format": fmt,
        "truncated": len(records) >= BULK_EXPORT_ROW_CAP,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _raise_for_status(response: httpx.Response, *, is_query: bool) -> None:
    if response.status_code >= 300:
        raise ZuoraResponseError(response, is_query=is_query)


def _query_envelope(records: list[Any], blob_writer: BlobWriter) -> dict[str, Any]:
    payload = json.dumps(records, default=str).encode("utf-8")
    if len(payload) > INLINE_BYTE_CAP:
        blob_key = blob_writer(payload, DATA_QUERY_SPILL_FILENAME, MIME_JSON)
        return {"result_blob_key": blob_key, "row_count": len(records), "spilled": True}
    return {"records": records, "row_count": len(records), "spilled": False}


def _to_json(records: list[Any]) -> bytes:
    return json.dumps(records, default=str, ensure_ascii=False).encode("utf-8")


def _to_csv(records: list[Any]) -> bytes:
    buffer = io.StringIO()
    dict_records = [r for r in records if isinstance(r, dict)]
    if not dict_records:
        return buffer.getvalue().encode("utf-8")
    columns: list[str] = []
    for record in dict_records:
        for key in record:
            if key not in columns:
                columns.append(key)
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(dict_records)
    return buffer.getvalue().encode("utf-8")


def _require_object_type(params: dict[str, Any]) -> str:
    object_type = _require_str(params, "type")
    if object_type not in SUPPORTED_OBJECT_TYPES:
        raise ValueError(
            f"'type' must be one of {sorted(SUPPORTED_OBJECT_TYPES)}, got {object_type!r}"
        )
    return object_type


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _clamp(value: Any, default: int, cap: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return max(1, min(cap, value))
