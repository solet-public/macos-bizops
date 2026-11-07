#!/usr/bin/env python3
"""Regression smoke for kind versus instance arguments to PeerRegistry.resolve."""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.services.store import open_store  # noqa: E402

from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import (  # noqa: E402
    PeerRegistry,
    PeerUnreachableError,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)


def _registry() -> PeerRegistry:
    store = open_store(
        get_peer_binding_schema(),
        namespace=PEER_BINDING_NAMESPACE,
        backend="in_memory",
    )
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-regression",
            agent_id="claude_code",
            agent_instance_id="agi-live-regression",
            session_label="Regression",
            parent_pid=12345,
        ),
    )
    return registry


def _unreachable(registry: PeerRegistry, peer_id: str) -> str:
    try:
        registry.resolve(peer_id)
    except PeerUnreachableError as error:
        return str(error)
    raise AssertionError(f"expected PeerUnreachableError for {peer_id!r}")


def test_malformed_instance_prefix_is_wrong_kind() -> None:
    message = _unreachable(_registry(), "agi-")
    assert "peer_wrong_kind" in message
    assert "peer_agent_instance_id" in message


def test_live_instance_id_in_kind_slot_is_wrong_kind() -> None:
    message = _unreachable(_registry(), "agi-live-regression")
    assert "peer_wrong_kind" in message, "mutant kill: wrong_kind_uses_instance_parameter"
    assert "peer_agent_instance_id" in message, "mutant kill: wrong_kind_uses_instance_parameter"
    assert "no live bindings" not in message


def test_unknown_kind_names_available_kinds() -> None:
    message = _unreachable(_registry(), "unregistered_agent_kind")
    assert "no live bindings" in message
    assert "claude_code" in message


def test_kind_and_instance_resolve() -> None:
    registry = _registry()
    binding = registry.resolve("claude_code", "agi-live-regression")
    assert binding.agent_id == "claude_code"
    assert binding.agent_instance_id == "agi-live-regression"


def main() -> int:
    tests = (
        test_live_instance_id_in_kind_slot_is_wrong_kind,
        test_malformed_instance_prefix_is_wrong_kind,
        test_unknown_kind_names_available_kinds,
        test_kind_and_instance_resolve,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
