"""NM-2 interface setup through public owners and qualified listener residuals.

The bridge's public port allocator binds a wildcard address, including
when preferred=0 (port_manager.py:235,251). That cannot satisfy the exact
127.0.0.1:0-only rule. Its dependent rows use the design's R6 alternative.
Discord/Signal listeners require external startup; public binding helpers
are still exercised and seeded without replacing their state service.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol, cast

PREPARER_NAME = "interfaces"
PLUGINS = frozenset({"agent_messaging_plugin", "iterm2_coding_agent_management_plugin", "macos_coding_agent_session_plugin", "discord_plugin", "signal_plugin"})
RUN_THREADS = frozenset({"agent_messaging_plugin-server"})
LOOPBACK_LISTENER = True
# R6 wins over this opt-in: listeners in RESIDUAL must never be invoked.
MEASURE_EXCLUDED = frozenset((plugin, "start_interface") for plugin in ("agent_messaging_plugin", "discord_plugin", "signal_plugin"))
_BRIDGE_VERBS = (
    "deliver_error",
    "deliver_job_completion",
    "deliver_result",
    "peer_ack_role_read_page",
    "peer_inbox",
    "peer_list",
    "peer_mark_role_covered",
    "peer_role_read_receipts",
    "peer_send_by_name",
    "post_message",
    "retire_role",
    "send_peer_message",
    "set_autonomic_slot",
    "start_interface",
)
_ITERM_VERBS = ("promote_primary", "restart_session", "retire_session", "terminate_session")
_MACOS_VERBS = ("list_bridges", "restart_bridge", "spawn_bridge", "terminate_bridge")
RESIDUAL: Mapping[tuple[str, str], str] = {
    **{("agent_messaging_plugin", verb): "R6" for verb in _BRIDGE_VERBS},
    **{("iterm2_coding_agent_management_plugin", verb): "R6" for verb in _ITERM_VERBS},
    **{(plugin, verb): "R6" for plugin in ("discord_plugin", "signal_plugin") for verb in ("post_message", "start_interface")},
}
INTERFACE_VERBS = (
    frozenset(RESIDUAL)
    | frozenset(("macos_coding_agent_session_plugin", verb) for verb in _MACOS_VERBS)
    | {
        ("agent_messaging_plugin", "peer_role_read_receipts"),
        ("agent_messaging_plugin", "read_unit_usage"),
    }
)


class CensusContextView(Protocol):
    """Local copy of design 4.1; no imports from the parallel U1 lane."""

    app_home: Path
    export_root: Path

    def get_service(self, name: str) -> object | None: ...
    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str: ...


def configure(plugin: str, home: Path, export_root: Path) -> dict[str, object]:
    """Keep configuration local; no credentials, live addresses or host files."""
    del export_root
    if plugin == "agent_messaging_plugin":
        return {"host": "127.0.0.1", "port": 0, "streamable_enabled": False, "allowed_working_directory_roots": [str(home)]}
    if plugin == "signal_plugin":
        return {"backend_endpoint": "https://signal.census.invalid", "allowed_signal_addresses": ["+12025550123"]}
    if plugin == "discord_plugin":
        return {"allowed_guild_ids": ["100000000000000001"], "allowed_channel_ids": ["100000000000000002"]}
    return {}


def _channel_binding(plugin: str, context: CensusContextView) -> dict[str, str]:
    state = context.get_service("state_service")
    sessions = context.get_service("session_manager")
    if state is None or sessions is None:
        return {"prerequisite": "public session_binding setup requires real isolated state_service and session_manager; U1 integration"}
    binding: dict[str, Any] | None
    if plugin == "discord_plugin":
        from discord_plugin.session_binding import get_or_create_binding

        binding = get_or_create_binding(state, sessions, "100000000000000002", None, "100000000000000003", "100000000000000001", "100000000000000004", "census-fixture", None)
    else:
        from signal_plugin.session_binding import get_or_create_binding as signal_binding

        binding = signal_binding(cast(Any, state), cast(Any, sessions), "+12025550123", "census-message", plugin)
    if not binding or not binding.get("session_id"):
        return {"prerequisite": f"{plugin}: public binding helper returned no session; listener remains R6"}
    return {"session_id": str(binding["session_id"]), "binding_id": str(binding.get("id") or "")}


def seed(plugin: str, context: CensusContextView) -> dict[str, str]:
    """Use only public setup paths; missing U1 services stay named advisories.

    The optional coding-agent service lifecycle is used only when its real
    provider exposes it. No tracker or private plugin attribute is fabricated.
    The caller owns readiness and terminal shutdown of its context providers.
    """
    if plugin == "agent_messaging_plugin":
        return _managed_rows(context)
    if plugin in {"discord_plugin", "signal_plugin"}:
        return _channel_binding(plugin, context)
    if plugin == "macos_coding_agent_session_plugin":
        service = context.get_service("coding_agent_session_service")
        readiness = getattr(service, "prepare_for_readiness", None)
        if callable(readiness):
            readiness()
            return {"readiness": "public provider lifecycle; watcher thread remains parked"}
        return {"prerequisite": "coding_agent_session_service provider readiness requires U1 context lifecycle"}
    return {"prerequisite": "R6: bridge port allocator requires a wildcard bind forbidden by the exact loopback guard"}


def _managed_rows(context: CensusContextView) -> dict[str, str]:
    """Create ledger-only synthetic identities; no host or bridge is started."""
    from agent_messaging_plugin.accepted_authorization_verbs import record_accepted_authorization
    from agent_messaging_plugin.gauge_canary_store import register_canary
    from agent_messaging_plugin.held_authorization_store import record_held_authorization
    from agent_messaging_plugin.memory_curation_verbs import slug_to_slot_tag
    from agent_messaging_plugin.model_capability_verbs import record_model_capability_cell, record_pool_reading, withdraw_model_capability_cell
    from agent_messaging_plugin.retired_role_registry import seed_retired_role_registry
    from agent_messaging_plugin.session_lifecycle_store import ManagedSessionSpec, insert_managed_session, transition_lifecycle_state

    state = cast(Any, context.get_service("state_service"))
    seeded = {"agent_instance_id": "agi-3a99b943722941aca0ac8243261cab96", "agent_session_id": "ases-893d793ef62b4c619a972d98d697f485", "since": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), "until": datetime.now(UTC).isoformat()}
    insert_managed_session(state, ManagedSessionSpec(agent_instance_id=seeded["agent_instance_id"], lane_id="census-local", assignment=None, work_class="analysis_deliverable", budget_line="census", host="synthetic", provisioning_mode="synthetic_no_process", agent_runtime="codex"))
    result = state.update_state("agent_messaging_plugin", {"table": "managed_session", "filters": {"agent_instance_id": seeded["agent_instance_id"]}}, {"agent_session_id": seeded["agent_session_id"]})
    if result.get("action_status") != "completed":
        raise RuntimeError(f"managed identity seed failed: {result}")
    for verb in ("clear_session", "compact_session", "drive_session", "retire_session", "terminate_session", "retire_completed_lane_sessions"):
        instance_id = f"agi-census-{verb}"
        insert_managed_session(state, ManagedSessionSpec(agent_instance_id=instance_id, lane_id="census-local", assignment=None, work_class="analysis_deliverable", budget_line="census", host="synthetic", provisioning_mode="synthetic_no_process", agent_runtime="codex"))
        transition_lifecycle_state(state, agent_instance_id=instance_id, from_state="spawning", to_state="live", directed_by="census", reason="owned no-process target")
        seeded[verb] = instance_id
    authorization = record_accepted_authorization(state, {"requesting_peer": "census", "owed_by_role": "census", "branch_or_request_ref": "census", "authorizing_message_id": "census-owned-message", "act": "inspect scratch", "predicate": "owned fixture", "actor": "census", "reason": "fixture setup"})
    seeded["authorization_id"] = str(authorization["entry_id"])
    seeded["held_authorization_id"] = record_held_authorization(state, requesting_peer="census", owed_by_role="census", branch_or_request_ref="census", reason="owned lifecycle fixture")
    for index, verb in enumerate(("arrest_gauge_canary", "register_synthetic_session", "retire_gauge_canary")):
        instance_id = f"agi-26be87e10bcb4aa09ea0a17b6c5da8a{index}"
        register_canary(state, agent_instance_id=instance_id, purpose="owned census row only", registered_by="census")
        seeded[verb] = instance_id
    record_pool_reading(state, {"profile_id": "anthropic-max-20x-2026-08-22", "pool_id": "rolling-five-hour", "consumed": 10.0, "remaining": 90.0, "as_of": seeded["since"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "source": "owned synthetic reading", "recorded_by": "census"})
    job = cast(Any, context.get_service("async_job_manager")).create_job(plugin_name="agent_messaging_plugin", action_name="rotate_session", request_data={"notes": "status fixture only; worker parked"})
    if job.get("action_status") != "completed":
        raise RuntimeError(f"real choreography job seed failed: {job}")
    seeded["job_id"] = str(job["data"]["job_id"])
    seeded["memory_slug"] = "census-memory"
    memory = cast(Any, context.get_service("memory_service"))
    memory.remember(content="owned census reinforcement target", tags=[slug_to_slot_tag(os.environ["SOLET_NAME"], seeded["memory_slug"])], embed=False)
    seed_retired_role_registry(state)
    for model in ("census-select", "census-withdraw", "census-reinstate"):
        record_model_capability_cell(state, {"runtime": "codex", "model": model, "effort": "medium", "capability_score": 90.0, "cost_per_task_usd": 0.01, "source_ref": "https://census.invalid/owned-reading", "note": "synthetic isolated census cell"})
    withdraw_model_capability_cell(state, {"runtime": "codex", "model": "census-reinstate", "effort": "medium", "expected_acceptance": "accepted", "actor": "census", "reason": "prepare reversal fixture"})
    return seeded


def handler_state(plugin: str, verb: str, state: dict[str, object], seeded: Mapping[str, str]) -> dict[str, object]:
    """Use the platform's public server-side identity constructors, not args."""
    authenticated = {"resolve_caller_provenance", "report_managed_dispatch", "preserve_lane_evidence", "provision_role_session", "qualify_fleet", "read_lane_evidence", "set_model_catalog_policy", "set_model_catalog_source", "set_model_cells_enabled", "resolve_managed_dispatch", "dispatch_managed_work", "spawn_session"}
    if plugin != "agent_messaging_plugin" or verb not in authenticated:
        return state
    from ananta.core.services.call_context import CallContext
    from ananta.llm.session_ledger.trigger_data import build_process_call_trigger_data
    from ananta.llm.session_ledger.types import AuthenticatedPrincipal

    principal = AuthenticatedPrincipal(client_id="census-owned-client", agent_id="codex", agent_instance_id=seeded["agent_instance_id"], bridge_id="census-owned-bridge", session_id=seeded["agent_session_id"])
    trigger = build_process_call_trigger_data(principal=principal, bridge_id=principal.bridge_id, session_id=principal.session_id, process_key="plugin::agent_messaging_plugin::census", reason="isolated census fixture", source_namespace=plugin, deliver_result_process_key="", deliver_error_process_key="")
    return {**state, "session_id": principal.session_id, "authenticated_principal": trigger["authenticated_principal"], "call_context": CallContext.for_external(principal.client_id), "inference_vertex_session_id": principal.agent_instance_id}


