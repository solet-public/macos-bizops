#!/usr/bin/env python3
"""Hermetic smoke for the bounded Google Workspace connection qualifier."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from gsuite_fakes import FakeVault, connect_accounts, run_queued  # noqa: E402  # pyright: ignore[reportMissingImports]


def main() -> int:
    plugin = GSuitePlugin()
    plugin.initialize({})
    connect_accounts(plugin, ["operator@example.com"])
    calls: list[tuple[str, str]] = []

    def get_profile(*, userId: str) -> SimpleNamespace:
        calls.append(("getProfile", userId))
        return SimpleNamespace(execute=lambda: {"emailAddress": "operator@example.com"})

    plugin._require_factory = lambda: SimpleNamespace(  # type: ignore[method-assign]  # noqa: SLF001
        gmail=lambda: SimpleNamespace(users=lambda: SimpleNamespace(getProfile=get_profile))
    )
    result = run_queued(plugin, "test_connection", {})
    assert result["action_status"] == "completed", result
    data = result["data"]
    assert data["account_identity"] == "operator@example.com" and data["harmless_read_ok"] is True, result
    assert data["account"] == "operator@example.com", result
    assert data["account_tier"] == "workspace_user", result
    assert data["granted_scopes"] == ["https://www.googleapis.com/auth/gmail.modify"], result
    assert calls == [("getProfile", "me")], calls
    assert "drive" not in repr(calls).lower()

    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator

    PluginRegistrationValidator().validate_edge_process_provider("g_suite_plugin", plugin, discover_actions(plugin))

    connect_accounts(plugin, [], FakeVault())
    disconnected = run_queued(plugin, "test_connection", {})
    assert disconnected["error"]["code"] == "gsuite.not_connected", disconnected
    print("PASS bounded Google connection qualifier and EDGE registration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
