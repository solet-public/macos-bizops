#!/usr/bin/env python3
"""A2 offline process-boundary lifecycle, revision claim and rollback proof."""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(_ROOT), str(Path(__file__).resolve().parent), str(_ROOT / "ananta/src"), str(_ROOT / "plugins/agent_messaging_plugin/src")]

from _real_state_fake import RealShapeState, _RealShapeTransaction  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_completed  # noqa: E402
from model_catalog_policy_smoke import fixture, source_row, typed  # noqa: E402
from model_cell_enablement_smoke import OperatorPlugin  # noqa: E402

from agent_messaging_plugin import model_catalog_policy as catalog  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

CONTEXT = {"caller_attribution_instance_id": "agi-operator"}
VERBS = ("set_model_catalog_policy", "set_model_catalog_source", "read_model_catalog_policy")


def setup() -> tuple[RealShapeState, OperatorPlugin]:
    state = fixture()
    require_completed(
        state.write_state(catalog.NS, {"table": "role_binding", "record": {"external_id": "role:Operator", "role": "Operator", "holder_kind": "session", "agent_instance_id": "agi-operator", "agent_session_id": "ases-operator", "holder_identity": {"agent_id": "codex", "session_label": "Operator"}, "claim_epoch": 1}}),
        "operator fixture",
    )
    plugin = object.__new__(OperatorPlugin)
    plugin.fixture_state = typed(state)
    return state, plugin


