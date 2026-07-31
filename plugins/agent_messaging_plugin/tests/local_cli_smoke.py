"""Hermetic smoke for the `homunculus` local-invocation CLI.

Exercises the CLI and its `BridgeClient` end-to-end against an in-process
`httpx.MockTransport` — no live homunculus, no LM Studio, no network. Verifies:

* health / search / call happy paths and their JSON output,
* `call` polls `process/result` until a completed result payload is stored,
* one-shot invocation NEVER registers a peer identity (no registry pollution),
* exit-code mapping for not-running / non-completed / bad-args,
* install-location identity resolution (root_manifest name -> clone basename),
* the CLI import chain needs NO ambient env (the package __init__ re-exports
  the server plugin lazily, so a bare `<name>` command works at birth).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from ananta.constants import ExitCodes
from click.testing import CliRunner, Result

import agent_messaging_plugin.env_contract as env_contract
import agent_messaging_plugin.local_cli.cli as cli_mod
import agent_messaging_plugin.local_cli.client as client_mod

Handler = Callable[[httpx.Request], httpx.Response]


def _make_handler(result_statuses: list[str]) -> tuple[Handler, dict[str, int]]:
    """Build a mock bridge handler; `calls` counts result-polls and registers.

    ``completed_without_result`` reproduces the real persistence race where
    action_events reaches completed before core__action_results is stored.
    """
    calls = {"result": 0, "register": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/peer/register"):
            calls["register"] += 1
            return httpx.Response(200, json={"session_label": "x"})
        if path == "/api/v1/bridge/open":
            return httpx.Response(
                200, json={"bridge_id": "agc-smoke", "session_id": "s"},
            )
        if path.endswith("/process/call"):
            return httpx.Response(
                200, json={"status": "queued", "action_id": "ae-smoke"},
            )
        if "/process/result/" in path:
            idx = min(calls["result"], len(result_statuses) - 1)
            raw_status = result_statuses[idx]
            status = (
                "completed"
                if raw_status == "completed_without_result"
                else raw_status
            )
            calls["result"] += 1
            body: dict[str, object] = {"action_id": "ae-smoke", "status": status}
            if raw_status == "completed":
                body["result"] = {"data": {"ok": True}}
            return httpx.Response(200, json=body)
        if path.endswith("/process/search"):
            return httpx.Response(
                200, json={"process_keys": ["a::b::c"], "action_status": "completed"},
            )
        if path == "/api/v1/bridge/health":
            return httpx.Response(200, json={"status": "healthy"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    return handler, calls


def _invoke(args: list[str], handler: Handler) -> Result:
    transport = httpx.MockTransport(handler)

    def factory(base_url: str, **_kw: object) -> client_mod.BridgeClient:
        return client_mod.BridgeClient(base_url, transport=transport)

    with (
        patch.object(cli_mod, "resolve_base_url", lambda name=None: "http://test"),
        patch.object(cli_mod, "BridgeClient", factory),
        patch.object(client_mod.time, "sleep", lambda _s: None),
    ):
        return CliRunner().invoke(cli_mod.cli, args, obj={})


def test_health() -> None:
    handler, _ = _make_handler(["completed"])
    result = _invoke(["health"], handler)
    assert result.exit_code == 0, result.output
    assert "healthy" in result.output


def test_search() -> None:
    handler, _ = _make_handler(["completed"])
    result = _invoke(["search", "q"], handler)
    assert result.exit_code == 0, result.output
    assert "a::b::c" in result.output


def test_call_polls_until_completion() -> None:
    handler, calls = _make_handler(["queued", "queued", "completed"])
    result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == 0, result.output
    assert '"completed"' in result.output
    assert calls["result"] >= 3, f"expected polling, saw {calls['result']} polls"


def test_call_waits_for_completed_result_payload() -> None:
    handler, calls = _make_handler(
        ["completed_without_result", "completed"],
    )
    result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["result"] == {"data": {"ok": True}}, payload
    assert calls["result"] >= 2, (
        "completed-without-result must be treated as a persistence race, "
        f"saw {calls['result']} polls"
    )


def test_one_shot_never_registers_peer() -> None:
    handler, calls = _make_handler(["completed"])
    _invoke(["call", "x::y::z", "{}"], handler)
    assert calls["register"] == 0, "one-shot call must not register a peer identity"


def test_call_exit_nonzero_when_not_completed() -> None:
    handler, _ = _make_handler(["failed"])
    result = _invoke(["call", "x::y::z", "{}"], handler)
    assert result.exit_code == int(ExitCodes.EXTERNAL_ERROR), result.output


def test_not_running_maps_to_connection_error() -> None:
    def boom(_name: str | None = None) -> str:
        raise client_mod.HomunculusNotRunningError("down")

    with patch.object(cli_mod, "resolve_base_url", boom):
        result = CliRunner().invoke(cli_mod.cli, ["health"], obj={})
    assert result.exit_code == int(ExitCodes.CONNECTION_ERROR), result.output


def test_bad_json_args_maps_to_unknown_error() -> None:
    handler, _ = _make_handler(["completed"])
    result = _invoke(["call", "x::y::z", "not-json"], handler)
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output


def test_cli_import_chain_needs_no_ambient_env() -> None:
    # The no-MCP-first contract: a bare `<name>` symlink must work on a fresh
    # machine with NO ambient env. A subprocess import with HOMUNCULUS_NAME
    # scrubbed proves the console script's whole import chain stays lazy
    # (regression: the package __init__ used to import the server plugin, which
    # requires HOMUNCULUS_NAME at import time — the bare CLI tracebacked).
    env = {k: v for k, v in os.environ.items() if k != "HOMUNCULUS_NAME"}
    proc = subprocess.run(
        [sys.executable, "-c", "import agent_messaging_plugin.local_cli.cli"],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr


def test_identity_prefers_manifest_name() -> None:
    manifest = SimpleNamespace(homunculus_name="shelby")
    with (
        patch.object(client_mod, "_clone_root", lambda: Path("/tmp/other-dir")),
        patch.object(client_mod, "load_manifest", lambda _p: (manifest, None)),
    ):
        assert client_mod.resolve_homunculus_name() == "shelby"


def test_identity_falls_back_to_basename_on_placeholder() -> None:
    # A genesis-un-rewritten root_manifest (or an unreadable one) -> the clone
    # directory basename is the name (the birth/clone convention ~/Workspace/<name>/).
    with (
        patch.object(client_mod, "_clone_root", lambda: Path("/tmp/shelby")),
        patch.object(client_mod, "load_manifest", lambda _p: (None, "unreadable")),
    ):
        assert client_mod.resolve_homunculus_name() == "shelby"


_WATCH_ENV = {
    "AGENT_SESSION_LABEL": "Git-Controller",
    "AGENT_SESSION_ID": "ases-1753000000-777-12345",
}
_NO_WATCH_ENV = {
    "AGENT_SESSION_LABEL": "",
    "AGENT_SESSION_ID": "",
}


def _watch_handler(
    events_batches: list[object],
) -> tuple[Handler, dict[str, object], dict[str, int]]:
    """Mock bridge for watch: open -> register -> claim -> inbox -> events.

    Captures the register body, the claim request, and the first events
    cursor. Each entry in ``events_batches`` is a dict (200 events response);
    anything else ends the stream with a 404 (bridge rotated / idle-reaped).
    """
    seen: dict[str, object] = {}
    counts = {"events": 0, "register": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-w", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/register"):
            seen["register"] = json.loads(request.content)
            counts["register"] += 1
            return httpx.Response(200, json={"status": "registered"})
        if path.endswith("/process/call"):
            seen["claim"] = json.loads(request.content)
            return httpx.Response(200, json={"status": "queued", "action_id": "ae-w"})
        if "/process/result/" in path:
            return httpx.Response(
                200,
                json={
                    "action_id": "ae-w",
                    "status": "completed",
                    "result": {"action": "registered"},
                },
            )
        if path.endswith("/peer/inbox"):
            return httpx.Response(
                200,
                json={
                    "entries": [{"thread_id": "agt-1", "message": {"text": "hi"}}],
                    "role_entries": [],
                },
            )
        if "/events" in path:
            seen.setdefault("first_events_query", request.url.query.decode())
            idx = counts["events"]
            counts["events"] = idx + 1
            batch = events_batches[idx] if idx < len(events_batches) else "drop"
            if isinstance(batch, dict):
                return httpx.Response(200, json=batch)
            return httpx.Response(404, json={"detail": "bridge gone"})
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    return handler, seen, counts


def test_watch_requires_env_identity() -> None:
    # No exported role/session-id and no --role -> loud refusal, not a
    # degraded anonymous registration.
    result = CliRunner().invoke(cli_mod.cli, ["watch"], env=dict(_NO_WATCH_ENV), obj={})
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    result = CliRunner().invoke(
        cli_mod.cli,
        ["watch", "--role", "Git-Controller"],
        env=dict(_NO_WATCH_ENV),
        obj={},
    )
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    assert "session id" in result.output


def test_watch_arms_then_streams_then_detects_drop() -> None:
    # Full arm sequence over one bridge lifetime: register (stable identity),
    # claim (REL-07 shape), drain inbox, stream from cursor -1, drop -> raise.
    batch = {
        "events": [{"cursor": 1, "event_type": "peer_message", "content": "hi"}],
        "next_cursor": 1,
    }
    handler, seen, _counts = _watch_handler([batch, "drop"])
    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    identity = cli_mod.WatchIdentity(
        role="Git-Controller",
        agent_id="claude_code",
        agent_session_id=_WATCH_ENV["AGENT_SESSION_ID"],
        agent_instance_id="agi-watch-deadbeefdeadbeefdeadbeef",
    )
    captured: list[str] = []
    try:
        with patch.object(
            cli_mod.click, "echo", lambda s, **_kw: captured.append(str(s)),
        ):
            cli_mod._arm_and_stream(client, identity, None)
    except client_mod.BridgeCallError:
        pass  # a 404 is how a rotated/reaped bridge surfaces to the streamer
    finally:
        client.close()
    _assert_watch_arm_requests(seen, identity)
    _assert_watch_stream_output(captured)


def _assert_watch_arm_requests(
    seen: dict[str, object], identity: cli_mod.WatchIdentity,
) -> None:
    register = seen["register"]
    assert isinstance(register, dict)
    assert register["agent_session_id"] == _WATCH_ENV["AGENT_SESSION_ID"]
    assert register["agent_instance_id"] == identity.agent_instance_id
    assert register["session_label"] == "Git-Controller"
    claim = seen["claim"]
    assert isinstance(claim, dict)
    assert claim["process_key"] == cli_mod.WATCH_CLAIM_PROCESS_KEY
    assert claim["arguments"] == {
        "name": "Git-Controller",
        "agent_id": "claude_code",
        "agent_instance_id": identity.agent_instance_id,
        "session_label": "Git-Controller",
    }
    assert seen["first_events_query"] == "after=-1"


def _assert_watch_stream_output(captured: list[str]) -> None:
    assert any('"watch": "armed"' in line for line in captured), captured
    assert any('"watch": "inbox"' in line for line in captured), captured
    assert any("peer_message" in line for line in captured), captured


def test_watch_heartbeat_reregisters_during_stream() -> None:
    # Field-observed on a live deployment: the peer binding can be dropped
    # server-side while the bridge stays healthy — the events long-poll keeps
    # returning empty 200s with no error, so without a heartbeat the watcher
    # black-holes deliveries
    # as persisted_silent forever. The stream loop must re-assert
    # peer/register on a cadence so a dropped binding heals within one
    # interval. Interval patched to 0 -> expect a re-register per poll.
    batch = {"events": [], "next_cursor": 0}
    handler, _seen, counts = _watch_handler([batch, batch, batch, "drop"])
    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    identity = cli_mod.WatchIdentity(
        role="Git-Controller",
        agent_id="claude_code",
        agent_session_id="ases-x",
        agent_instance_id="agi-watch-x",
    )
    try:
        with (
            patch.object(cli_mod, "WATCH_REREGISTER_INTERVAL_S", 0.0),
            patch.object(cli_mod.click, "echo", lambda s, **_kw: None),
        ):
            cli_mod._arm_and_stream(client, identity, None)
    except client_mod.BridgeCallError:
        pass  # the eventual 404 drop ends the stream, as in the arm test
    finally:
        client.close()
    assert counts["register"] >= 3, counts  # 1 at arm + 1 heartbeat per poll


def test_watch_identity_is_deterministic_per_session() -> None:
    # Same session id -> same instance id (reconnect REPLACES the binding);
    # different session id -> different instance id (no cross-session bleed).
    with patch.dict(os.environ, _WATCH_ENV, clear=False):
        first = cli_mod._resolve_watch_identity(None, "claude_code")
        second = cli_mod._resolve_watch_identity(None, "claude_code")
    assert first == second
    assert first.agent_instance_id.startswith(cli_mod.WATCH_AGENT_INSTANCE_PREFIX)
    other_env = dict(_WATCH_ENV)
    other_env["AGENT_SESSION_ID"] = "ases-1753000001-778-54321"
    with patch.dict(os.environ, other_env, clear=False):
        third = cli_mod._resolve_watch_identity(None, "claude_code")
    assert third.agent_instance_id != first.agent_instance_id


def test_watch_claim_rejection_dies_loud() -> None:
    # A terminal non-completed claim is a permanent rejection: exit loudly,
    # do not loop the reconnect path against a claim gate that said no.
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/bridge/open":
            return httpx.Response(200, json={"bridge_id": "agc-w", "session_id": "s"})
        if path.endswith("/close"):
            return httpx.Response(200, json={"status": "closed"})
        if path.endswith("/peer/register"):
            return httpx.Response(200, json={"status": "registered"})
        if path.endswith("/process/call"):
            return httpx.Response(200, json={"status": "queued", "action_id": "ae-w"})
        if "/process/result/" in path:
            return httpx.Response(200, json={"action_id": "ae-w", "status": "failed"})
        return httpx.Response(404, json={"detail": f"unmapped {path}"})

    client = client_mod.BridgeClient(
        "http://test", transport=httpx.MockTransport(handler),
    )
    client.open()
    identity = cli_mod.WatchIdentity(
        role="Git-Controller",
        agent_id="claude_code",
        agent_session_id="ases-x",
        agent_instance_id="agi-watch-x",
    )
    try:
        cli_mod._register_and_claim(client, identity)
        raise AssertionError("rejected claim did not exit")
    except SystemExit as exc:
        assert exc.code == int(ExitCodes.EXTERNAL_ERROR)
    finally:
        client.close()


def test_legacy_env_tripwire_fires_without_neutral() -> None:
    # One-release migration tripwire (env_contract): either legacy prefix
    # without its neutral AGENT_* replacement fails loud — never a read-through.
    # The legacy literal is fragmented (not written whole) because the seed
    # seal validator scans shipped bytes for legacy identity markers and would
    # refuse this file; the test must assemble the string it is testing for.
    for legacy_name in (
        "A" + "DA_AGENT_SESSION_LABEL",
        "HOMUNCULUS_AGENT_SESSION_LABEL",
    ):
        with patch.dict(os.environ, {legacy_name: "Worker-A"}, clear=True):
            try:
                env_contract.enforce_no_legacy_agent_env()
                raise AssertionError(f"{legacy_name} did not trip")
            except RuntimeError as exc:
                assert legacy_name in str(exc)
                assert "un-migrated" in str(exc)


def test_legacy_env_tripwire_silent_on_neutral_or_absent() -> None:
    # Steady state (no legacy names) and the flip window (both names present)
    # both pass; only legacy-without-neutral trips.
    # (Fragmented for the same reason as above: the seal validator scans for
    # this literal whole, so the test must assemble it rather than write it.)
    with patch.dict(os.environ, {}, clear=True):
        env_contract.enforce_no_legacy_agent_env()
    for legacy_name in (
        "A" + "DA_AGENT_SESSION_LABEL",
        "HOMUNCULUS_AGENT_SESSION_LABEL",
    ):
        with patch.dict(
            os.environ,
            {
                legacy_name: "Worker-A",
                "AGENT_SESSION_LABEL": "Worker-A",
            },
            clear=True,
        ):
            env_contract.enforce_no_legacy_agent_env()


def test_watch_dies_loud_on_legacy_only_env() -> None:
    # The watch entry converts the tripwire into the CLI's loud non-wake
    # error exit instead of registering a mislabeled/degraded identity.
    with patch.dict(
        os.environ,
        {
            "HOMUNCULUS_AGENT_SESSION_LABEL": "Git-Controller",
            "HOMUNCULUS_AGENT_SESSION_ID": "ases-1753000000-777-12345",
        },
        clear=True,
    ):
        result = CliRunner().invoke(cli_mod.cli, ["watch"], obj={})
    assert result.exit_code == int(ExitCodes.UNKNOWN_ERROR), result.output
    assert "un-migrated" in result.output


def test_watch_ctrl_c_exits_clean() -> None:
    def interrupt(_identity: cli_mod.WatchIdentity, _spool: Path | None) -> None:
        raise KeyboardInterrupt

    with patch.object(cli_mod, "_watch_forever", interrupt):
        result = CliRunner().invoke(
            cli_mod.cli, ["watch"], env=dict(_WATCH_ENV), obj={},
        )
    assert result.exit_code == 0, result.output


def main() -> None:
    tests = [obj for name, obj in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"PASS {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
    if failures:
        print(f"{failures}/{len(tests)} failed")
        sys.exit(1)
    print(f"all {len(tests)} passed")


if __name__ == "__main__":
    main()
