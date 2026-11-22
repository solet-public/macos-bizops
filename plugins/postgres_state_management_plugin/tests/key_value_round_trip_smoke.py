#!/usr/bin/env python3
"""The Postgres key-value store returns what set_key_value stored (r74 K2 a2, chg_90ddc12e).

``kv_set`` stores every value as its JSON text through the shared codec
(``ananta.services.key_value_operations.value_codec``); ``kv_get`` and
``kv_list`` decode it, so a read returns the value as written (object, list,
string, number, boolean, None) and text that is not JSON (rows written before
the codec existed) is returned as that string.

Two parts, the case table in ``ananta/tests/services/state_service/
_key_value_round_trip_cases.py`` run on each:

* ``check_with_dict_provider`` needs no database: the real ``kv_set``/``kv_get``/
  ``kv_list`` over a small in-file provider stub backed by a dict.
* ``check_real_postgres`` runs the same table against the real
  ``PostgresStatePlugin`` and against ``StateService`` (the object callers use)
  over a private PostgreSQL, then reads a legacy raw-text row. A missing fixture
  exits 77 (UNMEASURED), never a pass::

    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/key_value_round_trip_smoke.py
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
for _source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/postgres_state_management_plugin/src", ROOT / "ananta/tests/services/state_service"):
    sys.path.insert(0, str(_source))

from _key_value_round_trip_cases import run_cases  # noqa: E402
from postgres_state_management_plugin.key_value_ops import kv_get, kv_list, kv_set  # noqa: E402

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


class DictProvider:
    """The three provider calls the key-value operations make, over a dict."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str], dict[str, object]] = {}
        self._next = 0

    def generate_id(self, _table: str) -> str:
        self._next += 1
        return f"kv_{self._next}"

    def upsert(self, namespace: str, table: str, data: dict[str, object], conflict_columns: list[str]) -> str:
        assert (namespace, table) == ("core", "key_value_store")
        identity = cast(tuple[str, str, str], tuple(str(data[column]) for column in conflict_columns))
        self.rows[identity] = dict(data)
        return str(data["id"])

    def select(self, namespace: str, table: str, conditions: dict[str, object] | None = None, limit: int | None = None) -> list[dict[str, object]]:
        assert (namespace, table) == ("core", "key_value_store")
        found = [dict(row) for row in self.rows.values() if all(row.get(name) == wanted for name, wanted in (conditions or {}).items())]
        return found[:limit] if limit else found


class ProviderStore:
    """The three-method store shape the case table drives, over the key-value operations."""

    def __init__(self, provider: DictProvider) -> None:
        self._provider = cast(Any, provider)

    def set_key_value(self, namespace: str, key: str, value: object, scope: str = "GLOBAL", ttl: int | None = None) -> Mapping[str, object]:
        return kv_set(self._provider, namespace, key, value, scope)

    def get_key_value(self, namespace: str, key: str, scope: str = "GLOBAL") -> Mapping[str, object]:
        return kv_get(self._provider, namespace, key, scope)

    def list_key_values(self, namespace: str | None = None, scope: str | None = None, pattern: str | None = None) -> Mapping[str, object]:
        return kv_list(self._provider, namespace, scope)


def check_with_dict_provider() -> None:
    provider = DictProvider()
    run_cases(cast(Any, ProviderStore(provider)), lambda condition, label: _check(condition, f"dict provider: {label}"), "kv_round_trip")
    _check(all(isinstance(row["value"], str) for row in provider.rows.values()), "dict provider: the stored column is always text")
    provider.rows[("kv_round_trip", "legacy", "GLOBAL")] = {"id": "kv_legacy", "namespace": "kv_round_trip", "key": "legacy", "value": "ok", "scope": "GLOBAL"}
    legacy = cast(dict[str, Any], kv_get(cast(Any, provider), "kv_round_trip", "legacy"))
    _check(legacy["data"]["value"] == "ok" and legacy["data"]["found"] is True, "dict provider: raw legacy text reads back as that string")


def check_real_postgres() -> int:
    from ananta.config.core_schemas import CoreSchemaDefinitions
    from ananta.services.schema_manager import SchemaManager
    from postgres_state_management_plugin.plugin import PostgresStatePlugin

    from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture, skip_fixture

    try:
        with ledger_fixture("fast_return_census", schema="key_value_round_trip") as pg:
            plugin = cast(PostgresStatePlugin, pg.plugin)
            SchemaManager(pg.state, pg.plugin).initialize_schemas([CoreSchemaDefinitions.get_key_value_store_schema()])
            run_cases(cast(Any, plugin), lambda condition, label: _check(condition, f"real postgres plugin: {label}"), "kv_round_trip_plugin")
            run_cases(cast(Any, pg.state), lambda condition, label: _check(condition, f"real postgres StateService: {label}"), "kv_round_trip_state")

            provider = plugin._get_provider()  # pyright: ignore[reportPrivateUsage]
            provider.upsert(
                namespace="core",
                table="key_value_store",
                data={"id": provider.generate_id("core__key_value_store"), "namespace": "kv_round_trip_legacy", "key": "legacy", "value": "ok", "scope": "GLOBAL"},
                conflict_columns=["namespace", "key", "scope"],
            )
            for label, store in (("plugin", cast(Any, plugin)), ("StateService", cast(Any, pg.state))):
                got = cast(dict[str, Any], store.get_key_value("kv_round_trip_legacy", "legacy"))
                _check(got["data"]["found"] is True and got["data"]["value"] == "ok", f"real postgres {label}: the raw legacy text 'ok' reads back 'ok' through get")
                listed = cast(dict[str, Any], store.list_key_values("kv_round_trip_legacy"))
                _check([row["value"] for row in listed["data"]["values"]] == ["ok"], f"real postgres {label}: the raw legacy text 'ok' reads back 'ok' through list")
    except MissingFixtureError:
        return skip_fixture()
    return 0


def main() -> int:
    check_with_dict_provider()
    skipped = check_real_postgres() if not _failed else 0
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        return 1
    return skipped


if __name__ == "__main__":
    raise SystemExit(main())
