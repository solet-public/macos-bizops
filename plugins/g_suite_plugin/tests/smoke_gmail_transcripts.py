#!/usr/bin/env python3
"""Gmail REST-shape smoke: every Gmail verb through the REAL google-api-python-client.

The Gmail service is built from the discovery document bundled with
google-api-python-client (the same static document the plugin uses in
production) over a recording httplib2 stand-in. Each check asserts the exact
HTTP method, REST path, query parameters and JSON body the client sent, and
feeds back a real-shaped Gmail response (envelopes, nextPageToken,
resultSizeEstimate) or a real Google error body (400/401/403/404/429). No
network, no credentials.

Also checks, for every verb in gmail_registry.GMAIL_OPERATIONS, the three
registration artifacts: the @platform_process decorator, the EDGE definition
and the knowledge-base process JSON (process_key match, embedding_description
200-400 characters), plus a deferred producer.

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_gmail_transcripts.py
"""

from __future__ import annotations

import base64
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httplib2

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from g_suite_plugin.gmail_actions import GmailIO, OutgoingAttachment  # noqa: E402
from g_suite_plugin.gmail_registry import GMAIL_OPERATIONS  # noqa: E402
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from gsuite_fakes import connect_accounts  # noqa: E402  # pyright: ignore[reportMissingImports]

KB_DIR = REPO_ROOT / "plugins" / "g_suite_plugin" / "knowledge_base" / "processes"
BASE = "/gmail/v1/users/me"
_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


class RecordingHttp:
    """httplib2.Http stand-in: records each request, replays scripted (status, body)."""

    def __init__(self, responses: list[tuple[int, Any]]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, uri: str, method: str = "GET", body: Any = None, headers: Any = None, **_: Any) -> tuple[httplib2.Response, bytes]:
        parts = urlsplit(uri)
        payload = json.loads(body) if isinstance(body, (str, bytes)) and body else None
        self.requests.append({"method": method, "path": parts.path, "query": parse_qs(parts.query), "body": payload})
        status, content = self.responses.pop(0)
        raw = b"" if content is None else json.dumps(content).encode()
        return httplib2.Response({"status": str(status), "content-type": "application/json"}), raw


def _gmail(*responses: tuple[int, Any]) -> tuple[Any, RecordingHttp]:
    http = RecordingHttp(list(responses))
    return build("gmail", "v1", http=http, cache_discovery=False, static_discovery=True), http


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


_BLOBS: dict[str, tuple[bytes, str, str]] = {}


def _writer(content: bytes, name: str, mime: str) -> str:
    key = f"blob-{len(_BLOBS) + 1}"
    _BLOBS[key] = (content, name, mime)
    return key


def _loader(blob_id: str) -> OutgoingAttachment:
    return OutgoingAttachment(filename="report.csv", mime_type="text/csv", content=b"a,b\n1,2\n")


IO = GmailIO(attachment_loader=_loader, blob_writer=_writer)
MSG = {"id": "18c1", "threadId": "18c0", "labelIds": ["INBOX", "UNREAD"]}
FULL = {
    **MSG,
    "snippet": "Quarterly numbers",
    "historyId": "9001",
    "internalDate": "1730000000000",
    "sizeEstimate": 4321,
    "payload": {
        "mimeType": "multipart/mixed",
        "headers": [{"name": "From", "value": "Ann <ann@example.com>"}, {"name": "Subject", "value": "Q3"}, {"name": "Message-ID", "value": "<m1@example.com>"}],
        "parts": [
            {"mimeType": "multipart/alternative", "parts": [{"mimeType": "text/plain", "body": {"data": _b64("plain body")}}, {"mimeType": "text/html", "body": {"data": _b64("<p>html body</p>")}}]},
            {"mimeType": "application/pdf", "filename": "q3.pdf", "body": {"attachmentId": "ANGjdJ9", "size": 2048}},
        ],
    },
}
THREAD = {"id": "18c0", "historyId": "9002", "messages": [FULL, {**MSG, "id": "18c2", "labelIds": ["SENT"]}]}
LABEL = {"id": "Label_7", "name": "Clients/Acme", "type": "user", "labelListVisibility": "labelShow", "messageListVisibility": "show", "color": {"textColor": "#000000", "backgroundColor": "#16a766"}}
DRAFT = {"id": "r-55", "message": {"id": "18d1", "threadId": "18c0", "labelIds": ["DRAFT"]}}


