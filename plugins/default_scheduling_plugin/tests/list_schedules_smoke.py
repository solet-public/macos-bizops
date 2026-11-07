#!/usr/bin/env python3
"""Smoke — `default_scheduling_plugin.list_schedules` enumerate-all introspection verb.

Background: the scheduler had `get_schedules_by_tag` (tag-scoped) and the clear
verbs, but NO enumerate-all verb — so a schedule whose tag/id you do not already
know was unfindable (operator could not locate a stale 'OVERNIGHT sweep' cron to
clear it). `list_schedules` is the tag-agnostic companion: it returns ALL
schedules (optionally narrowed by status), read-only.

This smoke proves the NEW code's responsibility — the projection + optional
status-filter + count over `_load_schedules()`'s output, and the dual-form
(plugin:: + service_interface::) registration. `_load_schedules` is overridden
with a realistic fixture (the dict shape `ScheduleData.model_dump()` produces),
the established fake-source/real-logic unit boundary — `_load_schedules` itself
is exercised elsewhere. The LIVE end-to-end (real schedules table + the stale
cron surfacing) is verified post-restart via `process_call` per the
create-process skill Step 5; this smoke is the slice-ready, restart-free proof.

Project policy: no pytest. Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import logging
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import default_scheduling_plugin  # noqa: E402
from ananta.services.store import open_store  # noqa: E402
from default_scheduling_plugin.models import ActionData, ScheduleData  # noqa: E402
from default_scheduling_plugin.plugin import SchedulingPlugin  # noqa: E402

import agent_messaging_plugin  # noqa: E402
import ananta  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_dispatch import _resolve_peer_recipient  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import PEER_BINDING_NAMESPACE, get_peer_binding_schema  # noqa: E402

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


# A realistic `_load_schedules()` output: three schedules, one mimicking the
# stale 'OVERNIGHT sweep' cron the operator needs to find by enumeration (it
# carries a tag/id the caller does not already know).
_FIXTURE: dict[str, Any] = {
    "next_id": 1003,
    "scheduled_actions": {
        "1000": {
            "type": "recurring",
            "status": "scheduled",
            "label": "Coordinator-Dusk OVERNIGHT autonomous sweep",
            "cron_expression": "0 * * * *",
            "run_at": None,
            "tags": ["coordinator:dusk:overnight_sweep"],
            "session_id": None,
            "flow_id": "flow-dusk-sweep",
        },
        "1001": {
            "type": "recurring",
            "status": "scheduled",
            "label": "Ledger periodic poll",
            "cron_expression": "*/5 * * * *",
            "run_at": None,
            "tags": ["ledger:periodic_poll"],
            "session_id": None,
            "flow_id": "flow-ledger-poll",
        },
        "1002": {
            "type": "one_time",
            "status": "completed",
            "label": "one-shot check-in",
            "cron_expression": None,
            "run_at": "2026-06-21T00:00:00+00:00",
            "tags": ["followup:job-x"],
            "session_id": "session-abc",
            "flow_id": "flow-x",
        },
    },
}

_PROJECTED_KEYS = {
    "schedule_id",
    "type",
    "status",
    "label",
    "cron_expression",
    "run_at",
    "tags",
    "session_id",
    "flow_id",
}


def _build_plugin() -> SchedulingPlugin:
    plugin = SchedulingPlugin()
    plugin.logger = logging.getLogger("list_schedules_smoke")
    # Target the projection/filter logic, not the loader: _load_schedules is
    # existing, separately-exercised code. (fake-source / real-logic boundary)
    plugin._load_schedules = lambda: _FIXTURE  # type: ignore[method-assign]
    return plugin


def check_list_actions() -> None:
    """Exercise the existing projection before any newly added API."""
    expected_actions = [
        {
            "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
            "arguments": {"name": "Scheduling-Test", "content": "  Original \u03bb\ntext\n"},
            "result_processor": {"process_key": "fixture::result", "arguments": {"value": [1, "two"]}},
            "result_processor_kind": "deterministic_continuation",
        }
    ]
    fixture = deepcopy(_FIXTURE)
    fixture["scheduled_actions"]["1000"]["actions"] = [
        {
            "name": action["process_key"],
            "parameters": action["arguments"],
            "result_processor": action["result_processor"],
            "result_processor_kind": action["result_processor_kind"],
        }
        for action in expected_actions
    ]
    plugin = _build_plugin()
    plugin._load_schedules = lambda: fixture  # type: ignore[method-assign]
    result = plugin.list_schedules({}, {})
    row = next(item for item in result["data"]["schedules"] if item["schedule_id"] == "1000")
    _check(row.get("action_definitions") == expected_actions, "list_actions_match_storage")


def check_peer_hint() -> None:
    """A session hint cannot override the transport's sole peer-kind binding."""
    registry = PeerRegistry(bindings_store=open_store(get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory"))
    registry.register(BridgeBinding(bridge_id="bridge-a", agent_id="fixture", agent_instance_id="instance-a", agent_session_id="session-a", session_label="fixture-a", parent_pid=0))
    plugin = SchedulingPlugin()
    plugin.configure_memory_store()
    repo = plugin._memory_repository
    value = ScheduleData(label="peer hint", type="recurring", cron_expression="0 * * * *", actions=[ActionData(name="plugin::agent_messaging_plugin::send_peer_message", parameters={"peer_id": "fixture", "peer_agent_session_id": "session-b", "content": "  original \u03bb\ntext\r\n"})])
    sid = repo.save_schedule(value)
    before = (repo.load_schedule(sid), repo.history(sid), repo.epoch())
    listed = plugin.list_schedules({}, {})["data"]["schedules"][0]
    result = plugin.rebind_scheduled_action({"schedule_id": sid, "action_index": 0, "target_kind": "session", "target_value": "session-c", "expected_revision": 0, "reason": "correct recipient"}, {})
    current = repo.load_schedule(sid)
    assert current is not None
    arguments = current.actions[0].parameters
    recipient = _resolve_peer_recipient(peer_registry=registry, peer_id=arguments["peer_id"], peer_agent_instance_id=arguments.get("peer_agent_instance_id"), peer_agent_session_id=arguments.get("peer_agent_session_id"))
    _check(result["action_status"] == "error" or recipient.agent_session_id == "session-c", "session_hint_rebind_matches_dispatch")
    _check((result.get("error") or {}).get("code") == "scheduling.unsupported_destination" and (current, repo.history(sid), repo.epoch()) == before, "session_hint_refusal_no_writes")
    target = listed["targets"][0]
    _check(not target["supported"] and target["kind"] == "unknown" and target["value"] is None and target["advisory"] == "session_hint_is_fallback", "session_hint_not_effective_destination")
    _check(listed["action_definitions"][0]["arguments"] == value.actions[0].parameters and recipient.agent_session_id == "session-a", "session_hint_action_and_routing_preserved")


def main() -> int:
    print("list_schedules enumerate-all introspection smoke")
    print("================================================")

    check_list_actions()
    for module in (ananta, default_scheduling_plugin, agent_messaging_plugin):
        _check(Path(str(module.__file__)).resolve().is_relative_to(REPO_ROOT), "source_provenance_" + module.__name__)
    check_peer_hint()

    plugin = _build_plugin()

    # (1) Unfiltered: returns EVERY schedule with the canonical 9-field shape.
    res_all = plugin.list_schedules({}, {})
    _check(res_all.get("action_status") == "completed", "unfiltered call completes")
    data_all = res_all.get("data", {})
    _check(data_all.get("count") == 3, f"count == 3 (got {data_all.get('count')})")
    _check(len(data_all.get("schedules", [])) == 3, "returns all 3 schedules")
    _check(data_all.get("status_filter") is None, "status_filter is None when unfiltered")
    entry = next(
        (e for e in data_all.get("schedules", []) if e.get("schedule_id") == "1000"),
        None,
    )
    _check(entry is not None, "the stale OVERNIGHT-sweep schedule is enumerated")
    if entry is not None:
        _check(_PROJECTED_KEYS <= set(entry.keys()), f"entry preserves the 9 existing projected keys (got {sorted(entry.keys())})")
        _check(
            entry.get("tags") == ["coordinator:dusk:overnight_sweep"],
            "stale schedule surfaces its real tag (the unblock — caller now has the tag/id to clear)",
        )
        _check(entry.get("label") == "Coordinator-Dusk OVERNIGHT autonomous sweep", "label projected")
        _check(entry.get("cron_expression") == "0 * * * *", "cron_expression projected")

    # (2) Status filter narrows to matching schedules and echoes the filter.
    res_sched = plugin.list_schedules({"status": "scheduled"}, {})
    data_sched = res_sched.get("data", {})
    _check(data_sched.get("count") == 2, f"status='scheduled' returns 2 (got {data_sched.get('count')})")
    _check(data_sched.get("status_filter") == "scheduled", "status_filter echoed")
    _check(
        all(e.get("status") == "scheduled" for e in data_sched.get("schedules", [])),
        "every returned entry matches the status filter",
    )

    res_done = plugin.list_schedules({"status": "completed"}, {})
    _check(res_done.get("data", {}).get("count") == 1, "status='completed' returns 1")

    # (3) Dual-form registration: the service_interface:: form rides an
    # EdgeProcessDefinition (decorated<->declared parity — customizations are
    # optional since the 2026-07-15 frontier-first relax).
    edge_defs = plugin.get_edge_process_definitions()
    _check("list_schedules" in edge_defs, "list_schedules registered as an EdgeProcessDefinition (service_interface:: form)")

    print("\n------------------------------------------------")
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    if _failed:
        print("\nFailures:")
        for label in _failed:
            print(f"  - {label}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
