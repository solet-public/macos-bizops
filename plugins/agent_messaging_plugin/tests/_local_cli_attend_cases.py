"""HTTP transport cases for attend and inbox in the registered local CLI smoke.

The bridge is a fake OAuth + Streamable HTTP double behind
``httpx.MockTransport``; the click command, its token/initialize/identity
flow, SSE parsing and re-arm loop are real. The double reaches the command
only through objects that exist without the attend module
(``client.read_port_file`` and ``httpx.Client``), so importing this module
never imports ``local_cli.attend``: a tree without the command fails the
first assertion of ``test_attend_registers_with_authenticated_identity``
instead of an import. ``local_cli_smoke`` imports the ``test_attend_*``
routines and its ``main()`` collects them.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import signal
import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from ananta.constants import ExitCodes
from ananta.llm.agent_messaging.peer_inbox_cursor import encode_peer_inbox_since_cursor
from click.testing import CliRunner, Result

import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.client as client_mod

_STREAMABLE = "/api/v1/mcp/streamable"
_TOKEN = "/oauth/token"
_CLIENT_ID = "client-attend-fixture"
_SECRET = "s3cret-attend-fixture-value"
_SEALED_INSTANCE = "agi-oauth-client-attend-fixture"
_ENV_KEYS = ("AGENT_SESSION_ID", "AGENT_SESSION_LABEL", "AGENT_INSTANCE_ID")
_NO_LAUNCHER_ENV: dict[str, str | None] = dict.fromkeys(_ENV_KEYS)
_REAL_HTTPX_CLIENT = httpx.Client

_SEALED_IDENTITY: dict[str, Any] = {
    "transport": "streamable_http",
    "agent_id": "claude_phone",
    "agent_instance_id": _SEALED_INSTANCE,
    "agent_session_id": "ases-oauth-" + "0123456789abcdef" * 2,
    "session_label": "Attended Fixture",
    "identity_trust": "shared_oauth_bearer_verified",
    "streamable_no_auth": False,
}

_NOTIFICATION = {
    "jsonrpc": "2.0",
    "method": "notifications/claude/channel",
    "params": {"content": "hello attended", "meta": {"cursor": "1"}},
}
_ONE_EVENT_STREAM = f": heartbeat\n\nid: 1\nevent: message\ndata: {json.dumps(_NOTIFICATION)}\n\n"


class _AttendDouble:
    """One handler for /oauth/token and the Streamable endpoint; records all.

    ``token_script`` entries are ``None`` (issue ``tok-<n>``) or a canned
    response; past its end tokens are issued, unless ``strict_tokens`` makes
    any further token call an AssertionError. ``get_script`` entries are an
    SSE body or an exception to raise; an unscripted request of any kind is
    an AssertionError, so a broken command fails instead of spinning.
    """

    def __init__(
        self,
        *,
        get_script: list[str | BaseException],
        token_script: list[httpx.Response | None] | None = None,
        strict_tokens: bool = False,
        identity: Mapping[str, Any] | None = None,
    ) -> None:
        self.get_script = list(get_script)
        self.token_script = list(token_script or [])
        self.strict_tokens = strict_tokens
        self.identity = dict(_SEALED_IDENTITY if identity is None else identity)
        self.requests: list[dict[str, Any]] = []
        self.tokens_issued: list[str] = []
        self.token_calls = 0
        self.sessions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8", "replace")
        rpc = json.loads(body) if request.method == "POST" and request.url.path == _STREAMABLE else {}
        record: dict[str, Any] = {
            "method": request.method,
            "path": request.url.path,
            "url": str(request.url),
            "headers": dict(request.headers),
            "body": body,
            "rpc": rpc.get("method", ""),
        }
        if record["rpc"] == "initialize":
            record["env_seen"] = {key: os.environ.get(key) for key in _ENV_KEYS}
        self.requests.append(record)
        if request.url.path == _TOKEN:
            return self._token(request)
        if request.url.path != _STREAMABLE:
            raise AssertionError(f"unscripted request {request.method} {request.url.path}")
        if request.method == "POST":
            return self._post(rpc, request)
        if request.method == "GET":
            return self._get()
        if request.method == "DELETE":
            return httpx.Response(200)
        raise AssertionError(f"unscripted method {request.method}")

    def _token(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        scripted: httpx.Response | None = None
        if self.token_script:
            scripted = self.token_script.pop(0)
        elif self.strict_tokens:
            raise AssertionError(f"unscripted token call #{self.token_calls}")
        if scripted is not None:
            return scripted
        expected = "Basic " + base64.b64encode(f"{_CLIENT_ID}:{_SECRET}".encode()).decode()
        if request.headers.get("authorization") != expected:
            return httpx.Response(401, json={"error": "invalid_client"})
        token = f"tok-{len(self.tokens_issued) + 1}"
        self.tokens_issued.append(token)
        return httpx.Response(200, json={"access_token": token, "token_type": "Bearer"})

    def _post(self, rpc: dict[str, Any], request: httpx.Request) -> httpx.Response:
        method = rpc.get("method")
        if method == "initialize":
            self.sessions += 1
            session_id = f"mcp-{self.sessions}"
            result = {"protocolVersion": "2025-03-26", "capabilities": {}}
            return httpx.Response(
                200,
                json={"jsonrpc": "2.0", "id": rpc.get("id"), "result": result},
                headers={"Mcp-Session-Id": session_id},
            )
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/call" and rpc.get("params", {}).get("name") == "current_identity":
            payload = {**self.identity, "mcp_session_id": request.headers.get("mcp-session-id", "")}
            result = {"structuredContent": payload, "isError": False}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": rpc.get("id"), "result": result})
        raise AssertionError(f"unscripted JSON-RPC method {method!r}")

    def _get(self) -> httpx.Response:
        if not self.get_script:
            raise AssertionError("unscripted GET")
        step = self.get_script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return httpx.Response(200, content=step.encode(), headers={"content-type": "text/event-stream"})

    def of(self, *, method: str = "", rpc: str = "") -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == _STREAMABLE and (not method or r["method"] == method) and (not rpc or r["rpc"] == rpc)]


def _run_attend(
    double: _AttendDouble,
    *,
    env: Mapping[str, str | None] | None = None,
    extra_args: tuple[str, ...] = (),
    secret: str = _SECRET,
) -> Result:
    secret_path = Path(tempfile.mkdtemp(prefix="attend-smoke-")) / "client.secret"
    secret_path.write_text(secret + "\n", encoding="utf-8")
    transport = httpx.MockTransport(double)

    def client_factory(*args: Any, **kwargs: Any) -> httpx.Client:
        return _REAL_HTTPX_CLIENT(*args, transport=transport, **kwargs)

    previous_sigterm = signal.getsignal(signal.SIGTERM)
    args = ["attend", "--client-id", _CLIENT_ID, "--client-secret-file", str(secret_path), *extra_args]
    try:
        with (
            patch.object(client_mod, "read_port_file", lambda *_a, **_k: 18801),
            patch.object(httpx, "Client", client_factory),
            patch.object(client_mod.time, "sleep", lambda _s: None),
        ):
            return CliRunner().invoke(cli_mod.cli, args, env=dict(env or _NO_LAUNCHER_ENV), obj={})
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


def _lines(result: Result, kind: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for line in result.output.splitlines():
        if not line.startswith("{"):
            continue
        parsed = json.loads(line)
        if isinstance(parsed, dict) and parsed.get("attend") == kind:
            found.append(parsed)
    return found


def _wire_text(double: _AttendDouble) -> str:
    return "\n".join(f"{r['url']} {json.dumps(r['headers'])} {r['body']}" for r in double.requests)


def _assert_bearer_on_every_streamable_request(double: _AttendDouble) -> None:
    for request in double.of():
        auth = request["headers"].get("authorization", "")
        assert auth.removeprefix("Bearer ") in double.tokens_issued and auth.startswith("Bearer "), (
            "ATTEND_SENDS_BEARER",
            request["method"],
            request["rpc"],
            auth,
        )


def _assert_rearm_order(double: _AttendDouble) -> None:
    """Two arms: mcp-1 is DELETEd before initialize #2, mcp-2 is DELETEd last."""
    order = [(r["method"], r["rpc"], r["headers"].get("mcp-session-id", "")) for r in double.of()]
    initializes = [i for i, step in enumerate(order) if step[1] == "initialize"]
    assert len(initializes) == 2, order
    assert order.index(("DELETE", "", "mcp-1")) < initializes[1], order
    assert order[-1] == ("DELETE", "", "mcp-2"), order


