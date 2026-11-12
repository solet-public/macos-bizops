"""Vault-backed token store for Google OAuth credentials.

Single-account model: one refresh token and one access token per plugin
install. Vault keys are scoped (``<solet>.g_suite_plugin.*``).

Google token semantics differ from Schwab's in one important way: Google
refresh tokens are DURABLE and are not rotated on every refresh. A plain
``grant_type=refresh_token`` returns a new access token and usually NO new
refresh token. So the refresh path updates only the access token, and rotates
the refresh token ONLY on the rare occasion Google returns a changed one. There
is no 7-day hard cap; a refresh token stays valid until revoked, the app is
unpublished, or it goes 6 months unused.
"""

from __future__ import annotations

import itertools
import json
import threading
import weakref
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast

from ..constants import (
    ACCESS_TOKEN_EARLY_REFRESH_SECONDS,
    ACCESS_TOKEN_VALUE_KEY_ACCOUNT_EMAIL,
    ACCESS_TOKEN_VALUE_KEY_ACCOUNT_TIER,
    ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT,
    ACCESS_TOKEN_VALUE_KEY_GRANTED_SCOPES,
    ACCESS_TOKEN_VALUE_KEY_TOKEN,
    ACCOUNT_TIER_UNKNOWN,
    ERROR_AUTH_EXPIRED,
    ERROR_NOT_CONNECTED,
    ERROR_REFRESH_TOKEN_ROTATE_FAILED,
    ERROR_TOKEN_EXCHANGE_FAILED,
    ERROR_TOKEN_STORE_FAILED,
    ERROR_VAULT_NOT_AVAILABLE,
    VAULT_KEY_ACCESS_TOKEN,
    VAULT_KEY_REFRESH_TOKEN,
    VAULT_TAG_ACCESS_TOKEN,
    VAULT_TAG_REFRESH_TOKEN,
)
from .app_config import AppConfigError, AppConfigLoader
from .oauth_client import refresh_access_token

# Credential journal. Every credential value a TokenStore reads from, or begins
# writing to, a vault key is appended to that key's journal, on any thread and
# whether or not anyone is watching; a strict snapshot appends what it finds.
# The editor jobs scrub with the journal of their account's keys, so a value the
# store held, read or wrote at any time up to the job's exit is in the scrub set
# with no inference about which write replaced which value. A key is "seen" once
# its held value is known to be journaled (a read returned it, or a write applied
# it). The one value the journal cannot hold is a key's original value never read
# in this process: a write that BEGINS while its key is unseen may replace it
# unrecorded, so every window overlapping that write fails closed. While no
# window is open, each journal is trimmed to its last _JOURNAL_LIMIT values on
# every append (a later window starts from its own strict snapshot, and copies
# the values of writes still pending); trimming a key forgets that its held value
# is journaled. While any window is open nothing is trimmed, and a key gaining
# more than _JOURNAL_LIMIT values fails the watching windows closed. Watching
# costs no vault operation: unwatched callers are unchanged.
_KeyPair = tuple[str, str]
_JOURNAL_LIMIT = 64
_LOCK = threading.RLock()
# Per vault service: key -> journaled values, and the keys whose held value is journaled.
_JOURNAL: weakref.WeakKeyDictionary[Any, dict[str, list[str]]] = weakref.WeakKeyDictionary()
_SEEN: weakref.WeakKeyDictionary[Any, set[str]] = weakref.WeakKeyDictionary()
# Per vault service: key -> values appended while any window was open.
_GROWTH: weakref.WeakKeyDictionary[Any, dict[str, int]] = weakref.WeakKeyDictionary()
_IN_FLIGHT: dict[int, _Write] = {}
_ACTIVE_LEDGERS: dict[_KeyPair | None, list[CredentialLedger]] = {}
_WRITE_IDS = itertools.count(1)