def _call(verb: str, params: dict[str, Any], *responses: tuple[int, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    gmail, http = _gmail(*responses)
    action = GMAIL_OPERATIONS[verb][0]
    return action(gmail, params, IO), http.requests


def _check_messages_0() -> None:
    result, reqs = _call(
        "gmail_list_messages",
        {"query": "is:unread from:ann", "page_token": "p2", "label_ids": ["INBOX"], "include_spam_trash": True, "max": 50},
        (200, {"messages": [{"id": "18c1", "threadId": "18c0"}], "nextPageToken": "p3", "resultSizeEstimate": 120}),
    )
    r = reqs[0]
    _assert("list: GET users.messages", r["method"] == "GET" and r["path"] == f"{BASE}/messages")
    _assert("list: q/pageToken/labelIds/maxResults/includeSpamTrash sent", {k: v for k, v in r["query"].items() if k != "alt"} == {"q": ["is:unread from:ann"], "pageToken": ["p2"], "labelIds": ["INBOX"], "maxResults": ["50"], "includeSpamTrash": ["true"]}, str(r["query"]))
    _assert("list: next_page_token + estimate returned", result["next_page_token"] == "p3" and result["result_size_estimate"] == 120)


def _check_messages_1() -> None:
    result, reqs = _call("gmail_get_message", {"id": "18c1"}, (200, FULL))
    _assert("get full: GET .../messages/{id}?format=full", reqs[0]["path"] == f"{BASE}/messages/18c1" and reqs[0]["query"]["format"] == ["full"])
    _assert("get full: text + html bodies", result["body_text"] == "plain body" and result["body_html"] == "<p>html body</p>")
    _assert("get full: message-id header + labels", result["headers"]["message-id"] == "<m1@example.com>" and result["label_ids"] == ["INBOX", "UNREAD"])
    _assert("get full: attachment metadata", result["attachments"] == [{"attachment_id": "ANGjdJ9", "name": "q3.pdf", "mime": "application/pdf", "size": 2048}])


def _check_messages_2() -> None:
    raw_text = "From: ann@example.com\r\nSubject: Q3\r\n\r\nhello"
    result, reqs = _call("gmail_get_message", {"id": "18c1", "format": "raw"}, (200, {**MSG, "raw": _b64(raw_text)}))
    _assert("get raw: format=raw sent", reqs[0]["query"]["format"] == ["raw"])
    _assert("get raw: raw kept lossless + decoded text", result["raw"] == _b64(raw_text) and result["raw_rfc822"] == raw_text)


def _check_messages_3() -> None:
    result, reqs = _call("gmail_get_message", {"id": "18c1", "format": "metadata", "metadata_headers": ["From", "Subject"]}, (200, {**MSG, "payload": {"headers": [{"name": "From", "value": "a@b"}]}}))
    _assert("get metadata: metadataHeaders sent", reqs[0]["query"]["metadataHeaders"] == ["From", "Subject"] and result["headers_all"] == [{"name": "From", "value": "a@b"}])


def _check_messages_4() -> None:
    result, reqs = _call(
        "gmail_send",
        {"to": "bob@example.com", "cc": "c@example.com", "subject": "Re: Q3", "body": "text", "html_body": "<b>x</b>", "attachments": ["blob-a"], "thread_id": "18c0", "in_reply_to": "<m1@example.com>", "references": "<m1@example.com>"},
        (200, {"id": "18e1", "threadId": "18c0", "labelIds": ["SENT"]}),
    )
    sent = reqs[0]
    mime = base64.urlsafe_b64decode(sent["body"]["raw"]).decode()
    _assert("send: POST users.messages.send", sent["method"] == "POST" and sent["path"] == f"{BASE}/messages/send")
    _assert("send: reply threaded (threadId + In-Reply-To + References)", sent["body"]["threadId"] == "18c0" and "In-Reply-To: <m1@example.com>" in mime and "References: <m1@example.com>" in mime)
    _assert("send: cc, html alternative and attachment in MIME", "Cc: c@example.com" in mime and "text/html" in mime and "report.csv" in mime)
    _assert("send: result ids", result == {"id": "18e1", "thread_id": "18c0", "label_ids": ["SENT"]})


def check_messages() -> None:
    _check_messages_0()
    _check_messages_1()
    _check_messages_2()
    _check_messages_3()
    _check_messages_4()


def _check_message_writes_0() -> None:
    result, reqs = _call("gmail_modify_message", {"id": "18c1", "mark_read": True, "archive": True, "add_label_ids": ["Label_7"]}, (200, {**MSG, "labelIds": ["Label_7"]}))
    _assert("modify: POST .../messages/{id}/modify", reqs[0]["method"] == "POST" and reqs[0]["path"] == f"{BASE}/messages/18c1/modify")
    _assert("modify: mark_read+archive map to removing UNREAD+INBOX", reqs[0]["body"] == {"addLabelIds": ["Label_7"], "removeLabelIds": ["UNREAD", "INBOX"]}, str(reqs[0]["body"]))
    _assert("modify: resulting labels returned", result["label_ids"] == ["Label_7"])


def _check_message_writes_1() -> None:
    result, reqs = _call("gmail_batch_modify_messages", {"ids": ["a", "b"], "star": True, "mark_read": False}, (204, None))
    _assert("batchModify: POST .../messages/batchModify with ids", reqs[0]["path"] == f"{BASE}/messages/batchModify" and reqs[0]["body"] == {"ids": ["a", "b"], "addLabelIds": ["UNREAD", "STARRED"], "removeLabelIds": []}, str(reqs[0]["body"]))
    _assert("batchModify: count", result["modified_count"] == 2)


def _check_message_writes_2() -> None:
    for verb, suffix in (("gmail_trash_message", "trash"), ("gmail_untrash_message", "untrash")):
        result, reqs = _call(verb, {"id": "18c1"}, (200, MSG))
        _assert(f"{suffix}: POST .../messages/{{id}}/{suffix}", reqs[0]["method"] == "POST" and reqs[0]["path"] == f"{BASE}/messages/18c1/{suffix}" and result["id"] == "18c1")


def _check_message_writes_3() -> None:
    result, reqs = _call("gmail_delete_message", {"id": "18c1"}, (204, None))
    _assert("delete: DELETE .../messages/{id}", reqs[0]["method"] == "DELETE" and reqs[0]["path"] == f"{BASE}/messages/18c1" and result == {"id": "18c1", "deleted": True})


def _check_message_writes_4() -> None:
    result, reqs = _call("gmail_batch_delete_messages", {"ids": ["a", "b", "c"]}, (204, None))
    _assert("batchDelete: POST .../messages/batchDelete", reqs[0]["path"] == f"{BASE}/messages/batchDelete" and reqs[0]["body"] == {"ids": ["a", "b", "c"]} and result["deleted_count"] == 3)


def _check_message_writes_5() -> None:
    result, reqs = _call("gmail_insert_message", {"raw_rfc822": "Subject: x\r\n\r\nbody", "label_ids": ["INBOX"], "internal_date_source": "dateHeader"}, (200, MSG))
    _assert("insert: POST users.messages (insert)", reqs[0]["method"] == "POST" and reqs[0]["path"] == f"{BASE}/messages" and reqs[0]["query"]["internalDateSource"] == ["dateHeader"])
    _assert("insert: raw base64url + labelIds body", base64.urlsafe_b64decode(reqs[0]["body"]["raw"]) == b"Subject: x\r\n\r\nbody" and reqs[0]["body"]["labelIds"] == ["INBOX"])
    _assert("insert: message ref returned", result == {"id": "18c1", "thread_id": "18c0", "label_ids": ["INBOX", "UNREAD"]}, str(result))


def _check_message_writes_6() -> None:
    result, reqs = _call("gmail_import_message", {"raw": _b64("Subject: y\r\n\r\nz"), "never_mark_spam": True, "process_for_calendar": True}, (200, MSG))
    _assert("import: POST .../messages/import with flags", reqs[0]["path"] == f"{BASE}/messages/import" and reqs[0]["query"]["neverMarkSpam"] == ["true"] and reqs[0]["query"]["processForCalendar"] == ["true"])
    _assert("import: message ref returned", result["id"] == "18c1", str(result))


def _check_message_writes_7() -> None:
    for params in ({}, {"raw": "a", "raw_rfc822": "b"}):
        try:
            _call("gmail_import_message", params)
            ok = False
        except ValueError:
            ok = True
        _assert(f"import: exactly one of raw/raw_rfc822 required ({sorted(params)})", ok)


def _check_message_writes_8() -> None:
    for bad in ({"id": "x"}, {"id": "x", "add_label_ids": ["A"], "remove_label_ids": ["A"]}, {"ids": [str(i) for i in range(1001)], "star": True}):
        verb = "gmail_batch_modify_messages" if "ids" in bad else "gmail_modify_message"
        try:
            _call(verb, bad)
            ok = False
        except ValueError:
            ok = True
        _assert(f"invalid label change refused before any request ({verb})", ok)


def check_message_writes() -> None:
    _check_message_writes_0()
    _check_message_writes_1()
    _check_message_writes_2()
    _check_message_writes_3()
    _check_message_writes_4()
    _check_message_writes_5()
    _check_message_writes_6()
    _check_message_writes_7()
    _check_message_writes_8()


def check_attachment() -> None:
    data = b"%PDF-1.7 fake"
    result, reqs = _call("gmail_get_attachment", {"message_id": "18c1", "attachment_id": "ANGjdJ9", "filename": "q3.pdf", "mime_type": "application/pdf"}, (200, {"attachmentId": "ANGjdJ9", "size": len(data), "data": base64.urlsafe_b64encode(data).decode()}))
    _assert("attachment: GET .../messages/{mid}/attachments/{id}", reqs[0]["method"] == "GET" and reqs[0]["path"] == f"{BASE}/messages/18c1/attachments/ANGjdJ9")
    _assert("attachment: bytes stored as blob by default", _BLOBS[result["attachment_blob_key"]] == (data, "q3.pdf", "application/pdf"))
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "q3.pdf"
        body = {"attachmentId": "ANGjdJ9", "size": len(data), "data": base64.urlsafe_b64encode(data).decode()}
        result, _ = _call("gmail_get_attachment", {"message_id": "18c1", "attachment_id": "ANGjdJ9", "path": str(target)}, (200, body))
        _assert("attachment: written to the given path", target.read_bytes() == data and result["path"] == str(target))
        try:
            _call("gmail_get_attachment", {"message_id": "18c1", "attachment_id": "ANGjdJ9", "path": str(target)}, (200, body))
            refused = False
        except ValueError:
            refused = True
        _assert("attachment: existing file kept unless overwrite=true", refused)
        _call("gmail_get_attachment", {"message_id": "18c1", "attachment_id": "ANGjdJ9", "path": str(target), "overwrite": True}, (200, body))
        _assert("attachment: overwrite=true replaces", target.read_bytes() == data)


def _check_threads_0() -> None:
    result, reqs = _call("gmail_list_threads", {"query": "label:Clients", "page_token": "t2"}, (200, {"threads": [{"id": "18c0", "snippet": "Q3", "historyId": "9002"}], "nextPageToken": "t3", "resultSizeEstimate": 9}))
    _assert("threads.list: GET users.threads with q + pageToken", reqs[0]["path"] == f"{BASE}/threads" and reqs[0]["query"]["q"] == ["label:Clients"] and reqs[0]["query"]["pageToken"] == ["t2"])
    _assert("threads.list: paging fields", result["next_page_token"] == "t3" and result["threads"][0]["snippet"] == "Q3")


def _check_threads_1() -> None:
    result, reqs = _call("gmail_get_thread", {"id": "18c0"}, (200, THREAD))
    _assert("threads.get: GET .../threads/{id}?format=full", reqs[0]["path"] == f"{BASE}/threads/18c0" and reqs[0]["query"]["format"] == ["full"])
    _assert("threads.get: every message parsed", result["message_count"] == 2 and result["messages"][0]["body_text"] == "plain body")


def _check_threads_2() -> None:
    result, reqs = _call("gmail_modify_thread", {"id": "18c0", "archive": True}, (200, THREAD))
    _assert("threads.modify: POST .../threads/{id}/modify", reqs[0]["path"] == f"{BASE}/threads/18c0/modify" and reqs[0]["body"] == {"addLabelIds": [], "removeLabelIds": ["INBOX"]})
    _assert("threads.modify: message ids returned", result["message_ids"] == ["18c1", "18c2"])


def _check_threads_3() -> None:
    for verb, suffix in (("gmail_trash_thread", "trash"), ("gmail_untrash_thread", "untrash")):
        _, reqs = _call(verb, {"id": "18c0"}, (200, THREAD))
        _assert(f"threads.{suffix}: POST .../threads/{{id}}/{suffix}", reqs[0]["method"] == "POST" and reqs[0]["path"] == f"{BASE}/threads/18c0/{suffix}")


def _check_threads_4() -> None:
    result, reqs = _call("gmail_delete_thread", {"id": "18c0"}, (204, None))
    _assert("threads.delete: DELETE .../threads/{id}", reqs[0]["method"] == "DELETE" and reqs[0]["path"] == f"{BASE}/threads/18c0" and result["deleted"] is True)


def check_threads() -> None:
    _check_threads_0()
    _check_threads_1()
    _check_threads_2()
    _check_threads_3()
    _check_threads_4()


def _check_labels_0() -> None:
    result, reqs = _call("gmail_list_labels", {}, (200, {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}, LABEL]}))
    _assert("labels.list: GET users.labels", reqs[0]["path"] == f"{BASE}/labels" and result["count"] == 2 and result["labels"][1]["name"] == "Clients/Acme")


