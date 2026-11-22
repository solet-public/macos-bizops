"""The one typed reason an ended session's record carries.

A record that ended says why, as exactly one of :data:`END_REASONS`. The value is
written by :func:`session_lifecycle_store.transition_lifecycle_state`, in the same
predicated update that moves the record to terminated, and by nothing else. A
terminal write that names no reason stores :data:`UNKNOWN`, shown as "ended, reason
unknown", so no ending path can leave the field out. A record that ended before the
column existed holds a blank and reads the same way.

This module is a leaf: it imports only the lifecycle constants, so the store, the
end functions and the verbs can all import it without a cycle.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from .schema import LIFECYCLE_RETIRED, LIFECYCLE_TERMINATED

if TYPE_CHECKING:
    from collections.abc import Mapping

FINISHED: Final = "finished"
FAILED: Final = "failed"
STOPPED_BY_ASKER: Final = "stopped_by_asker"
STOPPED_BY_SOLET: Final = "stopped_by_solet"
REFUSED_BY_PROVIDER: Final = "refused_by_provider"
UNKNOWN: Final = "unknown"

END_REASONS: Final = (FINISHED, FAILED, STOPPED_BY_ASKER, STOPPED_BY_SOLET, REFUSED_BY_PROVIDER, UNKNOWN)

_ENDED_STATES: Final = frozenset({LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED})


def require_end_reason(value: object) -> str:
    """``value`` when it is one of :data:`END_REASONS`; otherwise ``ValueError``."""
    if not isinstance(value, str) or value not in END_REASONS:
        raise ValueError(f"end_reason must be one of {', '.join(END_REASONS)}; got {value!r}")
    return value


def end_reason_of(row: Mapping[str, Any]) -> str | None:
    """The reason a record ended: ``None`` while it has not ended, else the stored
    reason when it is a valid one, else :data:`UNKNOWN`."""
    if row.get("lifecycle_state") not in _ENDED_STATES:
        return None
    stored = row.get("end_reason")
    return stored if isinstance(stored, str) and stored in END_REASONS else UNKNOWN
