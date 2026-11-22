#!/usr/bin/env python3
"""A plugin schema declaration retires a table by name (r74 E2, chg_d5be421a).

``SchemaDefinition.retired_tables`` names tables the plugin no longer uses. When
a declaration lists a name that ownership records as active, the lifecycle drops
the table through the provider's drop chokepoint and deletes its ownership row in
the same transaction. Tables a declaration merely omits stay untouched: a
namespace is declared piecewise, so an omission is not a removal request.

Coverage, on this plugin's real lifecycle over a private PostgreSQL (the RDS
plugin's own smoke, ``rds_postgres_state_management_plugin/tests/
rds_retired_tables_smoke.py``, runs these same steps on its lifecycle copy):

* a declared table is created;
* a later declaration listing it in ``retired_tables`` drops the table and its
  ownership row, through the startup path (``install_plugin_schema``) and the
  ``update_plugin_schema`` path;
* a table the declaration omits without retiring it is left alone;
* a retired name with no ownership row is a no-op;
* a name both declared and retired is refused before anything is dropped.

The declaration round trip (JSON, standardizer) needs no database and runs first.

Run under the repository's private PostgreSQL provisioner
(``quality_gates/provision_test_pg.py --exec``); a missing fixture exits 77 (UNMEASURED), never a pass::

    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/retired_tables_smoke.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
for _source in (".", "ananta/src", "plugins/postgres_state_management_plugin/src"):
    sys.path.insert(0, str(ROOT / _source))

from ananta.services.plugin_schema_service.serialization import from_json, to_json  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema  # noqa: E402

from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402

NAMESPACE = "retired_tables_fixture"

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


def _declaration(*tables: str, retired: tuple[str, ...] = ()) -> SchemaDefinition:
    return SchemaDefinition(
        namespace=NAMESPACE,
        tables={name: TableSchema(table_name=name, id_prefix="rtf", columns={"label": ColumnDefinition(type=ColumnType.TEXT, not_null=True)}) for name in tables},
        retired_tables=retired,
    )


def check_declaration() -> None:
    """The field's own contract: default, JSON round trip, standardizer, refusal."""
    _check(_declaration("a").retired_tables == (), "retired_tables_defaults_to_empty")
    declared = _declaration("a", retired=("b", "c"))
    _check(to_json(declared)["retired_tables"] == ["b", "c"], "to_json_lists_retired_tables")
    _check(from_json(to_json(declared)).retired_tables == ("b", "c"), "json_round_trip_keeps_retired_tables")
    legacy = to_json(declared)
    del legacy["retired_tables"]
    _check(from_json(legacy).retired_tables == (), "payload_without_the_key_means_none_retired")
    _check(SchemaStandardizer().standardize_schema(declared).retired_tables == ("b", "c"), "standardizer_keeps_retired_tables")
    errors = _declaration("a", "b", retired=("b",)).validate()
    _check(any("retired" in error and "'b'" in error for error in errors), "declared_and_retired_name_fails_validate")
    _check(_declaration("a", retired=("b",)).validate() == [], "retiring_an_undeclared_name_validates")


class Backend:
    """One lifecycle copy over the private PostgreSQL, with its on-disk and ownership views."""

    def __init__(self, label: str, lifecycle: Any, provider: Any) -> None:
        self.label = label
        self.lifecycle = lifecycle
        self.provider = provider

    def check(self, condition: object, name: str) -> None:
        _check(condition, f"{self.label}: {name}")

    def on_disk(self, table: str) -> bool:
        return bool(self.provider.table_exists(NAMESPACE, table))

    def owned(self) -> dict[str, str]:
        return {name: row["status"] for name, row in self.lifecycle.get_installed_schema(NAMESPACE)["tables"].items()}

    def install(self, *tables: str, retired: tuple[str, ...] = ()) -> dict[str, Any]:
        return self.lifecycle.install_plugin_schema(NAMESPACE, to_json(_declaration(*tables, retired=retired)))

    def update(self, *tables: str, retired: tuple[str, ...] = ()) -> dict[str, Any]:
        return self.lifecycle.update_plugin_schema(NAMESPACE, to_json(_declaration(*tables, retired=retired)))


def check_startup_path(backend: Backend) -> None:
    """install_plugin_schema is what every start runs, so retirement must work there."""
    created = backend.install("a", "b", "c")
    backend.check(created["status"] == "installed" and all(backend.on_disk(t) for t in "abc"), "declared_tables_are_created")
    backend.check(backend.owned() == {"a": "active", "b": "active", "c": "active"}, "ownership_records_all_three_active")

    retired = backend.install("a", retired=("b",))
    backend.check(retired.get("retired_tables") == ["b"], "install_result_names_the_retired_table")
    backend.check(not backend.on_disk("b"), "install_drops_the_retired_table")
    backend.check("b" not in backend.owned(), "install_deletes_the_ownership_row")
    backend.check(backend.on_disk("c") and backend.owned() == {"a": "active", "c": "active"}, "omitted_unretired_table_is_left_alone")

    # The declaration stays in the plugin forever: repeating it is a clean no-op.
    again = backend.install("a", retired=("b",))
    backend.check("retired_tables" not in again and backend.on_disk("a") and backend.on_disk("c"), "retired_name_without_ownership_row_is_a_noop")


def check_update_path(backend: Backend) -> None:
    backend.update("a", "d")
    backend.check(backend.on_disk("d"), "update_adds_the_new_table")
    dropped = backend.update("a", retired=("d", "never_owned"))
    backend.check(dropped.get("retired_tables") == ["d"], "update_result_names_only_the_dropped_table")
    backend.check(not backend.on_disk("d") and "d" not in backend.owned(), "update_drops_the_retired_table_and_its_row")
    backend.check(backend.on_disk("c") and backend.owned() == {"a": "active", "c": "active"}, "update_leaves_other_tables_alone")


def check_refusal(backend: Backend) -> None:
    """A name both declared and retired refuses before anything is dropped."""
    refusal = ""
    try:
        backend.update("a", retired=("a", "c"))
    except ValueError as error:
        refusal = str(error)
    backend.check("retired" in refusal and "'a'" in refusal, "declared_and_retired_name_is_refused_by_name")
    backend.check(backend.on_disk("a") and backend.on_disk("c") and backend.owned() == {"a": "active", "c": "active"}, "refusal_drops_nothing")


def run_backend(label: str, lifecycle_class: Any) -> None:
    """Run every backend step on one lifecycle copy over the private PostgreSQL."""
    if not os.environ.get(MANIFEST_ENV):
        print(f"UNMEASURED: {MANIFEST_ENV} absent; run quality_gates/provision_test_pg.py --exec")
        raise SystemExit(skip_fixture())
    with ledger_fixture("ledger_ingest", schema="retired_tables_" + uuid4().hex[:12]) as fixture:
        provider = cast(Any, fixture.plugin)._get_provider()  # pyright: ignore[reportPrivateUsage]
        backend = Backend(label, lifecycle_class(provider), provider)
        for step in (check_startup_path, check_update_path, check_refusal):
            step(backend)


def report() -> int:
    print(f"\n{_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


def main() -> int:
    from postgres_state_management_plugin.postgres_backend.lifecycle import PluginSchemaLifecycle

    check_declaration()
    if not _failed:
        run_backend("local", PluginSchemaLifecycle)
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
