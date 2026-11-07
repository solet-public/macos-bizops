"""One tenant-bound REST operation, executed by the existing deferred worker."""

from __future__ import annotations

import base64
import logging
import re
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
    REQUEST_SENSITIVE_KEYS,
)
from .errors import classify_zuora_response
from .http_client import ZuoraClient

logger = logging.getLogger(PLUGIN_NAME)


def _sensitive(key: str) -> bool:
    return key.lower().replace("-", "").replace("_", "") in REQUEST_SENSITIVE_KEYS


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
    parsed = urlsplit(path)
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


def _secrets(value: Any) -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, item in value.items():
            if _sensitive(str(key)) and isinstance(item, str) and item:
                found.append(item)
            else:
                found.extend(_secrets(item))
        return found
    if isinstance(value, list):
        return [secret for item in value for secret in _secrets(item)]
    return []


def _encoded_pattern(value: bytes) -> bytes:
    """Match raw or percent-escaped UTF-8 bytes, including form '+' spaces.

    Hex digits alone are case-insensitive; credential letters remain exact.
    Allow mixed escaping rather than enumerating only all-upper/all-lower URLs.
    """
    parts: list[bytes] = []
    for byte in value:
        hex_pair = "".join(f"[{char}{char.upper()}]" if char.isalpha() else char for char in f"{byte:02x}")
        alternatives = [re.escape(bytes([byte])), b"%" + hex_pair.encode("ascii")]
        if byte == ord(" "):
            alternatives.append(rb"\+")
        parts.append(b"(?:" + b"|".join(alternatives) + b")")
    return b"".join(parts)


def _credential_pattern(secrets: list[str]) -> re.Pattern[bytes] | None:
    """Build request-local matchers; never retain credentials in a global cache."""
    variants: set[bytes] = set()
    for secret in secrets:
        if secret:
            raw = secret.encode("utf-8")
            variants.add(raw)
            for encoded in (base64.b64encode(raw), base64.urlsafe_b64encode(raw)):
                variants.update((encoded, encoded.rstrip(b"=")))
    if not variants:
        return None
    return re.compile(b"|".join(_encoded_pattern(value) for value in sorted(variants, key=lambda value: (len(value), value), reverse=True)))


def _redact_value(value: Any, pattern: re.Pattern[bytes] | None) -> Any:
    if isinstance(value, dict):
        return {_redact_value(str(key), pattern): REQUEST_REDACTION if _sensitive(str(key)) else _redact_value(item, pattern) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_value(item, pattern) for item in value]
    if isinstance(value, str) and pattern is not None:
        return pattern.sub(REQUEST_REDACTION.encode(), value.encode("utf-8")).decode("utf-8")
    return value


def _redact(value: Any, secrets: list[str]) -> Any:
    return _redact_value(value, _credential_pattern(secrets))


def secret_values(value: Any) -> list[str]:
    """Values held under secret-named keys anywhere in a JSON-shaped value."""
    return _secrets(value)


def redact(value: Any, secrets: list[str]) -> Any:
    """Redact raw, percent/form-encoded and base64 credential variants, plus secret-named keys."""
    return _redact(value, secrets)


def redact_bytes(value: bytes, secrets: list[str]) -> bytes:
    """Redact credential byte variants from downloaded content before it is written."""
    return _redact_binary(value, secrets)


def audit_path(path: str, query: dict[str, Any] | None) -> str:
    """Relative path with every query value replaced, safe for the audit log."""
    return _audit_path(path, query)


def _redact_binary(value: bytes, secrets: list[str]) -> bytes:
    pattern = _credential_pattern(secrets)
    return pattern.sub(REQUEST_REDACTION.encode(), value) if pattern is not None else value


def _audit_path(path: str, query: dict[str, Any] | None) -> str:
    parsed = urlsplit(path)
    keys = [key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]
    keys.extend((query or {}).keys())
    return urlunsplit(("", "", parsed.path, urlencode([(key, REQUEST_REDACTION) for key in keys]), ""))


def _validate_target(client: ZuoraClient, method: str, path: str, query: dict[str, Any] | None, headers: dict[str, str]) -> None:
    base = client._http.base_url  # noqa: SLF001 — same package, tenant configured by resolver
    target = client._http.build_request(method, path, params=query).url  # noqa: SLF001
    if (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
        raise ValueError("request must remain on the registered tenant")
    if any(key.lower() == "host" and value.lower() != base.netloc.decode().lower() for key, value in headers.items()):
        raise ValueError("Host must identify the registered tenant")


def _decode_body(response: httpx.Response, secrets: list[str]) -> tuple[Any, str]:
    try:
        return response.json(), "json"
    except ValueError:
        if response.headers.get("content-type", "").startswith(("text/", "application/xml")):
            return response.text, "text"
        content = _redact_binary(response.content, secrets)
        return base64.b64encode(content).decode("ascii"), "base64"


def _response_data(response: httpx.Response, secrets: list[str]) -> dict[str, Any]:
    body, body_encoding = _decode_body(response, secrets)
    secrets.extend(_secrets(body))
    # Binary bytes were scrubbed before wrapping. Scrubbing the wrapper itself
    # could replace coincidental base64 text and make it no longer decodable.
    safe_body = body if body_encoding == "base64" else _redact(body, secrets)
    headers = _redact({key: value for key, value in response.headers.items() if key in REQUEST_RESPONSE_HEADERS}, secrets)
    pagination = {key: safe_body[key] for key in REQUEST_PAGINATION_KEYS if isinstance(safe_body, dict) and key in safe_body}
    if "link" in headers:
        pagination["link"] = headers["link"]
    error = None
    if response.is_error:
        code, _ = classify_zuora_response(response, is_query=False)
        error = {"code": code, "message": "Zuora returned an HTTP error; inspect status and redacted body."}
    return {"status": response.status_code, "headers": headers, "body": safe_body, "body_encoding": body_encoding, "pagination": pagination, "error": error}


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
    secrets = [*client.credential_values(), *_secrets(params)]
    secrets.extend(value for key, value in parse_qsl(urlsplit(path).query) if _sensitive(key) and value)
    correlation_id = str(uuid4())
    started, status = time.monotonic(), None
    try:
        response = client.request(method, path, params=query, headers=headers, json=params.get("body"))
        status = response.status_code
        # Redact the minted credential even when the vendor echoes it.
        secrets.extend(client.credential_values())
        return {**_response_data(response, secrets), "correlation_id": correlation_id}
    finally:
        # OAuth may have succeeded even when the subsequent request raised.
        secrets.extend(client.credential_values())
        # No headers, payloads, query values, tenant host or exception strings.
        logger.info(
            "zuora.request method=%s path=%s status=%s duration_ms=%.3f correlation_id=%s",
            method,
            _redact(_audit_path(path, query), secrets),
            status,
            (time.monotonic() - started) * 1000,
            correlation_id,
        )
