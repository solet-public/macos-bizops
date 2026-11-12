"""Generic request preparation and execution; no per-family authorization policy."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from .constants import REQUEST_REDACTION
from .multipart import prepare_files

if TYPE_CHECKING:
    from .http_client import MarketoClient


def validate_path(path: str) -> None:
    """Keep bearer credentials on the registered tenant, including URL parser edge cases."""
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or path.startswith("//") or "\\" in path or any(c in path for c in "\r\n\t"):
        raise ValueError("request path must be relative to the registered Marketo instance")


def required_string(params: dict[str, Any], name: str) -> str:
    """Validate a required string without reflecting caller data in an error."""
    value = params.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} is a required string")
    return value


def validate_header(name: Any, value: Any) -> None:
    """Header syntax is a credential-integrity boundary."""
    if not isinstance(name, str) or not isinstance(value, str):
        raise ValueError("headers must contain string names and values")
    if any(c in name or c in value for c in "\r\n"):
        raise ValueError("request headers must not contain CR/LF")


def prepare_headers(headers: Any) -> dict[str, str]:
    if headers is None:
        return {}
    if not isinstance(headers, dict):
        raise ValueError("headers must contain string names and values")
    for name, value in headers.items():
        validate_header(name, value)
    # Caller Authorization is never used: the client injects the registered credential.
    return {name: value for name, value in headers.items() if name.lower() != "authorization"}


def prepare_request(params: dict[str, Any]) -> dict[str, Any]:
    """Validate shape and credential boundaries without making a vendor call."""
    path = required_string(params, "path")
    method = required_string(params, "method")
    validate_path(path)
    files = prepare_files(params.get("multipart_files"))
    if params.get("json_body") is not None and (params.get("form_body") is not None or files is not None):
        raise ValueError("use json_body or form_body/multipart_files, not both")
    return {
        "method": method,
        "path": path,
        "query": params.get("query"),
        "json_body": params.get("json_body"),
        "form_body": params.get("form_body"),
        "headers": prepare_headers(params.get("headers")),
        "multipart_files": files,
    }


def validate_target(origin: httpx.URL, target: httpx.URL, headers: dict[str, str]) -> None:
    """Compare the parsed transport target and any explicit Host to the registered origin."""
    if (target.scheme, target.host, target.port) != (origin.scheme, origin.host, origin.port):
        raise ValueError("request target must match the registered Marketo instance")
    for key, value in headers.items():
        if key.lower() == "host" and value.lower() != origin.netloc.decode().lower():
            raise ValueError("request Host must match the registered Marketo instance")


def decode_body(response: httpx.Response) -> Any:
    """Non-JSON vendor content is data, including bulk file downloads."""
    try:
        return response.json()
    except ValueError:
        return response.text


def first_error_code(body: Any) -> str:
    errors = body.get("errors") if isinstance(body, dict) else None
    if not isinstance(errors, list) or not errors or not isinstance(errors[0], dict):
        return ""
    return str(errors[0].get("code", ""))


def response_data(response: httpx.Response, body: Any, correlation_id: str) -> dict[str, Any]:
    """The response as received: status, body, selected headers and control keys."""
    result = {
        "status": response.status_code,
        "body": body,
        "correlation_id": correlation_id,
        "retry_after": response.headers.get("Retry-After"),
        "content_range": response.headers.get("Content-Range"),
    }
    if isinstance(body, dict):
        result.update({key: body[key] for key in ("nextPageToken", "success", "errors") if key in body})
    else:
        # Preserve download bytes, including a Range cutting a UTF-8 character.
        result["body_base64"] = base64.b64encode(response.content).decode()
    return result


def audit_path(path: str) -> str:
    """The path with every query value omitted, including query parameters embedded in the path."""
    parsed = urlsplit(path)
    query = urlencode([(key, REQUEST_REDACTION) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    return parsed.path + ("?" + query if query else "")


def execute_request(client: MarketoClient, request: dict[str, Any]) -> dict[str, Any]:
    """Run off the dispatch path using the shared client and token cache."""
    return client.request(
        request["method"],
        request["path"],
        query=request.get("query"),
        json_body=request.get("json_body"),
        form_body=request.get("form_body"),
        headers=request.get("headers"),
        multipart_files=request.get("multipart_files"),
    )
