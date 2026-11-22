#!/usr/bin/env python3
"""Exercise kinded policy grants, refusal provenance and the generic spawn boundary."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "ananta/src"))
sys.path.insert(0, str(ROOT / "plugins/agent_messaging_plugin/src"))

import capability_only_selector_smoke as direct  # noqa: E402
import generic_spawn_smoke as spawn  # noqa: E402
import model_cell_enablement_smoke as cells  # noqa: E402

from agent_messaging_plugin import model_cell_enablement as policy  # noqa: E402

PROCESS = "select_model_cell_for_kind"
FABLE = "claude-fable-fixture"
SCORE = 74.625
OFF_WORDS = "Turn everything off."
PLAN_WORDS = "Use Fable for planning."
# Measured on the release-74 capability-only fixture, without a policy or a pin.
DIRECT_KEYS = (
    "advisories",
    "billing_objective",
    "catalog_run_id",
    "dominated",
    "effective_required_score",
    "excluded",
    "freshness_window",
    "frontier",
    "ladder",
    "pin_basis",
    "pin_below_threshold",
    "pin_cost_delta",
    "pin_overrides_constraints",
    "required_score",
    "selected",
    "selection_mode",
    "stale_readings",
    "unscored_cells",
)


class ModelKindRuleLayerSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.db = cells.fixture()
        for model, runtime in ((FABLE, "claude_code"), ("cheap", "codex")):
            self.db.write_state(cells.NS, {"table": cells.CELLS, "record": cells.cell(model, runtime=runtime)})
        self.off = cells.change(self.db, enabled=False, words=OFF_WORDS)["rule_id"]
        self.planning = cells.change(self.db, selector={"model_family_prefix": "claude-fable"}, kind="planning", enabled=True, words=PLAN_WORDS)["rule_id"]
        self.plugin = object.__new__(cells.OperatorPlugin)
        self.plugin.fixture_state = cells.typed(self.db)

    def call(self, **params: object) -> dict[str, Any]:
        handler = getattr(self.plugin, PROCESS, None)
        if not callable(handler):
            self.fail(f"{self.id()}: the kind-aware EDGE process is missing")
        return cast(dict[str, Any], handler({"parameters": {"required_score": SCORE, **params}}, {}))

    def test_planning_grants_fable_and_preserves_the_fractional_shortfall(self) -> None:
        answer = self.call(kind="planning", runtime="claude_code")
        self.assertEqual(answer["action_status"], "completed", answer)
        data = answer["data"]
        self.assertEqual(data["selected"]["model"], FABLE)
        self.assertEqual(data["grant"], {"kind": "planning", "rule_id": self.planning, "words": PLAN_WORDS})
        self.assertEqual((data["required_score"], data["effective_required_score"], data["shortfall"]["required_score"]), (SCORE, SCORE, SCORE))
        self.assertFalse(cells.model_cell(self.db, FABLE)["enabled"])

    def test_generic_spawn_refuses_the_granted_tuple_with_zero_host_calls(self) -> None:
        grant = self.call(kind="planning")["data"]
        self.assertEqual(grant["grant"]["rule_id"], self.planning)
        chosen = grant["selected"]
        with spawn._fixture() as host, patch.object(host.plugin, "_get_state_service", return_value=cells.typed(self.db)):
            receipt, _ = host.spawn(host.generic_params(agent_runtime=chosen["runtime"], model=chosen["model"], effort=chosen["effort"]), caller=spawn._CALLER)
            self.assertEqual(receipt["error"]["code"], "model_cell_disabled")
            self.assertIn(self.off, receipt["error"]["message"])
            self.assertEqual(host.driver.specs, [], "the planning grant did not launch a host")

    def test_author_and_absent_kind_get_the_global_rule_and_its_words(self) -> None:
        for params in ({"kind": "author"}, {}, {"kind": None}):
            with self.subTest(params=params):
                answer = self.call(**params)
                self.assertEqual(answer["error"]["code"], "no_rule_serves")
                self.assertEqual(answer["data"], {"kind": params.get("kind"), "rule_id": self.off, "words": OFF_WORDS})
                self.assertIn(OFF_WORDS, answer["error"]["message"])

    def test_expired_grant_and_its_preference_have_no_opinion(self) -> None:
        cells.change(self.db, action="correct", rule_id=self.planning, selector={"model": FABLE}, kind="planning", enabled=True, weight=0.01, words="Expired words", ends_at="2000-01-01T00:00:00+00:00")
        self.assertTrue(policy.read_model_cell_policy(cells.typed(self.db))["rules"][-1]["lapsed"])
        answer = self.call(kind="planning")
        self.assertEqual((answer["error"]["code"], answer["data"]["rule_id"], answer["data"]["words"]), ("no_rule_serves", self.off, OFF_WORDS))

    def test_invalid_kind_is_refused(self) -> None:
        for kind in ("", " ", "two words", 7):
            with self.subTest(kind=kind):
                self.assertEqual(self.call(kind=kind)["error"]["code"], "parameter_invalid")

    def test_last_denying_rule_and_legacy_words(self) -> None:
        deny = cells.change(self.db, selector={"model": FABLE}, kind="planning", enabled=False, reason="Keep Fable idle.")
        stored = self.db.rows(cells.NS, cells.POLICY)[0]["rules"][-1]
        stored.pop("words")
        cells.change(self.db, selector={"model": FABLE}, kind="planning", enabled=None, weight=0.1, words="Only a preference")
        answer = self.call(kind="planning")
        self.assertEqual((answer["error"]["code"], answer["data"]["rule_id"], answer["data"]["words"]), ("no_rule_serves", deny["rule_id"], "Keep Fable idle."))

    def test_attribution_requires_a_cell_that_could_have_served(self) -> None:
        cells.change(self.db, selector={"model": "cheap"}, enabled=False, words="Still disabled")
        self.assertEqual(self.call(kind="author")["data"]["rule_id"], self.off)
        self.assertEqual(self.call(kind="planning", runtime="codex")["data"]["rule_id"], self.off)
        impossible_pin = {"runtime": "claude_code", "model": "absent", "effort": "medium"}
        self.assertEqual(self.call(kind="author", pin=impossible_pin, pin_basis="specific tuple")["error"]["code"], "pin_cell_unavailable")

    def test_kind_preference_and_caller_narrowing(self) -> None:
        cells.change(self.db, action="clear")
        pref = cells.change(self.db, selector={"model": FABLE}, kind="planning", enabled=None, weight=0.01)
        self.assertEqual(self.call(kind="planning", required_score=40)["data"]["preference"], {"weight": 0.01, "rule_id": pref["rule_id"]})
        self.assertEqual(self.call(kind="author", required_score=40)["data"]["selected"]["model"], "cheap")
        self.assertEqual(self.call(kind="planning", required_score=40, exclude_runtimes=["claude_code"])["data"]["selected"]["model"], "cheap")
        self.assertEqual(self.call(kind="planning", effort_ceiling="low")["error"]["code"], "catalog_ineligible")
        pin = {"model": FABLE, "runtime": "claude_code", "effort": "medium"}
        self.assertEqual(self.call(kind="author", pin=pin, pin_basis="explicit request")["data"]["selection_mode"], "pinned")

    def test_public_parameters_and_no_kind_direct_choice(self) -> None:
        data = direct.select(direct.state())
        self.assertEqual(tuple(sorted(data)), DIRECT_KEYS)
        self.assertEqual(tuple(data["selected"][key] for key in ("runtime", "model", "effort")), ("codex", "cheap-model", "xhigh"))
        self.assertEqual(set(cast(Any, self.plugin.select_dispatch_tier)._platform_process_metadata.parameters), direct.EXPECTED_PARAMETERS)
        self.assertIn(PROCESS, self.plugin.get_edge_process_definitions())
        parameters = getattr(self.plugin, PROCESS)._platform_process_metadata.parameters
        self.assertEqual(set(parameters), {"kind", "required_score", "runtime", "pin", "pin_basis", "effort_ceiling", "exclude_runtimes", "exclude_model_prefixes"})
        self.assertEqual([key for key, value in parameters.items() if value.required], ["required_score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
