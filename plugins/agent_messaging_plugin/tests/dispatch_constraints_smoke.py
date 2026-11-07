#!/usr/bin/env python3
"""Constraint filtering, bridge declaration and immutable receipt replay."""

from __future__ import annotations

import copy
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.dispatch_tier_selection import TierSelectionError  # noqa: E402
from agent_messaging_plugin.model_capability_store import CatalogError, ManualCell, record_manual_cell  # noqa: E402
from agent_messaging_plugin.models import BridgeSessionState  # noqa: E402
from agent_messaging_plugin.platform_surface import BridgeError, PlatformSurface  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MODEL_CAPABILITY_CELL  # noqa: E402
from agent_messaging_plugin.spawn_selection_receipts import SelectionReceiptError, replay_selection_receipt  # noqa: E402

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
KEYS = {"effort_ceiling": None, "exclude_model_prefixes": [], "exclude_runtimes": []}
GOLDEN: dict[str, Any] = {
    "freshness_window": {"hours": 72.0, "cell_hours": 72, "source": "request", "policy_revision_id": None, "advisories": []},
    "advisories": {"stale_reading_used": 0},
    "billing_objective": "metered_usd",
    "capability_floors": [],
    "catalog_run_id": "gen-6",
    "dominated": [
        {
            "stronger": {"enabled": True, "acceptance": "accepted", "capability_score": 60.0, "cost_per_task_usd": 1.0, "effort": "xhigh", "measured_at": "2026-10-16T12:00:00+00:00", "model": "cheap-model", "relative_cost_multiplier": 2.0, "runtime": "codex"},
            "weaker": {"enabled": True, "acceptance": "accepted", "capability_score": 55.0, "cost_per_task_usd": 4.0, "effort": "high", "measured_at": "2026-10-16T12:00:00+00:00", "model": "other-model", "relative_cost_multiplier": 8.0, "runtime": "claude_code"},
        },
        {
            "stronger": {"enabled": True, "acceptance": "accepted", "capability_score": 60.0, "cost_per_task_usd": 1.0, "effort": "xhigh", "measured_at": "2026-10-16T12:00:00+00:00", "model": "cheap-model", "relative_cost_multiplier": 2.0, "runtime": "codex"},
            "weaker": {"enabled": True, "acceptance": "accepted", "capability_score": 50.0, "cost_per_task_usd": 3.0, "effort": "low", "measured_at": "2026-10-16T12:00:00+00:00", "model": "other-model", "relative_cost_multiplier": 6.0, "runtime": "claude_code"},
        },
    ],
    "effective_required_score": 42.0,
    "excluded": {"below_threshold": 1, "capability_floor_disallowed": 0, "not_accepted": 0, "operator_disabled": 0, "unknown_effort": 0, "unpriced": 0},
    "frontier": [
        {"enabled": True, "acceptance": "accepted", "capability_score": 60.0, "cost_per_task_usd": 1.0, "effort": "xhigh", "measured_at": "2026-10-16T12:00:00+00:00", "model": "cheap-model", "relative_cost_multiplier": 2.0, "runtime": "codex"},
        {"enabled": True, "acceptance": "accepted", "capability_score": 65.0, "cost_per_task_usd": 5.0, "effort": "xhigh", "measured_at": "2026-10-16T12:00:00+00:00", "model": "other-model", "relative_cost_multiplier": 10.0, "runtime": "claude_code"},
    ],
    "ladder": [{"cost_delta": 1.5, "from_effort": "low", "points_per_cost_unit": 16.666666666666668, "score_gain": 25.0, "to_effort": "high"}, {"cost_delta": -1.0, "from_effort": "high", "points_per_cost_unit": None, "score_gain": 5.0, "to_effort": "xhigh"}],
    "policy_version": None,
    "required_score": 42.0,
    "selected": {"enabled": True, "acceptance": "accepted", "capability_score": 60.0, "cost_per_task_usd": 1.0, "effort": "xhigh", "measured_at": "2026-10-16T12:00:00+00:00", "model": "cheap-model", "relative_cost_multiplier": 2.0, "runtime": "codex"},
    "selection_receipt": {"billing_objective": "metered_usd", "cost_tolerance": 0.05, "max_staleness_hours": 72, "required_score": 42.0, "runtime_constraint": None, "score_margin": 0.0, "selected": {"effort": "xhigh", "model": "cheap-model", "runtime": "codex"}},
    "stale_readings": [],
    "unscored_cells": 0,
}
CELLS = (
    ("codex", "cheap-model", "low", 30, 0.5),
    ("codex", "cheap-model", "high", 55, 2.0),
    ("codex", "cheap-model", "xhigh", 60, 1.0),
    ("claude_code", "other-model", "low", 50, 3.0),
    ("claude_code", "other-model", "high", 55, 4.0),
    ("claude_code", "other-model", "xhigh", 65, 5.0),
)