class CredentialLedger(list[str]):
    """A registered window: every value journaled for its pairs while it is open; only ever grows."""

    def __init__(self, pair: _KeyPair | None) -> None:
        super().__init__()
        self.pair = pair
        self.uncertain_pairs: set[_KeyPair] = set()

    def watches(self, pair: _KeyPair) -> bool:
        return self.pair is None or self.pair == pair

    def add(self, *values: str | None) -> None:
        with _LOCK:
            self.extend(value for value in dict.fromkeys(values) if value and value not in self)

    def is_uncertain(self, pair: _KeyPair | None = None) -> bool:
        """True when a credential of ``pair`` (any pair when None) may be missing from the journal."""
        with _LOCK:
            return bool(self.uncertain_pairs) if pair is None else pair in self.uncertain_pairs


@dataclass
class _Write:
    pair: _KeyPair
    key: str
    new: str | None
    blind: bool


@contextmanager
def credential_ledger(refresh_key: str | None = None, access_key: str | None = None, seed: Iterable[str] = ()) -> Generator[CredentialLedger]:
    """Open a window on one key pair (or every pair when both keys are None)."""
    ledger = CredentialLedger(None if refresh_key is None or access_key is None else (refresh_key, access_key))
    with _LOCK:
        pending = [write for write in _IN_FLIGHT.values() if ledger.watches(write.pair)]
        ledger.uncertain_pairs.update(write.pair for write in pending if write.blind)
        ledger.add(*(write.new for write in pending))  # a pending value may already be trimmed from the journal
        _ACTIVE_LEDGERS.setdefault(ledger.pair, []).append(ledger)
    try:
        ledger.add(*seed)
        yield ledger
    finally:
        _close(ledger)


def _close(ledger: CredentialLedger) -> None:
    """Unregister a window; once none is open, trim every journal to its last _JOURNAL_LIMIT values."""
    with _LOCK:
        remaining = [entry for entry in _ACTIVE_LEDGERS.get(ledger.pair, []) if entry is not ledger]
        if remaining:
            _ACTIVE_LEDGERS[ledger.pair] = remaining
        else:
            _ACTIVE_LEDGERS.pop(ledger.pair, None)
        if _ACTIVE_LEDGERS:
            return
        _GROWTH.clear()
        for vault, journals in list(_JOURNAL.items()):
            for key in list(journals):
                _trim(vault, key)


def _trim(vault: Any, key: str) -> None:
    """Keep a key's last _JOURNAL_LIMIT values; if any were dropped, its held value may no longer be journaled."""
    values = _JOURNAL[vault][key]
    if len(values) > _JOURNAL_LIMIT:
        del values[:-_JOURNAL_LIMIT]
        _SEEN.get(vault, set()).discard(key)


def ledger_values(ledger: list[str]) -> list[str]:
    """A consistent copy of ``ledger`` (other threads may be appending)."""
    with _LOCK:
        return list(ledger)


def active_ledger_count() -> int:
    """How many windows are open (0 when no call is scrubbing)."""
    with _LOCK:
        return sum(len(ledgers) for ledgers in _ACTIVE_LEDGERS.values())


def journal_values(vault: Any, pair: _KeyPair) -> list[str]:
    """Every value journaled in ``vault`` for both keys of ``pair``."""
    with _LOCK:
        journals = _JOURNAL.get(vault, {})
        return [*journals.get(pair[0], ()), *journals.get(pair[1], ())]


def _watchers(pair: _KeyPair) -> list[CredentialLedger]:
    return [*_ACTIVE_LEDGERS.get(pair, ()), *_ACTIVE_LEDGERS.get(None, ())]


def _journal(vault: Any, pair: _KeyPair, key: str, value: str | None) -> None:
    if not value:
        return
    with _LOCK:
        values = _JOURNAL.setdefault(vault, {}).setdefault(key, [])
        added = value not in values
        if added:
            values.append(value)
        if not _ACTIVE_LEDGERS:
            _trim(vault, key)
            return
        watchers = _watchers(pair)
        for ledger in watchers:
            ledger.add(value)
        if not added:
            return
        growth = _GROWTH.setdefault(vault, {})
        growth[key] = growth.get(key, 0) + 1
        if growth[key] > _JOURNAL_LIMIT:
            for ledger in watchers:
                ledger.uncertain_pairs.add(pair)


