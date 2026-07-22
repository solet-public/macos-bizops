"""GAP-2 one-time backfill: project ``metadata.important`` → the ``important`` column.

The SQL-lockdown migration promoted the silent-bucket discriminator from a raw
``metadata->>'important'`` JSONB predicate (which the state interface cannot
express) to a first-class boolean column on ``core__agent_message`` (schema.py).
The additive column lands with ``default=False``, so EXISTING rows whose
``metadata.important`` is true would default to ``important=False`` and wrongly
re-surface in the silent peer-inbox (``list_peer_messages_for(silent_only=True)``).
This backfill copies the JSONB flag onto the column for those pre-migration rows.

**ONE-SHOT, durable-marker-gated**: runs to
completion EXACTLY ONCE, gated by a durable key-value marker set ONLY after a
SUCCESSFUL full pass. Set-on-success-only is self-healing — a fault before the
marker leaves it unset → the next boot re-runs. Idempotent regardless: it flips
ONLY rows where the JSONB flag is true and the column is still false, so a re-run
(or a lost marker) is a no-op.

State-interface ONLY (no raw SQL/DDL): ``query_state`` UNCAPPED over the whole
table — NOT ``query_ordered``, whose 100-cap would silently truncate this
unbounded table — to read every row, then ``update_state`` to flip the column on
the matching rows (a small set; IMPORTANT messages are a fraction). New rows get
the column from ``AgentMessagingRepository._insert_message`` directly, so this
backfill is purely for pre-migration history.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.schema import NAMESPACE, TABLE_AGENT_MESSAGE
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_ID = "id"
_COL_IMPORTANT = "important"
_COL_METADATA = "metadata"

# Durable one-shot marker (set ONLY after a successful full pass → self-healing).
_BACKFILL_MARKER_KEY = "agent_message_important_backfill_v1_complete"
_BACKFILL_MARKER_VALUE = "true"

# Returned ``status`` discriminators.
STATUS_COMPLETED = "completed"
STATUS_ALREADY_DONE = "already_done"


def backfill_message_important(state: StateManagementInterface) -> dict[str, object]:
    """ONE-SHOT projection of ``metadata.important`` onto the ``important`` column.

    Returns ``{"updated": [...ids], "status": ...}``. Gated by a durable marker
    (``status="already_done"`` on every boot after the first successful run). The
    marker is set ONLY after the full pass succeeds, so any fault before it leaves
    the marker unset → the next boot re-runs (self-healing).
    """
    if _backfill_already_complete(state):
        return {"updated": [], "status": STATUS_ALREADY_DONE}
    result = state.query_state(
        NAMESPACE, {"table": TABLE_AGENT_MESSAGE, "filters": {}},
    )
    updated: list[str] = []
    for row in require_records(result):
        if not _needs_backfill(row):
            continue
        message_id = str(row[_COL_ID])
        require_updated(
            state.update_state(
                NAMESPACE,
                {"table": TABLE_AGENT_MESSAGE, "filters": {_COL_ID: message_id}},
                {_COL_IMPORTANT: True},
            ),
        )
        updated.append(message_id)
    # SUCCESS — full pass completed without raising. Set the marker ONLY now so a
    # partial/failed run re-runs next boot.
    _mark_backfill_complete(state)
    if updated:
        logger.info(
            "agent_message important backfill flipped %d pre-migration row(s)",
            len(updated),
        )
    return {"updated": updated, "status": STATUS_COMPLETED}


def _needs_backfill(row: dict[str, object]) -> bool:
    """True iff ``metadata.important`` is truthy but the column is still false."""
    if bool(row.get(_COL_IMPORTANT, False)):
        return False
    return _metadata_important(row.get(_COL_METADATA))


def _metadata_important(value: object) -> bool:
    """Truthy ``metadata.important`` from a dict or JSON-string cell (dual-shape)."""
    if isinstance(value, str):
        value = json.loads(value) if value else {}
    if isinstance(value, dict):
        return bool(value.get("important", False))
    return False


def _backfill_already_complete(state: StateManagementInterface) -> bool:
    """True once the one-shot marker has been durably set by a successful run."""
    data = require_completed(
        state.get_key_value(NAMESPACE, _BACKFILL_MARKER_KEY),
        "get message-important backfill marker",
    )
    return bool(data.get("found"))


def _mark_backfill_complete(state: StateManagementInterface) -> None:
    """Durably record completion (set on success only → exactly-once + self-healing)."""
    require_completed(
        state.set_key_value(NAMESPACE, _BACKFILL_MARKER_KEY, _BACKFILL_MARKER_VALUE),
        "set message-important backfill marker",
    )


__all__ = ["backfill_message_important"]
