"""Single source for the wire shape of a ``peer_inbox`` page.

Three surfaces render the SAME ``PeerInbox`` dataclass for a caller:

- the localhost bridge route ``GET .../peer/inbox`` (``http_routes``) — what
  the no-MCP ``solet-bridge watch`` drain reads;
- the Streamable-HTTP MCP tool dispatch (``mcp_streamable.dispatch``);
- the ``plugin::agent_messaging_plugin::peer_inbox`` platform process
  (``plugin.peer_inbox_process``) — the pull path for sessions with no MCP
  and no long-lived watcher.

Before this module the shape was copied per surface. A copied shape is a
shape that drifts, and the drift is invisible until a consumer reads a key
that one surface stopped emitting — exactly the v10
``<<ADDRESS_ID>> not found`` class of bug, where a ``return_value_schema``
outlived the return it described. The process's declared
``return_value_schema`` is written against THIS module, so the schema and
every surface's payload move together or not at all.

Field-for-field faithful to ``ananta.llm.agent_messaging.models``: enums are
emitted as their ``.value`` (``role_section_status`` is ``"ok"`` / ``"error"``,
lowercase — the serialized token, not the Python member name), datetimes as
ISO-8601 strings, and ``None`` stays ``None`` so an exhausted cursor is
distinguishable from an empty string.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ananta.llm.agent_messaging.models import InboxReadCoverage

from ananta.llm.agent_messaging.peer_inbox_cursor import (
    PeerInboxCursorRejectedError,
    decode_peer_inbox_since_cursor,
)


class InvalidInboxCursorError(ValueError):
    """A malformed instance cursor; ``code`` is the stable failure code."""

    def __init__(self, cursor: str, message: str) -> None:
        super().__init__(message)
        self.code = f"invalid_{cursor}"


def parse_since_cursor(raw: object) -> datetime | None:
    """The forward ``since`` cursor as a datetime, ``None`` when absent or empty.

    One parser for every surface that accepts ``since``. A malformed value
    raises rather than restarting from the newest page: a broken cursor means
    the caller's paging is wrong, and silently re-reading would turn that into
    an unbounded re-read.

    ``created_at`` is stored as naive UTC, so an offset-bearing value (``Z``,
    ``+00:00``) is converted to naive UTC here: handed to a timestamp column as
    an aware datetime it would be compared in the database session's timezone.
    """
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError as exc:
        raise InvalidInboxCursorError(
            "since",
            f"'since' must be an ISO-8601 datetime horizon for the first forward page: {exc}",
        ) from exc
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(UTC).replace(tzinfo=None)


def parse_since_continuation_cursor(raw: object) -> str | None:
    """Validate the opaque tie-safe continuation, returning it unchanged."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise InvalidInboxCursorError(
            "since_cursor",
            "'since_cursor' must be the opaque string returned as next_since_cursor",
        )
    try:
        decode_peer_inbox_since_cursor(raw)
    except PeerInboxCursorRejectedError as exc:
        raise InvalidInboxCursorError("since_cursor", str(exc)) from exc
    return raw


def serialize_message(message: Any) -> dict[str, Any]:
    """Render one ``AgentMessageRow`` as JSON-safe primitives."""
    return {
        "id": message.id,
        "cursor": message.cursor,
        "role": message.role.value,
        "kind": message.kind.value,
        "content": [{"type": p.type, "text": p.text} for p in message.content],
        "action_id": message.action_id,
        "backend_session_id": message.backend_session_id,
        "error": message.error,
        "artifacts": [dataclasses.asdict(a) for a in message.artifacts],
        "metadata": message.metadata,
        "created_at": message.created_at.isoformat(),
    }


def serialize_peer_inbox_entry(entry: Any) -> dict[str, Any]:
    """Render one ``PeerInboxEntry`` — sender identity plus the message row."""
    return {
        "thread_id": entry.thread_id,
        "sender_agent_id": entry.sender_agent_id,
        "sender_agent_instance_id": entry.sender_agent_instance_id,
        "sender_session_label": entry.sender_session_label,
        "message": serialize_message(entry.message),
    }


def _serialize_read_coverage(coverage: InboxReadCoverage | None) -> dict[str, Any]:
    """Return the same JSON-native projection before any transport encodes it."""
    if coverage is None:
        return {"status": "unknown", "advisory": "Page read coverage unavailable."}
    sections = dataclasses.asdict(coverage)
    for section in sections.values():
        section["role_read_coverage"] = list(section["role_read_coverage"])
    return sections


