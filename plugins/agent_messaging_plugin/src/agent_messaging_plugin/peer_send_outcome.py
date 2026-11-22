"""One read, by message id, of what happened to a peer send (prb_8989586c).

A send returns before the recipient's pane is touched, so its receipt can only say
``driver_scheduled``, and senders took that for delivered. This module builds no
record of its own. It joins the two the platform already keeps:

* the finished wake attempt, held in this process by message id
  (:func:`delivery_drive.wake_attempt_record`), which says whether the drive typed
  anything into the recipient's pane; and
* the durable pickup row (:class:`delivery_drive.CodexPickupStore`), which survives
  a restart and records when the recipient's own inbox read returned the message.

The read is read-only and never waits for a drive. A record that is missing is a
named state (``not_confirmed``), not an error: the message itself is durable
whatever this read finds, so no outcome here is a reason to resend it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from .delivery_drive import CodexPickupStore, wake_attempt_record
from .session_lifecycle_control import (
    CLAUDE_PICKUP_PENDING,
    DRIVE_DRIVER_NOT_NEEDED,
    DRIVE_DRIVER_PENDING,
    DRIVE_DRIVER_SCHEDULED,
    DRIVE_DRIVER_SENT,
    DRIVE_DRIVER_TURN_OBSERVED,
    DRIVE_INELIGIBLE_STATE,
    DRIVE_NOT_MANAGED,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

STATE_READ_BY_RECIPIENT: Final[str] = "read_by_recipient"
STATE_WAKE_TYPED: Final[str] = "wake_typed"
STATE_WAKE_ATTEMPT_RUNNING: Final[str] = "wake_attempt_running"
STATE_NOT_DELIVERED: Final[str] = "not_delivered"
STATE_NO_DRIVER_WAKE: Final[str] = "no_driver_wake"
STATE_NOT_CONFIRMED: Final[str] = "not_confirmed"
STATE_CHANNEL_QUEUED: Final[str] = "channel_queued"

SOURCE_WAKE_ATTEMPT_RECORD: Final[str] = "wake_attempt_record"
SOURCE_PICKUP_ROW: Final[str] = "pickup_row"
SOURCE_NONE: Final[str] = "none"

# The pickup row's phases, as ``delivery_drive.CodexPickupStore`` writes them:
# ``remember`` (a new row) and the recipient's inbox read (``inbox_returned``).
PICKUP_PHASE_QUEUED: Final[str] = "queued"
PICKUP_PHASE_INBOX_RETURNED: Final[str] = "inbox_returned"

_WAKE_TYPED_OUTCOMES: Final[frozenset[str]] = frozenset({DRIVE_DRIVER_SENT, DRIVE_DRIVER_TURN_OBSERVED})
_RUNNING_OUTCOMES: Final[frozenset[str]] = frozenset({DRIVE_DRIVER_SCHEDULED, PICKUP_PHASE_QUEUED})
_NO_DRIVER_OUTCOMES: Final[frozenset[str]] = frozenset({DRIVE_NOT_MANAGED, DRIVE_INELIGIBLE_STATE})
_CHANNEL_OUTCOMES: Final[frozenset[str]] = frozenset({DRIVE_DRIVER_NOT_NEEDED})

# One fixed text per state. ``{detail}`` is the wake attempt's detail, or its outcome when it has none.
_EXPLANATIONS: Final[dict[str, str]] = {
    STATE_READ_BY_RECIPIENT: "The recipient's inbox read returned this message. That proves the read, not that the work was done.",
    STATE_WAKE_TYPED: "A wake notice was typed into the recipient's pane. The recipient has not yet read the message from its inbox.",
    STATE_WAKE_ATTEMPT_RUNNING: "The wake attempt has not finished. Read again in a few seconds.",
    STATE_NOT_DELIVERED: ("NOT DELIVERED: nothing reached the recipient's pane ({detail}). The message is stored and the recipient reads it at its next inbox read. Do not resend it."),
    STATE_NO_DRIVER_WAKE: ("The driver does not wake this recipient ({detail}). Whether it has seen the message depends on the delivery value of the send receipt. Do not resend it."),
    STATE_CHANNEL_QUEUED: "The message was queued on the recipient's live channel, which carries it to the session; no wake notice was typed. Delivery to the model is not observed here. Do not resend it.",
    STATE_NOT_CONFIRMED: ("No wake-attempt record for this message exists in the running process (it restarted, more than 512 sends followed, or the id is wrong). Delivery is not confirmed."),
}


def read_peer_send_outcome(state: StateManagementInterface | None, message_id: str) -> dict[str, object]:
    """What happened to the send ``message_id``: eight keys, never a refusal.

    ``delivery_state`` is the answer; ``wake_attempt_outcome``, ``wake_attempt_detail``
    and ``evidence_source`` say which record it came from; ``pickup`` carries the
    durable row for context; ``pickup_unavailable`` is true only when that row could
    not be read, so an absent ``pickup`` is not taken for an absent row.
    """
    recorded, finished = wake_attempt_record(message_id)
    pickup, pickup_unavailable = _read_pickup(state, message_id)
    outcome, detail, evidence_source = _pick_outcome(recorded, finished, pickup)
    delivery_state = _delivery_state(pickup, outcome)
    if delivery_state == STATE_READ_BY_RECIPIENT:
        evidence_source = SOURCE_PICKUP_ROW
    return {
        "message_id": message_id,
        "delivery_state": delivery_state,
        "wake_attempt_outcome": outcome,
        "wake_attempt_detail": detail,
        "evidence_source": evidence_source,
        "pickup": pickup,
        "pickup_unavailable": pickup_unavailable,
        "explanation": _EXPLANATIONS[delivery_state].format(detail=detail if detail is not None else outcome),
    }


def _read_pickup(state: StateManagementInterface | None, message_id: str) -> tuple[dict[str, object] | None, bool]:
    """The pickup row's six facts, or ``(None, True)`` when reading it raised."""
    if state is None:
        return None, False
    try:
        row = CodexPickupStore(state).read(message_id)
        if row is None:
            return None, False
        return (
            {
                "phase": row["phase"],
                "reason": row["reason"],
                "pending": row["pending"],
                "attempts": row["attempts"],
                "version": row["version"],
                "checked_at": str(row["checked_at"]),
            },
            False,
        )
    except Exception:  # noqa: BLE001 -- a pickup row that cannot be read is a named state of this read, never its failure
        logger.exception("peer_send_outcome: the pickup row for message %s could not be read", message_id)
        return None, True


