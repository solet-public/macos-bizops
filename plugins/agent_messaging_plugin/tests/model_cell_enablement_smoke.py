#!/usr/bin/env python3
"""Operator cell policy lifecycle and selector regression smoke, offline."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
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
from agent_messaging_plugin.dispatch_tier_selection import BillingObjective, CapabilityCell, Pin, TierSelectionError, select_tier  # noqa: E402
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
    return verbs.select_dispatch_tier(typed(state), {"required_score": 40, **params})


# Preference weight fixtures: the live shape at score 55, where Sonnet max and Opus xhigh both clear and Opus is cheaper.
_PLAN_NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)  # admits the newest shipped economics profile
_PLAN_ID = "anthropic-max-20x-2026-08-22"
_ECONOMICS = _ROOT / "plugins/agent_messaging_plugin/model_profiles/usage_economics.v1.json"
_OBJECTIVES = ("metered_usd", "relative", "allowance_weighted")
_SONNET = {"model_family_prefix": "claude-sonnet"}
_OLD_SELECTED_KEYS = {"runtime", "model", "effort", "capability_score", "cost_per_task_usd", "relative_cost_multiplier", "measured_at", "acceptance", "enabled"}


@contextmanager
def neutral_plan() -> Generator[None]:
    """The shipped flat-rate plan with every dispatch weight 1.0, so only the policy weight can order cells."""
    raw = json.loads(_ECONOMICS.read_text(encoding="utf-8"))
    plan = next(row for row in raw["profiles"] if row["profile_id"] == _PLAN_ID)
    plan["dispatch_weights"]["by_model_prefix"] = {"claude-sonnet": 1.0}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "usage_economics.v1.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with patch.object(verbs, "_USAGE_ECONOMICS_PROFILE_PATH", path):
            yield


def priced_cell(model: str, effort: str, *, score: float, usd: float) -> dict[str, Any]:
    stamp = _PLAN_NOW.isoformat()
    record = cell(model, runtime="claude_code")
    record.update({"effort": effort, "capability_score": score, "cost_per_task_usd": usd, "relative_cost_multiplier": round(usd * 100, 2), "measured_at": stamp, "accepted_at": stamp})
    return record


def sonnet_opus_state() -> RealShapeState:
    state = fixture()
    for record in (priced_cell("claude-sonnet-5-5", "xhigh", score=50, usd=4.09), priced_cell("claude-sonnet-5-5", "max", score=55, usd=10.02), priced_cell("claude-opus-5-5", "xhigh", score=57, usd=6.05)):
        state.write_state(NS, {"table": CELLS, "record": record})
    return state


def rule(state: RealShapeState, **params: Any) -> dict[str, Any]:
    """One operator verb call with no defaults, so a weight-only rule can omit enabled."""
    return policy.set_model_cells_enabled(typed(state), {"reason": "fixture", "expected_updated_at": policy.read_policy(typed(state)).updated_at, **params}, actor="ases-operator")


def pick(state: RealShapeState, objective: str = "metered_usd", **params: Any) -> dict[str, Any]:
    runtime = "claude_code" if objective == "allowance_weighted" else None
    if objective == "allowance_weighted":
        return verbs.select_dispatch_tier(typed(state), {"required_score": 55, "runtime": runtime, "max_staleness_hours": 72, **params}, now=_PLAN_NOW)
    raw = json.loads(_ECONOMICS.read_text(encoding="utf-8"))
    if objective == "metered_usd":
        raw["profiles"] = [profile for profile in raw["profiles"] if profile.get("kind") != "flat_rate_quota"]
    else:
        for profile in raw["profiles"]:
            if profile.get("kind") == "flat_rate_quota":
                profile.pop("dispatch_weights", None)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "usage_economics.v1.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with patch.object(verbs, "_USAGE_ECONOMICS_PROFILE_PATH", path):
            return verbs.select_dispatch_tier(typed(state), {"required_score": 55, "runtime": runtime, "max_staleness_hours": 72, **params}, now=_PLAN_NOW)


def read_cell(state: RealShapeState, model: str, effort: str) -> dict[str, Any]:
    return next(row for row in policy.read_model_cell_policy(typed(state))["cells"] if row["model"] == model and row["effort"] == effort)


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

    def test_pin_and_selection_follow_enablement(self) -> None:
        state = fixture()
        for model in ("cheap", "permitted"):
            state.write_state(NS, {"table": CELLS, "record": cell(model)})
        self.assertEqual(selected(state)["selected"]["model"], "cheap", "ENABLED_CELL_SELECTED")
        change(state, selector={"model": "cheap"}, enabled=False)
        self.assertNotEqual(selected(state)["selected"]["model"], "cheap", "DISABLED_CELL_NOT_SELECTED")
        catalog = selection_catalog(state.rows(NS, CELLS))
        try:
            select_tier(catalog, required_score=40, objective="metered_usd", now=datetime.now(UTC), max_age=timedelta(days=7), pin=Pin("codex", "cheap", "medium"))
        except TierSelectionError as exc:
            self.assertEqual(exc.code, "pin_cell_unavailable", "DISABLED_PIN_REFUSED")
        else:
            self.fail("DISABLED_PIN_REFUSED")
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
        args: dict[str, Any] = {"selector": {}, "enabled": True, "expected_updated_at": None, "reason": "fixture"}
        answer = plugin.set_model_cells_enabled(args, {"caller_attribution_instance_id": "agi-operator"})
        self.assertEqual(answer["action_status"], "completed", "OPERATOR_SET_ACCEPTED")
        args["expected_updated_at"] = policy.read_policy(typed(state)).updated_at
        before = deepcopy(state._rows)
        worker = {"caller_attribution_instance_id": "agi-worker"}  # derived identity of a session that does not hold Operator
        # Refused: no derived identity, and every attempt to supply an actor. Holding Operator is not a condition.
        for context, spoof in (({}, {}), (worker, {"actor": "ases-operator"}), (worker, {"agent_instance_id": "agi-operator"}), ({"caller_attribution_instance_id": "agi-operator"}, {"actor": "ases-operator"})):
            result = plugin.set_model_cells_enabled({**args, **spoof}, context)
            self.assertEqual(result["action_status"], "failed", "UNIDENTIFIED_OR_SPOOFED_REFUSED")
            self.assertEqual(state._rows, before, "UNIDENTIFIED_OR_SPOOFED_REFUSED")
        # A session that does not hold Operator is held to the same stale-premise refusal and writes nothing.
        stale = plugin.set_model_cells_enabled({**args, "expected_updated_at": "2000-01-01T00:00:00+00:00"}, worker)
        self.assertEqual(stale["action_status"], "failed", "NONOPERATOR_STALE_REFUSED")
        self.assertEqual(state._rows, before, "NONOPERATOR_STALE_WRITES_NOTHING")
        # Pass path: that session may set the value; the immutable history names it and its reason.
        applied = plugin.set_model_cells_enabled({**args, "selector": {"model_family_prefix": "claude-sonnet"}, "enabled": False, "reason": "nonoperator session applying the user's order"}, worker)
        self.assertEqual(applied["action_status"], "completed", "NONOPERATOR_SET_ACCEPTED")
        snapshot = json.loads(applied["data"]["history"][0]["note"])
        self.assertEqual((snapshot["actor"], snapshot["reason"]), ("ases-worker", "nonoperator session applying the user's order"), "HISTORY_NAMES_THE_NONOPERATOR_SESSION")
        self.assertEqual(json.loads(applied["data"]["history"][1]["note"])["actor"], "ases-operator", "EARLIER_WRITER_STAYS_IN_HISTORY")

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


class PreferenceWeightSmoke(unittest.TestCase):
    """Operator preference weight on cell policy rules: ordering, reporting, lifecycle and compatibility, offline."""

    def test_weight_orders_selection_under_every_objective(self) -> None:
        unweighted, weighted = sonnet_opus_state(), sonnet_opus_state()
        change(weighted, selector=_SONNET, weight=0.5)
        with neutral_plan():
            for objective in _OBJECTIVES:
                with self.subTest(objective=objective):
                    self.assertEqual(pick(unweighted, objective)["selected"]["model"], "claude-opus-5-5", f"NO_WEIGHT_{objective}_PICKS_THE_CHEAPER_OPUS")
                    self.assertEqual(pick(weighted, objective)["selected"]["model"], "claude-sonnet-5-5", f"WEIGHT_{objective}_PICKS_THE_PREFERRED_SONNET")
            self.assertEqual(pick(weighted, "metered_usd", runtime="claude_code")["selected"]["model"], "claude-sonnet-5-5", "WEIGHT_APPLIES_UNDER_A_RUNTIME_CONSTRAINT")
        self.assertEqual(pick(weighted, required_score=57)["selected"]["model"], "claude-opus-5-5", "WEIGHT_NEVER_LIFTS_A_CELL_OVER_THE_BAR")

    def test_weight_orders_but_never_changes_reported_cost(self) -> None:
        state = sonnet_opus_state()
        rule_id = change(state, selector=_SONNET, weight=0.5)["rule_id"]
        answer = pick(state)
        self.assertEqual(answer["selected"]["model"], "claude-sonnet-5-5")
        catalog_relative = read_cell(sonnet_opus_state(), "claude-sonnet-5-5", "max")["relative_cost_multiplier"]
        self.assertEqual(answer["selected"]["cost_per_task_usd"], 10.02, "REPORTED_USD_IS_THE_CATALOG_PRICE")
        self.assertEqual(answer["selected"]["relative_cost_multiplier"], catalog_relative, "REPORTED_RELATIVE_IS_THE_CATALOG_VALUE")
        self.assertEqual(answer["preference"], {"weight": 0.5, "rule_id": rule_id}, "APPLIED_WEIGHT_AND_RULE_REPORTED")
        self.assertAlmostEqual(answer["ladder"][0]["cost_delta"], 10.02 - 4.09, msg="LADDER_COST_DELTA_IS_UNWEIGHTED")
        self.assertEqual([row["model"] for row in answer["frontier"]], ["claude-opus-5-5"], "FRONTIER_USES_TRUE_COST")
        self.assertEqual([(row["weaker"]["model"], row["stronger"]["model"]) for row in answer["dominated"]], [("claude-sonnet-5-5", "claude-opus-5-5")], "DOMINATION_USES_TRUE_COST")
        pinned = pick(state, pin={"runtime": "claude_code", "model": "claude-sonnet-5-5", "effort": "max"}, pin_basis="fixture")
        self.assertAlmostEqual(pinned["pin_cost_delta"], 10.02 - 6.05, msg="PIN_COST_DELTA_IS_UNWEIGHTED")

    def test_ordering_cost_scales_and_cost_does_not(self) -> None:
        weighted = CapabilityCell("claude_code", "claude-sonnet-5-5", "max", 55, 10.02, 1002.0, _PLAN_NOW, "accepted", plan_weight=0.5, preference_weight=0.5, preference_rule="rule-x")
        true_costs: tuple[tuple[BillingObjective, float], ...] = (("metered_usd", 10.02), ("relative", 1002.0), ("allowance_weighted", 501.0))
        for objective, true_cost in true_costs:
            self.assertAlmostEqual(weighted.cost(objective) or 0.0, true_cost, msg=f"COST_{objective}_IS_THE_TRUE_COST")
            self.assertAlmostEqual(weighted.ordering_cost(objective) or 0.0, true_cost * 0.5, msg=f"ORDERING_COST_{objective}_IS_WEIGHTED")
        unpriced = CapabilityCell("claude_code", "claude-sonnet-5-5", "max", 55, None, None, _PLAN_NOW, "accepted", preference_weight=0.5)
        self.assertIsNone(unpriced.ordering_cost("metered_usd"), "UNPRICED_STAYS_UNPRICED")

    def test_weight_lifecycle_set_read_correct_clear(self) -> None:
        state = sonnet_opus_state()
        created = rule(state, selector=_SONNET, weight=0.5)
        rule_id = created["rule_id"]
        self.assertEqual(created["rules"], [{"id": rule_id, "selector": _SONNET, "weight": 0.5, "kind": None, "words": "fixture", "ends_at": None, "lapsed": False}], "WEIGHT_ONLY_RULE_STORES_NO_ENABLED_KEY")
        self.assertEqual(json.loads(created["history"][0]["note"])["next_rules"][0]["weight"], 0.5, "AUDIT_SNAPSHOT_KEEPS_THE_WEIGHT")
        sonnet, opus = read_cell(state, "claude-sonnet-5-5", "max"), read_cell(state, "claude-opus-5-5", "xhigh")
        self.assertEqual((sonnet["preference_weight"], sonnet["preference_provenance"], sonnet["enabled"]), (0.5, rule_id, True), "READBACK_SHOWS_THE_APPLIED_WEIGHT")
        self.assertEqual((opus["preference_weight"], opus["preference_provenance"]), (1.0, "default_weight"), "READBACK_DEFAULT_WEIGHT")
        self.assertEqual(pick(state)["selected"]["model"], "claude-sonnet-5-5")
        corrected = rule(state, action="correct", rule_id=rule_id, selector=_SONNET, weight=0.9)
        self.assertEqual(corrected["rules"], [{"id": rule_id, "selector": _SONNET, "weight": 0.9, "kind": None, "words": "fixture", "ends_at": None, "lapsed": False}], "WEIGHT_CORRECTED_IN_PLACE")
        self.assertEqual(pick(state)["selected"]["model"], "claude-opus-5-5", "CORRECTED_WEIGHT_NO_LONGER_PREFERS_SONNET")
        before = deepcopy(state._rows)
        with self.assertRaises(CatalogError, msg="CORRECTION_KEEPING_NEITHER_ENABLED_NOR_WEIGHT_REFUSED") as caught:
            rule(state, action="correct", rule_id=rule_id, selector=_SONNET)
        self.assertEqual(caught.exception.code, "parameter_invalid")
        self.assertEqual(state._rows, before, "REFUSED_CORRECTION_WRITES_NOTHING")
        both = rule(state, action="correct", rule_id=rule_id, selector=_SONNET, enabled=True, weight=0.5)
        self.assertEqual(both["rules"], [{"id": rule_id, "selector": _SONNET, "enabled": True, "weight": 0.5, "kind": None, "words": "fixture", "ends_at": None, "lapsed": False}], "ENABLED_AND_WEIGHT_TOGETHER")
        cleared = rule(state, action="correct", rule_id=rule_id, selector=_SONNET, enabled=True)
        self.assertEqual(cleared["rules"], [{"id": rule_id, "selector": _SONNET, "enabled": True, "kind": None, "words": "fixture", "ends_at": None, "lapsed": False}], "CORRECT_WITHOUT_WEIGHT_CLEARS_IT")
        self.assertEqual(pick(state)["selected"]["model"], "claude-opus-5-5", "CLEARED_WEIGHT_RESTORES_COST_ORDER")
        lone = rule(state, selector=_SONNET, weight=0.5)["rule_id"]
        self.assertEqual(pick(state)["selected"]["model"], "claude-sonnet-5-5")
        retired = rule(state, action="retire", rule_id=lone)
        self.assertNotIn(lone, [row["id"] for row in retired["rules"]], "RETIRE_CLEARS_A_WEIGHT_ONLY_RULE")
        self.assertEqual(pick(state)["selected"]["model"], "claude-opus-5-5", "RETIRED_WEIGHT_RESTORES_COST_ORDER")
        premise = policy.read_policy(typed(state)).updated_at
        rule(state, selector=_SONNET, weight=0.5)
        before = deepcopy(state._rows)
        with self.assertRaises(CatalogError, msg="STALE_WEIGHT_WRITE_REFUSED") as caught:
            rule(state, selector=_SONNET, weight=0.4, expected_updated_at=premise)
        self.assertEqual(caught.exception.code, "stale_policy_state")
        self.assertEqual(state._rows, before, "STALE_WEIGHT_WRITES_NOTHING")

    def test_weight_never_enables_a_disabled_cell_and_later_rules_override(self) -> None:
        state = sonnet_opus_state()
        off = change(state, selector={"model_family_prefix": "claude-opus"}, enabled=False)["rule_id"]
        tiny = rule(state, selector={"model_family_prefix": "claude-opus"}, weight=0.01)["rule_id"]
        answer = pick(state)
        self.assertEqual(answer["selected"]["model"], "claude-sonnet-5-5", "DISABLED_CELL_STAYS_OFF_UNDER_ANY_WEIGHT")
        self.assertEqual(answer["excluded"]["operator_disabled"], 1)
        opus = read_cell(state, "claude-opus-5-5", "xhigh")
        self.assertEqual((opus["enabled"], opus["enabled_provenance"], opus["preference_weight"], opus["preference_provenance"]), (False, off, 0.01, tiny), "WEIGHT_RULE_LEAVES_ENABLEMENT_ALONE")
        state = sonnet_opus_state()
        broad = rule(state, selector=_SONNET, weight=0.5)["rule_id"]
        narrow = rule(state, selector={"model": "claude-sonnet-5-5", "effort": "max"}, weight=2.0)["rule_id"]
        change(state, selector=_SONNET, enabled=True)
        top, low = read_cell(state, "claude-sonnet-5-5", "max"), read_cell(state, "claude-sonnet-5-5", "xhigh")
        self.assertEqual((top["preference_weight"], top["preference_provenance"]), (2.0, narrow), "LATER_WEIGHT_RULE_OVERRIDES_EARLIER")
        self.assertEqual(policy.preference(policy.read_policy(typed(state)), top), (2.0, narrow), "PREFERENCE_RESOLVES_LAST_MATCHING_WEIGHT_RULE")
        self.assertEqual(policy.preference(policy.read_policy(typed(state)), {"model": "unmatched"}), (1.0, None), "PREFERENCE_DEFAULT_WITHOUT_A_MATCH")
        self.assertEqual((low["preference_weight"], low["preference_provenance"]), (0.5, broad), "EARLIER_WEIGHT_STANDS_WHERE_LATER_DOES_NOT_MATCH")
        self.assertEqual(pick(state)["selected"]["model"], "claude-opus-5-5", "WEIGHT_ABOVE_ONE_DISFAVOURS_A_CELL")

    def test_policy_without_weights_reads_and_selects_as_before(self) -> None:
        state = sonnet_opus_state()
        change(state)
        old = [{"id": "rule-old", "selector": _SONNET, "enabled": True}]
        state.rows(NS, POLICY)[0]["rules"] = deepcopy(old)
        self.assertEqual(policy.read_model_cell_policy(typed(state))["rules"], [{**old[0], "kind": None, "words": "no words were recorded for this rule", "ends_at": None, "lapsed": False}], "OLD_RULES_READ_BACK_UNCHANGED")
        answer = pick(state)
        self.assertEqual(answer["selected"]["model"], "claude-opus-5-5", "OLD_POLICY_SELECTS_BY_COST")
        self.assertNotIn("preference", answer, "NO_WEIGHT_NO_PREFERENCE_FIELD")
        self.assertEqual(set(answer["selected"]), _OLD_SELECTED_KEYS, "SELECTED_PAYLOAD_SHAPE_UNCHANGED")
        weighted = sonnet_opus_state()
        change(weighted, selector=_SONNET, weight=0.5)
        self.assertEqual(set(pick(weighted)["selected"]), _OLD_SELECTED_KEYS, "SELECTED_SHAPE_UNCHANGED_UNDER_A_WEIGHT")

    def test_invalid_weight_is_refused_and_writes_nothing(self) -> None:
        state = sonnet_opus_state()
        change(state)
        before = deepcopy(state._rows)
        for bad in (0, -0.5, float("nan"), float("inf"), True, "0.5", [0.5]):
            with self.assertRaises(CatalogError, msg=f"BAD_WEIGHT_{bad!r}_REFUSED") as caught:
                rule(state, selector=_SONNET, weight=bad)
            self.assertEqual(caught.exception.code, "parameter_invalid")
        with self.assertRaises(CatalogError, msg="RULE_WITH_NEITHER_ENABLED_NOR_WEIGHT_REFUSED") as caught:
            rule(state, selector=_SONNET)
        self.assertEqual(caught.exception.code, "parameter_invalid")
        self.assertEqual(state._rows, before, "REFUSED_WEIGHT_WRITES_NOTHING")
        for stored in ({"id": "r", "selector": {}, "weight": 0}, {"id": "r", "selector": {}, "weight": "x"}, {"id": "r", "selector": {}, "enabled": True, "weight": True}, {"id": "r", "selector": {}}):
            state.rows(NS, POLICY)[0]["rules"] = [stored]
            with self.assertRaises(CatalogError, msg=f"MALFORMED_STORED_RULE_{stored}") as caught:
                policy.read_policy(typed(state))
            self.assertEqual(caught.exception.code, "policy_invalid")

    def test_unreadable_policy_degrades_to_cost_order_with_a_notice(self) -> None:
        state = sonnet_opus_state()
        change(state, selector=_SONNET, weight=0.5)
        state.rows(NS, POLICY)[0]["rules"] = [{"id": "r", "selector": {}, "weight": 0}]
        answer = pick(state)
        self.assertEqual(answer["selected"]["model"], "claude-opus-5-5", "UNREADABLE_POLICY_DOES_NOT_STOP_DISPATCH")
        self.assertTrue(any("preference weights were not applied" in line for line in answer["warnings"]), "UNREADABLE_POLICY_SAYS_SO")
        self.assertNotIn("preference", answer)

    def test_only_discards_weight_rules_and_says_so(self) -> None:
        state = sonnet_opus_state()
        weighed = rule(state, selector=_SONNET, weight=0.5)["rule_id"]
        answer = rule(state, selector={"model_family_prefix": "claude-opus"}, enabled=True, only=True)
        self.assertEqual(len(answer["rules"]), 2, "ONLY_REPLACES_EVERY_RULE")
        self.assertTrue(any(weighed in line for line in answer["warnings"]), "ONLY_NAMES_THE_WEIGHT_RULES_IT_REPLACED")
        self.assertIn(weighed, answer["history"][0]["note"], "ONLY_KEEPS_THE_REPLACED_WEIGHT_IN_HISTORY")

    def test_selection_follows_the_live_weight(self) -> None:
        state = sonnet_opus_state()
        rule_id = change(state, selector=_SONNET, weight=0.5)["rule_id"]
        self.assertEqual(pick(state)["selected"]["model"], "claude-sonnet-5-5", "WEIGHTED_SELECTION_CHOSES_SONNET")
        rule(state, action="retire", rule_id=rule_id)
        self.assertEqual(pick(state)["selected"]["model"], "claude-opus-5-5", "RETIRING_WEIGHT_CHANGES_SELECTION")


class RuleKindWordsEndSmoke(unittest.TestCase):
    """Exercise rule behavior through the real state-interface shape.

    These cases use the same state interface shape as policy writes, rather
    than replacing the policy reader with a matching stub. That keeps the
    audit rows, compare-and-set premise and cell updates in the exercised path.
    The planning rule has an exact kind, not an extra selector dimension:
    session limit callers share the selector vocabulary and have no kind.
    Each enabled and preference opinion is independent and ordered. An
    explicit no-kind request consequently retains the original global policy.
    A cell's stored flag is a write-time snapshot, while effective readback
    and provenance are evaluated together after the end boundary has passed.
    The fixed clock in the lapse case makes both sides of that boundary
    observable without sleeping or depending on host scheduling.
    Legacy words are recovered from policy-change snapshots, newest first;
    unrelated later edits cannot replace the rule's own explanation. More
    than one history page distinguishes a full search from a bounded view.
    Invalid inputs are checked against a copy of all state, including audit
    and cell tables, so a refusal that partially writes does not pass.
    Finally, the public wrapper exercises server-derived caller attribution
    and the process metadata, not just the implementation function.
    """

    def test_rule_keeps_kind_words_and_end_and_read_shows_them(self) -> None:
        state = fixture()
        end = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        result = change(state, kind="planning", words="my words", ends_at=end)
        row = result["rules"][0]
        self.assertEqual(
            (row.get("kind"), row.get("words"), row.get("ends_at"), row.get("lapsed")),
            ("planning", "my words", end, False),
            "RULE_FIELDS_READ_BACK",
        )

    def test_a_rule_with_a_kind_applies_only_to_that_kind(self) -> None:
        state = fixture()
        change(state, enabled=False, weight=2.0)
        change(state, kind="planning", enabled=True, weight=0.5)
        import inspect

        self.assertIn("kind", inspect.signature(policy.decision).parameters, "KIND_MATCH")
        current = policy.read_policy(typed(state))
        for kind, expected in (
            ("planning", (True, 0.5)),
            ("author", (False, 2.0)),
            (None, (False, 2.0)),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(
                    (
                        policy.decision(current, cell(), kind)[0],
                        policy.preference(current, cell(), kind)[0],
                    ),
                    expected,
                    "KIND_MATCH",
                )

    def test_a_rule_past_its_end_does_not_apply_and_reads_lapsed(self) -> None:
        state = fixture()
        state.write_state(NS, {"table": CELLS, "record": cell()})
        end = datetime.now(UTC) + timedelta(days=1)
        change(state, enabled=False, ends_at=end.isoformat())
        self.assertTrue(hasattr(policy, "_now"), "AFTER_END")
        current = policy.read_policy(typed(state))
        self.assertFalse(policy.decision(current, cell(), None, end - timedelta(seconds=1))[0], "BEFORE_END")
        with patch.object(policy, "_now", return_value=end + timedelta(seconds=1)):
            result = policy.read_model_cell_policy(typed(state))
        row = result["cells"][0]
        self.assertEqual(
            (
                row["enabled"],
                row["stored_enabled"],
                row["enabled_provenance"],
                result["rules"][0]["lapsed"],
            ),
            (True, False, "default_on", True),
            "AFTER_END",
        )
        past = change(state, enabled=False, ends_at=(datetime.now(UTC) - timedelta(days=1)).isoformat())
        self.assertTrue(any("end" in warning.lower() for warning in past["warnings"]), "PAST_END_WARNING")

    def test_a_kind_rule_is_not_stored_on_the_cells(self) -> None:
        state = fixture()
        state.write_state(NS, {"table": CELLS, "record": cell("fable")})
        change(state, enabled=False)
        change(state, selector={"model": "fable"}, kind="planning", enabled=True)
        self.assertFalse(model_cell(state, "fable")["enabled"], "NO_KIND_STORAGE")
        import inspect

        self.assertIn("kind", inspect.signature(policy.decision).parameters, "KIND_ENABLED")
        self.assertTrue(
            policy.decision(policy.read_policy(typed(state)), cell("fable"), "planning")[0],
            "KIND_ENABLED",
        )

    def test_old_rules_read_with_no_kind_no_end_and_the_history_reason_as_words(self) -> None:
        state = fixture()
        created = change(state, reason="introduction")
        identifier = created["rule_id"]
        change(state, action="correct", rule_id=identifier, enabled=False, reason="latest change")
        stored = state.rows(NS, POLICY)[0]["rules"][0]
        stored.pop("words", None)
        for i in range(105):
            change(state, selector={"model": f"other-{i}"}, reason="unrelated")
        result = policy.read_model_cell_policy(typed(state))
        old = next(row for row in result["rules"] if row["id"] == identifier)
        self.assertEqual(
            (old.get("kind"), old.get("ends_at"), old.get("words")),
            (None, None, "latest change"),
            "LEGACY_HISTORY_PAGINATES",
        )
        stored = state.rows(NS, POLICY)[0]["rules"][0]
        stored["id"] = "no-history"
        old = next(row for row in policy.read_model_cell_policy(typed(state))["rules"] if row["id"] == "no-history")
        self.assertEqual(old["words"], "no words were recorded for this rule", "EXHAUSTED_HISTORY")

    def test_a_bad_kind_words_or_end_is_refused_and_writes_nothing(self) -> None:
        state = fixture()
        policy.read_policy(typed(state))  # Materialize the fake table before comparing writes.
        before = deepcopy(state._rows)
        for key, value in (
            ("kind", " "),
            ("kind", 3),
            ("words", ""),
            ("words", 3),
            ("ends_at", "bad"),
            ("ends_at", "2026-01-01T00:00:00"),
        ):
            with (
                self.subTest(key=key, value=value),
                self.assertRaises(CatalogError, msg="INVALID_FIELD_REFUSED"),
            ):
                change(state, **{key: value})
            self.assertEqual(state._rows, before, "INVALID_FIELD_WRITES_NOTHING")

    def test_words_default_to_the_reason_and_the_surface_accepts_the_new_keys(self) -> None:
        state = fixture()
        plugin = object.__new__(OperatorPlugin)
        plugin.fixture_state = typed(state)
        end = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        answer = plugin.set_model_cells_enabled(
            {
                "selector": {},
                "enabled": True,
                "expected_updated_at": None,
                "reason": "because",
                "kind": "planning",
                "ends_at": end,
            },
            {"caller_attribution_instance_id": "agi-operator"},
        )
        self.assertEqual(answer["data"]["rules"][0].get("words"), "because", "REASON_DEFAULTS_WORDS")
        metadata = AgentMessagingPlugin.set_model_cells_enabled
        self.assertTrue(
            all(key in str(getattr(metadata, "_platform_process_metadata", metadata.__dict__)) for key in ("kind", "words", "ends_at")),
            "SURFACE_KEYS",
        )


if __name__ == "__main__":
    unittest.main()