def _check_labels_1() -> None:
    result, reqs = _call("gmail_get_label", {"id": "INBOX"}, (200, {"id": "INBOX", "name": "INBOX", "type": "system", "messagesTotal": 120, "messagesUnread": 7, "threadsTotal": 80, "threadsUnread": 5}))
    _assert("labels.get: counts returned", reqs[0]["path"] == f"{BASE}/labels/INBOX" and result["messages_unread"] == 7 and result["threads_total"] == 80)


def _check_labels_2() -> None:
    result, reqs = _call("gmail_create_label", {"name": "Clients/Acme", "label_list_visibility": "labelShow", "text_color": "#000000", "background_color": "#16a766"}, (200, LABEL))
    _assert("labels.create: POST users.labels with color", reqs[0]["method"] == "POST" and reqs[0]["body"] == {"name": "Clients/Acme", "labelListVisibility": "labelShow", "color": {"textColor": "#000000", "backgroundColor": "#16a766"}}, str(reqs[0]["body"]))
    _assert("labels.create: id returned", result["id"] == "Label_7")


def _check_labels_3() -> None:
    _, reqs = _call("gmail_patch_label", {"id": "Label_7", "name": "Clients/Acme Corp"}, (200, LABEL))
    _assert("labels.patch: PATCH .../labels/{id} with only the name", reqs[0]["method"] == "PATCH" and reqs[0]["path"] == f"{BASE}/labels/Label_7" and reqs[0]["body"] == {"name": "Clients/Acme Corp"})


