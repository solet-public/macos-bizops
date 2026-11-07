"""Typed vendor operations: one validated spec per Zuora REST route.

Every typed verb (build 2: Workflows, SQL Data Query, Files; build 3: Orders,
Subscriptions, Accounts; build 4: Payment Methods, Payments, Custom Objects,
Object Query; build 5: Billing Documents, Invoices, Credit and Debit Memos,
Bill Runs, Usage) is one :class:`Operation`: the exact vendor method and path
template, the caller parameters that map onto path segments, query keys and
documented request headers, and an optional body builder (a JSON document,
raw bytes with ``raw_content_type`` for a file upload, or a :class:`RawBody`
carrying its own multipart/form-data type). :func:`execute` is the one
executor they share, run on the plugin's single async worker thread.

Contract shared by every typed verb:

* Vendor RBAC is the only operation boundary. Reads, writes and deletes are
  equally first-class; nothing here asks for a role or a confirmation.
* Path segments are validated identifiers, percent-encoded, joined onto the
  registered tenant. Callers never supply a host or URL.
* A non-2xx response is a typed job error: 403 keeps the vendor's
  permission-denied detail (``zuora.permission_denied``); other classes use
  :func:`errors.classify_zuora_response`. Every message and every success body
  is redacted for the registered client id/secret, the minted bearer and any
  caller-supplied secret field, in raw, percent/form-encoded and base64 forms.
* One vendor call per operation. A 401 re-mints the bearer once (Zuora
  rejected the call before running it); nothing else is retried or replayed.
* The v1 billing routes can answer HTTP 200 with ``success: false``; an
  operation with ``envelope=True`` reports that as a typed job error carrying
  Zuora's ``reasons``, never as a completed write.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal, Protocol
from urllib.parse import parse_qs, quote, urlsplit
from uuid import uuid4

import httpx

from . import request_actions
from .constants import ERROR_PERMISSION_DENIED, ERROR_VALIDATION_FAILED, PLUGIN_NAME
from .errors import ZuoraServiceError, classify_zuora_response, vendor_detail
from .http_client import ZuoraClient

logger = logging.getLogger(PLUGIN_NAME)

_HTTP_FORBIDDEN: Final[int] = 403
_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
JSON_SUFFIX: Final[str] = ".json"
# Zuora-Track-Id: US-ASCII without colon, semicolon, double quote or quote (vendor-documented).
_TRACK_ID_FORBIDDEN: Final[frozenset[str]] = frozenset(":;\"'")

FieldKind = Literal["int_id", "str_id", "text", "optional_text", "bool", "count", "date", "str_list", "choice", "header", "track_id", "number"]
Paging = Literal["none", "page_number", "single_page", "cursor", "next_link"]


class PathGate(Protocol):
    """The plugin's export_allowed_roots containment gate (``ZuoraPlugin._export_path_gate``)."""

    def __call__(self, output_tsv_path: str, *, param_name: str = ..., suffix: str | None = ...) -> str: ...


@dataclass(frozen=True, slots=True)
class Field:
    """One caller parameter: its vendor name, its shape and whether Zuora requires it."""

    vendor: str
    kind: FieldKind
    required: bool = False
    maximum: int | None = None
    choices: tuple[str, ...] = ()
    default: str | None = None


@dataclass(frozen=True, slots=True)
class RawBody:
    """A non-JSON request body with its own ``Content-Type`` (a multipart/form-data file upload)."""

    content: bytes
    content_type: str


BodyBuilder = Callable[[dict[str, Any], PathGate], Any]
SecretFinder = Callable[[dict[str, Any]], list[str]]
QueryCheck = Callable[[dict[str, Any]], None]
QueryBuilder = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True, slots=True)
class Operation:
    """One vendor route. ``path`` names its path fields as ``{param}`` placeholders."""

    name: str
    method: str
    path: str
    path_fields: Mapping[str, Field] = field(default_factory=dict[str, Field])
    query_fields: Mapping[str, Field] = field(default_factory=dict[str, Field])
    body: BodyBuilder | None = None
    secrets: SecretFinder | None = None
    paging: Paging = "none"
    json_output: bool = False
    is_query: bool = False
    header_fields: Mapping[str, Field] = field(default_factory=dict[str, Field])
    query_check: QueryCheck | None = None
    envelope: bool = False
    query_builder: QueryBuilder | None = None
    raw_content_type: str | None = None
    # Top-level result keys the verb exists to return (e.g. a payment session ``token``):
    # exempt from secret-key-name redaction; credential-value variants are still scrubbed.
    revealed_keys: frozenset[str] = frozenset()

    @property
    def retryable(self) -> bool:
        """Reads may be retried by the platform; writes and deletes never are."""
        return self.method == "GET"


