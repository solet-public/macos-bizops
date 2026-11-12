"""Account-bound Snowflake REST transport using the existing RSA credential.

JWT contract: https://docs.snowflake.com/en/developer-guide/sql-api/authenticating
No credential is fetched here; AppConfigLoader remains the sole resolver.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from .app_config import SnowflakeAccountConfig
from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_INVALID_PARAMS,
    ERROR_PERMISSION_DENIED,
    REST_ERROR_RATE_LIMITED,
    REST_JWT_LIFETIME_SECONDS,
    REST_PROTECTED_HEADERS,
    REST_REDACTED,
)

logger = logging.getLogger(__name__)


class RestRequestError(ValueError):
    """A credential/tenant integrity refusal with no caller data in the message."""


def account_host(account: str) -> str:
    """Only a configured account identifier can determine the destination."""
    if not re.fullmatch(r"[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*", account):
        raise RestRequestError("configured account must be a Snowflake account identifier")
    return f"{account}.snowflakecomputing.com"


def relative_path(path: str) -> str:
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or path.startswith("//") or "\\" in path:
        raise RestRequestError("request path must be relative to the configured account")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise RestRequestError("request path cannot contain control characters")
    return "/" + path.lstrip("/")


def request_headers(headers: dict[str, str]) -> dict[str, str]:
    for key, value in headers.items():
        if any(char in key + value for char in "\r\n"):
            raise RestRequestError("request headers cannot contain CR or LF")
    return {key: value for key, value in headers.items() if key.lower() not in REST_PROTECTED_HEADERS}


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def authentication(config: SnowflakeAccountConfig) -> str:
    private_key = serialization.load_der_private_key(config.private_key_der, password=None)
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise RestRequestError("REST authentication requires the registered RSA key pair")
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(public_der).digest()).decode("ascii")
    # Locator.region.cloud uses the locator; organization-account is already qualified.
    account = config.account.split(".")[0].upper()
    subject = f"{account}.{config.user.upper()}"
    now = int(time.time())
    claims = {"iss": f"{subject}.{fingerprint}", "sub": subject, "iat": now, "exp": now + REST_JWT_LIFETIME_SECONDS}
    signing_input = _b64url(b'{"alg":"RS256","typ":"JWT"}') + "." + _b64url(json.dumps(claims).encode())
    signature = private_key.sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + _b64url(signature)


def _audit_path(url: httpx.URL) -> str:
    parsed = urlsplit(str(url))
    query = urlencode([(key, REST_REDACTED) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)])
    return urlunsplit(("", "", parsed.path, query, ""))


class _DuplicateJSONMemberError(ValueError):
    """A JSON object cannot represent every member without losing information."""


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSONMemberError
        result[key] = value
    return result


def _response_data(response: httpx.Response, result_hints: Callable[[dict[str, Any]], dict[str, Any]] | None = None) -> dict[str, Any]:
    advisory: str | None = None
    headers = dict(response.headers)
    try:
        # Decode strictly before parsing: JSON's byte-input encoding detection
        # can consume BOMs and turn non-UTF8 responses into a different body.
        text = response.content.decode("utf-8")
    except UnicodeError:
        body = base64.b64encode(response.content).decode("ascii")
        encoding = "base64"
    else:
        try:
            body = json.loads(text, object_pairs_hook=_unique_json_object)
            encoding = "json"
        except _DuplicateJSONMemberError:
            body = text
            encoding = "text"
            advisory = "Response body had duplicate JSON members and was preserved as text."
        except ValueError:
            body = text
            encoding = "text"
    result = {"status": response.status_code, "headers": headers, "body": body, "body_encoding": encoding}
    if result_hints is not None:
        result["_hints"] = result_hints(result)
    if advisory is not None:
        result["advisory"] = advisory
    # Pagination, SQL handles and Retry-After stay in body/headers, never followed.
    if response.status_code >= 400:
        result["error_code"] = _vendor_error(response.status_code)
    return result


def _vendor_error(status: int) -> str:
    return {401: ERROR_AUTH_FAILED, 403: ERROR_PERMISSION_DENIED, 429: REST_ERROR_RATE_LIMITED}.get(
        status,
        ERROR_API_ERROR,
    )


def perform_request(
    config: SnowflakeAccountConfig,
    params: dict[str, Any],
    *,
    timeout_seconds: int,
    transport: httpx.BaseTransport | None = None,
    result_hints: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One request, no automatic retries, redirects, vendor polling or SQL parsing."""
    host = account_host(config.account)
    path = relative_path(params["path"])
    headers = request_headers(params.get("headers", {}))
    token = authentication(config)
    headers.update({"Authorization": f"Bearer {token}", "X-Snowflake-Authorization-Token-Type": "KEYPAIR_JWT"})
    correlation_id = str(uuid.uuid4())
    started = time.monotonic()
    status: int | None = None
    audit_path = path.split("?", 1)[0]
    try:
        with httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        ) as client:
            url = httpx.URL(f"https://{host}{path}").copy_merge_params(params.get("query") or {})
            request = client.build_request(params["method"], url, headers=headers, json=params.get("body"))
            audit_path = _audit_path(request.url)
            response = client.send(request)
            status = response.status_code
            return {**_response_data(response, result_hints), "correlation_id": correlation_id}
    finally:
        # Built only from method, path without query values, status, duration and correlation id.
        logger.info("Snowflake REST audit %s", {"method": params["method"], "path": audit_path, "status": status, "duration_seconds": time.monotonic() - started, "correlation_id": correlation_id})


def run_request(plugin: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Worker entry: resolve once; keep all exception text away from job history."""
    if plugin._app_config_loader is None:
        return plugin._error(ERROR_AUTH_FAILED, "Snowflake account is not configured")
    try:
        config = plugin._app_config_loader.resolve()
        return plugin._success(perform_request(config, params, timeout_seconds=plugin._login_timeout_seconds()))
    except RestRequestError as exc:
        return plugin._error(ERROR_INVALID_PARAMS, str(exc))
    except Exception:
        return plugin._error(ERROR_API_ERROR, "Snowflake REST request failed; vendor state may have changed")
