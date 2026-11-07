"""Multiple connected Google accounts (Operator ord 90, 2026-11-07).

Contract:

- One credential set per account, keyed by the lower-cased Gmail-profile email
  address, under per-account vault keys
  (``<solet>.g_suite_plugin.account__<slug>__refresh_token`` / ``__access_token``).
  The vault index entry ``<solet>.g_suite_plugin.accounts_index`` lists each
  account's email, optional alias, default flag and migration flag.
- ``connect_account`` is repeatable; re-connecting an email refreshes it.
- Every verb takes an optional ``account`` (email or alias, case-insensitive).
  Exactly one account: the argument is optional. Several: the default is used.
  Every result names the account that was used. An unknown account, or several
  accounts with no default, is ``gsuite.unknown_account`` listing the accounts.
- The pre-multi-account single credential pair is migrated to the first account
  on first use (keyed by its Gmail-profile email), and also by connect_account
  before it adds another account, so the legacy account is never orphaned and
  stays the default unless set_default is passed. The legacy keys are kept
  until that account is disconnected, so nothing is lost silently, and the
  first result after the migration says so. A failed migration keeps the
  legacy keys, reports an advisory and is retried on the next call.
- No unified inbox, cross-account search, sync or merging (scope guard).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from .constants import (
    ERROR_NOT_CONNECTED,
    ERROR_TOKEN_STORE_FAILED,
    ERROR_UNKNOWN_ACCOUNT,
    VAULT_KEY_ACCOUNT_PREFIX,
    VAULT_KEY_ACCOUNTS_INDEX,
    VAULT_TAG_ACCOUNTS_INDEX,
)
from .oauth.app_config import AppConfigLoader
from .oauth.token_store import TokenStore, TokenStoreError

# (access_token) -> (lower-cased email, tier); the plugin supplies a Gmail profile probe.
AccountProbe = Callable[[str], tuple[str, str]]

_NOT_CONNECTED_MESSAGE = "No Google account connected. Run start_interface, then connect_account, open the returned authorize_url and approve."


@dataclass(frozen=True)
class AccountRecord:
    """One connected Google account as the index records it."""

    email: str
    alias: str = ""
    is_default: bool = False
    migrated_from_legacy: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "email": self.email,
            "alias": self.alias,
            "default": self.is_default,
            "migrated_from_legacy": self.migrated_from_legacy,
        }


@dataclass(frozen=True)
class ResolvedAccount:
    """The account a call runs as, plus a one-time note (legacy migration)."""

    record: AccountRecord
    note: str = ""


def account_key_base(email: str) -> str:
    """Vault key base for one account: readable slug plus a collision-proof hash."""
    slug = re.sub(r"[^a-z0-9]+", "_", email.lower()).strip("_")[:48]
    digest = hashlib.sha256(email.lower().encode()).hexdigest()[:10]
    return f"{VAULT_KEY_ACCOUNT_PREFIX}{slug}_{digest}"


class AccountStore:
    """Vault-backed registry of connected accounts and their token stores."""

    def __init__(self, vault_service: Any, app_config_loader: AppConfigLoader) -> None:
        self._vault = vault_service
        self._app_config_loader = app_config_loader
        self._legacy = TokenStore(vault_service, app_config_loader)

    # -- reads ---------------------------------------------------------------

    def records(self) -> list[AccountRecord]:
        result = self._vault.retrieve(VAULT_KEY_ACCOUNTS_INDEX) if self._index_exists() else None
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            return []
        value = (result.get("data") or {}).get("value")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        payload: Any = json.loads(value) if isinstance(value, str) else {}
        rows: Any = payload.get("accounts") if isinstance(payload, dict) else []
        return [_record_from(row) for row in rows or [] if isinstance(row, dict)]  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]

    def is_connected(self) -> bool:
        """True when any account is connected, or a legacy pair awaits migration."""
        return bool(self.records()) or self._legacy.is_connected()

    def token_store(self, email: str) -> TokenStore:
        base = account_key_base(email)
        return TokenStore(
            self._vault,
            self._app_config_loader,
            refresh_key=f"{base}__refresh_token",
            access_key=f"{base}__access_token",
        )

    def check_selector(self, selector: str | None) -> None:
        """Dispatch-time validation (no network): raise for an unknown account."""
        records = self.records()
        if not records:
            if not self._legacy.is_connected():
                raise TokenStoreError(ERROR_NOT_CONNECTED, _NOT_CONNECTED_MESSAGE)
            return  # the legacy pair migrates in the worker
        _select(records, selector)

    def legacy_pending(self) -> bool:
        """True while the legacy pair exists and no account was migrated from it."""
        return self._legacy_pending(self.records())

    def resolve(self, selector: str | None, probe: Callable[[TokenStore], tuple[str, str]]) -> ResolvedAccount:
        """The account a call runs as; migrates the legacy pair on first use."""
        records = self.records()
        note = ""
        if not records and self._legacy.is_connected():
            records = [self._migrate_legacy(probe, records)]
            note = _migrated_note(records[0])
        elif self._legacy_pending(records):
            note = self.migrate_pending_legacy(probe)
            records = self.records()
        if not records:
            raise TokenStoreError(ERROR_NOT_CONNECTED, _NOT_CONNECTED_MESSAGE)
        return ResolvedAccount(record=_select(records, selector), note=note)

    def migrate_pending_legacy(self, probe: Callable[[TokenStore], tuple[str, str]]) -> str:
        """Migrate a pending legacy pair now, keeping it the default if it is the first account.

        Returns the note to report ('' when nothing was pending). A failure
        leaves the legacy keys untouched and returns an advisory instead of
        raising: an old credential never blocks connecting another account
        (rul_becb4bed), and the advisory rides on every result until a later
        call migrates it.
        """
        records = self.records()
        if not self._legacy_pending(records):
            return ""
        try:
            return _migrated_note(self._migrate_legacy(probe, records))
        except Exception as exc:  # noqa: BLE001 — any probe/vault fault becomes the advisory; nothing was deleted
            code = getattr(exc, "code", "") or type(exc).__name__
            return f"The previously connected Google account (single-account credentials) could not be migrated ({code}); its credentials are kept unchanged and the migration is retried on the next call. Until then the account named in `account` answered; run list_accounts to see the connected accounts."

    # -- writes --------------------------------------------------------------

    def upsert(self, email: str, *, alias: str, set_default: bool) -> AccountRecord:
        """Add or refresh an account record; the first account becomes the default."""
        email = email.lower()
        current = self.records()
        others = [r for r in current if r.email != email]
        _require_unique_alias(others, alias)
        existing = next((r for r in current if r.email == email), AccountRecord(email=email))
        make_default = set_default or not others or existing.is_default
        record = replace(existing, alias=alias or existing.alias, is_default=make_default)
        if make_default:
            others = [replace(r, is_default=False) for r in others]
        self._write_index([*others, record])
        return record

    def set_default(self, selector: str) -> AccountRecord:
        records = self.records()
        chosen = _select(records, selector)
        updated = [replace(r, is_default=r.email == chosen.email) for r in records]
        self._write_index(updated)
        return replace(chosen, is_default=True)

    def remove(self, selector: str) -> tuple[AccountRecord, AccountRecord | None]:
        """Delete one account's credentials and index row; return (removed, new default)."""
        records = self.records()
        removed = _select(records, selector)
        self.token_store(removed.email).delete_all()
        if removed.migrated_from_legacy:
            self._legacy.delete_all()
        remaining = [r for r in records if r.email != removed.email]
        new_default: AccountRecord | None = None
        if remaining and not any(r.is_default for r in remaining):
            remaining[0] = replace(remaining[0], is_default=True)
            new_default = remaining[0]
        self._write_index(remaining)
        return removed, new_default

    # -- internals -----------------------------------------------------------

    def _legacy_pending(self, records: list[AccountRecord]) -> bool:
        return self._legacy.is_connected() and not any(r.migrated_from_legacy for r in records)

    def _migrate_legacy(self, probe: Callable[[TokenStore], tuple[str, str]], records: list[AccountRecord]) -> AccountRecord:
        """Copy the legacy pair to its account and index it; it is the default only if none is."""
        email, tier = probe(self._legacy)
        email = email.lower()
        existing = next((r for r in records if r.email == email), None)
        if existing is None:
            self._copy_legacy_pair(email, tier)
        # An account re-connected after a failed migration keeps its newer tokens.
        others = [r for r in records if r.email != email]
        is_default = existing.is_default if existing else not any(r.is_default for r in others)
        record = replace(existing or AccountRecord(email=email), is_default=is_default, migrated_from_legacy=True)
        self._write_index([*others, record])
        return record

    def _copy_legacy_pair(self, email: str, tier: str) -> None:
        refresh_value, envelope = self._legacy.read_raw_pair()
        if not refresh_value:
            raise TokenStoreError(ERROR_NOT_CONNECTED, _NOT_CONNECTED_MESSAGE)
        target = self.token_store(email)
        target.write_raw_pair(refresh_value, envelope)
        facts = target.credential_facts()
        target.record_credential_facts(
            granted_scopes=tuple(facts["granted_scopes"] or ()),
            account_email=email,
            account_tier=tier,
        )

    def _index_exists(self) -> bool:
        result = self._vault.exists(VAULT_KEY_ACCOUNTS_INDEX)
        return isinstance(result, dict) and bool((result.get("data") or {}).get("exists"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

    def _write_index(self, records: list[AccountRecord]) -> None:
        value = json.dumps({"accounts": [r.as_dict() for r in records]}, separators=(",", ":"))
        if self._index_exists():
            result = self._vault.rotate(VAULT_KEY_ACCOUNTS_INDEX, value)
        else:
            result = self._vault.store(key=VAULT_KEY_ACCOUNTS_INDEX, value=value, tags=[VAULT_TAG_ACCOUNTS_INDEX], metadata={})
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise TokenStoreError(ERROR_TOKEN_STORE_FAILED, "account index vault write failed")


def describe_accounts(records: list[AccountRecord]) -> str:
    if not records:
        return "none"
    return ", ".join(f"{r.email}" + (f" (alias '{r.alias}')" if r.alias else "") + (" [default]" if r.is_default else "") for r in records)


def _migrated_note(record: AccountRecord) -> str:
    role = "It is the default account." if record.is_default else "It is not the default; run set_default_account to make it one."
    return f"Migrated the previously connected Google account to {record.email} (multiple-account support). Its credentials were copied, not moved; the old single-account vault entries remain until this account is disconnected. {role}"


def _select(records: list[AccountRecord], selector: str | None) -> AccountRecord:
    if selector:
        wanted = selector.strip().lower()
        for record in records:
            if wanted in (record.email, record.alias.lower()):
                return record
        raise TokenStoreError(
            ERROR_UNKNOWN_ACCOUNT,
            f"No connected Google account matches '{selector}'. Connected accounts: {describe_accounts(records)}. Pass one of those emails or aliases as `account`, or run connect_account to add it.",
        )
    if len(records) == 1:
        return records[0]
    default = next((r for r in records if r.is_default), None)
    if default is None:
        raise TokenStoreError(
            ERROR_UNKNOWN_ACCOUNT,
            f"Several Google accounts are connected and none is the default: {describe_accounts(records)}. Pass `account` (email or alias) or run set_default_account.",
        )
    return default


def _require_unique_alias(others: list[AccountRecord], alias: str) -> None:
    if not alias:
        return
    clash = next((r for r in others if r.alias.lower() == alias.lower() or r.email == alias.lower()), None)
    if clash is not None:
        raise ValueError(f"alias '{alias}' is already used by {clash.email}; choose another alias")


def _record_from(row: dict[str, Any]) -> AccountRecord:
    return AccountRecord(
        email=str(row.get("email") or "").lower(),
        alias=str(row.get("alias") or ""),
        is_default=bool(row.get("default")),
        migrated_from_legacy=bool(row.get("migrated_from_legacy")),
    )
