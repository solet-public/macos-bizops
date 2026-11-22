#!/usr/bin/env python3
"""Channel-support smoke: where Claude Code surfaces development-channel events.

A bridge declares ``wake_capable`` from its own environment: true only for
Claude Code on a provider that honours ``--dangerously-load-development-channels``.
On a third-party provider (marker ``CLAUDE_CODE_USE_BEDROCK``) Claude Code
ignores the flag and never surfaces channel events, so the pane notice is that
session's only wake and the bridge must not claim a channel wake.

FAILING MUTATION: make ``bridge_wake_capable`` return ``is_claude_code`` alone
and the Bedrock leg of ``test_bridge_wake_capable`` reds; make
``provider_ignores_dev_channels`` return a constant and the predicate legs red.

Standalone -- not pytest. Run with::

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/channel_support_smoke.py
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.channel_support import (  # noqa: E402
    THIRD_PARTY_PROVIDER_ENV_MARKERS,
    bridge_wake_capable,
    provider_ignores_dev_channels,
)

_BEDROCK_MARKER = "CLAUDE_CODE_USE_BEDROCK"


def _check(condition: bool, label: str) -> None:
    assert condition, label
    print(f"  ok: {label}")


def test_provider_predicate() -> None:
    _check(provider_ignores_dev_channels({_BEDROCK_MARKER: "1"}) is True, "marker '1' -> channels ignored")
    _check(provider_ignores_dev_channels({_BEDROCK_MARKER: " 1 "}) is True, "whitespace around '1' is stripped")
    _check(provider_ignores_dev_channels({}) is False, "no marker -> channels surfaced")
    _check(provider_ignores_dev_channels({_BEDROCK_MARKER: "0"}) is False, "marker '0' -> channels surfaced")
    _check(provider_ignores_dev_channels({_BEDROCK_MARKER: ""}) is False, "empty marker -> channels surfaced")
    _check(provider_ignores_dev_channels({_BEDROCK_MARKER: "true"}) is False, "only the literal '1' counts")
    _check(_BEDROCK_MARKER in THIRD_PARTY_PROVIDER_ENV_MARKERS, "the field-verified marker is in the shared tuple")


def test_bridge_wake_capable() -> None:
    _check(bridge_wake_capable(is_claude_code=True, env={}) is True, "Claude Code, no marker -> wake capable")
    _check(
        bridge_wake_capable(is_claude_code=True, env={_BEDROCK_MARKER: "1"}) is False,
        "Claude Code on a third-party provider -> not wake capable",
    )
    _check(bridge_wake_capable(is_claude_code=False, env={}) is False, "non-Claude agent -> not wake capable")
    _check(
        bridge_wake_capable(is_claude_code=False, env={_BEDROCK_MARKER: "1"}) is False,
        "non-Claude agent on a third-party provider -> not wake capable",
    )


def main() -> int:
    print("=== channel_support_smoke ===")
    try:
        test_provider_predicate()
        test_bridge_wake_capable()
    except AssertionError:
        print("\nCHANNEL_SUPPORT_SMOKE: FAIL")
        traceback.print_exc()
        return 1
    except Exception:
        print("\nCHANNEL_SUPPORT_SMOKE: ERROR")
        traceback.print_exc()
        return 2
    print("\nCHANNEL_SUPPORT_SMOKE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