def serialize_peer_inbox_page(
    page: Any,
    recipient_agent_instance_id: str,
) -> dict[str, Any]:
    """Render a ``PeerInbox`` page, both sections, with their two cursors.

    ``instance_exhausted`` is the instance section's progress signal. It means
    exhausted under the instance cursor the request used: the newest-first
    backward ``after`` cursor (echo ``next_after_created_at``), or the forward
    oldest-first ``since`` read (continue with ``next_since_cursor``). The
    timestamp-only ``next_since_created_at`` stays as a compatibility
    observation; the opaque cursor carries the lossless ``(created_at, id)``
    boundary. A forward page never offers a backward cursor to misuse. The
    legacy ``after`` cursor can skip
    equal-``created_at`` rows at a page boundary, so it is not
    a claim of globally lossless observation. The role section (``role_entries`` + ``next_role_cursor``) is emitted
    ADDITIVELY; the instance section keys are byte-for-byte unchanged.
    ``role_section_status`` / ``role_section_error`` carry the v10 Q1
    fault-domain outcome so a caller can tell an empty role section (no role
    messages) from a failed one — status ``"ok"`` means the section was
    COMPUTED without error, never that it is drained. Drained is
    ``next_role_cursor is None``.

    A successful role page is not a drain; continue until
    ``next_role_cursor`` is null. ``role_limit`` is the effective limit and
    the additive truncation fields disclose whether a continuation was caused
    by that row limit or the role-page byte ceiling.
    """
    return {
        "recipient_agent_id": page.recipient_agent_id,
        "read_coverage": _serialize_read_coverage(page.read_coverage),
        "recipient_agent_instance_id": recipient_agent_instance_id,
        "entries": [serialize_peer_inbox_entry(entry) for entry in page.entries],
        "next_after_created_at": (page.next_after_created_at.isoformat() if page.next_after_created_at is not None else None),
        "next_since_created_at": (page.next_since_created_at.isoformat() if page.next_since_created_at is not None else None),
        "next_since_cursor": page.next_since_cursor,
        "instance_exhausted": page.instance_exhausted,
        "role_entries": [serialize_peer_inbox_entry(entry) for entry in page.role_entries],
        "next_role_cursor": page.next_role_cursor,
        "role_section_status": page.role_section_status.value,
        "role_section_error": page.role_section_error,
        # Pull-surface boundary (design §5): False / None until a session
        # calls peer_mark_role_covered at least once for a held role — no
        # behavior change for a caller that ignores these two keys.
        # role_floor_applied=True means the default drain's floor removed at
        # least one already-covered row this call; role_history_cursor is
        # populated ONLY on a genuine floor-stop (next_role_cursor is None
        # AND role_floor_applied) — echo it back as role_after for a
        # deliberate pre-mark read.
        "role_floor_applied": page.role_floor_applied,
        "role_history_cursor": page.role_history_cursor,
        "role_limit": page.role_limit,
        "role_page_truncated": page.role_page_truncated,
        "role_truncation_reason": (page.role_truncation_reason.value if page.role_truncation_reason is not None else None),
        "role_byte_ceiling": page.role_byte_ceiling,
        "role_read_page_token": page.role_read_page_token,
        "role_read_page_status": page.role_read_page_status,
        # The horizon the role section applied (the request's ``since``), null
        # when none was asked for or the role section failed. A caller that sent
        # ``since`` and reads null here must not treat role_entries as bounded.
        "role_since_created_at": (page.role_since_created_at.isoformat() if page.role_since_created_at is not None else None),
        # Which roles the role section read and, per role, how many rows the
        # covered-mark floor and a read receipt hid inside the window this call
        # scanned. Counts only, never hidden content; empty when the holder
        # holds no role or the role section failed. A role missing from this
        # list was not read; a role listed with floor_hidden>0 and no
        # role_entries is the "notified but empty" case (iss_14bedd40).
        # ``scan_truncated`` is present, and true, only for a role whose receipt
        # refill spent its page budget: its role_entries are every unserved row
        # the scan found, older rows were not scanned, and next_role_cursor
        # continues the scan. An absent key means the scan completed.
        "role_read_coverage": [
            {
                "role": coverage.role,
                "covered_mark_present": coverage.covered_mark_present,
                "floor_hidden": coverage.floor_hidden,
                "receipt_hidden": coverage.receipt_hidden,
                **({"scan_truncated": True} if coverage.scan_truncated else {}),
            }
            for coverage in page.role_read_coverage
        ],
    }
