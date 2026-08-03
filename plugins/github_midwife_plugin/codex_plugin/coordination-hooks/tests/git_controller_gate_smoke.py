#!/usr/bin/env python3
"""Stdlib contract smoke for the stock-Codex Git-Controller adapter."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True

_HERE = Path(__file__).resolve().parent
_CODEX_HOOKS = _HERE.parent / "hooks"
_CLAUDE_HOOKS = (
    _HERE.parents[2] / "claude_plugin" / "coordination-hooks" / "hooks"
)
_COMMON_HOOKS = _HERE.parents[2] / "coordination_hooks_common"
sys.path.insert(0, str(_CODEX_HOOKS))

# ruff: noqa: E402, I001
# pyright: reportMissingImports=false
import git_controller_gate as gate


GC = "Git-Controller"
PEER = "Architect"
_passed = 0
_failed: list[str] = []


def _check(condition: bool, description: str) -> None:
    global _passed
    if condition:
        _passed += 1
        return
    _failed.append(description)


def _run_hook(
    payload: dict[str, object] | None,
    *,
    role: str | None = PEER,
    controller: str | None = GC,
    malformed_stdin: bool = False,
    extra_env: dict[str, str] | None = None,
) -> tuple[int, str]:
    """Run the real adapter with a deliberately narrow identity environment."""
    env = dict(os.environ)
    for name in (
        gate.AGENT_ROLE_ENV,
        gate.GIT_CONTROLLER_ENV,
        "AGENT_IDENTITY",
        "AGENT_INSTANCE_ID",
        "AGENT_SESSION_LABEL",
        "AGENT_SESSION_ID",
    ):
        env.pop(name, None)
    if role is not None:
        env[gate.AGENT_ROLE_ENV] = role
    if controller is not None:
        env[gate.GIT_CONTROLLER_ENV] = controller
    if extra_env:
        env.update(extra_env)
    stdin = "{not valid json" if malformed_stdin else json.dumps(payload or {})
    proc = subprocess.run(
        [sys.executable, "-B", str(_CODEX_HOOKS / "git_controller_gate.py")],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10.0,
        check=False,
    )
    return proc.returncode, proc.stderr


def case_common_policy_bytes_match() -> None:
    """Claude and Codex ship exactly one reviewed policy implementation."""
    for name in (
        "_git_controller_lex.py",
        "_git_controller_walker.py",
        "_git_policy.py",
    ):
        canonical_bytes = (_COMMON_HOOKS / name).read_bytes()
        codex_bytes = (_CODEX_HOOKS / name).read_bytes()
        claude_bytes = (_CLAUDE_HOOKS / name).read_bytes()
        _check(
            codex_bytes == canonical_bytes,
            f"Codex materialized bytes match canonical {name}",
        )
        _check(
            claude_bytes == canonical_bytes,
            f"Claude materialized bytes match canonical {name}",
        )


def case_identity_contract_is_role_only() -> None:
    """Pin the adapter to the neutral launcher role, never label or UUID."""
    source = (_CODEX_HOOKS / "git_controller_gate.py").read_text()
    _check(gate.AGENT_ROLE_ENV == "AGENT_ROLE", "identity env is AGENT_ROLE")
    _check(
        gate.CODEX_GATED_TOOL_NAMES == frozenset({"Bash"}),
        "only the measured Codex Bash schema is routed",
    )
    _check("AGENT_SESSION_LABEL" not in source, "adapter ignores session label")
    _check("AGENT_SESSION_ID" not in source, "adapter ignores session UUID")
    _check('.get("session_id"' not in source, "adapter ignores hook thread UUID")


def case_opt_in_and_controller_role() -> None:
    mutation = {"tool_name": "Bash", "tool_input": {"command": "git stash"}}
    code, _ = _run_hook(mutation, controller=None)
    _check(code == 0, "controller env unset disables the gate")
    code, _ = _run_hook(mutation, role=GC)
    _check(code == 0, "configured controller role may mutate git")


def case_peer_and_missing_role_block_mutation() -> None:
    mutation = {"tool_name": "Bash", "tool_input": {"command": "git stash"}}
    code, stderr = _run_hook(mutation)
    _check(code == 2, "non-controller role is blocked from git stash")
    _check(f"role {PEER!r}" in stderr, "block output reports resolved AGENT_ROLE")
    # A5 / D-5a.3 POSITIVE LEG (the "3a" ruling, 2026-08-01): assert the text a
    # blocked session ACTUALLY RECEIVES is the text this copy carries. The
    # clause-consistency leg proves the same words sit in four files; it cannot
    # prove they ever render. CONTAINMENT, never whole-payload equality — the
    # refusal interpolates the resolved role and a command slice.
    _check(
        gate.POLICY_MESSAGE in stderr,
        "block output renders this copy's POLICY_MESSAGE verbatim",
    )
    # ⚠ DELIBERATE COVERAGE REMOVAL, disclosed rather than dropped silently.
    # This previously asserted `"escalate" in stderr`. The operator's ruled A5
    # wording (2026-08-01) contains NO escalation instruction. Removed because
    # the ruled text is authoritative, NOT because the property stopped
    # mattering — see the twin note in the claude_plugin copy's
    # case_subprocess_architect_git_stash_blocked.

    code, stderr = _run_hook(mutation, role=None)
    _check(code == 2, "missing role is blocked from git stash")
    _check("<unknown>" in stderr, "missing role is reported as unknown")
    code, _ = _run_hook(mutation, role="")
    _check(code == 2, "empty role is blocked from git stash")


def case_label_and_uuid_cannot_grant_controller() -> None:
    mutation = {
        "session_id": GC,
        "tool_name": "Bash",
        "tool_input": {"command": "git push"},
    }
    extra = {
        "AGENT_IDENTITY": GC,
        "AGENT_INSTANCE_ID": GC,
        "AGENT_SESSION_LABEL": GC,
        "AGENT_SESSION_ID": GC,
    }
    code, _ = _run_hook(mutation, role=None, extra_env=extra)
    _check(code == 2, "controller-looking non-role metadata grants no authority")
    code, _ = _run_hook(mutation, role=PEER, extra_env=extra)
    _check(code == 2, "AGENT_ROLE wins over controller-looking metadata")


def case_read_only_and_wrapped_commands() -> None:
    status = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    code, _ = _run_hook(status, role=None)
    _check(code == 0, "read-only git status is allowed without a role")
    wrapped = {
        "tool_name": "Bash",
        "tool_input": {"command": 'bash -c "git commit -m x"'},
    }
    code, _ = _run_hook(wrapped)
    _check(code == 2, "wrapped git mutation is blocked")


def case_unmeasured_tools_are_not_routed() -> None:
    """Do not claim edit/delegation protection before capturing live schemas."""
    for tool_name in ("apply_patch", "Agent", "Task"):
        payload = {"tool_name": tool_name, "tool_input": {"command": "git push"}}
        code, _ = _run_hook(payload)
        _check(code == 0, f"unmeasured Codex tool {tool_name!r} is not routed")


def case_malformed_payload_is_non_blocking() -> None:
    code, _ = _run_hook(None, malformed_stdin=True)
    _check(code == 0, "malformed JSON is non-blocking in mistake-prevention scope")
    code, _ = _run_hook({"tool_name": 7, "tool_input": "not-an-object"})
    _check(code == 0, "wrong-shaped payload is non-blocking")


def main() -> int:
    cases = [
        case_common_policy_bytes_match,
        case_identity_contract_is_role_only,
        case_opt_in_and_controller_role,
        case_peer_and_missing_role_block_mutation,
        case_label_and_uuid_cannot_grant_controller,
        case_read_only_and_wrapped_commands,
        case_unmeasured_tools_are_not_routed,
        case_malformed_payload_is_non_blocking,
    ]
    for case in cases:
        try:
            case()
        except Exception as exc:  # noqa: BLE001 -- aggregate smoke diagnostics
            _failed.append(f"{case.__name__} raised {type(exc).__name__}: {exc}")
    if _failed:
        print(f"FAIL: {_passed} passed, {len(_failed)} failed")
        for failure in _failed:
            print(f"  - {failure}")
        return 1
    print(f"PASS: {_passed} Codex Git-Controller adapter checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