def _check_labels_4() -> None:
    _, reqs = _call("gmail_update_label", {"id": "Label_7", "name": "Acme", "message_list_visibility": "hide"}, (200, LABEL))
    _assert("labels.update: PUT .../labels/{id} full resource", reqs[0]["method"] == "PUT" and reqs[0]["body"] == {"id": "Label_7", "name": "Acme", "messageListVisibility": "hide"})


def _check_labels_5() -> None:
    result, reqs = _call("gmail_delete_label", {"id": "Label_7"}, (204, None))
    _assert("labels.delete: DELETE .../labels/{id}", reqs[0]["method"] == "DELETE" and result["deleted"] is True)


def _check_labels_6() -> None:
    for bad in ({"name": "x", "label_list_visibility": "sometimes"}, {"name": "x", "text_color": "#000000"}):
        try:
            _call("gmail_create_label", bad)
            ok = False
        except ValueError:
            ok = True
        _assert(f"labels.create refuses {sorted(bad)} before any request", ok)


def check_labels() -> None:
    _check_labels_0()
    _check_labels_1()
    _check_labels_2()
    _check_labels_3()
    _check_labels_4()
    _check_labels_5()
    _check_labels_6()


def _check_drafts_history_profile_0() -> None:
    result, reqs = _call("gmail_create_draft", {"to": "bob@example.com", "subject": "Draft", "body": "hi", "thread_id": "18c0"}, (200, DRAFT))
    _assert("drafts.create: POST users.drafts with message.raw + threadId", reqs[0]["path"] == f"{BASE}/drafts" and reqs[0]["body"]["message"]["threadId"] == "18c0" and "raw" in reqs[0]["body"]["message"])
    _assert("drafts.create: draft id returned", result["id"] == "r-55" and result["message"]["id"] == "18d1")


