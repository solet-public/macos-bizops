"""Persistence helpers for normalized session-ledger events."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from datetime import datetime

from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter
from ananta.llm.session_ledger.repository import SessionLedgerRepository
from ananta.llm.session_ledger.schema import TABLE_EVENT
from ananta.llm.session_ledger.shared import (
    _strip_nuls,
    derive_event_external_id,
)
from ananta.llm.session_ledger.types import (
    EventType,
    IngestSourceKind,
    NormalizedSessionEvent,
    SourceVendor,
)

logger = logging.getLogger(__name__)

_OrdinalCounter = dict[tuple[str, str, str | None, str, datetime], int]


def _event_external_id(
    *,
    normalized: NormalizedSessionEvent,
    session_id: str,
    ordinals: _OrdinalCounter,
) -> str:
    """Derive the importer external id and advance the null-vendor ordinal."""
    if normalized.vendor_event_id is not None:
        return normalized.vendor_event_id
    role = normalized.role.value if normalized.role is not None else None
    content_key = _strip_nuls(normalized.content_text) or ""
    key = (session_id, normalized.event_type.value, role, content_key, normalized.event_at)
    ordinal = ordinals.get(key, 0)
    ordinals[key] = ordinal + 1
    return derive_event_external_id(
        vendor_event_id=None,
        session_id=session_id,
        event_type=normalized.event_type.value,
        role=role,
        content_key=content_key,
        event_at=normalized.event_at,
        ordinal=ordinal,
    )


def _find_existing_event_id_by_external_id(
    repository: SessionLedgerRepository,
    *,
    session_id: str,
    external_id: str,
) -> str | None:
    rows = repository._query_ordered(
        TABLE_EVENT,
        filters={"session_id": session_id, "external_id": external_id},
        order_by=[["sequence", "desc"], ["id", "desc"]],
        limit=1,
    )
    if not rows:
        return None
    event_id = rows[0].get("id")
    return str(event_id) if event_id is not None else None


def persist_normalized_for_session(
    repository: SessionLedgerRepository,
    blob_adapter: SessionLedgerBlobAdapter,
    *,
    session_id: str,
    normalized: NormalizedSessionEvent,
    batch_id: str,
    session_vendor: SourceVendor,
    source_kind: IngestSourceKind,
    ordinals: _OrdinalCounter,
) -> int:
    """Deduplicate, stage oversized content, and append one normalized event."""
    # vendor_event_id fast-path dedup (bug 2026-05-31): a cursor that failed
    # to persist re-yields events on the next poll. KEPT alongside the GAP-5
    # (session_id, external_id) upsert: it still dedups vendor-present
    # re-ingests of LEGACY rows (null external_id until the slice-2 backfill)
    # the upsert can't yet catch; for new rows the upsert is authoritative.
    if normalized.vendor_event_id is not None:
        existing_id = repository.find_event_id_by_vendor_id(
            session_id=session_id,
            vendor_event_id=normalized.vendor_event_id,
        )
        if existing_id is not None:
            logger.debug(
                "ledger persist: skipping duplicate session_id=%s vendor_event_id=%s (existing event_id=%s)",
                session_id,
                normalized.vendor_event_id,
                existing_id,
            )
            return 0
    external_id = _event_external_id(
        normalized=normalized,
        session_id=session_id,
        ordinals=ordinals,
    )
    if normalized.vendor_event_id is None:
        existing_id = _find_existing_event_id_by_external_id(
            repository,
            session_id=session_id,
            external_id=external_id,
        )
    else:
        existing_id = None
    if existing_id is not None:
        logger.debug(
            "ledger persist: skipping duplicate session_id=%s external_id=%s (existing event_id=%s)",
            session_id,
            external_id,
            existing_id,
        )
        return 0
    offload = normalized.content_text is not None and blob_adapter.should_offload_text(normalized.content_text)
    with blob_adapter.write_guard() if offload else nullcontext():
        content_blob_id: str | None = None
        if offload and normalized.content_text is not None:
            content_blob_id = blob_adapter.store_event_text(
                content_text=normalized.content_text,
                session_id=session_id,
                external_session_id=normalized.external_session_id,
                sequence=-1,
            )
        result = repository.append_event(
            session_id=session_id,
            normalized=normalized,
            batch_id=batch_id,
            content_blob_id=content_blob_id,
            session_vendor=session_vendor,
            source_kind=source_kind,
            external_id=external_id,
        )
    if result.deduped:
        # Upsert hit an existing row — event + children already persisted on
        # the first ingest; skip the child projection (no double-write).
        return 0
    _project_tool_call(
        repository,
        normalized=normalized,
        session_id=session_id,
        event_id=result.event_id,
    )
    return 1


def _project_tool_call(
    repository: SessionLedgerRepository,
    *,
    normalized: NormalizedSessionEvent,
    session_id: str,
    event_id: str,
) -> None:
    """Project TOOL_CALL / TOOL_RESULT into ``session_ledger__tool_call``."""
    if normalized.event_type is EventType.TOOL_CALL:
        tool_name = _extract_tool_name(normalized)
        if tool_name is None:
            return
        repository.record_tool_call(
            session_id=session_id,
            call_event_id=event_id,
            tool_name=tool_name,
            called_at=normalized.event_at,
        )
        return
    if normalized.event_type is EventType.TOOL_RESULT:
        tool_use_id = normalized.vendor_parent_event_id
        if tool_use_id is None:
            return
        call_event_id = repository.find_call_event_id_for_resolution(
            session_id=session_id,
            tool_use_vendor_id=tool_use_id,
        )
        if call_event_id is None:
            logger.warning(
                "tool_result has no matching tool_call in session %s (tool_use_id=%s); leaving projection pending",
                session_id,
                tool_use_id,
            )
            return
        repository.resolve_tool_call(
            call_event_id=call_event_id,
            result_event_id=event_id,
            status="succeeded",
            resolved_at=normalized.event_at,
        )


def _extract_tool_name(normalized: NormalizedSessionEvent) -> str | None:
    content = normalized.content_json
    if not isinstance(content, dict):
        return None
    name = content.get("tool_name")
    return name if isinstance(name, str) else None
