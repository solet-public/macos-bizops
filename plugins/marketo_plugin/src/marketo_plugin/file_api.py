"""All five published Files endpoints, including multipart image/file writes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Final

from . import asset_api
from .marketing_actions import _gate_and_check_parent, _require_str, _resolve_effective_limit
from .multipart import prepare_files

ENDPOINTS: Final[dict[str, tuple[str, str, bool]]] = {
    "get_asset_file": ("GET", "/file/{id}.json", False),
    "get_asset_file_by_name": ("GET", "/file/byName.json", False),
    "list_asset_files": ("GET", "/files.json", True),
    "create_asset_file": ("POST", "/files.json", False),
    "update_asset_file_content": ("POST", "/file/{id}/content.json", False),
}


def _values(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    if verb in {"get_asset_file_by_name", "create_asset_file"}:
        values["name"] = _require_str(params, "name")
    if verb == "create_asset_file" or params.get("folder") is not None:
        values["folder"] = asset_api._reference(params.get("folder"), "folder")
    if verb == "create_asset_file":
        if params.get("description") is not None:
            values["description"] = asset_api._text(params, "description")
        if params.get("insert_only") is not None:
            if not isinstance(params["insert_only"], bool):
                raise ValueError("insert_only must be a boolean")
            values["insertOnly"] = str(params["insert_only"]).lower()
    return values


def _paging(params: dict[str, Any]) -> dict[str, Any]:
    maximum = asset_api._integer(params, "max_return", 1) if params.get("max_return") is not None else asset_api.ASSET_PAGE_CAP
    if maximum > asset_api.ASSET_PAGE_CAP:
        raise ValueError("max_return exceeds vendor maximum 200")
    return {"maxReturn": maximum, "offset": asset_api._integer(params, "offset") if params.get("offset") is not None else 0}


def prepare(verb: str, params: dict[str, Any], path_gate: Callable[[str], str]) -> dict[str, Any]:
    """Prepare a JSON-safe job; files and images use the same vendor endpoints."""
    method, path, paged = ENDPOINTS[verb]
    if "{id}" in path:
        path = path.replace("{id}", str(asset_api._integer(params, "file_id", 1)))
    request: dict[str, Any] = {"method": method, "path": asset_api.BASE + path, "paged": paged}
    values = _values(verb, params)
    if method == "POST":
        request["multipart_files"] = prepare_files({"file": params.get("file")})
        request["form_body"] = values
    else:
        request["query"] = values
        request["resolved_path"] = _gate_and_check_parent(path_gate, _require_str(params, "output_tsv_path"))
    if paged:
        values.update(_paging(params))
        request["effective_limit"] = _resolve_effective_limit(params, verb=verb)
    return request
