#!/usr/bin/env python3
"""Smoke for the role covered-mark floor versus the authorization intake read
(Problem wgr_b6007d23; issues iss_ac0625ee, iss_dbbfe574, iss_ec266fd3).

``peer_mark_role_covered`` stores one mark per role and the role drain applies
it as a floor to every read. The authorization helper reads the same role, so a
mark that covers an authorization row (or a later row) hid it, and the only way
past the floor, the history cursor, never returns the mark's own row.

The fix is an explicit, observer-only ``include_covered`` read. Each check names
the mutation that turns it red:

- service reads past the mark   -> ``skip_floor = history_read`` (ignore
                                    ``include_covered``)
- mark's own row visible        -> seed the read at the mark like a history cursor
- observer-only refusal         -> drop the ``include_covered and not observer`` guard
- paging keeps the floor off    -> honour ``include_covered`` on the first page only
- default drain still floors    -> make ``include_covered`` default True
- read never moves the mark     -> have the read attest coverage
- process builder carries it    -> drop ``include_covered`` from
                                    ``peer_direct_reads.build_peer_inbox_request``
- CLI sends it on the role fetch only, refuses it without ``--observer``, requires and
  honours ``--since``, refuses a floored page, and transmits it to the server
- (the authorization helper's frontier argv is pinned in
  ``.claude/hooks/tests/git_controller_authorization_smoke.py``, which ships with the hook)

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/role_covered_floor_intake_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "agent_messaging"))

import local_cli_smoke as cli_smoke  # noqa: E402
import role_inbox_smoke as ris  # noqa: E402
from ananta.llm.agent_messaging.models import (  # noqa: E402
    PeerInboxRequest,
    RoleSectionStatus,
)
from ananta.llm.agent_messaging.schema import TABLE_ROLE_COVERED_MARK  # noqa: E402
from ananta.llm.agent_messaging.service import AgentRequestInvalidError  # noqa: E402

import agent_messaging_plugin.local_cli.cli as cli_mod  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_direct_reads import build_peer_inbox_request  # noqa: E402

_passed = 0
_failed: list[str] = []
_ROLE = "Git-Controller"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _seeded_state(*, mark_row: str = "f02") -> ris._FakeState:
    """Four rows; the mark covers THROUGH ``mark_row`` (f02 is the authorization)."""
    state = ris._FakeState()
    ris._seed_binding(state, _ROLE)
    for index, row_id in enumerate(("f01", "f02", "f03", "f04")):
        ris._seed_role_msg(
            state, row_id=row_id, role=_ROLE, created_at=f"2026-09-25T13:0{index}:00",
        )
    created = {"f01": "13:00", "f02": "13:01", "f03": "13:02", "f04": "13:03"}[mark_row]
    ris._seed_mark(
        state, role=_ROLE, covered_created_at=f"2026-09-25T{created}:00", covered_id=mark_row,
    )
    return state


def _read(
    state: ris._FakeState, *, observer: bool, include_covered: bool,
    limit: int = 10, role_after: str | None = None,
) -> tuple[Any, ...]:
    return ris._make_service(state).list_silent_for_roles(
        agent_instance_id=ris._INSTANCE, include_important=True, limit=limit,
        role_after=role_after, observer=observer, include_covered=include_covered,
    )


def test_service_reads_past_the_floor_including_the_mark_row() -> None:
    state = _seeded_state()
    entries, cursor, floor_applied, history = _read(state, observer=True, include_covered=True)
    _check(
        ris._page_ids(entries) == ["msg-f04", "msg-f03", "msg-f02", "msg-f01"],
        "include_covered: every row is returned, the mark's own row f02 included",
    )
    _check(cursor is None and floor_applied is False and history is None,
           "include_covered: exhausted, no floor applied, no history cursor")

    default, _, default_floor, default_history = _read(state, observer=True, include_covered=False)
    _check(ris._page_ids(default) == ["msg-f04", "msg-f03"] and default_floor is True,
           "control: the default observer read still honours the floor (pending count)")
    assert default_history is not None
    via_history, _, _, _ = _read(
        state, observer=True, include_covered=False, role_after=default_history,
    )
    _check("msg-f02" not in ris._page_ids(via_history),
           "control: the history cursor cannot return the mark's own row")


def test_service_default_is_floored_and_observer_only() -> None:
    state = _seeded_state()
    service = ris._make_service(state)
    entries, _, floor_applied, _ = service.list_silent_for_roles(
        agent_instance_id=ris._INSTANCE, include_important=True, limit=10, role_after=None,
    )
    _check(ris._page_ids(entries) == ["msg-f04", "msg-f03"] and floor_applied is True,
           "default read without the argument is floored")
    try:
        _read(state, observer=False, include_covered=True)
    except AgentRequestInvalidError:
        refused = True
    else:
        refused = False
    _check(refused, "include_covered without observer is refused")
    section = ris._make_service(state)._collect_role_section(
        PeerInboxRequest(
            recipient_agent_id="claude_code", recipient_agent_instance_id=ris._INSTANCE,
            limit=10, include_covered=True,
        ),
    )
    _check(section[4] is RoleSectionStatus.ERROR and section[0] == (),
           "the refused read surfaces as a role-section error, never an empty ok page")


def test_service_paging_keeps_the_floor_off() -> None:
    state = _seeded_state(mark_row="f04")
    seen: list[str] = []
    role_after: str | None = None
    for _ in range(10):
        entries, cursor, _, _ = _read(
            state, observer=True, include_covered=True, limit=2, role_after=role_after,
        )
        seen.extend(ris._page_ids(entries))
        if cursor is None:
            break
        role_after = cursor
    _check(seen == ["msg-f04", "msg-f03", "msg-f02", "msg-f01"],
           "include_covered pages through every row when the mark is the newest row")


def test_read_does_not_move_the_mark() -> None:
    state = _seeded_state()
    before = state.query_state(
        ris.ROLE_NAMESPACE, {"table": TABLE_ROLE_COVERED_MARK, "filters": {}},
    )
    _read(state, observer=True, include_covered=True)
    after = state.query_state(
        ris.ROLE_NAMESPACE, {"table": TABLE_ROLE_COVERED_MARK, "filters": {}},
    )
    _check(before == after, "an include_covered read leaves the stored mark byte-identical")


def test_process_builder_carries_the_flag() -> None:
    binding = BridgeBinding(
        bridge_id="agc-1", agent_id="claude_code", agent_instance_id=ris._INSTANCE,
        session_label=_ROLE, parent_pid=1, agent_session_id="sess-x",
    )
    on = build_peer_inbox_request({"observer": True, "include_covered": True}, binding)
    off = build_peer_inbox_request({"observer": True}, binding)
    _check(on.include_covered is True and off.include_covered is False,
           "peer_inbox process request carries include_covered, default False")


def _cli_calls(
    args: list[str], role_pages: list[dict[str, Any]] | None = None,
) -> tuple[Any, list[dict[str, Any]]]:
    """Run ``solet-bridge inbox`` with the page fetch replaced by a recorder.

    ``role_pages`` are served, in order, to the fetches that ask past the floor or the
    role walk; the direct-section fetch always gets an empty page.
    """
    calls: list[dict[str, Any]] = []
    empty = cli_smoke._role_addressed_inbox_page(role_entries=[])
    served = iter(role_pages or [])

    def recorder(_client: Any, _session: str, _limit: int, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs.get("include_covered"):
            return next(served, empty)
        return empty

    handler, _ = cli_smoke._ack_pages_handler()
    with (
        patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-floor-smoke"}),
        patch.object(cli_mod, "_one_shot_peer_inbox_page", recorder),
    ):
        return cli_smoke._invoke(args, handler), calls


_SINCE = "2026-09-17T00:00:00+00:00"


def test_cli_sends_it_on_the_role_fetch_only() -> None:
    result, calls = _cli_calls(["inbox", "--observer", "--include-covered", "--since", _SINCE])
    role_calls = [c for c in calls if c.get("include_covered")]
    other_calls = [c for c in calls if not c.get("include_covered")]
    _check(result.exit_code == 0 and len(role_calls) == 1 and len(other_calls) == 1,
           "inbox --observer --include-covered: the role fetch asks past the floor, "
           "the direct-section fetch does not")
    result, calls = _cli_calls(["inbox", "--observer"])
    _check(result.exit_code == 0 and not any(c.get("include_covered") for c in calls),
           "inbox --observer alone never asks past the floor (pending-count callers)")
    result, calls = _cli_calls(["inbox", "--include-covered", "--since", _SINCE])
    _check(result.exit_code != 0 and calls == [],
           "inbox --include-covered without --observer is refused before any fetch")


def test_cli_horizon_is_required_and_valid() -> None:
    result, calls = _cli_calls(["inbox", "--observer", "--include-covered"])
    _check(result.exit_code != 0 and calls == [],
           "--include-covered without --since is refused before any fetch")
    result, calls = _cli_calls(["inbox", "--observer", "--since", _SINCE])
    _check(result.exit_code != 0 and calls == [],
           "--since without --include-covered is refused before any fetch")
    result, calls = _cli_calls(["inbox", "--observer", "--include-covered", "--since", "yesterday"])
    _check(result.exit_code != 0 and calls == [], "a malformed --since is refused before any fetch")


def test_cli_horizon_bounds_the_walk() -> None:
    recent = cli_smoke._entry("recent", "2026-10-01T10:00:00")
    older = cli_smoke._entry("older", "2026-09-10T10:00:00")
    page = cli_smoke._role_addressed_inbox_page(role_entries=[recent, older])
    page["next_role_cursor"] = "more-history"
    result, calls = _cli_calls(
        ["inbox", "--observer", "--include-covered", "--since", _SINCE], [page],
    )
    payload = json.loads(result.output) if result.exit_code == 0 else {}
    ids = [m["message"]["id"] for m in payload.get("messages", []) if m["section"] == "role"]
    _check(ids == ["recent"] and payload.get("complete") is True
           and len([c for c in calls if c.get("include_covered")]) == 1,
           "within the horizon the row is found; the walk stops at the horizon, "
           "reports complete, and fetches no older page")


def test_cli_refuses_a_floored_include_covered_page() -> None:
    page = cli_smoke._role_addressed_inbox_page(
        role_entries=[cli_smoke._entry("recent", "2026-10-01T10:00:00")],
    )
    page["role_floor_applied"] = True
    result, _ = _cli_calls(
        ["inbox", "--observer", "--include-covered", "--since", _SINCE], [page],
    )
    text = result.output.lstrip()
    payload = json.JSONDecoder().raw_decode(text)[0] if text.startswith("{") else {}
    _check(result.exit_code != 0 and payload.get("complete") is False
           and payload.get("role_section_status") == "error"
           and "needs a deploy" in str(payload.get("role_section_error")),
           "an include_covered page that comes back floored is a loud role fault, "
           "naming a server that predates the fix or a route that dropped the parameter")


class _CapturingClient:
    """Stands in for ``BridgeClient``'s direct ``peer_inbox_for_session`` route call."""

    def __init__(self) -> None:
        self.arguments: dict[str, Any] = {}

    def peer_inbox_for_session(self, **kwargs: Any) -> dict[str, Any]:
        self.arguments = kwargs
        return {"role_entries": []}


