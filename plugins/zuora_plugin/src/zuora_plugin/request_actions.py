"""One tenant-bound REST operation, executed by the existing deferred worker."""

from __future__ import annotations

import base64
import logging
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from .constants import (
    PLUGIN_NAME,
    REQUEST_PAGINATION_KEYS,
    REQUEST_REDACTION,
    REQUEST_RESPONSE_HEADERS,
)
from .errors import classify_zuora_response
from .http_client import ZuoraClient

logger = logging.getLogger(PLUGIN_NAME)


def validate_request(params: dict[str, Any]) -> None:
    """Check typed input and credential/tenant boundaries before job persistence."""
    method, path = params.get("method"), params.get("path")
    if not isinstance(method, str) or not method or any(c.isspace() for c in method):
        raise ValueError("method must be an HTTP method string")
    _validate_path(path)
    _validate_headers(params.get("headers"))
    if params.get("query") is not None and not isinstance(params["query"], dict):
        raise ValueError("query must be an object")


def _validate_path(path: Any) -> None:
    if not isinstance(path, str) or not path or "\\" in path or any(c in path for c in "\r\n\t"):
        raise ValueError("path must be a relative tenant path")
    _validate_relative_url(path)


def _validate_relative_url(path: str) -> None:
    try:
        parsed = urlsplit(path)
    except ValueError:
        raise ValueError("path must be a valid relative tenant URL") from None
    if parsed.scheme or parsed.netloc or path.startswith("//") or path != path.strip():
        raise ValueError("path must be relative to the registered tenant, never an absolute URL")


def _validate_headers(headers: Any) -> None:
    if headers is None:
        return
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object of strings")
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or any(c in key + value for c in "\r\n"):
            raise ValueError("header names and values must be strings without CR/LF")
        if key.lower() == "authorization":
            raise ValueError("Authorization is supplied only by the registered credential")


def audit_path(path: str, query: dict[str, Any] | None) -> str:
    """Relative path with every query value replaced, safe for the audit log."""
    return _audit_path(path, query)


def _audit_path(path: str, query: dict[str, Any] | None) -> str:
    try:
        parsed = urlsplit(path)
    except ValueError:
        return "[invalid tenant path]"
    keys = [key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    keys.extend((query or {}).keys())
    return urlunsplit(("", "", parsed.path, urlencode([(key, REQUEST_REDACTION) for key in keys]), ""))


def _validate_target(client: ZuoraClient, method: str, path: str, query: dict[str, Any] | None, headers: dict[str, str]) -> None:
    base = client._http.base_url  # noqa: SLF001 — same package, tenant configured by resolver
    try:
        target = client._http.build_request(method, path, params=query).url  # noqa: SLF001
    except (ValueError, httpx.InvalidURL):
        raise ValueError("request must contain a valid relative tenant URL") from None
    if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
        raise ValueError("request must remain on the registered tenant")
    if any(key.lower() == "host" and value.lower() != base.netloc.decode().lower() for key, value in headers.items()):
        raise ValueError("Host must identify the registered tenant")


def _decode_body(response: httpx.Response) -> tuple[Any, str]:
    """The finished body (binary already wrapped) and its encoding."""
    try:
        return response.json(), "json"
    except ValueError:
        if response.headers.get("content-type", "").startswith(("text/", "application/xml")):
            return response.text, "text"
        return base64.b64encode(response.content).decode("ascii"), "base64"


def _response_data(response: httpx.Response, correlation_id: str) -> dict[str, Any]:
    """Interpret the response; every field is returned as received."""
    body, body_encoding = _decode_body(response)
    headers = {key: value for key, value in response.headers.items() if key in REQUEST_RESPONSE_HEADERS}
    pagination = {key: body[key] for key in REQUEST_PAGINATION_KEYS if isinstance(body, dict) and key in body}
    if "link" in headers:
        pagination["link"] = headers["link"]
    error = None
    if response.is_error:
        code, _ = classify_zuora_response(response, is_query=False)
        error = {"code": code, "message": "Zuora returned an HTTP error; inspect status and body."}
    return {"status": response.status_code, "headers": headers, "pagination": pagination, "error": error, "body": body, "body_encoding": body_encoding, "correlation_id": correlation_id}


def request(client: ZuoraClient, params: dict[str, Any]) -> dict[str, Any]:
    """Forward a single operation and preserve vendor outcomes as response data.

    HTTP failure status is data: completing the job means the request finished,
    not that a write succeeded. No response, redirect, or pagination link triggers
    another vendor call. In particular, writes are never automatically replayed.
    """
    validate_request(params)
    method, path = params["method"], params["path"]
    headers: dict[str, str] = params.get("headers") or {}
    query: dict[str, Any] | None = params.get("query")
    _validate_target(client, method, path, query, headers)
    correlation_id = str(uuid4())
    started, status = time.monotonic(), None
    try:
        response = client.request(method, path, params=query, headers=headers, json=params.get("body"))
        status = response.status_code
    finally:
        # No headers, payloads, query values, tenant host or exception strings.
        audit = f"zuora.request method={method} path={_audit_path(path, query)} status={status} duration_ms={(time.monotonic() - started) * 1000:.3f} correlation_id={correlation_id}"
        logger.info("%s", audit)
    return _response_data(response, correlation_id)
