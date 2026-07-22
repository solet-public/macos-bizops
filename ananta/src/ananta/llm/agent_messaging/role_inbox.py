"""Role-inbox section assembly: global k-way merge + threadless projection.

The role section of ``peer_inbox`` is a single globally-ordered, globally-
limited stream across every role a holder currently holds — NOT a per-role
concatenation (which would dup/skip at page boundaries). All role rows live in
the one ``core__agent_role_message`` table, so ``id`` is table-global and
``(created_at, id)`` totally orders rows across roles; the next page re-runs
each per-role query with ``after = next_role_cursor`` and merges again, with no
cross-role dup or skip (v9-B3).

Each role row is threadless (the role send creates NO ``agent_thread`` row), so
it projects to a :class:`PeerInboxEntry` straight from the envelope columns:
``thread_id`` is the synthetic ``"role:{recipient_key}"`` handle (display
only), the projected ``AgentMessageRow.id`` is the envelope ``message_id``, and
``cursor`` is the ``0`` sentinel (role rows never live in ``core__agent_message``
and never page by ``message.cursor``, so the sentinel cannot collide).
"""

from __future__ import annotations

from datetime import datetime

from ananta.services.state_service.ordered_query import normalize_sort_value

from .models import (
    AgentMessageRow,
    MessageContent,
    MessageKind,
    MessageRole,
    PeerInboxEntry,
    TextPart,
)
from .role_cursor import RoleCursorScope, encode_role_cursor

_COL_CREATED_AT = "created_at"
_COL_ID = "id"


def build_role_section(
    per_role_records: list[list[dict[str, object]]],
    *,
    scope: RoleCursorScope,
    limit: int,
) -> tuple[tuple[PeerInboxEntry, ...], str | None]:
    """Merge per-role pages into the global top-``limit`` + the next cursor.

    ``per_role_records`` is one ``query_ordered`` result list per held role
    (each already ``(created_at, id)``-descending and capped at ``limit``).
    Returns the projected entries (newest-first) and ``next_role_cursor`` —
    the opaque, scope-bound token of the last emitted row when a full page was
    returned (there may be more), else ``None``.
    """
    merged = _merge_top_n(per_role_records, limit)
    entries = tuple(project_role_entry(record) for record in merged)

    next_cursor: str | None = None
    if merged and len(merged) == limit:
        last = merged[-1]
        next_cursor = encode_role_cursor(
            scope,
            created_at_iso=normalize_sort_value(last.get(_COL_CREATED_AT)),
            row_id=str(last.get(_COL_ID, "")),
        )
    return entries, next_cursor


def _merge_top_n(
    per_role_records: list[list[dict[str, object]]], limit: int,
) -> list[dict[str, object]]:
    """Flatten the per-role pages and take the global ``(created_at, id)`` top-N.

    A role row matches exactly one role's query (``recipient_key`` is single-
    valued), so flattening cannot duplicate a row across the per-role lists.
    """
    flat = [record for records in per_role_records for record in records]
    flat.sort(key=_merge_sort_key, reverse=True)
    return flat[:limit]


def _merge_sort_key(record: dict[str, object]) -> tuple[str, str]:
    return (
        normalize_sort_value(record.get(_COL_CREATED_AT)),
        normalize_sort_value(record.get(_COL_ID)),
    )


def merge_undelivered_oldest_first(
    per_role_records: list[list[dict[str, object]]], limit: int,
) -> list[dict[str, object]]:
    """Global OLDEST-first ``(created_at, id)`` merge of per-role undelivered
    pages, capped at ``limit`` — the repair-drain page (Control #5).

    The drain emits this page, flips each ``delivered=true``, and re-queries
    (the flipped rows drop out of the ``delivered=false`` filter) until empty,
    so it needs no cursor — just the oldest ``limit`` owed rows each pass. FIFO
    oldest-first means an old row's wait is bounded by its position and never
    starves under newer arrivals.
    """
    flat = [record for records in per_role_records for record in records]
    flat.sort(key=_merge_sort_key)  # ascending == oldest-first (no reverse)
    return flat[:limit]


def project_role_entry(record: dict[str, object]) -> PeerInboxEntry:
    """Project one ``agent_role_message`` envelope row to a ``PeerInboxEntry``."""
    sender_agent_id = str(record.get("sender_agent_id", ""))
    sender_agent_instance_id = str(record.get("sender_agent_instance_id", ""))
    sender_session_label = str(record.get("sender_session_label") or "")
    thread_id = str(record.get("thread_id", ""))
    message = AgentMessageRow(
        id=str(record.get("message_id", "")),
        thread_id=thread_id,
        cursor=0,
        role=MessageRole.ORIGINATOR,
        kind=MessageKind.MESSAGE,
        content=_deserialize_content(record.get("content")),
        created_at=_coerce_datetime(record.get(_COL_CREATED_AT)),
        metadata={
            "peer": True,
            "sender_agent_id": sender_agent_id,
            "sender_agent_instance_id": sender_agent_instance_id,
            "sender_session_label": sender_session_label,
            "important": bool(record.get("important", False)),
        },
    )
    return PeerInboxEntry(
        thread_id=thread_id,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        message=message,
    )


def _deserialize_content(raw: object) -> MessageContent:
    """Rebuild the typed message parts from the JSON-stored envelope content."""
    if not isinstance(raw, list):
        return []
    parts: list[TextPart] = []
    for item in raw:
        if isinstance(item, dict):
            parts.append(
                TextPart(
                    type=str(item.get("type", "text")),
                    text=str(item.get("text", "")),
                ),
            )
    return parts


def _coerce_datetime(value: object) -> datetime:
    """Coerce a stored ``created_at`` (postgres ISO string or bootstrap datetime).

    The standardizer always populates ``created_at``, so an unparseable value
    is a genuine data anomaly — fail loud rather than silently fabricate a
    timestamp (Codex acceptance check #4: handle missing/invalid fields
    explicitly).
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(
                f"agent_role_message.created_at is not ISO-8601: {value!r}",
            ) from exc
    raise ValueError(
        f"agent_role_message.created_at has unexpected type "
        f"{type(value).__name__}: {value!r}",
    )


__all__ = [
    "build_role_section",
    "merge_undelivered_oldest_first",
    "project_role_entry",
]
