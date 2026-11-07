#!/usr/bin/env python3
"""A time horizon bounds BOTH ``peer_inbox`` sections on every surface
(Problem wgr_4b25ff56; issues iss_44d9f70a, iss_27efeab9, iss_14bedd40).

The inbox has two sections that page in the same direction (newest-first) with
different cursors: the instance section by ``after`` (rows OLDER than a time) or
``since`` (rows NEWER than a time), the role section by the opaque ``role_after``.
Before this fix the horizon reached only one section on each surface:

- ``solet-bridge inbox --since`` stopped the role walk at the horizon but drained
  the direct section against an empty mark, so every direct row came back as new
  (iss_44d9f70a);
- ``peer_inbox since`` bounded the instance section and ignored the role section,
  which is why a reader who used a time cursor could not tell old role mail from
  new (iss_27efeab9; ``after`` is a backward cursor by design and is unchanged);
- ``GET .../peer/inbox_for_session`` did not accept ``since`` at all.

The rule pinned here: a row created at or before the horizon never comes back as
new from either section, a row created after it is never skipped, and a section
that did not apply the horizon says so (``role_since_created_at`` is null). Every
check runs the REAL service, section-drain code, route and process against a
real-shape state fake; the CLI is driven through ``read_peer_inbox_for_session``,
the function the direct route calls. The role-reply check (iss_14bedd40) is the
control that shows the covered-mark floor keeps a reply written after the mark.

Each check names the mutation that turns it red:

- instance horizon reverted to ""      -> ``cli.inbox`` passes ``""`` to the direct drain
- ``since`` dropped at the route       -> remove ``since`` from ``inbox_for_session``
- role section ignores the horizon     -> drop ``since=`` from ``_query_role_page``
- role floor re-applied over new rows  -> make ``_apply_role_floor`` keep ``>= mark`` out
  (``kept = [r for r in records if _role_row_key(r) < mark]``)
- echo dropped                         -> serialize ``role_since_created_at`` as ``None``
- watch drain ignores its instance mark-> ``_drain_inbox`` passes ``""`` to the instance drain

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_inbox_horizon_both_sections_smoke.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "agent_messaging"))

import local_cli_smoke as cli_smoke  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.models import PeerInboxRequest, RoleSectionStatus  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.schema import NAMESPACE, TABLE_AGENT_MESSAGE  # noqa: E402
from peer_inbox_instance_exhaustion_smoke import _INSTANCE_ID, _SESSION_ID, _plugin, _seed  # noqa: E402
from peer_inbox_since_cursor_smoke import _at  # noqa: E402
from peer_inbox_since_surfaces_smoke import _route_client, _streamable_call  # noqa: E402

import agent_messaging_plugin.local_cli.cli as cli_mod  # noqa: E402
from agent_messaging_plugin.peer_direct_reads import read_peer_inbox_for_session  # noqa: E402

_passed = 0
_failed: list[str] = []
_ROLE = "Project-Solet-Main"
_HORIZON = 2  # direct rows 0..2 and role rows at seconds 0..2 are at/before it


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _service(plugin: Any) -> Any:
    return plugin._service  # noqa: SLF001


def _write_role(state: RealShapeState, plugin: Any, index: int, *, second: int) -> None:
    """A role reply written through the REAL persist path at a controlled clock."""
    state.now_iso = lambda: _at(second)
    _service(plugin).persist_role_message(
        recipient_kind="role", recipient_key=_ROLE, message_id=f"arm-r{index}",
        sender_agent_id="claude_code", sender_agent_instance_id="agi-lane",
        sender_session_label="Lane", important=True, content=[],
    )


def _bind_role(state: RealShapeState) -> None:
    """Hold ``_ROLE`` as a session binding, in the row shape the real table accepts."""
    result = state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(_ROLE),
                "role": _ROLE,
                "holder_kind": HOLDER_KIND_SESSION,
                "agent_instance_id": _INSTANCE_ID,
                "agent_session_id": _SESSION_ID,
                "holder_identity": {"agent_id": "claude_code", "session_label": _ROLE},
            },
            "conflict_columns": ["external_id"],
        },
    )
    assert result["action_status"] == "completed", result


def _world(*, mark_through: int | None = None) -> tuple[RealShapeState, Any]:
    """Six direct rows (seconds 0..5) and role rows at seconds 0,1,2,4,6.

    ``mark_through`` attests the role covered through the role row at that second.
    """
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    _bind_role(state)
    for index, second in enumerate((0, 1, 2, 4, 6)):
        _write_role(state, plugin, index, second=second)
    if mark_through is not None:
        index = (0, 1, 2, 4, 6).index(mark_through)
        _service(plugin).mark_role_covered(
            recipient_key=_ROLE, message_id=f"arm-r{index}",
            attested_by_agent_instance_id=_INSTANCE_ID,
            attested_by_agent_session_id=_SESSION_ID, attested_by_session_label=_ROLE,
        )
    return state, plugin


def _role_seconds(page: dict[str, Any]) -> list[str]:
    return [e["message"]["id"] for e in page["role_entries"]]


def _direct_cursors(page: dict[str, Any]) -> list[int]:
    return [e["message"]["cursor"] for e in page["entries"]]


def _read(plugin: Any, **params: object) -> dict[str, Any]:
    result = plugin.peer_inbox_action(
        {"agent_session_id": _SESSION_ID, "observer": True, "limit": 50, **params}, {},
    )
    assert result["action_status"] == "completed", result
    return cast("dict[str, Any]", result["data"])


def test_service_horizon_bounds_the_role_section() -> None:
    _, plugin = _world()
    unbounded = _read(plugin)
    _check(_role_seconds(unbounded) == ["arm-r4", "arm-r3", "arm-r2", "arm-r1", "arm-r0"]
           and unbounded.get("role_since_created_at") is None,
           "control: with no horizon the role section returns every row and echoes null")
    bounded = _read(plugin, since=_at(_HORIZON))
    _check(_role_seconds(bounded) == ["arm-r4", "arm-r3"],
           "iss_27efeab9: since bounds the role section to rows newer than the horizon")
    _check(_direct_cursors(bounded) == [3, 4, 5],
           "iss_27efeab9: since still bounds the direct section, oldest-first")
    _check(bounded.get("role_since_created_at") == _at(_HORIZON),
           "since is echoed as role_since_created_at, the proof the role section applied it")
    boundary = _read(plugin, since=_at(2))
    _check("arm-r2" not in _role_seconds(boundary),
           "the horizon is strict: a role row created AT the horizon is not new")


def test_role_paging_stays_inside_the_window() -> None:
    _, plugin = _world()
    seen: list[str] = []
    role_after: str | None = None
    for _ in range(6):
        page = _read(plugin, since=_at(_HORIZON), limit=1, **({"role_after": role_after} if role_after else {}))
        seen.extend(_role_seconds(page))
        role_after = page["next_role_cursor"]
        if role_after is None:
            break
    _check(seen == ["arm-r4", "arm-r3"] and role_after is None,
           "paging the role section under a horizon ends at the horizon, never reaching older rows")


def test_horizon_and_floor_are_independent() -> None:
    _, plugin = _world(mark_through=2)
    default = _read(plugin)
    _check(_role_seconds(default) == ["arm-r4", "arm-r3"] and default["role_floor_applied"] is True,
           "control: the floor alone hides rows covered through the mark")
    both = _read(plugin, since=_at(4))
    _check(_role_seconds(both) == ["arm-r4"] and both["role_floor_applied"] is False,
           "a horizon newer than the mark bounds the role section on its own")
    older = _read(plugin, since=_at(0))
    _check(_role_seconds(older) == ["arm-r4", "arm-r3"] and older["role_floor_applied"] is True,
           "a horizon older than the mark leaves the floor in force")


def test_reply_written_after_the_read_mark_appears() -> None:
    state, plugin = _world(mark_through=6)
    before = _read(plugin)
    _check(_role_seconds(before) == [] and before["role_floor_applied"] is True,
           "iss_14bedd40 control: every row is covered, so the floored page is empty")
    _write_role(state, plugin, 5, second=9)
    after = _read(plugin)
    _check(_role_seconds(after) == ["arm-r5"] and after["role_floor_applied"] is True,
           "iss_14bedd40: a role reply written after the mark appears, floor still applied")
    under_horizon = _read(plugin, since=_at(7))
    _check(_role_seconds(under_horizon) == ["arm-r5"],
           "iss_14bedd40: the same reply appears under a horizon older than it")
    skipped = _read(plugin, since=_at(9))
    _check(_role_seconds(skipped) == [],
           "a reply created at the horizon is not new; none newer than it was skipped")


def test_role_fault_reports_no_horizon() -> None:
    _, plugin = _world()
    page = _read(plugin, since=_at(_HORIZON), role_after="not-a-token")
    _check(page["role_section_status"] == RoleSectionStatus.ERROR.value
           and page.get("role_since_created_at") is None,
           "a failed role section never claims it applied the horizon")


def test_route_inbox_for_session_accepts_since() -> None:
    state, _ = _world()
    client, bridge_id = _route_client(state)
    base = f"/api/v1/bridge/{bridge_id}/peer/inbox_for_session?agent_session_id={_SESSION_ID}&observer=true&limit=50"
    horizon = _at(_HORIZON)
    with_since = client.get(f"{base}&since={horizon}")
    page = with_since.json()
    _check(with_since.status_code == 200 and _direct_cursors(page) == [3, 4, 5]
           and _role_seconds(page) == ["arm-r4", "arm-r3"],
           "iss_27efeab9: inbox_for_session since bounds both sections")
    _check(page.get("role_since_created_at") == horizon,
           "inbox_for_session echoes the role horizon")
    without = client.get(base).json()
    _check(len(without["role_entries"]) == 5 and without.get("role_since_created_at") is None,
           "control: inbox_for_session with no since is unbounded and echoes null")
    backward = client.get(f"{base}&after={_at(4)}").json()
    _check(_direct_cursors(backward) == [3, 2, 1, 0],
           "inbox_for_session forwards after: rows older than it, newest-first")
    bad = client.get(f"{base}&since=last-tuesday")
    _check(bad.status_code == 400 and bad.json()["code"] == "invalid_since",
           "inbox_for_session refuses a malformed since loudly")
    both = client.get(f"{base}&since={horizon}&after={_at(4)}")
    _check(both.status_code in (400, 409, 422),
           "inbox_for_session refuses naming both opposite cursors")


def test_route_inbox_bounds_both_sections() -> None:
    state, _ = _world()
    client, bridge_id = _route_client(state)
    page = client.get(f"/api/v1/bridge/{bridge_id}/peer/inbox?limit=50&since={_at(_HORIZON)}").json()
    _check(_direct_cursors(page) == [3, 4, 5] and _role_seconds(page) == ["arm-r4", "arm-r3"]
           and page.get("role_since_created_at") == _at(_HORIZON),
           "iss_27efeab9: the /peer/inbox route bounds both sections under since")


def test_streamable_tool_bounds_both_sections() -> None:
    state, _ = _world()
    response = _streamable_call(state, {"since": _at(_HORIZON), "limit": 50})
    page = json.loads(response.result["content"][0]["text"])
    _check(_direct_cursors(page) == [3, 4, 5] and _role_seconds(page) == ["arm-r4", "arm-r3"]
           and page.get("role_since_created_at") == _at(_HORIZON),
           "iss_27efeab9: the MCP tool bounds both sections under since")


def test_after_stays_a_backward_cursor() -> None:
    _, plugin = _world()
    page = _read(plugin, after=_at(4))
    _check(_direct_cursors(page) == [3, 2, 1, 0],
           "after is unchanged: it returns rows OLDER than the timestamp, newest-first")
    _check(len(page["role_entries"]) == 5 and page.get("role_since_created_at") is None,
           "after never touches the role section and echoes no horizon")


def _real_page_reader(plugin: Any) -> Any:
    """The CLI's page fetch, served by the real read the direct route performs."""

    def fetch(_client: Any, session: str, limit: int, **kwargs: Any) -> dict[str, Any]:
        raw = {"agent_session_id": session, "limit": limit, **{
            k: v for k, v in kwargs.items() if v not in (None, False)
        }}
        return read_peer_inbox_for_session(
            raw, peer_registry=plugin._peer_registry, service=_service(plugin),  # noqa: SLF001
        )

    return fetch


