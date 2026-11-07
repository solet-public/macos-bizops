"""Bounded scalar corpus count for ACT-R memory storage."""

from datetime import UTC, datetime
from typing import Any

from ananta.core.domain.enums import ActionStatus
from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.services.memory_service.schema import NAMESPACE


def corpus_counts(state: StateManagementInterface) -> dict[str, Any]:
    """Count all nondeleted memory rows with one owner scalar operation."""
    observed_from = datetime.now(UTC).isoformat()
    try:
        result = state.count(
            namespace=NAMESPACE,
            data={"table": "memory", "filters": {"is_deleted": 0}},
        )
    except Exception:
        result = {}
    observed_until = datetime.now(UTC).isoformat()
    value: object = None
    if result.get("action_status") == ActionStatus.COMPLETED.value:
        data = result.get("data")
        scalar = data.get("result") if isinstance(data, dict) else None
        candidate = scalar.get("value") if isinstance(scalar, dict) else None
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
            value = candidate
    complete = value is not None
    return {
        "schema_version": "1",
        "scope": "actr_memory_plugin.memory; is_deleted=0; active and archived, including knowledge-base chunks",
        "observed_from": observed_from,
        "observed_until": observed_until,
        "count": value,
        "complete": complete,
        "advisories": [] if complete else ["scalar_count_unavailable_or_malformed"],
    }
