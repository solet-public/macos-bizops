"""Jira error classification — typed codes + topology-safe messages.

Maps a pycontribs ``jira.JIRAError`` (which carries ``status_code``, ``text``,
and ``url``) to one of the plugin's ``jira.*`` error codes plus a message.

TOPOLOGY HYGIENE (umbrella design §1.6 / §2.4): ``str(JIRAError)`` embeds
``exc.url`` — which is the site host, exactly the topology the design forbids in
results — so this module NEVER stringifies the exception. Generic classes
(auth / permission / rate-limit / catch-all) return a fixed message with no
detail. Detail-allowed classes (not-found / bad-request) build their message
from ``exc.text`` ONLY — the Jira RESPONSE BODY, which describes the caller's own
query/object, not our host — parsing ``errorMessages``/``errors`` out of it when
the body is JSON, and falling back to a generic message when it is empty.

Kept out of ``plugin.py`` so the topology-leak smoke can exercise the classifier
directly without importing the platform.
"""

from __future__ import annotations

import json
from typing import Any

from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_BAD_REQUEST,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
)

_HTTP_BAD_REQUEST: int = 400
_HTTP_UNAUTHORIZED: int = 401
_HTTP_FORBIDDEN: int = 403
_HTTP_NOT_FOUND: int = 404
_HTTP_TOO_MANY_REQUESTS: int = 429


class JiraServiceError(Exception):
    """A typed plugin-internal fault (e.g. blob storage unavailable at point-of-use).

    Carries a ``jira.*``/service code the plugin's ``_run`` surfaces verbatim. The
    message is diagnostic-only; outward errors use the fixed safe_error mapping.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def classify_jira_error(exc: Any) -> tuple[str, str]:
    """Return ``(jira.* code, topology-safe message)`` for a JIRAError."""
    status = _status_code(exc)
    if status == _HTTP_UNAUTHORIZED:
        return (
            ERROR_AUTH_FAILED,
            "Jira authentication failed. Verify the service-account email + API token (the token may have expired or been revoked).",
        )
    if status == _HTTP_FORBIDDEN:
        return (
            ERROR_PERMISSION_DENIED,
            "Jira denied access. The service account lacks permission for this project or operation.",
        )
    if status == _HTTP_TOO_MANY_REQUESTS:
        return ERROR_RATE_LIMITED, "Jira rate limit exceeded. Retry after a short delay."
    if status == _HTTP_NOT_FOUND:
        return ERROR_NOT_FOUND, _detail("Jira resource not found (404)", exc)
    if status == _HTTP_BAD_REQUEST:
        return (
            ERROR_BAD_REQUEST,
            _detail(
                "Jira rejected the request (400) — on jql_search this usually means malformed JQL",
                exc,
            ),
        )
    # Catch-all, incl. connection-class faults whose str() would carry the host.
    return ERROR_API_ERROR, f"Jira API error (status {status})."


def _status_code(exc: Any) -> int | None:
    value = getattr(exc, "status_code", None)
    return value if isinstance(value, int) else None


def _detail(prefix: str, exc: Any) -> str:
    """Build a detail message from the RESPONSE BODY only (never str(exc))."""
    text = _clean_text(exc)
    return f"{prefix}: {text}" if text else f"{prefix}."


def _clean_text(exc: Any) -> str:
    text = getattr(exc, "text", None)
    if not isinstance(text, str) or not text:
        return ""
    parsed = _parse_error_messages(text)
    return parsed if parsed else text.strip()


def _parse_error_messages(text: str) -> str:
    """Extract Jira's ``errorMessages``/``errors`` when the body is a JSON object."""
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    parts: list[str] = []
    messages = payload.get("errorMessages")
    if isinstance(messages, list):
        parts.extend(str(m) for m in messages if isinstance(m, str) and m)
    errors = payload.get("errors")
    if isinstance(errors, dict):
        parts.extend(f"{k}: {v}" for k, v in errors.items() if isinstance(v, str))
    return "; ".join(parts)


_SAFE_ERRORS: dict[str, str] = {
    "jira.invalid_params": "Invalid Jira parameters; check the process contract and vendor operation schema.",
    "jira.not_configured": "Jira credentials or a required service are unavailable; check the site binding.",
    "jira.auth_failed": "Jira authentication failed; reconnect the site or correct its credentials.",
    "jira.permission_denied": "Jira denied this operation under the current account permissions.",
    "jira.not_found": "Jira resource not found.",
    "jira.bad_request": "Jira rejected the request; check the vendor operation parameters.",
    "jira.conflict": "Jira reported a conflict; read the resource before correcting it.",
    "jira.rate_limited": "Jira rate limit exceeded; retry after a delay.",
    "jira.api_error": "Jira operation failed; no vendor or exception details are returned.",
    "jira.unknown_site": "Unknown site; call sites(operation=list) for bound sites.",
    "jira.binding_changed": "Site binding changed after queuing; inspect the target before resubmitting.",
    "jira.stale_binding": "Stale expected_version; read the site index before retrying the correction.",
    "jira.vault_error": "Jira vault operation failed; no secret details are returned.",
    "jira.oauth_state": "OAuth state is missing, invalid or expired; begin again.",
    "jira.oauth_resources": "OAuth accessible resources do not match the requested Jira site.",
    "jira.token_expiring": "Jira credentials are expiring; rotate them.",
    "address_book_service_not_available": "The address-book service is unavailable.",
    "blob_storage_service_not_available": "The blob-storage service is unavailable.",
}


def safe_error(code: str) -> dict[str, str]:
    """Fixed text is safe even before the dispatcher's complete secret set exists.

    Exception messages, vendor details and unrecognized codes are never echoed.
    This also contains conversion/parsing faults while interpreting raw responses.
    """
    if code not in _SAFE_ERRORS:
        code = ERROR_API_ERROR
    return {"code": code, "message": _SAFE_ERRORS[code]}
