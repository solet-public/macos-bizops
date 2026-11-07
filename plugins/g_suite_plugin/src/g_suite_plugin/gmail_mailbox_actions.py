"""Gmail message writes, attachments and threads (users.messages.*, users.threads.*).

Each verb is ``(gmail, params, io) -> dict`` over the documented Gmail v1 REST
method named in its docstring. Writes and deletes are first-class; Google's
OAuth scope is the only boundary (permanent delete needs the opt-in full
mailbox scope, see constants.GMAIL_FULL_MAILBOX_SCOPE).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .constants import BLOB_NAMESPACE
from .gmail_actions import GmailIO, decode_b64url_bytes, shape_message
from .gmail_params import as_str, as_str_list, label_changes, list_args, optional_bool, page_fields, require_ids, require_str

_THREAD_FORMATS = frozenset({"full", "metadata", "minimal"})
_INTERNAL_DATE_SOURCES = frozenset({"receivedTime", "dateHeader"})


def _message_ref(message: dict[str, Any]) -> dict[str, Any]:
    return {"id": message.get("id"), "thread_id": message.get("threadId"), "label_ids": message.get("labelIds") or []}


def _thread_ref(thread: dict[str, Any]) -> dict[str, Any]:
    messages = thread.get("messages") or []
    return {
        "id": thread.get("id"),
        "history_id": as_str(thread.get("historyId")),
        "message_ids": [m.get("id") for m in messages],
        "label_ids": sorted({label for m in messages for label in m.get("labelIds") or []}),
    }


# ---------------------------------------------------------------------------
# users.messages writes
# ---------------------------------------------------------------------------


def modify_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.modify: add/remove labels (mark_read, archive, star conveniences)."""
    message_id = require_str(params, "id")
    body = label_changes(params)
    return _message_ref(gmail.users().messages().modify(userId="me", id=message_id, body=body).execute())


