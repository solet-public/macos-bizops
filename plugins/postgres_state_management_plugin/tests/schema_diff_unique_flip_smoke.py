#!/usr/bin/env python3
"""Smokes for schema_diff column ``unique`` flip support (2026-07-17).

Extends the active-update column-mutation path (previously only the
additive-CHECK-enum expansion) to handle a column whose ONLY difference is
its ``unique`` flag — emitting a single ``ALTER TABLE ... DROP CONSTRAINT``
(True->False) or ``ALTER TABLE ... ADD CONSTRAINT ... UNIQUE`` (False->True)
on the inline-unique constraint name ``<full_table>_<col>_key``, instead of
raising ``NotImplementedError``.

Motivation: an inline column ``UNIQUE`` constraint on a mutable natural key
(e.g. address-book ``name``) needs to be dropped declaratively via a normal
cutover's schema reconciliation — not a raw ``execute_sql`` escape hatch.

Coverage:
* ``unique_drop`` — unique True->False yields exactly one DROP CONSTRAINT op
  on the Postgres-default ``<full_table>_<col>_key`` name.
* ``unique_add`` — unique False->True yields exactly one ADD CONSTRAINT ...
  UNIQUE op on that same name, naming the column.
* ``compound_refused`` — unique + another field (not_null) both differ still
  raises NotImplementedError (single-axis discipline preserved).
* ``diff_schema_e2e`` — the drop lands through the top-level diff_schema.

Per [[sandbox-mutating-smokes]] all fixtures are in-memory; schema_diff is
pure-functional (no DB / filesystem side effects).

Run:
    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/schema_diff_unique_flip_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)
from postgres_state_management_plugin.postgres_backend.schema_diff import (  # noqa: E402
    _diff_or_refuse_column_changes,
    _is_unique_only_change,
    diff_schema,
)

_NS = "ab_test"
_TABLE = "thing"
_CONSTRAINT = "ab_test__thing_name_key"  # <full_table>_<col>_key

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


def _table(unique: bool) -> TableSchema:
    return TableSchema(
        table_name=_TABLE,
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "name": ColumnDefinition(
                type=ColumnType.TEXT, not_null=True, unique=unique,
            ),
        },
    )


def _schema(unique: bool) -> SchemaDefinition:
    return SchemaDefinition(namespace=_NS, tables={_TABLE: _table(unique)})


def unique_drop() -> None:
    print("unique_drop:")
    _check(
        _is_unique_only_change(
            _table(True).columns["name"], _table(False).columns["name"]
        ),
        "_is_unique_only_change True->False classified as unique-only",
    )
    ops = _diff_or_refuse_column_changes(
        namespace=_NS, table_name=_TABLE,
        current_table=_table(True), declared_table=_table(False),
        schema_name="example",
    )
    _check(len(ops) == 1, f"exactly 1 op emitted (DROP); got {len(ops)}")
    if len(ops) == 1:
        s = ops[0].as_string()
        _check(
            "DROP CONSTRAINT" in s and _CONSTRAINT in s,
            f"op is DROP CONSTRAINT on the inline-unique name (got {s!r})",
        )


def unique_add() -> None:
    print("unique_add:")
    ops = _diff_or_refuse_column_changes(
        namespace=_NS, table_name=_TABLE,
        current_table=_table(False), declared_table=_table(True),
        schema_name="example",
    )
    _check(len(ops) == 1, f"exactly 1 op emitted (ADD); got {len(ops)}")
    if len(ops) == 1:
        s = ops[0].as_string()
        _check(
            "ADD CONSTRAINT" in s and "UNIQUE" in s and _CONSTRAINT in s and '"name"' in s,
            f"op is ADD CONSTRAINT ... UNIQUE (\"name\") (got {s!r})",
        )


def compound_refused() -> None:
    print("compound_refused:")
    current = TableSchema(
        table_name=_TABLE,
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "name": ColumnDefinition(
                type=ColumnType.TEXT, not_null=True, unique=True,
            ),
        },
    )
    declared = TableSchema(
        table_name=_TABLE,
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "name": ColumnDefinition(
                type=ColumnType.TEXT, not_null=False, unique=False,
            ),
        },
    )
    _check(
        not _is_unique_only_change(current.columns["name"], declared.columns["name"]),
        "unique + not_null compound is NOT classified as unique-only",
    )
    try:
        _diff_or_refuse_column_changes(
            namespace=_NS, table_name=_TABLE,
            current_table=current, declared_table=declared, schema_name="example",
        )
    except NotImplementedError:
        _check(True, "compound (unique + not_null) raises NotImplementedError")
    else:
        _check(False, "compound mutation should have raised NotImplementedError")


def diff_schema_e2e() -> None:
    print("diff_schema_e2e:")
    ops = diff_schema(
        namespace=_NS,
        current=_schema(True),
        declared=_schema(False),
        mode="update",
        schema_name="example",
        current_index_physical_names={},
    )
    strs = [op.as_string() for op in ops]
    _check(
        any("DROP CONSTRAINT" in s and _CONSTRAINT in s for s in strs),
        f"diff_schema e2e includes DROP CONSTRAINT on the unique name (got {len(ops)} ops)",
    )


def main() -> int:
    unique_drop()
    unique_add()
    compound_refused()
    diff_schema_e2e()
    print()
    print(f"  passed: {_passed}")
    print(f"  failed: {len(_failed)}")
    for label in _failed:
        print(f"    - {label}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
