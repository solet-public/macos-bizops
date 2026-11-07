#!/usr/bin/env python3
"""Real plugin schema union, registry prefixes and insert standard-field contract."""

from __future__ import annotations

import importlib
import sys
import unittest
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import cast

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "ananta/src"), str(ROOT / "solet_setup_contracts/src"), *(str(p) for p in sorted((ROOT / "plugins").glob("*/src")))]

from ananta.core.plugins.capabilities import collect_schemas  # noqa: E402
from ananta.services import schema_manager, state_service  # noqa: E402
from ananta.types.schema_standardizer import StandardFieldDefinitions  # noqa: E402
from ananta.types.schema_types import SchemaDefinition  # noqa: E402

from quality_gates.schema_init_gate import _entry_points, _platform_schemas  # noqa: E402


class RecordingLifecycle:
    def install_plugin_schema(self, namespace: str, schema_payload: dict[str, object]) -> dict[str, object]:
        return {"status": "recorded"}


def real_schemas() -> list[SchemaDefinition]:
    definitions = cast(list[SchemaDefinition], _platform_schemas(1536))
    entries = _entry_points(ROOT)
    if not entries:
        raise AssertionError("no real plugin entry points")
    for entry in entries:
        module = importlib.import_module(entry.module)
        if not Path(str(module.__file__)).resolve().is_relative_to(ROOT):
            raise AssertionError(f"foreign plugin source: {module.__file__}")
        factory = cast(Callable[[], object], getattr(module, entry.attribute))
        plugin = factory()
        plugin.name = entry.plugin_name
        definitions.extend(collect_schemas({entry.plugin_name: plugin}))
    print(f"REAL_SCHEMA_INPUTS plugins={len(entries)} definitions={len(definitions)}", flush=True)
    return definitions


class AllPluginStandardFields(unittest.TestCase):
    def test_real_plugin_union_prefix_and_prepared_standard_fields(self) -> None:
        state = state_service.StateService()
        manager = schema_manager.SchemaManager(state, RecordingLifecycle())
        state.set_schema_manager(manager)
        definitions = real_schemas()
        expected: dict[str, set[str]] = defaultdict(set)
        declarations: dict[str, int] = defaultdict(int)
        for definition in definitions:
            expected[definition.namespace].update(definition.tables)
            declarations[definition.namespace] += 1
            manager.initialize_schemas([definition])
        self.assertTrue(any(n > 1 for n in declarations.values()), "real_same_namespace_declarations")
        tables = 0
        standard = StandardFieldDefinitions.get_standard_fields()
        for namespace, names in expected.items():
            registered = manager.get_schema(namespace)
            self.assertIsNotNone(registered, namespace)
            if registered is None:
                raise AssertionError(f"missing namespace {namespace}")
            self.assertEqual(set(registered.tables), names, f"same_namespace_union:{namespace}")
            for name, table in registered.tables.items():
                with self.subTest(namespace=namespace, table=name):
                    self.assertTrue(table.id_prefix, "declared_id_prefix")
                    self.assertEqual(state._get_table_id_prefix(namespace, name), table.id_prefix, "registry_id_prefix")
                    representative: dict[str, object] = {}
                    record = state.prepare_record(namespace, name, representative)
                    self.assertEqual(representative, {}, "input_unchanged")
                    self.assertTrue(str(record["id"]).startswith(str(table.id_prefix) + "-"), "generated_id_prefix")
                    for column in ("id", "namespace", "name", "created_by", "updated_by"):
                        self.assertIsNotNone(record.get(column), f"prepared_standard_field:{column}")
                    for column in standard:
                        if table.columns[column].not_null:
                            self.assertIsNotNone(record.get(column), f"not_null_standard:{column}")
                    explicit: dict[str, object] = {"id": "explicit-id", "name": "business-name", "created_by": "author", "updated_by": "updater"}
                    self.assertEqual(state.prepare_record(namespace, name, explicit)["id"], "explicit-id", "explicit_id_preserved")
                    self.assertEqual(state.prepare_record(namespace, name, explicit)["name"], "business-name", "explicit_name_preserved")
                    tables += 1
        print(f"REAL_SCHEMA_COVERAGE namespaces={len(expected)} tables={tables}", flush=True)


if __name__ == "__main__":
    for module in (schema_manager, state_service):
        print(f"module.__file__: {module.__file__}", flush=True)
    unittest.main()