def _assert_secret_never_leaks(result: Result, double: _AttendDouble) -> None:
    assert _SECRET not in result.output, result.output
    for request in double.requests:
        assert _SECRET not in request["url"] and _SECRET not in request["body"], request


def _assert_wire_free_of(double: _AttendDouble, values: tuple[str, ...]) -> None:
    wire = _wire_text(double)
    for value in values:
        assert value not in wire, (value, wire)


def test_attend_registers_with_authenticated_identity() -> None:
    """No launcher env: arm, one delivery, re-arm after stream end, DELETE at exit."""
    double = _AttendDouble(get_script=[_ONE_EVENT_STREAM, KeyboardInterrupt()])
    result = _run_attend(double)
    assert result.exit_code == 0, ("ATTEND_REGISTERS_AUTHENTICATED_IDENTITY", result.exit_code, result.output)
    armed = _lines(result, "armed")
    assert len(armed) == 2, armed
    assert armed[0]["agent_instance_id"] == _SEALED_INSTANCE, armed
    assert armed[0]["mcp_session_id"] == "mcp-1", armed
    assert [i["env_seen"] for i in double.of(rpc="initialize")] == [_NO_LAUNCHER_ENV] * 2, double.of()
    _assert_bearer_on_every_streamable_request(double)
    _assert_rearm_order(double)
    events = _lines(result, "event")
    assert events == [{"attend": "event", "params": _NOTIFICATION["params"]}], events
    _assert_secret_never_leaks(result, double)


