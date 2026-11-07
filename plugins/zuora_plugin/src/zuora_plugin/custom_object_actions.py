"""Zuora Custom Objects and Object Query: every operation in the vendor's four sections.

Checked against the Zuora v1 API reference markdown (developer.zuora.com,
``/v1-api-reference/api/{custom-object-definitions,custom-object-records,
custom-object-jobs,object-queries}``, reference version 2026-11-06) and the
Object Query guide ``/docs/guides/expand-filter-fields-sort``.

Custom object lifecycle (vendor-defined): definitions are created (several
per call), listed, read, updated through Zuora's asynchronous migration
action (``POST /objects/migrations`` answers with a ``jobId``; fields are
added, changed or removed, the object relabelled or re-audited) and deleted
(refused while records exist). Records are created (optionally allowing
partial success), listed by query, ids or cursor, read, replaced (``PUT``,
with an optional ``If-Match`` precondition for stale-write refusal), merged
(``PATCH``, JSON Merge Patch: a field set to ``null`` is cleared) and
deleted singly or by batch (update or delete up to 1,000 per call). Bulk jobs
are submitted, given their one ``.csv`` file (the job starts when the upload
finishes), listed, read, asked for their row errors and cancelled while
pending or in progress, their terminal transition.

Object Query is read-only by vendor design: ``GET /object-query/{object}``
lists and ``GET /object-query/{object}/{key}`` reads one record of any of the
vendor's 48 standard objects (accounts, invoices, payments, subscriptions,
usages and the rest, by their plural kebab-case path name) or a custom object
(``{namespace}__{object}s``, e.g. ``default__cars``). ``filter[]`` and
``expand[]`` repeat, ``sort[]`` takes one field, ``fields[]`` and each
``<object>.fields[]`` take a comma-separated field list; pages follow the
vendor ``nextPage`` cursor (``pageSize`` 1 to 99).
"""

from __future__ import annotations

from typing import Any, Final

from .commerce_fields import IDEMPOTENT_HEADERS, V1_HEADERS, vendor_body
from .typed_operations import Field, Operation, PathGate, coerce, read_file_bytes

CSV_SUFFIX: Final[str] = ".csv"
OBJECT: Final[dict[str, Field]] = {"object": Field("object", "str_id", required=True)}
RECORD: Final[dict[str, Field]] = {**OBJECT, "record_id": Field("record_id", "str_id", required=True)}
JOB: Final[dict[str, Field]] = {"job_id": Field("job_id", "str_id", required=True)}
OBJECT_QUERY_PAGE_MAX: Final[int] = 99
_FIELD_LIST_KEY: Final[str] = ".fields[]"
_DEFINITIONS = "/objects/definitions/default"
_RECORDS = "/objects/records/default/{object}"
_JOBS = "/objects/jobs"


def cursor_check(params: dict[str, Any]) -> None:
    """Custom object records: Zuora requires ``pageSize`` whenever ``cursor`` is sent."""
    if params.get("cursor") is not None and params.get("page_size") is None:
        raise ValueError("cursor needs page_size: Zuora requires pageSize when paging records by cursor")


def object_field_lists(params: dict[str, Any]) -> dict[str, Any]:
    """``object_fields`` {object: [field, ...]} as the guide's ``<object>.fields[]=a,b`` keys."""
    value = params.get("object_fields")
    if value is None:
        return {}
    if not isinstance(value, dict) or not value:
        raise ValueError("object_fields must be a non-empty object of {object name: [field, ...]}")
    out: dict[str, Any] = {}
    for name, fields in value.items():
        key = coerce("object_fields key", Field("object", "str_id"), name)
        out[f"{key}{_FIELD_LIST_KEY}"] = ",".join(coerce(f"object_fields.{key}", Field("fields", "str_list"), fields))
    return out


def joined_fields(params: dict[str, Any]) -> dict[str, Any]:
    """``fields`` as one comma-separated ``fields[]`` value plus every ``<object>.fields[]``."""
    out = object_field_lists(params)
    fields = params.get("fields")
    if fields is not None:
        out["fields[]"] = ",".join(coerce("fields", Field("fields[]", "str_list"), fields))
    return out


def _bulk_file(params: dict[str, Any], gate: PathGate) -> bytes:
    """The job's one ``.csv`` file, read from a contained ``input_path``."""
    return read_file_bytes(gate, params.get("input_path"), param_name="input_path", suffix=CSV_SUFFIX)


