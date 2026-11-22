#!/usr/bin/env python3
"""Acceptance smoke for the capability-only dispatch selector contract."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from dispatch_pin_smoke import NOW, PIN, selector_surface_refuses, state  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402

EXPECTED_PARAMETERS = {
    "required_score",
    "runtime",
    "pin",
    "pin_basis",
    "effort_ceiling",
    "exclude_model_prefixes",
    "exclude_runtimes",
    "score_margin",
    "cost_tolerance",
    "max_staleness_hours",
}


def check(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def select(fixture: StateManagementInterface, **kwargs: Any) -> dict[str, Any]:
    return verbs.select_dispatch_tier(fixture, {"required_score": 42, "max_staleness_hours": 72, **kwargs}, now=NOW)


def main() -> int:
    metadata = cast(Any, AgentMessagingPlugin.select_dispatch_tier)._platform_process_metadata
    check(set(metadata.parameters) == EXPECTED_PARAMETERS, "declared parameters are capability plus narrowing only")

    plain = select(state())
    check(plain["selected"] is not None, "required_score alone selects a cell")
    retired_result_fields = {"selection_" + "receipt", "policy_version", "capability_" + "floors"}
    check(not retired_result_fields & plain.keys(), "answer has no retired selector fields")
    check("capability_" + "floor_disallowed" not in plain["excluded"], "answer has no capability floor exclusion")
    check("pin_" + "floor_disallowed" not in plain["advisories"], "answer has no pin floor advisory")

    check(select(state(), runtime="claude_code")["selected"]["runtime"] == "claude_code", "runtime narrows the selected cell")
    pinned = select(state(), pin=PIN, pin_basis="capability-only smoke")
    check({key: pinned["selected"][key] for key in PIN} == PIN, "pin narrows to the named accepted cell")
    check(select(state(), effort_ceiling="low")["selected"]["effort"] == "low", "effort ceiling narrows the answer")
    check(select(state(), exclude_model_prefixes=["cheap"])["selected"]["model"] == "other-model", "model-prefix exclusion narrows the answer")
    check(select(state(), exclude_runtimes=["claude_code"])["selected"]["runtime"] == "codex", "runtime exclusion narrows the answer")

    check(selector_surface_refuses("dispatch_" + "kind", "review"), "dispatch kind is refused as unknown")
    check(selector_surface_refuses("scope_" + "tags", ["state_schema"]), "scope tags are refused as unknown")
    check(selector_surface_refuses("billing_objective", "metered_usd"), "billing_objective is refused as unknown")

    check(not hasattr(verbs, "verify_" + "selection_" + "receipt"), "selection receipt verifier is absent")
    try:
        importlib.import_module("agent_messaging_plugin.model_dispatch_policy")
    except ModuleNotFoundError:
        policy_absent = True
    else:
        policy_absent = False
    check(policy_absent, "retired model dispatch policy module is absent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