def _assert_identity_flags_are_usage_errors() -> None:
    for flag in (("--agent-instance-id", "agi-self-asserted"), ("--role", "Git-Controller")):
        double = _AttendDouble(get_script=[])
        result = _run_attend(double, extra_args=flag)
        assert result.exit_code == 2, (flag, result.exit_code, result.output)
        assert "No such option" in result.output, result.output
        assert double.requests == [], double.requests


def test_attend_refuses_self_asserted_identity() -> None:
    """Identity flags do not exist; env identity with a refused credential gets nowhere."""
    _assert_identity_flags_are_usage_errors()
    self_asserted = {
        "AGENT_INSTANCE_ID": "agi-self-asserted-0000",
        "AGENT_SESSION_ID": "ases-self-asserted",
        "AGENT_SESSION_LABEL": "Self-Asserted",
    }
    refused = httpx.Response(401, json={"error": "invalid_client"})
    double = _AttendDouble(get_script=[], token_script=[refused])
    result = _run_attend(double, env=self_asserted)
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), (result.exit_code, result.output)
    assert "attend refused: the bridge did not issue a token" in result.output, result.output
    assert double.of() == [], double.of()
    _assert_wire_free_of(double, ("process_call", "peer_claim_role", *self_asserted.values()))
    empty = _run_attend(_AttendDouble(get_script=[]), secret="   ")
    assert empty.exit_code == int(ExitCodes.UNKNOWN_ERROR), (empty.exit_code, empty.output)
    assert "attend needs a non-empty client secret" in empty.output, empty.output


def test_attend_refuses_unauthenticated_bridge_identity() -> None:
    """A no-auth bridge session is not an identity: refuse, DELETE, never hold."""
    no_auth = {
        "agent_id": "tunnel_passthrough",
        "agent_instance_id": "tunnel_passthrough",
        "agent_session_id": "",
        "session_label": "streamable_no_auth",
        "identity_trust": "outer_boundary_only",
        "streamable_no_auth": True,
    }
    double = _AttendDouble(get_script=[KeyboardInterrupt()], identity=no_auth)
    result = _run_attend(double)
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), ("ATTEND_REFUSES_UNAUTHENTICATED_IDENTITY", result.output)
    assert "attend refused: the bridge did not authenticate this identity" in result.output, result.output
    assert len(double.of(method="DELETE")) == 1, double.of()
    assert double.of(method="GET") == [], double.of()
    assert _lines(result, "armed") == [], result.output


