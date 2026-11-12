"""Typed Salesforce API families over the pinned-org request transport.

Each family is one verb with an ``operation`` parameter, and every operation is
one documented vendor method and path relative to ``/services/data/v{version}``:

- ``composite``: REST API Developer Guide, Composite Resources: composite,
  composite graph, composite batch, sObject tree and sObject Collections
  (create, retrieve by GET or POST, update, upsert and delete).
- ``tooling``: Tooling API Developer Guide, REST resources: query (and the
  ``nextRecordsUrl`` follow-up), search, executeAnonymous, runTestsAsynchronous,
  runTestsSynchronous, completions, the Tooling sObject list, describe, and
  record create, read, update and delete.
- ``bulk_jobs``: Bulk API 2.0 Developer Guide: query jobs (create, get
  information, list all, get results, abort and delete) and the ingest job
  list and delete the four ingest verbs did not cover.
- ``reports``: Reports and Dashboards REST API Developer Guide, report
  resources: report list, create and clone, describe, run synchronously (GET,
  or POST with filters), run asynchronously and read instances, the query
  resource, save, delete, and the report type list and describe.
- ``dashboards``: the same guide's dashboard resources: dashboard list and
  clone, describe, results, refresh, refresh status, save and delete.
- ``rest_resources``: REST API Developer Guide resources the CRUD verbs do not
  cover: resources by version, limits, recently viewed items, sObject basic
  information, global and object describe, updated and deleted record ids,
  records by external id (get, upsert, delete), relationship traversal,
  query, queryAll and their ``nextRecordsUrl`` follow-up, query explain, SOSL
  search and parameterized search.
- ``actions``: REST API Developer Guide, Invocable Actions: the standard and
  custom action lists, the actions of one custom type (apex, flow,
  quickAction, emailAlert...), describe and invoke.
- ``metadata``: Metadata API Developer Guide, REST resources for Metadata:
  deployRequest create (multipart deploy package), status, cancel and quick
  deploy of a validated deployment.
- ``content_files``: REST API Developer Guide, blob data: ContentVersion
  upload (base64 ``VersionData``), ``VersionData`` download and ContentDocument
  delete. The local file moves are ``transfers``'.

Build 5 also adds Tooling composite (``/tooling/composite``) and
apexCompileResults (API 68.0 and later; its request body is exactly ``{}``),
the Bulk API 2.0 parallel ``resultPages`` resource and its ``nextRecordsUrl``
follow-up, the
report instance delete and sObject relationship update and delete.

Input checks mirror those documents: path identifiers, documented query
parameter names and value types, required body members and enumerated body
values. Nothing here classifies reads against writes: the registered user's
Salesforce permissions decide every operation, including deletes. Transport
and the audit line are ``request_actions.forward``'s, the same call the
generic verb makes, so a typed verb can never return or log what the generic
verb would not. Error codes, failure counts and pagination hints are read from
the forwarded call's vendor body and headers, and the result is returned as
received.

Pagination is surfaced, never followed: ``nextRecordsUrl`` becomes a
``query_more`` path or a ``queryLocator`` value, a Bulk results page's
``Sforce-Locator`` header becomes the next ``locator``, and a report run whose
``allData`` is false says so, because the vendor returns only its first
detail rows and has no next page to request. Bulk query results are
CSV record data, so they are written under ``registered path addresses`` exactly as
``bulk_job_results`` writes its CSVs, never returned inline.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlsplit

from .client import SalesforceCliExecutor
from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_BAD_REQUEST,
    ERROR_CONFLICT,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_PRECONDITION_FAILED,
    ERROR_RATE_LIMITED,
)
from .errors import SalesforceCliCallError, TypedFamilyError, classify_salesforce_error
from .request_actions import Forwarded, forward
from .transfers import TransferGates
from .transfers import local_path_parameter as transfer_path_parameter
from .transfers import run as run_transfer
from .transfers import validate as validate_transfer

QueryKind = Literal["bool", "int", "text", "record_id", "datetime", "id_list", "name_list", "job_type", "concurrency", "completions_type"]
# "empty": the vendor takes exactly {} and refuses any field (Tooling apexCompileResults).
BodyKind = Literal["none", "object", "optional", "empty"]
Media = Literal["json", "csv"]
Transfer = Literal["none", "zip_package", "file_upload", "file_download"]
PathGate = Callable[[str], str]

# Salesforce API names: standard, custom (__c), event (__e), external (__x),
# namespaced (ns__Name__c). Record and job ids are 15 or 18 characters.
_API_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}")
_RECORD_ID = re.compile(r"[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?")
# Report type API names join entities with '$' (CustomEntity$Invoice__c).
_REPORT_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_$]{0,254}")
# Invocable action names; quickAction and emailAlert names are Object.Name.
_ACTION_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,79}(?:\.[A-Za-z][A-Za-z0-9_]{0,79})?")
# An external id value is caller data; it is percent-encoded into its segment.
_EXTERNAL_ID = re.compile(r"[^\x00-\x1f\x7f]{1,255}")
_ISO_DATETIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?(?:Z|[+-][0-9]{2}:?[0-9]{2})")
_PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")
_SALESFORCE_ID = (_RECORD_ID, "a 15- or 18-character Salesforce id")
_SEGMENTS: Mapping[str, tuple[re.Pattern[str], str]] = {
    **dict.fromkeys(("id", "job_id", "report_id", "dashboard_id", "instance_id", "deploy_request_id", "content_version_id", "content_document_id"), _SALESFORCE_ID),
    **dict.fromkeys(("sobject", "external_id_field", "relationship", "action_type"), (_API_NAME, "a Salesforce API name")),
    "report_type": (_REPORT_TYPE, "a report type API name"),
    "action": (_ACTION_NAME, "an invocable action name such as chatterPost or Account.NewCase"),
    "external_id": (_EXTERNAL_ID, "a non-empty external id value of at most 255 characters"),
}
# The vendor's query-more locator paths, e.g. .../query/01gD0000002HU6KIAW-2000.
_QUERY_MORE: Mapping[str, tuple[re.Pattern[str], str]] = {
    "tooling": (re.compile(r"/services/data/v[0-9]+\.[0-9]/tooling/query/[A-Za-z0-9-]+"), "a Tooling query"),
    "rest_resources": (re.compile(r"/services/data/v[0-9]+\.[0-9]/query(?:All)?/[A-Za-z0-9-]+"), "a query or queryAll"),
    # The Bulk guide's example prints this path in lower case (resultpages); it is followed verbatim.
    "bulk_jobs": (re.compile(r"/services/data/v[0-9]+\.[0-9]/jobs/query/[A-Za-z0-9]{15}(?:[A-Za-z0-9]{3})?/result[Pp]ages\?locator=[A-Za-z0-9._~%-]+"), "a Bulk query job resultPages"),
}
# Composite and Collections limits from the REST API Developer Guide.
_COLLECTION_IDS_MAX = 200
_BULK_DELIMITERS = frozenset({"BACKQUOTE", "CARET", "COMMA", "PIPE", "SEMICOLON", "TAB"})
_BULK_LINE_ENDINGS = frozenset({"LF", "CRLF"})
_BULK_QUERY_OPERATIONS = frozenset({"query", "queryAll"})
_JOB_TYPES = frozenset({"BigObjectIngest", "Classic", "V2Ingest", "V2Query"})
_CONCURRENCY_MODES = frozenset({"parallel", "serial"})
_COMPLETIONS_TYPES = frozenset({"apex", "visualforce"})
# HTTP outcome classes when the vendor errorCode is not more specific. A 300
# (Multiple Choices) answers an external id that matches several records.
_STATUS_ERRORS: Mapping[int, str] = {300: ERROR_CONFLICT, 400: ERROR_BAD_REQUEST, 401: ERROR_AUTH_FAILED, 403: ERROR_PERMISSION_DENIED, 404: ERROR_NOT_FOUND, 409: ERROR_CONFLICT, 412: ERROR_PRECONDITION_FAILED, 429: ERROR_RATE_LIMITED}


@dataclass(frozen=True)
class Operation:
    method: str
    path: str
    query: Mapping[str, QueryKind] = field(default_factory=dict)
    required_query: frozenset[str] = frozenset()
    body: BodyKind = "none"
    body_required: tuple[str, ...] = ()
    media: Media = "json"
    fixed_body: Mapping[str, Any] | None = None
    transfer: Transfer = "none"


def _composite() -> dict[str, Operation]:
    collections = "/composite/sobjects"
    return {
        "composite": Operation("POST", "/composite", body="object", body_required=("compositeRequest",)),
        "graph": Operation("POST", "/composite/graph", body="object", body_required=("graphs",)),
        "batch": Operation("POST", "/composite/batch", body="object", body_required=("batchRequests",)),
        "tree": Operation("POST", "/composite/tree/{sobject}", body="object", body_required=("records",)),
        "collections_create": Operation("POST", collections, body="object", body_required=("records",)),
        "collections_retrieve": Operation("GET", collections + "/{sobject}", {"ids": "id_list", "fields": "name_list"}, frozenset({"ids", "fields"})),
        "collections_retrieve_many": Operation("POST", collections + "/{sobject}", body="object", body_required=("ids", "fields")),
        "collections_update": Operation("PATCH", collections, body="object", body_required=("records",)),
        "collections_upsert": Operation("PATCH", collections + "/{sobject}/{external_id_field}", body="object", body_required=("records",)),
        "collections_delete": Operation("DELETE", collections, {"ids": "id_list", "allOrNone": "bool"}, frozenset({"ids"})),
    }


def _tooling() -> dict[str, Operation]:
    sobjects = "/tooling/sobjects"
    return {
        "query": Operation("GET", "/tooling/query", {"q": "text"}, frozenset({"q"})),
        "query_more": Operation("GET", "{next_records_url}"),
        "search": Operation("GET", "/tooling/search", {"q": "text"}, frozenset({"q"})),
        "execute_anonymous": Operation("GET", "/tooling/executeAnonymous", {"anonymousBody": "text"}, frozenset({"anonymousBody"})),
        "run_tests_async": Operation("POST", "/tooling/runTestsAsynchronous", body="object"),
        "run_tests_sync": Operation("POST", "/tooling/runTestsSynchronous", body="object"),
        "completions": Operation("GET", "/tooling/completions", {"type": "completions_type"}, frozenset({"type"})),
        "list_sobjects": Operation("GET", sobjects),
        "sobject_info": Operation("GET", sobjects + "/{sobject}"),
        "describe": Operation("GET", sobjects + "/{sobject}/describe"),
        "create": Operation("POST", sobjects + "/{sobject}", body="object"),
        "get": Operation("GET", sobjects + "/{sobject}/{id}", {"fields": "name_list"}),
        "update": Operation("PATCH", sobjects + "/{sobject}/{id}", body="object"),
        "delete": Operation("DELETE", sobjects + "/{sobject}/{id}"),
        "composite_resources": Operation("GET", "/tooling/composite"),
        "composite": Operation("POST", "/tooling/composite", body="object", body_required=("compositeRequest",)),
        # API 68.0 and later; the request body must be exactly {}.
        "apex_compile_results": Operation("POST", "/tooling/apexCompileResults", body="empty"),
    }


def _bulk_jobs() -> dict[str, Operation]:
    query_jobs = "/jobs/query"
    listing: Mapping[str, QueryKind] = {"isPkChunkingEnabled": "bool", "jobType": "job_type", "concurrencyMode": "concurrency", "queryLocator": "text"}
    return {
        "query_create": Operation("POST", query_jobs, body="object", body_required=("operation", "query")),
        "query_get": Operation("GET", query_jobs + "/{job_id}"),
        "query_list": Operation("GET", query_jobs, listing),
        "query_results": Operation("GET", query_jobs + "/{job_id}/results", {"locator": "text", "maxRecords": "int"}, media="csv"),
        # Available from API 58.0. The guide defines no query parameter; the next
        # set of page links is the response's nextRecordsUrl, followed verbatim.
        "query_result_pages": Operation("GET", query_jobs + "/{job_id}/resultPages"),
        "query_result_pages_more": Operation("GET", "{next_records_url}"),
        "query_abort": Operation("PATCH", query_jobs + "/{job_id}", fixed_body={"state": "Aborted"}),
        "query_delete": Operation("DELETE", query_jobs + "/{job_id}"),
        "ingest_list": Operation("GET", "/jobs/ingest", listing),
        "ingest_delete": Operation("DELETE", "/jobs/ingest/{job_id}"),
    }


def _reports() -> dict[str, Operation]:
    reports = "/analytics/reports"
    report = reports + "/{report_id}"
    details: Mapping[str, QueryKind] = {"includeDetails": "bool"}
    metadata = ("reportMetadata",)
    return {
        "list": Operation("GET", reports),
        "create": Operation("POST", reports, body="object", body_required=metadata),
        "clone": Operation("POST", reports, {"cloneId": "record_id"}, frozenset({"cloneId"}), body="object", body_required=metadata),
        "describe": Operation("GET", report + "/describe"),
        "run": Operation("GET", report, details),
        "run_filtered": Operation("POST", report, details, body="object", body_required=metadata),
        "run_async": Operation("POST", report + "/instances", details, body="optional"),
        "instances": Operation("GET", report + "/instances"),
        "instance_results": Operation("GET", report + "/instances/{instance_id}"),
        # The vendor deletes an instance whose status is Success or Error.
        "instance_delete": Operation("DELETE", report + "/instances/{instance_id}"),
        "query": Operation("POST", reports + "/query", details, body="object", body_required=metadata),
        "update": Operation("PATCH", report, body="object", body_required=metadata),
        "delete": Operation("DELETE", report),
        "report_types": Operation("GET", "/analytics/reportTypes"),
        "describe_report_type": Operation("GET", "/analytics/reportTypes/{report_type}"),
    }


def _dashboards() -> dict[str, Operation]:
    dashboards = "/analytics/dashboards"
    dashboard = dashboards + "/{dashboard_id}"
    # Dashboard Results documents these four for GET and PUT; Dashboard Status for GET.
    view: Mapping[str, QueryKind] = {"runningUser": "record_id", "filter1": "text", "filter2": "text", "filter3": "text"}
    return {
        "list": Operation("GET", dashboards),
        "clone": Operation("POST", dashboards, {"cloneId": "record_id"}, frozenset({"cloneId"}), body="optional"),
        "describe": Operation("GET", dashboard + "/describe", {"loadComponentProperties": "bool"}),
        "results": Operation("GET", dashboard, view),
        "refresh": Operation("PUT", dashboard, view, body="optional"),
        "status": Operation("GET", dashboard + "/status", view),
        "update": Operation("PATCH", dashboard, {"isStickyFilterSave": "bool"}, body="object"),
        "delete": Operation("DELETE", dashboard),
    }


def _rest_resources() -> dict[str, Operation]:
    sobject = "/sobjects/{sobject}"
    by_external_id = sobject + "/{external_id_field}/{external_id}"
    window: Mapping[str, QueryKind] = {"start": "datetime", "end": "datetime"}
    return {
        "resources": Operation("GET", "/"),
        "limits": Operation("GET", "/limits"),
        "recent": Operation("GET", "/recent", {"limit": "int"}),
        "describe_global": Operation("GET", "/sobjects"),
        "sobject_info": Operation("GET", sobject),
        "describe": Operation("GET", sobject + "/describe"),
        "updated": Operation("GET", sobject + "/updated", window, frozenset(window)),
        "deleted": Operation("GET", sobject + "/deleted", window, frozenset(window)),
        "get_by_external_id": Operation("GET", by_external_id, {"fields": "name_list"}),
        "upsert_by_external_id": Operation("PATCH", by_external_id, {"updateOnly": "bool"}, body="object"),
        "delete_by_external_id": Operation("DELETE", by_external_id),
        "relationship": Operation("GET", sobject + "/{id}/{relationship}", {"fields": "name_list"}),
        "relationship_update": Operation("PATCH", sobject + "/{id}/{relationship}", body="object"),
        "relationship_delete": Operation("DELETE", sobject + "/{id}/{relationship}"),
        "query": Operation("GET", "/query", {"q": "text"}, frozenset({"q"})),
        "query_all": Operation("GET", "/queryAll", {"q": "text"}, frozenset({"q"})),
        "query_more": Operation("GET", "{next_records_url}"),
        "explain": Operation("GET", "/query", {"explain": "text"}, frozenset({"explain"})),
        "search": Operation("GET", "/search", {"q": "text"}, frozenset({"q"})),
        "parameterized_search": Operation("POST", "/parameterizedSearch", body="object", body_required=("q",)),
    }


def _actions() -> dict[str, Operation]:
    standard = "/actions/standard"
    custom = "/actions/custom"
    return {
        "list": Operation("GET", "/actions"),
        "list_standard": Operation("GET", standard),
        "list_custom": Operation("GET", custom),
        "list_custom_type": Operation("GET", custom + "/{action_type}"),
        "describe_standard": Operation("GET", standard + "/{action}"),
        "describe_custom": Operation("GET", custom + "/{action_type}/{action}"),
        "invoke_standard": Operation("POST", standard + "/{action}", body="object", body_required=("inputs",)),
        "invoke_custom": Operation("POST", custom + "/{action_type}/{action}", body="object", body_required=("inputs",)),
    }


def _metadata() -> dict[str, Operation]:
    deploy_request = "/metadata/deployRequest"
    one = deploy_request + "/{deploy_request_id}"
    return {
        "deploy": Operation("POST", deploy_request, body="object", body_required=("deployOptions",), transfer="zip_package"),
        "deploy_status": Operation("GET", one, {"includeDetails": "bool"}),
        "deploy_cancel": Operation("PATCH", one, fixed_body={"deployResult": {"status": "Canceling"}}),
        # POST, not PATCH: the vendor reads a PATCH as a new deployment.
        "deploy_quick": Operation("POST", one, body="object", body_required=("validatedDeployRequestId",)),
    }


def _content_files() -> dict[str, Operation]:
    return {
        "upload": Operation("POST", "/sobjects/ContentVersion", body="object", body_required=("PathOnClient",), transfer="file_upload"),
        "download": Operation("GET", "/sobjects/ContentVersion/{content_version_id}/VersionData", transfer="file_download"),
        "delete_document": Operation("DELETE", "/sobjects/ContentDocument/{content_document_id}"),
    }


FAMILIES: Mapping[str, Mapping[str, Operation]] = {
    "composite": _composite(),
    "tooling": _tooling(),
    "bulk_jobs": _bulk_jobs(),
    "reports": _reports(),
    "dashboards": _dashboards(),
    "rest_resources": _rest_resources(),
    "actions": _actions(),
    "metadata": _metadata(),
    "content_files": _content_files(),
}


def operation_names(family: str) -> str:
    """Operation names for verb parameter descriptions."""
    return ", ".join(FAMILIES[family])


def path_parameters(family: str) -> list[str]:
    """Path placeholders the family's operations use, in first-use order."""
    names = (name for operation in FAMILIES[family].values() for name in _PLACEHOLDER.findall(operation.path))
    return list(dict.fromkeys(names))


