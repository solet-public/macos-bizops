#!/usr/bin/env python3
"""Unit smoke for ``authority_contract.py`` (T2, seat's design ruling
2026-08-05, approved text message arm-b79d254b9d1bd8c732e9398ae2901257) --
the fleet delegation contract rendered into every spawn's
``--append-system-prompt``.

Proves: every named placeholder resolves; a target repository root reaches
the repository-scoped binding and displaces origin-specific bindings; the seat's
two approved edits hold (the opening sentence says "a worker of class", never
"claiming role"; FIRST ACTIONS never instructs claiming the role_class string
as a role name); and the RED-FIRST render-time guard the seat's approval
explicitly required -- an unresolved ``{placeholder}`` in the template file
must never ship into a live system prompt.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/authority_contract_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from _register_unit_double import FIXTURE_UNIT_ID, fixture_assignment, fixture_assignment_record  # noqa: E402

from agent_messaging_plugin import authority_contract  # noqa: E402
from agent_messaging_plugin.authority_contract import (  # noqa: E402
    UnresolvedPlaceholderError,
    render_authority_delegation_contract,
)
from agent_messaging_plugin.headless_adapter import _authority_system_prompt  # noqa: E402
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _spawn_session_request_from_params,
)
from agent_messaging_plugin.register_unit_client import RegisterUnitError  # noqa: E402

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


def _render(**overrides: str) -> str:
    fields = {
        "agent_instance_id": "agi-contract-1",
        "role_class": "project",
        "lane_id": "fleet-token-efficiency",
        "assignment": fixture_assignment(),
        "spawned_by_role": "Coordinator-Main",
    }
    fields.update(overrides)
    return render_authority_delegation_contract(**fields)  # type: ignore[arg-type]


def test_all_placeholders_resolve() -> None:
    text = _render()
    _check("agi-contract-1" in text, "agent_instance_id substitutes")
    _check("'project'" in text, "role_class substitutes")
    _check("'fleet-token-efficiency'" in text, "lane_id substitutes")
    _check("drv_fixture-revision" in text, "registered revision substitutes")
    _check("'Coordinator-Main'" in text, "spawned_by_role substitutes")
    for name in (
        "agent_instance_id",
        "role_class",
        "lane_id",
        "revision_id",
        "unit_id",
        "spawned_by_role",
    ):
        _check(f"{{{name}}}" not in text, f"no unresolved {{{name}}} remains")


def test_target_repository_scopes_bindings() -> None:
    text = _render(repository_root="/workspace/project-solet")
    _check(
        "TARGET REPOSITORY: /workspace/project-solet" in text,
        "resolved target repository root reaches the authority contract",
    )
    _check(
        "AGENTS.md or CLAUDE.md" in text and "Do not inherit" in text,
        "contract delegates repository-specific rules to the target bootstrap",
    )
    _check(
        all(
            rule in text
            for rule in (
                "Agent/Task tool is forbidden",
                "git merge-base --is-ancestor",
                "waiting on a peer wake with no fallback",
                "conflict between your task and its brief",
            )
        ),
        "fleet-wide conduct rules remain present for every target repository",
    )
    _check(
        "StateManagementInterface only for database access" not in text,
        "origin database binding is absent from the generic authority contract",
    )
    from_worktree = _authority_system_prompt(
        {
            **fixture_assignment_record(),
            "agent_instance_id": "agi-contract-worktree",
            "worktree_path": "/workspace/project-solet-lane",
        },
    )
    _check(
        "TARGET REPOSITORY: /workspace/project-solet-lane" in from_worktree,
        "spawned target worktree scopes bindings when no explicit root is present",
    )


def test_edit_1_worker_of_class_not_claiming_role() -> None:
    """The seat's first approved edit: the opening sentence must never
    read as if role_class were a role NAME being claimed."""
    text = _render(role_class="project")
    _check(
        "a worker of class 'project' in lane" in text,
        "opening sentence uses 'a worker of class', the corrected wording",
    )
    _check(
        "claiming role 'project'" not in text,
        "the REJECTED wording ('claiming role') never appears",
    )


def test_edit_2_first_actions_never_claims_the_class_string() -> None:
    """The seat's second approved edit: FIRST ACTIONS must instruct
    claiming a DISPATCH-ASSIGNED role name, never the role_class value
    itself -- the exact defect (every fresh worker literally claiming the
    string 'project' as a role name) the edit fixes."""
    text = _render(role_class="project")
    _check(
        "Claim role 'project' if not already bound." not in text,
        "the REJECTED instruction ('Claim role <class>') never appears",
    )
    _check(
        "Claim the durable role NAME your spawning role's dispatch assigns" in text,
        "FIRST ACTIONS correctly defers the role NAME to dispatch prose",
    )
    _check(
        "your role_class governs the claim policy; never invent a role name yourself" in text,
        "the corrected instruction names role_class as policy, not identity",
    )


def test_edit_3_tier_statement_replaces_never_wait_clause() -> None:
    """fleet-watch-transport-migration phase 2 slice 1+5, per the
    capability-tier guardrail redesign section 4: the REJECTED clause
    (which live-measured evidence showed reads like something a hijacker
    would write, to a model weighing a genuine capability escalation) must
    never appear again; the honest tier-0/tier-1 statement must."""
    text = _render()
    _check(
        "must never request, expect, or wait for" not in text,
        "the REJECTED clause ('must never request, expect, or wait for ... confirmation') never appears",
    )
    _check(
        "tier-1" in text and "SEAT-NATIVE" in text,
        "the tier-1/seat-native statement is present",
    )
    _check(
        "You will never be asked to author, spec, or execute a tier-1 action" in text,
        "the contract states plainly that tier-1 is never asked of a worker -- the honest replacement text, verbatim",
    )
    _check(
        "routing it to your spawning role over the peer channel is the DESIGNED path" in text,
        "handing a tier-1-shaped task up to the spawning role is stated as the designed path, not an exception",
    )


def test_native_relay_pointer_reaches_spawned_contract() -> None:
    """The adapter-loaded worker prompt points platform relay work at the
    canonical procedure without granting new authority."""
    text = _authority_system_prompt(
        {
            **fixture_assignment_record(),
            "agent_instance_id": "agi-relay-pointer",
            "role_class": "ephemeral",
            "lane_id": "relay-pointer",
            "spawned_by_role": "Coordinator-Main",
            "repository_root": "/workspace/platform-repository",
        }
    )
    _check(
        "RELAY WORKFLOW POINTER" in text and "canonical Git-Controller commit skill" in text,
        "adapter-rendered prompt points at the canonical relay procedure",
    )
    _check(
        "This is a source pointer; the registered brief and repository authority still govern the work." in text,
        "relay pointer does not expand authority",
    )


def test_missing_fields_render_as_blank_not_raise() -> None:
    """A direct driver.spawn() call that bypasses spawn_session (e.g. an
    existing test fixture) supplies no role_class/spawned_by_role -- this
    must render blank slots, never raise, so authority-at-first-contact
    stays ON by default even for a caller that doesn't know about T2."""
    text = render_authority_delegation_contract(
        agent_instance_id="agi-bare-1",
        role_class="",
        lane_id="",
        assignment=fixture_assignment(),
        spawned_by_role="",
    )
    _check("agi-bare-1" in text, "the one field that WAS supplied still substitutes")
    _check("{role_class}" not in text, "an empty-string field is still a resolved substitution")


