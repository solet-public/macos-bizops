"""Execute one pinned Drive/People/Tasks method as the resolved account.

The official client owns URL escaping, media encoding and response decoding.
Paging is explicit: pass the returned nextPageToken back as pageToken. No
response envelope or vendor fields are projected away.

For the editor APIs (Docs, Sheets, Slides) the API-wide response-format
parameters are honoured exactly: the official client always rewrites ``alt`` to
``json`` and cannot parse a JSONP ``callback`` reply, so ``alt=proto``,
``alt=media`` and ``callback`` send the caller's value and return Google's raw
reply as a blob instead of a parsed response.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from googleapiclient.discovery import key2param
from googleapiclient.http import MediaInMemoryUpload

from .account_processes import VerbFailureError
from .gmail_actions import GmailIO
from .workspace_registry import WORKSPACE_METHODS, parameter_schemas


def _validate_scalar(name: str, value: Any, schema: dict[str, Any]) -> None:
    kind = schema.get("type", "string")
    types = {"string": str, "integer": int, "boolean": bool, "number": (int, float)}
    expected = types.get(kind, str)
    if not isinstance(value, expected) or (kind in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"{name} must be {kind}")
    _validate_constraints(name, value, schema)


def _validate_constraints(name: str, value: Any, schema: dict[str, Any]) -> None:
    if schema.get("pattern") and not re.match(schema["pattern"], str(value)):
        raise ValueError(f"{name} must match vendor pattern {schema['pattern']}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{name} must be one of {schema['enum']}")
    if "minimum" in schema and int(value) < int(schema["minimum"]):
        raise ValueError(f"{name} violates vendor minimum {schema['minimum']}")
    if "maximum" in schema and int(value) > int(schema["maximum"]):
        raise ValueError(f"{name} violates vendor maximum {schema['maximum']}")


def request_arguments(verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate supplied path/query types and vendor ranges before transport."""
    schemas = parameter_schemas(verb)
    result: dict[str, Any] = {}
    for name, schema in schemas.items():
        value = params.get(name)
        if value is None:
            if schema.get("required"):
                raise ValueError(f"{name} is required")
            continue
        if schema.get("repeated"):
            if not isinstance(value, list):
                raise ValueError(f"{name} must be an array")
            for item in value:
                _validate_scalar(name, item, schema)
        else:
            _validate_scalar(name, value, schema)
        result[key2param(name)] = value
    return result


def _check_entitlement(verb: str, method: dict[str, Any], facts: dict[str, Any]) -> None:
    """Known consumer directory calls have an actionable tier error.

    Pass: Workspace account; producer reads stored tier on every call; transcript
    smoke covers Workspace success. Unknown tier/grants remain advisory and
    Google determines access. Known missing scopes name all acceptable grants.
    """
    if "DirectoryPeople" in method["id"] and facts.get("account_tier") == "consumer":
        raise VerbFailureError("gsuite.permission_denied", f"{verb} reads a Workspace domain directory; this account tier is consumer. Select a connected Workspace account.")
    granted = facts.get("granted_scopes")
    accepted = method.get("scopes", [])
    if granted and accepted and not set(granted).intersection(accepted):
        raise VerbFailureError("gsuite.missing_scope", f"{verb} needs one of {', '.join(accepted)}. Reconnect this account with start_interface then connect_account and approve the requested access.")


RAW_ALT_PRODUCTS: frozenset[str] = frozenset({"docs", "sheets", "slides"})
_RAW_ALT_MIME: dict[str, str] = {"proto": "application/x-protobuf", "media": "application/octet-stream"}
_ALT_JSON = re.compile(r"(?<=[?&])alt=json(?=&|$)")


def raw_reply_mime(method: dict[str, Any], params: dict[str, Any]) -> str | None:
    """MIME of the raw reply the caller asked for, or None for a parsed JSON reply."""
    if method["id"].split(".")[0] not in RAW_ALT_PRODUCTS or method.get("supportsMediaDownload"):
        return None
    if params.get("callback") is not None:
        return "text/javascript"
    return _RAW_ALT_MIME.get(str(params.get("alt")))


def _raw_body(_response: Any, content: bytes) -> bytes:
    return content


def _request_kwargs(verb: str, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    _, method = WORKSPACE_METHODS[verb]
    kwargs = request_arguments(verb, params)
    if "request" in method and params.get("body") is not None:
        body = params["body"]
        if not isinstance(body, dict):
            raise ValueError("body must be a JSON object")
        kwargs["body"] = body
    if params.get("blob_key"):
        attachment = io.attachment_loader(params["blob_key"])
        kwargs["media_body"] = MediaInMemoryUpload(attachment.content, mimetype=params.get("mime_type") or attachment.mime_type)
    if "fields" in parameter_schemas(verb) and "fields" not in kwargs:
        kwargs["fields"] = "*"
    return kwargs


def _build_request(service: Any, method: dict[str, Any], kwargs: dict[str, Any], params: dict[str, Any]) -> Any:
    resource = service
    parts = method["id"].split(".")[1:]
    for part in parts[:-1]:
        resource = getattr(resource, part)()
    method_name = parts[-1]
    if kwargs.get("alt") == "media" and method.get("supportsMediaDownload"):
        method_name += "_media"
    request = getattr(resource, method_name)(**kwargs)
    if raw_reply_mime(method, params) is not None:
        request.uri = _ALT_JSON.sub(f"alt={params.get('alt') or 'json'}", request.uri)
        request.postproc = _raw_body
    for key, header in (("if_match", "If-Match"), ("if_none_match", "If-None-Match")):
        if params.get(key) is not None:
            if not isinstance(params[key], str):
                raise ValueError(f"{key} must be an HTTP etag string")
            request.headers[header] = params[key]
    return request


def _result(response: Any, method: dict[str, Any], params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    if not method.get("response") and not method.get("supportsMediaDownload"):
        return {"response": None, "method": method["id"]}
    raw_mime = raw_reply_mime(method, params)
    if raw_mime is not None:
        key = io.blob_writer(response, method["id"], raw_mime)
        return {"file_blob_key": key, "mime_type": raw_mime, "size": len(response), "method": method["id"]}
    if isinstance(response, bytes):
        mime = str(params.get("mimeType") or "application/octet-stream")
        key = io.blob_writer(response, str(params.get("fileId") or "download"), mime)
        return {"file_blob_key": key, "mime_type": mime, "size": len(response)}
    return {"response": response, "method": method["id"]}


def execute_method(service: Any, verb: str, params: dict[str, Any], io: GmailIO, facts: dict[str, Any]) -> dict[str, Any]:
    """Execute one vendor request with explicit account, scopes and concurrency."""
    _, method = WORKSPACE_METHODS[verb]
    _check_entitlement(verb, method, facts)
    kwargs = _request_kwargs(verb, params, io)
    request = _build_request(service, method, kwargs, params)
    response_headers: dict[str, str] = {}

    def capture_headers(response: Any) -> None:
        for header in ("etag", "last-modified"):
            if response.get(header):
                response_headers[header] = str(response[header])

    request.add_response_callback(capture_headers)
    result = _result(request.execute(), method, params, io)
    result["response_headers"] = response_headers
    return result


def producer(factory: Callable[[], Any], io: Callable[[], GmailIO], verb: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def produce(params: dict[str, Any]) -> dict[str, Any]:
        account_factory = factory()
        product, _ = WORKSPACE_METHODS[verb]
        return execute_method(account_factory.workspace_api(product), verb, params, io(), account_factory.credential_facts())

    return produce
