#!/usr/bin/env python3
"""Tier U: append-only proof lifecycle, clean-only policy and racing CAS."""

# ruff: noqa: E402
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates.smoke_pass_store import SmokePassStore, StaleProofVersionError, clean_observation

KEY = "a" * 64
PRINCIPAL = "fixture-owner"


def observation(**overrides: object) -> dict[str, object]:
    return {"key": KEY, "smoke": "suite/check_smoke.py", "verdict": "passed", "exit_code": 0, "complete": True, "identity_complete": True, "attempt_count": 1, **overrides}


class StoreTests(unittest.TestCase):
    def test_full_lifecycle_preserves_original_red(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))
            red = store.observe(KEY, observation(verdict="failed", exit_code=1), expected_version=0, principal=PRINCIPAL)
            self.assertFalse(red["reusable"])
            clean = store.observe(KEY, observation(), expected_version=1, principal=PRINCIPAL)
            self.assertTrue(clean["reusable"])
            store.correct(KEY, {"annotation": "measured"}, expected_version=2, principal=PRINCIPAL, reason="set annotation")
            store.correct(KEY, {"annotation": "corrected"}, expected_version=3, principal=PRINCIPAL, reason="correct annotation")
            self.assertEqual(store.get(KEY)["metadata"], {"annotation": "corrected"})
            store.clear(KEY, ["annotation"], clear_reuse=True, expected_version=4, principal=PRINCIPAL, reason="clear annotation")
            self.assertFalse(store.get(KEY)["reusable"])
            store.retire(KEY, expected_version=5, principal=PRINCIPAL, reason="retire")
            store.reinstate(KEY, current_key=KEY, expected_version=6, principal=PRINCIPAL, reason="revalidated")
            self.assertTrue(store.get(KEY)["reusable"])
            self.assertEqual(store.get(KEY, as_of=1)["observation"]["verdict"], "failed")
            self.assertEqual(len(store.history(KEY)), 7)
            self.assertEqual(store.get(KEY)["metadata"], {})

    def test_no_nonclean_result_reuses(self) -> None:
        self.assertTrue(clean_observation(observation()))
        for delta in ({"verdict": "skipped"}, {"verdict": "not_run"}, {"verdict": "failed"}, {"attempt_count": 0}, {"attempt_count": 2}, {"retried": True}, {"skipped": True}, {"known_failure": True}, {"timed_out": True}, {"partial": True}, {"identity_complete": False}, {"complete": False}, {"exit_code": 77}):
            with self.subTest(delta=delta):
                self.assertFalse(clean_observation(observation(**delta)))

    def test_stale_and_simultaneous_writers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))

            def write() -> str:
                try:
                    store.observe(KEY, observation(), expected_version=0, principal=PRINCIPAL)
                    return "accepted"
                except StaleProofVersionError as error:
                    self.assertEqual(error.current_version, 1)
                    return "stale"

            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(sorted(pool.map(lambda _: write(), range(2))), ["accepted", "stale"])
            self.assertEqual(len(store.history(KEY)), 1)

    def test_unsafe_store_permissions_cannot_supply_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))
            self.assertTrue(store.observe(KEY, observation(), expected_version=0, principal=PRINCIPAL)["reusable"])
            key_directory = store.root / KEY
            key_directory.chmod(0o777)
            try:
                with self.assertRaises(PermissionError):
                    store.get(KEY)
            finally:
                key_directory.chmod(0o700)
            event = key_directory / "000000000001.json"
            event.chmod(0o666)
            try:
                with self.assertRaises(PermissionError):
                    store.get(KEY)
            finally:
                event.chmod(0o600)
            self.assertTrue(store.get(KEY)["reusable"])

    def test_a_store_cannot_retain_a_raw_attempt(self) -> None:
        self.assertFalse(hasattr(SmokePassStore, "retain_attempt"))

    def test_authorization_and_immutable_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))
            with self.assertRaises(PermissionError):
                store.observe(KEY, observation(), expected_version=0, principal="")
            with self.assertRaises(ValueError):
                store.get("../escape")
            with self.assertRaises(ValueError):
                store.correct(KEY, {"verdict": "passed"}, expected_version=0, principal=PRINCIPAL, reason="unsafe")
            with self.assertRaises(ValueError):
                store.clear(KEY, ["key"], clear_reuse=False, expected_version=0, principal=PRINCIPAL, reason="unsafe")
            with self.assertRaises(ValueError):
                store.reinstate(KEY, current_key="b" * 64, expected_version=0, principal=PRINCIPAL, reason="obsolete")