def invoke(plugin: OperatorPlugin, name: str, arguments: dict[str, Any], *, context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate through ActionFactory's real metadata contract, then execute it."""
    method = getattr(plugin, name)
    metadata = method._platform_process_metadata
    key = f"plugin::agent_messaging_plugin::{name}"
    actions: list[dict[str, Any]] = []
    factory = ActionFactory(
        process_registry={"processes": {key: {"parameters": {field: param.to_dict() for field, param in metadata.parameters.items()}}}},
        template_engine=SimpleNamespace(resolve_templates=lambda definition, _context: definition),
        state_service=SimpleNamespace(generate_unique_string=lambda **_kw: {"action_status": "completed", "data": {"random_string": "catalog-smoke"}}),
        action_event_recorder=SimpleNamespace(store_action_event=lambda action: actions.append(action) or "ae-catalog-smoke"),
    )
    factory.submit_action_definition({"process_key": key, "arguments": arguments, "flow_id": "flow-catalog-smoke", "result_processor_kind": None, "error_processor_kind": None})
    return method(actions[0]["parameters"], CONTEXT if context is None else context)


def data(answer: dict[str, Any]) -> dict[str, Any]:
    assert answer["action_status"] == "completed", answer
    return answer["data"]


def write(plugin: OperatorPlugin, kind: str, action: str, expected: int | None, **kwargs: Any) -> dict[str, Any]:
    return invoke(plugin, f"set_model_catalog_{kind}", {"stable_id": kind, "action": action, "expected_revision": expected, "reason": "explicit operator fixture", **kwargs})


def read(plugin: OperatorPlugin, kind: str, **kwargs: Any) -> dict[str, Any]:
    return data(invoke(plugin, "read_model_catalog_policy", {"kind": kind, "stable_id": kind, **kwargs}, context={}))


def source_values() -> dict[str, Any]:
    row = source_row(1)
    return {name: row[name] for name in catalog.CatalogSource.__dataclass_fields__ if name != "provenance" and row.get(name) is not None}


class CatalogVerbsSmoke(unittest.TestCase):
    def test_t1_operator_process_lifecycle_and_paginated_history(self) -> None:
        state, plugin = setup()
        definitions = plugin.get_edge_process_definitions()
        self.assertTrue(set(VERBS) <= definitions.keys(), "DYNAMIC_EDGE_REGISTRATION")
        for name in VERBS:
            metadata = getattr(AgentMessagingPlugin, name)._platform_process_metadata
            self.assertEqual(metadata.name, name)
            self.assertNotIn("actor", metadata.parameters)
        for kind, values, field in (("policy", {"confidence_level": 0.95, "interval_method": "bootstrap"}, "confidence_level"), ("source", source_values(), "credential_ref")):
            absent = read(plugin, kind, history_limit=2)
            self.assertIsNone(absent["current"])
            self.assertEqual(absent["history"], [])
            self.assertTrue(absent["warnings"])
            first = data(write(plugin, kind, "set", None, values=values))["current"]
            changed = 0.9 if kind == "policy" else "credential:replacement"
            data(write(plugin, kind, "correct", 1, values={field: changed}))
            data(write(plugin, kind, "clear", 2, clear_fields=[field]))
            retired = data(write(plugin, kind, "retire", 3))["current"]
            self.assertEqual(retired["provenance"]["lifecycle_state"], "retired")
            self.assertIsNone(read(plugin, kind)["current"][field])
            data(write(plugin, kind, "reopen", 4))
            page = read(plugin, kind, history_limit=2)
            records = list(page["history"])
            while page["next_after"] is not None:
                page = read(plugin, kind, history_limit=2, after=page["next_after"])
                records.extend(page["history"])
            self.assertEqual([row["provenance"]["action"] for row in records], ["set", "correct", "clear", "retire", "reopen"])
            self.assertEqual(records[0], first, "PRIOR_SNAPSHOT_UNCHANGED")
            for index, row in enumerate(records):
                self.assertEqual(row["provenance"]["actor"], "ases-operator")
                self.assertEqual(row["provenance"]["revision"], index + 1)
                self.assertEqual(row["provenance"]["expected_revision"], index or None)
                self.assertEqual(row["provenance"]["predecessor_revision_id"], records[index - 1]["provenance"]["revision_id"] if index else None)
            self.assertIsNone(records[2][field], "CLEAR_STAYS_NULL")
            self.assertEqual(read(plugin, kind)["current"], records[-1])
        self.assertFalse(any(table == "model_cell_policy" for _, table in state._rows), "CELL_ENABLEMENT_UNTOUCHED")

    def test_t2_stale_spoofed_nonoperator_and_atomic_rollback(self) -> None:
        state, plugin = setup()
        for kind, values in (("policy", {}), ("source", source_values())):
            data(write(plugin, kind, "set", None, values=values))
            before = deepcopy(state._rows)
            for action, expected in (("set", None), ("correct", 9)):
                answer = write(plugin, kind, action, expected, values={"dispatch_score_margin": 2} if kind == "policy" else {"priority": 2})
                self.assertEqual(answer["action_status"], "failed", "STALE_REVISION_REFUSED")
                self.assertEqual(state._rows, before, "STALE_WRITES_NOTHING")
            arguments = {"stable_id": kind, "action": "retire", "expected_revision": 1, "reason": "fixture"}
            method = getattr(plugin, f"set_model_catalog_{kind}")
            for context, extra in (({}, {}), ({"caller_attribution_instance_id": "agi-worker"}, {}), (CONTEXT, {"actor": "ases-operator"}), (CONTEXT, {"agent_session_id": "ases-operator"})):
                self.assertEqual(method({**arguments, **extra}, context)["action_status"], "failed", "SPOOFED_OR_NONOPERATOR_REFUSED")
                self.assertEqual(state._rows, before)
            state.fail_next("write")  # history insertion follows the projection CAS
            with self.assertRaises(RuntimeError):
                write(plugin, kind, "retire", 1)
            self.assertEqual(state._rows, before, "HISTORY_FAILURE_ROLLS_BACK_PROJECTION")
            # Simulate a competing writer winning after our transactional read.
            with patch.object(_RealShapeTransaction, "update_state", return_value=0):
                self.assertEqual(write(plugin, kind, "retire", 1)["action_status"], "failed", "CAS_LOSER_REFUSED")
            self.assertEqual(state._rows, before, "CAS_LOSER_HAS_NO_HISTORY")
            data(write(plugin, kind, "retire", 1))
            self.assertEqual(read(plugin, kind)["current"]["provenance"]["revision"], 2, "CURRENT_OPERATOR_AFTER_REFUSAL_PASSES")

    def test_t3_every_optional_field_clear_and_reversal(self) -> None:
        state, plugin = setup()
        values = {
            "dispatch_score_margin": 2.0,
            "cost_tie_tolerance": 0.03,
            "confidence_level": 0.95,
            "interval_method": "bootstrap",
            "interval_method_revision": "v1",
            "bootstrap_resamples": 500,
            "outcome_window_hours": 24.0,
            "staleness_window_hours": 48.0,
            "refresh_interval_seconds": 60.0,
            "refresh_dedupe_seconds": 5.0,
        }
        self.assertEqual(set(values), set(catalog.CatalogPolicy.__dataclass_fields__) - {"provenance"})
        data(write(plugin, "policy", "set", None, values=values))
        fields = list(values)
        data(write(plugin, "policy", "clear", 1, clear_fields=fields))
        self.assertTrue(all(read(plugin, "policy")["current"][field] is None for field in fields))
        data(write(plugin, "policy", "retire", 2))
        data(write(plugin, "policy", "reopen", 3))
        self.assertTrue(all(read(plugin, "policy")["current"][field] is None for field in fields))
        data(write(plugin, "policy", "correct", 4, values=values))
        data(write(plugin, "source", "set", None, values=source_values()))
        data(write(plugin, "source", "correct", 1, values={"endpoint": "https://third.example/catalog", "credential_ref": "credential:next", "priority": 2, "enabled": False, "aliases": {}, "metric_units": {}}))
        data(write(plugin, "source", "clear", 2, clear_fields=["endpoint", "credential_ref", "priority"]))
        current = read(plugin, "source")["current"]
        self.assertTrue(all(current[name] is None for name in ("endpoint", "credential_ref", "priority")))
        self.assertEqual(current["aliases"], {})
        self.assertEqual(current["metric_units"], {})
        self.assertFalse(current["enabled"])
        self.assertEqual(len(read(plugin, "source", history_limit=10)["history"]), 3)

    def test_invalid_typed_writes_and_terminal_transitions(self) -> None:
        state, plugin = setup()
        invalid = ({"confidence_level": True}, {"confidence_level": 1.0}, {"bootstrap_resamples": 1.5}, {"refresh_interval_seconds": 0}, {"dispatch_score_margin": float("nan")}, {"interval_method": " "}, {"dispatch_score_margin": None}, {"api_key": "secret"})
        for values in invalid:
            before = deepcopy(state._rows)
            self.assertEqual(write(plugin, "policy", "set", None, values=values)["action_status"], "failed")
            self.assertEqual(state._rows, before)
        for values in ({"enabled": "true"}, {"credential_value": "secret"}, {"endpoint": "https://user:secret@third.example"}, {"aliases": {"model": 5}}):
            self.assertEqual(write(plugin, "source", "set", None, values={**source_values(), **values})["action_status"], "failed")
        data(write(plugin, "policy", "set", None))
        for action, kwargs in (("reopen", {}), ("clear", {"clear_fields": ["stable_id"]}), ("retire", {"values": {"cost_tie_tolerance": 0.1}})):
            before = deepcopy(state._rows)
            self.assertEqual(write(plugin, "policy", action, 1, **kwargs)["action_status"], "failed")
            self.assertEqual(state._rows, before)
        data(write(plugin, "policy", "retire", 1))
        self.assertEqual(write(plugin, "policy", "retire", 2)["action_status"], "failed")
        data(write(plugin, "policy", "reopen", 2))


if __name__ == "__main__":
    unittest.main()