def writes_csv(family: str) -> bool:
    return any(operation.media == "csv" for operation in FAMILIES[family].values())


def local_paths(family: str) -> list[str]:
    """Local file parameters the family's transfer operations take, in first-use order."""
    names = (transfer_path_parameter(operation.transfer) for operation in FAMILIES[family].values() if operation.transfer != "none")
    return list(dict.fromkeys(names))


def _operation(family: str, params: Mapping[str, Any]) -> tuple[str, Operation]:
    name = params.get("operation")
    if not isinstance(name, str) or name not in FAMILIES[family]:
        raise TypedFamilyError(f"operation must be one of: {operation_names(family)}")
    return name, FAMILIES[family][name]


def _query_more_path(family: str, value: object) -> str:
    pattern, source = _QUERY_MORE[family]
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise TypedFamilyError(f"next_records_url must be the nextRecordsUrl of {source}, e.g. /services/data/v62.0/query/01gxx-2000")
    return value


def _path(family: str, operation: Operation, params: Mapping[str, Any], api_version: str) -> str:
    if operation.path == "{next_records_url}":
        return _query_more_path(family, params.get("next_records_url"))

    def segment(match: re.Match[str]) -> str:
        key = match.group(1)
        value = params.get(key)
        pattern, kind = _SEGMENTS[key]
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise TypedFamilyError(f"{key} must be {kind}")
        return quote(value, safe="")

    return f"/services/data/v{api_version}" + _PLACEHOLDER.sub(segment, operation.path)


