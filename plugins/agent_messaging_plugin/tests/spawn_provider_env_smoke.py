#!/usr/bin/env python3
"""Per-spawn provider overlay: request parsing, host applicability, host spec, and one end-to-end spawn.

Offline. The end-to-end leg uses the lifecycle smoke's in-memory state and fake
host drivers (imported as a module, as spawn_first_go_smoke does), with lane
worktree provisioning stubbed out because it needs an APP_HOME-derived checkout.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(REPO_ROOT),
    str(REPO_ROOT / "ananta" / "src"),
    str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"),
    str(Path(__file__).parent),
]

import session_lifecycle_verbs_smoke as base  # noqa: E402

from agent_messaging_plugin import session_hosts  # noqa: E402
from agent_messaging_plugin.plugin import _spawn_session_request_from_params  # noqa: E402
from agent_messaging_plugin.register_unit_client import VerbError  # noqa: E402
from agent_messaging_plugin.spawn_provider_env import (  # noqa: E402
    provider_env_host_spec,
    refuse_unapplied_provider_env,
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


_BASE_RAW: dict[str, object] = {
    "role_class": "fixture",
    "lane_id": "lane-provider-env",
    "unit_id": "unt_provider_fixture",
    "work_class": "dev",
    "budget_line": "fixture",
    "agent_runtime": "claude_code",
}
_BEDROCK = {"CLAUDE_CODE_USE_BEDROCK": "1"}
_PROXY_VALUE = "https://provider.example.invalid"
_PROXY = {"ANTHROPIC_BASE_URL": _PROXY_VALUE}


def test_parse_is_request_local() -> None:
    first = _spawn_session_request_from_params({**_BASE_RAW, "provider_env": _BEDROCK}, "operator:test")
    second = _spawn_session_request_from_params({**_BASE_RAW, "provider_env": _PROXY}, "operator:test")
    unset = _spawn_session_request_from_params(dict(_BASE_RAW), "operator:test")
    _check(first.provider_env == _BEDROCK and second.provider_env == _PROXY, "two requests each carry their own overlay")
    _check(unset.provider_env == {} and unset.provider_env is not first.provider_env, "an omitted overlay parses to a fresh empty mapping")
    first.provider_env["INJECTED"] = "x"
    _check("INJECTED" not in second.provider_env and "INJECTED" not in unset.provider_env, "mutating one request's overlay leaves the others untouched")
    try:
        _spawn_session_request_from_params({**_BASE_RAW, "provider_env": {"ANTHROPIC_BASE_URL": 1}}, "operator:test")
    except VerbError as exc:
        _check(exc.code == "provider_env_invalid", "a non-string overlay value is refused as provider_env_invalid")
    else:
        _check(False, "a non-string overlay value is refused as provider_env_invalid")


def test_host_applicability_matrix() -> None:
    claude = _spawn_session_request_from_params({**_BASE_RAW, "provider_env": _PROXY}, "operator:test")
    codex = replace(claude, agent_runtime="codex")
    unset = _spawn_session_request_from_params(dict(_BASE_RAW), "operator:test")
    refused: dict[str, str] = {}
    for label, request, host in (
        ("tmux", claude, "tmux"),
        ("headless", claude, "headless"),
        ("codex-tmux", codex, "tmux"),
    ):
        try:
            refuse_unapplied_provider_env(request, host)
        except VerbError as exc:
            refused[label] = exc.code
            _check(_PROXY_VALUE not in exc.message, f"{label} refusal names keys, never values")
    _check("tmux" not in refused, "a Claude overlay on the tmux host (which applies it) is accepted")
    _check(refused.get("headless") == "provider_selection_not_applied_by_host", "a Claude overlay on the headless host is refused by host")
    _check(refused.get("codex-tmux") == "provider_unsupported_for_runtime", "a codex runtime refuses the Claude overlay with the runtime code, even on tmux")
    refuse_unapplied_provider_env(unset, "headless")
    _check(True, "an empty overlay passes the applicability check on any host")


def test_host_spec_entry() -> None:
    claude = _spawn_session_request_from_params({**_BASE_RAW, "provider_env": _PROXY}, "operator:test")
    unset = _spawn_session_request_from_params(dict(_BASE_RAW), "operator:test")
    entry = provider_env_host_spec(claude)
    _check(dict(entry) == {"provider_env": _PROXY}, "a non-empty overlay is the only key of the host spec entry")
    _check(dict(provider_env_host_spec(unset)) == {}, "an ordinary spawn adds no host spec key")
    _check(entry["provider_env"] is not claude.provider_env, "the host spec entry is a copy, not the request's own mapping")


class _CapturingChannel:
    def insert(self, text: str) -> None:
        del text

    def submit(self) -> None:
        return None

    def send(self, text: str) -> None:
        del text


class _SpecCapturingDriver:
    """Records every host spawn spec; the channel satisfies DriverChannel."""

    def __init__(self) -> None:
        self.specs: list[dict[str, object]] = []
        self.channel = _CapturingChannel()

    def spawn(self, spec: object) -> str:
        self.specs.append(dict(cast(dict[str, object], spec)))
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _CapturingChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def test_end_to_end_host_spec_carries_overlay_and_result_does_not_echo_it() -> None:
    state = base._state()
    key = (session_hosts.DEFAULT_AGENT_RUNTIME, "tmux")
    original_driver = session_hosts._REGISTRY[key]  # noqa: SLF001 -- test seam
    capturing = _SpecCapturingDriver()
    session_hosts._REGISTRY[key] = capturing  # noqa: SLF001 -- test seam
    overlay_value = "host-spec-fixture-value-5510"
    try:
        with patch.object(base.lifecycle_verbs, "_provision_worktree_for_request", lambda *_a, **_k: None):
            result = base.spawn_session(
                state,
                base._spawn_req(
                    host="tmux",
                    visibility="tmux",
                    lane_id="lane-provider-env-host-spec",
                    provider_env={"PROVIDER_FIXTURE_KEY": overlay_value},
                ),
            )
        _check(
            len(capturing.specs) == 1 and capturing.specs[0].get("provider_env") == {"PROVIDER_FIXTURE_KEY": overlay_value},
            "the tmux host spawn spec carries the provider overlay",
        )
        _check(overlay_value not in repr(result), "the spawn result never echoes the overlay value")
        base.terminate_session(state, agent_instance_id=str(result["agent_instance_id"]), directed_by="operator:none")
    finally:
        session_hosts._REGISTRY[key] = original_driver  # noqa: SLF001 -- test seam


def main() -> int:
    test_parse_is_request_local()
    test_host_applicability_matrix()
    test_host_spec_entry()
    test_end_to_end_host_spec_carries_overlay_and_result_does_not_echo_it()
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
