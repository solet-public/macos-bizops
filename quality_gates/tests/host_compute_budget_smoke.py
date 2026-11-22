#!/usr/bin/env python3
"""Exercise the independent client of the neutral lease protocol."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from quality_gates import host_compute_budget  # noqa: E402


def write_host_snapshot(root: Path) -> None:
    """Publish the numeric format with fixture capacities and demands."""
    root.mkdir(parents=True, exist_ok=True)
    capacities = {"cpu_slots": {"value": 24}, "pg_segments": {"value": 28}}
    demands = {kind: {"cpu_slots": {"value": cpu}, "pg_segments": {"value": 0}} for kind, cpu in {"full_battery": 8, "landing_battery": 8, "deploy": 16, "landing_gate": 18, "scoped_checkin": 8}.items()}
    revisions = {f"capacity:{resource}": 1 for resource in capacities}
    revisions.update({f"demand:{kind}:{resource}": 1 for kind, rows in demands.items() for resource in rows})
    snapshot = {"host_format_version": 1, "capacities": capacities, "demands": demands, "revision_vector": revisions}
    (root / "settings.json").write_text(json.dumps(snapshot))


class HostComputeBudgetSmoke(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / "home"
        home.mkdir()
        self.root = Path(temporary.name) / "host"
        environment = patch.dict(os.environ, {"HOME": str(home), "HOST_HEAVY_LEASE_DIR": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)

    def test_atomic_admission_pid_identity_and_measured_completion(self) -> None:
        root = self.root
        write_host_snapshot(root)
        first = host_compute_budget.try_acquire("full_battery", 8, token="first")
        self.assertTrue(first.admitted, first.reason)
        second = host_compute_budget.try_acquire("deploy", 16, token="second")
        self.assertTrue(second.admitted, second.reason)
        third = host_compute_budget.try_acquire("full_battery", 8, token="third")
        self.assertFalse(third.admitted, "8_PLUS_16_PLUS_8_EXCEEDS_24")
        row = json.loads((root / "leases/first.json").read_text())
        self.assertEqual(row["owner_start"], host_compute_budget.process_birth(os.getpid()))
        row["owner_start"] = "stale-pid-start"
        (root / "leases/first.json").write_text(json.dumps(row))
        retry = host_compute_budget.try_acquire("full_battery", 8, token="third")
        self.assertTrue(retry.admitted, retry.reason)
        self.assertFalse((root / "leases/first.json").exists())
        host_compute_budget.release(second)
        host_compute_budget.release(retry)
        use = json.loads((root / "use/third.json").read_text())
        self.assertEqual(use["peak_pg_segments"], 0)
        self.assertIsInstance(use["cpu_seconds"], float)
        self.assertIsInstance(use["wall_seconds"], float)
        self.assertGreaterEqual(use["wall_seconds"], 0)
        self.assertFalse((root / "leases/third.json").exists())

    def test_missing_or_unknown_settings_advise(self) -> None:
        root = self.root
        missing = host_compute_budget.try_acquire("full_battery", token="missing")
        self.assertTrue(missing.admitted, missing.reason)
        self.assertTrue(any(advisory.startswith("settings_unavailable:") for advisory in missing.advisories))
        self.assertIn("capacity_unknown:cpu_slots", missing.advisories)
        host_compute_budget.release(missing)

        write_host_snapshot(root)
        settings_path = root / "settings.json"
        snapshot = json.loads(settings_path.read_text())
        for version in (True, 1.0, "1", 2, None):
            with self.subTest(version=version):
                settings_path.write_text(json.dumps({**snapshot, "host_format_version": version}))
                outcome = host_compute_budget.try_acquire("full_battery", 25)
                self.assertTrue(outcome.admitted, outcome.reason)
                self.assertIn("settings_schema_unknown", outcome.advisories)
                self.assertIn("capacity_unknown:cpu_slots", outcome.advisories)
                host_compute_budget.release(outcome)


if __name__ == "__main__":
    unittest.main()
