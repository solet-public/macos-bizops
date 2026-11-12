"""Typed Bulk Import and Extract job operations; vendor state stays explicit.

Each invocation is a deferred platform job. A completed platform job is one
completed HTTP operation, not a completed vendor import/export. Keep batchId
or exportId and call the corresponding status verb. No polling thread sleeps
while a vendor job is queued, and no uncertain mutation is retried blindly.
"""

from __future__ import annotations

from typing import Any, Final
from urllib.parse import quote

from .asset_api import _integer
from .errors import MarketoEnvelopeError, MarketoTransportError
from .http_client import MarketoClient
from .marketing_actions import _require_str
from .multipart import prepare_files
from .request import decode_body, validate_header

# (method, path, operation). Adobe's swagger-mapi.json plus the Bulk guides.
ENDPOINTS: Final[dict[str, tuple[str, str, str]]] = {}
for _family, _submit, _status in (
    ("lead", "/leads.json", "/leads/batch/{batch_id}"),
    ("custom_object", "/customobjects/{api_name}/import.json", "/customobjects/{api_name}/import/{batch_id}/status"),
    ("program_member", "/program/{program_id}/members/import.json", "/program/members/import/{batch_id}/status"),
):
    ENDPOINTS[f"import_{_family}s"] = ("POST", _submit, "import")
    ENDPOINTS[f"get_{_family}_import_status"] = ("GET", _status + ".json", "status")
    for _report in ("failures", "warnings"):
        _base = _status.removesuffix("/status")
        ENDPOINTS[f"get_{_family}_import_{_report}"] = ("GET", _base + f"/{_report}.json", "download")
for _family, _resource in (("lead", "leads"), ("activity", "activities"), ("program_member", "program/members")):
    _base = f"/{_resource}/export"
    ENDPOINTS[f"list_bulk_{_family}_extract_jobs"] = ("GET", _base + ".json", "list")
    ENDPOINTS[f"create_bulk_{_family}_extract"] = ("POST", _base + "/create.json", "create")
    for _operation in ("enqueue", "cancel", "status", "file"):
        _verb = "download" if _operation == "file" else "get" if _operation == "status" else _operation
        _suffix = "_status" if _operation == "status" else ""
        ENDPOINTS[f"{_verb}_bulk_{_family}_extract{_suffix}"] = ("POST" if _operation in {"enqueue", "cancel"} else "GET", _base + "/{export_id}/" + _operation + ".json", "download" if _operation == "file" else _operation)


def _path(path: str, params: dict[str, Any]) -> str:
    for key in ("batch_id", "program_id", "api_name", "export_id"):
        if "{" + key + "}" not in path:
            continue
        if key == "batch_id":
            value = str(_integer(params, key, 1))
        else:
            value = _require_str(params, key)
            if value in {".", ".."}:
                raise ValueError(f"{key} cannot be a dot path segment")
            if key == "program_id" and not value.isdecimal():
                raise ValueError("program_id must be a numeric id string")
        path = path.replace("{" + key + "}", quote(value, safe=""))
    return "/bulk/v1" + path


def _import_query(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    value = _require_str(params, "format")
    formats = {"csv", "tsv", "ssv", "CSV", "TSV", "SSV"} if verb == "import_program_members" else {"csv", "tsv", "ssv"}
    if value not in formats:
        raise ValueError("format must match a vendor-documented import format")
    query: dict[str, Any] = {"format": value}
    for key, wire in (("lookup_field", "lookupField"), ("partition_name", "partitionName"), ("list_id", "listId"), ("program_member_status", "programMemberStatus")):
        if params.get(key) is not None:
            query[wire] = _integer(params, key, 1) if key == "list_id" else _require_str(params, key)
    if verb == "import_program_members":
        query["programMemberStatus"] = _require_str(params, "program_member_status")
    return query


def _create_fields(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    body: dict[str, Any] = {}
    fields = params.get("fields")
    # The current Activity guide documents fields as optional (despite the older OpenAPI required list).
    if fields is not None or "activity" not in verb:
        if not isinstance(fields, list) or not fields or not all(isinstance(item, str) and item for item in fields):
            raise ValueError("fields must be a nonempty array of field names")
        body["fields"] = fields
    return body


def _create_body(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    filters = params.get("filter")
    if not isinstance(filters, dict) or not filters:
        raise ValueError("filter must be a nonempty vendor filter object")
    body: dict[str, Any] = {"filter": filters, **_create_fields(verb, params)}
    if params.get("format") is not None:
        value = _require_str(params, "format")
        if value not in {"CSV", "TSV", "SSV"}:
            raise ValueError("extract format must be CSV, TSV or SSV")
        body["format"] = value
    if params.get("column_header_names") is not None:
        headers = params["column_header_names"]
        if not isinstance(headers, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in headers.items()):
            raise ValueError("column_header_names must map field names to string headers")
        body["columnHeaderNames"] = headers
    return body


def _list_query(params: dict[str, Any]) -> dict[str, Any]:
    query = {wire: params[key] for key, wire in (("status", "status"), ("batch_size", "batchSize"), ("next_page_token", "nextPageToken")) if params.get(key) is not None}
    if "batchSize" in query and not 1 <= _integer(params, "batch_size", 1) <= 300:
        raise ValueError("batch_size exceeds vendor maximum 300")
    if "status" in query and (not isinstance(query["status"], list) or not all(isinstance(item, str) for item in query["status"])):
        raise ValueError("status must be an array of vendor job statuses")
    return query


def _download_headers(params: dict[str, Any]) -> dict[str, str]:
    if params.get("range") is None:
        return {}
    value = _require_str(params, "range")
    # Adobe delegates Range to HTTP semantics, including list whitespace and
    # case-insensitive units. Let the vendor interpret it after injection validation.
    validate_header("Range", value)
    return {"Range": value}


def prepare(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate only documented shapes; nested vendor filter options pass intact."""
    method, path, operation = ENDPOINTS[verb]
    request: dict[str, Any] = {"method": method, "path": _path(path, params), "operation": operation}
    if operation == "import":
        request["query"] = _import_query(verb, params)
        request["multipart_files"] = prepare_files({"file": params.get("file")})
    elif operation == "create":
        request["json_body"] = _create_body(verb, params)
    elif operation == "list":
        request["query"] = _list_query(params)
    elif operation == "download":
        request["headers"] = _download_headers(params)
    return request


def _check_response(client: MarketoClient, status: int, body: Any) -> None:
    if status >= 500 or status in {401, 403} and (not isinstance(body, dict) or not body.get("errors")):
        raise MarketoTransportError(status)
    if isinstance(body, dict) and not body.get("success"):
        raise MarketoEnvelopeError(body)
    if status >= 400 or status < 200 or status >= 300:
        raise MarketoTransportError(status)


def execute(client: MarketoClient, request: dict[str, Any]) -> dict[str, Any]:
    """Retain vendor envelope, terminal states, row reports and continuation hints."""
    response, correlation_id = client.request_raw(
        request["method"],
        request["path"],
        query=request.get("query"),
        json_body=request.get("json_body"),
        headers=request.get("headers"),
        multipart_files=request.get("multipart_files"),
    )
    body = decode_body(response)
    _check_response(client, response.status_code, body)
    if request["operation"] == "download":
        return client.response_payload(response, correlation_id)
    if not isinstance(body, dict) or not isinstance(body.get("result"), list):
        raise ValueError("Marketo Bulk response omitted its result array")
    return body
