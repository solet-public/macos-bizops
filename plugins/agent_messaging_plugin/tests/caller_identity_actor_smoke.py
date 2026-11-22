#!/usr/bin/env python3
"""Focused regression for caller identity: actor derivation and its error envelope."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import DispatchActor  # noqa: E402
from agent_messaging_plugin.verb_errors import VerbError  # noqa: E402

_passed = 0
_failed: list[str] = []


class _Registry:
    def __init__(self, bindings: dict[str, str]) -> None:
        self.bindings = bindings

    def agent_session_id_for_instance(self, instance: str) -> str:
        return self.bindings.get(instance, "")


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _plugin(bindings: dict[str, str]) -> AgentMessagingPlugin:
    plugin = AgentMessagingPlugin()
    plugin._peer_registry = cast(Any, _Registry(bindings))  # noqa: SLF001
    plugin._get_state_service = lambda: object()  # type: ignore[method-assign]  # noqa: SLF001
    return plugin


def _actor(plugin: AgentMessagingPlugin, state: dict[str, Any]) -> DispatchActor | str:
    try:
        return plugin._dispatch_actor_from_state(state)  # noqa: SLF001
    except VerbError as exc:
        return exc.code


def _check_catalog_consumer_envelopes(plugin: AgentMessagingPlugin) -> None:
    """A rejected caller reaches the verb result as the same code and message."""
    anonymous = plugin.set_model_catalog_policy({}, {})
    error = anonymous["error"]
    _check(
        anonymous["action_status"] == "failed" and error["code"] == "dispatch_authentication_required",
        "catalog verb refuses an unidentified caller with its stable code",
    )
    _check(
        error["message"] and anonymous["data"] == {},
        "catalog verb envelope carries the refusal message and an empty data map",
    )
    unregistered = _plugin({}).set_model_cells_enabled({}, {"caller_attribution_instance_id": "agi-stale"})
    _check(
        unregistered["error"]["code"] == "dispatch_identity_unregistered",
        "model cell verb refuses a caller without a live peer binding",
    )


def main() -> int:
    plugin = _plugin({"agi-registered": "ases-registered", "agi-cli": "ases-cli"})
    attributed = _actor(plugin, {"caller_attribution_instance_id": "agi-cli"})
    precedence = _actor(
        plugin,
        {
            "inference_vertex_session_id": "agi-registered",
            "caller_attribution_instance_id": "agi-cli",
        },
    )
    _check(
        attributed == DispatchActor("agi-cli", "ases-cli", "live_peer_binding"),
        "attributed CLI call resolves its current peer binding",
    )
    _check(
        precedence == DispatchActor("agi-registered", "ases-registered", "live_peer_binding"),
        "registered inference identity takes precedence over caller attribution",
    )
    _check(
        _actor(_plugin({}), {"caller_attribution_instance_id": "agi-stale"}) == "dispatch_identity_unregistered" and _actor(plugin, {}) == "dispatch_authentication_required",
        "missing or stale identity does not fabricate another session",
    )
    _check_catalog_consumer_envelopes(plugin)
    print(f"\ncaller identity actor smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
