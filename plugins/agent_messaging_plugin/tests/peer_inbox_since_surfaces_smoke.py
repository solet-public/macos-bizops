#!/usr/bin/env python3
"""``since`` reaches the service from every ``peer_inbox`` surface (iss_17aefd54).

``peer_inbox_since_cursor_smoke`` pins the semantics through the platform
process. The same page is also served by the localhost ``/peer/inbox`` route,
the Streamable-HTTP MCP tool and the stdio MCP bridge tool (which forwards to
the route), and each parses its own arguments: a surface that dropped ``since``
would hand its callers the newest-first page back with no error. Each surface is
driven end to end against the real service and rows, and the advertised tool
text is checked against the behavior it describes. Hermetic.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402, TC002
from ananta.llm.agent_messaging.models import PeerInboxRequest  # noqa: E402
from ananta.llm.agent_messaging.service import AgentRequestInvalidError  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from peer_inbox_instance_exhaustion_smoke import _AGENT_ID, _INSTANCE_ID, _SESSION_ID, _plugin, _seed  # noqa: E402
from peer_inbox_since_cursor_smoke import _at, _call  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.local_cli.client import BridgeClient  # noqa: E402
from agent_messaging_plugin.mcp_bridge import __main__ as stdio_bridge  # noqa: E402
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    DispatchContext,
    JsonRpcError,
    JsonRpcRequest,
    dispatch_request,
)
from agent_messaging_plugin.mcp_streamable.session import StreamableSession  # noqa: E402
from agent_messaging_plugin.mcp_streamable.tools import TOOLS  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_inbox_view import InvalidInboxCursorError  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _cursors(page: dict[str, Any]) -> list[int]:
    return [entry["message"]["cursor"] for entry in page["entries"]]


def test_cov_base_red() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    result = _call(_plugin(state), since=_at(1), limit=2)
    assert result["action_status"] == "completed", result
    page = result["data"]
    assert _cursors(page) == [2, 3], page
    _check("read_coverage" in page, "cov: page statement present")


def _cov_page(page: dict[str, Any]) -> None:
    for section, field in (("direct", "entries"), ("role", "role_entries")):
        coverage = page["read_coverage"][section]
        entries = page[field]
        _check(coverage["count"] == len(entries), f"cov: {section} exact count")
        for name, index in (("first", 0), ("last", -1)):
            expected = {key: entries[index]["message"][key] for key in ("created_at", "id")} if entries else None
            _check(coverage[name] == expected, f"cov: {section} {name} exact key")
        _check(coverage["unread_count"] is None, f"cov: {section} unread unknown")
        _check(coverage["range_semantics"] == "returned_rows_only", f"cov: {section} returned rows only")


def test_cov_surfaces() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    args = {"since": _at(1), "limit": 2, "observer": True}
    process = _call(_plugin(state), **args)["data"]
    client, bridge_id = _route_client(state)
    route = client.get(f"/api/v1/bridge/{bridge_id}/peer/inbox", params=args).json()
    stream = json.loads(_streamable_call(state, args).result["content"][0]["text"])
    forwarder = _CapturingForwarder()

    async def route_get(path: str) -> dict[str, Any]:
        return cast("dict[str, Any]", client.get(path.replace("agc-stdio", bridge_id)).json())

    try:
        with patch.object(forwarder, "_get", route_get):
            stdio = asyncio.run(stdio_bridge._tool_peer_inbox(forwarder, args))  # noqa: SLF001
    finally:
        asyncio.run(forwarder._client.aclose())  # noqa: SLF001
    stdio_page = stdio
    for page in (process, route, stream, stdio_page):
        _cov_page(page)
        _check(page["read_coverage"] == process["read_coverage"], "cov: equivalent surfaces")


def test_cov_roles() -> None:
    # Reuse the real service/RealShapeState role fixture without adding a module.
    from peer_inbox_horizon_both_sections_smoke import _world  # noqa: PLC0415

    _, plugin = _world(mark_through=2)
    first = _call(plugin, since=_at(0), limit=1, observer=True)["data"]
    _cov_page(first)
    coverage = first["read_coverage"]["role"]
    references = coverage["role_read_coverage"]
    _check(len(references) == len(first["role_read_coverage"]) and all(all(ref[key] == value for key, value in old.items()) for ref, old in zip(references, first["role_read_coverage"], strict=True)), "cov: role hidden references")
    token = first["next_role_cursor"]
    second = _call(plugin, since=_at(0), role_after=token, limit=1, observer=True)["data"]
    _cov_page(second)
    role = second["read_coverage"]["role"]
    _check(role["role_after"] == token and role["effective_since"] == _at(0), "cov: role boundary provenance")
    covered = _call(plugin, observer=True, include_covered=True, limit=50)["data"]
    _cov_page(covered)
    _check(covered["read_coverage"]["role"]["include_covered"] is True, "cov: include covered explicit")
    normal = _call(plugin, limit=50)["data"]
    _cov_page(normal)
    _check(normal["read_coverage"]["role"]["role_floor_applied"] is True, "cov: normal role floor explicit")
    with patch.object(plugin._service, "_enumerate_held_roles", side_effect=ValueError("cov-role-fault")):  # noqa: SLF001
        failed = _call(plugin, observer=True)["data"]
    role = failed["read_coverage"]["role"]
    _check(role["status"] == "error" and role["count"] is None and role["exhausted"] is None, "cov: role fault unknown not empty")


def _route_client(state: RealShapeState) -> tuple[TestClient, str]:
    plugin = _plugin(state)
    registry = plugin._peer_registry  # noqa: SLF001
    manager = BridgeSessionManager(
        session_id_factory=lambda _n: "ags-since",
        idle_timeout_s=3600,
        max_pending_events=50,
        long_poll_timeout_s=1,
    )
    bridge_id = manager.open(solet_name="", parent_pid=77).bridge_id
    registry.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id=_AGENT_ID,
            agent_instance_id=_INSTANCE_ID,
            session_label="Instance-Reader",
            parent_pid=77,
            agent_session_id=_SESSION_ID,
        ),
    )
    app = FastAPI()
    register_routes(
        app,
        bridge_manager=manager,
        peer_registry=registry,
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, plugin._service),  # noqa: SLF001
        config={"long_poll_timeout_seconds": 1},
        state_service=cast(StateManagementInterface, state),
    )
    return TestClient(app), bridge_id


def test_route() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    client, bridge_id = _route_client(state)
    base = f"/api/v1/bridge/{bridge_id}/peer/inbox?limit=50"
    since = client.get(f"{base}&since={_at(2)}")
    page = since.json()
    _check(since.status_code == 200 and _cursors(page) == [3, 4, 5], "route: since returns the rows newer than T, oldest-first")
    _check(page["next_since_created_at"] == _at(5) and page["next_after_created_at"] is None, "route: a since page carries only the forward cursor")
    first = client.get(f"/api/v1/bridge/{bridge_id}/peer/inbox?limit=2&since={_at(2)}").json()
    continued = client.get(
        f"/api/v1/bridge/{bridge_id}/peer/inbox?limit=2&since_cursor={first['next_since_cursor']}",
    ).json()
    _check(
        _cursors(first) == [3, 4] and _cursors(continued) == [5],
        "route: next_since_cursor continues the forward page",
    )
    after = client.get(f"{base}&after={_at(2)}").json()
    _check(_cursors(after) == [1, 0] and after["next_since_created_at"] is None, "route: after still walks backward")
    aware_after = client.get(f"{base}&after={_at(2)}Z").json()
    _check(_cursors(aware_after) == [1, 0], "route: an offset-aware after cursor reaches the same storage instant")
    both = client.get(f"{base}&after={_at(2)}&since={_at(1)}")
    _check(both.status_code == 400, "route: naming both cursors is refused")
    bad = client.get(f"{base}&since=last-tuesday")
    _check(bad.status_code == 400 and bad.json()["code"] == "invalid_since", "route: a malformed since is a 400 invalid_since")


def _streamable_call(state: RealShapeState, arguments: dict[str, Any]) -> Any:
    plugin = _plugin(state)
    binding = BridgeBinding(
        bridge_id="agc-stream",
        agent_id=_AGENT_ID,
        agent_instance_id=_INSTANCE_ID,
        session_label="Instance-Reader",
        parent_pid=1,
        agent_session_id=_SESSION_ID,
    )
    session = StreamableSession(
        mcp_session_id="mcp-since",
        bridge_id="agc-stream",
        session_id="ags-stream",
        agent_id=_AGENT_ID,
        agent_instance_id=_INSTANCE_ID,
        session_label="Instance-Reader",
        binding=binding,
        agent_session_id=_SESSION_ID,
    )
    context = DispatchContext(
        bridge_manager=cast(Any, object()),
        peer_registry=plugin._peer_registry,  # noqa: SLF001
        platform_surface=cast(Any, object()),
        agent_messaging_service=cast(Any, plugin._service),  # noqa: SLF001
        state_service=cast(StateManagementInterface, state),
        solet_name="example-test",
    )
    return dispatch_request(
        JsonRpcRequest(method="tools/call", params={"name": "peer_inbox", "arguments": arguments}, id=1),
        session=session,
        context=context,
    )


def test_streamable_dispatch() -> None:
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    response = _streamable_call(state, {"since": _at(2), "limit": 50})
    payload = json.loads(response.result["content"][0]["text"])
    _check(_cursors(payload) == [3, 4, 5], "streamable: since returns the rows newer than T, oldest-first")
    _check(payload["next_since_created_at"] == _at(5), "streamable: the forward cursor is returned")
    first_response = _streamable_call(state, {"since": _at(2), "limit": 2})
    first = json.loads(first_response.result["content"][0]["text"])
    second_response = _streamable_call(
        state,
        {"since_cursor": first["next_since_cursor"], "limit": 2},
    )
    second = json.loads(second_response.result["content"][0]["text"])
    _check(
        _cursors(first) == [3, 4] and _cursors(second) == [5],
        "streamable: next_since_cursor continues the forward page",
    )
    aware_after = _streamable_call(state, {"after": _at(2) + "Z", "limit": 50})
    aware_payload = json.loads(aware_after.result["content"][0]["text"])
    _check(
        _cursors(aware_payload) == [1, 0],
        "streamable: an offset-aware after cursor reaches the same storage instant",
    )
    for label, arguments in (
        ("naming both cursors", {"since": _at(1), "after": _at(2)}),
        ("a malformed since", {"since": "last-tuesday"}),
        ("a non-string since", {"since": 12345}),
    ):
        try:
            _streamable_call(state, arguments)
        except JsonRpcError as exc:
            _check("since" in str(exc), f"streamable: {label} is refused with a JSON-RPC error naming since")
            if label == "a non-string since":
                _check("must be a string" in str(exc), "streamable: a non-string since is refused as a type error, not parsed")
        else:
            _check(False, f"streamable: {label} is refused")


def test_invalid_since_cursor_is_rejected_on_every_surface() -> None:
    """Presence and strict token bytes fail closed at every entry point."""
    state = RealShapeState()
    _seed(state, count=6, threads=2)
    plugin = _plugin(state)
    first = _call(plugin, since=_at(-1), limit=1)
    valid = cast("str", first["data"]["next_since_cursor"])
    cases = (
        ("empty token", "", False),
        ("whitespace token", " \t", False),
        ("since plus an empty token", "", True),
        ("junk-wrapped valid token", f"!!{valid}!!", False),
        ("truncated token", valid[:-1], False),
    )
    client, bridge_id = _route_client(state)
    route = f"/api/v1/bridge/{bridge_id}/peer/inbox"
    session_route = f"{route}_for_session"

    for label, cursor, with_since in cases:
        arguments: dict[str, Any] = {"since_cursor": cursor, "limit": 50}
        if with_since:
            arguments["since"] = _at(-1)

        process = _call(plugin, **arguments)
        _check(
            process["action_status"] == "failed",
            f"process: {label} is rejected",
        )

        route_response = client.get(route, params=arguments)
        _check(
            route_response.status_code == 400,
            f"bridge inbox route: {label} is rejected",
        )

        session_params = {
            "agent_session_id": _SESSION_ID,
            "observer": "true",
            **arguments,
        }
        session_response = client.get(session_route, params=session_params)
        _check(
            session_response.status_code == 400,
            f"inbox_for_session route: {label} is rejected",
        )

        try:
            _streamable_call(state, arguments)
        except JsonRpcError:
            _check(True, f"streamable: {label} is rejected")
        else:
            _check(False, f"streamable: {label} is rejected")

        try:
            plugin._service.peer_inbox(  # noqa: SLF001
                PeerInboxRequest(
                    recipient_agent_id=_AGENT_ID,
                    recipient_agent_instance_id=_INSTANCE_ID,
                    recipient_agent_session_id=_SESSION_ID,
                    since_created_at=(datetime.fromisoformat(_at(-1)) if with_since else None),
                    since_cursor=cursor,
                    limit=50,
                ),
            )
        except AgentRequestInvalidError:
            _check(True, f"direct service: {label} is rejected")
        else:
            _check(False, f"direct service: {label} is rejected")


class _CapturingLocalClient(BridgeClient):
    def __init__(self) -> None:
        self.requested_path = ""

    def _require_bridge(self) -> str:
        return "agc-local-cli"

    def _get(self, path: str) -> dict[str, Any]:
        self.requested_path = path
        return {}


def test_local_cli_client_rejects_present_empty_since_cursor() -> None:
    """The one-shot client must not erase an explicitly supplied token."""
    client = _CapturingLocalClient()
    for label, cursor in (("empty", ""), ("whitespace", " \t")):
        try:
            client.peer_inbox_for_session(
                agent_session_id=_SESSION_ID,
                limit=5,
                since_cursor=cursor,
            )
        except InvalidInboxCursorError:
            _check(True, f"local CLI client: explicit {label} token fails loud")
        else:
            _check(False, f"local CLI client: explicit {label} token fails loud")


class _CapturingForwarder(Forwarder):
    def __init__(self) -> None:
        super().__init__(
            base_url="http://127.0.0.1:1",
            solet_name="example-test",
            agent_id=_AGENT_ID,
            agent_instance_id="agi-stdio",
            agent_session_id="ags-stdio",
            session_label="Stdio-Reader",
            parent_pid=123,
            provides_inference=False,
        )
        self._bridge_id = "agc-stdio"
        self.requested_path = ""

    async def _get(self, path: str) -> dict[str, Any]:
        self.requested_path = path
        return {}


def test_stdio_bridge_forwards_since() -> None:
    forwarder = _CapturingForwarder()
    try:
        asyncio.run(stdio_bridge._tool_peer_inbox(forwarder, {"since": _at(2), "limit": 5}))  # noqa: SLF001
    finally:
        asyncio.run(forwarder._client.aclose())  # noqa: SLF001
    _check(
        "since=" in forwarder.requested_path and "after=" not in forwarder.requested_path,
        "stdio bridge: the since argument reaches the /peer/inbox query",
    )
    forwarder = _CapturingForwarder()
    try:
        asyncio.run(
            stdio_bridge._tool_peer_inbox(  # noqa: SLF001
                forwarder,
                {"since_cursor": "opaque-token", "limit": 5},
            ),
        )
    finally:
        asyncio.run(forwarder._client.aclose())  # noqa: SLF001
    _check(
        "since_cursor=opaque-token" in forwarder.requested_path,
        "stdio bridge: the opaque continuation reaches the /peer/inbox query",
    )


def _tool_text(tools: list[Any]) -> tuple[str, dict[str, Any]]:
    entry = next(t for t in tools if (t["name"] if isinstance(t, dict) else t.name) == "peer_inbox")
    if isinstance(entry, dict):
        return entry["description"], entry["inputSchema"]["properties"]
    return entry.description, entry.inputSchema["properties"]


def test_advertised_text_matches_behavior() -> None:
    for label, tools in (("streamable", TOOLS), ("stdio", stdio_bridge.TOOLS)):
        description, props = _tool_text(list(tools))
        _check("since" in props, f"{label}: the tool schema offers the forward cursor")
        _check("since_cursor" in props, f"{label}: the tool schema offers the tie-safe continuation")
        _check("forward cursor; omit only for the first page" not in props["after"]["description"], f"{label}: after is no longer advertised as the forward cursor")
        _check("instance section is oldest-first" not in description, f"{label}: the description no longer calls the instance section oldest-first")
        _check("BACKWARD" in props["after"]["description"] and "FORWARD" in props["since"]["description"], f"{label}: each cursor names its direction")
        _check("next_since_cursor" in props["since"]["description"], f"{label}: the forward horizon points to the lossless continuation")


def main() -> None:
    print("=== peer_inbox since surfaces smoke ===")
    test_cov_base_red()
    test_cov_surfaces()
    test_cov_roles()
    test_route()
    test_streamable_dispatch()
    test_invalid_since_cursor_is_rejected_on_every_surface()
    test_local_cli_client_rejects_present_empty_since_cursor()
    test_stdio_bridge_forwards_since()
    test_advertised_text_matches_behavior()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