def state() -> StateManagementInterface:
    fake = RealShapeState()
    typed = cast("StateManagementInterface", fake)
    for runtime, model, effort, score, cost in CELLS:
        record_manual_cell(typed, ManualCell(runtime, model, effort, score, cost, "constraints fixture", None))
    for row in fake.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["measured_at"] = NOW.isoformat()
        row["accepted_at"] = NOW.isoformat()
    return typed


def select(**kwargs: Any) -> dict[str, Any]:
    return verbs.select_dispatch_tier(state(), {"required_score": 42, "billing_objective": "metered_usd", "max_staleness_hours": 72, **kwargs}, now=NOW)


def check(condition: object, name: str) -> None:
    assert condition, name
    print("PASS", name)


def refusal(callback: Any) -> tuple[str, str]:
    try:
        callback()
    except (CatalogError, TierSelectionError, SelectionReceiptError) as exc:
        return exc.code, exc.message
    return "", ""


class Template:
    def resolve_templates(self, action_def: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        del context
        return action_def


class StateService:
    def generate_unique_string(self, length: int, encoding: str) -> dict[str, Any]:
        del encoding
        return {"action_status": "completed", "data": {"random_string": "smoke"[:length]}}


class Recorder:
    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []

    def store_action_event(self, action: dict[str, Any]) -> str:
        self.actions.append(action)
        return f"ae-constraints-{len(self.actions)}"


class Flows:
    def create_flow(self, **kwargs: Any) -> str:
        return "flow-constraints"

    def update_flow_status(self, flow_id: str, status: str) -> None:
        pass


class Context:
    def build_context(self, **kwargs: Any) -> dict[str, Any]:
        return {}


class Bridges:
    def get(self, bridge_id: str) -> BridgeSessionState:
        return BridgeSessionState(bridge_id=bridge_id, session_id="sess-constraints")


class FixturePlugin(AgentMessagingPlugin):
    def _get_state_service(self) -> StateManagementInterface:
        return state()


def bridge_accepts_constraints() -> None:
    key = "plugin::agent_messaging_plugin::select_dispatch_tier"
    metadata = AgentMessagingPlugin.select_dispatch_tier._platform_process_metadata
    recorder = Recorder()
    factory = ActionFactory(
        process_registry={"processes": {key: {"parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()}}}},
        template_engine=Template(),
        state_service=StateService(),
        action_event_recorder=recorder,
    )
    surface = PlatformSurface(action_factory=factory, flow_manager=Flows(), compilation_context_builder=Context(), bridge_manager=Bridges(), export_policy=ProcessExportPolicy(allow_patterns=("plugin::*",)))
    error = None
    try:
        surface.process_call(key, {"required_score": 42, "billing_objective": "metered_usd", "effort_ceiling": "max", "exclude_model_prefixes": ["unused"], "exclude_runtimes": ["codex"]}, trigger_data={"bridge_id": "agc-constraints", "session_id": "sess-constraints"}, deliver_to_bridge=False)
    except BridgeError as exc:
        error = exc.code
    check(error is None and len(recorder.actions) == 1, "BRIDGE_ACCEPTS_CONSTRAINTS")
    plugin = object.__new__(FixturePlugin)
    result = AgentMessagingPlugin.select_dispatch_tier(plugin, recorder.actions[0]["parameters"], {})
    answer = result["data"]
    check(answer["selection_receipt"]["selected"] == {"runtime": "claude_code", "model": "other-model", "effort": "low"} and all(key in answer["selection_receipt"] for key in KEYS), "BRIDGE_ACCEPTS_CONSTRAINTS wrapper tuple and receipt")


def replay(receipt: dict[str, Any]) -> dict[str, Any]:
    return replay_selection_receipt(state(), synthetic_qualification=False, enforce=True, difficulty_score=42, selection_receipt=receipt, dispatch_kind="fix", scope_tags=(), agent_runtime=receipt["selected"]["runtime"], model=receipt["selected"]["model"], effort=receipt["selected"]["effort"])


def pure_assertions() -> None:
    high = select(effort_ceiling="high")
    check(high["selected"]["effort"] == "high" and high["excluded"]["constraint_effort_ceiling"] == 2, "EFFORT_CEILING_HIGH_NEVER_RETURNS_XHIGH")
    prefix = select(exclude_model_prefixes=["cheap"])
    check(prefix["selected"]["model"] == "other-model" and prefix["excluded"]["constraint_model_prefix"] == 3, "EXCLUDE_MODEL_PREFIX_REMOVES_CELLS_AND_COUNTS")
    runtime = select(exclude_runtimes=["codex"])
    check(runtime["selected"]["runtime"] == "claude_code" and runtime["excluded"]["constraint_runtime"] == 3, "EXCLUDE_RUNTIME_REMOVES_CELLS_AND_COUNTS")
    empty = refusal(lambda: select(exclude_runtimes=["codex", "claude_code"]))
    check(empty[0] == "no_cell_clears_threshold" and "'constraint_runtime': 6" in empty[1] and select(exclude_runtimes=[])["selected"]["model"] == "cheap-model", "CONSTRAINTS_EMPTY_SET_REFUSES_NAMING_CONSTRAINT")


def invalid_parameters() -> None:
    for params in ({"effort_ceiling": "bogus"}, {"effort_ceiling": []}, {"exclude_model_prefixes": "cheap"}, {"exclude_runtimes": [1]}, {"exclude_model_prefixes": [""]}, {"exclude_runtimes": None}):
        check(refusal(lambda params=params: select(**params))[0] == "parameter_invalid", "CONSTRAINT_PARAMETER_INVALID")
    check(select(effort_ceiling="max", exclude_model_prefixes=["unused"], exclude_runtimes=["unused"])["selected"]["effort"] == "xhigh", "CONSTRAINT_PARAMETER_INVALID valid companion")


def replay_matches(receipt: dict[str, Any]) -> bool:
    try:
        return replay(receipt) == receipt
    except SelectionReceiptError:
        return False


def receipt_assertions() -> None:
    plain = select()
    prefix = select(exclude_model_prefixes=["cheap"])
    stripped = copy.deepcopy(plain)
    for key in (*KEYS, "pin", "pin_basis"):
        stripped["selection_receipt"].pop(key)
    for key in ("selection_mode", "pin_basis", "pin_cost_delta", "pin_overrides_constraints", "pin_below_threshold"):
        stripped.pop(key)
    check(stripped == GOLDEN, "UNPINNED_ANSWER_IDENTICAL_EXCEPT_NEW_KEYS")
    check(all(plain["selection_receipt"][key] == value for key, value in KEYS.items()) and prefix["selection_receipt"]["exclude_model_prefixes"] == ["cheap"], "RECEIPT_HAS_THREE_CONSTRAINT_KEYS_ALWAYS")
    receipt = prefix["selection_receipt"]
    check(replay_matches(receipt), "REPLAY_WITH_CONSTRAINTS_PASSES")
    changed = copy.deepcopy(receipt)
    changed["exclude_model_prefixes"] = []
    check(refusal(lambda: replay(changed))[0] == "selection_receipt_mismatch", "REPLAY_WITH_CHANGED_CONSTRAINT_MISMATCHES")
    legacy = copy.deepcopy(GOLDEN["selection_receipt"])
    before = copy.deepcopy(legacy)
    check(replay(legacy) is legacy and legacy == before, "REPLAY_PREDEPLOY_CONSTRAINT_DEFAULTS_PASSES")
    saved = copy.deepcopy(receipt)
    corrected = select(effort_ceiling="high", exclude_model_prefixes=[], exclude_runtimes=["claude_code"])
    cleared = select(effort_ceiling=None, exclude_model_prefixes=[], exclude_runtimes=[])
    unknown = {**receipt, "unknown": True}
    check(corrected["selected"]["effort"] == "high" and cleared == plain and receipt == saved and refusal(lambda: replay(unknown))[0] == "selection_receipt_invalid", "CONSTRAINT_CORRECTION_AND_CLEAR")


def main() -> int:
    if "--golden" in sys.argv:
        print(json.dumps(select(), sort_keys=True))
        return 0
    bridge_accepts_constraints()
    pure_assertions()
    invalid_parameters()
    receipt_assertions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
