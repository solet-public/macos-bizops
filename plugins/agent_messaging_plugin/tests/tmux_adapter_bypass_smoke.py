#!/usr/bin/env python3
"""Bypass-permissions startup regressions for the tmux host driver.

All panes and tmux commands in this module are fakes. Shared driver fixtures
remain in tmux_adapter_smoke, whose real-tmux tier is not invoked here.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

import tmux_adapter_smoke as shared  # noqa: E402
from tmux_adapter_smoke import (  # noqa: E402
    _check,
    _configured_driver,
    _confirm_flow_run_fn,
    _effective_env_marker,
    _extract_claude_argv_from_pane_command,
    _FakeCompleted,
    _is_send_enter,
    _third_party_provider_run_fn,
)

from agent_messaging_plugin.channel_support import THIRD_PARTY_PROVIDER_ENV_MARKERS  # noqa: E402
from agent_messaging_plugin.session_hosts import DriverChannelSendError, HostCannotSpawnError  # noqa: E402
from agent_messaging_plugin.tmux_adapter import _pane_shows_dev_channels_prompt, _TmuxSendKeysDriverChannel  # noqa: E402


def test_spawn_confirms_bypass_then_dev_channels_prompt() -> None:
    """The first Enter must accept the selected Yes row, then confirm channels."""
    calls: list[list[str]] = []
    keys: list[str] = []

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "send-keys" in cmd and "-l" not in cmd:
            keys.append(cmd[-1])
        if "capture-pane" in cmd:
            if keys == ["Down", "Enter", "Enter"]:
                return _FakeCompleted(stdout="TUI booted, channels active\n")
            if keys == ["Down", "Enter"]:
                return _FakeCompleted(stdout="WARNING: Loading development channels\nEnter to confirm\n")
            return _FakeCompleted(
                stdout="WARNING: Claude Code running in Bypass Permissions mode\n❯ No, exit\nYes, I accept\nEnter to confirm · Esc to cancel\n",
            )
        return _FakeCompleted()

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=run_fn, confirm_timeout_seconds=0.05)
        host_ref = driver.spawn({"agent_instance_id": "agi-bypass01", "label": "lane-bypass"})
        _check(host_ref == "fleet-testhom-lane-bypass-bypass01", "bypass then channels returns the expected host reference")
        _check(keys == ["Down", "Enter", "Enter"], "bypass then channels sends exactly Down, Enter, Enter")
        _check(sum(_is_send_enter(cmd) for cmd in calls) == 2, "each dialogue receives one literal Enter")
        _check(not any("kill-session" in cmd for cmd in calls), "both confirmed dialogues preserve the session")


def test_provider_spawn_confirms_bypass_without_dev_channels() -> None:
    """R1: omitting development channels must retain bypass confirmation."""
    calls: list[list[str]] = []
    keys: list[str] = []

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "send-keys" in cmd and "-l" not in cmd:
            keys.append(cmd[-1])
        if "capture-pane" in cmd:
            if keys == ["Down", "Enter"]:
                return _FakeCompleted(stdout="TUI booted on provider\n")
            return _FakeCompleted(
                stdout="WARNING: Claude Code running in Bypass Permissions mode\nNo, exit\nYes, I accept\nEnter to confirm · Esc to cancel\n",
            )
        return _FakeCompleted()

    with tempfile.TemporaryDirectory() as tmp, _effective_env_marker("1"):
        driver = _configured_driver(Path(tmp), run_fn=run_fn, confirm_timeout_seconds=0.05)
        host_ref = driver.spawn({"agent_instance_id": "agi-provider-bypass", "transport": "watch"})
        launch = next(cmd for cmd in calls if "new-session" in cmd)
        argv = _extract_claude_argv_from_pane_command(launch[-1])
        _check("--dangerously-load-development-channels" not in argv, "R1 provider fixture omits development channels")
        _check(argv[argv.index("--permission-mode") + 1] == "bypassPermissions", "R1 provider fixture retains bypass mode")
        _check(bool(host_ref), "provider bypass confirmation returns a host reference without a dev dialogue")
        _check(keys == ["Down", "Enter"], "R1 provider bypass receives exactly Down, Enter without development channels")
        _check(not any("kill-session" in cmd for cmd in calls), "confirmed provider bypass preserves the session")


def test_provider_spawn_without_bypass_dialogue_returns_after_bounded_wait() -> None:
    """Already-accepted bypass mode need not display a dialogue at startup."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp, _effective_env_marker("1"):
        driver = _configured_driver(
            Path(tmp),
            run_fn=_third_party_provider_run_fn(calls),
            confirm_timeout_seconds=0.05,
            confirm_poll_interval_seconds=0.01,
        )
        host_ref = driver.spawn({"agent_instance_id": "agi-accepted-bypass", "transport": "watch"})
        _check(bool(host_ref), "an absent optional bypass dialogue still returns the provider session")
        _check(any("capture-pane" in cmd for cmd in calls), "bypass mode is inspected even when no dialogue appears")
        _check(not any("send-keys" in cmd for cmd in calls), "no acceptance keys are sent when the optional dialogue is absent")
        _check(not any("kill-session" in cmd for cmd in calls), "absence of an optional bypass dialogue never kills the session")


