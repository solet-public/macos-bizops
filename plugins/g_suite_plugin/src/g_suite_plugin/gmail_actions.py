"""Gmail message read/send verbs plus the helpers every Gmail module shares.

Every Gmail verb is a pure function ``(gmail, params, io) -> dict`` over an
already-built ``gmail`` service client (from
:class:`oauth.service_factory.GoogleServiceFactory`). Blob I/O stays OUT of
these modules: :class:`GmailIO` carries the plugin's attachment loader and
blob writer, so the plugin owns all blob-storage coupling while these modules
own Gmail REST shapes and MIME.

Invalid parameters raise ``ValueError``; the plugin's safe-call wrapper maps
that to ``gsuite.invalid_params``. Google API errors propagate to the plugin's
``HttpError`` classifier.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

from .gmail_params import as_str, as_str_list, list_args, page_fields, require_str

_GET_FORMATS = frozenset({"full", "raw", "metadata", "minimal"})
_WANTED_HEADERS = frozenset({"from", "to", "cc", "bcc", "subject", "date", "reply-to", "message-id", "in-reply-to", "references"})


@dataclass(frozen=True)
class OutgoingAttachment:
    """A blob resolved to bytes, ready to attach to an outgoing message."""

    filename: str
    mime_type: str
    content: bytes


AttachmentLoader = Callable[[str], OutgoingAttachment]
BlobWriter = Callable[[bytes, str, str], str]


@dataclass(frozen=True)
class GmailIO:
    """Plugin-owned blob coupling handed to every Gmail verb."""

    attachment_loader: AttachmentLoader
    blob_writer: BlobWriter


def list_messages(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.list: ids matching a Gmail query, one page (newest first)."""
    kwargs = list_args(params)
    label_ids = as_str_list(params.get("label_ids"))
    if label_ids:
        kwargs["labelIds"] = label_ids
    response = gmail.users().messages().list(**kwargs).execute()
    rows = [{"id": m.get("id"), "thread_id": m.get("threadId")} for m in response.get("messages") or []]
    return {"messages": rows, "count": len(rows), **page_fields(response)}


def get_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.get in full (parsed), raw (RFC 822), metadata or minimal format."""
    message_id = require_str(params, "id")
    fmt = as_str(params.get("format")) or "full"
    if fmt not in _GET_FORMATS:
        raise ValueError(f"'format' must be one of {sorted(_GET_FORMATS)}, got '{fmt}'")
    kwargs: dict[str, Any] = {"userId": "me", "id": message_id, "format": fmt}
    metadata_headers = as_str_list(params.get("metadata_headers"))
    if fmt == "metadata" and metadata_headers:
        kwargs["metadataHeaders"] = metadata_headers
    message = gmail.users().messages().get(**kwargs).execute()
    return shape_message(message, fmt)


def send_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.send: plain-text and/or HTML, cc/bcc, blob attachments, replies."""
    body = build_outgoing(params, io)
    sent = gmail.users().messages().send(userId="me", body=body).execute()
    return {"id": sent.get("id"), "thread_id": sent.get("threadId"), "label_ids": sent.get("labelIds") or []}


# ---------------------------------------------------------------------------
# Shared message shaping
# ---------------------------------------------------------------------------


def shape_message(message: dict[str, Any], fmt: str = "full") -> dict[str, Any]:
    """Map a Gmail Message resource to this plugin's snake_case result."""
    shaped: dict[str, Any] = {
        "id": message.get("id"),
        "thread_id": message.get("threadId"),
        "label_ids": message.get("labelIds") or [],
        "snippet": as_str(message.get("snippet")),
        "internal_date": as_str(message.get("internalDate")),
        "history_id": as_str(message.get("historyId")),
        "size_estimate": int(message.get("sizeEstimate") or 0),
    }
    if fmt == "raw":
        raw = as_str(message.get("raw"))
        shaped["raw"] = raw
        shaped["raw_rfc822"] = decode_b64url(raw)
        return shaped
    payload = message.get("payload") or {}
    if fmt == "metadata":
        shaped["headers_all"] = [{"name": as_str(h.get("name")), "value": as_str(h.get("value"))} for h in payload.get("headers") or []]
    if fmt in ("full", "metadata"):
        shaped["headers"] = _extract_headers(payload)
    if fmt == "full":
        body_text, body_html, attachments = _walk_parts(payload)
        shaped.update({"body_text": body_text, "body_html": body_html, "attachments": attachments})
    return shaped


def build_outgoing(params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """Build a Gmail ``{raw, threadId?}`` body from message fields (send and drafts)."""
    to_addr = require_str(params, "to")
    attachments = [io.attachment_loader(blob_id) for blob_id in as_str_list(params.get("attachments"))]
    mime = _build_mime(to_addr, params, attachments)
    body: dict[str, Any] = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
    thread_id = as_str(params.get("thread_id"))
    if thread_id:
        body["threadId"] = thread_id
    return body


def decode_b64url(data: str) -> str:
    if not data:
        return ""
    return decode_b64url_bytes(data).decode("utf-8", errors="replace")


def decode_b64url_bytes(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded)


def _build_mime(to_addr: str, params: dict[str, Any], attachments: list[OutgoingAttachment]) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to_addr
    for header, key in (("Cc", "cc"), ("Bcc", "bcc"), ("In-Reply-To", "in_reply_to"), ("References", "references")):
        value = as_str(params.get(key))
        if value:
            message[header] = value
    message["Subject"] = as_str(params.get("subject"))
    message.set_content(as_str(params.get("body")))
    html_body = as_str(params.get("html_body"))
    if html_body:
        message.add_alternative(html_body, subtype="html")
    for attachment in attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        message.add_attachment(
            attachment.content,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.filename,
        )
    return message


def _extract_headers(payload: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        name = as_str(header.get("name")).lower()
        if name in _WANTED_HEADERS:
            headers[name] = as_str(header.get("value"))
    return headers


def _walk_parts(payload: dict[str, Any]) -> tuple[str, str, list[dict[str, Any]]]:
    """Recursively collect the first text/plain and text/html bodies and all attachment metadata."""
    bodies = {"text/plain": "", "text/html": ""}
    attachments: list[dict[str, Any]] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = as_str(part.get("mimeType"))
        body = part.get("body") or {}
        filename = as_str(part.get("filename"))
        if filename and body.get("attachmentId"):
            attachments.append(
                {
                    "attachment_id": as_str(body.get("attachmentId")),
                    "name": filename,
                    "mime": mime_type,
                    "size": int(body.get("size") or 0),
                }
            )
        elif mime_type in bodies and not bodies[mime_type]:
            bodies[mime_type] = decode_b64url(as_str(body.get("data")))
        for child in part.get("parts") or []:
            visit(child)

    visit(payload)
    return bodies["text/plain"], bodies["text/html"], attachments
