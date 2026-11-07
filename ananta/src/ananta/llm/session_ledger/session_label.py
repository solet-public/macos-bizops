"""Exact-row label maintenance with atomic, permanently retained clear receipts.

Ingest remains the set/correct/restore path. These receipts begin with this
feature; they do not reconstruct a complete historical metadata ledger.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.llm.session_ledger.base import LedgerRepositoryError, SessionLedgerRepositoryBase
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_SESSION_LABEL_CLEAR,
    NAMESPACE,
    TABLE_SESSION,
    TABLE_SESSION_LABEL_CLEAR,
)
from ananta.llm.session_ledger.shared import _new_id

RELOAD_SAFE = True


def _datetime(value: object, *, token: bool = False) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if not isinstance(value, datetime):
        raise ValueError("A datetime or ISO timestamp is required")
    if value.tzinfo is None:
        if token:
            raise ValueError("A timezone-aware timestamp is required")
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _records(result: ActionResult) -> list[dict[str, object]]:
    """Keep malformed successes distinct from valid empty database results."""
    if result.get("action_status") not in {"completed", "success"}:
        raise LedgerRepositoryError(f"label read failed: {result.get('error')!r}")
    data = result.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise LedgerRepositoryError("label read requires data.records as a list of dictionaries")
    return cast(list[dict[str, object]], records)


def _response(session_id: str, row: dict[str, object] | None, confirm: bool) -> dict[str, Any]:
    label = row.get("vendor_session_label") if row is not None else None
    return {
        "session_id": session_id,
        "confirmed": confirm,
        "status": "not_found" if row is None else "already_clear" if label is None else "preview",
        "previous_label": label,
        "current_label": label,
        "expected_updated_at": _datetime(row["updated_at"]).isoformat() if row is not None else None,
        "cleared_count": 0,
        "clear_id": None,
    }


class SessionLabelMaintenance:
    """Use typed state operations without broadening the repository surface."""

    def __init__(self, repository: SessionLedgerRepositoryBase) -> None:
        self._state = repository._state
        self._clock = repository._clock

    def _session(self, session_id: str) -> dict[str, object] | None:
        rows = _records(
            self._state.read_state(
                NAMESPACE,
                {
                    "table": TABLE_SESSION,
                    "filters": {"id": session_id, "is_deleted": 0},
                    "limit": 1,
                },
            )
        )
        return rows[0] if rows else None

    def clear(
        self,
        session_id: str,
        expected_updated_at: datetime | str | None,
        confirm: bool,
        principal: CallContext,
    ) -> dict[str, Any]:
        """Preview or CAS one non-null label and insert its receipt atomically."""
        if not confirm:
            return _response(session_id, self._session(session_id), False)
        with self._state.transactional() as txn:
            rows = txn.query_state(
                NAMESPACE,
                {
                    "table": TABLE_SESSION,
                    "filters": {"id": session_id, "is_deleted": 0},
                    "limit": 1,
                },
            )
            result = _response(session_id, rows[0] if rows else None, True)
            if result["status"] in {"not_found", "already_clear"}:
                return result
            token = _datetime(expected_updated_at, token=True)
            if token != _datetime(rows[0]["updated_at"]):
                result["status"] = "conflict"
                return result
            previous = rows[0]["vendor_session_label"]
            now = self._clock()
            affected = txn.update_state(
                NAMESPACE,
                {
                    "table": TABLE_SESSION,
                    "filters": {"id": session_id, "is_deleted": 0, "updated_at": token, "vendor_session_label": previous},
                },
                {"vendor_session_label": None, "updated_at": now},
            )
            if type(affected) is not int or affected not in (0, 1):
                raise LedgerRepositoryError(f"label clear expected 0 or 1 affected rows, got {affected!r}")
            if affected == 0:
                result["status"] = "conflict"
                return result
            clear_id = _new_id(ID_PREFIX_SESSION_LABEL_CLEAR)
            receipt = cast(StateServiceProtocol, self._state).prepare_record(
                NAMESPACE,
                TABLE_SESSION_LABEL_CLEAR,
                {
                    "id": clear_id,
                    "namespace": NAMESPACE,
                    "session_id": session_id,
                    "previous_label": previous,
                    "principal_kind": principal.principal_kind,
                    "principal_id": principal.principal_id,
                    "cleared_at": now,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            txn.write_state(NAMESPACE, {"table": TABLE_SESSION_LABEL_CLEAR, "record": receipt})
            result.update(status="cleared", current_label=None, cleared_count=1, clear_id=clear_id)
            return result

    def history(
        self,
        session_id: str,
        limit: int,
        after_cleared_at: datetime | str | None,
        after_id: str | None,
    ) -> dict[str, Any]:
        """Read scoped immutable receipts with a total-order keyset cursor."""
        if (after_cleared_at is None) != (after_id is None):
            raise ValueError("Both after_cleared_at and after_id are required together")
        limit = max(1, min(100, limit))
        query: dict[str, object] = {
            "table": TABLE_SESSION_LABEL_CLEAR,
            "filters": {"session_id": session_id},
            "order_by": [["cleared_at", "asc"], ["id", "asc"]],
            "limit": limit,
        }
        if after_cleared_at is not None:
            query["after"] = [_datetime(after_cleared_at, token=True), after_id]
        rows = _records(self._state.query_ordered(NAMESPACE, query))
        current = _response(session_id, self._session(session_id), False)
        if current["status"] != "not_found":
            current["status"] = "found"
        cursor = None
        if len(rows) == limit:
            cursor = {"after_cleared_at": _datetime(rows[-1]["cleared_at"]).isoformat(), "after_id": rows[-1]["id"]}
        return {"session_id": session_id, "clears": rows, "next_cursor": cursor, "current": current}
