"""Generic Salesforce calls through the existing pinned-org CLI transport.

No operation policy lives here: Salesforce decides what its credential may do.
Only request framing and credential/tenant integrity are checked locally.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
import uuid
from typing import Any
from urllib.parse import SplitResult, parse_qsl, quote, quote_plus, urlencode, urlsplit, urlunsplit

from .client import SalesforceCliExecutor
from .constants import REDACTED, REQUEST_RESPONSE_HEADERS

logger = logging.getLogger(__name__)
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_SECRET_KEY = re.compile(r"authorization|cookie|password|passwd|secret|token|sessionid|apikey|credential", re.I)
_BEARER = re.compile(r"\bBearer\s+[^\s<>\"']+", re.I)
_XML_SECRET = re.compile(
    r"(<(?:[\w.-]+:)?(?:password|secret|accessToken|refreshToken|sessionId)[^>]*>)(.*?)(</[^>]+>)",
    re.I | re.S,
)


def _secret_key(key: str) -> bool:
    return bool(_SECRET_KEY.search(re.sub(r"[^a-zA-Z0-9]", "", key)))


def _relative_url(path: Any) -> SplitResult:
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty relative path")
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or path.startswith(("//", "\\")) or any(c in path for c in "\r\n"):
        raise ValueError("path must stay relative to the registered Salesforce instance")
    return parsed


def _request_path(params: dict[str, Any]) -> str:
    parsed = _relative_url(params.get("path"))
    query = params.get("query", {})
    if not isinstance(query, dict):
        raise ValueError("query must be an object")
    encoded = urlencode(query, doseq=True)
    combined = "&".join(part for part in (parsed.query, encoded) if part)
    # A leading slash also keeps a caller's path out of CLI flag parsing.
    return urlunsplit(("", "", "/" + parsed.path.lstrip("/"), combined, parsed.fragment))


def _request_headers(params: dict[str, Any]) -> dict[str, str]:
    headers = params.get("headers", {})
    if not isinstance(headers, dict):
        raise ValueError("headers must be an object")
    for name, value in headers.items():
        if not isinstance(name, str) or not _HTTP_TOKEN.fullmatch(name) or not isinstance(value, str):
            raise ValueError("headers must contain HTTP header names and string values")
        if name.lower() in {"authorization", "host"} or any(c in value for c in "\r\n"):
            raise ValueError("headers cannot override credentials/host or contain CR/LF")
    return headers


def _request(params: dict[str, Any]) -> tuple[str, str, dict[str, str], str | None]:
    method = params.get("method")
    if not isinstance(method, str) or not _HTTP_TOKEN.fullmatch(method):
        raise ValueError("method must be an HTTP method token")
    body = params.get("body")
    if body is not None and not isinstance(body, str):
        raise ValueError("body must be raw JSON, XML or text as a string")
    return method, _request_path(params), _request_headers(params), body


def _secret_pattern(secret: str) -> re.Pattern[str]:
    """Match credential echoes in raw, URL and common base64 representations."""
    encoded = secret.encode("utf-8")
    forms = {secret, json.dumps(secret)[1:-1], base64.b64encode(encoded).decode("ascii"), base64.urlsafe_b64encode(encoded).decode("ascii")}
    forms |= {value.rstrip("=") for value in forms}
    forms |= {quote(value, safe="") for value in forms} | {quote_plus(value, safe="") for value in forms}
    patterns = []
    for value in sorted(forms, key=len, reverse=True):
        # Only percent escape hex digits are case-insensitive, not the secret.
        pieces = re.split(r"(%[0-9A-Fa-f]{2})", value)
        patterns.append("".join(f"(?i:{piece})" if re.fullmatch(r"%[0-9A-Fa-f]{2}", piece) else re.escape(piece) for piece in pieces))
    return re.compile("|".join(patterns))


def _redact(value: Any, secrets: list[str]) -> Any:
    if isinstance(value, dict):
        return {key: REDACTED if _secret_key(str(key)) else _redact(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        value = _BEARER.sub("Bearer " + REDACTED, value)
        value = _XML_SECRET.sub(lambda match: match[1] + REDACTED + match[3], value)
        for secret in filter(None, secrets):
            value = _secret_pattern(secret).sub(lambda _match: REDACTED, value)
    return value


def _credential_values(key: str, value: Any) -> list[str]:
    if isinstance(value, list):
        return [secret for item in value for secret in _credential_values(key, item)]
    text = str(value)
    values = [text]
    if text.lower().startswith("bearer "):
        values.append(text.split(" ", 1)[1].strip())
    if "cookie" in key.lower() and "=" in text:
        values.append(text.split("=", 1)[1].split(";", 1)[0])
    return values


def _secrets(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [secret for key, item in value.items() for secret in (_credential_values(str(key), item) if _secret_key(str(key)) and item else _secrets(item))]
    if isinstance(value, list):
        return [secret for item in value for secret in _secrets(item)]
    return []


def _audit_path(path: str) -> str:
    parsed = urlsplit(path)
    return urlunsplit(("", "", parsed.path, urlencode([(key, REDACTED) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]), ""))


def _body_secrets(body: Any) -> list[str]:
    if not isinstance(body, str):
        return _secrets(body)
    try:
        return _secrets(json.loads(body))
    except json.JSONDecodeError:
        return [match[2] for match in _XML_SECRET.finditer(body)]


def _typed_boundary(response: dict[str, Any], expected_media: str) -> bool:
    """Prove the header/body split for a typed operation's documented media type.

    The CLI prints header lines and then the body with no separator. Every
    header line matches ``name: value``. A JSON document starts with ``{`` or
    ``[`` and a Bulk API 2.0 CSV page starts with a quoted column name, so
    neither can be read as a header line and no header line can begin either.
    When the declared Content-Type matches and the remainder after the header
    lines decodes completely in that type, that line is the only possible
    boundary. An empty body is proven only by the printed blank line plus the
    vendor's own statement that there is nothing: status 204, or a CSV page
    whose Sforce-NumberOfRecords is 0. Anything else keeps the generic verb's
    uncertain-output shape.
    """
    headers = response.get("headers", {})
    content_type = str(headers.get("content-type", "")).lower()
    candidate = response.get("_redaction_context")
    if content_type.startswith("application/json") and isinstance(candidate, (dict, list)):
        return True
    csv = expected_media == "csv" and content_type.startswith("text/csv")
    if candidate == "":
        separated = str(response.get("transcript", "")).endswith(("\n\n", "\r\n\r\n"))
        return separated and (response.get("statusCode") == 204 or (csv and headers.get("sforce-numberofrecords") == "0"))
    return csv and isinstance(candidate, str) and candidate.startswith('"')


def _with_typed_body(response: dict[str, Any], expected_media: str | None) -> dict[str, Any]:
    """Replace an uncertain body with its proven typed body; otherwise unchanged."""
    if expected_media is None or "body_advisory" not in response or not _typed_boundary(response, expected_media):
        return response
    proven = {key: value for key, value in response.items() if key not in ("body_advisory", "transcript")}
    proven["body"] = response["_redaction_context"]
    return proven


def authenticated_request(executor: SalesforceCliExecutor, params: dict[str, Any], *, expected_media: str | None = None) -> dict[str, Any]:
    """Forward once; preserve HTTP failures as data, without an automatic retry.

    ``expected_media`` is set only by typed verbs, whose operation documents its
    response type; see ``_typed_boundary``. The generic verb never sets it.
    """
    method, path, headers, body = _request(params)
    secrets = _secrets({"headers": headers, "query": dict(parse_qsl(urlsplit(path).query))}) + _body_secrets(body)
    correlation_id = str(uuid.uuid4())
    started = time.monotonic()
    status: int | None = None
    try:
        response = executor.run_rest(method, path, body=body, headers=headers, structured_response=True)
        status = response["statusCode"]
        secrets.extend(_secrets(response.get("headers", {})) + _body_secrets(response.get("body")) + _body_secrets(response.get("_redaction_context")))
        response = _with_typed_body(response, expected_media)
        response_headers = {key.lower(): _redact(value, secrets) for key, value in response.get("headers", {}).items() if key.lower() in REQUEST_RESPONSE_HEADERS}
        result = {
            "status": status,
            "headers": response_headers,
            "body": _redact(response.get("body"), secrets),
            "correlation_id": correlation_id,
        }
        if isinstance(result["body"], dict) and "nextRecordsUrl" in result["body"]:
            result["nextRecordsUrl"] = result["body"]["nextRecordsUrl"]
        for field in ("body_advisory", "encoding_advisory", "transcript"):
            if field in response:
                result[field] = _redact(response[field], secrets)
        return result
    finally:
        logger.info(
            "Salesforce request audit %s",
            {"method": method, "path": _redact(_audit_path(path), secrets), "status": status, "duration_ms": round((time.monotonic() - started) * 1000), "correlation_id": correlation_id},
        )