def test_attend_ignores_launcher_env_identity() -> None:
    """Launcher-shaped env is never read: the armed identity is the server's."""
    env = {"AGENT_INSTANCE_ID": "agi-self-asserted-1111", "AGENT_SESSION_ID": "ases-self-asserted-1111"}
    double = _AttendDouble(get_script=[KeyboardInterrupt()])
    result = _run_attend(double, env=env)
    armed = (_lines(result, "armed") or [{}])[0]
    assert armed.get("agent_instance_id") == _SEALED_INSTANCE, ("ATTEND_IDENTITY_FROM_SERVER", armed, result.output)
    assert result.exit_code == 0, (result.exit_code, result.output)
    wire = _wire_text(double)
    for value in env.values():
        assert value not in wire, (value, wire)


def test_attend_exits_when_client_revoked() -> None:
    """Re-arm: an unavailable token endpoint is retried; an OAuth refusal ends it."""
    unavailable = httpx.Response(503)
    revoked = httpx.Response(401, json={"error": "invalid_client"})
    # Token calls: arm #1, GET #1, DELETE mcp-1, re-arm (503), re-arm (401).
    double = _AttendDouble(
        get_script=[""],
        token_script=[None, None, None, unavailable, revoked],
        strict_tokens=True,
    )
    result = _run_attend(double)
    retries = _lines(result, "retry")
    assert len(retries) == 1 and double.token_calls == 5, (
        "ATTEND_RETRIES_UNAVAILABLE_REARM",
        retries,
        double.token_calls,
        result.output,
    )
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), (result.exit_code, result.output)
    assert "attend: the bridge no longer accepts client" in result.output, result.output
    assert len(double.of(rpc="initialize")) == 1, double.of()


def test_attend_wire_constants_match_server() -> None:
    """The client's copies of server wire constants equal the server symbols."""
    import agent_messaging_plugin.local_cli.attend as attend_mod
    from agent_messaging_plugin.mcp_streamable import dispatch, oauth, router, tools

    assert attend_mod.STREAMABLE_PATH == router.STREAMABLE_PATH, attend_mod.STREAMABLE_PATH
    assert attend_mod.TOKEN_PATH == oauth.OAUTH_TOKEN_PATH, attend_mod.TOKEN_PATH
    assert attend_mod.PROTOCOL_VERSION == tools.SUPPORTED_PROTOCOL_VERSION, attend_mod.PROTOCOL_VERSION
    # dispatch.py:983 spells it as a bare literal, not a constant; either quote style matches.
    source = inspect.getsource(dispatch._identity_trust)
    quoted = {q + attend_mod.OUTER_BOUNDARY_TRUST + q for q in ("'", '"')}
    assert any(literal in source for literal in quoted), (attend_mod.OUTER_BOUNDARY_TRUST, source)


