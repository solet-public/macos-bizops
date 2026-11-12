"""Generic Salesforce calls through the existing pinned-org CLI transport.

No operation policy lives here: Salesforce decides what its credential may do.
Only request framing and credential/tenant integrity are checked locally.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from .client import SalesforceCliExecutor
from .constants import REDACTED, REQUEST_RESPONSE_HEADERS

logger = logging.getLogger(__name__)
_HTTP_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _relative_url(path: Any) -> SplitResult:
    if not isinstance(path, str) or not path:
        raise ValueError("path must be a non-empty relative path")
    try:
        parsed = urlsplit(path)
    except ValueError:
        # urlsplit quotes the netloc in its message; the path is never echoed.
        raise ValueError("path is not a valid relative URL") from None
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


def _request(params: dict[str, Any]) -> tuple[str, str, dict[str, str], str | bytes | None]:
    """Bytes bodies come only from typed verbs (a multipart deploy package); caller JSON cannot carry bytes."""
    method = params.get("method")
    if not isinstance(method, str) or not _HTTP_TOKEN.fullmatch(method):
        raise ValueError("method must be an HTTP method token")
    body = params.get("body")
    if body is not None and not isinstance(body, (str, bytes)):
        raise ValueError("body must be raw JSON, XML or text as a string")
    return method, _request_path(params), _request_headers(params), body


def _audit_path(path: str) -> str:
    parsed = urlsplit(path)
    return urlunsplit(("", "", parsed.path, urlencode([(key, REDACTED) for key, _ in parse_qsl(parsed.query, keep_blank_values=True)]), ""))


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
    candidate = response.get("_decoded_body")
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
    proven["body"] = response["_decoded_body"]
    return proven


def stream_to_file(executor: SalesforceCliExecutor, params: dict[str, Any], output_path: str) -> str:
    """Download one GET response's bytes to ``output_path``; return the audit correlation id.

    The same framing, header refusals and audit line as ``authenticated_request``;
    the CLI's file mode prints no status, so the caller proves the bytes it
    wrote (``transfers``).
    """
    _method, path, headers, _body = _request({**params, "method": "GET", "body": None})
    correlation_id = str(uuid.uuid4())
    started = time.monotonic()
    try:
        executor.stream_rest("GET", path, output_path, headers=headers)
    finally:
        logger.info("Salesforce request audit %s", {"method": "GET", "path": _audit_path(path), "status": None, "stream_to_file": True, "duration_ms": round((time.monotonic() - started) * 1000), "correlation_id": correlation_id})
    return correlation_id


@dataclass(frozen=True)
class Forwarded:
    """One forwarded call: the result as received, its body and its selected headers.

    Every reading of vendor structure (outcomes, failure counts, pagination, a
    download's ContentSize and Checksum) uses ``raw_result``, ``raw_body`` and
    ``raw_headers``; none of them is ever logged.
    """

    raw_result: dict[str, Any]
    raw_body: Any
    raw_headers: Mapping[str, str] = field(default_factory=dict)


def authenticated_request(executor: SalesforceCliExecutor, params: dict[str, Any], *, expected_media: str | None = None) -> dict[str, Any]:
    """Forward once; preserve HTTP failures as data, without an automatic retry.

    ``expected_media`` is set only by typed verbs, whose operation documents its
    response type; see ``_typed_boundary``. The generic verb never sets it.
    """
    return forward(executor, params, expected_media=expected_media).raw_result


def forward(executor: SalesforceCliExecutor, params: dict[str, Any], *, expected_media: str | None = None) -> Forwarded:
    """``authenticated_request`` keeping the body and selected headers for in-process checks."""
    method, path, headers, body = _request(params)
    correlation_id = str(uuid.uuid4())
    started = time.monotonic()
    status: int | None = None
    try:
        response = executor.run_rest(method, path, body=body, headers=headers, structured_response=True)
        status = response["statusCode"]
        response = _with_typed_body(response, expected_media)
        raw_body = response.get("body")
        raw_headers = {key.lower(): value for key, value in response.get("headers", {}).items() if key.lower() in REQUEST_RESPONSE_HEADERS}
        result: dict[str, Any] = {"status": status, "headers": dict(raw_headers), "body": raw_body, "correlation_id": correlation_id}
        # Control keys are read from the vendor body.
        if isinstance(raw_body, dict) and "nextRecordsUrl" in raw_body:
            result["nextRecordsUrl"] = raw_body["nextRecordsUrl"]
        for name in ("body_advisory", "encoding_advisory", "transcript"):
            if name in response:
                result[name] = response[name]
        return Forwarded(raw_result=result, raw_body=raw_body, raw_headers=raw_headers)
    finally:
        # Built only from method, path without query values, status, duration and correlation id.
        logger.info("Salesforce request audit %s", {"method": method, "path": _audit_path(path), "status": status, "duration_ms": round((time.monotonic() - started) * 1000), "correlation_id": correlation_id})
