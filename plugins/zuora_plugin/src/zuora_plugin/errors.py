"""Zuora error classification — typed codes + topology-safe messages.

Maps an ``httpx.Response`` (or a raised transport-level exception) to one of
the plugin's ``zuora.*`` error codes plus a message.

TOPOLOGY HYGIENE (umbrella design §1.6/§2.4): auth/connection/rate-limit
classes NEVER echo the raw response body or the request URL (the tenant's
``base_url`` is topology). Detail-allowed classes (object-not-found /
validation-failed / query-failed) build their message from the Zuora
response body's ``reasons`` list ONLY — that describes the caller's own
object/query, not our tenant host.
"""

from __future__ import annotations

from typing import Any

import httpx

from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_OBJECT_NOT_FOUND,
    ERROR_QUERY_FAILED,
    ERROR_RATE_LIMITED,
    ERROR_VALIDATION_FAILED,
)

# Plain int literals rather than ``httpx.codes.*`` members — httpx's stubs
# type each member's literal as a (code, phrase) tuple, which makes a direct
# ``status == httpx.codes.X`` comparison against ``response.status_code``
# (typed ``int``) a pyright reportUnnecessaryComparison false-positive.
_HTTP_UNAUTHORIZED: int = 401
_HTTP_FORBIDDEN: int = 403
_HTTP_NOT_FOUND: int = 404
_HTTP_TOO_MANY_REQUESTS: int = 429


class ZuoraServiceError(Exception):
    """A typed plugin-internal fault (e.g. blob storage unavailable at point-of-use)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def classify_zuora_response(response: httpx.Response, *, is_query: bool) -> tuple[str, str]:
    """Return ``(zuora.* code, topology-safe message)`` for a non-2xx response.

    ``is_query`` selects between ``zuora.query_failed`` (Data Query / ZOQL
    verbs) and ``zuora.validation_failed`` (object CRUD verbs) for the 4xx
    class whose message describes the caller's own input, not our tenant.
    """
    status = response.status_code
    if status == _HTTP_UNAUTHORIZED or status == _HTTP_FORBIDDEN:
        return (
            ERROR_AUTH_FAILED,
            "Zuora authentication failed. Verify the OAuth client_id and client_secret.",
        )
    if status == _HTTP_NOT_FOUND:
        return ERROR_OBJECT_NOT_FOUND, _detail("Zuora object not found (404)", response)
    if status == _HTTP_TOO_MANY_REQUESTS:
        return ERROR_RATE_LIMITED, "Zuora API rate limit exceeded. Retry after a short delay."
    if 400 <= status < 500:
        prefix = "Zuora rejected the query" if is_query else "Zuora rejected the request"
        code = ERROR_QUERY_FAILED if is_query else ERROR_VALIDATION_FAILED
        return code, _detail(f"{prefix} ({status})", response)
    # 5xx and anything else: a connection/server-class fault — generic, never
    # the raw body (could carry tenant-specific diagnostic detail).
    return ERROR_API_ERROR, f"Zuora API error (status {status})."


def vendor_detail(response: httpx.Response) -> str:
    """The caller-input messages from a Zuora error body, joined; empty when none."""
    return _clean_reasons(response)


def _detail(prefix: str, response: httpx.Response) -> str:
    """Build a detail message from the response body's ``reasons`` list ONLY."""
    text = _clean_reasons(response)
    return f"{prefix}: {text}" if text else f"{prefix}."


def _clean_reasons(response: httpx.Response) -> str:
    """Join the caller-input messages Zuora's error envelopes carry.

    Billing APIs use ``reasons[].message``; the Workflow APIs use ``errors`` or
    ``Errors`` (strings or objects with ``message``); Data Query uses a
    top-level ``message``. Nothing else in the body is echoed.
    """
    try:
        payload: Any = response.json()
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    for key in ("reasons", "errors", "Errors"):
        entries = payload.get(key)
        if isinstance(entries, list):
            parts.extend(_entry_message(entry) for entry in entries)
    top_level = payload.get("message")
    if isinstance(top_level, str):
        parts.append(top_level)
    return "; ".join(part for part in parts if part)


def _entry_message(entry: Any) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        message = entry.get("message")
        if isinstance(message, str):
            return message
    return ""
