"""Filter normalization and hard result bounds for the public fleet roster."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from ananta.services.state_service.bounded_read import ReadCeilingError

from .schema import (
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_SPAWNING,
)
from .session_lifecycle_store import list_managed_sessions_bounded

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

# A fleet roster is an operator-facing context payload, not an export surface.
# Fifty rows is enough for ordinary lane/host/state coordination while staying
# well under the state provider's 100-row page cap. The 250 hard maximum permits
# a deliberately broader filtered survey without allowing a caller to turn this
# verb back into a whole append-mostly-ledger dump.
LIST_SESSIONS_DEFAULT_LIMIT: Final = 50
LIST_SESSIONS_MAX_LIMIT: Final = 250

_FILTER_KEYS: Final = ("lane_id", "work_class", "host", "lifecycle_state")
_LIVE_STATES: Final = (
    LIFECYCLE_SPAWNING,
    LIFECYCLE_LIVE,
    LIFECYCLE_IDLE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
)


class SessionListError(ValueError):
    """A public list contract violation with its stable processor code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def _selected_filters(
    filters: dict[str, Any] | None, *, live_only: bool,
) -> dict[str, Any]:
    selected = {
        key: value
        for key in _FILTER_KEYS
        if filters is not None and (value := filters.get(key)) not in (None, "")
    }
    if not selected and not live_only:
        accepted = ", ".join((*_FILTER_KEYS, "live_only=true"))
        raise SessionListError(
            "filter_required",
            f"list_sessions requires at least one fleet filter ({accepted}); "
            "to list the live fleet pass {\"live_only\": true}.",
        )
    if live_only:
        selected["lifecycle_state"] = _live_lifecycle_filter(
            selected.get("lifecycle_state")
        )
    return selected


def _live_lifecycle_filter(current_filter: object) -> object:
    """Intersect an optional lifecycle predicate with the non-terminal set."""
    if current_filter is None:
        return list(_LIVE_STATES)
    if isinstance(current_filter, (list, tuple)):
        return [value for value in current_filter if value in _LIVE_STATES]
    return current_filter if current_filter in _LIVE_STATES else []


def _validated_limit(limit: object) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= LIST_SESSIONS_MAX_LIMIT
    ):
        raise SessionListError(
            "invalid_limit",
            f"list_sessions limit must be an integer from 1 through {LIST_SESSIONS_MAX_LIMIT}.",
        )
    return limit


def list_session_rows(
    state: StateManagementInterface,
    filters: dict[str, Any] | None,
    *,
    live_only: bool,
    limit: object,
) -> list[dict[str, Any]]:
    """Return a complete bounded roster or refuse before returning any rows."""
    selected_filters = _selected_filters(filters, live_only=live_only)
    selected_limit = _validated_limit(limit)
    try:
        return list_managed_sessions_bounded(
            state, selected_filters, limit=selected_limit,
        )
    except ReadCeilingError as exc:
        raise SessionListError(
            "result_over_limit",
            f"list_sessions matched more than limit={selected_limit} rows; narrow the fleet "
            "filters and retry.",
        ) from exc


__all__ = [
    "LIST_SESSIONS_DEFAULT_LIMIT",
    "LIST_SESSIONS_MAX_LIMIT",
    "SessionListError",
    "list_session_rows",
]
