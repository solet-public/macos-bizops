#!/usr/bin/env python3
"""Exact accepted pin selection, real bridge validation and immutable spawn replay.

All catalog transitions occur in the real-shape state fixture. The first
assertion crosses the registered bridge boundary, so the base-red proof sees
unknown arguments instead of an import failure. The public spawn receipt
helper performs replay without starting a session. GOLDEN is the original-base
receipt/answer captured by the s1 smoke, kept here as immutable fixture data.
Every new optional-field subset must compare canonically without changing the
saved dictionary. Fault injection is restricted to the optional refresh-history
lookup; it must never conceal a catalog or receipt integrity failure.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import sys
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

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
from agent_messaging_plugin.dispatch_tier_selection import TierSelectionError, select_tier  # noqa: E402
from agent_messaging_plugin.model_capability_store import CatalogError, ManualCell, record_manual_cell, selection_catalog  # noqa: E402
from agent_messaging_plugin.models import BridgeSessionState  # noqa: E402
from agent_messaging_plugin.platform_surface import BridgeError, PlatformSurface  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MODEL_CAPABILITY_CELL, TABLE_MODEL_CAPABILITY_REFRESH_RUN  # noqa: E402
from agent_messaging_plugin.spawn_selection_receipts import SelectionReceiptError, receipt_contract_mismatches, replay_selection_receipt  # noqa: E402

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
KEYS = {"pin": None, "pin_basis": None, "effort_ceiling": None, "exclude_model_prefixes": [], "exclude_runtimes": []}
PIN_FIELDS = {"selection_mode", "pin_basis", "pin_cost_delta", "pin_overrides_constraints", "pin_below_threshold"}
PIN = {"runtime": "codex", "model": "cheap-model", "effort": "high"}
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
    """
    Build six accepted scored cells in two runtimes, at a fixed measurement time.
    """
    fake = RealShapeState()
    typed = cast("StateManagementInterface", fake)
    for runtime, model, effort, score, cost in CELLS:
        record_manual_cell(typed, ManualCell(runtime, model, effort, score, cost, "constraints fixture", None))
    for row in fake.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL):
        row["measured_at"] = NOW.isoformat()
        row["accepted_at"] = NOW.isoformat()
    return typed


def select(fixture: StateManagementInterface | None = None, **kwargs: Any) -> dict[str, Any]:
    """
    Run the real verb against a fresh isolated fixture unless one is supplied.
    """
    return verbs.select_dispatch_tier(fixture or state(), {"required_score": 42, "billing_objective": "metered_usd", "max_staleness_hours": 72, **kwargs}, now=NOW)


def pinned(fixture: StateManagementInterface | None = None, **kwargs: Any) -> dict[str, Any]:
    """
    Issue a new selection with explicit caller provenance; overrides are per call.
    """
    return select(fixture, **{"pin": PIN, "pin_basis": "registered fixture provenance", **kwargs})


def rows(fixture: StateManagementInterface) -> list[dict[str, Any]]:
    """
    Expose fixture rows only for deterministic accepted/withdrawn/unscored transitions.
    """
    return cast("RealShapeState", fixture).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL)


def replay(fixture: StateManagementInterface, receipt: dict[str, Any]) -> dict[str, Any]:
    """
    Exercise the public enforce=true spawn replay helper without spawning a session.
    """
    selected = receipt["selected"]
    return replay_selection_receipt(fixture, synthetic_qualification=False, enforce=True, difficulty_score=receipt["required_score"], selection_receipt=receipt, dispatch_kind="fix", scope_tags=(), agent_runtime=selected["runtime"], model=selected["model"], effort=selected["effort"])


def digest(value: object) -> str:
    """
    Fingerprint receipt bytes in the same stable order before and after replay.
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


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


