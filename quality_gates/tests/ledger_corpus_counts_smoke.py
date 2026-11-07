"""Contract checks for the scalar session-ledger corpus count."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))


def _service(state: object) -> Any:
    """Build the real read facade around a narrow state fake."""
    from ananta.services.session_ledger_service.service import SessionLedgerService

    service = object.__new__(SessionLedgerService)
    service._state_service = state
    return service


class CountState:
    """State fake that rejects every row read and records scalar calls."""

    def __init__(self, result: object, rows: list[dict[str, object]] | None = None) -> None:
        self.result = result
        self.rows = rows
        self.calls: list[tuple[str, dict[str, object]]] = []

    def count(self, namespace: str, data: dict[str, object]) -> object:
        self.calls.append((namespace, data))
        if isinstance(self.result, BaseException):
            raise self.result
        if self.rows is not None:
            filters = data.get("filters")
            assert isinstance(filters, dict), filters
            value = sum(1 for row in self.rows if filters.get("is_deleted") != 0 or row.get("is_deleted") == 0)
            return {"action_status": "completed", "data": {"result": {"value": value}}}
        return self.result

    def query_state(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("scalar_only: row query attempted")

    def query_ordered(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("scalar_only: ordered row query attempted")


def scalar_reads() -> None:
    """The service exposes the dedicated count read."""
    from ananta.services.session_ledger_service.service import SessionLedgerService

    service = object.__new__(SessionLedgerService)
    assert callable(getattr(service, "corpus_counts", None)), "missing corpus_counts"


def scalar_only() -> None:
    """One owning scalar call returns only a fixed-size envelope."""
    state = CountState({"action_status": "completed", "data": {"result": {"value": 17}}})
    result = _service(state).corpus_counts()
    assert result["count"] == 17 and result["complete"] is True, result
    assert len(state.calls) == 1, state.calls
    namespace, data = state.calls[0]
    assert namespace == "session_ledger", namespace
    assert data.get("table") == "event", data
    assert set(result) == {
        "schema_version",
        "scope",
        "observed_from",
        "observed_until",
        "count",
        "complete",
        "advisories",
    }, result
    observed_from = datetime.fromisoformat(result["observed_from"])
    observed_until = datetime.fromisoformat(result["observed_until"])
    assert observed_from.utcoffset() == timedelta(0), observed_from
    assert observed_until.utcoffset() == timedelta(0), observed_until
    assert observed_from <= observed_until, (observed_from, observed_until)


def scope_rows() -> None:
    """Sibling and disabled-source events count; deleted events do not."""
    events = [
        {"session": "canonical", "source_enabled": True, "is_deleted": 0},
        {"session": "sibling", "source_enabled": True, "is_deleted": 0},
        {"session": "disabled-source", "source_enabled": False, "is_deleted": 0},
        {"session": "canonical", "source_enabled": True, "is_deleted": 1},
    ]
    state = CountState({}, events)
    result = _service(state).corpus_counts()
    assert result["count"] == 3 and result["complete"] is True, result
    assert state.calls == [("session_ledger", {"table": "event", "filters": {"is_deleted": 0}})], state.calls
    assert state.calls == [("session_ledger", {"table": "event", "filters": {"is_deleted": 0}})], state.calls


def zero_empty() -> None:
    """A completed empty-set count is exact zero."""
    state = CountState({"action_status": "completed", "data": {"result": {"value": 0}}})
    result = _service(state).corpus_counts()
    assert result["count"] == 0 and result["complete"] is True, result
    assert result["advisories"] == [], result


def failed_count() -> None:
    """A failed scalar result remains unavailable rather than zero."""
    state = CountState({"action_status": "error", "data": {"result": {"value": 0}}})
    result = _service(state).corpus_counts()
    assert result["count"] is None and result["complete"] is False, result
    assert result["advisories"] == ["count_unavailable:state_count_failed"], result
    raised = CountState(RuntimeError("sensitive detail is not returned"))
    result = _service(raised).corpus_counts()
    assert result["count"] is None and result["complete"] is False, result
    assert result["advisories"] == ["count_unavailable:state_count_raised"], result


def bad_scalar() -> None:
    """Malformed, boolean, negative, and noninteger scalars are unavailable."""
    for value in (True, -1, "4", 4.0, None):
        state = CountState({"action_status": "completed", "data": {"result": {"value": value}}})
        result = _service(state).corpus_counts()
        assert result["count"] is None and result["complete"] is False, (value, result)
        assert result["advisories"] == ["count_unavailable:invalid_scalar"], (value, result)
    malformed = CountState({"action_status": "completed", "data": []})
    result = _service(malformed).corpus_counts()
    assert result["count"] is None and result["complete"] is False, result
    missing = CountState(None)
    result = _service(missing).corpus_counts()
    assert result["count"] is None and result["complete"] is False, result


def discovery_json() -> None:
    """Decorator discovery and the shipped process definition agree."""
    from ananta.services.session_ledger_service.interfaces.public import SessionLedgerReadAPI

    metadata = SessionLedgerReadAPI.corpus_counts._service_interface_metadata
    process = metadata.to_process_dict()
    path = _ROOT / "ananta/knowledge_base/processes/session_ledger_service/corpus_counts.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    assert metadata.name == "corpus_counts" and metadata.provider == "session_ledger_service", metadata
    assert metadata.is_discoverable is True and metadata.parameters == {}, metadata
    assert process["provider_type"] == "service_interface" and process["parameters"] == {}, process
    assert document["process_key"] == "service_interface::session_ledger_service::corpus_counts", document
    assert document["parameters"] == {}, document
    assert document["display_name"] == metadata.display_name, (document, metadata)


def private_pg() -> None:
    """Real private PostgreSQL COUNT exceeds the 100-row materialization cap."""
    if "PROJECT_SOLET_DESK_TEST_DSN" not in os.environ:
        print("SKIP private PostgreSQL corpus-count case: provision_test_pg fixture absent")
        return
    fixture_root = _ROOT / "ananta/tests/llm/session_ledger"
    sys.path.insert(0, str(fixture_root))
    from session_usage_totals_smoke import Fixture, claude_line

    fixture = Fixture()
    try:
        external = "corpus-count-over-row-cap"
        lines = [claude_line(index, minute=index) for index in range(125)]
        fixture.ingest(external, lines)
        from types import SimpleNamespace
        from typing import cast

        from ananta.services.session_ledger_service.service import SessionLedgerService

        service = SessionLedgerService(
            state_service=fixture.state,
            blob_storage_service=cast(Any, SimpleNamespace()),
            plugin_manager=cast(Any, SimpleNamespace(plugins={})),
        )
        result = service.corpus_counts()
        assert result["count"] > 100 and result["complete"] is True, result
        assert result["count"] == 125, result
        assert result["scope"].startswith("session_ledger__event where is_deleted = 0"), result
    finally:
        fixture.close()


def main() -> None:
    scalar_reads()
    scalar_only()
    scope_rows()
    zero_empty()
    failed_count()
    bad_scalar()
    discovery_json()
    private_pg()
    print("OK: ledger corpus counts")


if __name__ == "__main__":
    main()