class EndOfProofTests(unittest.TestCase):
    OPEN = "a" * 40
    CLOSED = "b" * 40

    def _key(self, store: SmokePassStore, number: int, *, scope: str | None, tree: str, written_at: float) -> str:
        key = f"{number:064x}"
        store.begin(key, {"smoke": "suite/check_smoke.py"}, expected_version=0, principal=PRINCIPAL)
        if scope is not None:
            store.observe(key, observation(key=key, key_scope=scope, candidate_tree=tree), expected_version=1, principal=PRINCIPAL)
        os.utime(store.root / key, (written_at, written_at))
        return key

    def test_each_clause_of_the_rule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))
            now = time.time()
            for number in range(20):
                self._key(store, number, scope="tree", tree=self.OPEN, written_at=now)
                self._key(store, 20 + number, scope="tree", tree=self.CLOSED, written_at=now)
            for number in range(40, 50):
                self._key(store, number, scope="closure", tree=self.CLOSED, written_at=now)
            for number in range(50, 60):
                self._key(store, number, scope=None, tree=self.CLOSED, written_at=now)
            # Three copies that earlier runs left behind: the end still removes them after seven days.
            legacy = store.root / "attempts"
            legacy.mkdir(mode=0o700, exist_ok=True)
            for number in range(3):
                (legacy / f"{number:064x}.json").write_text("{}")
            self.assertEqual(store.prune({self.OPEN}, principal=PRINCIPAL, now=now), {"kept": 60, "removed_keys": 0, "removed_attempts": 0, "unreadable": 0})
            self.assertEqual(store.prune(None, principal=PRINCIPAL, now=now + 2 * 86400)["removed_keys"], 0)
            middle = store.prune({self.OPEN}, principal=PRINCIPAL, now=now + 2 * 86400)
            self.assertEqual(middle, {"kept": 40, "removed_keys": 20, "removed_attempts": 0, "unreadable": 0})
            self.assertEqual(len(list((store.root / "attempts").glob("*.json"))), 3)
            final = store.prune({self.OPEN}, principal=PRINCIPAL, now=now + 8 * 86400)
            self.assertEqual(final, {"kept": 0, "removed_keys": 40, "removed_attempts": 3, "unreadable": 0})
            self.assertEqual(store.list(), [])

    def test_the_store_is_bounded_over_thirty_days(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))
            start = time.time()
            total_removed = 0
            for day in range(30):
                for number in range(10):
                    self._key(store, day * 20 + number, scope="tree", tree=self.OPEN, written_at=start + day * 86400)
                    self._key(store, day * 20 + 10 + number, scope="tree", tree=self.CLOSED, written_at=start + day * 86400)
                result = store.prune({self.OPEN}, principal=PRINCIPAL, now=start + (day + 1) * 86400)
                total_removed += result["removed_keys"]
                if day in (0, 1, 7, 8, 19, 29):
                    print(f"day {day}: {result['kept']} keys")
                if day in (7, 8, 19, 29):
                    self.assertEqual(result["kept"], 80)
            self.assertEqual(total_removed, 520)

    def test_an_unreadable_event_holds_the_key_to_the_limit_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SmokePassStore(Path(directory))
            now = time.time()
            key = self._key(store, 1, scope="tree", tree=self.CLOSED, written_at=now)
            (store.root / key / "000000000002.json").write_bytes(b"not json")
            result = store.prune({self.OPEN}, principal=PRINCIPAL, now=now + 2 * 86400)
            self.assertEqual(result, {"kept": 1, "removed_keys": 0, "removed_attempts": 0, "unreadable": 1})
            self.assertTrue((store.root / key).exists())
            self.assertEqual(store.prune({self.OPEN}, principal=PRINCIPAL, now=now + 8 * 86400)["removed_keys"], 1)


if __name__ == "__main__":
    unittest.main()