def _cli(plugin: Any, args: list[str]) -> dict[str, Any]:
    handler, _ = cli_smoke._ack_pages_handler()
    with (
        patch.dict(os.environ, {"AGENT_SESSION_ID": _SESSION_ID}),
        patch.object(cli_mod, "_one_shot_peer_inbox_page", _real_page_reader(plugin)),
    ):
        result = cli_smoke._invoke(args, handler)
    text = result.output.lstrip()
    return json.JSONDecoder().raw_decode(text)[0] if text.startswith("{") else {}


def _sections(payload: dict[str, Any], section: str) -> list[str]:
    return [m["message"]["id"] for m in payload.get("messages", []) if m["section"] == section]


def test_cli_since_bounds_the_direct_section() -> None:
    _, plugin = _world()
    horizon = datetime.fromisoformat(_at(_HORIZON)).isoformat()
    bounded = _cli(plugin, ["inbox", "--observer", "--include-covered", "--since", horizon])
    direct = _sections(bounded, "direct")
    _check(len(direct) == 3 and bounded.get("complete") is True,
           "iss_44d9f70a: --since returns only the direct rows newer than the horizon, complete")
    _check(_sections(bounded, "role") == ["arm-r3", "arm-r4"],
           "--since returns only the role rows newer than the horizon, oldest-first")
    everything = _cli(plugin, ["inbox", "--observer"])
    _check(len(_sections(everything, "direct")) == 6 and len(_sections(everything, "role")) == 5,
           "control: without --since the CLI still returns every direct and role row")
    late = _cli(plugin, ["inbox", "--observer", "--include-covered", "--since", datetime.fromisoformat(_at(5)).isoformat()])
    _check(_sections(late, "direct") == [] and late.get("complete") is True,
           "a horizon at the newest direct row returns no direct rows and is still complete")