class _RegistrationClock:
    """Advance only the inbox recovery clock; no wall-clock wait in the smoke.

    The CLI and HTTP client normally import the same time module. Replacing
    the CLI's module reference keeps its deadline deterministic without
    changing the process-result poller's clock. The existing invocation
    fixture already removes that poller's sleeps.

    A nonzero starting time catches code that treats the absolute monotonic
    reading as elapsed time. Recorded sleeps expose cadence and the shortened
    final interval independently of the number of HTTP requests.
    """

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_inbox_recovers_after_registration_gap() -> None:
    """The real parser retries identical GETs, then emits both sections before ACK.

    The first refusal models the startup gap. The second occurs with a
    nonempty role cursor, after a direct message and a role page were read.
    This catches implementations that retry only the command's first read,
    restart pagination, or accidentally reuse the direct cursor for role mail.

    The request allowlist excludes registry, claim and send endpoints. The
    only process calls that the fixture accepts acknowledge issued page tokens.
    Their timeline must follow both JSON emission and an explicit flush, so
    successful recovery cannot consume mail before the caller receives it.
    """
    from local_cli_smoke import _ack_pages_handler, _entry, _invoke, _role_addressed_inbox_page

    timeline: list[str] = []
    base, acks = _ack_pages_handler(timeline=timeline)
    requests: list[tuple[str, dict[str, str]]] = []
    queries: list[dict[str, str]] = []
    refused: set[str] = set()
    direct = _entry("direct-1", "2026-09-12T00:00:00")
    clock = _RegistrationClock()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/peer/inbox_for_session"):
            query = dict(request.url.params)
            queries.append(query)
            # Lose registration both at first read and on a later role cursor.
            key = str(request.url.query)
            if any((len(queries) == 1, query.get("role_after") == "r-1")) and key not in refused:
                refused.add(key)
                return httpx.Response(400, json={"code": "identity_not_registered", "message": "watcher is recovering; re-arm it if the gap persists"})
            if query.get("observer") == "true":
                page = _role_addressed_inbox_page()
                page["entries"] = [] if query.get("after") else [direct]
                page["next_after_created_at"] = direct["message"]["created_at"]
                return httpx.Response(200, json=page)
        return base(request)

    original_emit = cli_mod._emit
    with (
        patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-gap"}),
        patch.object(cli_mod, "time", clock),
        patch.object(cli_mod, "_emit", lambda value: (timeline.append("emit"), original_emit(value))[1]),
        patch.object(cli_mod, "_flush_inbox_output", lambda: timeline.append("flush")),
    ):
        result = _invoke(["inbox", "--limit", "1"], handler)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["messages"] == [
        {**direct, "section": "direct"},
        {**_entry("role-1", "2026-09-12T00:00:01"), "section": "role"},
        {**_entry("role-2", "2026-09-12T00:00:02"), "section": "role"},
    ], payload
    assert (payload["complete"], payload["direct_count"], payload["role_count"]) == (True, 1, 2)
    assert queries[0] == queries[1] == {"agent_session_id": "ases-gap", "limit": "1", "observer": "true"}
    assert queries[2]["after"] == "2026-09-12T00:00:00"
    assert queries[-2] == queries[-1] == {"agent_session_id": "ases-gap", "limit": "1", "role_after": "r-1"}
    assert all(path.endswith(("/open", "/close", "/peer/inbox_for_session", "/process/call", "/process/result/ae-0", "/process/result/ae-1")) for path, _ in requests), requests
    assert (acks, timeline) == (["page-0", "page-1"], ["emit", "flush", "ack:page-0", "ack:page-1"])
    assert (clock.sleeps, result.stderr.count("waiting briefly")) == ([cli_mod.WATCH_RECONNECT_DELAY_S] * 2, 1), result.stderr


def test_inbox_registration_deadline_preserves_refusal() -> None:
    """A later page shares the first gap's budget and retains its original remedy.

    Read one fails, read two succeeds with an empty direct section, and the
    independent role read then fails until expiry. A fresh budget per page
    would exceed the recorded total. A fresh refusal per attempt would lose
    the original actionable watcher and session-id instructions.

    The budget is deliberately not divisible by the reconnect cadence: its
    last sleep must be shortened and no request may start at the deadline.
    The successful intermediate read must not reset the clock or emit a
    partial result. No role page was displayed, so no acknowledgement is due.
    """
    from local_cli_smoke import _ack_pages_handler, _invoke, _role_addressed_inbox_page

    clock = _RegistrationClock()
    queries: list[dict[str, str]] = []
    base, acks = _ack_pages_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/peer/inbox_for_session"):
            query = dict(request.url.params)
            queries.append(query)
            if len(queries) == 2:
                return httpx.Response(200, json=_role_addressed_inbox_page())
            return httpx.Response(
                400,
                json={
                    "code": "identity_not_registered",
                    "message": "ORIGINAL: re-arm the existing watcher; check AGENT_SESSION_ID" if len(queries) == 1 else "later refusal",
                },
            )
        return base(request)

    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-expiry"}), patch.object(cli_mod, "time", clock):
        result = _invoke(["inbox"], handler)
    budget = cli_mod.WATCH_REREGISTER_INTERVAL_S + cli_mod.WATCH_REQUEST_TIMEOUT_S + cli_mod.WATCH_RECONNECT_DELAY_S
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), result.output
    assert "identity_not_registered" in result.stderr and "ORIGINAL: re-arm the existing watcher; check AGENT_SESSION_ID" in result.stderr
    assert "later refusal" not in result.stderr and result.stdout == ""
    assert sum(clock.sleeps) == budget and clock.monotonic() == 100.0 + budget
    assert len(queries) == 1 + len(clock.sleeps), queries
    assert queries[0] == queries[1] and all(q == queries[2] for q in queries[2:])
    assert acks == [] and result.stderr.count("waiting briefly") == 1


