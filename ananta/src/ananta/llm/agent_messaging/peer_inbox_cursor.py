"""Opaque, lossless continuation cursor for the direct peer inbox.

The forward direct-inbox read starts from an ISO-8601 ``since`` horizon and
orders rows by the total key ``(created_at, id)``. A timestamp alone cannot
continue that ordering: if a page ends inside a group of rows sharing one
``created_at``, a subsequent strict-timestamp read skips the rest. The cursor
therefore carries both the original horizon and the last emitted total key.

The wire format is base64 over the fixed-field string
``v1|{horizon_iso}|{created_at_iso}|{id}``. It is opaque to callers, versioned,
and fail-closed. Identity is not encoded because peer-inbox authorization is
still derived server-side from the caller's live binding; a cursor can narrow
that authorized view but can never widen it.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime

_CURSOR_VERSION = "v1"
_FIELD_SEP = "|"
_FIELD_COUNT = 4


class PeerInboxCursorRejectedError(ValueError):
    """The direct-inbox continuation token is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class PeerInboxSinceCursor:
    """Decoded fixed horizon plus the last emitted total-order key."""

    horizon: datetime
    after_created_at: datetime
    after_id: str


def encode_peer_inbox_since_cursor(
    *, horizon: datetime, after_created_at: datetime, after_id: str,
) -> str:
    """Encode the fixed horizon and last-emitted ``(created_at, id)`` pair."""
    if not after_id:
        raise ValueError("peer inbox since cursor requires a non-empty message id")
    raw = _FIELD_SEP.join(
        (
            _CURSOR_VERSION,
            _canonical_iso(horizon),
            _canonical_iso(after_created_at),
            after_id,
        ),
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def decode_peer_inbox_since_cursor(token: str) -> PeerInboxSinceCursor:
    """Decode a continuation token, rejecting malformed or incoherent input."""
    if not token:
        raise PeerInboxCursorRejectedError(
            "peer inbox since cursor must be a non-empty string",
    )
    try:
        encoded = token.encode("ascii")
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
        raw = decoded.decode("utf-8")
    except (binascii.Error, UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise PeerInboxCursorRejectedError(
            f"peer inbox since cursor is not valid base64: {token!r}",
        ) from exc
    if base64.urlsafe_b64encode(decoded) != encoded:
        raise PeerInboxCursorRejectedError(
            f"peer inbox since cursor is not canonical base64: {token!r}",
        )
    parts = raw.split(_FIELD_SEP)
    if len(parts) != _FIELD_COUNT:
        raise PeerInboxCursorRejectedError(
            f"peer inbox since cursor must have {_FIELD_COUNT} fields: {token!r}",
        )
    version, horizon_raw, after_raw, after_id = parts
    if version != _CURSOR_VERSION:
        raise PeerInboxCursorRejectedError(
            f"unsupported peer inbox since cursor: {token!r}",
        )
    if not after_id:
        raise PeerInboxCursorRejectedError(
            "peer inbox since cursor is missing the message id",
        )
    horizon = _parse_iso(horizon_raw, field="horizon")
    after_created_at = _parse_iso(after_raw, field="created_at")
    if after_created_at <= horizon:
        raise PeerInboxCursorRejectedError(
            "peer inbox since cursor continuation must be newer than its horizon",
        )
    return PeerInboxSinceCursor(
        horizon=horizon,
        after_created_at=after_created_at,
        after_id=after_id,
    )


def _parse_iso(raw: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise PeerInboxCursorRejectedError(
            f"peer inbox since cursor {field} is not ISO-8601: {raw!r}",
        ) from exc
    return _to_naive_utc(parsed)


def _canonical_iso(value: datetime) -> str:
    return _to_naive_utc(value).isoformat()


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


__all__ = [
    "PeerInboxCursorRejectedError",
    "PeerInboxSinceCursor",
    "decode_peer_inbox_since_cursor",
    "encode_peer_inbox_since_cursor",
]
