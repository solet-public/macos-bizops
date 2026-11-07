"""Transactional in-memory state for job tests; no database or host access."""

from __future__ import annotations

import threading
from contextlib import AbstractContextManager
from copy import deepcopy
from types import TracebackType
from typing import Any

from ananta.config.core_schemas import CoreSchemaDefinitions
from ananta.services.state_service import StateService
from ananta.services.state_service.ordered_query import apply_ordered_query_in_memory, parse_ordered_query
from ananta.services.string_generation_operations.string_generation_service import StringGenerationService
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import ColumnDefinition

_JOB_SCHEMAS = {
    table: schema
    for definition in (
        CoreSchemaDefinitions.get_job_schema(),
        CoreSchemaDefinitions.get_job_payload_schema(),
    )
    for table, schema in SchemaStandardizer().standardize_schema(definition).tables.items()
}


def _assert_not_null_columns(
    columns: dict[str, ColumnDefinition],
    record: dict[str, Any],
) -> None:
    for name, column in columns.items():
        if not (column.not_null or column.primary_key):
            continue
        if name in record:
            assert record[name] is not None, f"NOT NULL column {name} is null"
        else:
            assert column.default is not None and column.default != "__CONTRACT:auto_id_with_prefix__", f"NOT NULL column {name} has no database default"


def _assert_prepared_job_record(table: str, record: dict[str, Any]) -> None:
    schema = _JOB_SCHEMAS.get(table)
    if schema is None:
        return
    # Nullable metadata still belongs to the standard-preparation contract.
    for name in ("namespace", "name", "created_by", "updated_by"):
        assert name in record, f"transaction requires prepared standard field {name}"
    _assert_not_null_columns(schema.columns, record)


class JobState:
    def __init__(self) -> None:
        self._id_service = StateService.__new__(StateService)
        self._id_service._string_generation_service = StringGenerationService()
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.lock = threading.RLock()
        self.serial = 0
        self.fail_write = ""
        self.fail_update = ""
        self.fail_commit = False
        self.write_state_calls: list[dict[str, Any]] = []
        self.update_state_calls: list[dict[str, Any]] = []

    def prepare_record(
        self,
        namespace: str,
        table_name: str,
        record: dict[str, object],
    ) -> dict[str, object]:
        return self._id_service.prepare_record(namespace, table_name, record)

    @staticmethod
    def matches(row: dict[str, Any], filters: dict[str, Any]) -> bool:
        for key, value in filters.items():
            actual = row.get(key)
            if value == {"op": "is_null"}:
                matched = actual is None
            elif isinstance(value, list):
                matched = actual in value
            else:
                matched = actual == value
            if not matched:
                return False
        return True

    def read_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        with self.lock:
            rows = [deepcopy(row) for row in self.rows.get(query["table"], []) if self.matches(row, query.get("filters", {}))]
            order = query.get("order_by")
            if isinstance(order, str):
                field, direction = order.split()
                rows.sort(key=lambda row: row[field], reverse=direction.lower() == "desc")
            return {"action_status": "completed", "data": {"records": rows[: query.get("limit")]}}

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        with self.lock:
            spec = parse_ordered_query(query)
            rows = apply_ordered_query_in_memory(self.rows.get(spec.table, []), spec)
            return {"action_status": "completed", "data": {"records": rows, "count": len(rows)}}

    def query_state(self, namespace: str, filters: dict[str, Any]) -> dict[str, Any]:
        return self.read_state(namespace, filters)

    def write_state(
        self,
        namespace: str,
        data: dict[str, Any],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> dict[str, Any]:
        del namespace, calling_service, calling_namespace
        with self.lock:
            if data["table"] == self.fail_write:
                raise RuntimeError(f"injected write failure: {self.fail_write}")
            self.serial += 1
            row = deepcopy(data["record"])
            row.setdefault("id", f"{data['table']}-{self.serial}")
            self.rows.setdefault(data["table"], []).append(row)
            self.write_state_calls.append(deepcopy(data))
            return {"action_status": "completed", "data": {"result": {"generated_id": row["id"]}}}

    def update_state(
        self,
        namespace: str,
        query: dict[str, Any],
        updates: dict[str, Any],
    ) -> dict[str, Any]:
        del namespace
        with self.lock:
            if query["table"] == self.fail_update:
                raise RuntimeError(f"injected update failure: {self.fail_update}")
            count = 0
            for row in self.rows.get(query["table"], []):
                if self.matches(row, query.get("filters", {})):
                    row.update(deepcopy(updates))
                    count += 1
            self.update_state_calls.append(deepcopy(updates))
            return {"action_status": "completed", "data": {"result": {"updated": count}}}

    def upsert_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            keys = data["conflict_columns"]
            filters = {key: data["record"][key] for key in keys}
            present = any(self.matches(row, filters) for row in self.rows.get(data["table"], []))
            if not present:
                self.write_state(namespace, data)
            return {"action_status": "completed", "data": {"result": {"inserted": not present}}}

    def delete_records(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        del namespace
        with self.lock:
            rows = self.rows.get(query["table"], [])
            remaining = [row for row in rows if not self.matches(row, query["filters"])]
            self.rows[query["table"]] = remaining
            return {"action_status": "completed", "data": {"result": {"deleted": len(rows) - len(remaining)}}}

    def count(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        count = len(self.read_state(namespace, data)["data"]["records"])
        return {"action_status": "completed", "data": {"result": {"value": count}}}

    def transactional(self) -> JobTransaction:
        return JobTransaction(self)


class JobTransaction(AbstractContextManager["JobTransaction"]):
    def __init__(self, state: JobState) -> None:
        self.state = state
        self.before: dict[str, list[dict[str, Any]]] = {}

    def __enter__(self) -> JobTransaction:
        self.state.lock.acquire()
        self.before = deepcopy(self.state.rows)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            if exc_type is not None or self.state.fail_commit:
                self.state.rows = self.before
            if exc_type is None and self.state.fail_commit:
                raise RuntimeError("injected commit failure")
        finally:
            self.state.lock.release()

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        _assert_prepared_job_record(data["table"], data["record"])
        return str(self.state.write_state(namespace, data)["data"]["result"]["generated_id"])

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> int:
        return int(self.state.update_state(namespace, query, updates)["data"]["result"]["updated"])

    def query_state(self, namespace: str, filters: dict[str, Any]) -> list[dict[str, Any]]:
        return self.state.read_state(namespace, filters)["data"]["records"]