OBJECT_QUERY_FIELDS: Final[dict[str, Field]] = {
    "page_size": Field("pageSize", "count", maximum=OBJECT_QUERY_PAGE_MAX),
    "cursor": Field("cursor", "text"),
    "sort": Field("sort[]", "text"),
    "expand": Field("expand[]", "str_list"),
    "filter": Field("filter[]", "str_list"),
    "include_null_fields": Field("includeNullFields", "bool"),
}

CUSTOM_OBJECT_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation("create_custom_object_definitions", "POST", _DEFINITIONS, header_fields=V1_HEADERS, body=vendor_body("definitions")),
    Operation("list_custom_object_definitions", "GET", _DEFINITIONS, query_fields={"select": Field("select", "choice", choices=("type",))}, header_fields=V1_HEADERS),
    Operation("get_custom_object_definition", "GET", _DEFINITIONS + "/{object}", path_fields=OBJECT, header_fields=V1_HEADERS),
    Operation("delete_custom_object_definition", "DELETE", _DEFINITIONS + "/{object}", path_fields=OBJECT, header_fields=V1_HEADERS),
    Operation("update_custom_object_definition", "POST", "/objects/migrations", header_fields=V1_HEADERS, body=vendor_body("actions")),
    Operation("create_custom_object_records", "POST", _RECORDS, path_fields=OBJECT, header_fields=V1_HEADERS, body=vendor_body("records")),
    Operation(
        "list_custom_object_records",
        "GET",
        _RECORDS,
        path_fields=OBJECT,
        query_fields={"q": Field("q", "text"), "ids": Field("ids", "str_list"), "page_size": Field("pageSize", "count"), "cursor": Field("cursor", "text")},
        header_fields=V1_HEADERS,
        query_check=cursor_check,
        paging="cursor",
    ),
    Operation("get_custom_object_record", "GET", _RECORDS + "/{record_id}", path_fields=RECORD, header_fields=V1_HEADERS),
    Operation(
        "update_custom_object_record",
        "PUT",
        _RECORDS + "/{record_id}",
        path_fields=RECORD,
        header_fields={**V1_HEADERS, "if_match": Field("If-Match", "header")},
        body=vendor_body(),
    ),
    Operation("patch_custom_object_record", "PATCH", _RECORDS + "/{record_id}", path_fields=RECORD, header_fields=V1_HEADERS, body=vendor_body()),
    Operation("delete_custom_object_record", "DELETE", _RECORDS + "/{record_id}", path_fields=RECORD, header_fields=V1_HEADERS),
    Operation("batch_custom_object_records", "POST", "/objects/batch/default/{object}", path_fields=OBJECT, header_fields=V1_HEADERS, body=vendor_body("action")),
    Operation("submit_custom_object_job", "POST", _JOBS, header_fields=V1_HEADERS, body=vendor_body("namespace", "object", "operation")),
    Operation(
        "list_custom_object_jobs",
        "GET",
        _JOBS,
        query_fields={"page_size": Field("pageSize", "count"), "cursor": Field("cursor", "text"), "status": Field("status", "text")},
        header_fields=V1_HEADERS,
        paging="cursor",
    ),
    Operation("get_custom_object_job", "GET", _JOBS + "/{job_id}", path_fields=JOB, header_fields=V1_HEADERS),
    Operation(
        "upload_custom_object_job_file",
        "POST",
        _JOBS + "/{job_id}/files",
        path_fields=JOB,
        header_fields=V1_HEADERS,
        body=_bulk_file,
        raw_content_type="text/csv",
    ),
    Operation("list_custom_object_job_errors", "GET", _JOBS + "/{job_id}/errors", path_fields=JOB, header_fields=V1_HEADERS),
    Operation("cancel_custom_object_job", "PATCH", _JOBS + "/{job_id}/cancel", path_fields=JOB, header_fields=V1_HEADERS),
)

OBJECT_QUERY_OPERATIONS: Final[tuple[Operation, ...]] = (
    Operation(
        "query_objects",
        "GET",
        "/object-query/{object}",
        path_fields=OBJECT,
        query_fields=OBJECT_QUERY_FIELDS,
        header_fields=IDEMPOTENT_HEADERS,
        query_builder=joined_fields,
        paging="cursor",
    ),
    Operation(
        "query_object",
        "GET",
        "/object-query/{object}/{key}",
        path_fields={**OBJECT, "key": Field("key", "str_id", required=True)},
        query_fields=OBJECT_QUERY_FIELDS,
        header_fields=IDEMPOTENT_HEADERS,
        query_builder=joined_fields,
    ),
)