def _check_drafts_history_profile_1() -> None:
    _, reqs = _call("gmail_update_draft", {"id": "r-55", "to": "bob@example.com", "body": "v2"}, (200, DRAFT))
    _assert("drafts.update: PUT .../drafts/{id}", reqs[0]["method"] == "PUT" and reqs[0]["path"] == f"{BASE}/drafts/r-55" and reqs[0]["body"]["id"] == "r-55")


def _check_drafts_history_profile_2() -> None:
    result, reqs = _call("gmail_get_draft", {"id": "r-55"}, (200, {"id": "r-55", "message": FULL}))
    _assert("drafts.get: GET .../drafts/{id}?format=full parsed", reqs[0]["query"]["format"] == ["full"] and result["message"]["body_text"] == "plain body")


def _check_drafts_history_profile_3() -> None:
    result, reqs = _call("gmail_list_drafts", {"query": "to:bob", "page_token": "d2"}, (200, {"drafts": [DRAFT], "nextPageToken": "d3", "resultSizeEstimate": 1}))
    _assert("drafts.list: GET users.drafts with q + pageToken", reqs[0]["path"] == f"{BASE}/drafts" and reqs[0]["query"]["q"] == ["to:bob"] and result["next_page_token"] == "d3")


def _check_drafts_history_profile_4() -> None:
    result, reqs = _call("gmail_send_draft", {"id": "r-55"}, (200, {"id": "18e2", "threadId": "18c0", "labelIds": ["SENT"]}))
    _assert("drafts.send: POST users.drafts.send {id}", reqs[0]["path"] == f"{BASE}/drafts/send" and reqs[0]["body"] == {"id": "r-55"} and result["id"] == "18e2")


