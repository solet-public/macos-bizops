"""Gmail labels, drafts, history and profile (users.labels.*, users.drafts.*, users.history.list, users.getProfile).

Each verb is ``(gmail, params, io) -> dict`` over the documented Gmail v1 REST
method named in its docstring.
"""

from __future__ import annotations

from typing import Any

from .gmail_actions import GmailIO, build_outgoing, shape_message
from .gmail_params import as_str, as_str_list, clamp_max, list_args, page_fields, require_str

_LABEL_LIST_VISIBILITY = frozenset({"labelShow", "labelShowIfUnread", "labelHide"})
_MESSAGE_LIST_VISIBILITY = frozenset({"show", "hide"})
_DRAFT_FORMATS = frozenset({"full", "raw", "metadata", "minimal"})
_HISTORY_TYPES = frozenset({"messageAdded", "messageDeleted", "labelAdded", "labelRemoved"})


# ---------------------------------------------------------------------------
# users.labels
# ---------------------------------------------------------------------------


def _shape_label(label: dict[str, Any]) -> dict[str, Any]:
    color = label.get("color") or {}
    shaped: dict[str, Any] = {
        "id": label.get("id"),
        "name": as_str(label.get("name")),
        "type": as_str(label.get("type")),
        "label_list_visibility": as_str(label.get("labelListVisibility")),
        "message_list_visibility": as_str(label.get("messageListVisibility")),
        "text_color": as_str(color.get("textColor")),
        "background_color": as_str(color.get("backgroundColor")),
    }
    for source, target in (("messagesTotal", "messages_total"), ("messagesUnread", "messages_unread"), ("threadsTotal", "threads_total"), ("threadsUnread", "threads_unread")):
        if source in label:
            shaped[target] = int(label.get(source) or 0)
    return shaped


def _label_body(params: dict[str, Any], *, require_name: bool) -> dict[str, Any]:
    body: dict[str, Any] = {}
    name = require_str(params, "name") if require_name else as_str(params.get("name"))
    if name:
        body["name"] = name
    for key, field, allowed in (
        ("label_list_visibility", "labelListVisibility", _LABEL_LIST_VISIBILITY),
        ("message_list_visibility", "messageListVisibility", _MESSAGE_LIST_VISIBILITY),
    ):
        value = as_str(params.get(key))
        if value and value not in allowed:
            raise ValueError(f"'{key}' must be one of {sorted(allowed)}")
        if value:
            body[field] = value
    text, background = as_str(params.get("text_color")), as_str(params.get("background_color"))
    if bool(text) != bool(background):
        raise ValueError("pass both 'text_color' and 'background_color' (Gmail's palette hex values) or neither")
    if text:
        body["color"] = {"textColor": text, "backgroundColor": background}
    if not body:
        raise ValueError("nothing to change: pass name, a visibility or both colors")
    return body


