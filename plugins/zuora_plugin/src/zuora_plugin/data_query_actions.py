"""Zuora SQL Data Query jobs and Files: submit, list, read, cancel, download, restore.

Checked against the Zuora v1 API reference (reference version 2026-11-06):
Data Queries (``POST/GET /query/jobs``, ``GET/DELETE /query/jobs/{job-id}``)
and Files (``GET /v1/files/{file-id}``, ``GET /v1/files/{file-id}/status``,
``PUT /v1/files/{file-id}/restore``). This is SQL Data Query, distinct from
the ZOQL ``data_query``/``bulk_export`` verbs.

Data Query lifecycle (vendor-defined): a job is ``submitted`` →
``accepted`` → ``in_progress`` → ``completed`` | ``failed``; Zuora cancels an
accepted or in-progress job on ``DELETE`` (status ``cancelled``), which is its
only terminal action. The completed job's ``dataFile`` is a vendor-issued
pre-signed URL off the tenant host: :func:`download_data_query_result` reads
it from Zuora's own job record, never from the caller, and fetches it with no
bearer token. ``read_deleted`` queries the vendor's 30-day deleted-record
window. Files are read, status-checked, and restored from the archive.
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import urlsplit

from .commerce_fields import V1_HEADERS
from .constants import ERROR_API_ERROR
from .errors import ZuoraServiceError
from .http_client import ZuoraClient
from .request_actions import redact_bytes
from .typed_operations import Field, Operation, PathGate, call, collect, contained_output, response_body, write_file

QUERY_STATUSES: Final[tuple[str, ...]] = ("submitted", "accepted", "in_progress", "completed", "failed", "cancelled")
OUTPUT_FORMATS: Final[tuple[str, ...]] = ("JSON", "CSV", "TSV", "DSV")
COMPRESSIONS: Final[tuple[str, ...]] = ("NONE", "GZIP", "ZIP")
_JOB_ID: Final[dict[str, Field]] = {"job_id": Field("job_id", "str_id", required=True)}
# Request headers the Data Queries reference documents on every route; submit adds Idempotency-Key.
QUERY_HEADERS: Final[dict[str, Field]] = {
    "zuora_entity_ids": Field("Zuora-Entity-Ids", "header"),
    "zuora_org_ids": Field("Zuora-Org-Ids", "header"),
    "zuora_track_id": Field("Zuora-Track-Id", "track_id"),
}
SUBMIT_HEADERS: Final[dict[str, Field]] = {**QUERY_HEADERS, "idempotency_key": Field("Idempotency-Key", "header")}
_FILE_ID: Final[dict[str, Field]] = {"file_id": Field("file_id", "str_id", required=True)}


def _submit_body(params: dict[str, Any], _gate: PathGate) -> dict[str, Any]:
    body = collect(
        {
            "query": Field("query", "text", required=True),
            "output_format": Field("outputFormat", "choice", required=True, choices=OUTPUT_FORMATS, default="CSV"),
            "compression": Field("compression", "choice", required=True, choices=COMPRESSIONS, default="NONE"),
            "column_separator": Field("columnSeparator", "optional_text"),
            "read_deleted": Field("readDeleted", "bool"),
            "source_data": Field("sourceData", "choice", choices=("LIVE", "WAREHOUSE")),
            "warehouse_size": Field("warehouseSize", "choice", choices=("xsmall",)),
            "use_index_join": Field("useIndexJoin", "bool"),
            "encryption_key": Field("encryptionKey", "text"),
        },
        params,
    )
    separator = body.get("columnSeparator")
    if (body["outputFormat"] == "DSV") != (separator is not None):
        raise ValueError("column_separator is required with output_format DSV and applies only to DSV")
    if separator is not None and len(separator) != 1:
        raise ValueError("column_separator must be a single character")
    if "warehouseSize" in body and body.get("sourceData") != "WAREHOUSE":
        raise ValueError("warehouse_size applies only when source_data is WAREHOUSE")
    body["output"] = {"target": "S3"}
    return body


SUBMIT = Operation("submit_data_query", "POST", "/query/jobs", body=_submit_body, is_query=True, header_fields=SUBMIT_HEADERS)
LIST_JOBS = Operation(
    "list_data_query_jobs",
    "GET",
    "/query/jobs",
    query_fields={"query_status": Field("queryStatus", "choice", choices=QUERY_STATUSES), "page_size": Field("pageSize", "count")},
    is_query=True,
    header_fields=QUERY_HEADERS,
    paging="single_page",
)
GET_JOB = Operation("get_data_query_job", "GET", "/query/jobs/{job_id}", path_fields=_JOB_ID, is_query=True, header_fields=QUERY_HEADERS)
CANCEL_JOB = Operation("cancel_data_query_job", "DELETE", "/query/jobs/{job_id}", path_fields=_JOB_ID, is_query=True, header_fields=QUERY_HEADERS)
DOWNLOAD_RESULT = Operation("download_data_query_result", "GET", "/query/jobs/{job_id}", path_fields=_JOB_ID, is_query=True, header_fields=QUERY_HEADERS)
# The Files reference documents the four v1 request headers on all three routes.
DOWNLOAD_FILE = Operation("download_file", "GET", "/v1/files/{file_id}", path_fields=_FILE_ID, header_fields=V1_HEADERS)
FILE_STATUS = Operation("get_file_status", "GET", "/v1/files/{file_id}/status", path_fields=_FILE_ID, header_fields=V1_HEADERS)
RESTORE_FILE = Operation("restore_file", "PUT", "/v1/files/{file_id}/restore", path_fields=_FILE_ID, header_fields=V1_HEADERS)

OPERATIONS: Final[tuple[Operation, ...]] = (SUBMIT, LIST_JOBS, GET_JOB, CANCEL_JOB, FILE_STATUS, RESTORE_FILE)


def _require_output_path(params: dict[str, Any]) -> None:
    if not isinstance(params.get("output_path"), str) or not params["output_path"]:
        raise ValueError("output_path is required: an absolute path under an export_allowed_roots entry")


def validate_download(params: dict[str, Any]) -> None:
    """Dispatch-time check shared by both download verbs."""
    _require_output_path(params)


def download_data_query_result(client: ZuoraClient, params: dict[str, Any], gate: PathGate) -> dict[str, Any]:
    """Read the job; when completed, fetch its vendor-issued ``dataFile`` and write it to output_path.

    A job that is not completed is reported (``downloaded: false`` with its
    ``query_status``), not refused: poll again until it completes.
    """
    _require_output_path(params)
    resolved = contained_output(gate, params["output_path"], suffix=None)
    response, secrets, result = call(DOWNLOAD_RESULT, client, params, gate)
    job = response_body(response)
    record = job.get("data") if isinstance(job, dict) else None
    if not isinstance(record, dict):
        raise ZuoraServiceError(ERROR_API_ERROR, "Zuora returned a data query job without its data object.")
    status = record.get("queryStatus")
    data_file = record.get("dataFile")
    if status != "completed" or not isinstance(data_file, str) or not data_file:
        return {**result, "downloaded": False, "query_status": status}
    if urlsplit(data_file).scheme != "https":
        raise ZuoraServiceError(ERROR_API_ERROR, "Zuora's dataFile location is not an https URL; nothing was downloaded.")
    download = client.fetch_presigned(data_file)
    if download.is_error:
        raise ZuoraServiceError(ERROR_API_ERROR, f"Zuora's result file download failed (status {download.status_code}); request a fresh dataFile with get_data_query_job.")
    handle = write_file(resolved, redact_bytes(download.content, secrets))
    return {**result, "downloaded": True, "query_status": status, "output_rows": record.get("outputRows"), "file": handle}


def download_file(client: ZuoraClient, params: dict[str, Any], gate: PathGate) -> dict[str, Any]:
    """Fetch one tenant file (export result, invoice PDF, report) and write it to output_path."""
    _require_output_path(params)
    resolved = contained_output(gate, params["output_path"], suffix=None)
    response, secrets, result = call(DOWNLOAD_FILE, client, params, gate)
    handle = write_file(resolved, redact_bytes(response.content, secrets))
    return {**result, "content_type": response.headers.get("content-type"), "file": handle}