# ---------------------------------------------------------------------------
# Input validation — mirrors the vendor's documented parameter shapes
# ---------------------------------------------------------------------------


def coerce(name: str, spec: Field, value: Any) -> Any:
    """Validate one present parameter and return the value sent to Zuora."""
    return _VALIDATORS[spec.kind](name, spec, value)


def _int_id(name: str, _spec: Field, value: Any) -> int:
    return _int_value(name, value, minimum=1, maximum=None)


def _count(name: str, spec: Field, value: Any) -> int:
    return _int_value(name, value, minimum=1, maximum=spec.maximum)


def _str_id(name: str, _spec: Field, value: Any) -> str:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    if not isinstance(text, str) or not _ID_PATTERN.match(text) or ".." in text:
        raise ValueError(f"{name} must be a Zuora identifier (letters, digits, '_', '-', '.', ':')")
    return text


def _text(name: str, _spec: Field, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _optional_text(name: str, _spec: Field, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string; pass an empty string to clear it")
    return value


def _bool(name: str, _spec: Field, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def _date(name: str, _spec: Field, value: Any) -> str:
    if not isinstance(value, str) or len(value) != len("2026-01-31"):
        raise ValueError(f"{name} must be a YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a YYYY-MM-DD date") from exc
    return value


def _number(name: str, _spec: Field, value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    return value


def _str_list(name: str, _spec: Field, value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{name} must be a non-empty list of non-empty strings")
    return [str(item) for item in value]


def _choice(name: str, spec: Field, value: Any) -> Any:
    if value not in spec.choices:
        raise ValueError(f"{name} must be one of {', '.join(spec.choices)}")
    return value


def _header(name: str, _spec: Field, value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or not all(" " <= char <= "~" for char in value):
        raise ValueError(f"{name} must be a non-empty printable US-ASCII header value")
    return value


def _track_id(name: str, spec: Field, value: Any) -> str:
    text = _header(name, spec, value)
    if _TRACK_ID_FORBIDDEN.intersection(text):
        raise ValueError(f"{name} must not contain a colon, semicolon, double quote or quote (Zuora-Track-Id rule)")
    return text


_VALIDATORS: Final[dict[str, Callable[[str, Field, Any], Any]]] = {
    "int_id": _int_id,
    "count": _count,
    "str_id": _str_id,
    "text": _text,
    "optional_text": _optional_text,
    "bool": _bool,
    "date": _date,
    "str_list": _str_list,
    "choice": _choice,
    "header": _header,
    "track_id": _track_id,
    "number": _number,
}


def _int_value(name: str, value: Any, *, minimum: int, maximum: int | None) -> int:
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{name} must be an integer of at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum} (Zuora's documented maximum)")
    return value


def collect(fields: Mapping[str, Field], params: dict[str, Any]) -> dict[str, Any]:
    """Validate the present fields, refuse a missing required one, keyed by vendor name."""
    out: dict[str, Any] = {}
    for name, spec in fields.items():
        value = params.get(name, spec.default)
        if value is None:
            if spec.required:
                raise ValueError(f"{name} is required")
            continue
        out[spec.vendor] = coerce(name, spec, value)
    return out


def object_param(params: dict[str, Any], name: str, *, required: bool) -> dict[str, Any] | None:
    """Return a JSON object parameter, refusing any other shape."""
    value = params.get(name)
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return {str(key): item for key, item in value.items()}


def build_path(op: Operation, params: dict[str, Any]) -> str:
    """Fill the path template with validated, percent-encoded identifiers."""
    segments: dict[str, str] = {}
    for name, spec in op.path_fields.items():
        value = params.get(name)
        if value is None:
            raise ValueError(f"{name} is required")
        segments[name] = quote(str(coerce(name, spec, value)), safe="")
    return op.path.format(**segments)


def build_query(op: Operation, params: dict[str, Any]) -> dict[str, Any]:
    """The vendor query: declared fields, the pairing check, then any composed keys."""
    query = collect(op.query_fields, params)
    if op.query_check is not None:
        op.query_check(params)
    if op.query_builder is not None:
        query.update(op.query_builder(params))
    return query


def validate(op: Operation, params: dict[str, Any], gate: PathGate | None = None) -> None:
    """Dispatch-time check: everything except reading caller files and calling Zuora."""
    build_path(op, params)
    build_query(op, params)
    collect(op.header_fields, params)
    if op.body is not None:
        op.body(params, gate if gate is not None else _no_file_gate)
    if op.json_output and params.get("output_path") is not None and not isinstance(params["output_path"], str):
        raise ValueError("output_path must be an absolute path string")


def _no_file_gate(output_tsv_path: str, *, param_name: str = "path", suffix: str | None = None) -> str:
    """Dispatch-time stand-in: the worker applies the real containment gate."""
    del suffix
    if not output_tsv_path:
        raise ValueError(f"{param_name} must be an absolute path string")
    return ""


def read_json_file(gate: PathGate, path: Any, *, param_name: str) -> dict[str, Any] | None:
    """Read a JSON object from a contained ``.json`` file. ``None`` at dispatch time."""
    if not isinstance(path, str):
        raise ValueError(f"{param_name} must be an absolute path string")
    resolved = gate(path, param_name=param_name, suffix=JSON_SUFFIX)
    if not resolved:
        return None
    try:
        with open(resolved, encoding="utf-8") as handle:
            loaded: Any = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{param_name} must name a readable JSON file ({type(exc).__name__})") from exc
    if not isinstance(loaded, dict) or not loaded:
        raise ValueError(f"{param_name} must hold a non-empty JSON object")
    return {str(key): item for key, item in loaded.items()}


def read_file_bytes(gate: PathGate, path: Any, *, param_name: str, suffix: str) -> bytes:
    """Read a contained upload file; empty bytes at dispatch time (the worker reads it)."""
    if not isinstance(path, str):
        raise ValueError(f"{param_name} must be an absolute path string")
    resolved = gate(path, param_name=param_name, suffix=suffix)
    if not resolved:
        return b""
    try:
        with open(resolved, "rb") as handle:
            content = handle.read()
    except OSError as exc:
        raise ValueError(f"{param_name} must name a readable {suffix} file ({type(exc).__name__})") from exc
    if not content:
        raise ValueError(f"{param_name} must not be empty")
    return content


def multipart_file(gate: PathGate, path: Any, *, param_name: str, media_types: Mapping[str, str]) -> RawBody:
    """One contained file as Zuora's multipart/form-data ``file`` part; ``media_types`` maps each allowed suffix to its type.

    At dispatch time (no file read) the content is empty; the worker reads the
    file through the containment gate and frames it with a fresh boundary that
    does not occur in the file.
    """
    suffix = next((known for known in media_types if isinstance(path, str) and path.lower().endswith(known)), None)
    if suffix is None:
        raise ValueError(f"{param_name} must be an absolute path ending in one of {', '.join(media_types)} (the file types Zuora accepts here)")
    content = read_file_bytes(gate, path, param_name=param_name, suffix=path[-len(suffix) :])
    boundary = f"zuora-plugin-{uuid4().hex}"
    while boundary.encode("ascii") in content:
        boundary = f"zuora-plugin-{uuid4().hex}"
    name = path.replace("\\", "/").rsplit("/", 1)[-1].replace('"', "")
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {media_types[suffix]}\r\n\r\n'
    framed = head.encode("utf-8") + content + f"\r\n--{boundary}--\r\n".encode("ascii")
    return RawBody(framed if content else b"", f"multipart/form-data; boundary={boundary}")


def contained_output(gate: PathGate, path: Any, *, suffix: str | None) -> str:
    """Resolve ``output_path`` through the containment gate BEFORE any vendor call."""
    if not isinstance(path, str):
        raise ValueError("output_path must be an absolute path string")
    return gate(path, param_name="output_path", suffix=suffix)


def write_file(resolved: str, content: bytes) -> dict[str, Any]:
    """Write downloaded bytes to an already-contained path; return the handle the caller relays."""
    with open(resolved, "wb") as handle:
        handle.write(content)
    return {"path": resolved, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


def raise_for_vendor_error(op: Operation, response: httpx.Response, secrets: list[str]) -> None:
    """Map a non-2xx response to a typed, redacted job error."""
    if not response.is_error:
        return
    if response.status_code == _HTTP_FORBIDDEN:
        detail = vendor_detail(response)
        message = "Zuora denied this operation for the registered OAuth client's role (403)"
        code, message = ERROR_PERMISSION_DENIED, f"{message}: {detail}" if detail else f"{message}."
    else:
        code, message = classify_zuora_response(response, is_query=op.is_query)
    raise ZuoraServiceError(code, str(request_actions.redact(message, secrets)))


def raise_for_unsuccessful_envelope(op: Operation, response: httpx.Response, secrets: list[str]) -> None:
    """A v1 route's HTTP 200 carrying ``success: false`` (``Success`` on the object routes) is a vendor refusal, not a completion."""
    if not op.envelope:
        return
    body = response_body(response)
    if isinstance(body, dict) and (body.get("success") is False or body.get("Success") is False):
        detail = vendor_detail(response)
        message = f"Zuora reported success false (status {response.status_code})"
        raise ZuoraServiceError(ERROR_VALIDATION_FAILED, str(request_actions.redact(f"{message}: {detail}" if detail else f"{message}.", secrets)))


def response_body(response: httpx.Response) -> Any:
    """Decode a JSON body; an empty body is ``None`` and any other body is text."""
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def call(op: Operation, client: ZuoraClient, params: dict[str, Any], gate: PathGate) -> tuple[httpx.Response, list[str], dict[str, Any]]:
    """Validate, send one vendor request, map errors; return the response, secrets and query."""
    path = build_path(op, params)
    query = build_query(op, params)
    headers = collect(op.header_fields, params)
    body = op.body(params, gate) if op.body is not None else None
    secrets = [*request_actions.secret_values(body), *(op.secrets(params) if op.secrets is not None else [])]
    if isinstance(body, RawBody):
        headers["Content-Type"] = body.content_type
        body = body.content
    content = body if isinstance(body, bytes) else None
    if op.raw_content_type is not None:
        headers["Content-Type"] = op.raw_content_type
    correlation_id, started, status = str(uuid4()), time.monotonic(), None
    try:
        response = client.send(op.method, path, params=query or None, json=None if content is not None else body, content=content, headers=headers or None)
        status = response.status_code
    finally:
        secrets.extend(client.credential_values())
        logger.info(
            "zuora.%s method=%s path=%s status=%s duration_ms=%.3f correlation_id=%s",
            op.name,
            op.method,
            request_actions.redact(request_actions.audit_path(path, query), secrets),
            status,
            (time.monotonic() - started) * 1000,
            correlation_id,
        )
    raise_for_vendor_error(op, response, secrets)
    raise_for_unsuccessful_envelope(op, response, secrets)
    return response, secrets, {"operation": op.name, "method": op.method, "path": path, "status": response.status_code, "correlation_id": correlation_id}


def execute(op: Operation, client: ZuoraClient, params: dict[str, Any], gate: PathGate) -> dict[str, Any]:
    """Run one typed operation; return its redacted vendor body and paging hints."""
    wants_file = op.json_output and params.get("output_path") is not None
    resolved = contained_output(gate, params["output_path"], suffix=JSON_SUFFIX) if wants_file else None
    response, secrets, result = call(op, client, params, gate)
    body = _redacted_body(op, response_body(response), secrets)
    if op.paging != "none":
        result["pagination"] = _PAGERS[op.paging](params, body)
    if resolved is not None:
        rendered = json.dumps(body, indent=2, sort_keys=True).encode("utf-8")
        return {**result, "file": write_file(resolved, rendered)}
    return {**result, "data": body}


def _redacted_body(op: Operation, raw: Any, secrets: list[str]) -> Any:
    """Redact the vendor body; ``revealed_keys`` skip key-name redaction but keep value scrubbing."""
    body = request_actions.redact(raw, secrets)
    if isinstance(raw, dict) and isinstance(body, dict):
        body.update({key: request_actions.redact(raw[key], secrets) for key in op.revealed_keys if key in raw})
    return body


def _page_size(params: dict[str, Any]) -> int | None:
    size = params.get("page_size")
    return int(size) if isinstance(size, (int, str)) and str(size).isdigit() else None


def _pagination(params: dict[str, Any], body: Any) -> dict[str, Any]:
    """Zuora's page-numbered lists; ``next_page`` is the page number to pass back, or ``None`` at the end.

    The Workflow routes carry ``pagination.next_page``; the v1 billing routes
    (Orders, Subscriptions) carry a top-level ``nextPage`` URL, absent on the
    last page, whose own ``page`` query value is the next page number.
    """
    page = params.get("page")
    current = int(page) if isinstance(page, (int, str)) and str(page).isdigit() else 1
    if isinstance(body, dict) and "pagination" not in body:
        link = body.get("nextPage")
        return {"page": current, "next_page": _link_page(link, current) if isinstance(link, str) and link else None, "vendor": link}
    vendor = body.get("pagination") if isinstance(body, dict) else None
    more = isinstance(vendor, dict) and bool(vendor.get("next_page"))
    return {"page": current, "next_page": current + 1 if more else None, "vendor": vendor}


def _link_page(link: str, current: int) -> int:
    values = parse_qs(urlsplit(link).query).get("page", [])
    return int(values[0]) if values and values[0].isdigit() else current + 1


def _single_page(params: dict[str, Any], body: Any) -> dict[str, Any]:
    """A list Zuora answers in one response with no continuation (SQL Data Query jobs).

    ``may_have_more`` is true when the response filled the requested
    ``page_size`` (narrow the filter or raise ``page_size``), false when it
    fell short, and ``None`` when no ``page_size`` was sent, since the vendor
    documents no default.
    """
    records = body.get("data") if isinstance(body, dict) else None
    returned = len(records) if isinstance(records, list) else 0
    limit = _page_size(params)
    return {
        "page": 1,
        "next_page": None,
        "returned": returned,
        "page_size": limit,
        "may_have_more": None if limit is None else returned >= limit,
        "hint": "Zuora returns this list in one response with no continuation; filter by query_status or raise page_size to see more.",
    }


_CURSOR_KEYS: Final[tuple[str, ...]] = ("nextPage", "cursor")
_RECORD_KEYS: Final[tuple[str, ...]] = ("data", "records", "jobs")


def _cursor_page(params: dict[str, Any], body: Any) -> dict[str, Any]:
    """Cursor-paged lists: Object Query's ``nextPage``, custom object records' and jobs' ``cursor``.

    ``next_cursor`` is the value to pass back as ``cursor`` (with the same
    filters and ``page_size``), or ``None`` at the end: when Zuora sent no
    cursor, or when it returned fewer records than the requested ``page_size``
    (the jobs list's cursor names the last job of the page, so its presence
    alone does not prove another page exists).
    """
    found: dict[str, Any] = body if isinstance(body, dict) else {}
    vendor = next((found[key] for key in _CURSOR_KEYS if isinstance(found.get(key), str) and found[key]), None)
    records = next((found[key] for key in _RECORD_KEYS if isinstance(found.get(key), list)), [])
    limit = _page_size(params)
    short = limit is not None and len(records) < limit
    return {
        "cursor": params.get("cursor"),
        "next_cursor": None if short else vendor,
        "returned": len(records),
        "vendor": vendor,
        "hint": "Pass next_cursor back as cursor with the same filters and page_size; null means the last page.",
    }


def _next_link(_params: dict[str, Any], body: Any) -> dict[str, Any]:
    """A route that answers a ``nextPage`` URL but documents no page parameter (invoice and debit memo application parts).

    ``next_path`` is the tenant-relative path and query of Zuora's link, to
    fetch with the ``request`` verb; ``None`` means Zuora sent no further page.
    """
    link = body.get("nextPage") if isinstance(body, dict) else None
    if not isinstance(link, str) or not link:
        return {"next_path": None, "vendor": None, "hint": "Zuora sent no nextPage link: this is the whole list."}
    parts = urlsplit(link)
    return {
        "next_path": parts.path + (f"?{parts.query}" if parts.query else ""),
        "vendor": link,
        "hint": "Zuora documents no page parameter on this route: fetch next_path with the request verb (method GET).",
    }


_PAGERS: Final[dict[str, Callable[[dict[str, Any], Any], dict[str, Any]]]] = {"page_number": _pagination, "single_page": _single_page, "cursor": _cursor_page, "next_link": _next_link}

ActionFn = Callable[[ZuoraClient, dict[str, Any], PathGate], dict[str, Any]]


def action(op: Operation) -> ActionFn:
    """The worker-side callable registered in ``async_jobs.ACTION_HANDLERS``."""

    def run(client: ZuoraClient, params: dict[str, Any], gate: PathGate) -> dict[str, Any]:
        return execute(op, client, params, gate)

    return run
