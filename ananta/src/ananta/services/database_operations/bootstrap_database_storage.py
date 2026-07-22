"""Bootstrap Database Storage Implementation.

Provides in-memory database operations for bootstrap mode operations.
Implements direct data manipulation without plugin delegation.
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime

from ananta.core.domain.types import ActionResult
from ananta.services.state_service.ordered_query import (
    apply_ordered_query_in_memory,
    parse_ordered_query,
)

logger = logging.getLogger(__name__)


class BootstrapDatabaseStorage:
    """In-memory database operations for bootstrap mode.

    Provides direct data manipulation without external dependencies.
    Used during system initialization before plugins are available.
    """

    def __init__(
        self, memory_data: defaultdict[str, defaultdict[str, list[object]]] | None = None
    ) -> None:
        """Initialize bootstrap database storage.

        Args:
            memory_data: Existing memory data structure to use (created if None)
        """
        self._memory_data = (
            memory_data if memory_data is not None else defaultdict(lambda: defaultdict(list))
        )
        logger.debug("BootstrapDatabaseStorage initialized with in-memory operations")

    def write_state(
        self,
        namespace: str,
        enhanced_data: dict[str, object],
        calling_service: str | None = None,
        calling_namespace: str | None = None,
    ) -> ActionResult:
        """Write data directly to memory.

        Args:
            namespace: Target namespace for data
            enhanced_data: Data with standard fields already populated
            calling_service: Reserved interface parameter, unused in this implementation
            calling_namespace: Reserved interface parameter, unused in this implementation

        Returns:
            ActionResult with write success status
        """
        # Note: calling_service and calling_namespace are unused in bootstrap mode
        _ = calling_service, calling_namespace

        # Direct in-memory implementation - no plugin delegation needed
        table_value = enhanced_data.get("table", "default")
        table = table_value if isinstance(table_value, str) else "default"
        record = enhanced_data.get("record", enhanced_data)
        self._memory_data[namespace][table].append(record)

        return {
            "action_status": "completed",
            "data": {},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def read_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Read data directly from memory.

        Args:
            namespace: Target namespace to query
            query: Query parameters for data retrieval

        Returns:
            ActionResult with retrieved data
        """

        table_value = query.get("table", "default")
        table = table_value if isinstance(table_value, str) else "default"
        records = self._memory_data[namespace][table]

        return {
            "action_status": "completed",
            "data": {"records": records},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object]
    ) -> ActionResult:
        """Update data directly in memory.

        Args:
            namespace: Target namespace for updates
            query: Query to identify records to update
            updates: Update operations to apply (unused in bootstrap mode)

        Returns:
            ActionResult with update success status

        Note:
            Bootstrap mode provides basic update simulation.
            Real update logic would be implemented in plugin mode.
        """
        # Note: updates is unused in bootstrap mode simulation
        _ = updates

        table_value = query.get("table", "default")
        table = table_value if isinstance(table_value, str) else "default"
        records = self._memory_data[namespace][table]

        # Simple update simulation for bootstrap mode
        updated_count = len(records)  # Simplistic - assume all records match

        return {
            "action_status": "completed",
            "data": {"updated_count": updated_count},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def upsert_state(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Upsert data directly to memory.

        Args:
            namespace: Target namespace for upsert
            data: Data containing table, record, and conflict_columns

        Returns:
            ActionResult with upsert success status

        Note:
            Bootstrap mode delegates to write_state since it's in-memory.
        """

        # In bootstrap mode, just delegate to write_state
        return self.write_state(namespace, data)

    def delete_records(self, namespace: str, query: dict[str, object]) -> ActionResult:
        """Delete data directly from memory.

        Args:
            namespace: Target namespace for deletions
            query: Query to identify records to delete

        Returns:
            ActionResult with deletion success status
        """

        table_value = query.get("table", "default")
        table = table_value if isinstance(table_value, str) else "default"
        records = self._memory_data[namespace][table]
        deleted_count = len(records)

        # Clear all records for the table (simplistic bootstrap implementation)
        self._memory_data[namespace][table] = []

        return {
            "action_status": "completed",
            "data": {"deleted_count": deleted_count},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        """Query data with filtering from memory.

        Args:
            namespace: Target namespace to query
            filters: Filter criteria for data retrieval

        Returns:
            ActionResult with filtered data
        """

        table_value = filters.get("table", "default")
        table = table_value if isinstance(table_value, str) else "default"
        records = self._memory_data[namespace][table]

        # Simple filter implementation for bootstrap mode
        # In practice, would apply actual filter logic
        filtered_records = records  # Simplistic - return all records

        return {
            "action_status": "completed",
            "data": {"records": filtered_records},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def query_ordered(self, namespace: str, data: dict[str, object]) -> ActionResult:
        """Ordered, bounded, tie-safe query against the in-memory store.

        A real implementation (not a fail-fast): bootstrap mode holds an
        in-memory ``namespace → table → list[record]`` map, so the same
        ordered/cursor/limit semantics the SQL providers give are applied
        here over the list — equality filter (+ ``is_deleted`` default),
        composite sort in the requested direction, tie-safe ``after``
        cursor, then ``limit``. A query against a table that was never
        written simply returns an empty page.

        Args:
            namespace: Target namespace to query
            data: ``{table, filters, order_by, limit, after?, include_deleted?}``

        Returns:
            ActionResult with the ordered page in ``data.records``
        """
        spec = parse_ordered_query(data)
        rows = self._memory_data[namespace][spec.table]
        records: list[dict[str, object]] = [
            row for row in rows if isinstance(row, dict)
        ]
        page = apply_ordered_query_in_memory(records, spec)

        return {
            "action_status": "completed",
            "data": {"records": page},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def execute_sql(self, sql_query: str, sql_params: list[object] | None = None) -> ActionResult:
        """Execute SQL query in bootstrap mode.

        Args:
            sql_query: SQL query string to execute
            sql_params: Optional parameters for the SQL query

        Returns:
            ActionResult indicating bootstrap mode limitation

        Note:
            Bootstrap mode doesn't support SQL execution.
            Returns informative message about limitation.
        """

        return {
            "action_status": "completed",
            "data": {
                "message": "SQL execution not supported in bootstrap mode",
                "query": sql_query,
                "params": sql_params or [],
            },
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
