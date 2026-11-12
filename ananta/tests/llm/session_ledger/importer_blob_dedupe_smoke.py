#!/usr/bin/env python3
"""Regression smoke for importer dedupe-before-blob and content reuse.

The smoke uses the repository's typed state stub for deterministic ordering
assertions. The private PostgreSQL gate runs the named importer/session-ledger
smokes against the throwaway provisioned fixture; this focused smoke keeps the
three blob-count assertions fast and independent of fixture setup.
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import StubStateService  # noqa: E402
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.importer import (  # noqa: E402
    SessionLedgerImporter,
)
from ananta.llm.session_ledger.importer_persistence import (  # noqa: E402
    _event_external_id,
    _OrdinalCounter,
)
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_EVENT  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    SourceVendor,
)

_NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
_OVERSIZED = "same event text " * 20_000


@dataclass(slots=True)
class _Blob:
    blob_id: str
    content: bytes
    metadata: dict[str, object]


@dataclass(slots=True)
class _BlobService:
    blobs: list[_Blob] = field(default_factory=list)
    root: TemporaryDirectory[str] = field(default_factory=TemporaryDirectory)

    def get_blob_store_root(self) -> Path:
        return Path(self.root.name)

    def store_blob(
        self,
        namespace: str,
        content: bytes,
        metadata: dict[str, object],
    ) -> dict[str, Any]:
        blob_id = f"bmd_{len(self.blobs) + 1:06d}"
        self.blobs.append(_Blob(blob_id, bytes(content), dict(metadata)))
        return {"action_status": "completed", "data": {"blob_id": blob_id}}

    def search_blobs(
        self,
        namespace: str,
        metadata_filters: dict[str, object],
    ) -> dict[str, Any]:
        file_hash = metadata_filters.get("file_hash")
        files = [{"blob_id": blob.blob_id, "metadata": blob.metadata} for blob in self.blobs if blob.metadata.get("file_hash") == file_hash]
        return {
            "action_status": "completed",
            "data": {"files": files, "total_count": len(files)},
        }


def _event(text: str, external_session_id: str) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id=external_session_id,
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=text,
        content_json=None,
        event_at=_NOW,
        vendor_event_id=None,
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _importer(
    state: StubStateService,
    blobs: _BlobService,
) -> SessionLedgerImporter:
    repo = SessionLedgerRepository(state, clock=lambda: _NOW)  # type: ignore[arg-type]
    return SessionLedgerImporter(
        registry=SessionSourceRegistry({}),
        repository=repo,
        blob_adapter=SessionLedgerBlobAdapter(blobs),  # type: ignore[arg-type]
    )


def _persist(
    importer: SessionLedgerImporter,
    *,
    session_id: str,
    external_session_id: str,
    ordinals: _OrdinalCounter,
) -> int:
    return importer._persist_normalized_for_session(
        session_id=session_id,
        normalized=_event(_OVERSIZED, external_session_id),
        batch_id="batch_smoke",
        session_vendor=SourceVendor.CODEX,
        source_kind=IngestSourceKind.CODEX_PUSHED,
        ordinals=ordinals,
    )


def test_same_session_second_pass_writes_no_blob() -> None:
    state = StubStateService()
    blobs = _BlobService()
    importer = _importer(state, blobs)
    external_id = _event_external_id(
        normalized=_event(_OVERSIZED, "session-one"),
        session_id="session-one",
        ordinals={},
    )
    query_count = 0

    def existing_after_first(filters: dict[str, object]) -> bool:
        nonlocal query_count
        if filters.get("external_id") != external_id:
            return False
        query_count += 1
        return query_count > 1

    state.add_query_response(TABLE_EVENT, [{"id": "evt_existing"}], when=existing_after_first)
    assert _persist(importer, session_id="session-one", external_session_id="session-one", ordinals={}) == 1
    assert _persist(importer, session_id="session-one", external_session_id="session-one", ordinals={}) == 0
    assert len(blobs.blobs) == 1, "the second import pass must not stage a duplicate blob"


def test_identical_text_across_sessions_reuses_blob() -> None:
    state = StubStateService()
    blobs = _BlobService()
    importer = _importer(state, blobs)
    assert _persist(importer, session_id="session-one", external_session_id="session-one", ordinals={}) == 1
    assert _persist(importer, session_id="session-two", external_session_id="session-two", ordinals={}) == 1
    expected_hash = hashlib.sha256(_OVERSIZED.encode("utf-8")).hexdigest()
    assert len(blobs.blobs) == 1, "identical event text must have one stored copy"
    assert blobs.blobs[0].metadata.get("file_hash") == expected_hash


def test_existing_event_dedupe_leaves_no_blob() -> None:
    state = StubStateService()
    blobs = _BlobService()
    importer = _importer(state, blobs)
    external_id = _event_external_id(
        normalized=_event(_OVERSIZED, "session-one"),
        session_id="session-one",
        ordinals={},
    )
    state.add_query_response(
        TABLE_EVENT,
        [{"id": "evt_existing"}],
        when=lambda filters: filters.get("external_id") == external_id,
    )
    assert _persist(importer, session_id="session-one", external_session_id="session-one", ordinals={}) == 0
    assert not blobs.blobs, "an event deduped before append must not leave a blob"


def main() -> int:
    for test in (
        test_same_session_second_pass_writes_no_blob,
        test_identical_text_across_sessions_reuses_blob,
        test_existing_event_dedupe_leaves_no_blob,
    ):
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
