#!/usr/bin/env python3
"""Operator cell policy lifecycle and selector regression smoke, offline."""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "ananta/src"))
sys.path.insert(0, str(_ROOT / "plugins/agent_messaging_plugin/src"))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NS  # noqa: E402

from agent_messaging_plugin import model_capability_store as store  # noqa: E402
from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin import model_cell_enablement as policy  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import Pin, TierSelectionError, select_tier  # noqa: E402
from agent_messaging_plugin.model_capability_reconcile import ReconcileOutcome  # noqa: E402
from agent_messaging_plugin.model_capability_store import CatalogError, selection_catalog  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MODEL_CAPABILITY_CELL as CELLS,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MODEL_CELL_POLICY as POLICY,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    get_model_capability_cell_schema,
    get_model_capability_observation_schema,
    get_model_capability_refresh_run_schema,
    get_model_cell_policy_schema,
)


class OperatorPlugin(AgentMessagingPlugin):
    fixture_state: StateManagementInterface

    def _get_state_service(self) -> StateManagementInterface:
        return self.fixture_state

    def _claimant_session_id(self, agent_instance_id: str) -> str:
        return {"agi-operator": "ases-operator", "agi-worker": "ases-worker"}.get(agent_instance_id, "")


def fixture() -> RealShapeState:
    state = RealShapeState()
    for table in (get_model_capability_cell_schema(), get_model_capability_observation_schema(), get_model_capability_refresh_run_schema(), get_model_cell_policy_schema()):
        state._enforced_columns[table.table_name] = frozenset(table.columns) | frozenset({"id", "name", "namespace", "created_at", "updated_at", "created_by", "updated_by", "is_deleted"})
    state.write_state(
        NS,
        {
            "table": "role_binding",
            "record": {
                "external_id": "role:Operator",
                "role": "Operator",
                "holder_kind": "session",
                "agent_instance_id": "agi-operator",
                "agent_session_id": "ases-operator",
                "holder_identity": {"agent_id": "codex", "session_label": "Operator"},
                "claim_epoch": 1,
            },
        },
    )
    return state


def cell(model: str = "cheap", *, runtime: str = "codex", enabled: bool = True) -> dict[str, Any]:
    stamp = datetime.now(UTC).isoformat()
    return {
        "provider": "openai" if runtime == "codex" else "anthropic",
        "runtime": runtime,
        "model": model,
        "effort": "medium",
        "capability_score": 50,
        "cost_per_task_usd": 1.0 if model == "cheap" else 2.0,
        "relative_cost_multiplier": 1.0,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": stamp,
        "accepted_at": stamp,
        "acceptance": "accepted",
        "agreeing_observation_ids": [],
        "disagreement_note": None,
        "staleness_window_hours": 168,
        "last_refresh_run_id": "mcr-fixture",
        "enabled": enabled,
    }


def typed(state: RealShapeState) -> StateManagementInterface:
    return cast(StateManagementInterface, state)


def model_cell(state: RealShapeState, model: str) -> dict[str, Any]:
    return next(row for row in state.rows(NS, CELLS) if row["model"] == model)


def change(state: RealShapeState, **params: Any) -> dict[str, Any]:
    return policy.set_model_cells_enabled(typed(state), {"selector": {}, "enabled": True, "reason": "fixture", "expected_updated_at": policy.read_policy(typed(state)).updated_at, **params}, actor="ases-operator")


def selected(state: RealShapeState, **params: Any) -> dict[str, Any]:
    return verbs.select_dispatch_tier(typed(state), {"required_score": 40, "billing_objective": "metered_usd", **params})