def _check_drafts_history_profile_5() -> None:
    result, reqs = _call("gmail_delete_draft", {"id": "r-55"}, (204, None))
    _assert("drafts.delete: DELETE .../drafts/{id}", reqs[0]["method"] == "DELETE" and result["deleted"] is True)


def _check_drafts_history_profile_6() -> None:
    history = {"history": [{"id": "9010", "messagesAdded": [{"message": {"id": "18f1", "threadId": "18f0", "labelIds": ["INBOX"]}}], "labelsRemoved": [{"message": {"id": "18c1", "threadId": "18c0"}, "labelIds": ["UNREAD"]}]}], "historyId": "9020", "nextPageToken": "h2"}
    result, reqs = _call("gmail_list_history", {"start_history_id": "9000", "history_types": ["messageAdded", "labelRemoved"], "label_id": "INBOX"}, (200, history))
    _assert("history.list: GET users.history with startHistoryId/historyTypes/labelId", reqs[0]["path"] == f"{BASE}/history" and reqs[0]["query"]["startHistoryId"] == ["9000"] and reqs[0]["query"]["historyTypes"] == ["messageAdded", "labelRemoved"] and reqs[0]["query"]["labelId"] == ["INBOX"])
    _assert("history.list: records shaped + paging", result["history"][0]["messages_added"][0]["id"] == "18f1" and result["history"][0]["labels_removed"][0]["label_ids"] == ["UNREAD"] and result["next_page_token"] == "h2" and result["history_id"] == "9020")


def _check_drafts_history_profile_7() -> None:
    try:
        _call("gmail_list_history", {"start_history_id": "1", "history_types": ["everything"]})
        ok = False
    except ValueError:
        ok = True
    _assert("history.list refuses an unknown history type", ok)


def _check_drafts_history_profile_8() -> None:
    result, reqs = _call("gmail_get_profile", {}, (200, {"emailAddress": "ann@gmail.com", "messagesTotal": 5000, "threadsTotal": 3000, "historyId": "9020"}))
    _assert("getProfile: GET users/me/profile", reqs[0]["path"] == f"{BASE}/profile" and result["messages_total"] == 5000 and result["history_id"] == "9020")


def check_drafts_history_profile() -> None:
    _check_drafts_history_profile_0()
    _check_drafts_history_profile_1()
    _check_drafts_history_profile_2()
    _check_drafts_history_profile_3()
    _check_drafts_history_profile_4()
    _check_drafts_history_profile_5()
    _check_drafts_history_profile_6()
    _check_drafts_history_profile_7()
    _check_drafts_history_profile_8()


