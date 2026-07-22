"""Drive verb implementations — pure functions over a built Drive service.

Same shape as gmail_actions: take an already-built ``drive`` service client and
a ``params`` dict, return plain result dicts. Blob I/O is injected —
``download_file`` receives a ``blob_writer`` callable so the plugin owns the
blob-storage coupling while this module owns Drive + media transfer.

This module is the worked example of the blob-spill pattern the remaining
export/download verbs (sheets_export, docs_export, slides_export) replicate:
the verb returns a ``*_blob_key``; the verb must be declared in
``get_edge_process_definitions`` (decorated<->declared parity — a FATAL
``edge_process_mismatch`` otherwise; field_sensitivities declarations were
retired in the 2026-07-15 frontier-first relax).

Invalid parameters raise ``ValueError`` (-> gsuite.invalid_params); Google API
errors propagate to the plugin's ``HttpError`` classifier.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from googleapiclient.http import MediaInMemoryUpload

from .constants import (
    DRIVE_DEFAULT_PAGE_SIZE,
    DRIVE_FOLDER_MIME_TYPE,
    DRIVE_PAGE_SIZE_CAP,
    DRIVE_SHARE_ALLOWED_ROLES,
)
from .gmail_actions import AttachmentLoader

# blob_writer(content, filename, mime_type) -> blob_id (the returned file_blob_key)
BlobWriter = Callable[[bytes, str, str], str]

_LIST_FIELDS = "files(id,name,mimeType,modifiedTime,size)"
_GOOGLE_NATIVE_PREFIX = "application/vnd.google-apps"


def list_files(drive: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List Drive files matching a Drive query (newest first)."""
    query = _as_str(params.get("query"))
    kwargs: dict[str, Any] = {
        "pageSize": _clamp_page_size(params.get("max")),
        "fields": _LIST_FIELDS,
        "orderBy": "modifiedTime desc",
    }
    if query:
        kwargs["q"] = query
    response = drive.files().list(**kwargs).execute()
    rows = [_file_row(item) for item in (response.get("files") or [])]
    return {"files": rows, "count": len(rows)}


def download_file(drive: Any, params: dict[str, Any], blob_writer: BlobWriter) -> dict[str, Any]:
    """Download a binary Drive file into blob storage; return its file_blob_key.

    Google-native docs (Docs/Sheets/Slides) cannot be downloaded with get_media
    — they must be exported. Those are rejected with a pointer to the matching
    export verb rather than a raw Google 403.
    """
    file_id = _require_str(params, "id")
    meta = drive.files().get(fileId=file_id, fields="name,mimeType").execute()
    name = _as_str(meta.get("name")) or file_id
    mime = _as_str(meta.get("mimeType")) or "application/octet-stream"
    if mime.startswith(_GOOGLE_NATIVE_PREFIX):
        raise ValueError(
            f"'{name}' is a Google-native doc ({mime}); use the matching export verb "
            "(docs_export / sheets_export / slides_export), not drive_download_file."
        )
    content = drive.files().get_media(fileId=file_id).execute()
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError(f"Drive returned non-bytes content for '{name}'")
    blob_key = blob_writer(bytes(content), name, mime)
    return {"file_blob_key": blob_key, "name": name, "mime": mime}


def upload_file(
    drive: Any,
    params: dict[str, Any],
    attachment_loader: AttachmentLoader,
) -> dict[str, Any]:
    """Upload bytes to Drive from a blob (``blob_key``).

    Content comes from blob storage ONLY. The verb deliberately does NOT read an
    arbitrary local filesystem path: an agent-invokable local-file read on a verb
    that writes to a connected corporate Drive is a secret-exfiltration primitive
    (SECURITY, Codex review 2026-07-08). To upload a local file, ingest it into
    blob storage first via ``blob_storage_service.store_blob_from_file`` (which
    validates the path — "the agent supplies the path, never the bytes",
    STORAGE_CONVENTIONS / Blob Storage Operations) and pass the resulting
    blob_key here. Reuses the plugin's blob-attachment loader (the same one
    gmail_send uses) so this module does not duplicate blob-storage retrieval.
    """
    name = _require_str(params, "name")
    blob_key = _require_str(params, "blob_key")
    attachment = attachment_loader(blob_key)
    mime = _as_str(params.get("mime")) or attachment.mime_type
    metadata: dict[str, Any] = {"name": name}
    parent = _as_str(params.get("parent"))
    if parent:
        metadata["parents"] = [parent]
    media = MediaInMemoryUpload(attachment.content, mimetype=mime)
    created = drive.files().create(body=metadata, media_body=media, fields="id,webViewLink").execute()
    return {"id": _as_str(created.get("id")), "web_view_link": _as_str(created.get("webViewLink"))}


def create_folder(drive: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Create a Drive folder, optionally nested under a parent folder id."""
    name = _require_str(params, "name")
    metadata: dict[str, Any] = {"name": name, "mimeType": DRIVE_FOLDER_MIME_TYPE}
    parent = _as_str(params.get("parent"))
    if parent:
        metadata["parents"] = [parent]
    created = drive.files().create(body=metadata, fields="id").execute()
    return {"id": _as_str(created.get("id"))}


def share_file(drive: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Grant a Drive file/folder permission to an email address at a given role."""
    file_id = _require_str(params, "id")
    email = _require_str(params, "email")
    role = _require_str(params, "role")
    if role not in DRIVE_SHARE_ALLOWED_ROLES:
        raise ValueError(f"'role' must be one of {sorted(DRIVE_SHARE_ALLOWED_ROLES)}")
    permission = (
        drive.permissions()
        .create(
            fileId=file_id,
            sendNotificationEmail=False,
            body={"type": "user", "role": role, "emailAddress": email},
            fields="id",
        )
        .execute()
    )
    return {"ok": True, "permission_id": _as_str(permission.get("id"))}


def export_media_to_blob(
    drive: Any,
    file_id: str,
    mime_type: str,
    filename: str,
    blob_writer: BlobWriter,
) -> str:
    """Export a Google-native doc (Docs/Sheets/Slides) via Drive's export_media.

    Shared by sheets_actions.export_spreadsheet, docs_actions.export_document,
    and slides_actions.export_presentation — Google-native files cannot use
    get_media (see download_file above), only export_media on the Drive
    service, regardless of which product API created the file.
    """
    content = drive.files().export_media(fileId=file_id, mimeType=mime_type).execute()
    if not isinstance(content, (bytes, bytearray)):
        raise ValueError(f"Drive export returned non-bytes content for '{file_id}'")
    return blob_writer(bytes(content), filename, mime_type)


def resolve_export_mime(fmt: str, mime_by_format: dict[str, str]) -> str:
    """Map a caller-supplied ``format`` string to its export mime type."""
    mime = mime_by_format.get(fmt.lower())
    if not mime:
        raise ValueError(f"unsupported export format '{fmt}'; choose one of {sorted(mime_by_format)}")
    return mime


def _file_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": _as_str(item.get("id")),
        "name": _as_str(item.get("name")),
        "mime": _as_str(item.get("mimeType")),
        "modified": _as_str(item.get("modifiedTime")),
        "size": _as_optional_int(item.get("size")),
    }


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _clamp_page_size(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return DRIVE_DEFAULT_PAGE_SIZE
    return max(1, min(DRIVE_PAGE_SIZE_CAP, value))