def _managed_parameters(verb: str, params: dict[str, object], seeded: Mapping[str, str], home: Path) -> dict[str, object]:
    """Match validators with ordinary values and keep optional inputs explicit."""
    now = datetime.now(UTC).isoformat()
    values: dict[str, object] = {
        "runtime": "codex",
        "model": "census-select",
        "effort": "medium",
        "provider": "openai",
        "kind": "policy",
        "trigger": "manual",
        "role_class": "principal",
        "work_class": "analysis_deliverable",
        "status": "working",
        "captured_at": now,
        "retired_at": now,
        "checked_at": now,
        "measured_at": now,
        "observed_at": now,
        "reviewed_at": now,
        "as_of": now,
        "since": seeded["since"],
        "until": seeded["until"],
        "stuck": [],
        "actions": [],
        "escalations": [],
        "condition_kind": "deadline",
        "condition_ref": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        "expected_acceptance": "accepted",
        "agent_instance_id": seeded["agent_instance_id"],
        "agent_session_id": seeded["agent_session_id"],
    }
    result = {key: values.get(key, value) for key, value in params.items()}
    additions: dict[str, dict[str, object]] = {
        "list_sessions": {"live_only": True},
        "read_session_usage_window": {"vendor": "codex", "since": seeded["since"], "until": seeded["until"]},
        "record_model_capability_cell": {"capability_score": 90.0, "cost_per_task_usd": 0.01, "source_ref": "https://census.invalid/owned-reading"},
        "reinstate_model_capability_cell": {"model": "census-reinstate", "expected_acceptance": "withdrawn"},
        "withdraw_model_capability_cell": {"model": "census-withdraw", "expected_acceptance": "accepted"},
        "get_authorization_history": {"authorizing_message_id": "census-owned-message"},
        "change_accepted_authorization": {"entry_id": seeded["authorization_id"], "expected_version": 1, "operation": "correct", "notes": "corrected owned fixture"},
        "check_choreography_job_status": {"job_id": seeded["job_id"]},
        "reinforce_by_slug": {"slug": seeded["memory_slug"]},
        "retire_held_authorization": {"entry_id": seeded["held_authorization_id"]},
        "retire_completed_lane_sessions": {"agent_instance_ids": [seeded["retire_completed_lane_sessions"]]},
        "read_unit_usage": {"agent_instance_id": seeded["agent_instance_id"]},
        "record_allowance_pool_reading": {"profile_id": "anthropic-max-20x-2026-08-22", "pool_id": "rolling-five-hour", "consumed": 10.0, "remaining": 90.0, "as_of": seeded["since"], "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
        "retract_allowance_pool_reading": {"profile_id": "anthropic-max-20x-2026-08-22", "pool_id": "rolling-five-hour"},
        "arrest_gauge_canary": {"arrest_from": seeded["since"], "arrest_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat(), "expected_notice_type": "gauge_stale_notice"},
        "register_synthetic_session": {"lane_id": "ledger-lane", "report_by_seconds": 300},
        "spawn_session": {"working_directory": str(home), "role_class": "ephemeral"},
        "dispatch_managed_work": {"working_directory": str(home), "role_class": "ephemeral"},
    }
    if verb == "get_authorization_history":
        result.pop("entry_id", None)
    if verb == "read_unit_usage":
        result.pop("unit_id", None)
    result.update(additions.get(verb, {}))
    if verb in seeded:
        result["agent_instance_id"] = seeded[verb]
    return result


def parameters(plugin: str, verb: str, params: dict[str, object], seeded: Mapping[str, str], home: Path) -> dict[str, object]:
    """Supply handler-valid identities without mutating shared parameter input."""
    if plugin == "agent_messaging_plugin" and seeded.get("agent_instance_id"):
        return _managed_parameters(verb, params, seeded, home)
    return _interface_parameters(plugin, verb, params, seeded)


def _interface_parameters(plugin: str, verb: str, params: dict[str, object], seeded: Mapping[str, str]) -> dict[str, object]:
    """Preserve the existing listener and optional-provider input contract."""
    result = dict(params)
    if (plugin, verb) not in INTERFACE_VERBS:
        return result
    if plugin == "agent_messaging_plugin" and verb == "read_unit_usage":
        result.pop("unit_id", None)
        result["agent_instance_id"] = "agi-census-fixture"
    elif plugin == "agent_messaging_plugin" and verb == "peer_role_read_receipts":
        result.update(agent_session_id="ases-census-fixture", candidates=[])
    else:
        result.update(agent_instance_id="agi-census-fixture", solet_name="census-fixture")
    if seeded.get("session_id"):
        result["session_id"] = seeded["session_id"]
    if verb == "start_interface" and plugin == "signal_plugin":
        result["backend_endpoint"] = "https://signal.census.invalid"
    return result


def prove(root: Path, plugin_class: type[object], function_name: str) -> dict[str, object]:
    """Use the same source-only prover for the explicitly named R6 rows.

    Keep modules independently discoverable: the only shared implementation
    is U4's residual prover, loaded when U1 requests an R6 proof.
    """
    from fast_return_census_preparers.residual import prove as residual_proof

    return residual_proof(root, plugin_class, function_name)