def list_labels(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.labels.list: every system and user label (id to name mapping)."""
    labels = [_shape_label(label) for label in gmail.users().labels().list(userId="me").execute().get("labels") or []]
    return {"labels": labels, "count": len(labels)}


def get_label(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.labels.get: one label with its message and thread counts."""
    return _shape_label(gmail.users().labels().get(userId="me", id=require_str(params, "id")).execute())


def create_label(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.labels.create: a new user label."""
    body = _label_body(params, require_name=True)
    return _shape_label(gmail.users().labels().create(userId="me", body=body).execute())


def patch_label(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.labels.patch: change only the fields passed (rename, visibility, color)."""
    label_id = require_str(params, "id")
    body = _label_body(params, require_name=False)
    return _shape_label(gmail.users().labels().patch(userId="me", id=label_id, body=body).execute())


def update_label(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.labels.update: replace the label resource (name required; omitted fields reset)."""
    label_id = require_str(params, "id")
    body = {"id": label_id, **_label_body(params, require_name=True)}
    return _shape_label(gmail.users().labels().update(userId="me", id=label_id, body=body).execute())


def delete_label(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.labels.delete: delete a user label (messages keep existing, only the label goes)."""
    label_id = require_str(params, "id")
    gmail.users().labels().delete(userId="me", id=label_id).execute()
    return {"id": label_id, "deleted": True}


# ---------------------------------------------------------------------------
# users.drafts
# ---------------------------------------------------------------------------


def _shape_draft(draft: dict[str, Any], fmt: str = "minimal") -> dict[str, Any]:
    message = draft.get("message") or {}
    return {"id": draft.get("id"), "message": shape_message(message, fmt) if fmt != "minimal" else {"id": message.get("id"), "thread_id": message.get("threadId"), "label_ids": message.get("labelIds") or []}}


def create_draft(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.drafts.create: a draft from to/cc/bcc/subject/body/html_body/attachments (reply: thread_id + in_reply_to)."""
    draft = gmail.users().drafts().create(userId="me", body={"message": build_outgoing(params, io)}).execute()
    return _shape_draft(draft)


def update_draft(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.drafts.update: replace a draft's message content."""
    draft_id = require_str(params, "id")
    draft = gmail.users().drafts().update(userId="me", id=draft_id, body={"id": draft_id, "message": build_outgoing(params, io)}).execute()
    return _shape_draft(draft)


def get_draft(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.drafts.get: one draft in full, raw, metadata or minimal format."""
    fmt = as_str(params.get("format")) or "full"
    if fmt not in _DRAFT_FORMATS:
        raise ValueError(f"'format' must be one of {sorted(_DRAFT_FORMATS)}, got '{fmt}'")
    draft = gmail.users().drafts().get(userId="me", id=require_str(params, "id"), format=fmt).execute()
    return _shape_draft(draft, fmt)


def list_drafts(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.drafts.list: drafts matching a Gmail query, one page."""
    response = gmail.users().drafts().list(**list_args(params)).execute()
    rows = [_shape_draft(draft) for draft in response.get("drafts") or []]
    return {"drafts": rows, "count": len(rows), **page_fields(response)}


def send_draft(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.drafts.send: send an existing draft as it stands (the draft is consumed)."""
    sent = gmail.users().drafts().send(userId="me", body={"id": require_str(params, "id")}).execute()
    return {"id": sent.get("id"), "thread_id": sent.get("threadId"), "label_ids": sent.get("labelIds") or []}


def delete_draft(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.drafts.delete: permanently delete a draft (not moved to Trash)."""
    draft_id = require_str(params, "id")
    gmail.users().drafts().delete(userId="me", id=draft_id).execute()
    return {"id": draft_id, "deleted": True}


# ---------------------------------------------------------------------------
# users.history / users.getProfile
# ---------------------------------------------------------------------------


def _shape_history(record: dict[str, Any]) -> dict[str, Any]:
    def refs(key: str, with_labels: bool) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in record.get(key) or []:
            message = item.get("message") or {}
            row: dict[str, Any] = {"id": message.get("id"), "thread_id": message.get("threadId")}
            if with_labels:
                row["label_ids"] = item.get("labelIds") or []
            rows.append(row)
        return rows

    return {
        "id": as_str(record.get("id")),
        "messages_added": refs("messagesAdded", False),
        "messages_deleted": refs("messagesDeleted", False),
        "labels_added": refs("labelsAdded", True),
        "labels_removed": refs("labelsRemoved", True),
    }


def list_history(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.history.list: mailbox changes since start_history_id (incremental sync)."""
    kwargs: dict[str, Any] = {"userId": "me", "startHistoryId": require_str(params, "start_history_id"), "maxResults": clamp_max(params.get("max"))}
    history_types = as_str_list(params.get("history_types"))
    unknown = sorted(set(history_types) - _HISTORY_TYPES)
    if unknown:
        raise ValueError(f"'history_types' accepts {sorted(_HISTORY_TYPES)}, got {unknown}")
    if history_types:
        kwargs["historyTypes"] = history_types
    for key, field in (("label_id", "labelId"), ("page_token", "pageToken")):
        value = as_str(params.get(key))
        if value:
            kwargs[field] = value
    response = gmail.users().history().list(**kwargs).execute()
    records = [_shape_history(record) for record in response.get("history") or []]
    return {"history": records, "count": len(records), "history_id": as_str(response.get("historyId")), "next_page_token": as_str(response.get("nextPageToken"))}


def get_profile(gmail: Any, params: dict[str, Any], io: GmailIO) -> dict[str, Any]:
    """users.getProfile: mailbox address, message and thread totals, current history id."""
    profile = gmail.users().getProfile(userId="me").execute()
    return {
        "email_address": as_str(profile.get("emailAddress")),
        "messages_total": int(profile.get("messagesTotal") or 0),
        "threads_total": int(profile.get("threadsTotal") or 0),
        "history_id": as_str(profile.get("historyId")),
    }
