"""Capped coding-agent plugin-list fixture exercised by executable hydration."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any


def run_plugin_list_output_cap(
    target: Path,
    runtime: Any,
    *,
    plugin_root: Path,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
    bounded_outcome: Callable[..., Any],
    operation_handlers: Callable[[], dict[str, Any]],
) -> None:
    """Exercise the production capped capture path with closed fake vectors."""

    from github_midwife_plugin import setup_plugin_operations
    from github_midwife_plugin.installation_plugin_doctor import (
        selected_hooks,
        selected_plugins,
    )

    selector = "coordination-hooks@iris"
    codex_root = target / "plugins/github_midwife_plugin/codex_plugin/coordination-hooks"
    claude_root = runtime.home / ".claude/plugins/cache/iris/coordination-hooks/fixture"
    shutil.copytree(plugin_root / "codex_plugin/coordination-hooks", codex_root)
    shutil.copytree(plugin_root / "claude_plugin/coordination-hooks", claude_root)
    for manifest in (codex_root / "hooks/hooks.json", claude_root / "hooks/hooks.json"):
        manifest.write_text(
            manifest.read_text(encoding="utf-8").replace(
                '"command": "python3',
                f'"command": "{target}/.venv/bin/python3',
            ),
            encoding="utf-8",
        )
    registry = runtime.home / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(
        json.dumps({"plugins": {selector: [{"installPath": str(claude_root)}]}}),
        encoding="utf-8",
    )

    codex_vector = ("/fixture/codex", "plugin", "list", "-m", "iris", "--json")
    claude_vector = ("/fixture/claude", "plugin", "list", "--json")
    codex_row = {
        "pluginId": selector,
        "installed": True,
        "enabled": True,
        "source": {"path": str(codex_root)},
    }
    claude_row = {"id": selector, "enabled": True, "installPath": str(claude_root)}
    oversized = json.dumps({"installed": [{"filler": "x" * 5_000}, codex_row]})
    capped = bounded_outcome(
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout=oversized,
        stderr="",
    )
    check(capped.stdout_truncated, "production helper marks oversized stdout")
    check(capped.stdout_bytes == len(oversized.encode("utf-8")), "production helper keeps byte count")

    install_request = request(
        target,
        operation_id="install_codex_plugin",
        operation_ref="hydration::codex.install_plugin",
    )
    unfiltered_vector = ("/fixture/codex", "plugin", "list", "--json")
    runtime.responses[unfiltered_vector] = command_outcome(0, False, 1, oversized[:4096], "")
    production_vector = setup_plugin_operations.plugin_list_vector
    setup_plugin_operations.plugin_list_vector = (
        lambda _cli, _marketplace, _executable=None: unfiltered_vector
    )
    try:
        legacy = operation_handlers()[install_request.operation_ref](install_request, runtime)
    finally:
        setup_plugin_operations.plugin_list_vector = production_vector
    check(legacy["checkpoint_status"] == "pending", "legacy capped unfiltered scan is red")

    runtime.responses[codex_vector] = capped
    truncated = operation_handlers()[install_request.operation_ref](install_request, runtime)
    check(truncated["checkpoint_status"] == "blocked", "truncated list blocks")
    check(truncated["error_kind"] == "output_truncated", "truncated list names stable kind")
    check("stdout" in str(truncated["repair"]), "truncated list repair includes size observation")

    runtime.responses[codex_vector] = bounded_outcome(
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout=json.dumps({"installed": [codex_row]}),
        stderr="",
    )
    verified = operation_handlers()[install_request.operation_ref](install_request, runtime)
    check(verified["checkpoint_status"] == "verified", "filtered structural Codex list is green")

    post_apply = request(
        target,
        operation_id="install_codex_plugin",
        operation_ref="hydration::codex.install_plugin",
        probe_purpose="post_apply",
    )
    runtime.responses[codex_vector] = bounded_outcome(
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout='{"installed": []}',
        stderr="",
    )
    post_apply_result = operation_handlers()[post_apply.operation_ref](post_apply, runtime)
    check(post_apply_result["checkpoint_status"] == "blocked", "post-apply absence blocks")
    check(post_apply_result["error_kind"] == "plugin_not_visible", "post-apply absence is observed")
    check("Post-apply probe observed" in str(post_apply_result["repair"]), "post-apply repair is current")
    check("stdout 17 bytes" in str(post_apply_result["repair"]), "post-apply repair includes size")

    runtime.responses[codex_vector] = bounded_outcome(
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout=json.dumps({"installed": [codex_row]}),
        stderr="",
    )
    runtime.responses[claude_vector] = bounded_outcome(
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout=json.dumps([claude_row]),
        stderr="",
    )
    completion = request(
        target,
        operation_id="coding_agent_plugins",
        operation_ref="setup::coding_agents.verify_plugins",
        public_inputs={"selected_coding_agents": ["codex", "claude_code"]},
    )
    completion_result = selected_plugins(completion, runtime)
    check(completion_result["checkpoint_status"] == "verified", "doctor completion uses structural lists")

    runtime.responses[codex_vector] = capped
    doctor_truncated = selected_plugins(completion, runtime)
    check(doctor_truncated["checkpoint_status"] == "blocked", "doctor refuses capped JSON")
    check(doctor_truncated["error_kind"] == "output_truncated", "doctor preserves truncation kind")

    runtime.responses[codex_vector] = bounded_outcome(
        returncode=0,
        timed_out=False,
        duration_ms=1,
        stdout=json.dumps({"installed": [codex_row]}),
        stderr="",
    )
    commands_before = len(runtime.commands)
    codex_only = request(
        target,
        operation_id="coding_agent_plugins",
        operation_ref="setup::coding_agents.verify_plugins",
        public_inputs={"selected_coding_agents": ["codex"]},
    )
    codex_only_result = selected_plugins(codex_only, runtime)
    check(
        codex_only_result["checkpoint_status"] == "verified",
        "selected Codex plugin verifies without Claude artifacts",
    )
    check(
        all(command[0] != "claude" for command in runtime.commands[commands_before:]),
        "one-agent plugin aggregate never probes the unselected Claude client",
    )

    codex_hook = codex_root / "hooks/step_zero_reminder.js"
    runtime.responses[("/fixture/node", str(codex_hook))] = command_outcome(0, False, 1, "ok", "")
    commands_before = len(runtime.commands)
    codex_hooks_only = request(
        target,
        operation_id="fresh_session_hooks_active",
        operation_ref="setup::coding_agents.verify_hooks",
        public_inputs={"selected_coding_agents": ["codex"]},
    )
    codex_hooks_result = selected_hooks(codex_hooks_only, runtime)
    check(
        codex_hooks_result["checkpoint_status"] == "verified",
        "selected Codex hook verifies without Claude artifacts",
    )
    check(
        all(command[0] != "claude" for command in runtime.commands[commands_before:]),
        "one-agent hook aggregate never probes the unselected Claude client",
    )


def run_genesis_profile_projection(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
) -> None:
    """Assert the genesis operation receives the resolved profile decisions."""

    from github_midwife_plugin.setup_adapter import dispatch_request

    python = target / ".venv" / "bin" / "python3"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    apply_request = request(
        target,
        operation_id="run_genesis",
        operation_ref="genesis::solet.run",
        phase="apply",
        probe_purpose=None,
        approval_fingerprint="sha256:" + "c" * 64,
        dry_run=False,
        public_inputs={
            "solet_name": "iris",
            "clone_directory": str(target),
            "setup_profile": "macos-bizops",
            "autostart": "enabled",
        },
    )
    response = dispatch_request(apply_request, runtime)
    check(response["checkpoint_status"] == "applied", "genesis adapter apply succeeds")
    check(
        runtime.command_environments[-1].get("SOLET_PROFILE") == "macos-bizops",
        "genesis adapter passes the projected setup profile as SOLET_PROFILE",
    )
    check(
        runtime.command_environments[-1].get("SOLET_AUTOSTART") == "enabled",
        "genesis adapter passes the resolved autostart decision",
    )

    missing_profile = request(
        target,
        operation_id="run_genesis",
        operation_ref="genesis::solet.run",
        phase="apply",
        probe_purpose=None,
        approval_fingerprint="sha256:" + "c" * 64,
        dry_run=False,
        public_inputs={
            "solet_name": "iris",
            "clone_directory": str(target),
            "setup_profile": "macos-bizops",
        },
    )
    blocked = dispatch_request(missing_profile, runtime)
    check(
        blocked["checkpoint_status"] == "blocked"
        and blocked["error_kind"] == "adapter_protocol_error",
        "genesis adapter rejects a request without the resolved autostart decision",
    )


def run_session_retrieval_envelope(
    target: Path,
    runtime: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
) -> Any:
    """Reject nested session qualifications while accepting the public envelope."""

    from github_midwife_plugin.setup_adapter import dispatch_request

    probe_request = request(
        target,
        operation_id="session_sources_retrievable",
        operation_ref="service_interface::session_ledger_service.qualify_selected_sources",
        probe_purpose="completion",
    )
    qualification_vector = (
        str(target / ".venv/bin/solet-bridge"),
        "call",
        "service_interface::session_ledger_service::qualify_selected_sources",
        json.dumps(
            {
                "target": str(target),
                "name": "iris",
                "answers_fingerprint": "sha256:" + "b" * 64,
            },
            separators=(",", ":"),
        ),
    )
    qualification_payload: dict[str, Any] = {
        "target_identity_matched": True,
        "answers_fingerprint_matched": True,
        "sources": [
            {
                "source": "codex_local",
                "selected": True,
                "consented": True,
                "registered": True,
                "backfill_count": 1,
                "retrieval_ok": True,
            }
        ],
    }

    def qualification_result(*, nested: bool) -> Any:
        result_payload: dict[str, Any] = {
            "success": True,
            "action_status": "completed",
        }
        if nested:
            result_payload["data"] = qualification_payload
        else:
            result_payload.update(qualification_payload)
        runtime.responses[qualification_vector] = command_outcome(
            0,
            False,
            1,
            json.dumps({"result": result_payload}),
            "",
        )
        return dispatch_request(probe_request, runtime)

    writes_before = len(runtime.writes)
    commands_before = len(runtime.commands)
    postcondition = qualification_result(nested=False)
    check(
        postcondition["checkpoint_status"] == "verified",
        "flat session-source envelope verifies on the registered probe route",
    )
    check(
        len(runtime.writes) == writes_before,
        "verified session-source probe performs no mutation",
    )
    check(
        runtime.commands[commands_before:] == [qualification_vector],
        "verified session-source probe qualifies state without register or backfill",
    )
    nested_postcondition = qualification_result(nested=True)
    check(
        nested_postcondition["checkpoint_status"] == "blocked",
        "nested session-source envelope blocks instead of being accepted as flat",
    )
    return postcondition


def _public_evidence_item(response: Any) -> dict[str, Any]:
    evidence_items = response.get("evidence")
    if not isinstance(evidence_items, list) or len(evidence_items) != 1:
        raise AssertionError("fixture response must contain exactly one evidence item")
    evidence_item = evidence_items[0]
    if not isinstance(evidence_item, dict):
        raise AssertionError("fixture evidence item must be an object")
    return evidence_item


def _assert_public_evidence(
    response: Any,
    *,
    label: str,
    check: Callable[[object, str], None],
    reject_dict: bool = False,
) -> None:
    from solet_manager.adapter_validation import validate_evidence
    from solet_manager.errors import AdapterProtocolError

    evidence_item = _public_evidence_item(response)
    for field in ("observed", "expected"):
        value = evidence_item.get(field)
        is_public_scalar = value is None or isinstance(value, bool | int | float | str)
        is_unique_string_array = (
            isinstance(value, list)
            and all(isinstance(item, str) for item in value)
            and value == sorted(set(value))
        )
        check(
            is_public_scalar or is_unique_string_array,
            f"{label} {field} uses the closed public evidence shape",
        )
    validate_evidence(evidence_item)
    check(True, f"{label} is accepted by the manager evidence validator")
    if not reject_dict:
        return
    dict_observed = {**evidence_item, "observed": {"must": "remain rejected"}}
    try:
        validate_evidence(dict_observed)
    except AdapterProtocolError:
        check(True, "manager validator rejects dict-shaped observed evidence")
    else:
        check(False, "manager validator rejects dict-shaped observed evidence")


def run_public_evidence_shape_regression(
    target: Path,
    session_response: Any,
    *,
    request: Callable[..., Any],
    check: Callable[[object, str], None],
    command_outcome: Any,
) -> None:
    """Validate all repaired producers against the manager's closed evidence shape."""

    from github_midwife_plugin.installation_doctor import truncated_solet_call_output
    from github_midwife_plugin.setup_adapter import _runtime_failure

    _assert_public_evidence(
        session_response,
        label="session-source evidence",
        check=check,
        reject_dict=True,
    )

    truncated_response = truncated_solet_call_output(
        request(
            target,
            operation_id="output_truncated",
            operation_ref="service_interface::session_ledger_service.qualify_selected_sources",
            probe_purpose="completion",
        ),
        "service_interface::session_ledger_service::qualify_selected_sources",
        command_outcome(
            0,
            False,
            1,
            "captured",
            "",
            stdout_truncated=True,
            stdout_bytes=65_537,
        ),
    )
    _assert_public_evidence(
        truncated_response,
        label="truncated-output evidence",
        check=check,
    )

    runtime_failure = _runtime_failure(
        request(
            target,
            operation_id="adapter_runtime_error",
            operation_ref="service_interface::session_ledger_service.qualify_selected_sources",
            probe_purpose="completion",
        ),
        OSError("fixture transport failure"),
    )
    _assert_public_evidence(
        runtime_failure,
        label="runtime-failure evidence",
        check=check,
    )