def _pick_outcome(
    recorded: bool,
    finished: tuple[str, str | None] | None,
    pickup: dict[str, object] | None,
) -> tuple[str | None, str | None, str]:
    """``(outcome, detail, evidence_source)``: the first record that has something to say."""
    if finished is not None:
        return finished[0], finished[1], SOURCE_WAKE_ATTEMPT_RECORD
    if recorded:
        return DRIVE_DRIVER_SCHEDULED, None, SOURCE_WAKE_ATTEMPT_RECORD
    if pickup is not None and not _is_claude_placeholder(pickup):
        return str(pickup["phase"]), str(pickup["reason"]), SOURCE_PICKUP_ROW
    return None, None, SOURCE_NONE


def _is_claude_placeholder(pickup: dict[str, object]) -> bool:
    """A Claude recipient's pickup row never records the finished attempt; it keeps this placeholder."""
    return pickup["phase"] == DRIVE_DRIVER_PENDING and pickup["reason"] == CLAUDE_PICKUP_PENDING


def _delivery_state(pickup: dict[str, object] | None, outcome: str | None) -> str:
    if pickup is not None and not pickup["pending"] and pickup["phase"] == PICKUP_PHASE_INBOX_RETURNED:
        return STATE_READ_BY_RECIPIENT
    if outcome is None:
        return STATE_NOT_CONFIRMED
    if outcome in _WAKE_TYPED_OUTCOMES:
        return STATE_WAKE_TYPED
    if outcome in _RUNNING_OUTCOMES:
        return STATE_WAKE_ATTEMPT_RUNNING
    if outcome in _CHANNEL_OUTCOMES:
        return STATE_CHANNEL_QUEUED
    if outcome in _NO_DRIVER_OUTCOMES:
        return STATE_NO_DRIVER_WAKE
    return STATE_NOT_DELIVERED


__all__ = ["read_peer_send_outcome"]