class CellEnablementSmoke(unittest.TestCase):
    def test_disabled_cheap_cell(self) -> None:
        now = datetime.now(UTC)
        rows = [{"runtime": "codex", "model": model, "effort": "medium", "capability_score": 50, "cost_per_task_usd": cost, "relative_cost_multiplier": cost, "measured_at": now.isoformat(), "acceptance": "accepted", "enabled": enabled} for model, cost, enabled in (("cheap", 1.0, False), ("permitted", 2.0, True))]
        result = select_tier(selection_catalog(rows), required_score=40, objective="metered_usd", now=now, max_age=timedelta(days=7))
        self.assertEqual(result.selected.model, "permitted", "DISABLED_CHEAP_CELL_NOT_SELECTED")

    def test_only_lifecycle_history(self) -> None:
        state = fixture()
        for model, runtime in (("cheap", "codex"), ("claude-sonnet-test", "claude_code"), ("unscored", "codex")):
            record = cell(model, runtime=runtime)
            if model == "unscored":
                record.update({"capability_score": None, "acceptance": "withdrawn"})
            state.write_state(NS, {"table": CELLS, "record": record})
        answer = change(state, selector={"model_family_prefix": "claude-sonnet"}, only=True)
        self.assertEqual([row["model"] for row in answer["cells"] if row["enabled"]], ["claude-sonnet-test"], "ONLY_SONNET_ONE_CALL")
        self.assertEqual(selected(state)["selected"]["model"], "claude-sonnet-test")
        self.assertTrue(answer["cells"][0]["enabled_provenance"])
        self.assertEqual(len(answer["rules"]), 2)
        change(state, selector={"model": "cheap"}, enabled=True)
        self.assertEqual(selected(state)["selected"]["model"], "cheap", "ONLY_REVERSAL")
        rule = change(state, selector={"model": "cheap"}, enabled=False)["rule_id"]
        before = policy.read_model_cell_policy(typed(state))
        change(state, action="correct", rule_id=rule, selector={"model": "cheap"}, enabled=True)
        change(state, action="retire", rule_id=rule)
        result = change(state, action="clear")
        self.assertTrue(all(row["enabled"] for row in result["cells"]), "CLEAR_ALL_DEFAULT_ON")
        self.assertEqual(result["rules"], [])
        self.assertGreater(len(result["history"]), len(before["history"]), "APPEND_ONLY_POLICY_HISTORY")
        self.assertIn("previous_rules", result["history"][0]["note"])
        history = store.read_cell_history(typed(state), runtime="codex", model="cheap", effort="medium", limit=100)
        self.assertTrue(any(row["metric"] == "enabled_change" for row in history["observations"]))

    def test_pin_and_receipt_replay(self) -> None:
        state = fixture()
        for model in ("cheap", "permitted"):
            state.write_state(NS, {"table": CELLS, "record": cell(model)})
        receipt = selected(state)["selection_receipt"]
        change(state, selector={"model": "cheap"}, enabled=False)
        catalog = selection_catalog(state.rows(NS, CELLS))
        try:
            select_tier(catalog, required_score=40, objective="metered_usd", now=datetime.now(UTC), max_age=timedelta(days=7), pin=Pin("codex", "cheap", "medium"))
        except TierSelectionError as exc:
            self.assertEqual(exc.code, "pin_cell_unavailable", "DISABLED_PIN_REFUSED")
        else:
            self.fail("DISABLED_PIN_REFUSED")
        with self.assertRaises(CatalogError, msg="DISABLED_RECEIPT_REPLAY_REFUSED"):
            verbs.verify_selection_receipt(typed(state), difficulty_score=40, selection_receipt=receipt, dispatch_kind=None, scope_tags=(), agent_runtime="codex", model="cheap", effort="medium")
        change(state, action="clear")
        self.assertEqual(selected(state)["selected"]["model"], "cheap", "REENABLE_RESTORES_ELIGIBILITY")

    def test_stale_and_rollback(self) -> None:
        state = fixture()
        state.write_state(NS, {"table": CELLS, "record": cell()})
        initial = change(state)
        premise = initial["updated_at"]
        change(state, selector={"model": "elsewhere"}, enabled=False)
        before = deepcopy(state._rows)
        try:
            change(state, expected_updated_at=premise, enabled=False)
        except CatalogError as exc:
            self.assertEqual(exc.code, "stale_policy_state", "STALE_POLICY_WRITES_NOTHING")
        else:
            self.fail("STALE_POLICY_WRITES_NOTHING")
        self.assertEqual(state._rows, before, "STALE_POLICY_WRITES_NOTHING")
        state.fail_next("write")  # the audit insert fails after the policy update
        with self.assertRaises(RuntimeError):
            change(state, selector={"model": "missing"}, only=True)
        self.assertEqual(state._rows, before, "ONLY_UPDATE_ROLLS_BACK")

    def test_future_and_refresh_preserve(self) -> None:
        state = fixture()
        change(state, selector={"model_family_prefix": "off-"}, enabled=False)
        policy.write_catalog_cell(typed(state), {key: value for key, value in cell("off-new").items() if key != "enabled"})
        policy.write_catalog_cell(typed(state), {key: value for key, value in cell("on-new").items() if key != "enabled"})
        self.assertFalse(model_cell(state, "off-new")["enabled"], "NEW_CELL_RESPECTS_OFF_RULE")
        self.assertTrue(model_cell(state, "on-new")["enabled"])
        outcome = ReconcileOutcome(("codex", "off-new", "medium"), "accepted", 55, 2.0, datetime.now(UTC).isoformat(), [], None)
        store._write_cell(typed(state), outcome, "mcr-refresh", staleness_window_hours=168, floor=1.0)
        self.assertFalse(model_cell(state, "off-new")["enabled"], "REFRESH_PRESERVES_DISABLED")
        verbs.record_model_capability_cell(typed(state), {"runtime": "codex", "model": "off-new", "effort": "medium", "capability_score": 55, "cost_per_task_usd": 2.0, "source_ref": "fixture"})
        self.assertFalse(state.rows(NS, CELLS)[0]["enabled"], "MANUAL_PRESERVES_DISABLED")

    def test_reseed_preserves(self) -> None:
        state = fixture()
        store.seed_catalog(typed(state), seed=store.load_seed_table())
        target = next(row for row in state.rows(NS, CELLS) if row["runtime"] == "codex" and row["acceptance"] == "pending_crosscheck")
        change(state, selector={"model": target["model"]}, enabled=False)
        store.seed_catalog(typed(state), seed=store.load_seed_table())
        self.assertFalse(next(row for row in state.rows(NS, CELLS) if row["id"] == target["id"])["enabled"], "RESEED_PRESERVES_DISABLED")

    def test_withdraw_reinstate_keeps_disabled(self) -> None:
        state = fixture()
        state.write_state(NS, {"table": CELLS, "record": cell()})
        change(state, enabled=False)
        params = {"runtime": "codex", "model": "cheap", "effort": "medium", "expected_acceptance": "accepted", "actor": "fixture", "reason": "fixture"}
        verbs.withdraw_model_capability_cell(typed(state), params)
        self.assertFalse(state.rows(NS, CELLS)[0]["enabled"], "WITHDRAW_PRESERVES_DISABLED")
        verbs.reinstate_model_capability_cell(typed(state), {**params, "expected_acceptance": "withdrawn"})
        self.assertFalse(state.rows(NS, CELLS)[0]["enabled"], "REINSTATE_PRESERVES_DISABLED")

    def test_new_cell_stale_policy_rolls_back(self) -> None:
        state = fixture()
        change(state)
        stale = policy.read_policy(typed(state))
        change(state, selector={"model_family_prefix": "off-"}, enabled=False)
        before = deepcopy(state._rows)
        with patch.object(policy, "read_policy", return_value=stale), self.assertRaises(CatalogError) as caught:
            policy.write_catalog_cell(typed(state), {key: value for key, value in cell("off-racing").items() if key != "enabled"})
        self.assertEqual(caught.exception.code, "stale_policy_state")
        self.assertEqual(state._rows, before, "NEW_CELL_STALE_POLICY_WRITES_NOTHING")

    def test_operator_wrapper(self) -> None:
        state = fixture()
        plugin = object.__new__(OperatorPlugin)
        plugin.fixture_state = typed(state)
        args = {"selector": {}, "enabled": True, "expected_updated_at": None, "reason": "fixture"}
        answer = plugin.set_model_cells_enabled(args, {"caller_attribution_instance_id": "agi-operator"})
        self.assertEqual(answer["action_status"], "completed", "OPERATOR_SET_ACCEPTED")
        args["expected_updated_at"] = policy.read_policy(typed(state)).updated_at
        before = deepcopy(state._rows)
        for context, spoof in (({}, {}), ({"caller_attribution_instance_id": "agi-worker"}, {}), ({"caller_attribution_instance_id": "agi-worker"}, {"actor": "ases-operator"}), ({"caller_attribution_instance_id": "agi-operator"}, {"actor": "ases-operator"})):
            result = plugin.set_model_cells_enabled({**args, **spoof}, context)
            self.assertEqual(result["action_status"], "failed", "SPOOFED_OPERATOR_REFUSED")
            self.assertEqual(state._rows, before, "SPOOFED_OPERATOR_REFUSED")

    def test_quota_telemetry_is_independent(self) -> None:
        state = fixture()
        sonnet = cell("claude-sonnet-5", runtime="claude_code")
        sonnet["cost_per_task_usd"] = 0.1
        for record in (sonnet, cell("permitted")):
            state.write_state(NS, {"table": CELLS, "record": record})
        now = datetime.now(UTC)
        verbs.record_pool_reading(typed(state), {"profile_id": "anthropic-max-20x-2026-08-22", "pool_id": "weekly-all-model", "consumed": 100, "as_of": now.isoformat(), "next_reset_at": (now + timedelta(hours=2)).isoformat(), "source": "fixture exhausted", "recorded_by": "fixture"})
        with patch.object(verbs, "read_pool_readings_view", side_effect=RuntimeError("unreadable quota")):
            result = selected(state)
        self.assertEqual(result["selected"]["model"], "claude-sonnet-5", "ENABLED_EXHAUSTED_CELL_SELECTABLE")
        self.assertNotIn("quota_exhausted", result["excluded"])
        self.assertNotIn("expired_allowance_readings", result)

    def test_policy_audit_does_not_suppress_refresh(self) -> None:
        state = fixture()
        stale = cell()
        stale["measured_at"] = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        state.write_state(NS, {"table": CELLS, "record": stale})
        change(state)
        answer = selected(state)
        self.assertIn("refresh_request", answer, "POLICY_AUDIT_DOES_NOT_SUPPRESS_REFRESH")

    def test_validation_and_pagination(self) -> None:
        state = fixture()
        for index in range(205):
            state.write_state(NS, {"table": CELLS, "record": cell(f"model-{index:04}")})
        change(state, selector={"provider": "openai", "runtime": "codex", "effort": "medium"}, enabled=False)
        self.assertTrue(all(not row["enabled"] for row in state.rows(NS, CELLS)), "ALL_CENSUS_PAGES_UPDATED")
        answer = change(state, selector={"model_family_prefix": "future"}, enabled=False)
        self.assertTrue(answer["warnings"], "ZERO_MATCH_RULE_RETAINED")
        for params in ({"enabled": "false"}, {"selector": {"effort": 1}}, {"only": "true"}):
            before = deepcopy(state._rows)
            with self.assertRaises(CatalogError):
                change(state, **params)
            self.assertEqual(state._rows, before)
        state.rows(NS, POLICY)[0]["rules"] = {"bad": "policy"}
        with self.assertRaises(CatalogError) as caught:
            policy.read_policy(typed(state))
        self.assertEqual(caught.exception.code, "policy_invalid")


if __name__ == "__main__":
    unittest.main()