def _envelope_token(envelope: str) -> str | None:
    try:
        payload: Any = json.loads(envelope)
    except ValueError:
        return None
    token = payload.get(ACCESS_TOKEN_VALUE_KEY_TOKEN) if isinstance(payload, dict) else None
    return token if isinstance(token, str) else None


_FACT_KEYS = (
    ACCESS_TOKEN_VALUE_KEY_GRANTED_SCOPES,
    ACCESS_TOKEN_VALUE_KEY_ACCOUNT_EMAIL,
    ACCESS_TOKEN_VALUE_KEY_ACCOUNT_TIER,
)

# Google's documented limit for an External OAuth client in Testing mode: its
# refresh tokens expire 7 days after consent (invalid_grant on the next refresh).
_REAUTH_GUIDANCE = (
    "Google rejected the stored refresh token (invalid_grant), so the account must be "
    "reconnected. The most common cause: a self-registered External OAuth client whose "
    "consent screen is in Testing mode, where Google expires refresh tokens 7 days after "
    "consent. Other causes: the user revoked access, changed the password, or the token "
    "went 6 months unused. Reconnect: run start_interface, then connect_account, open the "
    "returned authorize_url and approve. To stop the weekly expiry, set the app's OAuth "
    "consent screen publishing status to In production in Google Cloud console."
)