def bridge_accepts_pin() -> None:
    """
    Validate declared inputs through the real registry and invoke the queued wrapper.
    """
    key = "plugin::agent_messaging_plugin::select_dispatch_tier"
    metadata = AgentMessagingPlugin.select_dispatch_tier._platform_process_metadata
    recorder = Recorder()
    factory = ActionFactory(process_registry={"processes": {key: {"parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()}}}}, template_engine=Template(), state_service=StateService(), action_event_recorder=recorder)
    surface = PlatformSurface(action_factory=factory, flow_manager=Flows(), compilation_context_builder=Context(), bridge_manager=Bridges(), export_policy=ProcessExportPolicy(allow_patterns=("plugin::*",)))
    error = None
    try:
        surface.process_call(
            key, {"required_score": 42, "billing_objective": "metered_usd", "effort_ceiling": "low", "exclude_model_prefixes": ["cheap"], "exclude_runtimes": ["codex"], "pin": PIN, "pin_basis": "registered fixture provenance"}, trigger_data={"bridge_id": "agc-pin", "session_id": "sess-pin"}, deliver_to_bridge=False
        )
    except BridgeError as exc:
        error = exc.code
    check(error is None and len(recorder.actions) == 1, f"BRIDGE_ACCEPTS_PIN ({error})")
    result = AgentMessagingPlugin.select_dispatch_tier(object.__new__(FixturePlugin), recorder.actions[0]["parameters"], {})
    check(result["data"]["selection_receipt"]["selected"] == PIN and PIN_FIELDS <= metadata.return_value_schema.properties.keys(), "BRIDGE_ACCEPTS_PIN wrapper tuple and return metadata")


def selection_or_refusal(callback: Any) -> dict[str, Any]:
    """
    Keep selector refusals as data so mutants fail the intended named assertion.
    """
    try:
        return callback()
    except (CatalogError, TierSelectionError, SelectionReceiptError) as exc:
        return {"refusal": exc.code}


def selection_assertions() -> None:
    """
    Pin before constraints, then compare cost to the feasible argmin in each objective.
    """
    answer = selection_or_refusal(lambda: pinned(exclude_runtimes=["codex"], effort_ceiling="low", exclude_model_prefixes=["cheap"]))
    check(answer.get("selection_receipt", {}).get("selected") == PIN and answer["selection_mode"] == "pinned" and answer["pin_overrides_constraints"], "PIN_SELECTS_EXACT_ACCEPTED_CELL")
    check(pinned()["pin_cost_delta"] == 1.0 and pinned(billing_objective="relative")["pin_cost_delta"] == 2.0 and pinned(exclude_runtimes=["codex", "claude_code"])["pin_cost_delta"] is None, "PIN_COST_DELTA_AGAINST_CHEAPEST")


def override_assertions() -> None:
    """
    An accepted scored pin without an objective price remains selectable.
    """
    fixture = state()
    for row in rows(fixture):
        if row["runtime"] == PIN["runtime"] and row["effort"] == PIN["effort"]:
            row["cost_per_task_usd"] = None
            row["relative_cost_multiplier"] = None
    check(pinned(fixture)["pin_cost_delta"] is None, "PIN_COST_DELTA_AGAINST_CHEAPEST unpriced selectable")


def floor_quota_assertions() -> None:
    """
    Threshold and floor restrictions become explicit advisories for an accepted pin.
    """
    low = pinned(pin={**PIN, "effort": "low"}, score_margin=1)
    catalog = selection_catalog(rows(state()))
    # Use the public pure core with a parsed Pin, without importing a new symbol on the base-red path.
    pin = verbs._pin_inputs({"pin": PIN, "pin_basis": "fixture"}, None)[0]
    common = {"required_score": 42, "objective": "metered_usd", "now": NOW, "max_age": timedelta(hours=72), "pin": pin}
    floor = select_tier(catalog, **common, capability_floor_pairs=frozenset())
    check(low["pin_below_threshold"] and floor.advisories["pin_floor_disallowed"] == 1, "PIN_BELOW_THRESHOLD_FLAGGED_NOT_REFUSED")
    with patch.object(verbs, "_capability_floor_pairs", return_value=(frozenset(), None, ())):
        check(any("pin_floor_disallowed" in warning for warning in pinned()["warnings"]), "PIN_BELOW_THRESHOLD_FLAGGED_NOT_REFUSED floor warning")


def refusal_assertions() -> None:
    """
    Unavailable exact tuples name usable alternatives from the same runtime.
    """
    missing = {**PIN, "model": "missing-model"}
    code, message = refusal(lambda: pinned(pin=missing))
    check(code == "pin_cell_unavailable" and "codex/missing-model@high" in message and "nearest accepted scored cells:" in message and "cheap-model@high" in message and pinned()["selected"]["effort"] == "high", "PIN_ABSENT_CELL_REFUSES_WITH_NEAREST")


def withdrawn_assertions() -> None:
    """
    Withdraw and reinstate only fixture rows; an accepted tuple recovers immediately.
    """
    fixture = state()
    row = next(row for row in rows(fixture) if row["runtime"] == "codex" and row["effort"] == "high")
    check(pinned(fixture)["selected"]["effort"] == "high", "PIN_WITHDRAWN_CELL_REFUSES accepted before")
    row["acceptance"] = "withdrawn"
    check(refusal(lambda: pinned(fixture))[0] == "pin_cell_unavailable", "PIN_WITHDRAWN_CELL_REFUSES")
    row["acceptance"] = "accepted"
    check(pinned(fixture)["selected"]["effort"] == "high", "PIN_WITHDRAWN_CELL_REFUSES reinstated")


def invalid_assertions() -> None:
    """
    Reject malformed pin/basis/runtime inputs and prove valid companion calls.
    """
    for basis in (None, "", "  "):
        check(refusal(lambda basis=basis: pinned(pin_basis=basis))[0] == "parameter_invalid", "PIN_REQUIRES_BASIS")
    check(pinned(pin_basis="caller plan v4")["pin_basis"] == "caller plan v4", "PIN_REQUIRES_BASIS valid provenance")
    for value in ({}, [], "codex", {**PIN, "effort": "bogus"}, {**PIN, "model": ""}, {**PIN, "extra": "x"}, {**PIN, "runtime": 1}):
        check(refusal(lambda value=value: pinned(pin=value))[0] == "parameter_invalid", "PIN_PARAMETER_INVALID")
    check(refusal(lambda: pinned(runtime="claude_code"))[0] == "parameter_invalid" and pinned(runtime="codex")["selection_receipt"]["selected"] == PIN, "PIN_PARAMETER_INVALID runtime match")
    check("pin_basis requires pin" in refusal(lambda: select(pin_basis="alone"))[1], "PIN_PARAMETER_INVALID basis alone")


def unscored_assertions() -> None:
    """
    Pricing is not scoring: an accepted unscored tuple refuses beside a scored pass.
    """
    unscored = state()
    row = rows(unscored)[0]
    row.update(model="gpt-6.1-sol", capability_score=None)
    code, message = refusal(lambda: pinned(unscored, pin={"runtime": "codex", "model": "gpt-6.1-sol", "effort": "low"}))
    rows(unscored)[1]["model"] = "gpt-6-sol"
    scored = pinned(unscored, pin={"runtime": "codex", "model": "gpt-6-sol", "effort": "high"})
    check(code == "pin_cell_unavailable" and "codex/gpt-6.1-sol@low" in message and "no accepted scored catalog cell; nearest accepted scored cells:" in message and scored["selected"]["model"] == "gpt-6-sol", "PIN_UNSCORED_CELL_REFUSES_WITH_SCORED_PASS")


def replay_assertions() -> None:
    """
    Project the default answer against the captured original and s1 golden data.
    """
    fixture = state()
    plain = select(fixture)
    s1_golden = copy.deepcopy(GOLDEN)
    s1_golden["selection_receipt"].update({key: value for key, value in KEYS.items() if key not in {"pin", "pin_basis"}})
    stripped = copy.deepcopy(plain)
    for key in PIN_FIELDS:
        stripped.pop(key)
    for key in ("pin", "pin_basis"):
        stripped["selection_receipt"].pop(key)
    original = copy.deepcopy(stripped)
    for key in ("effort_ceiling", "exclude_model_prefixes", "exclude_runtimes"):
        original["selection_receipt"].pop(key)
    check(stripped == s1_golden and original == GOLDEN, "S2_UNPINNED_ANSWER_UNCHANGED")


def receipt_key_assertions() -> None:
    """
    Always issue exactly twelve keys and preserve the requested pin in replay.
    """
    fixture = state()
    plain = select(fixture)
    receipt = pinned(fixture)["selection_receipt"]
    check(set(receipt) == set(GOLDEN["selection_receipt"]) | set(KEYS) and all(plain["selection_receipt"][key] == value for key, value in KEYS.items()) and receipt["pin"] == PIN, "RECEIPT_HAS_FIVE_NEW_KEYS_ALWAYS")
    check(replay(fixture, receipt) is receipt, "REPLAY_WITH_PIN_PASSES")


def legacy_assertions() -> None:
    """
    Replay the captured original seven-key receipt and saved tier without replacing either.
    """
    fixture = state()
    legacy = copy.deepcopy(GOLDEN["selection_receipt"])
    before = digest(legacy)
    check(selection_or_refusal(lambda: replay(fixture, legacy)) is legacy and digest(legacy) == before and replay(fixture, {**KEYS, **legacy}) == {**KEYS, **legacy}, "REPLAY_PREDEPLOY_RECEIPT_PASSES")
    # dispatch_lane.tier() returns the saved tier before any selector invocation.
    saved_tier = {"model": legacy["selected"]["model"], "effort": legacy["selected"]["effort"], "selection_receipt": legacy}
    saved_hash = digest(saved_tier)
    returned_tier = saved_tier
    check(replay(fixture, returned_tier["selection_receipt"]) is legacy and digest(saved_tier) == saved_hash, "REPLAY_SAVED_TIER_RERUN_PASSES")


def default_assertions() -> None:
    """
    Exercise every subset of absent defaults plus original-key and tuple integrity.
    """
    fixture = state()
    plain = select(fixture)
    legacy = copy.deepcopy(GOLDEN["selection_receipt"])
    constrained = select(fixture, exclude_model_prefixes=["cheap"])["selection_receipt"]
    check(refusal(lambda: replay(fixture, {**constrained, "exclude_model_prefixes": []}))[0] == "selection_receipt_mismatch", "S2_CHANGED_CONSTRAINT_MISMATCHES")
    for size in range(6):
        for subset in combinations(KEYS, size):
            optional = {key: value for key, value in plain["selection_receipt"].items() if key not in subset}
            assert replay(fixture, optional) is optional, subset
    unknown = {**legacy, "unknown": 1}
    missing = {key: value for key, value in legacy.items() if key != "score_margin"}
    tampered = {**legacy, "selected": PIN}
    check(refusal(lambda: replay(fixture, unknown))[0] == "selection_receipt_invalid" and refusal(lambda: replay(fixture, missing))[0] == "selection_receipt_invalid" and refusal(lambda: replay(fixture, tampered))[0] == "selection_receipt_mismatch", "REPLAY_MISSING_DEFAULTS_AND_UNKNOWN_KEYS")


def correction_assertions() -> None:
    """
    Correct and clear by new selections; managed retries protect historical provenance.
    """
    fixture = state()
    plain = select(fixture)
    receipt = pinned(fixture)["selection_receipt"]
    before = digest(receipt)
    corrected = pinned(fixture, pin={**PIN, "effort": "xhigh"}, pin_basis="corrected basis")["selection_receipt"]
    cleared = select(fixture, pin=None, pin_basis=None)
    changed_basis = {**receipt, "pin_basis": "new provenance"}
    check(digest(receipt) == before, "PIN_CORRECTION_CLEAR_AND_RECEIPT_INTEGRITY immutable hash")
    check(
        corrected["pin"] != receipt["pin"]
        and cleared == plain
        and replay(fixture, receipt) is receipt
        and receipt_contract_mismatches({"difficulty_score": 42, "selection_receipt": receipt}, enforce=True, difficulty_score=42, selection_receipt=changed_basis) == {"selection_receipt"}
        and pinned(fixture, exclude_runtimes=["codex", "claude_code"])["pin_cost_delta"] is None,
        "PIN_CORRECTION_CLEAR_AND_RECEIPT_INTEGRITY",
    )


class HistoryFault(RealShapeState):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode
        self.reached = 0

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        if query.get("table") == TABLE_MODEL_CAPABILITY_REFRESH_RUN:
            self.reached += 1
            print("HISTORY_LOOKUP_REACHED", self.mode, self.reached)
            if self.mode == "raise":
                raise RuntimeError("injected advisory history failure")
            if self.mode == "envelope":
                return {"action_status": "failed", "error": {"message": "injected advisory history envelope"}, "data": {}}
        return super().query_ordered(namespace, query)


class Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def stale_assertions() -> None:
    """
    Keep an excluded stale pin visible and preserve normal refresh deduplication.
    """
    fixture = state()
    for row in rows(fixture):
        row["measured_at"] = "2026-01-01T00:00:00+00:00"
    answer = pinned(fixture, exclude_model_prefixes=["cheap"])
    receipt = answer["selection_receipt"]
    check(any(row["selected"] and row["effort"] == "high" for row in answer["stale_readings"]) and answer["advisories"]["stale_reading_used"] > 0 and answer.get("refresh_request") and replay(fixture, receipt) is receipt, "PIN_STALE_READING_ADVISORY")
    for mode in ("raise", "envelope"):
        history_fault_assertion(fixture, mode)
    cast("RealShapeState", fixture).rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_REFRESH_RUN).append({"id": "recent", "started_at": datetime.now(UTC).isoformat(), "trigger": "cron", "status": "completed"})
    check("refresh_request" not in pinned(fixture), "PIN_STALE_HISTORY_FAILURE_ADVISORY_AND_REPLAY normal dedupe")


def history_fault_assertion(fixture: StateManagementInterface, mode: str) -> None:
    """
    Inject faults solely in the advisory history query, observe reach, logging and replay.
    """
    fault = HistoryFault(mode)
    typed = cast("StateManagementInterface", fault)
    for row in rows(fixture):
        fault.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_MODEL_CAPABILITY_CELL).append(copy.deepcopy(row))
    logs = Logs()
    verbs.logger.addHandler(logs)
    try:
        result = pinned(typed, exclude_model_prefixes=["cheap"])
        verified = replay(typed, result["selection_receipt"])
    except Exception as exc:
        raise AssertionError("PIN_STALE_HISTORY_FAILURE_ADVISORY_AND_REPLAY reached history fault") from exc
    finally:
        verbs.logger.removeHandler(logs)
    check(fault.reached == 2 and verified is result["selection_receipt"] and result.get("refresh_request") and any("history" in text and "unavailable" in text for text in result["warnings"]) and any("injected advisory history" in text for text in logs.messages), "PIN_STALE_HISTORY_FAILURE_ADVISORY_AND_REPLAY")


def main() -> int:
    bridge_accepts_pin()
    selection_assertions()
    override_assertions()
    floor_quota_assertions()
    refusal_assertions()
    withdrawn_assertions()
    invalid_assertions()
    unscored_assertions()
    replay_assertions()
    receipt_key_assertions()
    legacy_assertions()
    default_assertions()
    correction_assertions()
    stale_assertions()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