def test_cli_transmits_include_covered_to_the_server() -> None:
    client = _CapturingClient()
    cli_mod._one_shot_peer_inbox_page(
        cast("Any", client), "ases-x", 100, after=None, role_after=None,
        observer=True, include_covered=True,
    )
    on = dict(client.arguments)
    cli_mod._one_shot_peer_inbox_page(
        cast("Any", client), "ases-x", 100, after=None, role_after=None, observer=True,
    )
    _check(on.get("include_covered") is True and client.arguments.get("include_covered") is False,
           "the direct inbox route call carries include_covered=True exactly when asked")


def test_models_default_is_floored() -> None:
    request = PeerInboxRequest(
        recipient_agent_id="claude_code", recipient_agent_instance_id=ris._INSTANCE,
    )
    _check(request.include_covered is False, "PeerInboxRequest.include_covered defaults to False")


def main() -> int:
    print("=== role covered-mark floor vs authorization intake smoke ===")
    test_service_reads_past_the_floor_including_the_mark_row()
    test_service_default_is_floored_and_observer_only()
    test_service_paging_keeps_the_floor_off()
    test_read_does_not_move_the_mark()
    test_process_builder_carries_the_flag()
    test_cli_sends_it_on_the_role_fetch_only()
    test_cli_horizon_is_required_and_valid()
    test_cli_horizon_bounds_the_walk()
    test_cli_refuses_a_floored_include_covered_page()
    test_cli_transmits_include_covered_to_the_server()
    test_models_default_is_floored()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