def test_spawn_dev_channels_without_bypass_sends_only_enter() -> None:
    """A non-bypass argv retains the original single-Enter confirmation."""
    calls: list[list[str]] = []
    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=_confirm_flow_run_fn(calls))
        host_ref = driver.spawn(
            {"agent_instance_id": "agi-devonly1", "permission_mode": "default"},
        )
        launch = next(cmd for cmd in calls if "new-session" in cmd)
        argv = _extract_claude_argv_from_pane_command(launch[-1])
        _check(argv[argv.index("--permission-mode") + 1] == "default", "the dev-only fixture has no bypass mode")
        sent = [cmd for cmd in calls if "send-keys" in cmd]
        _check(sent == [[driver._tmux_bin, "send-keys", "-t", f"={host_ref}:", "Enter"]], "dev-only path sends Enter to the exact pane and sends no Down")
        captures = [cmd for cmd in calls if "capture-pane" in cmd]
        _check(
            bool(captures) and all(cmd[cmd.index("-t") + 1] == f"={host_ref}:" for cmd in captures),
            "dev-only confirmation captures the exact pane",
        )
        _check(not any("kill-session" in cmd for cmd in calls), "dev-only confirmation returns without cleanup")


def test_spawn_refuses_when_bypass_prompt_never_clears() -> None:
    """A visible bypass dialogue that stays after confirmation is terminal."""
    calls: list[list[str]] = []

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        if "capture-pane" in cmd:
            return _FakeCompleted(
                stdout="WARNING: Claude Code running in Bypass Permissions mode\nNo, exit\nYes, I accept\nEnter to confirm · Esc to cancel\n",
            )
        return _FakeCompleted()

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(
            Path(tmp),
            run_fn=run_fn,
            confirm_timeout_seconds=0.05,
            confirm_poll_interval_seconds=0.01,
        )
        refused = False
        try:
            driver.spawn({"agent_instance_id": "agi-bypass02"})
        except HostCannotSpawnError as exc:
            refused = True
            _check("still showed the Bypass Permissions dialogue" in str(exc), "the refusal identifies the stuck bypass dialogue")
        _check(refused, "a bypass dialogue that never clears cannot return a host reference")
        _check([cmd[-1] for cmd in calls if "send-keys" in cmd] == ["Down", "Enter"], "the stuck bypass receives Down and Enter without a dev-channels Enter")
        _check(sum("kill-session" in cmd for cmd in calls) == 1, "the stuck fake session is cleaned up exactly once")


