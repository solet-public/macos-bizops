#!/usr/bin/env python3
"""No-database postgres delete filter regression and shared-builder parity.

The recording cursor validates generated SQL before applying the exact gauge
prune predicate to an in-memory fixture. This is builder/adapter proof, not
live PostgreSQL behavioral coverage.
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
for package in ("ananta", "plugins/postgres_state_management_plugin"):
    sys.path.insert(0, str(ROOT / package / "src"))

import postgres_state_management_plugin.postgres_backend.provider as provider_module  # noqa: E402
from postgres_state_management_plugin.postgres_backend.provider import PostgresProvider as PgProvider  # noqa: E402


def provider_fixture(cls: Any) -> Any:
    provider = cls.__new__(cls)
    provider.config = SimpleNamespace(schema_name="fixture")
    return provider


def prove_older_rows_lt(provider: Any, label: str) -> None:
    rows = [("old", "agi-main", 1), ("tie", "agi-main", 2),
            ("new", "agi-main", 3), ("other", "agi-other", 1)]
    cursor = MagicMock()
    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor

    def execute(statement: Any, params: list[object]) -> None:
        head, predicate = statement.as_string(None).split(" WHERE ")
        assert head.split(" ", 2) == ["DELETE", "FROM", '"fixture"."gauge__history"'], f"{label}: hard_delete_header"
        assert predicate == '"agent_instance_id" = %s AND "recorded_at" < %s', f"{label}: older_rows_lt_predicate"
        assert params == ["agi-main", 2], f"{label}: older_rows_lt_bound_values"
        rows[:] = [row for row in rows if not (row[1] == params[0] and row[2] < 2)]
        cursor.rowcount = 1

    cursor.execute.side_effect = execute
    filters = {"agent_instance_id": "agi-main", "recorded_at": {"op": "lt", "value": 2}}
    with patch.object(provider, "get_connection") as acquire:
        acquire.return_value.__enter__.return_value = connection
        assert provider.delete("gauge", "history", filters, soft_delete=False) == 1
    assert [row[0] for row in rows] == ["tie", "new", "other"], f"{label}: older_rows_lt_survivors"
    print(f"PASS {label}: older_rows_lt keeps cutoff ties, newer rows and other instances")


def prove_shared_grammar(provider: Any, label: str) -> None:
    cases: tuple[tuple[object, str, list[object]], ...] = (
        ("value", '"field" = %s', ["value"]),
        ([1, 2], '"field" = ANY(%s)', [[1, 2]]),
        ((1, 2), '"field" = ANY(%s)', [[1, 2]]),
        ([], '"field" = ANY(%s)', [[]]),
        ({"op": "is_null"}, '"field" IS NULL', []),
        ({"op": "is_not_null"}, '"field" IS NOT NULL', []),
        ({"op": "lt", "value": 2}, '"field" < %s', [2]),
        ({"op": "lte", "value": 2}, '"field" <= %s', [2]),
        ({"op": "gt", "value": 2}, '"field" > %s', [2]),
        ({"op": "gte", "value": 2}, '"field" >= %s', [2]),
    )
    for value, expected, values in cases:
        filters = {"field": value}
        for soft in (False, True):
            statement, params = provider.build_delete_sql("gauge", "history", filters, soft_delete=soft)
            assert statement.as_string(None).split(" WHERE ")[1] == expected, f"{label}: shared_grammar_{value}"
            assert params == ([1] if soft else []) + values
    print(f"PASS {label}: shared delete builder scalar/array/null/range grammar")


def prove_invalid_before_execution(provider: Any, label: str) -> None:
    invalid: tuple[dict[str, object], ...] = ({"op": "unknown"}, {"op": "lt"})
    for value in invalid:
        for soft in (False, True):
            with patch.object(provider, "get_connection") as acquire:
                try:
                    provider.delete("gauge", "history", {"field": value}, soft_delete=soft)
                except ValueError as exc:
                    assert "op" in str(exc) or "value" in str(exc)
                else:
                    raise AssertionError(f"{label}: invalid_operator_must_refuse")
                acquire.assert_not_called()
    print(f"PASS {label}: unknown operator and missing comparison value refuse before execution")


def prove_autocommit_bindings(provider: Any, label: str) -> None:
    aware = datetime(2026, 10, 4, tzinfo=UTC)
    cursor, connection = MagicMock(), MagicMock()
    connection.cursor.return_value.__enter__.return_value = cursor
    cursor.rowcount = 1
    with patch.object(provider, "get_connection") as acquire, patch.object(
        provider, "build_delete_sql", wraps=provider.build_delete_sql,
    ) as builder:
        acquire.return_value.__enter__.return_value = connection
        provider.delete("gauge", "history", {"recorded_at": {"op": "lt", "value": aware}}, soft_delete=False)
        builder.assert_called_once()
    assert cursor.execute.call_args.args[1] == [aware], f"{label}: autocommit_raw_datetime_binding"
    print(f"PASS {label}: autocommit routes through shared builder and preserves raw datetime binding")


def main() -> None:
    print(f"module.__file__: {provider_module.__file__}", flush=True)
    for cls, label in ((PgProvider, "postgres"),):
        provider = provider_fixture(cls)
        prove_older_rows_lt(provider, label)
        prove_shared_grammar(provider, label)
        prove_invalid_before_execution(provider, label)
        prove_autocommit_bindings(provider, label)


if __name__ == "__main__":
    main()