def test_task_controls_peer_ack_while_native_uptake_remains_required() -> None:
    """Exercise the real template through both its renderer and host adapter."""
    _check(
        Path(authority_contract.__file__).resolve() == REPO_ROOT / "plugins/agent_messaging_plugin/src/agent_messaging_plugin/authority_contract.py",
        "authority renderer imports from this checkout",
    )
    _check(
        authority_contract._TEMPLATE_PATH.resolve()  # noqa: SLF001
        == REPO_ROOT / "plugins/agent_messaging_plugin/templates/authority_delegation_contract.txt",
        "renderer reads this checkout's actual authority template",
    )
    adapted = _authority_system_prompt(
        {
            **fixture_assignment_record(),
            "agent_instance_id": "agi-contract-1",
            "role_class": "project",
            "lane_id": "fleet-token-efficiency",
            "spawned_by_role": "Coordinator-Main",
            "repository_root": "/workspace/target",
        }
    )
    _check(
        adapted == _render(repository_root="/workspace/target"),
        "adapter and direct renderer load identical real template bytes",
    )
    for text in (_render(), adapted):
        _check(
            "Ack to 'Coordinator-Main' with a scope read-back before touching code." not in text,
            "no unconditional peer scope-ACK prerequisite reaches the worker",
        )
        _check(
            "Peer ACK messages are required only when the bound task explicitly requires them." in text,
            "bound task controls peer ACK messages, including explicit requirements",
        )
        _check(
            "Do not generate an extra peer ACK or wake from this generic contract." in text,
            "generic bootstrap adds no peer ACK or wake",
        )
        _check(
            "For managed dispatch, before touching code, verify your own current attempt, role binding,"
            " assignment digest and exact registered revision through managed_dispatch_status;"
            " submit the required structured native ACK through report_managed_dispatch and verify"
            " its accepted event and fresh active status." in text,
            "managed identity verification and accepted native ACK still precede code",
        )
        _check(
            "Peer prose, report_alive and first_turn_delivered do not substitute for managed uptake." in text,
            "transport, liveness and peer prose cannot substitute for native uptake",
        )


