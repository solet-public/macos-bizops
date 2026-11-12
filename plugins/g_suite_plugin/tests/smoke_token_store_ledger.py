"""TokenStore credential ledgers: keyed by the account's vault key pair, no change when unregistered.

``oauth.token_store.credential_ledger(refresh_key, access_key, seed)`` registers
an append-only ledger for one account. While registered, every TokenStore for
that key pair, on any thread or instance, records into ALL ledgers registered
for the pair: each access token it writes or returns, each refresh token it
writes or rotates, and the client secret it refreshes with. With no ledger
registered for a pair (Gmail, Calendar, Drive and every other caller) the hooks
are no-ops: vault operations, values and returned tokens are identical.
Hermetic: in-memory vault, patched OAuth refresh, no network.
"""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/g_suite_plugin/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from g_suite_plugin.oauth import token_store  # noqa: E402
from g_suite_plugin.oauth.oauth_client import GoogleTokens  # noqa: E402
from g_suite_plugin.oauth.token_store import TokenStore, active_ledger_count, credential_ledger, ledger_values  # noqa: E402
from gsuite_fakes import CLIENT_SECRET, FakeVault, fake_loader  # noqa: E402  # pyright: ignore[reportMissingImports]

PAIR = ("r", "a")


class LoggingVault(FakeVault):
    """FakeVault that logs every operation, to compare registered and unregistered runs."""

    def __init__(self) -> None:
        super().__init__()
        self.log: list[tuple[str, str]] = []

    def store(self, key: str, value: str, tags: list[str] | None = None, metadata: dict[str, str] | None = None) -> dict[str, Any]:
        self.log.append(("store", key))
        return super().store(key, value, tags, metadata)

    def rotate(self, key: str, value: str) -> dict[str, Any]:
        self.log.append(("rotate", key))
        return super().rotate(key, value)

    def retrieve(self, key: str) -> dict[str, Any]:
        self.log.append(("retrieve", key))
        return super().retrieve(key)


def store_for(vault: FakeVault, pair: tuple[str, str] = PAIR) -> TokenStore:
    return TokenStore(vault, fake_loader(), refresh_key=pair[0], access_key=pair[1])


def lifecycle(vault: LoggingVault) -> list[str]:
    """Initial store, cached read, two forced refreshes (one rotating, one on another instance)."""
    store = store_for(vault)
    store.store_initial_tokens(refresh_token="refresh-1", access_token="access-1", expires_in=3600)
    returned = [store.get_access_token()]
    replies = [GoogleTokens("access-2", None, 3600), GoogleTokens("access-3", "refresh-2", 3600)]
    with patch.object(token_store, "refresh_access_token", side_effect=replies):
        returned.append(store.get_access_token(force_refresh=True))
        returned.append(store_for(vault).get_access_token(force_refresh=True))
    return returned


class TokenStoreLedgerTests(unittest.TestCase):
    def tearDown(self) -> None:
        self.assertEqual(active_ledger_count(), 0)
        self.assertEqual(token_store._ACTIVE_LEDGERS, {})  # noqa: SLF001

    def test_unregistered_pair_changes_nothing(self) -> None:
        unregistered, registered, other_pair = LoggingVault(), LoggingVault(), LoggingVault()
        baseline = lifecycle(unregistered)
        with credential_ledger(*PAIR) as ledger:
            observed = lifecycle(registered)
        with credential_ledger("other-r", "other-a") as unrelated:
            elsewhere = lifecycle(other_pair)
        self.assertEqual(baseline, ["access-1", "access-2", "access-3"])
        self.assertEqual(observed, baseline)
        self.assertEqual(elsewhere, baseline)
        # Byte-identical when no ledger watches the pair, even while another pair is watched.
        self.assertEqual(other_pair.log, unregistered.log)
        # Watching costs no vault operation: the journal records values the store already handles.
        self.assertEqual(registered.log, unregistered.log)
        self.assertEqual(registered.values.keys(), unregistered.values.keys())
        self.assertEqual(registered.values["r"], unregistered.values["r"])
        self.assertTrue(ledger)
        self.assertEqual(unrelated, [])

    def test_records_every_credential_through_any_instance_and_thread(self) -> None:
        vault = LoggingVault()
        with credential_ledger(*PAIR, seed=["seed-secret", "", "seed-secret"]) as ledger:
            lifecycle(vault)
            worker = threading.Thread(target=lambda: store_for(vault).store_initial_tokens(refresh_token="refresh-t", access_token="access-t", expires_in=3600))
            worker.start()
            worker.join()
        self.assertEqual(ledger, ["seed-secret", "refresh-1", "access-1", CLIENT_SECRET, "access-2", "refresh-2", "access-3", "refresh-t", "access-t"])

    def test_overlapping_ledgers_on_one_account_each_see_every_credential(self) -> None:
        vault = LoggingVault()
        store_for(vault).store_initial_tokens(refresh_token="refresh-0", access_token="access-0", expires_in=-60)
        with credential_ledger(*PAIR) as first:
            with patch.object(token_store, "refresh_access_token", return_value=GoogleTokens("from-first", None, 3600)):
                store_for(vault).get_access_token()
            with credential_ledger(*PAIR) as second:
                with patch.object(token_store, "refresh_access_token", return_value=GoogleTokens("from-second", None, 3600)):
                    pool_thread = threading.Thread(target=lambda: store_for(vault).get_access_token(force_refresh=True))
                    pool_thread.start()
                    pool_thread.join()
                self.assertEqual(active_ledger_count(), 2)
            with patch.object(token_store, "refresh_access_token", return_value=GoogleTokens("after-second", None, 3600)):
                store_for(vault).get_access_token(force_refresh=True)
        self.assertIn("from-first", first)
        self.assertIn("from-second", first)
        self.assertIn("from-second", second)
        self.assertIn("from-first", second)  # replaced while the second ledger was registered (R2)
        self.assertNotIn("after-second", second)
        self.assertIn("after-second", first)
        self.assertEqual(ledger_values(first), first)

    def test_registry_is_empty_after_exit_error_and_nesting(self) -> None:
        with self.assertRaises(RuntimeError), credential_ledger(*PAIR, seed=["outer"]) as outer:
            with credential_ledger(*PAIR):
                self.assertEqual(active_ledger_count(), 2)
            self.assertEqual(active_ledger_count(), 1)
            raise RuntimeError("stop")
        self.assertEqual(outer, ["outer"])
        self.assertEqual(active_ledger_count(), 0)
        self.assertEqual(token_store._ACTIVE_LEDGERS, {})  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
