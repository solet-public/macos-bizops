"""Bounded, confirm-gated reclaim of orphaned ledger event-text blobs.

Only the filesystem provider's session_ledger/event_content_text population
is eligible. The importer publishes its event reference under the same stable
file lock. Confirmed classification and unlink hold it for one blob at a time.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ananta.interfaces.blob_storage_service_interface import BlobStorageServiceInterface
from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.llm.session_ledger.blob_adapter import blob_write_guard

logger = logging.getLogger(__name__)
RECLAIM_PAGE_SIZE = 100
RECLAIM_LOCK_TIMEOUT_SECONDS = 5.0
MINIMUM_BLOB_AGE = timedelta(hours=1)
_BLOB_NAMESPACE = "default_blob_storage_plugin"
_LEDGER_NAMESPACE = "session_ledger"


def _records(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    if result.get("action_status") != "completed":
        raise RuntimeError(f"reclaim state read failed: {result.get('error')}")
    data = result.get("data")
    rows = data.get("records") if isinstance(data, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("reclaim state read returned malformed records")
    return rows


def _created_at(value: object) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("blob creation timestamp is unreadable")
    # Platform DATETIME readback is naive UTC, not local wall clock.
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _kind(metadata: dict[str, Any]) -> str:
    plugin_metadata = metadata.get("plugin_metadata")
    if isinstance(plugin_metadata, str):
        plugin_metadata = json.loads(plugin_metadata)
    if not isinstance(plugin_metadata, dict) or not isinstance(plugin_metadata.get("kind"), str):
        raise ValueError("blob kind is ambiguous")
    return plugin_metadata["kind"]


def _is_old_event_text(metadata: dict[str, Any], cutoff: datetime) -> bool:
    return _kind(metadata) == "event_content_text" and _created_at(metadata.get("created_at")) < cutoff


class EventTextBlobReclaimer:
    """Page metadata and probe references without loading the event corpus.

    Retired metadata with leftover files follows the same selector and guard,
    but retries only unlink, counted as ``unlinked_after_tombstone``. A failed
    live delete or terminal unlink raises with ``failed_count`` and partial
    counts; it never increments successful-delete counters.
    """

    def __init__(
        self,
        *,
        state: StateManagementInterface,
        blobs: BlobStorageServiceInterface,
        blob_root: Path,
        page_size: int = RECLAIM_PAGE_SIZE,
        lock_timeout: float = RECLAIM_LOCK_TIMEOUT_SECONDS,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 1 <= page_size <= RECLAIM_PAGE_SIZE or lock_timeout < 0:
            raise ValueError("invalid reclaim page size or lock timeout")
        if blobs.get_blob_store_root() != blob_root:
            raise ValueError("reclaim root must be the provider's shared filesystem root")
        self._state = state
        self._blobs = blobs
        self._root = blob_root
        self._page_size = page_size
        self._lock_timeout = lock_timeout
        self._clock = clock

    def run(self, *, confirm: bool = False) -> dict[str, Any]:
        """Preview by default. Confirmed lock contention is counted and skipped."""
        if type(confirm) is not bool:
            raise TypeError("confirm must be a boolean")
        cutoff = self._clock() - MINIMUM_BLOB_AGE
        counts: dict[str, Any] = {
            "confirmed": confirm,
            "scanned_count": 0,
            "candidate_count": 0,
            "candidate_bytes": 0,
            "deleted_count": 0,
            "deleted_bytes": 0,
            "unlinked_after_tombstone": 0,
            "failed_count": 0,
            "ambiguous_count": 0,
            "skipped_locked": 0,
            "excluded_count": 0,
        }
        for row in self._rows():
            counts["scanned_count"] += 1
            if not confirm:
                self._consider(row, cutoff, counts, confirm=False)
                continue
            with blob_write_guard(self._root, timeout=self._lock_timeout) as acquired:
                if not acquired:
                    counts["skipped_locked"] += 1
                    continue
                self._consider(row, cutoff, counts, confirm=True)
        return counts

    def _rows(self) -> Iterator[dict[str, Any]]:
        after: list[object] | None = None
        while True:
            query: dict[str, object] = {
                "table": "metadata",
                "filters": {"plugin_namespace": _LEDGER_NAMESPACE},
                "order_by": [["created_at", "asc"], ["id", "asc"]],
                "limit": self._page_size,
                "include_deleted": True,
            }
            if after is not None:
                query["after"] = after
            rows = _records(self._state.query_ordered(_BLOB_NAMESPACE, query))
            if not rows:
                return
            # Capture before deletion: keyset traversal is stable under unlink.
            last = rows[-1]
            cursor = [last["created_at"], last["id"]]
            if cursor == after:
                raise RuntimeError("reclaim metadata cursor did not advance")
            yield from rows
            after = cursor

    def _consider(self, row: dict[str, Any], cutoff: datetime, counts: dict[str, Any], *, confirm: bool) -> None:
        try:
            candidate = self._candidate_size(row, cutoff)
        except Exception as exc:  # noqa: BLE001 — unreadable classification is counted and never deleted
            counts["ambiguous_count"] += 1
            logger.warning("reclaim left unreadable/ambiguous blob %s: %s", row.get("blob_id"), exc)
            return
        if candidate is None:
            counts["excluded_count"] += 1
            return
        size, tombstoned = candidate
        counts["candidate_count"] += 1
        counts["candidate_bytes"] += size
        if confirm:
            try:
                if tombstoned:
                    self._tombstone_path(str(row["blob_id"])).unlink()
                    counts["unlinked_after_tombstone"] += 1
                else:
                    result = self._blobs.delete_blob(namespace=_LEDGER_NAMESPACE, blob_id=str(row["blob_id"]))
                    data = result.get("data")
                    if result.get("action_status") != "completed" or not isinstance(data, dict) or data.get("deleted") is not True:
                        raise RuntimeError(f"provider delete failed: {result.get('error')}")
                    counts["deleted_count"] += 1
                    counts["deleted_bytes"] += size
            except Exception as exc:
                counts["failed_count"] += 1
                raise RuntimeError(f"reclaim delete failed; no automatic retry; partial counts={counts}; error={exc}") from exc

    def _tombstone_path(self, blob_id: str) -> Path:
        path = self._root / blob_id
        if blob_id in {"", ".", ".."} or Path(blob_id).name != blob_id or path.is_symlink():
            raise ValueError("tombstone file identity is unsafe")
        return path

    def _terminal_file_absent(self, row: dict[str, Any], blob_id: str) -> bool:
        return row.get("is_deleted") == 1 and not self._tombstone_path(blob_id).is_file()

    def _candidate_size(self, row: dict[str, Any], cutoff: datetime) -> tuple[int, bool] | None:
        blob_id = row.get("blob_id")
        if not isinstance(blob_id, str) or not blob_id:
            raise ValueError("blob id missing")
        if self._terminal_file_absent(row, blob_id):
            return None
        metadata = self._metadata(blob_id)
        if metadata.get("id") != row.get("id"):
            raise ValueError("blob metadata identity changed")
        if metadata.get("plugin_namespace") != _LEDGER_NAMESPACE:
            return None
        if not _is_old_event_text(metadata, cutoff):
            return None
        size = metadata.get("size")
        if type(size) is not int or size < 0:
            raise ValueError("blob byte count is ambiguous")
        references = _records(
            self._state.query_ordered(
                _LEDGER_NAMESPACE,
                {
                    "table": "event",
                    "filters": {"content_blob_id": blob_id},
                    "order_by": [["created_at", "asc"], ["id", "asc"]],
                    "limit": 1,
                    "include_deleted": True,
                },
            )
        )
        return None if references else (size, metadata.get("is_deleted") == 1)

    def _metadata(self, blob_id: str) -> dict[str, Any]:
        identities = _records(
            self._state.query_ordered(
                _BLOB_NAMESPACE,
                {
                    "table": "metadata",
                    "filters": {"blob_id": blob_id, "plugin_namespace": _LEDGER_NAMESPACE},
                    "order_by": [["created_at", "asc"], ["id", "asc"]],
                    "limit": 2,
                    "include_deleted": True,
                },
            )
        )
        if len(identities) != 1:
            raise ValueError("blob metadata identity is missing or duplicated")
        result = self._blobs.get_blob_metadata(namespace=_LEDGER_NAMESPACE, blob_id=blob_id)
        if result.get("action_status") != "completed":
            raise RuntimeError("blob metadata unreadable")
        data = result.get("data")
        metadata = data.get("metadata") if isinstance(data, dict) else None
        if not isinstance(metadata, dict) or metadata.get("blob_id") != blob_id:
            raise ValueError("blob identity is ambiguous")
        return metadata