class TokenStoreError(RuntimeError):
    """Raised when token resolution cannot proceed; carries a typed error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TokenStore:
    """Vault-backed access/refresh token manager for the Google plugin.

    The vault service is duck-typed (mirrors VaultServiceInterface) so this
    plugin never imports from macos_vault_plugin.
    """

    def __init__(
        self,
        vault_service: Any,
        app_config_loader: AppConfigLoader,
        *,
        refresh_key: str = VAULT_KEY_REFRESH_TOKEN,
        access_key: str = VAULT_KEY_ACCESS_TOKEN,
    ) -> None:
        if vault_service is None:
            raise TokenStoreError(
                ERROR_VAULT_NOT_AVAILABLE,
                "vault_service is required for TokenStore",
            )
        self._vault = vault_service
        self._app_config_loader = app_config_loader
        # One account's key pair; the defaults are the legacy single-account pair.
        self._refresh_key = refresh_key
        self._access_key = access_key

    # ------------------------------------------------------------------
    # Bootstrap write path — called by the OAuth callback handler
    # ------------------------------------------------------------------

    def store_initial_tokens(
        self,
        refresh_token: str,
        access_token: str,
        expires_in: int,
    ) -> None:
        """Write both tokens after a successful authorization_code exchange.

        A new consent starts with no recorded credential facts; the callback
        records them with :meth:`record_credential_facts` right after.
        """
        self._write_refresh_token(refresh_token)
        self._write_envelope(_build_access_envelope(access_token, expires_in, {}))

    def record_credential_facts(
        self,
        *,
        granted_scopes: tuple[str, ...],
        account_email: str,
        account_tier: str,
    ) -> None:
        """Record what Google granted and which account tier signed in.

        Stored on the access envelope and carried across every refresh, so a
        missing-scope error and test_connection can name the actual grant.
        """
        envelope = self._read_envelope()
        if envelope is None:
            raise TokenStoreError(ERROR_NOT_CONNECTED, "No access token stored to record credential facts on.")
        envelope[ACCESS_TOKEN_VALUE_KEY_GRANTED_SCOPES] = list(granted_scopes)
        envelope[ACCESS_TOKEN_VALUE_KEY_ACCOUNT_EMAIL] = account_email
        envelope[ACCESS_TOKEN_VALUE_KEY_ACCOUNT_TIER] = account_tier
        self._write_envelope(json.dumps(envelope, separators=(",", ":")))

    def credential_facts(self) -> dict[str, Any]:
        """The recorded grant and tier; ``None``/"unknown" when never recorded."""
        envelope = self._read_envelope() or {}
        scopes = envelope.get(ACCESS_TOKEN_VALUE_KEY_GRANTED_SCOPES)
        granted = [str(scope) for scope in scopes] if isinstance(scopes, list) else None  # pyright: ignore[reportUnknownVariableType]
        email = envelope.get(ACCESS_TOKEN_VALUE_KEY_ACCOUNT_EMAIL)
        tier = envelope.get(ACCESS_TOKEN_VALUE_KEY_ACCOUNT_TIER)
        return {
            "granted_scopes": granted,
            "account_email": email if isinstance(email, str) else "",
            "account_tier": tier if isinstance(tier, str) and tier else ACCOUNT_TIER_UNKNOWN,
        }

    def issued_secrets(self) -> list[str]:
        """Every secret this credential holds, for redaction (never logged)."""
        secrets: list[str] = []
        refresh_value = self._read_value(self._refresh_key)
        if refresh_value:
            secrets.append(refresh_value)
        envelope = self._read_envelope() or {}
        access_value = envelope.get(ACCESS_TOKEN_VALUE_KEY_TOKEN)
        if isinstance(access_value, str) and access_value:
            secrets.append(access_value)
        try:
            client_secret = self._app_config_loader.load().client_secret
        except Exception:  # noqa: BLE001 — redaction must still run on the tokens it has
            client_secret = ""
        if client_secret:
            secrets.append(client_secret)
        return secrets

    def held_credentials(self) -> list[str]:
        """Strict snapshot: read and journal both stored credentials and the client secret.

        Unlike :meth:`issued_secrets`, any failed vault read raises, so a caller
        knows when the journal may be missing a held credential.
        """
        values = [self._strict_credential(key) for key in (self._refresh_key, self._access_key)]
        values.append(self.current_client_secret())
        return [value for value in values if value]

    def _strict_credential(self, key: str) -> str | None:
        exists = self._vault.exists(key)
        if not _is_completed(exists):
            raise TokenStoreError(ERROR_TOKEN_STORE_FAILED, "Stored credentials could not be read.")
        if not (exists.get("data") or {}).get("exists", False):
            self._journal_read(key, None)
            return None
        result = self._vault.retrieve(key)
        if not _is_completed(result):
            raise TokenStoreError(ERROR_TOKEN_STORE_FAILED, "Stored credentials could not be read.")
        stored = (result.get("data") or {}).get("value")
        return self._journal_read(key, stored if isinstance(stored, str) else None)

    def current_client_secret(self) -> str:
        """The OAuth client secret in use now (app config, no vault), added to every open window."""
        secret = self._app_config_loader.load().client_secret
        with _LOCK:
            for ledger in _watchers(self._pair()):
                ledger.add(secret)
        return secret

    def journal_values(self) -> list[str]:
        """Every credential value journaled for this account's two keys."""
        return journal_values(self._vault, self._pair())

    def _pair(self) -> _KeyPair:
        return (self._refresh_key, self._access_key)

    def _journal_read(self, key: str, stored: str | None) -> str | None:
        """Journal a value a read returned (the token inside an access envelope); the key is now seen."""
        credential = (_envelope_token(stored) if key == self._access_key else stored) if stored is not None else None
        with _LOCK:
            _journal(self._vault, self._pair(), key, credential)
            _SEEN.setdefault(self._vault, set()).add(key)
        return credential

    def _journaled_write[Result](self, key: str, new: str | None, put: Callable[[], Result]) -> Result:
        """Journal the value BEFORE the vault call; a write to an unseen key fails overlapping windows closed."""
        pair = self._pair()
        with _LOCK:
            write_id = next(_WRITE_IDS)
            blind = key not in _SEEN.get(self._vault, set())
            _IN_FLIGHT[write_id] = _Write(pair, key, new, blind)
            _journal(self._vault, pair, key, new)
            if blind:
                for ledger in _watchers(pair):
                    ledger.uncertain_pairs.add(pair)
        applied = False
        try:
            result = put()
            applied = _is_completed(result)
            return result
        finally:
            with _LOCK:
                del _IN_FLIGHT[write_id]
                if applied:
                    _SEEN.setdefault(self._vault, set()).add(key)

    # ------------------------------------------------------------------
    # Runtime read path — called by the service factory
    # ------------------------------------------------------------------

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a non-expired access token, refreshing via OAuth if needed."""
        if not force_refresh:
            cached = self._read_cached_access_token()
            if cached is not None:
                return cached
        return self._refresh_and_persist()

    @property
    def refresh_key(self) -> str:
        return self._refresh_key

    @property
    def access_key(self) -> str:
        return self._access_key

    def read_raw_pair(self) -> tuple[str | None, str | None]:
        """The stored refresh token and access envelope, verbatim (account migration)."""
        return self._read_value(self._refresh_key), self._read_value(self._access_key)

    def write_raw_pair(self, refresh_token: str, access_envelope: str | None) -> None:
        """Write a refresh token and access envelope verbatim (account migration)."""
        self._write_refresh_token(refresh_token)
        if access_envelope is not None:
            self._write_envelope(access_envelope)

    def delete_all(self) -> None:
        """Delete this account's refresh token and access envelope from vault."""
        for key in (self._refresh_key, self._access_key):
            if self._exists(key):
                self._journaled_write(key, None, partial(self._vault.delete, key))

    def token_health(self) -> str:
        """No-network health word: access_token_fresh, refreshes_on_next_call or missing_refresh_token."""
        if not self._exists(self._refresh_key):
            return "missing_refresh_token"
        envelope = self._read_envelope() or {}
        expires_raw = envelope.get(ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT)
        if isinstance(expires_raw, str) and _is_token_fresh(datetime.fromisoformat(expires_raw)):
            return "access_token_fresh"
        return "refreshes_on_next_call"

    def is_connected(self) -> bool:
        """True when a refresh token is present in vault."""
        return self._exists(self._refresh_key)

    # ------------------------------------------------------------------
    # Internal — refresh path
    # ------------------------------------------------------------------

    def _refresh_and_persist(self) -> str:
        try:
            app_config = self._app_config_loader.load()
        except AppConfigError as exc:
            raise TokenStoreError(exc.code, str(exc)) from exc

        refresh_value = self._read_value(self._refresh_key)
        if refresh_value is None:
            raise TokenStoreError(
                ERROR_NOT_CONNECTED,
                "No Google refresh token stored. Run connect_account first.",
            )
        with _LOCK:
            for ledger in _watchers(self._pair()):
                ledger.add(app_config.client_secret)
        try:
            tokens = refresh_access_token(app_config, refresh_token=refresh_value)
        except Exception as exc:
            raise _classify_refresh_error(exc) from exc

        # Google usually omits refresh_token on refresh; rotate only if it
        # actually returned a new, different one.
        if tokens.refresh_token and tokens.refresh_token != refresh_value:
            self._rotate_refresh_token(tokens.refresh_token)
        self._write_access_token(tokens.access_token, tokens.expires_in)
        return tokens.access_token

    # ------------------------------------------------------------------
    # Internal — vault operations
    # ------------------------------------------------------------------

    def _write_refresh_token(self, refresh_token: str) -> None:
        result = self._journaled_write(self._refresh_key, refresh_token, partial(self._put_refresh_token, refresh_token))
        _require_completed(result, "refresh-token vault write failed")

    def _put_refresh_token(self, refresh_token: str) -> Any:
        if self._exists(self._refresh_key):
            return self._vault.rotate(self._refresh_key, refresh_token)
        return self._vault.store(
            key=self._refresh_key,
            value=refresh_token,
            tags=[VAULT_TAG_REFRESH_TOKEN],
            metadata={"issued_at": datetime.now(UTC).isoformat()},
        )

    def _rotate_refresh_token(self, new_refresh_token: str) -> None:
        result = self._journaled_write(self._refresh_key, new_refresh_token, partial(self._vault.rotate, self._refresh_key, new_refresh_token))
        if not _is_completed(result):
            raise TokenStoreError(
                ERROR_REFRESH_TOKEN_ROTATE_FAILED,
                "Vault refresh-token rotation failed",
            )

    def _write_access_token(self, access_token: str, expires_in: int) -> None:
        previous = self._read_envelope() or {}
        facts = {key: previous[key] for key in _FACT_KEYS if key in previous}
        self._write_envelope(_build_access_envelope(access_token, expires_in, facts))

    def _read_envelope(self) -> dict[str, Any] | None:
        envelope = self._read_value(self._access_key)
        if envelope is None:
            return None
        try:
            payload: Any = json.loads(envelope)
        except ValueError:
            return None
        return cast(dict[str, Any], payload) if isinstance(payload, dict) else None

    def _write_envelope(self, envelope: str) -> None:
        result = self._journaled_write(self._access_key, _envelope_token(envelope), partial(self._put_envelope, envelope))
        _require_completed(result, "access-token vault write failed")

    def _put_envelope(self, envelope: str) -> Any:
        if self._exists(self._access_key):
            return self._vault.rotate(self._access_key, envelope)
        return self._vault.store(
            key=self._access_key,
            value=envelope,
            tags=[VAULT_TAG_ACCESS_TOKEN],
            metadata={},
        )

    def _read_cached_access_token(self) -> str | None:
        envelope = self._read_value(self._access_key)
        if envelope is None:
            return None
        token, expires_at = _parse_access_envelope(envelope)
        if not _is_token_fresh(expires_at):
            return None
        return token

    def _read_value(self, key: str) -> str | None:
        exists = self._vault.exists(key)
        if not _is_completed(exists):
            return None
        if not (exists.get("data") or {}).get("exists", False):
            self._journal_read(key, None)  # definitely absent: nothing unjournaled is held
            return None
        result = self._vault.retrieve(key)
        if not _is_completed(result):
            return None
        data = result.get("data") or {}
        value = data.get("value")
        text = value if isinstance(value, str) else None
        self._journal_read(key, text)
        return text

    def _exists(self, key: str) -> bool:
        result = self._vault.exists(key)
        if not _is_completed(result):
            return False
        data = result.get("data") or {}
        return bool(data.get("exists", False))


