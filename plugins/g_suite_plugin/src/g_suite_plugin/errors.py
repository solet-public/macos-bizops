"""Map Google API faults to this plugin's typed ``gsuite.*`` error codes.

Messages carry Google's own ``reason`` and ``message`` parsed from the error
body, and the missing-scope case names the scope and the reconnect step instead
of an opaque 403. Callers redact the message before it leaves the plugin.
"""

from __future__ import annotations

import json
from typing import Any, cast

from googleapiclient.errors import HttpError

from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_EXPIRED,
    ERROR_INVALID_PARAMS,
    ERROR_MISSING_SCOPE,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
    GMAIL_FULL_MAILBOX_SCOPE,
)
from .oauth.token_store import TokenStoreError

_SCOPE_REASONS = frozenset({"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"})

# Verbs whose Google method accepts ONLY the full mailbox scope.
FULL_MAILBOX_VERBS = frozenset({"gmail_delete_message", "gmail_batch_delete_messages", "gmail_delete_thread"})


def http_status(exc: HttpError) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    status = getattr(getattr(exc, "resp", None), "status", None)
    return int(status) if isinstance(status, (int, str)) and str(status).isdigit() else None


def google_error(exc: HttpError) -> tuple[set[str], str]:
    """(reasons, message) from a Google JSON error body; empty when unparseable."""
    error = _error_object(getattr(exc, "content", b""))
    if not error:
        return set(), ""
    reasons = _reasons(error.get("errors")) | _reasons(error.get("details"))
    message = error.get("message")
    return reasons, message if isinstance(message, str) else ""


def _error_object(content: Any) -> dict[str, Any]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)
    try:
        payload: Any = json.loads(text)
    except ValueError:
        return {}
    error: Any = payload.get("error") if isinstance(payload, dict) else None
    return cast(dict[str, Any], error) if isinstance(error, dict) else {}


def _reasons(items: Any) -> set[str]:
    rows: list[Any] = items if isinstance(items, list) else []  # pyright: ignore[reportUnknownVariableType]
    return {str(item["reason"]) for item in rows if isinstance(item, dict) and isinstance(item.get("reason"), str)}  # pyright: ignore[reportUnknownMemberType]


def missing_scope_message(verb: str, google_message: str, granted: list[str] | None) -> str:
    grant = ", ".join(granted) if granted else "not recorded (connected before scope recording)"
    if verb in FULL_MAILBOX_VERBS:
        need = f"{verb} needs the full Gmail scope {GMAIL_FULL_MAILBOX_SCOPE}, which is requested only on request. Reconnect: run start_interface, then connect_account with gmail_permanent_delete=true (and the same account), open authorize_url and approve. gmail_trash_message / gmail_trash_thread work without it."
    else:
        need = f"{verb} needs a scope this account's grant lacks. Reconnect: run start_interface, then connect_account (same account), open authorize_url and approve the requested access."
    return f"{need} Google said: {google_message or 'insufficient authentication scopes'}. Granted scopes: {grant}."


_STATUS_CODES: dict[int, tuple[str, str]] = {
    401: (ERROR_AUTH_EXPIRED, "Google rejected the access token (401): {detail}. If this repeats, run connect_account for this account again."),
    403: (ERROR_PERMISSION_DENIED, "Google denied access (403, {reasons}): {detail}"),
    404: (ERROR_NOT_FOUND, "Resource not found (404): {detail}"),
    429: (ERROR_RATE_LIMITED, "Google rate limit hit (429): {detail}. Wait and retry."),
    400: (ERROR_INVALID_PARAMS, "Google rejected the request (400, {reasons}): {detail}"),
}


def classify_http_error(exc: HttpError, verb: str = "", granted: list[str] | None = None) -> tuple[str, str]:
    status = http_status(exc)
    reasons, google_message = google_error(exc)
    if status == 403 and reasons & _SCOPE_REASONS:
        return ERROR_MISSING_SCOPE, missing_scope_message(verb, google_message, granted)
    code, template = _STATUS_CODES.get(status or 0, (ERROR_API_ERROR, f"Google API error ({status}): {{detail}}"))
    return code, template.format(detail=google_message or str(exc), reasons=", ".join(sorted(reasons)) or "unspecified")


def classify_exception(exc: Exception, verb: str = "", granted: list[str] | None = None) -> tuple[str, str]:
    """Map any raised exception to (error_code, message); one taxonomy for inline and job paths."""
    if isinstance(exc, ValueError):
        return ERROR_INVALID_PARAMS, str(exc)
    if isinstance(exc, HttpError):
        return classify_http_error(exc, verb, granted)
    code = getattr(exc, "code", None)
    if isinstance(exc, TokenStoreError) or isinstance(code, str):
        return str(code), str(exc)
    return ERROR_API_ERROR, f"Google API call failed: {exc}"
