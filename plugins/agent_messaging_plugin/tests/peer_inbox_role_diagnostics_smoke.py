#!/usr/bin/env python3
"""``peer_inbox`` says which roles it read and what it hid, and the CLI says what
horizon it applied (iss_ab82bd8a, iss_b4e89438; Problem wgr_4b25ff56).

Two gaps, both of the form "the response cannot tell a filtered set from a broken one":

- iss_ab82bd8a: a role holder was notified of a role reply, read ``peer_inbox``, and got
  an empty ``role_entries`` with ``role_floor_applied: true`` (iss_14bedd40). The response
  did not say which roles the read covered, nor how many rows the covered-mark floor or a
  display receipt hid, so the empty page could not be diagnosed from the page. Every
  surface now carries ``role_read_coverage``: one ``{role, covered_mark_present,
  floor_hidden, receipt_hidden}`` entry per role read. Counts only, taken inside the
  window the call already scanned, so a nonzero value is exact and a count is a lower bound.
- iss_b4e89438: ``solet-bridge inbox --since`` returned no record of the horizon it applied,
  so a caller could not tell a bounded set from one an older server left unfiltered. The role
  pages now carry ``since`` to the server, the payload reports ``since_applied`` (``direct``
  the client-side stop, ``role`` the server's echo), and a role page whose
  ``role_since_created_at`` is null or differs fails the command with
  ``role_since_not_applied`` rather than printing an unfiltered set as bounded.

Every check runs the REAL service, persist path, receipt write path, process, routes and CLI
drain against a real-shape state fake; the CLI is driven through ``read_peer_inbox_for_session``,
the function the direct route calls.

Each check names the mutation that turns it red (the mutation sweep runs each one):

- horizon dropped in the CLI merge   -> ``cli.inbox`` emits ``"since_applied": None``
                                        (``CLI reports the applied horizon`` checks)
- null echo tolerated                -> ``_role_horizon_fault`` returns ``None`` for a null echo
                                        (``a null echo fails loud`` checks)
- floor count hard-wired to 0        -> ``floor_hidden=0`` in ``_read_role_section``
                                        (``floor-hidden rows are counted`` checks)
- receipt count hard-wired to 0      -> ``receipt_hidden=0`` in ``_read_role_section``
                                        (``receipt-hidden rows are counted`` checks)
- roles not named                    -> ``_read_role_section`` appends no coverage
                                        (``names the roles read`` checks)
- coverage dropped at the serializer -> ``serialize_peer_inbox_page`` omits ``role_read_coverage``
                                        (the route and MCP checks)
- CLI never sends ``since``          -> ``_one_shot_peer_inbox_page`` passes ``since=None``
                                        (``the role pages carry the horizon`` check)
- naive/aware echo compared raw      -> ``_role_horizon_fault`` compares ``datetime.fromisoformat``
                                        values without ``to_naive_utc`` (``same instant, different
                                        spelling`` checks, iss_19d8af7f N1)
- horizon fault checked before error -> ``_fetch`` records ``_role_horizon_fault`` ahead of the
                                        ``role_section_status == "error"`` check (``a role error is
                                        never masked`` check, iss_19d8af7f N2)
- misplaced "instead" restored       -> ``peer_inbox.json`` reads ``across sessions instead would
                                        resume`` (``the description reads`` check, iss_19d8af7f N3)

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_inbox_role_diagnostics_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "agent_messaging"))

import local_cli_smoke as cli_smoke  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from peer_inbox_horizon_both_sections_smoke import (  # noqa: E402
    _ROLE,
    _bind_role,
    _real_page_reader,
    _role_seconds,
    _service,
)
from peer_inbox_instance_exhaustion_smoke import _INSTANCE_ID, _SESSION_ID, _plugin, _seed  # noqa: E402
from peer_inbox_since_cursor_smoke import _at  # noqa: E402
from peer_inbox_since_surfaces_smoke import _route_client, _streamable_call  # noqa: E402

import agent_messaging_plugin.local_cli.cli as cli_mod  # noqa: E402

_passed = 0
_failed: list[str] = []
_OTHER_ROLE = "Architect"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _write_role(state: RealShapeState, plugin: Any, role: str, index: int, *, second: int) -> None:
    """A role reply written through the REAL persist path at a controlled clock."""
    state.now_iso = lambda: _at(second)
    _service(plugin).persist_role_message(
        recipient_kind="role", recipient_key=role, message_id=f"arm-{role[:1].lower()}{index}",
        sender_agent_id="claude_code", sender_agent_instance_id="agi-lane",
        sender_session_label="Lane", important=True, content=[],
    )


def _bind_other_role(state: RealShapeState) -> None:
    result = state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(_OTHER_ROLE),
                "role": _OTHER_ROLE,
                "holder_kind": HOLDER_KIND_SESSION,
                "agent_instance_id": _INSTANCE_ID,
                "agent_session_id": _SESSION_ID,
                "holder_identity": {"agent_id": "claude_code", "session_label": _OTHER_ROLE},
            },
            "conflict_columns": ["external_id"],
        },
    )
    assert result["action_status"] == "completed", result


def _mark(plugin: Any, role: str, message_id: str) -> None:
    _service(plugin).mark_role_covered(
        recipient_key=role, message_id=message_id,
        attested_by_agent_instance_id=_INSTANCE_ID,
        attested_by_agent_session_id=_SESSION_ID, attested_by_session_label=role,
    )


def _world(*, mark_through: str | None = None, bind: bool = True) -> tuple[RealShapeState, Any]:
    """Six direct rows and five ``_ROLE`` rows (arm-p0..p4 at seconds 0,1,2,4,6)."""
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    if bind:
        _bind_role(state)
    for index, second in enumerate((0, 1, 2, 4, 6)):
        _write_role(state, plugin, _ROLE, index, second=second)
    if mark_through is not None:
        _mark(plugin, _ROLE, mark_through)
    return state, plugin


def _read(plugin: Any, **params: object) -> dict[str, Any]:
    result = plugin.peer_inbox_action(
        {"agent_session_id": _SESSION_ID, "observer": True, "limit": 50, **params}, {},
    )
    assert result["action_status"] == "completed", result
    return cast("dict[str, Any]", result["data"])


def _coverage(page: dict[str, Any]) -> defaultdict[str, defaultdict[str, Any]]:
    """Coverage by role; a missing role or key reads as ``None`` so a page from a release
    without the field FAILS each check rather than crashing the run."""
    by_role: defaultdict[str, defaultdict[str, Any]] = defaultdict(lambda: defaultdict(lambda: None))
    for entry in page.get("role_read_coverage") or []:
        by_role[entry["role"]].update(entry)
    return by_role


def _ack(plugin: Any, page: dict[str, Any]) -> None:
    token = page["role_read_page_token"]
    assert token, page
    _service(plugin).acknowledge_role_read_page(
        agent_session_id=_SESSION_ID, agent_instance_id=_INSTANCE_ID, token=token,
    )


def _ids(page: dict[str, Any]) -> list[str]:
    return _role_seconds(page)


def test_names_the_roles_read() -> None:
    _, plugin = _world()
    page = _read(plugin)
    cover = _coverage(page)
    _check(list(cover) == [_ROLE] and cover[_ROLE]["covered_mark_present"] is False
           and cover[_ROLE]["floor_hidden"] == 0 and cover[_ROLE]["receipt_hidden"] == 0,
           "names the roles read: a holder of one role with no mark reads it, nothing hidden")
    state, plugin = _world()
    _bind_other_role(state)
    _write_role(state, plugin, _OTHER_ROLE, 0, second=3)
    both = _coverage(_read(plugin))
    _check(sorted(both) == sorted([_ROLE, _OTHER_ROLE]),
           "names the roles read: a holder of two roles gets one coverage entry per role")
    _, plugin = _world(bind=False)
    _check(_read(plugin).get("role_read_coverage") == [],
           "names the roles read: a holder with no role reads none, so the list is empty")


def test_floor_hidden_rows_are_counted() -> None:
    _, plugin = _world(mark_through="arm-p2")
    page = _read(plugin)
    cover = _coverage(page)[_ROLE]
    _check(_ids(page) == ["arm-p4", "arm-p3"] and cover["covered_mark_present"] is True
           and cover["floor_hidden"] == 3 and cover["receipt_hidden"] == 0,
           "floor-hidden rows are counted: a mark through the third row hides exactly three")
    _, plugin = _world(mark_through="arm-p4")
    page = _read(plugin)
    cover = _coverage(page)[_ROLE]
    _check(_ids(page) == [] and page["role_floor_applied"] is True and cover["floor_hidden"] == 5,
           "floor-hidden rows are counted: the iss_14bedd40 shape (empty role_entries, floor "
           "applied) says five rows were hidden by the floor")
    covered = _read(plugin, include_covered=True)
    cover = _coverage(covered)[_ROLE]
    _check(len(_ids(covered)) == 5 and cover["floor_hidden"] == 0
           and cover["covered_mark_present"] is False,
           "control: include_covered reads no mark, so it hides nothing and says no mark was applied")


def test_per_role_counts_are_separate() -> None:
    state, plugin = _world(mark_through="arm-p2")
    _bind_other_role(state)
    for index, second in enumerate((0, 1)):
        _write_role(state, plugin, _OTHER_ROLE, index, second=second)
    cover = _coverage(_read(plugin))
    _check(cover[_ROLE]["floor_hidden"] == 3 and cover[_OTHER_ROLE]["floor_hidden"] == 0
           and cover[_OTHER_ROLE]["covered_mark_present"] is False,
           "floor-hidden rows are counted per role: one role's mark never hides the other's rows")


def test_receipt_hidden_rows_are_counted() -> None:
    _, plugin = _world()
    first = _read(plugin, observer=False)
    _ack(plugin, first)
    second = _read(plugin, observer=False)
    cover = _coverage(second)[_ROLE]
    _check(len(_ids(first)) == 5 and _ids(second) == [] and cover["receipt_hidden"] == 5
           and cover["floor_hidden"] == 0,
           "receipt-hidden rows are counted: five rows already shown are hidden by receipts, none by the floor")
    _, plugin = _world(mark_through="arm-p2")
    first = _read(plugin, observer=False)
    _ack(plugin, first)
    second = _read(plugin, observer=False)
    cover = _coverage(second)[_ROLE]
    _check(_ids(first) == ["arm-p4", "arm-p3"] and _ids(second) == []
           and cover["receipt_hidden"] == 2 and cover["floor_hidden"] == 3,
           "receipt-hidden and floor-hidden rows are counted apart: two shown rows, three below the mark")
    observer = _coverage(_read(plugin))[_ROLE]
    _check(observer["receipt_hidden"] == 0,
           "control: an observer read applies no receipt filter, so it reports none hidden by receipts")


def test_a_failed_role_section_reports_no_coverage() -> None:
    _, plugin = _world()
    page = _read(plugin, role_after="not-a-token")
    _check(page["role_section_status"] == "error" and page.get("role_read_coverage") == [],
           "a failed role section claims no coverage (empty list, status error)")


def test_surfaces_carry_the_coverage() -> None:
    state, plugin = _world(mark_through="arm-p2")
    expected = _read(plugin).get("role_read_coverage")
    client, bridge_id = _route_client(state)
    base = f"/api/v1/bridge/{bridge_id}/peer/inbox_for_session?agent_session_id={_SESSION_ID}&observer=true&limit=50"
    route = client.get(base).json()
    _check(route.get("role_read_coverage") == expected and bool(expected) and expected[0]["floor_hidden"] == 3,
           "the inbox_for_session route carries role_read_coverage, identical to the process")
    plain = client.get(f"/api/v1/bridge/{bridge_id}/peer/inbox?limit=50").json()
    _check(bool(expected) and plain.get("role_read_coverage") == expected,
           "the /peer/inbox route carries role_read_coverage, identical to the process")
    response = _streamable_call(state, {"limit": 50})
    mcp = json.loads(response.result["content"][0]["text"])
    _check(bool(expected) and mcp.get("role_read_coverage") == expected,
           "the MCP peer_inbox tool carries role_read_coverage, identical to the process")


def _cli(plugin: Any, args: list[str], *, reader: Any = None) -> tuple[int, dict[str, Any], str]:
    handler, _ = cli_smoke._ack_pages_handler()
    with (
        patch.dict(os.environ, {"AGENT_SESSION_ID": _SESSION_ID}),
        patch.object(cli_mod, "_one_shot_peer_inbox_page", reader or _real_page_reader(plugin)),
    ):
        result = cli_smoke._invoke(args, handler)
    text = result.output.lstrip()
    payload = json.JSONDecoder().raw_decode(text)[0] if text.startswith("{") else {}
    return result.exit_code, payload, result.output


def _horizon(second: int) -> str:
    return datetime.fromisoformat(_at(second)).isoformat()


def test_cli_reports_the_applied_horizon() -> None:
    _, plugin = _world()
    code, payload, _ = _cli(plugin, ["inbox", "--observer", "--include-covered", "--since", _horizon(2)])
    applied = payload.get("since_applied") or {}
    _check(code == 0 and payload.get("complete") is True
           and applied.get("role") is not None and applied.get("direct") is not None,
           "CLI reports the applied horizon: since_applied names a direct stop and a role echo")
    _check(applied.get("role") is not None
           and datetime.fromisoformat(applied["role"]) == datetime.fromisoformat(_at(2))
           and datetime.fromisoformat(applied["direct"]) == datetime.fromisoformat(_at(2)),
           "CLI reports the applied horizon: both sections name the instant that was asked for")
    code, none_set, _ = _cli(plugin, ["inbox", "--observer"])
    _check(code == 0 and none_set.get("since_applied") is None,
           "control: without --since the payload reports no horizon (null), not a made-up one")
    offset = datetime.fromisoformat(_at(2) + "+00:00").isoformat()
    code, shifted, _ = _cli(plugin, ["inbox", "--observer", "--include-covered", "--since", offset])
    _check(code == 0 and shifted.get("complete") is True and shifted.get("since_applied") is not None,
           "CLI reports the applied horizon: an offset-form --since echoes as the same instant, so it is accepted")


def _instance_cursor_keys(call: dict[str, Any]) -> set[str]:
    return {
        key for key in ("since", "since_cursor", "after") if call.get(key)
    }


def _carry_horizon_and_tie_safe_direct_cursor(
    role_calls: list[dict[str, Any]], direct_calls: list[dict[str, Any]],
) -> bool:
    """Role pages keep the horizon; direct pages use since then its opaque cursor."""
    if not role_calls or not direct_calls:
        return False
    role_is_bounded = all(
        _instance_cursor_keys(call) == {"since"} for call in role_calls
    )
    direct_is_tie_safe = _instance_cursor_keys(direct_calls[0]) == {"since"} and all(
        _instance_cursor_keys(call) == {"since_cursor"}
        for call in direct_calls[1:]
    )
    return role_is_bounded and direct_is_tie_safe


def test_cli_sends_the_horizon_on_role_pages_only() -> None:
    _, plugin = _world()
    real = _real_page_reader(plugin)
    calls: list[dict[str, Any]] = []

    def recording(client: Any, session: str, limit: int, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return real(client, session, limit, **kwargs)

    code, _, _ = _cli(plugin, ["inbox", "--observer", "--include-covered", "--since", _horizon(2)], reader=recording)
    role_calls = [c for c in calls if c.get("include_covered")]
    direct_calls = [c for c in calls if not c.get("include_covered")]
    _check(code == 0 and _carry_horizon_and_tie_safe_direct_cursor(role_calls, direct_calls),
           "role pages carry the fixed horizon; direct pages start with since and continue only with since_cursor")


def test_the_horizon_crosses_the_wire_on_role_pages_only() -> None:
    """Real client, real URL: ``since`` is a query parameter of the role fetches and only those."""
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-smoke", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/inbox_for_session"):
            params = dict(request.url.params)
            seen.append(params)
            page = cli_smoke._role_addressed_inbox_page(role_entries=[])
            page["role_since_created_at"] = params.get("since")
            return httpx.Response(200, json=page)
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    with patch.dict(os.environ, {"AGENT_SESSION_ID": _SESSION_ID}):
        result = cli_smoke._invoke(
            ["inbox", "--observer", "--include-covered", "--since", _horizon(2)], handler,
        )
    role = [p for p in seen if p.get("include_covered") == "true"]
    direct = [p for p in seen if p.get("include_covered") != "true"]
    _check(result.exit_code == 0 and _carry_horizon_and_tie_safe_direct_cursor(role, direct),
           "the fixed role horizon and tie-safe direct cursor cross the wire on their own pages")


def _stripping_reader(plugin: Any, *, drop_echo: bool, shifted_echo: str | None = None) -> Any:
    """A server that ignores ``since`` (an older release), or echoes a different instant."""
    real = _real_page_reader(plugin)

    def fetch(client: Any, session: str, limit: int, **kwargs: Any) -> dict[str, Any]:
        page = real(client, session, limit, **{k: v for k, v in kwargs.items() if k != "since"})
        if shifted_echo is not None:
            page["role_since_created_at"] = shifted_echo
        elif drop_echo:
            page["role_since_created_at"] = None
        return page

    return fetch


def test_cli_fails_loud_when_the_horizon_is_not_applied() -> None:
    _, plugin = _world()
    args = ["inbox", "--observer", "--include-covered", "--since", _horizon(2)]
    code, payload, _ = _cli(plugin, args, reader=_stripping_reader(plugin, drop_echo=True))
    error = str(payload.get("role_section_error"))
    _check(code != 0 and payload.get("complete") is False and error.startswith("role_since_not_applied")
           and payload.get("role_section_status") == "error",
           "a null echo fails loud: non-zero exit, complete false, the named error role_since_not_applied")
    _check(isinstance(payload.get("since_applied"), dict) and payload["since_applied"].get("role") is None,
           "a null echo fails loud: since_applied.role is null, never a claimed horizon")
    code, payload, _ = _cli(plugin, args, reader=_stripping_reader(plugin, drop_echo=False, shifted_echo=_at(0)))
    _check(code != 0 and str(payload.get("role_section_error")).startswith("role_since_not_applied"),
           "a mismatched echo fails loud: a role page bounded by another instant is not the window asked for")
    code, payload, _ = _cli(plugin, args)
    _check(code == 0 and payload.get("role_section_error") is None,
           "control: the same read against a server that applies the horizon succeeds")


def test_cli_reports_the_role_coverage() -> None:
    _, plugin = _world(mark_through="arm-p2")
    code, payload, _ = _cli(plugin, ["inbox", "--observer"])
    cover = _coverage(payload)
    _check(code == 0 and cover[_ROLE]["floor_hidden"] == 3,
           "floor-hidden rows are counted: the CLI payload carries the first role page's coverage")
    _check(cover[_ROLE]["covered_mark_present"] is True,
           "names the roles read: the CLI payload names the role and says its mark was applied")


def test_the_horizon_echo_is_compared_as_the_same_instant() -> None:
    """iss_19d8af7f N1: a naive and an aware spelling of one instant are not a mismatch."""
    naive, aware = _at(2), _at(2) + "+00:00"
    plus_two = (datetime.fromisoformat(_at(2) + "+00:00").astimezone(timezone(timedelta(hours=2)))).isoformat()
    for sent, echoed, spelling in (
        (naive, aware, "naive sent, aware echo"),
        (aware, naive, "aware sent, naive echo"),
        (plus_two, naive, "UTC+2 sent, naive-UTC echo"),
    ):
        fault = cli_mod._role_horizon_fault({"role_since_created_at": echoed}, sent)
        _check(fault is None, f"same instant, different spelling: {spelling} is not role_since_not_applied")
    different = cli_mod._role_horizon_fault({"role_since_created_at": _at(3) + "+00:00"}, naive)
    _check(isinstance(different, str) and different.startswith("role_since_not_applied"),
           "control: an aware echo of a DIFFERENT instant is still a mismatch")
    _, plugin = _world()
    args = ["inbox", "--observer", "--include-covered", "--since", _horizon(2)]
    code, payload, _ = _cli(plugin, args, reader=_stripping_reader(plugin, drop_echo=False, shifted_echo=aware))
    _check(code == 0 and payload.get("role_section_error") is None,
           "same instant, different spelling: the CLI accepts a server that echoes the horizon as an aware timestamp")


def _role_error_reader(plugin: Any) -> Any:
    """A role page that failed: status error, a named error, and (as a real failure) no ``since`` echo."""
    real = _real_page_reader(plugin)

    def fetch(client: Any, session: str, limit: int, **kwargs: Any) -> dict[str, Any]:
        page = real(client, session, limit, **kwargs)
        if kwargs.get("include_covered"):
            page["role_section_status"] = "error"
            page["role_section_error"] = "role_store_unavailable"
            page["role_since_created_at"] = None
        return page

    return fetch


def test_a_role_error_is_never_masked_by_the_horizon_fault() -> None:
    """iss_19d8af7f N2: the role page's own error wins over the not-applied echo it caused."""
    _, plugin = _world()
    args = ["inbox", "--observer", "--include-covered", "--since", _horizon(2)]
    code, payload, _ = _cli(plugin, args, reader=_role_error_reader(plugin))
    _check(code != 0 and payload.get("role_section_error") == "role_store_unavailable",
           "a role error is never masked: the named role error is reported, not role_since_not_applied")
    code, payload, _ = _cli(plugin, args, reader=_stripping_reader(plugin, drop_echo=True))
    _check(code != 0 and str(payload.get("role_section_error")).startswith("role_since_not_applied"),
           "control: a healthy role page with a null echo still reports role_since_not_applied")


def test_the_description_reads() -> None:
    """iss_19d8af7f N3: ``instead`` sits on the sentence it contrasts with."""
    process = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes" / "peer_inbox.json"
    description = json.loads(process.read_text())["description"]
    _check("across sessions would instead resume reading OLDER messages" in description
           and "across sessions instead would" not in description,
           "the description reads: the next_role_cursor warning puts instead before resume")


def main() -> int:
    print("=== peer_inbox role diagnostics and CLI horizon smoke ===")
    test_names_the_roles_read()
    test_floor_hidden_rows_are_counted()
    test_per_role_counts_are_separate()
    test_receipt_hidden_rows_are_counted()
    test_a_failed_role_section_reports_no_coverage()
    test_surfaces_carry_the_coverage()
    test_cli_reports_the_applied_horizon()
    test_cli_sends_the_horizon_on_role_pages_only()
    test_the_horizon_crosses_the_wire_on_role_pages_only()
    test_cli_fails_loud_when_the_horizon_is_not_applied()
    test_cli_reports_the_role_coverage()
    test_the_horizon_echo_is_compared_as_the_same_instant()
    test_a_role_error_is_never_masked_by_the_horizon_fault()
    test_the_description_reads()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
