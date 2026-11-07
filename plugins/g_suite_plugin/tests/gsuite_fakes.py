"""Hermetic fakes shared by the g_suite_plugin smokes (no network, no Keychain).

``FakeVault`` mirrors the vault service's store/retrieve/exists/rotate/delete
envelope shapes in memory. ``connect_accounts`` wires a plugin to a REAL
``AccountStore`` over that vault and connects the given emails, so smokes
exercise the actual multiple-account model rather than a stub.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))

from g_suite_plugin.accounts import AccountStore  # noqa: E402
from g_suite_plugin.oauth.app_config import AppConfigLoader  # noqa: E402
from g_suite_plugin.oauth.token_store import TokenStore  # noqa: E402

CLIENT_SECRET = "client-secret-XYZ-123456"


def _ok(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data}


class FakeVault:
    """In-memory vault with the real service's result envelopes."""

    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.deleted: list[str] = []

    def store(self, key: str, value: str, tags: list[str] | None = None, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        self.values[key] = value
        return _ok({"key": key})

    def rotate(self, key: str, value: str) -> dict[str, Any]:
        self.values[key] = value
        return _ok({"key": key})

    def retrieve(self, key: str) -> dict[str, Any]:
        if key not in self.values:
            return {"action_status": "error", "error": {"message": "missing"}}
        return _ok({"value": self.values[key]})

    def exists(self, key: str) -> dict[str, Any]:
        return _ok({"exists": key in self.values})

    def delete(self, key: str) -> dict[str, Any]:
        self.values.pop(key, None)
        self.deleted.append(key)
        return _ok({"deleted": True})


def fake_loader() -> AppConfigLoader:
    config = SimpleNamespace(client_id="client-id", client_secret=CLIENT_SECRET, redirect_uri="http://127.0.0.1/cb")
    return SimpleNamespace(load=lambda: config)  # type: ignore[return-value]


def connect_accounts(plugin: Any, emails: list[str], vault: FakeVault | None = None) -> AccountStore:
    """Bind a real AccountStore to ``plugin`` and connect ``emails`` (first = default)."""
    vault = vault or FakeVault()
    loader = fake_loader()
    accounts = AccountStore(vault, loader)
    plugin._app_config_loader = loader  # noqa: SLF001
    plugin._token_store = TokenStore(vault, loader)  # noqa: SLF001
    plugin._accounts = accounts  # noqa: SLF001
    for index, email in enumerate(emails):
        store = accounts.token_store(email)
        store.store_initial_tokens(refresh_token=f"refresh-{index}-abcdefgh", access_token=f"access-{index}-abcdefgh", expires_in=3600)
        store.record_credential_facts(
            granted_scopes=("https://www.googleapis.com/auth/gmail.modify",),
            account_email=email,
            account_tier="consumer" if email.endswith("@gmail.com") else "workspace_user",
        )
        accounts.upsert(email, alias="", set_default=False)
    return accounts
