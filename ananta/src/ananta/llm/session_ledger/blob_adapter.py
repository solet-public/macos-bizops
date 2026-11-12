"""Adapter between large normalized events and ``blob_storage_service``.

Spec §11.1. When a ``NormalizedSessionEvent.content_text`` exceeds the
inline limit (``schema.CONTENT_INLINE_TEXT_MAX_BYTES``), the repository
asks the adapter to stage the bytes through ``blob_storage_service``.
The blob is written under namespace ``session_ledger`` so blob keys
are tenant-scoped via the configured blob provider's namespacing
convention.
"""

from __future__ import annotations

import fcntl
import logging
import re
import time
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING

from ananta.llm.session_ledger.schema import CONTENT_INLINE_TEXT_MAX_BYTES, NAMESPACE

if TYPE_CHECKING:
    from ananta.interfaces.blob_storage_service_interface import (
        BlobStorageServiceInterface,
    )

logger = logging.getLogger(__name__)

CONTENT_TEXT_MIME = "text/plain"
ATTACHMENT_TEXT_MIME_DEFAULT = "text/plain"

# blob_storage returns ``content`` either as raw bytes (bootstrap mode) or a
# hex-encoded ASCII string (the filesystem provider — one pair per byte).
_BLOB_HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class BlobAdapterError(RuntimeError):
    """Raised when blob_storage_service rejects a staged write.

    Repository callers convert this into an import-batch failure (no
    silent fallback to inline content). This keeps the secret-scan
    invariant intact: text that should have gone to blob doesn't quietly
    end up in an inline column.
    """


@contextmanager
def blob_write_guard(root: Path, *, timeout: float | None = None) -> Generator[bool]:
    """Serialize filesystem blob acquisition/reference and reclaim across processes.

    Open a fresh descriptor per acquisition so threads in one process also
    exclude each other. Never unlink this stable lock file: changing its inode
    would split ownership. Importers wait; reclaim may yield False on timeout.
    """
    lock_path = root / ".session_ledger_write_guard.lock"
    with lock_path.open("a") as handle:
        acquired = False
        try:
            if timeout is None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                acquired = True
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            break
                        time.sleep(min(0.01, remaining))
            yield acquired
        finally:
            if acquired:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _decode_blob_text(raw_content: object, blob_id: str) -> str:
    """Decode a ``retrieve_blob`` ``content`` field back to its UTF-8 string.

    Mirrors the platform's blob decode (bytes → UTF-8; hex-string →
    ``bytes.fromhex().decode``). Fail-loud on any other shape — the slice-2
    backfill hashes this text to reproduce the live external_id, so a silent
    wrong decode would diverge the dedup key.
    """
    if isinstance(raw_content, bytes):
        return raw_content.decode("utf-8")
    if isinstance(raw_content, str) and raw_content and _BLOB_HEX_RE.match(raw_content):
        return bytes.fromhex(raw_content).decode("utf-8")
    raise BlobAdapterError(
        f"blob {blob_id!r} returned undecodable content (type={type(raw_content).__name__})",
    )


