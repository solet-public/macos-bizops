"""Generic request preparation and execution; no per-family authorization policy."""

from __future__ import annotations

import base64
import logging
import re
from functools import lru_cache
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from .constants import REQUEST_REDACTION, REQUEST_SECRET_KEYS

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
    return {
        "method": method,
        "path": path,
        "query": params.get("query"),
        "json_body": params.get("json_body"),
        "form_body": params.get("form_body"),
        "headers": prepare_headers(params.get("headers")),
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


def response_data(response: httpx.Response, body: Any, secrets: tuple[str, ...], correlation_id: str) -> dict[str, Any]:
    safe_body = redact_value(body, secrets)
    result = {
        "status": response.status_code,
        "body": safe_body,
        "correlation_id": correlation_id,
        "retry_after": redact_value(response.headers.get("Retry-After"), secrets),
    }
    if isinstance(safe_body, dict):
        result.update({key: safe_body[key] for key in ("nextPageToken", "success", "errors") if key in safe_body})
    return result


def _percent_byte_pattern(value: int) -> str:
    """Match either hex case and percent escaping introduced by URL serialization."""
    digits = f"{value:02x}"
    return "%(?:25)*" + "".join(f"[{digit.lower()}{digit.upper()}]" if digit.isalpha() else digit for digit in digits)


def _encoded_character_pattern(character: str) -> str:
    """Keep literal case significant while allowing mixed raw/percent/form encoding."""
    alternatives = [re.escape(character), "".join(_percent_byte_pattern(value) for value in character.encode())]
    if character == " ":
        alternatives.extend([r"\+", _percent_byte_pattern(ord("+"))])
    return "(?:" + "|".join(alternatives) + ")"


@lru_cache
def _credential_pattern(secret: str) -> re.Pattern[str]:
    """Cover UTF-8 URL/form encodings and padded/unpadded standard/URL-safe base64."""
    encoded = secret.encode()
    standard = base64.b64encode(encoded).decode()
    urlsafe = base64.urlsafe_b64encode(encoded).decode()
    variants = {secret, standard, standard.rstrip("="), urlsafe, urlsafe.rstrip("=")}
    patterns = ["".join(_encoded_character_pattern(character) for character in variant) for variant in sorted(variants, key=lambda value: (-len(value), value))]
    return re.compile("|".join(patterns))


def _redact_text(value: str, secrets: tuple[str, ...]) -> str:
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = _credential_pattern(secret).sub(REQUEST_REDACTION, value)
    return value


def redact_value(value: Any, secrets: tuple[str, ...]) -> Any:
    """Preserve vendor data except credential fields and encoded credential values."""
    if isinstance(value, dict):
        return {_redact_text(str(key), secrets): REQUEST_REDACTION if str(key).lower() in REQUEST_SECRET_KEYS else redact_value(item, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item, secrets) for item in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def audit_path(path: str, secrets: tuple[str, ...]) -> str:
    """Redact ALL query values, including when query parameters are embedded in path."""
    parsed = urlsplit(path)
    query = urlencode([(key, REQUEST_REDACTION) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    return str(redact_value(parsed.path + ("?" + query if query else ""), secrets))


class CredentialLogFilter(logging.Filter):
    """HTTPX logs identity URLs; redact this client's secret before handlers see them."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secrets: tuple[str, ...] = (secret,)

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(value.copy_with(query=urlencode([(key, REQUEST_REDACTION) for key, _ in value.params.multi_items()]).encode()) if isinstance(value, httpx.URL) else value for value in record.args)
        record.msg = redact_value(record.getMessage(), self.secrets)
        record.args = ()
        return True


def execute_request(client: MarketoClient, request: dict[str, Any]) -> dict[str, Any]:
    """Run off the dispatch path using the shared client and token cache."""
    return client.request(
        request["method"],
        request["path"],
        query=request.get("query"),
        json_body=request.get("json_body"),
        form_body=request.get("form_body"),
        headers=request.get("headers"),
    )
