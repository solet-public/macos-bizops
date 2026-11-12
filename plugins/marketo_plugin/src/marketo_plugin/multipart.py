"""JSON-safe multipart inputs; HTTPX owns boundary and disposition encoding."""

from __future__ import annotations

import base64
import binascii
from typing import Any


def _metadata(part: dict[str, Any], key: str) -> str:
    value = part.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"each file requires a {key} string")
    if any(c in value for c in "\r\n"):
        raise ValueError("multipart metadata must not contain CR/LF")
    return value


def _part(name: Any, part: Any) -> dict[str, str]:
    if not isinstance(name, str) or not name or not isinstance(part, dict):
        raise ValueError("multipart_files requires named file part objects")
    _metadata({"name": name}, "name")
    filename = _metadata(part, "filename")
    content_type = _metadata(part, "content_type")
    content = part.get("content_base64")
    if not isinstance(content, str):
        raise ValueError("each file requires content_base64")
    try:
        base64.b64decode(content, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("content_base64 must be valid base64") from exc
    return {"filename": filename, "content_type": content_type, "content_base64": content}


def prepare_files(value: Any) -> dict[str, dict[str, str]] | None:
    """Validate file parts before job creation without reading arbitrary local paths."""
    if value is None:
        return None
    if not isinstance(value, dict) or not value:
        raise ValueError("multipart_files must be a nonempty object of file parts")
    return {name: _part(name, part) for name, part in value.items()}


def decode_files(parts: dict[str, dict[str, str]] | None) -> dict[str, tuple[str, bytes, str]] | None:
    """Rebuild replayable byte parts for each auth retry in the background worker."""
    if parts is None:
        return None
    return {name: (part["filename"], base64.b64decode(part["content_base64"], validate=True), part["content_type"]) for name, part in parts.items()}