class SessionLedgerBlobAdapter:
    """Stage text/binary payloads into blob_storage_service for the ledger.

    Construction takes the platform's ``blob_storage_service`` wrapper.
    All writes happen under ``namespace='session_ledger'``.
    """

    __slots__ = ("_blob_storage_service",)

    def __init__(self, blob_storage_service: BlobStorageServiceInterface) -> None:
        self._blob_storage_service = blob_storage_service

    @property
    def blob_storage_service(self) -> BlobStorageServiceInterface:
        """Return the bound blob storage service (used by startup smoke for identity check)."""
        return self._blob_storage_service

    def write_guard(self) -> AbstractContextManager[bool]:
        """Guard offloaded event acquisition through reference publication.

        Remote/bootstrap stores have no shared filesystem and cannot run the
        filesystem reclaim verb; they do not participate in this file lock.
        """
        root = self._blob_storage_service.get_blob_store_root()
        return nullcontext(True) if root is None else blob_write_guard(root)

    def should_offload_text(self, content_text: str) -> bool:
        """Return True iff inline storage would exceed the per-event byte cap.

        UTF-8 length is the relevant measure because the column is TEXT and
        Postgres treats TEXT length as byte length under UTF-8.
        """
        return len(content_text.encode("utf-8")) > CONTENT_INLINE_TEXT_MAX_BYTES

    def store_event_text(
        self,
        *,
        content_text: str,
        session_id: str,
        external_session_id: str,
        sequence: int,
    ) -> str:
        """Store an oversized event's text and return the blob_id.

        Metadata is non-secret diagnostic data (session ids + sequence).
        """
        content = content_text.encode("utf-8")
        file_hash = sha256(content).hexdigest()
        existing_blob_id = self._find_event_blob_by_hash(file_hash)
        if existing_blob_id is not None:
            return existing_blob_id
        metadata: dict[str, object] = {
            "kind": "event_content_text",
            "session_id": session_id,
            "external_session_id": external_session_id,
            "sequence": sequence,
            "mime_type": CONTENT_TEXT_MIME,
            "file_hash": file_hash,
        }
        return self._store(
            content=content,
            metadata=metadata,
            kind_for_error="event_content_text",
        )

    def _find_event_blob_by_hash(self, file_hash: str) -> str | None:
        """Return one existing content-addressed blob for ``file_hash``.

        The blob provider owns the indexed metadata lookup. A malformed or
        failed response is an adapter error, never a reason to silently store
        another copy of the payload.
        """
        result = self._blob_storage_service.search_blobs(
            namespace=NAMESPACE,
            metadata_filters={
                "file_hash": file_hash,
                "limit": 1,
            },
        )
        if result.get("action_status") != "completed":
            error = result.get("error") or "blob_storage_service returned non-completed status"
            raise BlobAdapterError(
                f"blob hash lookup failed: status={result.get('action_status')!r} error={error!r}",
            )
        data = result.get("data")
        if not isinstance(data, dict):
            raise BlobAdapterError(f"blob hash lookup returned malformed data: {data!r}")
        files = data.get("files")
        if not isinstance(files, list):
            raise BlobAdapterError(f"blob hash lookup returned malformed files: {files!r}")
        if not files:
            return None
        first = files[0]
        blob_id = first.get("blob_id") if isinstance(first, dict) else None
        if not isinstance(blob_id, str) or not blob_id:
            raise BlobAdapterError(f"blob hash lookup returned malformed file row: {first!r}")
        return blob_id

    def fetch_event_text(self, blob_id: str) -> str:
        """Retrieve a previously offloaded event's ``content_text`` by ``blob_id``.

        The inverse of :meth:`store_event_text` — used by the slice-2 external_id
        backfill to recompute the CONTENT-addressed derivation for legacy
        OFFLOADED rows (whose stored ``content_text`` is NULL). Returns the exact
        UTF-8 string that was offloaded (the blob holds the raw, un-stripped
        ``content_text``); fail-loud on a non-completed retrieve or undecodable
        content so a wrong dedup key can never be silently derived.
        """
        result = self._blob_storage_service.retrieve_blob(blob_id)
        status = result.get("action_status")
        if status != "completed":
            error = result.get("error") or "blob_storage_service returned non-completed status"
            raise BlobAdapterError(
                f"blob retrieval failed for blob_id={blob_id!r}: status={status!r} error={error!r}",
            )
        data = result.get("data") or {}
        return _decode_blob_text(data.get("content"), blob_id)

    def store_attachment_text(
        self,
        *,
        content_text: str,
        event_id: str,
        mime_type: str,
        filename: str | None,
    ) -> str:
        """Store a clean text-attachment payload and return the blob_id.

        The filename is recorded in metadata for operator inspection.
        """
        metadata: dict[str, object] = {
            "kind": "attachment_text",
            "event_id": event_id,
            "mime_type": mime_type or ATTACHMENT_TEXT_MIME_DEFAULT,
        }
        if filename is not None:
            metadata["filename"] = filename
        return self._store(
            content=content_text.encode("utf-8"),
            metadata=metadata,
            kind_for_error="attachment_text",
        )

    def store_attachment_binary(
        self,
        *,
        content: bytes,
        event_id: str,
        mime_type: str,
        filename: str | None,
    ) -> str:
        """Store a binary-attachment payload and return the blob_id.

        Binary attachments are scanned only on filename + MIME type (spec
        §10.10.2). The caller is responsible for that scan; this adapter
        just performs the upload.
        """
        metadata: dict[str, object] = {
            "kind": "attachment_binary",
            "event_id": event_id,
            "mime_type": mime_type,
        }
        if filename is not None:
            metadata["filename"] = filename
        return self._store(
            content=content,
            metadata=metadata,
            kind_for_error="attachment_binary",
        )

    def _store(
        self,
        *,
        content: bytes,
        metadata: dict[str, object],
        kind_for_error: str,
    ) -> str:
        result = self._blob_storage_service.store_blob(
            namespace=NAMESPACE,
            content=content,
            metadata=metadata,
        )
        status = result.get("action_status")
        if status != "completed":
            error = result.get("error") or "blob_storage_service returned non-completed status"
            raise BlobAdapterError(f"blob staging failed for {kind_for_error}: status={status!r} error={error!r}")
        data = result.get("data") or {}
        blob_id_obj: object = data.get("blob_id")
        if blob_id_obj is None:
            inner = data.get("result")
            if isinstance(inner, dict):
                blob_id_obj = inner.get("blob_id")
        if not isinstance(blob_id_obj, str) or not blob_id_obj:
            raise BlobAdapterError(f"blob_storage_service did not return a blob_id for {kind_for_error}: {data!r}")
        return blob_id_obj


__all__ = [
    "BlobAdapterError",
    "SessionLedgerBlobAdapter",
]