def _error(code: int, status: str, message: str, reason: str, detail_reason: str = "") -> tuple[int, Any]:
    error: dict[str, Any] = {"code": code, "message": message, "errors": [{"message": message, "domain": "global", "reason": reason}], "status": status}
    if detail_reason:
        error["details"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": detail_reason, "domain": "googleapis.com", "metadata": {"service": "gmail.googleapis.com"}}]
    return code, {"error": error}


def check_errors_through_plugin() -> None:
    plugin = GSuitePlugin()
    plugin.initialize({})
    connect_accounts(plugin, ["ann@gmail.com"])
    cases = [
        ("gmail_delete_message", {"id": "18c1"}, _error(403, "PERMISSION_DENIED", "Request had insufficient authentication scopes.", "insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT"), "gsuite.missing_scope", "gmail_permanent_delete=true"),
        ("gmail_get_message", {"id": "18c1"}, _error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.", "authError"), "gsuite.auth_expired", "connect_account"),
        ("gmail_get_message", {"id": "nope"}, _error(404, "NOT_FOUND", "Requested entity was not found.", "notFound"), "gsuite.not_found", "Requested entity was not found"),
        ("gmail_list_messages", {}, _error(429, "RESOURCE_EXHAUSTED", "User-rate limit exceeded.", "rateLimitExceeded"), "gsuite.rate_limited", "User-rate limit exceeded"),
        ("gmail_modify_message", {"id": "18c1", "add_label_ids": ["Label_999"]}, _error(400, "INVALID_ARGUMENT", "Invalid label: Label_999", "invalidArgument"), "gsuite.invalid_params", "Invalid label: Label_999"),
        ("gmail_list_history", {"start_history_id": "1"}, _error(403, "PERMISSION_DENIED", "Delegation denied", "forbidden"), "gsuite.permission_denied", "forbidden"),
        ("gmail_get_message", {"id": "18c1"}, _error(400, "INVALID_ARGUMENT", "bad token access-0-abcdefgh echoed", "invalidArgument"), "gsuite.invalid_params", "bad token access-0-abcdefgh echoed"),
    ]
    result: dict[str, Any] = {}
    for verb, params, response, code, needle in cases:
        gmail, _ = _gmail(response)
        plugin._require_factory = lambda gmail=gmail: SimpleNamespace(gmail=lambda: gmail)  # type: ignore[method-assign]
        result = plugin._run(lambda verb=verb, params=params: plugin._gmail_producer(GMAIL_OPERATIONS[verb][0])(params), verb, params)
        error = result.get("error") or {}
        _assert(f"{verb} HTTP {response[0]} -> {code}", error.get("code") == code, str(error))
        _assert(f"{verb} HTTP {response[0]} message is actionable ({needle!r})", needle in str(error.get("message")), str(error.get("message")))
    gmail, _ = _gmail((200, {"emailAddress": "ann@gmail.com", "messagesTotal": 1, "threadsTotal": 1, "historyId": "1"}))
    plugin._require_factory = lambda: SimpleNamespace(gmail=lambda: gmail)  # type: ignore[method-assign]
    ok = plugin._run(lambda: plugin._gmail_producer(GMAIL_OPERATIONS["gmail_get_profile"][0])({}), "gmail_get_profile", {})
    _assert("success result names the account used", ok["data"].get("account") == "ann@gmail.com", str(ok))


def check_registration() -> None:
    plugin = GSuitePlugin()
    plugin.initialize({})
    edges = plugin.get_edge_process_definitions()
    producers = plugin._deferred_producers()
    for verb in GMAIL_OPERATIONS:
        metadata = getattr(getattr(type(plugin), verb, None), "_platform_process_metadata", None)
        parameters = getattr(metadata, "parameters", None) or {}
        kb = KB_DIR / f"{verb}.json"
        doc = json.loads(kb.read_text()) if kb.exists() else {}
        embedding = str(doc.get("embedding_description", ""))
        ok = metadata is not None and getattr(metadata, "name", "") == verb and verb in edges and verb in producers and doc.get("process_key") == f"plugin::g_suite_plugin::{verb}" and 200 <= len(embedding) <= 400
        _assert(f"{verb}: decorator + EDGE + producer + KB json", ok, f"decorated={metadata is not None} edge={verb in edges} producer={verb in producers} kb={bool(doc)} emb={len(embedding)}")
        account = parameters.get("account")
        _assert(f"{verb}: optional account argument declared", account is not None and not getattr(account, "required", True))


def main() -> int:
    print("\ng_suite_plugin Gmail REST-shape smoke (real client, recorded HTTP)")
    print("=" * 60)
    check_messages()
    check_message_writes()
    check_attachment()
    check_threads()
    check_labels()
    check_drafts_history_profile()
    check_errors_through_plugin()
    check_registration()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All Gmail REST-shape checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
