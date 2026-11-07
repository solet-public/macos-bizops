"""Scheduling-owned immutable history, cursor validation and audit provenance."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from ananta.core.services.call_context import CallContext
from ananta.interfaces.state_management_interface import StateTransaction
from ananta.interfaces.state_service_protocol import StateServiceProtocol

from .action_target import targets_for
from .models import ScheduleData

RELOAD_SAFE = True


def actor_from(state: dict[str, Any]) -> str:
    """Accept only the server-constructed CallContext, never a params actor."""
    context = state.get("call_context")
    if isinstance(context, CallContext):
        identity = context.principal_id or context.calling_plugin
        return f"{context.principal_kind}:{identity}" if identity else context.principal_kind
    return "attribution_unavailable"


def records_from(result: object) -> list[dict[str, Any]]:
    """A missing/malformed page cannot prove an empty registry."""
    if not isinstance(result, dict) or result.get("action_status") != "completed":
        detail = result.get("error") or result.get("error_message") if isinstance(result, dict) else "malformed_response"
        raise RuntimeError(f"provider_read_failed: {detail or 'unspecified_error'}")
    data = result.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("malformed_page")
    return records


def row_cursor(row: dict[str, Any]) -> tuple[datetime, str]:
    """Normalize the immutable composite for ordering without losing the wire value."""
    created = row.get("created_at")
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if not isinstance(created, datetime) or not isinstance(row.get("id"), str) or not row["id"]:
        raise ValueError("malformed_cursor")
    return created.replace(tzinfo=UTC) if created.tzinfo is None else created.astimezone(UTC), row["id"]


def history_record(before: ScheduleData | None, after: ScheduleData | None, operation: str, actor: str, reason: str) -> dict[str, object]:
    """No FK to the active row: terminal deletion must retain this evidence."""
    snapshot = after if after is not None else before
    if snapshot is None or not snapshot.id:
        raise ValueError("history_requires_schedule_identity")
    revision = snapshot.revision + (1 if after is None else 0)
    return {
        "schedule_id": snapshot.id,
        "revision": revision,
        "operation": operation,
        "before_target": json.dumps(targets_for(before) if before else [], ensure_ascii=False),
        "after_target": json.dumps(targets_for(after) if after else [], ensure_ascii=False),
        "before_status": before.status if before else None,
        "after_status": after.status if after else "cleared" if operation == "clear" else "completed",
        "snapshot": snapshot.model_dump_json(),
        "actor": actor,
        "reason": reason,
    }


def append_history(state: StateServiceProtocol, transaction: StateTransaction, namespace: str, before: ScheduleData | None, after: ScheduleData | None, operation: str, actor: str, reason: str) -> None:
    """Baseline observation and mutation are in the caller's row transaction."""
    if before is not None and before.revision == 0 and transaction.count(namespace, {"table": "binding_history", "filters": {"schedule_id": before.id}}) == 0:
        baseline = history_record(None, before, "observed_baseline", "attribution_unavailable", "First observed revision; historical creator is unknown")
        transaction.write_state(namespace, {"table": "binding_history", "record": state.prepare_record(namespace, "binding_history", baseline)})
    event = history_record(before, after, operation, actor, reason)
    transaction.write_state(namespace, {"table": "binding_history", "record": state.prepare_record(namespace, "binding_history", event)})
    transaction.increment_and_return(namespace, {"table": "registry_metadata", "filters": {"key": "registry"}, "column": "epoch", "by": 1})


def decoded_history(row: dict[str, Any]) -> dict[str, Any]:
    """Return independent structured snapshots; history itself is never updated."""
    result = dict(row)
    for key in ("before_target", "after_target", "snapshot"):
        if isinstance(result.get(key), str):
            result[key] = json.loads(result[key])
    return result