def test_required_unit_id_reaches_the_authority_contract() -> None:
    raw = {
        "role_class": "ephemeral",
        "lane_id": "lane-unit-id",
        "unit_id": FIXTURE_UNIT_ID,
        "work_class": "read_only",
        "budget_line": "test",
    }
    request = _spawn_session_request_from_params(raw, "operator:test")
    metadata = AgentMessagingPlugin.spawn_session_action._platform_process_metadata
    _check(metadata.parameters["unit_id"].required is True, "public spawn requires an existing Unit")
    present = _authority_system_prompt(
        {
            "agent_instance_id": "agi-unit-id",
            "role_class": request.role_class,
            "lane_id": request.lane_id,
            **fixture_assignment_record(),
            "spawned_by_role": "Test-Dispatcher",
            "repository_root": "/fixture/project-solet",
        }
    )
    _check(
        f"UNIT ID: {FIXTURE_UNIT_ID}" in present and "drv_fixture-revision" in present,
        "host authority retains Unit and pinned revision",
    )
    try:
        _authority_system_prompt({"agent_instance_id": "agi-missing"})
    except RegisterUnitError as exc:
        _check(exc.code == "unit_assignment_historical", "missing native assignment cannot launch")
    else:
        raise AssertionError("host accepted an unassigned historical row")


def test_unresolved_placeholder_raises() -> None:
    """RED-FIRST, the seat's explicitly-required leg: the KB-ships-unrendered
    trap class. A template with a genuine unresolved placeholder (a typo,
    or a future field the render function doesn't know about yet) must
    refuse to return text, never silently ship a raw {placeholder} token
    into a live system prompt."""
    original_path = authority_contract._TEMPLATE_PATH  # noqa: SLF001
    with tempfile.TemporaryDirectory() as tmp:
        broken_template = Path(tmp) / "broken.txt"
        broken_template.write_text("Hello {agent_instance_id}, you forgot {this_one}.")
        authority_contract._TEMPLATE_PATH = broken_template  # noqa: SLF001
        try:
            raised = False
            try:
                _render()
            except UnresolvedPlaceholderError as exc:
                raised = True
                _check(
                    "this_one" in str(exc),
                    "the error names the specific unresolved placeholder",
                )
        finally:
            authority_contract._TEMPLATE_PATH = original_path  # noqa: SLF001
    _check(
        raised,
        "an unresolved placeholder raises UnresolvedPlaceholderError, never silently ships",
    )


def test_render_is_deterministic() -> None:
    _check(
        _render() == _render(),
        "identical inputs produce identical output, no hidden randomness",
    )


def main() -> int:
    test_all_placeholders_resolve()
    test_target_repository_scopes_bindings()
    test_edit_1_worker_of_class_not_claiming_role()
    test_edit_2_first_actions_never_claims_the_class_string()
    test_edit_3_tier_statement_replaces_never_wait_clause()
    test_native_relay_pointer_reaches_spawned_contract()
    test_task_controls_peer_ack_while_native_uptake_remains_required()
    test_missing_fields_render_as_blank_not_raise()
    test_required_unit_id_reaches_the_authority_contract()
    test_unresolved_placeholder_raises()
    test_render_is_deterministic()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
