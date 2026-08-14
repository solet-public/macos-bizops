#!/usr/bin/env python3
"""Transport-shim smoke: the D1 L1 verbs as actually reached through
``AgentMessagingPlugin`` methods (not just the pure functions in
``session_lifecycle_verbs.py``, which ``session_lifecycle_verbs_smoke.py``
already covers). Verifies the ``params``/``state`` extraction, the
``VerbError`` -> ``_failure_result`` mapping, and that ``call_context`` flows
into ``directed_by`` on the session_transition audit trail.

Builds a BARE plugin instance (``object.__new__`` — the full
``AgentMessagingPlugin.__init__`` wants an orchestrator/config this smoke has
no business standing up) and monkeypatches ``_get_state_service`` to hand
back a real ``RealShapeState`` — the same technique other unit smokes in
this suite use to reach an EDGE method's body directly. This does NOT
exercise the platform's process registry, `process_search`, or a live
`process_call` (no unresolved `<<FIELD>>` check) — that layer needs a
running solet and a restart to pick up the new processes; NOT verified
here, and this slice's commit request must say so explicitly.

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/session_lifecycle_transport_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import TABLE_SESSION_TRANSITION  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
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


def _bare_plugin(state: StateManagementInterface) -> AgentMessagingPlugin:
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    return plugin


def _params(**kwargs: object) -> dict[str, Any]:
    return {"parameters": kwargs}


def _state_with_context() -> dict[str, Any]:
    return {"call_context": CallContext.for_operator()}


def _error_code(result: dict[str, Any]) -> str | None:
    error = result.get("error")
    return error.get("code") if isinstance(error, dict) else None


def test_spawn_session_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    plugin = _bare_plugin(state)
    result = plugin.spawn_session(
        _params(
            role_class="bogus", lane_id="lane-1", brief_ref="b", work_class="read_only",
            budget_line="b1",
        ),
        _state_with_context(),
    )
    _check(
        result.get("action_status") == "failed" and _error_code(result) == "unknown_role_class",
        f"spawn_session transport maps VerbError to a failure result with the "
        f"right code (got {result!r})",
    )

    ok = plugin.spawn_session(
        _params(
            role_class="ephemeral", lane_id="lane-1", brief_ref="b", work_class="read_only",
            budget_line="b1", host="operator",
        ),
        _state_with_context(),
    )
    _check(
        ok.get("action_status") == "failed" and _error_code(ok) == "host_cannot_spawn",
        "a valid-but-doomed spawn (operator host) surfaces host_cannot_spawn "
        "through the transport, not an unhandled exception",
    )


def test_list_and_status_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-z", lane_id="lane-z", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    listed = plugin.list_sessions(_params(lane_id="lane-z"), {})
    _check(
        listed.get("action_status") == "completed"
        and len(listed["data"]["sessions"]) == 1,
        f"list_sessions transport returns the envelope-wrapped sessions list (got {listed!r})",
    )

    status = plugin.session_status(_params(agent_instance_id="agi-z"), {})
    _check(
        status.get("action_status") == "completed"
        and status["data"]["agent_instance_id"] == "agi-z",
        "session_status transport returns the ledger row",
    )

    missing = plugin.session_status(_params(agent_instance_id="agi-none"), {})
    _check(
        missing.get("action_status") == "failed" and _error_code(missing) == "session_not_found",
        "session_status(unknown) transport surfaces session_not_found",
    )


def test_terminate_retire_directed_by_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-y", lane_id="lane-y", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    terminated = plugin.terminate_session(
        _params(agent_instance_id="agi-y"), _state_with_context(),
    )
    _check(
        terminated.get("action_status") == "completed"
        and terminated["data"]["already_terminal"] is False,
        "terminate_session transport lands the transition",
    )

    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_TRANSITION, "filters": {"agent_instance_id": "agi-y"}},
    )
    rows = require_records(result)
    _check(
        len(rows) == 1 and rows[0]["directed_by"] == "operator",
        f"call_context flows through format_directed_by into the audit "
        f"trail's directed_by column (got {rows[0].get('directed_by') if rows else None!r})",
    )

    retired = plugin.retire_session(_params(agent_instance_id="agi-y"), _state_with_context())
    _check(
        retired.get("action_status") == "completed" and retired["data"]["already_retired"] is False,
        "retire_session transport lands terminated -> retired",
    )


def test_report_alive_transport() -> None:
    state = cast("StateManagementInterface", RealShapeState())
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-w", lane_id="lane-w", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    plugin = _bare_plugin(state)
    bad_status = plugin.report_alive(
        _params(agent_instance_id="agi-w", status="bogus"), _state_with_context(),
    )
    _check(
        bad_status.get("action_status") == "failed" and _error_code(bad_status) == "unknown_status",
        "report_alive transport surfaces unknown_status through the failure envelope",
    )


def main() -> int:
    test_spawn_session_transport()
    test_list_and_status_transport()
    test_terminate_retire_directed_by_transport()
    test_report_alive_transport()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