def _boolean(key: str, value: object) -> str:
    if not isinstance(value, bool):
        raise TypedFamilyError(f"{key} must be a boolean")
    return "true" if value else "false"


def _pattern(regex: re.Pattern[str], kind: str) -> Callable[[str, object], str]:
    def check(key: str, value: object) -> str:
        if not isinstance(value, str) or regex.fullmatch(value) is None:
            raise TypedFamilyError(f"{key} must be {kind}")
        return value

    return check


def _integer(key: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TypedFamilyError(f"{key} must be a positive integer")
    return value


def _text(key: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise TypedFamilyError(f"{key} must be a non-empty string")
    return value


def _choice(choices: frozenset[str]) -> Callable[[str, object], str]:
    def check(key: str, value: object) -> str:
        if not isinstance(value, str) or value not in choices:
            raise TypedFamilyError(f"{key} must be one of: {', '.join(sorted(choices))}")
        return value

    return check


def _joined(pattern: re.Pattern[str], kind: str, limit: int | None) -> Callable[[str, object], str]:
    """The vendor takes these lists comma-separated in one query value."""

    def check(key: str, value: object) -> str:
        items = value if isinstance(value, list) else None
        if not items or not all(isinstance(item, str) and pattern.fullmatch(item) for item in items):
            raise TypedFamilyError(f"{key} must be a non-empty list of {kind}")
        if limit is not None and len(items) > limit:
            raise TypedFamilyError(f"{key} accepts at most {limit} values")
        return ",".join(items)

    return check


_QUERY_CHECKS: Mapping[QueryKind, Callable[[str, object], object]] = {
    "bool": _boolean,
    "int": _integer,
    "text": _text,
    "record_id": _pattern(_RECORD_ID, "a 15- or 18-character Salesforce id"),
    "datetime": _pattern(_ISO_DATETIME, "an ISO 8601 date-time with a zone, e.g. 2026-11-01T00:00:00+00:00"),
    "id_list": _joined(_RECORD_ID, "Salesforce ids", _COLLECTION_IDS_MAX),
    "name_list": _joined(_API_NAME, "field API names", None),
    "job_type": _choice(_JOB_TYPES),
    "concurrency": _choice(_CONCURRENCY_MODES),
    "completions_type": _choice(_COMPLETIONS_TYPES),
}


def _query(operation: Operation, params: Mapping[str, Any]) -> dict[str, object]:
    raw = params.get("query") or {}
    if not isinstance(raw, dict):
        raise TypedFamilyError("query must be an object of vendor query parameters")
    unknown = sorted(set(raw) - set(operation.query))
    if unknown:
        allowed = ", ".join(operation.query) or "none"
        raise TypedFamilyError(f"query parameters {unknown} are not documented for this operation (allowed: {allowed})")
    missing = sorted(operation.required_query - set(raw))
    if missing:
        raise TypedFamilyError(f"query parameters {missing} are required for this operation")
    return {key: _QUERY_CHECKS[operation.query[key]](key, value) for key, value in raw.items()}


def _body(family: str, name: str, operation: Operation, params: Mapping[str, Any]) -> Mapping[str, Any] | None:
    body = params.get("body")
    if operation.body == "none":
        if body is not None:
            raise TypedFamilyError("this operation takes no body object")
        return operation.fixed_body
    if operation.body == "optional" and body is None:
        return None
    if operation.body == "empty":
        if body is not None and body != {}:
            raise TypedFamilyError("this operation's request body is the empty object {}; the vendor refuses any field")
        return {}
    checked = _body_members(operation, body)
    if family == "bulk_jobs" and name == "query_create":
        _bulk_query_body(checked)
    return checked


def _body_members(operation: Operation, body: object) -> Mapping[str, Any]:
    """A present body must be a non-empty object holding every required member."""
    if not isinstance(body, dict) or not body:
        raise TypedFamilyError("body must be a non-empty JSON object")
    checked: dict[str, Any] = body
    missing = [member for member in operation.body_required if checked.get(member) in (None, "", [], {})]
    if missing:
        raise TypedFamilyError(f"body is missing required members: {', '.join(missing)}")
    return checked


def _bulk_query_body(body: Mapping[str, Any]) -> None:
    """Bulk API 2.0 'Create a Query Job' request body values."""
    _choice(_BULK_QUERY_OPERATIONS)("operation", body.get("operation"))
    _text("query", body.get("query"))
    for key, choices in (("columnDelimiter", _BULK_DELIMITERS), ("lineEnding", _BULK_LINE_ENDINGS), ("contentType", frozenset({"CSV"}))):
        if key in body:
            _choice(choices)(key, body[key])


def _headers(params: Mapping[str, Any], operation: Operation, has_body: bool) -> dict[str, str]:
    """Documented request headers (Sforce-Query-Options, Sforce-Call-Options, If-Match...) pass through."""
    headers = params.get("headers") or {}
    if not isinstance(headers, dict):
        raise TypedFamilyError("headers must be an object of HTTP header strings")
    accept = "text/csv" if operation.media == "csv" else "application/json"
    defaults = {"Accept": accept, **({"Content-Type": "application/json"} if has_body else {})}
    present = {key.lower() for key in headers}
    return {**{key: value for key, value in defaults.items() if key.lower() not in present}, **headers}


def build_request(family: str, params: Mapping[str, Any], api_version: str) -> dict[str, Any]:
    """Validate typed input and return ``authenticated_request`` parameters."""
    name, operation = _operation(family, params)
    query = _query(operation, params)
    body = _body(family, name, operation, params)
    if operation.transfer != "none":
        validate_transfer(operation.transfer, params, body)
    request: dict[str, Any] = {"method": operation.method, "path": _path(family, operation, params, api_version), "query": query, "headers": _headers(params, operation, body is not None)}
    if body is not None:
        request["body"] = json.dumps(body)
    return request


def _vendor_error_code(body: object) -> str | None:
    """The sf.* class of the first vendor errorCode, when that code has one."""
    first = body[0] if isinstance(body, list) and body and isinstance(body[0], dict) else None
    vendor_code = first.get("errorCode") if first is not None else None
    if not isinstance(vendor_code, str) or not vendor_code:
        return None
    code, _message = classify_salesforce_error(SalesforceCliCallError(vendor_code, ""))
    return None if code == ERROR_API_ERROR else code


def _error_code(status: int | None, body: object) -> str | None:
    """A vendor errorCode with a known sf.* class wins over the HTTP status class."""
    if status is not None and 200 <= status < 300:
        return None
    vendor = _vendor_error_code(body)
    if vendor is not None:
        return vendor
    return _STATUS_ERRORS.get(status, ERROR_API_ERROR) if status is not None else ERROR_API_ERROR


def _http_failed(status: object) -> bool:
    return isinstance(status, int) and not isinstance(status, bool) and status >= 400


def _count(items: object, failed: Callable[[dict[str, Any]], bool]) -> int:
    return sum(1 for item in items if isinstance(item, dict) and failed(item)) if isinstance(items, list) else 0


# Per-part outcome shapes inside a 2xx: (marker member, list member, failed-part test).
_PART_OUTCOMES: tuple[tuple[str, str, Callable[[dict[str, Any]], bool]], ...] = (
    ("compositeResponse", "compositeResponse", lambda item: _http_failed(item.get("httpStatusCode"))),
    ("graphs", "graphs", lambda item: item.get("isSuccessful") is False),
    # batch results carry statusCode; tree results carry errors.
    ("hasErrors", "results", lambda item: _http_failed(item.get("statusCode")) or bool(item.get("errors"))),
)


def _failures(body: object) -> int:
    """Count failed parts inside a 2xx response that reports per-part outcomes."""
    if isinstance(body, list):
        # sObject Collections results carry success; invocable action results carry isSuccess.
        return _count(body, lambda item: item.get("success") is False or item.get("isSuccess") is False)
    if not isinstance(body, dict):
        return 0
    checked: dict[str, Any] = body
    return _object_failures(checked)


def _object_failures(body: Mapping[str, Any]) -> int:
    """Per-part outcomes inside a JSON object body, by the vendor shape that carries them."""
    deploy = body.get("deployResult")
    if isinstance(deploy, dict):
        return _deploy_failures(deploy)
    if body.get("status") == "PARTIAL_FAILURE":
        # apexCompileResults lists only the classes and triggers that failed to compile.
        return max(_count(body.get("results"), lambda item: item.get("success") is False), 1)
    for marker, member, failed in _PART_OUTCOMES:
        if marker in body and isinstance(body.get(member), list):
            return _count(body[member], failed)
    return 1 if body.get("success") is False or body.get("compiled") is False else 0


def _deploy_failures(deploy: Mapping[str, Any]) -> int:
    """A Failed or SucceededPartial deployment counts its component and test errors, at least one."""
    if deploy.get("status") not in ("Failed", "SucceededPartial"):
        return 0
    counts = (deploy.get(key) for key in ("numberComponentErrors", "numberTestErrors"))
    # The vendor prints these counts as strings in the REST examples; only ASCII
    # digits are read, so no vendor text can reach an int() conversion error.
    errors = sum(value if isinstance(value, int) and not isinstance(value, bool) else int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else 0 for value in counts)
    return max(errors, 1)


def _pagination(forwarded: Forwarded, problems: list[str]) -> dict[str, Any]:
    """Explicit next-call hints read from the vendor structure."""
    body = forwarded.raw_body
    return {**(_body_hints(body, problems) if isinstance(body, dict) else {}), **_header_hints(forwarded.raw_headers)}


def _url_query(url: str, problems: list[str], where: str) -> dict[str, list[str]]:
    """Query parameters of a raw vendor URL, or none.

    ``urlsplit`` can raise with the URL's own text in its message (an NFKC
    netloc, a bracketed host), and that text can carry a credential, so the
    exception never leaves: only its type and where it happened are recorded.
    """
    try:
        return parse_qs(urlsplit(url).query)
    except ValueError as exc:
        problems.append(f"{type(exc).__name__} parsing {where}")
        return {}


def _body_hints(body: Mapping[str, Any], problems: list[str]) -> dict[str, Any]:
    hint: dict[str, Any] = {}
    if body.get("allData") is False:
        hint["report_all_data"] = False
    next_url = body.get("nextRecordsUrl")
    if isinstance(next_url, str) and next_url:
        hint["next_records_url"] = next_url
        locator = _url_query(next_url, problems, "nextRecordsUrl").get("queryLocator")
        if locator:
            hint["next_query_locator"] = locator[0]
    pages = body.get("resultPages")
    if isinstance(pages, list):
        hint["result_locators"] = _result_locators(pages, problems)
    return hint


def _result_locators(pages: list[Any], problems: list[str]) -> list[str]:
    """Each parallel result page is a query_results call with this locator."""
    urls = (page.get("resultUrl") for page in pages if isinstance(page, dict))
    return [_url_query(url, problems, "resultPages[].resultUrl").get("locator", [""])[0] for url in urls if isinstance(url, str)]


def _interpret(status: int | None, forwarded: Forwarded) -> tuple[str | None, int, dict[str, Any], list[str]]:
    """Error code, failure count, hints and recorded problems from the raw vendor response.

    Any exception while reading vendor data is contained here: its text can
    quote the data, so only the exception type is recorded and the outcome
    falls back to the HTTP status class.
    """
    problems: list[str] = []
    try:
        error_code = _error_code(status, forwarded.raw_body)
        failures = _failures(forwarded.raw_body) if error_code is None else 0
        return error_code, failures, _pagination(forwarded, problems), problems
    except Exception as exc:  # noqa: BLE001 — vendor data must never escape through exception text
        problems.append(f"{type(exc).__name__} interpreting the vendor response")
        return _error_code(status, None), 0, {}, problems


def _header_hints(headers: Mapping[str, Any]) -> dict[str, Any]:
    hint: dict[str, Any] = {}
    locator_header = headers.get("sforce-locator")
    if isinstance(locator_header, str) and locator_header and locator_header != "null":
        hint["next_locator"] = locator_header
    if isinstance(headers.get("sforce-numberofrecords"), str):
        hint["number_of_records"] = headers["sforce-numberofrecords"]
    return hint


def _write_csv(result: dict[str, Any], output_path: str) -> None:
    """Write one Bulk results page; the CSV is record data and never stays inline."""
    body = result.pop("body", None)
    if result.get("body_advisory") or not isinstance(body, str):
        result.pop("transcript", None)
        raise SalesforceCliCallError("", "the Bulk results response boundary was uncertain; no CSV was written")
    parent_dir = os.path.dirname(output_path)
    if not os.path.isdir(parent_dir):
        raise TypedFamilyError(f"the parent directory of output_csv_path does not exist ({parent_dir}); create it first")
    with open(output_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(body)
    result["output_csv_path"] = output_path
    result["bytes_written"] = len(body.encode("utf-8"))


def _call(operation: Operation, executor: SalesforceCliExecutor, request: dict[str, Any], params: Mapping[str, Any], path_gate: PathGate | TransferGates | None) -> tuple[Forwarded, str | None]:
    """One vendor call (a download is a record read plus its bytes); returns the result and any CSV path."""
    if operation.transfer != "none":
        return run_transfer(operation.transfer, executor, request, params, path_gate), None
    output_path = None
    if operation.media == "csv":
        if not callable(path_gate):
            raise TypedFamilyError("this operation needs the export path gate")
        output_path = path_gate(_text("output_csv_path", params.get("output_csv_path")))
    return forward(executor, request, expected_media=operation.media), output_path


def run_family(family: str, executor: SalesforceCliExecutor, params: dict[str, Any], path_gate: PathGate | TransferGates | None = None) -> dict[str, Any]:
    """Worker entry: validate, admit any local path, then make the operation's vendor call."""
    name, operation = _operation(family, params)
    request = build_request(family, params, executor.api_version)
    forwarded, output_path = _call(operation, executor, request, params, path_gate)
    result = dict(forwarded.raw_result)
    status = result.get("status")
    # Outcomes, failure counts and pagination are read from the vendor structure.
    error_code, failures, hint, problems = _interpret(status if isinstance(status, int) and not isinstance(status, bool) else None, forwarded)
    if output_path is not None and error_code is None:
        _write_csv(result, output_path)
    data: dict[str, Any] = {**result, "family": family, "operation": name, "method": operation.method}
    if error_code is not None:
        data["error_code"] = error_code
    if problems:
        data["vendor_structure_advisory"] = "; ".join(problems) + ". The vendor text is withheld because it can carry credentials; failure counts and pagination hints may be incomplete."
    if failures:
        data["failures"] = failures
    if hint:
        data["pagination"] = hint
    return data
