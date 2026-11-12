"""Named Jira API families, pinned to the vendor operation inventory.

All vendor query/body fields are forwarded. Routing and authentication come
only from the selected site. See vendor_api_coverage.md for advisory corrections
to inconsistent Software query schemas, preserving the original inventory hash.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

from .errors import JiraServiceError
from .http import NO_BODY, Response
from .schema_validation import validate as validate_schema
from .site_context import CURRENT_BINDING
from .sites import SiteStore, authorization

INVENTORY: dict[str, Any] = json.loads(Path(__file__).with_name("vendor_operations.json").read_text())
FILTER_FORMS = {
    ("getAllBoards", "type"): {"scrum", "kanban", "simple"},
    ("getAllSprints", "state"): {"future", "active", "closed"},
}


def operations(family: str) -> str:
    return ", ".join(INVENTORY["families"][family])


def validate(value: Any, schema: dict[str, Any], label: str) -> None:
    validate_schema(value, schema, label, INVENTORY["schemas"])


def _query(value: Any, parameter: dict[str, Any]) -> Any:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        values = [_query(v, {}) for v in value]
        return values if parameter.get("explode", True) else ",".join(str(v) for v in values)
    return value


def _parameter(value: Any, parameter: dict[str, Any], operation: str) -> Any:
    """Apply explicit prose-backed scalar/list corrections before serialization."""
    name = parameter["name"]
    filters = FILTER_FORMS.get((operation, name))
    if filters is not None:
        values = value.split(",") if isinstance(value, str) else value
        validate(values, {"type": "array", "items": {"type": "string", "enum": sorted(filters)}}, "query." + name)
        return ",".join(values)
    # Software spec's fields.items={} object conflicts with its prose field names.
    if name == "fields" and parameter["schema"].get("items", {}).get("type") == "object":
        values = value.split(",") if isinstance(value, str) else value
        validate(values, {"type": "array", "items": {"type": "string"}}, "query.fields")
        return ",".join(values)
    validate(value, parameter["schema"], parameter["in"] + "." + name)
    return _query(value, parameter)


def _inputs(spec: dict[str, Any], params: dict[str, Any], operation: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
    path = spec["path"]
    raw_query = params.get("query", {})
    if not isinstance(raw_query, dict):
        raise ValueError("query must be an object")
    query = dict(raw_query)
    path_params, headers = params.get("path", {}), params.get("headers", {})
    if not all(isinstance(v, dict) for v in (path_params, headers)):
        raise ValueError("path, query and headers must be objects")
    sources = {"path": path_params, "query": query, "header": headers}
    for parameter in spec["parameters"]:
        location, name = parameter["in"], parameter["name"]
        source = sources.get(location, {})
        if name not in source:
            if parameter.get("required"):
                raise ValueError(f"{location}.{name} is required")
            continue
        value = _parameter(source[name], parameter, operation)
        if location == "path":
            path = path.replace("{" + name + "}", quote(str(value), safe=""))
        if location == "query":
            query[name] = value
    if "{" in path:
        raise ValueError("Missing vendor path parameter")
    return path, query, headers


def _headers(headers: dict[Any, Any], auth: str) -> dict[str, str]:
    result = {"Accept": "application/json", "Content-Type": "application/json"}
    for key, value in headers.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", key):
            raise ValueError("Invalid header name")
        if not isinstance(value, str) or "\r" in value or "\n" in value:
            raise ValueError("Header values must be strings without line breaks")
        if key.lower() in ("authorization", "host", "cookie", "proxy-authorization", "content-length"):
            raise ValueError("Authentication and routing headers are managed by the site binding")
        result[key] = value
    result["Authorization"] = auth
    return result


def _body(spec: dict[str, Any], params: dict[str, Any]) -> Any:
    if "body" not in params and "body_json" not in params:
        if spec["body_required"]:
            raise ValueError("body is required")
        return NO_BODY
    body = params.get("body")
    if "body_json" in params:
        if "body" in params:
            raise ValueError("Pass either body or body_json")
        body = json.loads(params["body_json"])
    validate(body, spec["body"], "body")
    return body


def execute(store: SiteStore, site: str, family: str, params: dict[str, Any]) -> dict[str, Any]:
    operation = params.get("operation")
    choices = INVENTORY["families"][family]
    if not isinstance(operation, str) or operation not in choices:
        raise ValueError(f"operation must be one of: {operations(family)}")
    spec = choices[operation]
    path, query, headers = _inputs(spec, params, operation)
    body = _body(spec, params)
    root, auth = authorization(store.credentials(site, CURRENT_BINDING.get()))
    encoded = urlencode({k: _query(v, {}) for k, v in query.items()}, doseq=True)
    url = root + path + ("?" + encoded if encoded else "")
    response = store.http(spec["method"], url, _headers(headers, auth), body)
    check_response(response)
    return {
        "site": site,
        "operation": operation,
        "status": response.status,
        "response": response.data,
        "next_query": next_query(response.data, params.get("query", {})),
        "retry_after": response.headers.get("retry-after"),
    }


def check_response(response: Response) -> None:
    if 200 <= response.status < 300:
        return
    code = {400: "bad_request", 401: "auth_failed", 403: "permission_denied", 404: "not_found", 409: "conflict", 429: "rate_limited"}.get(response.status, "api_error")
    detail = ""
    if response.status in (400, 404, 409) and isinstance(response.data, dict):
        detail = " " + json.dumps(response.data)
    retry = response.headers.get("retry-after")
    if response.status == 429 and retry:
        detail = " Retry-After: " + retry
    raise JiraServiceError("jira." + code, f"Jira returned HTTP {response.status}." + detail)


def next_query(data: Any, query: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if data.get("nextPageToken"):
        return {**query, "nextPageToken": data["nextPageToken"]}
    rows = next((data[k] for k in ("values", "issues", "worklogs") if isinstance(data.get(k), list)), [])
    start = data.get("startAt", query.get("startAt", 0))
    total = data.get("total")
    more = data.get("isLast") is False
    if isinstance(total, int) and isinstance(start, int):
        more = more or start + len(rows) < total
    if more and rows:
        return {**query, "startAt": start + len(rows)}
    return None
