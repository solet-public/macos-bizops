#!/usr/bin/env python3
"""Typed-schema smoke for nullability-relaxation schema diffs."""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
# macos_self_deployment_plugin is resolved PER PROFILE and is absent from
# macos_free_minimal, so only the blue-green preflight leg depends on it; that
# leg runs where the plugin ships and is disclosed as skipped where it does not
# (the same repair io_interface_shadow_docs_smoke applies to its discord leg).
_PREFLIGHT_SRC = REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"
if _PREFLIGHT_SRC.is_dir():
    sys.path.insert(0, str(_PREFLIGHT_SRC))

from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)
from postgres_state_management_plugin.postgres_backend.schema_diff import (  # noqa: E402
    _is_nullability_relaxation_only,
    diff_schema,
)

_FIXTURE_NAMESPACE = "schema_diff_nullability_fixture"
_FIXTURE_TABLE = "nullable_controls"
_RELAXED_COLUMNS = (
    "ttl_seconds",
    "expires_at",
    "expires_at_window_seconds",
)
_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _schemas() -> tuple[SchemaDefinition, SchemaDefinition]:
    declared_table = TableSchema(
        table_name=_FIXTURE_TABLE,
        columns={
            "ttl_seconds": ColumnDefinition(type=ColumnType.INTEGER),
            "expires_at": ColumnDefinition(type=ColumnType.DATETIME),
            "expires_at_window_seconds": ColumnDefinition(type=ColumnType.INTEGER),
            "default_score": ColumnDefinition(
                type=ColumnType.REAL,
                not_null=True,
                default=0.0,
            ),
            "control_state": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
            ),
        },
    )
    current_columns = dict(declared_table.columns)
    for name in _RELAXED_COLUMNS:
        current_columns[name] = dataclasses.replace(
            current_columns[name],
            not_null=True,
        )
    current_table = dataclasses.replace(declared_table, columns=current_columns)
    return (
        SchemaDefinition(
            namespace=_FIXTURE_NAMESPACE,
            tables={_FIXTURE_TABLE: current_table},
        ),
        SchemaDefinition(
            namespace=_FIXTURE_NAMESPACE,
            tables={_FIXTURE_TABLE: declared_table},
        ),
    )


def _check_fixture_controls(declared: SchemaDefinition, current: SchemaDefinition) -> None:
    declared_table = declared.tables[_FIXTURE_TABLE]
    _check(
        all(not declared_table.columns[name].not_null for name in _RELAXED_COLUMNS),
        "the three fixture TTL columns are declared nullable",
    )
    _check(
        declared_table.columns["default_score"].default == 0.0 and declared_table.columns["default_score"].not_null and declared_table.columns["control_state"].not_null,
        "defaulted and required control columns retain their declared shape",
    )
    _check(
        current.tables[_FIXTURE_TABLE].columns["default_score"] == declared_table.columns["default_score"] and current.tables[_FIXTURE_TABLE].columns["control_state"] == declared_table.columns["control_state"],
        "defaulted and required control columns are unaffected by the diff",
    )


def _check_relaxation_operations(
    current: SchemaDefinition,
    declared: SchemaDefinition,
) -> None:
    declared_table = declared.tables[_FIXTURE_TABLE]
    _check(
        all(
            _is_nullability_relaxation_only(
                current.tables[_FIXTURE_TABLE].columns[name],
                declared_table.columns[name],
            )
            for name in _RELAXED_COLUMNS
        ),
        "all three changes are single-axis nullability relaxations",
    )
    operations = diff_schema(
        namespace=_FIXTURE_NAMESPACE,
        current=current,
        declared=declared,
        mode="update",
        schema_name="example",
        current_index_physical_names={},
    )
    rendered = [operation.as_string() for operation in operations]
    _check(len(rendered) == 3, f"exactly three typed DDL operations emitted; got {rendered!r}")
    _check(
        all("DROP NOT NULL" in operation for operation in rendered) and all(any(f'"{name}"' in operation for operation in rendered) for name in _RELAXED_COLUMNS),
        "typed schema diff emits DROP NOT NULL for exactly the three nullable columns",
    )


def _check_additive_preflight(
    current: SchemaDefinition,
    declared: SchemaDefinition,
) -> None:
    if not _PREFLIGHT_SRC.is_dir():
        print("  SKIP  macos_self_deployment_plugin: not in this profile's resolved plugin set")
        return
    from macos_self_deployment_plugin.schema_preflight import (
        classify_snapshot_diff,
        schemas_to_snapshot,
    )

    old_snapshot = schemas_to_snapshot({_FIXTURE_NAMESPACE: current})
    new_snapshot = schemas_to_snapshot({_FIXTURE_NAMESPACE: declared})
    verdict = classify_snapshot_diff(old_snapshot, new_snapshot)
    _check(
        verdict.is_additive and not verdict.breaking_changes,
        "blue-green schema preflight classifies the exact relaxation as additive",
    )


def test_exact_f1_relaxation() -> None:
    current, declared = _schemas()
    _check_fixture_controls(declared, current)
    _check_relaxation_operations(current, declared)
    _check_additive_preflight(current, declared)


def test_tightening_and_compound_changes_still_refuse() -> None:
    current, declared = _schemas()
    nullable = declared.tables[_FIXTURE_TABLE].columns["ttl_seconds"]
    required = current.tables[_FIXTURE_TABLE].columns["ttl_seconds"]
    _check(
        not _is_nullability_relaxation_only(nullable, required),
        "nullable to NOT NULL is not misclassified as a relaxation",
    )
    compound = dataclasses.replace(nullable, type=required.type, unique=True)
    _check(
        not _is_nullability_relaxation_only(required, compound),
        "nullability plus another shape change remains outside the relaxation path",
    )


def main() -> int:
    test_exact_f1_relaxation()
    test_tightening_and_compound_changes_still_refuse()
    print(f"\nschema diff nullability relaxation smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
