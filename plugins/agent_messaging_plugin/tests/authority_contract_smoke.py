#!/usr/bin/env python3
"""Unit smoke for ``authority_contract.py`` (T2, seat's design ruling
2026-08-05, approved text message arm-b79d254b9d1bd8c732e9398ae2901257) --
the fleet delegation contract rendered into every spawn's
``--append-system-prompt``.

Proves: every named placeholder resolves; the literal
``{"op": "is_null"}`` JSON example in the BINDING RULES section survives
rendering untouched (confirms plain ``str.replace`` is used, not
``str.format``, which would misparse it); the seat's two approved edits
hold (the opening sentence says "a worker of class", never "claiming
role"; FIRST ACTIONS never instructs claiming the role_class string as a
role name); and the RED-FIRST render-time guard the seat's approval
explicitly required -- an unresolved ``{placeholder}`` in the template
file must never ship into a live system prompt.

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

from agent_messaging_plugin import authority_contract  # noqa: E402
from agent_messaging_plugin.authority_contract import (  # noqa: E402
    UnresolvedPlaceholderError,
    render_authority_delegation_contract,
)

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
        "brief_ref": "workbench/2026-08-04_wave_dispatch_brief.md",
        "spawned_by_role": "Coordinator-Main",
    }
    fields.update(overrides)
    return render_authority_delegation_contract(**fields)  # type: ignore[arg-type]


def test_all_placeholders_resolve() -> None:
    text = _render()
    _check("agi-contract-1" in text, "agent_instance_id substitutes")
    _check("'project'" in text, "role_class substitutes")
    _check("'fleet-token-efficiency'" in text, "lane_id substitutes")
    _check("workbench/2026-08-04_wave_dispatch_brief.md" in text, "brief_ref substitutes")
    _check("'Coordinator-Main'" in text, "spawned_by_role substitutes")
    for name in ("agent_instance_id", "role_class", "lane_id", "brief_ref", "spawned_by_role"):
        _check(f"{{{name}}}" not in text, f"no unresolved {{{name}}} remains")


def test_json_example_survives_literally() -> None:
    """RED-FIRST for the str.format() trap: the template's own BINDING
    RULES text contains a literal {"op": "is_null"} JSON example -- if
    rendering used str.format() instead of plain substitution, this would
    raise a ValueError (unexpected '"' in field name) rather than render
    cleanly."""
    text = _render()
    _check(
        '{"op": "is_null"}' in text,
        "the literal JSON example survives rendering byte-for-byte",
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
        "the REJECTED clause ('must never request, expect, or wait for ... "
        "confirmation') never appears",
    )
    _check(
        "tier-1" in text and "SEAT-NATIVE" in text,
        "the tier-1/seat-native statement is present",
    )
    _check(
        "You will never be asked to author, spec, or execute a tier-1 action" in text,
        "the contract states plainly that tier-1 is never asked of a worker "
        "-- the honest replacement text, verbatim",
    )
    _check(
        "routing it to your spawning role over the peer channel is the DESIGNED path" in text,
        "handing a tier-1-shaped task up to the spawning role is stated as "
        "the designed path, not an exception",
    )


def test_missing_fields_render_as_blank_not_raise() -> None:
    """A direct driver.spawn() call that bypasses spawn_session (e.g. an
    existing test fixture) supplies no role_class/spawned_by_role -- this
    must render blank slots, never raise, so authority-at-first-contact
    stays ON by default even for a caller that doesn't know about T2."""
    text = render_authority_delegation_contract(
        agent_instance_id="agi-bare-1", role_class="", lane_id="", brief_ref="",
        spawned_by_role="",
    )
    _check("agi-bare-1" in text, "the one field that WAS supplied still substitutes")
    _check("{role_class}" not in text, "an empty-string field is still a resolved substitution")


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
        raised, "an unresolved placeholder raises UnresolvedPlaceholderError, never silently ships",
    )


def test_render_is_deterministic() -> None:
    _check(
        _render() == _render(), "identical inputs produce identical output, no hidden randomness",
    )


def main() -> int:
    test_all_placeholders_resolve()
    test_json_example_survives_literally()
    test_edit_1_worker_of_class_not_claiming_role()
    test_edit_2_first_actions_never_claims_the_class_string()
    test_edit_3_tier_statement_replaces_never_wait_clause()
    test_missing_fields_render_as_blank_not_raise()
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