def test_inbox_registration_retry_preserves_horizon_and_continuation() -> None:
    """Observer, covered floor, horizon and opaque continuation survive each retry.

    Every distinct page is refused once before it succeeds. The direct walk
    starts with a fixed timestamp horizon, then uses a real encoded cursor.
    The role walk independently carries that horizon and the covered-floor
    override. Retrying must repeat each query byte-for-byte, including flags
    that differ between the two sections.

    This is an observer invocation: even a server that returns role tokens
    cannot authorize a display acknowledgement. A single advisory belongs on
    stderr, keeping stdout usable as one complete JSON result.
    """
    from local_cli_smoke import _ack_pages_handler, _entry, _invoke, _role_addressed_inbox_page

    horizon = "2026-09-12T00:00:00.000000"
    cursor = encode_peer_inbox_since_cursor(
        horizon=datetime.fromisoformat(horizon),
        after_created_at=datetime.fromisoformat("2026-09-12T00:00:01"),
        after_id="direct-horizon",
    )
    queries: list[dict[str, str]] = []
    clock = _RegistrationClock()
    base, acks = _ack_pages_handler()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/peer/inbox_for_session"):
            query = dict(request.url.params)
            queries.append(query)
            if len(queries) % 2:
                return httpx.Response(400, json={"code": "identity_not_registered", "message": "wait for watcher"})
            page = _role_addressed_inbox_page()
            page["role_since_created_at"] = horizon
            if "include_covered" not in query and "since_cursor" not in query:
                page["entries"] = [_entry("direct-horizon", "2026-09-12T00:00:01")]
                page["instance_exhausted"] = False
                page["next_since_cursor"] = cursor
            return httpx.Response(200, json=page)
        return base(request)

    with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-horizon"}), patch.object(cli_mod, "time", clock):
        result = _invoke(["inbox", "--observer", "--include-covered", "--since", horizon], handler)
    assert result.exit_code == 0, result.output
    common = {"agent_session_id": "ases-horizon", "limit": "100", "observer": "true"}
    assert queries == [
        {**common, "since": horizon},
        {**common, "since": horizon},
        {**common, "since_cursor": cursor},
        {**common, "since_cursor": cursor},
        {**common, "include_covered": "true", "since": horizon},
        {**common, "include_covered": "true", "since": horizon},
    ], queries
    payload = json.loads(result.stdout)
    assert payload["since_applied"] == {"direct": horizon, "role": horizon} and payload["complete"] is True
    assert acks == [] and result.stderr.count("waiting briefly") == 1


def test_other_refusals_are_not_retried() -> None:
    """Only a structured missing-binding code can wait; text is never authority.

    Ambiguous identity and malformed cursors require a caller correction.
    Neither a numeric code nor error text mentioning the retryable spelling
    supplies that structured code. A successful HTTP status with a list body
    is still a malformed page, not an empty inbox or a registration gap.

    Each case traverses the real parser and HTTP error decoder. The first
    refusal remains actionable in stderr; no case sleeps, emits a success
    payload, or acknowledges a role page that the caller never received.
    """
    from local_cli_smoke import Handler, _ack_pages_handler, _invoke

    responses = [
        httpx.Response(409, json={"code": "peer_session_ambiguous", "message": "resolve ambiguous identity"}),
        httpx.Response(400, json={"code": "malformed_cursor", "message": "invalid cursor"}),
        httpx.Response(400, json={"code": 123, "message": "identity_not_registered"}),
        httpx.Response(503, text="identity_not_registered: non-JSON transport failure"),
        httpx.Response(200, json=["not a page"]),
    ]
    for response in responses:
        clock = _RegistrationClock()
        reads: list[str] = []
        base, acks = _ack_pages_handler()

        def handler(
            request: httpx.Request,
            reads: list[str] = reads,
            response: httpx.Response = response,
            base: Handler = base,
        ) -> httpx.Response:
            if request.url.path.endswith("/peer/inbox_for_session"):
                reads.append(str(request.url))
                return response
            return base(request)

        with patch.dict(os.environ, {"AGENT_SESSION_ID": "ases-other"}), patch.object(cli_mod, "time", clock):
            result = _invoke(["inbox"], handler)
        assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), result.output
        assert len(reads) == 1 and clock.sleeps == [] and acks == []
        assert (response.text if response.status_code != 200 else "non-object body") in result.stderr
        assert "waiting briefly" not in result.stderr