def test_dev_channels_prompt_requires_its_warning_marker() -> None:
    """Shared confirmation instructions cannot identify development channels."""
    _check(not _pane_shows_dev_channels_prompt("Enter to confirm · Esc to cancel"), "shared Enter instructions alone do not match development channels")
    _check(
        not _pane_shows_dev_channels_prompt("WARNING: Claude Code running in Bypass Permissions mode\nYes, I accept\nEnter to confirm · Esc to cancel"),
        "the bypass dialogue does not match development channels",
    )
    _check(_pane_shows_dev_channels_prompt("WARNING: Loading development channels\nEnter to confirm"), "the actual development-channels warning still matches")


def test_driver_channel_uses_exact_pane_targets() -> None:
    calls: list[list[str]] = []

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted()

    channel = _TmuxSendKeysDriverChannel(
        tmux_bin="tmux",
        session="named-session",
        run_fn=run_fn,
        stable_timeout_seconds=0,
    )
    channel.send("message")
    channel._capture_pane_colored()
    targeted = [cmd for cmd in calls if "-t" in cmd]
    _check(
        {cmd[1] for cmd in targeted} == {"paste-buffer", "capture-pane", "send-keys"},
        "the channel exercised paste, capture and send",
    )
    _check(
        all(cmd[cmd.index("-t") + 1] == "=named-session:" for cmd in targeted),
        "the channel uses a colon-terminated exact pane target for every targeted command",
    )
    failing_channel = _TmuxSendKeysDriverChannel(
        tmux_bin="tmux",
        session="named-session",
        run_fn=lambda *_args, **_kw: _FakeCompleted(returncode=1),
    )
    failure = ""
    try:
        failing_channel.insert("message")
    except DriverChannelSendError as exc:
        failure = str(exc)
    _check(
        failure == "tmux pane 'named-session' did not accept the composer insert.",
        "channel failure identifies the raw session name",
    )


def test_driver_host_operations_use_exact_targets() -> None:
    calls: list[list[str]] = []

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted()

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=run_fn)
        driver.alive("named-session")
        driver._capture_pane_text("named-session")
        driver._pane_pid("named-session")
        driver._kill_session("named-session")
    targets = {cmd[1]: cmd[cmd.index("-t") + 1] for cmd in calls if "-t" in cmd}
    _check(
        targets
        == {
            "has-session": "=named-session",
            "capture-pane": "=named-session:",
            "list-panes": "=named-session:",
            "kill-session": "=named-session",
        },
        f"host operations use exact targets of the right kind (got {targets!r})",
    )


def test_invalid_session_reference_is_a_failed_probe() -> None:
    calls: list[list[str]] = []

    def run_fn(cmd: list[str], **_kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        return _FakeCompleted()

    with tempfile.TemporaryDirectory() as tmp:
        driver = _configured_driver(Path(tmp), run_fn=run_fn)
        _check(not driver.alive("bad:ref"), "an invalid reference is not alive")
        _check(driver._capture_pane_text("bad:ref") == "", "capture of an invalid reference is empty")
        driver.terminate("bad:ref", grace_seconds=1)
        _check(not calls, "an invalid reference reaches no tmux command")


def main() -> int:
    for marker in THIRD_PARTY_PROVIDER_ENV_MARKERS:
        os.environ.pop(marker, None)
    test_spawn_confirms_bypass_then_dev_channels_prompt()
    test_provider_spawn_confirms_bypass_without_dev_channels()
    test_provider_spawn_without_bypass_dialogue_returns_after_bounded_wait()
    test_spawn_dev_channels_without_bypass_sends_only_enter()
    test_spawn_refuses_when_bypass_prompt_never_clears()
    test_dev_channels_prompt_requires_its_warning_marker()
    test_driver_channel_uses_exact_pane_targets()
    test_driver_host_operations_use_exact_targets()
    test_invalid_session_reference_is_a_failed_probe()

    print(f"PASSED: {shared._passed}")
    print(f"FAILED: {len(shared._failed)}")
    return int(bool(shared._failed))


if __name__ == "__main__":
    raise SystemExit(main())
