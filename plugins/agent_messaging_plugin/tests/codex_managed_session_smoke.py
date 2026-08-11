#!/usr/bin/env python3
"""Offline smoke for managed Codex runtime parity.

Named mutations this suite must catch:

* collapsing runtime into host (``codex-headless``) instead of preserving the
  ``(agent_runtime, host)`` tuple;
* restart defaulting a Codex row back to Claude;
* silently accepting a Claude provider/provider_env overlay on Codex;
* treating app-server ``/clear`` as a new managed-session identity;
* tmux sending text+Enter without styled-pane pickup verification;
* watch registration claiming the session label as a role.

All fakes are records-only and return the real envelope/result shapes consumed
by production code.  No Codex model turn, tmux server, bridge, or database is
used.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.codex_adapter import (  # noqa: E402
    CodexAppServerHostDriver,
    CodexTmuxHostDriver,
    _CodexAppServerClient,
    _CodexTmuxDriverChannel,
    _identity_env,
)
from agent_messaging_plugin.local_cli.cli import (  # noqa: E402
    WatchIdentity,
    _register_without_claim,
)
from agent_messaging_plugin.plugin import (  # noqa: E402
    AgentMessagingPlugin,
    _spawn_session_request_from_params,
)
from agent_messaging_plugin.schema import get_managed_session_schema  # noqa: E402
from agent_messaging_plugin.session_hosts import (  # noqa: E402
    DriverChannelSendError,
    HostCannotSpawnError,
    resolve_host_driver,
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


def _executable(path: Path, name: str) -> str:
    target = path / name
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    return str(target)


def _codex_home(path: Path, *, plugin_enabled: bool = True) -> Path:
    home = path / "codex-home"
    home.mkdir()
    enabled = "true" if plugin_enabled else "false"
    (home / "config.toml").write_text(
        "[mcp_servers.testhom]\n"
        "command = \"homunculus\"\n"
        "[plugins.\"coordination-hooks@testhom-development\"]\n"
        f"enabled = {enabled}\n",
    )
    return home


class _FakeClient:
    instances: list[_FakeClient] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.pid = 4242
        self.started = False
        self.closed = False
        self.sent: list[str] = []
        self.__class__.instances.append(self)

    def start(self) -> None:
        self.started = True

    def alive(self) -> bool:
        return self.started and not self.closed

    def send(self, text: str) -> None:
        self.sent.append(text)

    def close(self, _grace_seconds: float) -> None:
        self.closed = True


def test_runtime_registry_is_orthogonal() -> None:
    headless, headless_name = resolve_host_driver("headless", "codex")
    tmux, tmux_name = resolve_host_driver("tmux", "codex")
    _check(
        isinstance(headless, CodexAppServerHostDriver) and headless_name == "headless",
        "agent_runtime=codex + host=headless resolves the app-server driver",
    )
    _check(
        isinstance(tmux, CodexTmuxHostDriver) and tmux_name == "tmux",
        "agent_runtime=codex + host=tmux resolves the interactive driver",
    )


def test_runtime_is_schema_and_restart_sticky() -> None:
    column = get_managed_session_schema().columns["agent_runtime"]
    _check(
        column.default == "claude_code" and column.not_null is not True,
        "managed_session.agent_runtime is declarative nullable TEXT with claude_code default",
    )
    request = _spawn_session_request_from_params(
        {
            "role_class": "ephemeral",
            "lane_id": "lane-codex",
            "brief_ref": "brief.md",
            "work_class": "read_only",
            "budget_line": "budget",
            "agent_runtime": "codex",
        },
        "operator:test",
    )
    _check(request.agent_runtime == "codex", "spawn transport preserves agent_runtime=codex")
    defaulted = _spawn_session_request_from_params(
        {
            "role_class": "ephemeral",
            "lane_id": "lane-claude",
            "brief_ref": "brief.md",
            "work_class": "read_only",
            "budget_line": "budget",
        },
        "operator:test",
    )
    _check(
        defaulted.agent_runtime == "claude_code",
        "omitted runtime keeps backward-compatible claude_code behavior",
    )
    params = AgentMessagingPlugin()._build_restart_spawn_params(  # noqa: SLF001
        {
            "brief_ref": "brief.md",
            "work_class": "read_only",
            "budget_line": "budget",
            "agent_runtime": "codex",
            "host": "headless",
        },
        "ephemeral",
        "lane-codex",
        "",
    )
    _check(
        params["agent_runtime"] == "codex",
        "restart carries codex runtime instead of silently respawning Claude",
    )


def test_headless_spawn_uses_codex_native_config_and_identity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _FakeClient.instances.clear()
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            homunculus_bin=_executable(root, "homunculus"),
            homunculus_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            client_factory=_FakeClient,
        )
        host_ref = driver.spawn(
            {
                "agent_instance_id": "agi-codex-1",
                "lane_id": "cheap-review",
                "model": "gpt-5.6-luna",
                "effort": "low",
                "transport": "mcp",
                "role_class": "ephemeral",
                "brief_ref": "brief.md",
                "spawned_by_role": "Coordinator-Main",
            },
        )
        client = _FakeClient.instances[-1]
        argv = client.kwargs["argv"]
        env = client.kwargs["env"]
        _check(host_ref == "4242" and client.started, "headless spawn starts one persistent client")
        _check(
            "app-server" in argv and "--dangerously-bypass-hook-trust" in argv,
            "headless Codex launches persistent app-server with managed hook trust",
        )
        _check(
            any("model_reasoning_effort=\"low\"" == item for item in argv),
            "Codex effort is applied through Codex's own config surface",
        )
        _check(
            any(
                'mcp_servers.testhom.env.AGENT_INSTANCE_ID="agi-codex-1"' == item
                for item in argv
            ),
            "MCP bridge identity is overridden per managed spawn",
        )
        _check(
            any(
                'mcp_servers.testhom.env.AGENT_ROLE_AUTOBIND="0"' == item
                for item in argv
            ),
            "managed MCP registration preserves label without binding it as a role",
        )
        _check(
            env["AGENT_IDENTITY"] == "codex"
            and env["AGENT_SESSION_ID"] == "ases-agi-codex-1"
            and "AGENT_ROLE" not in env,
            "process env uses Codex identity and grants no role at launch",
        )
        _check(
            client.kwargs["model"] == "gpt-5.6-luna",
            "cheaper Codex model selection reaches thread/start",
        )


def test_codex_provider_overlay_is_refused_loud() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            homunculus_bin=_executable(root, "homunculus"),
            homunculus_name="testhom",
            codex_home=_codex_home(root),
            cwd=root,
            client_factory=_FakeClient,
        )
        raised = False
        try:
            driver.spawn(
                {
                    "agent_instance_id": "agi-provider-refuse",
                    "transport": "mcp",
                    "provider_env": {"CLAUDE_CODE_USE_BEDROCK": "1"},
                },
            )
        except HostCannotSpawnError as exc:
            raised = True
            _check(
                "provider_unsupported_for_runtime" in str(exc),
                "provider refusal exposes the stable error token",
            )
        _check(raised, "Codex never silently ignores a Claude provider overlay")


def test_codex_environment_does_not_adopt_parent_runtime_or_provider() -> None:
    with patch.dict(
        os.environ,
        {
            "CODEX_THREAD_ID": "parent-thread",
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_AUTH_TOKEN": "secret-never-forward",
            "OPENAI_API_KEY": "codex-auth-remains-available",
        },
        clear=True,
    ):
        env = _identity_env(
            agent_instance_id="agi-child",
            agent_session_id="ases-child",
            label="child",
            homunculus_name="testhom",
            transport="mcp",
        )
    _check(
        "CODEX_THREAD_ID" not in env,
        "managed Codex never adopts the operator's parent thread identity",
    )
    _check(
        "CLAUDE_CODE_USE_BEDROCK" not in env and "ANTHROPIC_AUTH_TOKEN" not in env,
        "managed Codex receives no inherited Claude provider overlay or secret",
    )
    _check(
        env["OPENAI_API_KEY"] == "codex-auth-remains-available",
        "Codex-native authentication remains available after provider isolation",
    )


def test_verify_config_fails_loud_without_coordination_plugin() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        driver = CodexAppServerHostDriver(
            codex_bin=_executable(root, "codex"),
            homunculus_bin=_executable(root, "homunculus"),
            homunculus_name="testhom",
            codex_home=_codex_home(root, plugin_enabled=False),
            cwd=root,
        )
        remedies = driver.verify_config(transport="mcp")
        _check(
            any("coordination-hooks" in remedy for remedy in remedies),
            "missing/disabled Codex coordination hooks fail loud with a remedy",
        )


def test_app_server_channel_translates_clear_compact_and_active_turn() -> None:
    client = _CodexAppServerClient(
        argv=["codex"],
        cwd=Path("/tmp"),
        env={},
        developer_instructions="authority",
        model="gpt-5.6-luna",
    )
    calls: list[tuple[str, dict[str, object]]] = []
    next_thread = iter(("thread-after-clear",))

    def fake_request(method: str, params: Any) -> dict[str, Any]:
        calls.append((method, dict(params)))
        if method == "thread/start":
            return {"thread": {"id": next(next_thread)}}
        return {}

    client._request = fake_request  # type: ignore[method-assign]  # noqa: SLF001
    client._thread_id = "thread-original"  # noqa: SLF001
    client.send("/clear")
    _check(
        client._thread_id == "thread-after-clear",  # noqa: SLF001
        "/clear rotates only the Codex backend thread inside the same channel object",
    )
    client.send("/compact")
    _check(
        calls[-1] == ("thread/compact/start", {"threadId": "thread-after-clear"}),
        "/compact maps to Codex thread/compact/start",
    )
    client._active_turn_id = "turn-active"  # noqa: SLF001
    client.send("follow-up")
    _check(
        calls[-1][0] == "turn/steer"
        and calls[-1][1]["expectedTurnId"] == "turn-active",
        "a Stop-hook-active Codex turn receives explicit work via turn/steer",
    )


class _PaneRun:
    def __init__(self, captures: list[str]) -> None:
        self.captures = captures
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], **_kwargs: Any) -> Any:
        self.calls.append(argv)
        if "capture-pane" in argv:
            value = self.captures.pop(0) if len(self.captures) > 1 else self.captures[0]
            return SimpleNamespace(returncode=0, stdout=value, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        self.value += 0.1
        return self.value

    def sleep(self, _seconds: float) -> None:
        self.value += 0.1


def test_tmux_channel_verifies_styled_pickup() -> None:
    ready = "OpenAI Codex\n› review this"
    run = _PaneRun([ready, ready, ready, "OpenAI Codex\nWorking\n›"])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-test",
        run_fn=run,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        stable_samples=1,
        verify_timeout_seconds=2.0,
    )
    channel.send("review this")
    literal_index = next(i for i, call in enumerate(run.calls) if "-l" in call)
    enter_index = next(i for i, call in enumerate(run.calls) if call[-1] == "Enter")
    _check(literal_index < enter_index, "tmux text and Enter are separate ordered operations")
    _check(
        any("capture-pane" in call and "-e" in call for call in run.calls),
        "tmux pickup verification uses styled capture-pane -e",
    )


def test_tmux_channel_refuses_two_enter_noops() -> None:
    run = _PaneRun(["OpenAI Codex\n› dim-ghost"])
    clock = _Clock()
    channel = _CodexTmuxDriverChannel(
        tmux_bin="tmux",
        session="codex-noop",
        run_fn=run,
        sleep_fn=clock.sleep,
        now_fn=clock.now,
        stable_samples=1,
        verify_timeout_seconds=0.5,
    )
    raised = False
    try:
        channel.send("stranded")
    except DriverChannelSendError:
        raised = True
    enters = [call for call in run.calls if call[-1] == "Enter"]
    _check(len(enters) == 2, "an Enter no-op gets one separate retry, never an unbounded loop")
    _check(raised, "two Enter no-ops fail loud instead of calling ghost text delivered")


class _RegisterOnlyClient:
    def __init__(self) -> None:
        self.registered: list[dict[str, str]] = []

    def peer_register(self, **kwargs: str) -> dict[str, object]:
        self.registered.append(kwargs)
        return {"registered": True}

    def peer_claim_role(self, **_kwargs: object) -> dict[str, object]:
        raise AssertionError("--no-claim must not call peer_claim_role")


def test_watch_registration_does_not_claim_label() -> None:
    client = _RegisterOnlyClient()
    result = _register_without_claim(
        client,  # type: ignore[arg-type]
        WatchIdentity(
            role="lane-label-not-a-role",
            agent_id="codex",
            agent_session_id="ases-agi-codex-watch",
            agent_instance_id="agi-watch-codex",
        ),
    )
    _check(len(client.registered) == 1, "--no-claim still registers durable presence")
    _check(
        result == {"claimed": False, "reason": "managed_registration_only"},
        "--no-claim returns an explicit non-claim result",
    )


def main() -> int:
    tests = [
        test_runtime_registry_is_orthogonal,
        test_runtime_is_schema_and_restart_sticky,
        test_headless_spawn_uses_codex_native_config_and_identity,
        test_codex_provider_overlay_is_refused_loud,
        test_codex_environment_does_not_adopt_parent_runtime_or_provider,
        test_verify_config_fails_loud_without_coordination_plugin,
        test_app_server_channel_translates_clear_compact_and_active_turn,
        test_tmux_channel_verifies_styled_pickup,
        test_tmux_channel_refuses_two_enter_noops,
        test_watch_registration_does_not_claim_label,
    ]
    for test in tests:
        test()
    print(f"\nPASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
