#!/usr/bin/env python3
"""Tier U: append-only proof lifecycle, clean-only policy and racing CAS."""

# ruff: noqa: E402
from __future__ import annotations

import sys
import tempfile
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
            store.correct(KEY, {"change_id": "change-one", "annotation": "measured"}, expected_version=2, principal=PRINCIPAL, reason="set links")
            store.correct(KEY, {"change_id": "change-two"}, expected_version=3, principal=PRINCIPAL, reason="correct link")
            store.clear(KEY, ["annotation"], clear_reuse=True, expected_version=4, principal=PRINCIPAL, reason="clear active link")
            self.assertFalse(store.get(KEY)["reusable"])
            store.retire(KEY, expected_version=5, principal=PRINCIPAL, reason="retire")
            store.reinstate(KEY, current_key=KEY, expected_version=6, principal=PRINCIPAL, reason="revalidated")
            self.assertTrue(store.get(KEY)["reusable"])
            self.assertEqual(store.get(KEY, as_of=1)["observation"]["verdict"], "failed")
            self.assertEqual(len(store.history(KEY)), 7)
            self.assertEqual(store.get(KEY)["metadata"], {"change_id": "change-two"})

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


if __name__ == "__main__":
    unittest.main()
