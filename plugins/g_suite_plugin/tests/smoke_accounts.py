#!/usr/bin/env python3
"""Multiple-account model smoke for g_suite_plugin (hermetic: in-memory vault, no network).

Exercises the Operator ord 90 contract on a REAL AccountStore:
  1. first account is the default; aliases; re-connect refreshes, not duplicates
  2. resolution: one account needs no argument; several use the default; email
     or alias selects; unknown and no-default are gsuite.unknown_account listing
     the connected accounts
  3. every result names the account used (inline _run and deferred job paths)
  4. legacy single-account pair migrates on first use: copied (legacy kept),
     result carries an account_note; disconnect then deletes legacy keys too
  5. list_accounts / set_default_account / disconnect_account (revoke patched;
     only that account's keys deleted; default moves on)
  6. OAuth callback completion stores under the probed email with tier + scopes;
     connect_account requests the full mailbox scope only on request; connecting
     a second account before the legacy pair migrated keeps the legacy account
     (and its default unless set_default), and a failed migration loses nothing
     and says so; a failed Gmail probe falls back to the Drive account email
  7. refresh never sends scopes; invalid_grant names the 7-day Testing-mode expiry
  8. redaction: raw, percent-encoded and base64 token variants scrubbed from
     results and error messages

Run:
    SOLET_NAME=<name> .venv/bin/python3 plugins/g_suite_plugin/tests/smoke_accounts.py
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import quote, quote_plus

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from g_suite_plugin import account_processes  # noqa: E402
from g_suite_plugin.accounts import account_key_base  # noqa: E402
from g_suite_plugin.constants import (  # noqa: E402
    GMAIL_FULL_MAILBOX_SCOPE,
    GMAIL_MODIFY_SCOPE,
    VAULT_KEY_ACCESS_TOKEN,
    VAULT_KEY_REFRESH_TOKEN,
)
from g_suite_plugin.oauth import oauth_client, service_factory  # noqa: E402
from g_suite_plugin.oauth.oauth_client import GoogleTokens  # noqa: E402
from g_suite_plugin.oauth.token_store import TokenStore, TokenStoreError  # noqa: E402
from g_suite_plugin.plugin import GSuitePlugin  # noqa: E402
from g_suite_plugin.redaction import REDACTED, redact_text  # noqa: E402
from gsuite_fakes import CLIENT_SECRET, FakeVault, connect_accounts, fake_loader  # noqa: E402  # pyright: ignore[reportMissingImports]

_passed = 0
_failed: list[str] = []
_STATE = {"session_id": "s1", "flow_id": "f1"}


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _plugin(emails: list[str], vault: FakeVault | None = None) -> tuple[GSuitePlugin, FakeVault]:
    plugin = GSuitePlugin()
    plugin.initialize({})
    vault = vault or FakeVault()
    connect_accounts(plugin, emails, vault)
    return plugin, vault


def _profile_factory(seen: list[str]) -> Any:
    """A factory whose gmail().users().getProfile() records the active account."""

    def gmail() -> Any:
        def get_profile(*, userId: str) -> Any:
            return SimpleNamespace(execute=lambda: {"emailAddress": seen[-1] if seen else "x@example.com"})

        return SimpleNamespace(users=lambda: SimpleNamespace(getProfile=get_profile))

    return SimpleNamespace(gmail=gmail)


def test_registry_basics() -> None:
    plugin, _ = _plugin(["Work@Corp.example", "home@gmail.com"])
    accounts = plugin._require_accounts()
    records = accounts.records()
    _assert("emails are lower-cased", [r.email for r in records] == ["work@corp.example", "home@gmail.com"])
    _assert("first account is the default", records[0].is_default and not records[1].is_default)
    accounts.upsert("home@gmail.com", alias="home", set_default=False)
    accounts.upsert("work@corp.example", alias="work", set_default=False)
    records = accounts.records()
    _assert("re-connect refreshes, never duplicates", len(records) == 2)
    _assert("alias recorded", {r.alias for r in records} == {"home", "work"})
    _assert("re-connect keeps the default", next(r for r in records if r.email == "work@corp.example").is_default)
    raised = ""
    try:
        accounts.upsert("third@gmail.com", alias="HOME", set_default=False)
    except ValueError as exc:
        raised = str(exc)
    _assert("duplicate alias refused with the owner named", "home@gmail.com" in raised, raised)
    key_a = account_key_base("a.b@x.com")
    key_b = account_key_base("a_b@x.com")
    _assert("look-alike emails get distinct vault keys", key_a != key_b)


def test_resolution() -> None:
    plugin, _ = _plugin(["one@gmail.com"])
    probe = plugin._probe_store
    accounts = plugin._require_accounts()
    _assert("single account needs no argument", accounts.resolve(None, probe).record.email == "one@gmail.com")
    plugin, _ = _plugin(["work@corp.example", "home@gmail.com"])
    accounts = plugin._require_accounts()
    accounts.upsert("home@gmail.com", alias="home", set_default=False)
    _assert("several accounts: default used", accounts.resolve(None, probe).record.email == "work@corp.example")
    _assert("alias selects (case-insensitive)", accounts.resolve("HOME", probe).record.email == "home@gmail.com")
    _assert("email selects", accounts.resolve("Home@Gmail.com", probe).record.email == "home@gmail.com")
    try:
        accounts.resolve("nobody@x.com", probe)
        code, message = "", ""
    except TokenStoreError as exc:
        code, message = exc.code, str(exc)
    _assert("unknown account -> gsuite.unknown_account", code == "gsuite.unknown_account", code)
    _assert("unknown account error lists connected accounts", "work@corp.example" in message and "home@gmail.com" in message, message)
    result = plugin.gmail_list_messages({"account": "nobody@x.com"}, _STATE)
    _assert("dispatch refuses an unknown account before any job", result["error"]["code"] == "gsuite.unknown_account", str(result))


def test_result_names_account() -> None:
    plugin, _ = _plugin(["work@corp.example", "home@gmail.com"])
    plugin._require_accounts().upsert("home@gmail.com", alias="home", set_default=False)
    seen: list[str] = []
    real_call = plugin._call_as_account

    def spy(verb: str, params: dict[str, Any], produce: Any) -> dict[str, Any]:
        selector = params.get("account")
        seen.append("home@gmail.com" if selector == "home" else "work@corp.example")
        return real_call(verb, params, produce)

    plugin._call_as_account = spy  # type: ignore[method-assign]
    plugin._require_factory = lambda: _profile_factory(seen)  # type: ignore[method-assign]
    default_result = plugin.test_connection({}, {})
    _assert("inline result names the default account", default_result["data"].get("account") == "work@corp.example", str(default_result))
    alias_result = plugin.test_connection({"account": "home"}, {})
    _assert("inline result names the selected account", alias_result["data"].get("account") == "home@gmail.com", str(alias_result))
    _assert("consumer tier recorded for gmail.com", alias_result["data"].get("account_tier") == "consumer", str(alias_result))


def test_legacy_migration() -> None:
    vault = FakeVault()
    legacy = TokenStore(vault, fake_loader())
    legacy.store_initial_tokens(refresh_token="legacy-refresh-12345678", access_token="legacy-access-12345678", expires_in=3600)
    plugin, _ = _plugin([], vault)
    plugin._probe_store = staticmethod(lambda store: ("Owner@Corp.example", "workspace_user"))  # type: ignore[method-assign, assignment]
    _assert("legacy pair counts as connected", plugin._require_accounts().is_connected())
    resolved = plugin._require_accounts().resolve(None, plugin._probe_store)
    _assert("legacy migrated to the profile email", resolved.record.email == "owner@corp.example")
    _assert("migration note says copied, not moved", "copied, not moved" in resolved.note, resolved.note)
    _assert("legacy keys kept after migration", VAULT_KEY_REFRESH_TOKEN in vault.values and VAULT_KEY_ACCESS_TOKEN in vault.values)
    migrated = plugin._require_accounts().token_store("owner@corp.example")
    _assert("refresh token copied", migrated.read_raw_pair()[0] == "legacy-refresh-12345678")
    _assert("tier recorded on migration", migrated.credential_facts()["account_tier"] == "workspace_user")
    again = plugin._require_accounts().resolve(None, plugin._probe_store)
    _assert("migration runs once (no second note)", again.note == "")
    with patch.object(account_processes, "revoke_token", return_value="revoked"):
        result = plugin.disconnect_account({"account": "owner@corp.example"}, {})
    _assert("disconnect of migrated account succeeds", result["action_status"] == "completed", str(result))
    _assert("disconnect deletes legacy keys of a migrated account", VAULT_KEY_REFRESH_TOKEN not in vault.values)


def test_account_verbs() -> None:
    plugin, vault = _plugin(["work@corp.example", "home@gmail.com"])
    listed = plugin.list_accounts({}, {})["data"]
    _assert("list_accounts counts both", listed["count"] == 2, str(listed))
    row = listed["accounts"][1]
    _assert("list row has tier", row["tier"] == "consumer", str(row))
    _assert("list row has granted scopes", row["granted_scopes"] == [GMAIL_MODIFY_SCOPE], str(row))
    _assert("list row has default flag", row["default"] is False, str(row))
    _assert("list row has token health", row["token_health"] == "access_token_fresh", str(row))
    changed = plugin.set_default_account({"account": "home@gmail.com"}, {})
    _assert("set_default_account switches the default", changed["data"]["account"] == "home@gmail.com" and plugin._require_accounts().resolve(None, plugin._probe_store).record.email == "home@gmail.com")
    work_keys = [k for k in vault.values if account_key_base("work@corp.example") in k]
    revoked: list[str] = []
    with patch.object(account_processes, "revoke_token", side_effect=lambda token: revoked.append(token) or "revoked"):
        result = plugin.disconnect_account({"account": "home@gmail.com"}, {})
    data = result["data"]
    _assert("disconnect revoked the account's refresh token", revoked == ["refresh-1-abcdefgh"], str(revoked))
    _assert("disconnect reports revoke + deletion", data["revoke_status"] == "revoked" and data["credentials_deleted"] is True, str(data))
    _assert("default moves to the remaining account", data["new_default"] == "work@corp.example", str(data))
    _assert("other account's keys untouched", all(k in vault.values for k in work_keys))
    _assert("removed account's keys gone", not any(account_key_base("home@gmail.com") in k for k in vault.values))
    missing = plugin.disconnect_account({}, {})
    _assert("disconnect without account -> invalid_params", missing["error"]["code"] == "gsuite.invalid_params")


def test_connect_and_callback() -> None:
    plugin, _ = _plugin([])
    plugin._app_config_loader = SimpleNamespace(load=lambda: SimpleNamespace(client_id="cid", client_secret=CLIENT_SECRET, redirect_uri="http://127.0.0.1/cb"))  # type: ignore[assignment]
    plain = plugin.connect_account({"alias": "work"}, {})["data"]
    _assert("default consent omits the full mailbox scope", GMAIL_FULL_MAILBOX_SCOPE not in plain["requested_scopes"], str(plain["requested_scopes"]))
    _assert("default consent requests gmail.modify", GMAIL_MODIFY_SCOPE in plain["requested_scopes"])
    full = plugin.connect_account({"gmail_permanent_delete": True, "set_default": True}, {})["data"]
    _assert("permanent delete opt-in requests the full scope", GMAIL_FULL_MAILBOX_SCOPE in full["requested_scopes"])
    _assert("authorize_url carries the full scope", quote(GMAIL_FULL_MAILBOX_SCOPE, safe="") in full["authorize_url"] or "mail.google.com" in full["authorize_url"])
    tokens = GoogleTokens(access_token="new-access-abcdefgh", refresh_token="new-refresh-abcdefgh", expires_in=3600, granted_scopes=(GMAIL_MODIFY_SCOPE,))
    with patch("g_suite_plugin.account_processes.probe_account", return_value=("work@corp.example", "workspace_user")):
        plugin._complete_connect(plain["state"], tokens)
    record = plugin._require_accounts().records()[0]
    _assert("callback stores under the probed email with alias", record.email == "work@corp.example" and record.alias == "work" and record.is_default)
    facts = plugin._require_accounts().token_store("work@corp.example").credential_facts()
    _assert("callback records tier and granted scopes", facts["account_tier"] == "workspace_user" and facts["granted_scopes"] == [GMAIL_MODIFY_SCOPE], str(facts))
    _assert("pending choice consumed", plain["state"] not in plugin._connect.choices)


_LEGACY_REFRESH = "legacy-refresh-12345678"
_NEW_TOKENS = GoogleTokens(access_token="home-access-abcdefgh", refresh_token="home-refresh-abcdefgh", expires_in=3600, granted_scopes=(GMAIL_MODIFY_SCOPE,))


def _legacy_install() -> tuple[GSuitePlugin, FakeVault]:
    """An upgraded install: only the pre-multi-account pair, never migrated."""
    vault = FakeVault()
    TokenStore(vault, fake_loader()).store_initial_tokens(refresh_token=_LEGACY_REFRESH, access_token="legacy-access-12345678", expires_in=3600)
    plugin, _ = _plugin([], vault)
    plugin._probe_store = staticmethod(lambda store: ("owner@corp.example", "workspace_user"))  # type: ignore[method-assign, assignment]
    return plugin, vault


def _connect_home(plugin: GSuitePlugin, *, set_default: bool) -> str:
    """Complete a consent for home@gmail.com before the legacy pair was ever used."""
    state = f"state-home-{set_default}"
    plugin._connect.choices[state] = ("home", set_default)
    with patch("g_suite_plugin.account_processes.probe_account", return_value=("home@gmail.com", "consumer")):
        return plugin._complete_connect(state, _NEW_TOKENS)


def _active_account_factory() -> Any:
    """A factory whose Gmail profile answers as the account the call runs as."""
    profile = SimpleNamespace(getProfile=lambda *, userId: SimpleNamespace(execute=lambda: {"emailAddress": account_processes.ACTIVE_ACCOUNT.get()}))
    return SimpleNamespace(gmail=lambda: SimpleNamespace(users=lambda: profile))


def _unnamed_call(plugin: GSuitePlugin) -> dict[str, Any]:
    plugin._require_factory = _active_account_factory  # type: ignore[method-assign]
    return plugin.test_connection({}, {})["data"]


def _defaults(plugin: GSuitePlugin) -> dict[str, bool]:
    return {row["email"]: row["default"] for row in plugin.list_accounts({}, {})["data"]["accounts"]}


def test_connect_before_legacy_migration_keeps_legacy_default() -> None:
    plugin, vault = _legacy_install()
    note = _connect_home(plugin, set_default=False)
    listed = plugin.list_accounts({}, {})["data"]
    _assert("(a) connect before migration lists BOTH accounts", _defaults(plugin).keys() == {"owner@corp.example", "home@gmail.com"}, str(listed))
    _assert("(a) the legacy account is still the default", _defaults(plugin) == {"owner@corp.example": True, "home@gmail.com": False}, str(listed))
    _assert("(a) nothing left pending migration", listed["legacy_pending_migration"] is False, str(listed))
    _assert("(a) the connect reports the migration", "owner@corp.example" in note, note)
    accounts = plugin._require_accounts()
    _assert("(a) legacy_pending() is false and a second migrate_pending_legacy is a no-op", not accounts.legacy_pending() and accounts.migrate_pending_legacy(plugin._probe_store) == "")
    result = _unnamed_call(plugin)
    _assert("(a) a verb without `account` still acts on the legacy account and names it", result["account"] == "owner@corp.example" and result["account_identity"] == "owner@corp.example", str(result))
    _assert("(a) legacy keys kept", vault.values.get(VAULT_KEY_REFRESH_TOKEN) == _LEGACY_REFRESH)


def test_connect_before_legacy_migration_with_set_default() -> None:
    plugin, _ = _legacy_install()
    _connect_home(plugin, set_default=True)
    _assert("(b) set_default=True makes the new account the default", _defaults(plugin) == {"owner@corp.example": False, "home@gmail.com": True}, str(_defaults(plugin)))
    _assert("(b) a verb without `account` acts on the new default and names it", _unnamed_call(plugin)["account"] == "home@gmail.com")


def test_connect_with_failed_legacy_migration_loses_nothing() -> None:
    plugin, vault = _legacy_install()
    before = {key: vault.values[key] for key in (VAULT_KEY_REFRESH_TOKEN, VAULT_KEY_ACCESS_TOKEN)}

    def failing_probe(store: TokenStore) -> tuple[str, str]:
        raise TokenStoreError("gsuite.auth_expired", "invalid_grant")

    plugin._probe_store = staticmethod(failing_probe)  # type: ignore[method-assign, assignment]
    advisory = _connect_home(plugin, set_default=False)
    _assert("(c) a failed migration never blocks the new connect", "home@gmail.com" in _defaults(plugin), str(_defaults(plugin)))
    _assert("(c) the connect reports an advisory naming the failure", "could not be migrated" in advisory and "gsuite.auth_expired" in advisory, advisory)
    _assert("(c) legacy keys intact (no data loss)", all(vault.values.get(key) == value for key, value in before.items()))
    _assert("(c) list_accounts still shows the legacy pair pending", plugin.list_accounts({}, {})["data"]["legacy_pending_migration"] is True)
    result = _unnamed_call(plugin)
    _assert("(c) no silent switch: the result names the account and carries the advisory", result["account"] == "home@gmail.com" and "could not be migrated" in result.get("account_note", ""), str(result))
    plugin._probe_store = staticmethod(lambda store: ("owner@corp.example", "workspace_user"))  # type: ignore[method-assign, assignment]
    retried = _unnamed_call(plugin)
    _assert("(c) a later call retries and migrates without moving the default", _defaults(plugin) == {"home@gmail.com": True, "owner@corp.example": False} and "owner@corp.example" in retried.get("account_note", ""), str(retried))


def test_probe_falls_back_when_gmail_is_not_granted() -> None:
    def fake_build(name: str, version: str, **kwargs: Any) -> Any:
        if name == "gmail":
            raise RuntimeError("403 insufficientPermissions: Gmail not granted")
        about = SimpleNamespace(get=lambda *, fields: SimpleNamespace(execute=lambda: {"user": {"emailAddress": "Home@Gmail.com"}}))
        return SimpleNamespace(about=lambda: about)

    with patch("g_suite_plugin.oauth.service_factory.build", side_effect=fake_build):
        email, tier = service_factory.probe_account("access-abcdefgh")
    _assert("failed Gmail probe falls back to the Drive account email", email == "home@gmail.com", email)
    _assert("fallback records tier 'unknown'", tier == "unknown", tier)


def test_refresh_and_reauth_message() -> None:
    captured: dict[str, Any] = {}

    class _Creds:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)
            self.token = "refreshed-token-abcdefgh"
            self.refresh_token = None
            self.expiry = None

        def refresh(self, request: Any) -> None:
            return None

    app = SimpleNamespace(client_id="cid", client_secret=CLIENT_SECRET, redirect_uri="http://x")
    with patch.object(oauth_client, "Credentials", _Creds):
        oauth_client.refresh_access_token(app, refresh_token="rt-abcdefgh")  # type: ignore[arg-type]
    _assert("refresh never sends scopes (no invalid_scope after a scope is added)", "scopes" not in captured, str(captured))
    vault = FakeVault()
    store = TokenStore(vault, fake_loader())
    store.store_initial_tokens(refresh_token="rt-abcdefgh-123", access_token="at", expires_in=-100)
    with patch("g_suite_plugin.oauth.token_store.refresh_access_token", side_effect=RuntimeError("invalid_grant: Token has been expired or revoked.")):
        try:
            store.get_access_token()
            code, message = "", ""
        except TokenStoreError as exc:
            code, message = exc.code, str(exc)
    _assert("invalid_grant -> gsuite.auth_expired", code == "gsuite.auth_expired", code)
    _assert("message names the 7-day Testing-mode expiry and reconnect steps", "7 days" in message and "Testing mode" in message and "connect_account" in message and "In production" in message, message)
    store.record_credential_facts(granted_scopes=("a",), account_email="x@y.z", account_tier="consumer")
    with patch("g_suite_plugin.oauth.token_store.refresh_access_token", return_value=GoogleTokens(access_token="fresh-abcdefgh", refresh_token=None, expires_in=3600)):
        store.get_access_token()
    _assert("credential facts survive a refresh", store.credential_facts()["account_tier"] == "consumer")


def test_redaction() -> None:
    secret = "ya29.A0Ab+cd/ef=gh"
    variants = [
        secret,
        quote(secret, safe=""),
        quote_plus(secret),
        base64.b64encode(secret.encode()).decode(),
        base64.urlsafe_b64encode(secret.encode()).decode().rstrip("="),
    ]
    text = " | ".join(variants)
    scrubbed = redact_text(text, [secret])
    _assert("every token variant redacted", all(v not in scrubbed for v in variants) and scrubbed.count(REDACTED) == len(variants), scrubbed)
    plugin, _ = _plugin(["me@corp.example"])
    leak = "access-0-abcdefgh"

    def produce_leak() -> dict[str, Any]:
        return {"echo": f"token={quote(leak, safe='')}", "nested": [base64.b64encode(leak.encode()).decode()]}

    result = plugin._run(produce_leak, "gmail_get_profile", {})
    _assert("result payload redacted (percent + base64)", leak not in json.dumps(result) and base64.b64encode(leak.encode()).decode() not in json.dumps(result), json.dumps(result))

    def produce_error() -> dict[str, Any]:
        raise RuntimeError(f"upstream echoed refresh-0-abcdefgh and {CLIENT_SECRET}")

    failed = plugin._run(produce_error, "gmail_get_profile", {})
    message = failed["error"]["message"]
    _assert("error message redacts refresh token and client secret", "refresh-0-abcdefgh" not in message and CLIENT_SECRET not in message and REDACTED in message, message)


def main() -> int:
    print("\ng_suite_plugin multiple-account smoke")
    print("=" * 40)
    test_registry_basics()
    test_resolution()
    test_result_names_account()
    test_legacy_migration()
    test_account_verbs()
    test_connect_and_callback()
    test_connect_before_legacy_migration_keeps_legacy_default()
    test_connect_before_legacy_migration_with_set_default()
    test_connect_with_failed_legacy_migration_loses_nothing()
    test_probe_falls_back_when_gmail_is_not_granted()
    test_refresh_and_reauth_message()
    test_redaction()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All account smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
