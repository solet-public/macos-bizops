#!/usr/bin/env python3
"""A1 offline schema-registration and typed current/history read boundaries."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "ananta/src"))
sys.path.insert(0, str(_ROOT / "plugins/agent_messaging_plugin/src"))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.plugins.capabilities import collect_schemas  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.state_results import StateOperationError, require_completed  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402

from agent_messaging_plugin import model_catalog_policy as catalog  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CELL_POLICY,
    get_model_capability_cell_schema,
    get_session_lifecycle_schema_definition,
)


class RecordingLifecycle:
    def __init__(self) -> None:
        self.installs: list[dict[str, Any]] = []

    def install_plugin_schema(self, namespace: str, schema: dict[str, Any]) -> dict[str, str]:
        assert namespace
        self.installs.append(schema)
        return {"status": "recorded"}


def fixture() -> RealShapeState:
    state = RealShapeState()
    schema = SchemaStandardizer().standardize_schema(catalog.get_model_catalog_policy_schema_definition())
    for name, table in schema.tables.items():
        state._enforced_columns[name] = frozenset(table.columns)
    return state


def typed(state: RealShapeState) -> StateManagementInterface:
    return cast(StateManagementInterface, state)


def provenance(stable_id: str, revision: int, *, state: str = "active", action: str = "set") -> dict[str, Any]:
    return {
        "stable_id": stable_id,
        "revision_id": f"{stable_id}-r{revision}",
        "revision": revision,
        "expected_revision": revision - 1 if revision > 1 else None,
        "predecessor_revision_id": f"{stable_id}-r{revision - 1}" if revision > 1 else None,
        "actor": "agi-configuration-owner",
        "reason": "reviewed fixture configuration",
        "action": action,
        "recorded_at": "2026-10-29T07:00:00+00:00",
        "lifecycle_state": state,
    }


def put(state: RealShapeState, table: str, row: dict[str, Any]) -> None:
    require_completed(state.write_state(catalog.NS, {"table": table, "record": row}), "fixture write")


def source_row(revision: int, *, state: str = "active", action: str = "set") -> dict[str, Any]:
    return {
        **provenance("provider-registration", revision, state=state, action=action),
        "provider_id": "third-provider",
        "source_id": "provider-api",
        "implementation_id": "third-provider-adapter",
        "implementation_version": "2.0",
        "endpoint": "https://provider.example/models",
        "credential_ref": "credential:provider-api",
        "metric_units": {"input_price": "USD/token", "capability": "benchmark-points"},
        "priority": None,
        "aliases": {"provider-model-id": "harness-model-id"},
        "enabled": True,
    }


class CatalogPolicySmoke(unittest.TestCase):
    def test_dynamic_collection_initialization_and_legacy_preservation(self) -> None:
        lifecycle = RecordingLifecycle()
        schemas = collect_schemas({"agent_messaging": AgentMessagingPlugin()})
        independent = [schema for schema in schemas if catalog.TABLE_MODEL_CATALOG_POLICY in schema.tables]
        self.assertEqual(len(independent), 1)
        expected = {
            catalog.TABLE_MODEL_CATALOG_POLICY,
            catalog.TABLE_MODEL_CATALOG_POLICY_HISTORY,
            catalog.TABLE_MODEL_CATALOG_SOURCE,
            catalog.TABLE_MODEL_CATALOG_SOURCE_HISTORY,
        }
        expected |= {
            "model_catalog_run",
            "model_catalog_run_event",
            "model_catalog_run_result",
            "model_catalog_schedule",
            "model_catalog_schedule_history",
        }
        self.assertEqual(set(independent[0].tables), expected)
        original = get_session_lifecycle_schema_definition()
        self.assertEqual(original.version, "1.16.0", "LIFECYCLE_VERSION_BUMPED_FOR_NULLABLE_WINDOW")
        self.assertTrue({TABLE_MODEL_CAPABILITY_CELL, TABLE_MODEL_CELL_POLICY} <= original.tables.keys())
        cell_schema = get_model_capability_cell_schema()
        window = cell_schema.columns["staleness_window_hours"]
        self.assertFalse(window.not_null, "UNKNOWN_WINDOW_IS_REPRESENTABLE_AS_NULL")
        self.assertEqual(window.type.name, "INTEGER", "WINDOW_RETAINS_INTEGER_TYPE")
        self.assertIsNone(window.default, "UNKNOWN_WINDOW_HAS_NO_NUMERIC_DEFAULT")
        self.assertIn("unknown", window.description.lower(), "UNKNOWN_WINDOW_DESCRIPTION")
        self.assertIn("measured", window.description.lower(), "MEASURED_WINDOW_DESCRIPTION")
        self.assertTrue(all(not expected.intersection(schema.tables) for schema in schemas if schema is not independent[0]))
        manager = SchemaManager(cast(Any, object()), lifecycle)
        manager.initialize_schemas(schemas)
        registered = manager.get_schema(catalog.NS)
        self.assertIsNotNone(registered)
        assert registered is not None
        standard_fields = {"id", "external_id", "created_at", "updated_at", "is_deleted"}
        self.assertTrue(
            standard_fields <= registered.tables[TABLE_MODEL_CAPABILITY_CELL].columns.keys(),
            "CAPABILITY_CELL_HAS_STANDARD_FIELDS",
        )
        registered_window = registered.tables[TABLE_MODEL_CAPABILITY_CELL].columns["staleness_window_hours"]
        self.assertFalse(registered_window.not_null, "STANDARDIZED_WINDOW_REMAINS_NULLABLE")
        self.assertIsNone(registered_window.default, "STANDARDIZED_UNKNOWN_WINDOW_HAS_NO_DEFAULT")
        self.assertTrue(expected | original.tables.keys() <= registered.tables.keys())
        self.assertEqual(len(lifecycle.installs), len(schemas), "REAL_INITIALIZE_SCHEMAS_REACHES_LIFECYCLE")
        for name in expected:
            self.assertTrue({"id", "external_id", "created_at", "updated_at", "is_deleted"} <= registered.tables[name].columns.keys())
            self.assertFalse(
                {"created_at", "updated_at", "id"}.intersection(independent[0].tables[name].columns),
                "NO_PROTECTED_FIELD_REDECLARATION",
            )
        manager.initialize_schemas(schemas)
        self.assertEqual(len(lifecycle.installs), len(schemas), "INITIALIZATION_IS_IDEMPOTENT")

    def test_optional_policy_values_and_revision_page_frontier(self) -> None:
        state = fixture()
        self.assertIsNone(catalog.read_catalog_policy(typed(state), "catalog"))
        for revision, confidence in ((1, 0.95), (2, None), (3, 0.9)):
            row = {
                **provenance("catalog", revision, action="clear" if revision == 2 else "set"),
                "confidence_level": confidence,
            }
            put(state, catalog.TABLE_MODEL_CATALOG_POLICY_HISTORY, row)
            if revision == 3:
                put(state, catalog.TABLE_MODEL_CATALOG_POLICY, row)
        put(state, catalog.TABLE_MODEL_CATALOG_POLICY_HISTORY, provenance("another-policy", 1))
        current = catalog.read_catalog_policy(typed(state), "catalog")
        assert current is not None
        self.assertEqual(current.provenance.revision_id, "catalog-r3")
        self.assertEqual(current.confidence_level, 0.9)
        self.assertIsNone(current.dispatch_score_margin)
        self.assertIsNone(current.bootstrap_resamples)
        first = catalog.read_catalog_policy_history(typed(state), "catalog", limit=2)
        self.assertEqual([row.provenance.revision for row in first.records], [1, 2])
        self.assertIsNone(first.records[1].confidence_level, "CLEARED_VALUE_HAS_NO_DEFAULT")
        self.assertEqual(first.records[1].provenance.predecessor_revision_id, "catalog-r1")
        self.assertEqual(first.records[1].provenance.expected_revision, 1)
        self.assertIsNotNone(first.next_after)
        second = catalog.read_catalog_policy_history(typed(state), "catalog", limit=2, after=first.next_after)
        self.assertEqual([row.provenance.revision for row in second.records], [3])
        self.assertIsNone(second.next_after)
        schema = catalog.get_model_catalog_policy_schema_definition()
        columns = schema.tables[catalog.TABLE_MODEL_CATALOG_POLICY].columns
        optional = {name for name, column in columns.items() if not column.not_null}
        for name in optional - {"expected_revision", "predecessor_revision_id"}:
            self.assertIsNone(getattr(first.records[1], name))
            self.assertIsNone(columns[name].default)

    def test_source_identity_credentials_and_terminal_history(self) -> None:
        state = fixture()
        self.assertIsNone(catalog.read_catalog_source(typed(state), "provider-registration"))
        rows = [
            source_row(1),
            source_row(2, state="retired", action="retire"),
            source_row(3, action="reopen"),
        ]
        rows[1].update(endpoint=None, credential_ref=None)
        for row in rows:
            put(state, catalog.TABLE_MODEL_CATALOG_SOURCE_HISTORY, row)
        put(state, catalog.TABLE_MODEL_CATALOG_SOURCE, rows[-1])
        current = catalog.read_catalog_source(typed(state), "provider-registration")
        assert current is not None
        self.assertEqual(current.credential_ref, "credential:provider-api")
        self.assertEqual(current.implementation_version, "2.0")
        self.assertEqual(current.metric_units["input_price"], "USD/token")
        self.assertEqual(current.aliases, {"provider-model-id": "harness-model-id"})
        self.assertIsNone(current.priority)
        page = catalog.read_catalog_source_history(typed(state), "provider-registration", limit=3)
        self.assertIsNone(page.next_after)
        retired = page.records[1]
        self.assertEqual(retired.provenance.lifecycle_state, "retired")
        self.assertIsNone(retired.credential_ref)
        self.assertIsNone(retired.endpoint)
        self.assertEqual(page.records[2].provenance.predecessor_revision_id, retired.provenance.revision_id)
        for table in catalog.get_model_catalog_policy_schema_definition().tables.values():
            self.assertFalse({"credential_value", "api_key", "secret"}.intersection(table.columns))
        for name in (
            catalog.TABLE_MODEL_CATALOG_POLICY,
            catalog.TABLE_MODEL_CATALOG_POLICY_HISTORY,
            catalog.TABLE_MODEL_CATALOG_SOURCE,
            catalog.TABLE_MODEL_CATALOG_SOURCE_HISTORY,
        ):
            self.assertTrue(any(index.unique and index.columns == ["revision_id"] for index in catalog.get_model_catalog_policy_schema_definition().tables[name].indexes))

    def test_coordinator_identity_and_nullable_unknowns(self) -> None:
        schemas = catalog.get_model_catalog_policy_schema_definition().tables
        for name in (
            "model_catalog_run",
            "model_catalog_run_event",
            "model_catalog_run_result",
            "model_catalog_schedule",
            "model_catalog_schedule_history",
        ):
            table = schemas[name]
            for column in table.columns.values():
                if not column.not_null:
                    self.assertIsNone(column.default)
            self.assertFalse({"credential_value", "api_key", "secret"}.intersection(table.columns))
        run = catalog.CatalogRun(
            "generation-1",
            2,
            "failed",
            None,
            None,
            1,
            "mcr-1",
            None,
            "manual",
            "request-1",
            None,
            None,
            None,
            "result-1",
        )
        event = catalog.CatalogRunEvent("event-1", run.generation_id, 2, "recovery", "failed", None, None)
        result = catalog.CatalogRunResult("result-1", run.generation_id, "failed", None, None)
        schedule = catalog.CatalogSchedule("schedule-generation-1", 2, "rearmed", "sch-new", "sch-old", None, 1, None, None)
        self.assertEqual(event.generation_id, result.generation_id)
        self.assertEqual(run.result_id, result.result_id)
        self.assertEqual(len({run.generation_id, event.event_id, result.result_id, schedule.generation_id}), 4)
        self.assertEqual(schedule.replaced_schedule_id, "sch-old")
        self.assertIsNone(schedule.due_at)
        self.assertFalse(schemas["model_catalog_run"].columns["started_at"].not_null)
        self.assertFalse(schemas["model_catalog_schedule"].columns["due_at"].not_null)

    def test_unavailable_reads_are_distinct_from_absence(self) -> None:
        state = fixture()
        readers = (
            lambda: catalog.read_catalog_policy(typed(state), "missing"),
            lambda: catalog.read_catalog_source(typed(state), "missing"),
            lambda: catalog.read_catalog_policy_history(typed(state), "missing", limit=2),
            lambda: catalog.read_catalog_source_history(typed(state), "missing", limit=2),
        )
        for read in readers:
            state.fail_next("query")
            with self.assertRaises(StateOperationError):
                read()
        self.assertEqual(catalog.read_catalog_policy_history(typed(state), "missing", limit=2).records, ())
        self.assertEqual(catalog.read_catalog_source_history(typed(state), "missing", limit=2).records, ())


if __name__ == "__main__":
    unittest.main()