def test_cli_horizon_accepts_an_offset_form() -> None:
    _, plugin = _world()
    offset = datetime.fromisoformat(_at(_HORIZON) + "+00:00").isoformat()
    payload = _cli(plugin, ["inbox", "--observer", "--include-covered", "--since", offset])
    _check(len(_sections(payload, "direct")) == 3,
           "a UTC-offset --since bounds the direct section the same as the naive form")


def test_cli_since_continuation_is_tie_safe() -> None:
    """The CLI must echo the opaque cursor when a timestamp group spans pages."""
    state, plugin = _world()
    for row in state.rows(NAMESPACE, TABLE_AGENT_MESSAGE):
        if str(row.get("id", "")).startswith("agm-_"):
            row["created_at"] = _at(0)
    payload = _cli(
        plugin,
        [
            "inbox", "--observer", "--include-covered",
            "--limit", "2", "--since", _at(-1),
        ],
    )
    direct_cursors = [
        message["message"]["cursor"]
        for message in payload.get("messages", [])
        if message["section"] == "direct"
    ]
    _check(
        direct_cursors == list(range(6)) and payload.get("complete") is True,
        "CLI echoes next_since_cursor and reaches equal-timestamp rows exactly once",
    )


def test_watch_drain_applies_its_marks_to_both_sections() -> None:
    _, plugin = _world()
    reader = _real_page_reader(plugin)

    class _Client:
        def peer_inbox(self, **kwargs: Any) -> dict[str, Any]:
            return reader(None, _SESSION_ID, kwargs["limit"], **{
                k: v for k, v in kwargs.items() if k in ("after", "role_after", "observer")
            })

    with tempfile.TemporaryDirectory() as tmp:
        marks = Path(tmp) / "marks.json"
        cli_mod.write_watch_marks(marks, instance_after=_at(_HORIZON), role_high_water=_at(_HORIZON))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_mod._drain_inbox(cast("Any", _Client()), None, marks)  # noqa: SLF001
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    instance = [e["entry"]["message"]["cursor"] for e in lines if e["section"] == "entries"]
    role = [e["entry"]["message"]["id"] for e in lines if e["section"] == "role_entries"]
    _check(instance == [3, 4, 5] and role == ["arm-r3", "arm-r4"],
           "class check: the watch drain emits only rows newer than its marks, on both sections")


def test_request_default_has_no_horizon() -> None:
    request = PeerInboxRequest(recipient_agent_id="claude_code", recipient_agent_instance_id=_INSTANCE_ID)
    _check(request.since_created_at is None, "PeerInboxRequest.since_created_at defaults to None")


def main() -> int:
    print("=== peer_inbox horizon bounds both sections smoke ===")
    test_service_horizon_bounds_the_role_section()
    test_role_paging_stays_inside_the_window()
    test_horizon_and_floor_are_independent()
    test_reply_written_after_the_read_mark_appears()
    test_role_fault_reports_no_horizon()
    test_route_inbox_for_session_accepts_since()
    test_route_inbox_bounds_both_sections()
    test_streamable_tool_bounds_both_sections()
    test_after_stays_a_backward_cursor()
    test_cli_since_bounds_the_direct_section()
    test_cli_horizon_accepts_an_offset_form()
    test_cli_since_continuation_is_tie_safe()
    test_watch_drain_applies_its_marks_to_both_sections()
    test_request_default_has_no_horizon()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
