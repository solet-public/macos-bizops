"""Typed Folder and My Token endpoints from Adobe's Asset OpenAPI reference.

Verified against AdobeDocs/marketo-apis/static/swagger-asset.json, 2026-11-07.
Writes are form encoded, including embedded JSON parent references. Vendor
RBAC and workspace access authorize calls; these endpoints offer no CAS/ETag.
Read back before retrying an uncertain write. Job history retains each result.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Final

from .errors import MarketoEnvelopeError, MarketoTransportError
from .http_client import MarketoClient
from .marketing_actions import _gate_and_check_parent, _require_str, _resolve_effective_limit, _write_records_tsv

# Adobe's hard per-call maximum, not an additional plugin capability limit.
ASSET_PAGE_CAP: Final[int] = 200
TOKEN_NAME_CAP: Final[int] = 50
FOLDER_DESCRIPTION_CAP: Final[int] = 2000
FOLDER_TYPES: Final[frozenset[str]] = frozenset({"Folder", "Program"})
TOKEN_TYPES: Final[frozenset[str]] = frozenset({"date", "number", "rich text", "score", "sfdc campaign", "text"})
BASE: Final[str] = "/rest/asset/v1"
# verb -> (method, path, paged). All ten published endpoints are registered.
ENDPOINTS: Final[dict[str, tuple[str, str, bool]]] = {
    "get_asset_folder": ("GET", "/folder/{id}.json", False),
    "get_asset_folder_by_name": ("GET", "/folder/byName.json", False),
    "list_asset_folders": ("GET", "/folders.json", True),
    "get_asset_folder_content": ("GET", "/folder/{id}/content.json", True),
    "create_asset_folder": ("POST", "/folders.json", False),
    "update_asset_folder": ("POST", "/folder/{id}.json", False),
    "delete_asset_folder": ("POST", "/folder/{id}/delete.json", False),
    "get_asset_tokens": ("GET", "/folder/{id}/tokens.json", False),
    "set_asset_token": ("POST", "/folder/{id}/tokens.json", False),
    "delete_asset_token": ("POST", "/folder/{id}/tokens/delete.json", False),
}


def is_write(verb: str) -> bool:
    return ENDPOINTS[verb][0] == "POST"


def result_type(verb: str) -> str:
    return f"marketo_{verb}_result"


def _integer(params: dict[str, Any], key: str, minimum: int = 0) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"'{key}' must be an integer >= {minimum}")
    return value


def _choice(params: dict[str, Any], key: str, choices: frozenset[str]) -> str:
    value = _require_str(params, key)
    if value not in choices:
        raise ValueError(f"'{key}' must be one of {sorted(choices)}")
    return value


def _reference(value: Any, key: str) -> str:
    if not isinstance(value, dict):
        raise ValueError(f"'{key}' must be a folder reference object with id and type")
    _integer(value, "id", 1)
    _choice(value, "type", FOLDER_TYPES)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _text(params: dict[str, Any], key: str, *, cap: int | None = None) -> str:
    value = params.get(key)
    if not isinstance(value, str):
        raise ValueError(f"'{key}' must be a string (an empty value clears it)")
    if cap is not None and len(value) > cap:
        raise ValueError(f"'{key}' exceeds the vendor maximum of {cap} characters")
    return value


def _folder_query(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if verb in {"get_asset_folder", "get_asset_folder_content"} or params.get("folder_type") is not None:
        query["type"] = _choice(params, "folder_type", FOLDER_TYPES)
    if verb == "get_asset_folder_by_name":
        query["name"] = _require_str(params, "name")
        if params.get("root") is not None:
            query["root"] = _require_str(params, "root")
            query["type"] = _choice(params, "folder_type", FOLDER_TYPES)
    if verb == "list_asset_folders":
        if params.get("root") is not None:
            query["root"] = _reference(params["root"], "root")
        if params.get("max_depth") is not None:
            query["maxDepth"] = _integer(params, "max_depth")
    if params.get("workspace") is not None:
        query["workSpace"] = _require_str(params, "workspace")
    return query


def _folder_body(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    if verb == "create_asset_folder":
        body = {"name": _require_str(params, "name"), "parent": _reference(params.get("parent"), "parent")}
    else:
        body = {"type": _choice(params, "folder_type", FOLDER_TYPES)}
        if verb == "update_asset_folder" and params.get("name") is not None:
            body["name"] = _require_str(params, "name")
    if params.get("description") is not None:
        body["description"] = _text(params, "description", cap=FOLDER_DESCRIPTION_CAP)
    if params.get("is_archive") is not None:
        if not isinstance(params["is_archive"], bool):
            raise ValueError("'is_archive' must be a boolean")
        body["isArchive"] = str(params["is_archive"]).lower()
    return body


def _token_parameters(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if verb != "get_asset_tokens" or params.get("folder_type") is not None:
        values["folderType"] = _choice(params, "folder_type", FOLDER_TYPES)
    if verb != "get_asset_tokens":
        values["name"] = _require_str(params, "name")
        if len(values["name"]) > TOKEN_NAME_CAP:
            raise ValueError(f"'name' exceeds the vendor maximum of {TOKEN_NAME_CAP} characters")
        # Deletion also accepts types of existing tokens; only creation has an enum.
        values["type"] = _choice(params, "token_type", TOKEN_TYPES) if verb == "set_asset_token" else _require_str(params, "token_type")
    if verb == "set_asset_token":
        values["value"] = _text(params, "value")
    return values


def prepare(verb: str, params: dict[str, Any], path_gate: Callable[[str], str]) -> dict[str, Any]:
    """Validate vendor inputs and export containment before creating a job."""
    method, path, paged = ENDPOINTS[verb]
    if "{id}" in path:
        path = path.replace("{id}", str(_integer(params, "folder_id", 1)))
    request: dict[str, Any] = {"method": method, "path": BASE + path, "paged": paged}
    if "token" in verb:
        values = _token_parameters(verb, params)
    elif method == "GET":
        values = _folder_query(verb, params)
    else:
        values = _folder_body(verb, params)
    request["query" if method == "GET" else "form_body"] = values
    if method == "GET":
        request["resolved_path"] = _gate_and_check_parent(path_gate, _require_str(params, "output_tsv_path"))
    if paged:
        maximum = _integer(params, "max_return", 1) if params.get("max_return") is not None else ASSET_PAGE_CAP
        if maximum > ASSET_PAGE_CAP:
            raise ValueError(f"'max_return' must be <= the vendor maximum of {ASSET_PAGE_CAP}")
        values.update(maxReturn=maximum, offset=_integer(params, "offset") if params.get("offset") is not None else 0)
        request["effective_limit"] = _resolve_effective_limit(params, verb=verb)
    return request


def _call(client: MarketoClient, request: dict[str, Any], query: dict[str, Any]) -> dict[str, Any]:
    response = client.request(request["method"], request["path"], query=query, form_body=request.get("form_body"))
    payload = response["body"]
    status = response["status"]
    if status >= 500 or not isinstance(payload, dict):
        raise MarketoTransportError(status)
    if status in {401, 403} and not payload.get("errors"):
        raise MarketoTransportError(status)
    if not payload.get("success"):
        raise MarketoEnvelopeError(payload)
    if status >= 400:
        raise MarketoTransportError(status)
    if not isinstance(payload.get("result"), list):
        raise ValueError("Marketo Asset response omitted the result array")
    return payload


def _read_pages(client: MarketoClient, request: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]], bool]:
    query = dict(request.get("query", {}))
    records: list[Any] = []
    envelopes: list[dict[str, Any]] = []
    truncated = False
    while True:
        payload = _call(client, request, query)
        page = payload["result"]
        envelopes.append({key: value for key, value in payload.items() if key != "result"})
        records.extend(page)
        if not request["paged"]:
            break
        has_more = len(page) >= query["maxReturn"]
        if not has_more:
            break
        query["offset"] += len(page)
        if len(records) >= request["effective_limit"]:
            truncated = True
            break
    return records, envelopes, truncated


def execute(client: MarketoClient, request: dict[str, Any]) -> dict[str, Any]:
    """Execute form writes or paginated reads, retaining redacted envelope metadata."""
    if request["method"] == "POST":
        return _call(client, request, {})
    records, envelopes, truncated = _read_pages(client, request)
    limit = request.get("effective_limit", len(records))
    handle = _write_records_tsv(client.redact(records[:limit]), request["resolved_path"])
    handle.update(truncated=truncated or len(records) > limit, envelopes=envelopes)
    # Resuming from the consumed page boundary would skip rows if the limit cut a page.
    if handle["truncated"] and request["paged"]:
        handle["next_offset"] = request["query"]["offset"] + limit
    return handle