def batch_modify_messages(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.batchModify: one label change across up to 1,000 messages."""
    ids = require_ids(params)
    body = {"ids": ids, **label_changes(params)}
    gmail.users().messages().batchModify(userId="me", body=body).execute()
    return {"modified_count": len(ids), "add_label_ids": body["addLabelIds"], "remove_label_ids": body["removeLabelIds"]}


def trash_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.trash: move to Trash (recoverable for 30 days)."""
    return _message_ref(gmail.users().messages().trash(userId="me", id=require_str(params, "id")).execute())


def untrash_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.untrash: restore from Trash."""
    return _message_ref(gmail.users().messages().untrash(userId="me", id=require_str(params, "id")).execute())


def delete_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.delete: permanent, immediate delete (needs https://mail.google.com/)."""
    message_id = require_str(params, "id")
    gmail.users().messages().delete(userId="me", id=message_id).execute()
    return {"id": message_id, "deleted": True}


def batch_delete_messages(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.batchDelete: permanently delete up to 1,000 messages (needs https://mail.google.com/)."""
    ids = require_ids(params)
    gmail.users().messages().batchDelete(userId="me", body={"ids": ids}).execute()
    return {"deleted_count": len(ids), "ids": ids}


def insert_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.insert: place a raw RFC 822 message in the mailbox, like IMAP APPEND (no scanning, no sending)."""
    kwargs = _raw_upload_kwargs(params)
    return _message_ref(gmail.users().messages().insert(**kwargs).execute())


def import_message(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.import: import a raw RFC 822 message with standard delivery scanning and classification."""
    kwargs = _raw_upload_kwargs(params)
    if optional_bool(params, "never_mark_spam"):
        kwargs["neverMarkSpam"] = True
    if optional_bool(params, "process_for_calendar"):
        kwargs["processForCalendar"] = True
    return _message_ref(gmail.users().messages().import_(**kwargs).execute())


def _raw_upload_kwargs(params: dict[str, Any]) -> dict[str, Any]:
    raw = as_str(params.get("raw"))
    raw_rfc822 = as_str(params.get("raw_rfc822"))
    if bool(raw) == bool(raw_rfc822):
        raise ValueError("pass exactly one of 'raw' (base64url, as gmail_get_message format=raw returns) or 'raw_rfc822' (the message text)")
    encoded = raw or base64.urlsafe_b64encode(raw_rfc822.encode()).decode()
    body: dict[str, Any] = {"raw": encoded}
    label_ids = as_str_list(params.get("label_ids"))
    if label_ids:
        body["labelIds"] = label_ids
    kwargs: dict[str, Any] = {"userId": "me", "body": body}
    source = as_str(params.get("internal_date_source"))
    if source:
        if source not in _INTERNAL_DATE_SOURCES:
            raise ValueError(f"'internal_date_source' must be one of {sorted(_INTERNAL_DATE_SOURCES)}")
        kwargs["internalDateSource"] = source
    if optional_bool(params, "deleted"):
        kwargs["deleted"] = True
    return kwargs


# ---------------------------------------------------------------------------
# users.messages.attachments
# ---------------------------------------------------------------------------


def get_attachment(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.messages.attachments.get: bytes to a local path, or to blob storage by default."""
    message_id = require_str(params, "message_id")
    attachment_id = require_str(params, "attachment_id")
    response = gmail.users().messages().attachments().get(userId="me", messageId=message_id, id=attachment_id).execute()
    content = decode_b64url_bytes(as_str(response.get("data")))
    result: dict[str, Any] = {"message_id": message_id, "attachment_id": attachment_id, "size": len(content)}
    path = as_str(params.get("path"))
    if path:
        result["path"] = str(_write_local(Path(path), content, optional_bool(params, "overwrite") is True))
        return result
    name = as_str(params.get("filename")) or f"gmail-attachment-{attachment_id[:16]}"
    mime = as_str(params.get("mime_type")) or "application/octet-stream"
    result.update({"attachment_blob_key": io.blob_writer(content, name, mime), "namespace": BLOB_NAMESPACE, "name": name, "mime": mime})
    return result


def _write_local(target: Path, content: bytes, overwrite: bool) -> Path:
    if not target.is_absolute():
        raise ValueError(f"'path' must be absolute, got '{target}'")
    if not target.parent.is_dir():
        raise ValueError(f"'path' parent directory does not exist: {target.parent}")
    if target.exists() and not overwrite:
        raise ValueError(f"'{target}' already exists; pass overwrite=true to replace it")
    target.write_bytes(content)
    return target


# ---------------------------------------------------------------------------
# users.threads
# ---------------------------------------------------------------------------


def list_threads(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.threads.list: conversations matching a Gmail query, one page."""
    kwargs = list_args(params)
    label_ids = as_str_list(params.get("label_ids"))
    if label_ids:
        kwargs["labelIds"] = label_ids
    response = gmail.users().threads().list(**kwargs).execute()
    rows = [{"id": t.get("id"), "snippet": as_str(t.get("snippet")), "history_id": as_str(t.get("historyId"))} for t in response.get("threads") or []]
    return {"threads": rows, "count": len(rows), **page_fields(response)}


def get_thread(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.threads.get: every message of one conversation, parsed like gmail_get_message."""
    thread_id = require_str(params, "id")
    fmt = as_str(params.get("format")) or "full"
    if fmt not in _THREAD_FORMATS:
        raise ValueError(f"'format' must be one of {sorted(_THREAD_FORMATS)}, got '{fmt}'")
    kwargs: dict[str, Any] = {"userId": "me", "id": thread_id, "format": fmt}
    metadata_headers = as_str_list(params.get("metadata_headers"))
    if fmt == "metadata" and metadata_headers:
        kwargs["metadataHeaders"] = metadata_headers
    thread = gmail.users().threads().get(**kwargs).execute()
    messages = [shape_message(m, fmt) for m in thread.get("messages") or []]
    return {"id": thread.get("id"), "history_id": as_str(thread.get("historyId")), "messages": messages, "message_count": len(messages)}


def modify_thread(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.threads.modify: label change applied to every message in the thread."""
    thread_id = require_str(params, "id")
    return _thread_ref(gmail.users().threads().modify(userId="me", id=thread_id, body=label_changes(params)).execute())


def trash_thread(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.threads.trash: move the whole conversation to Trash."""
    return _thread_ref(gmail.users().threads().trash(userId="me", id=require_str(params, "id")).execute())


def untrash_thread(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.threads.untrash: restore the whole conversation from Trash."""
    return _thread_ref(gmail.users().threads().untrash(userId="me", id=require_str(params, "id")).execute())


def delete_thread(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.threads.delete: permanently delete the conversation (needs https://mail.google.com/)."""
    thread_id = require_str(params, "id")
    gmail.users().threads().delete(userId="me", id=thread_id).execute()
    return {"id": thread_id, "deleted": True}