def _classify_refresh_error(exc: Exception) -> TokenStoreError:
    error_text = str(exc).lower()
    if "invalid_grant" in error_text or "invalid_client" in error_text:
        return TokenStoreError(ERROR_AUTH_EXPIRED, _REAUTH_GUIDANCE)
    return TokenStoreError(ERROR_TOKEN_EXCHANGE_FAILED, f"Google token refresh failed: {exc}")


def _build_access_envelope(access_token: str, expires_in: int, facts: dict[str, Any]) -> str:
    expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
    return json.dumps(
        {
            ACCESS_TOKEN_VALUE_KEY_TOKEN: access_token,
            ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT: expires_at.isoformat(),
            **facts,
        },
        separators=(",", ":"),
    )


def _parse_access_envelope(envelope: str) -> tuple[str, datetime]:
    payload: Any = json.loads(envelope)
    if not isinstance(payload, dict):
        raise TokenStoreError(
            ERROR_TOKEN_EXCHANGE_FAILED,
            "Stored access-token entry is not a JSON object",
        )
    token = payload.get(ACCESS_TOKEN_VALUE_KEY_TOKEN)
    expires_at_raw = payload.get(ACCESS_TOKEN_VALUE_KEY_EXPIRES_AT)
    if not isinstance(token, str) or not isinstance(expires_at_raw, str):
        raise TokenStoreError(
            ERROR_TOKEN_EXCHANGE_FAILED,
            "Stored access-token entry is missing token or expires_at",
        )
    return token, datetime.fromisoformat(expires_at_raw)


def _is_completed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("action_status") == "completed"


def _require_completed(result: Any, message: str) -> None:
    """Raise a typed TokenStoreError when a vault store/rotate did not complete.

    Message is generic on purpose — it never includes token material, so a
    failure surfaced to the caller/logs cannot leak a secret.
    """
    if not _is_completed(result):
        raise TokenStoreError(ERROR_TOKEN_STORE_FAILED, message)


def _is_token_fresh(expires_at: datetime) -> bool:
    threshold = datetime.now(UTC) + timedelta(seconds=ACCESS_TOKEN_EARLY_REFRESH_SECONDS)
    return expires_at > threshold
