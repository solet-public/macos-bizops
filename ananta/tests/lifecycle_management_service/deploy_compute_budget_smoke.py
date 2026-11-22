#!/usr/bin/env python3
"""Deterministic admission wait, notice and immutable use-record evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from ananta.services.lifecycle_management_service import _deploy_by_rule as deploy_by_rule  # noqa: E402

from quality_gates import host_compute_budget  # noqa: E402


def _instant(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("missing timestamp")
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None:
        raise ValueError("timestamp has no UTC offset")
    return instant.astimezone(UTC)


def _commit(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{40}", value) is None:
        raise ValueError("missing or invalid full commit SHA")
    return value


def _read_evidence(path: Path, key: str, start: datetime, end: datetime) -> tuple[list[dict[str, Any]], datetime]:
    """Read an explicitly complete export, not an inferred complete row sample."""
    packet: object = json.loads(path.read_text())
    if not isinstance(packet, dict) or packet.get("complete") is not True:
        raise ValueError(f"{key} export is not explicitly complete")
    covered_until = _instant(packet.get("window_end"))
    if _instant(packet.get("window_start")) > start or covered_until < end:
        raise ValueError(f"{key} export does not cover the requested 24 hours")
    rows = packet.get(key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{key} rows missing or invalid")
    return cast(list[dict[str, Any]], rows), covered_until


def _production_merges(rows: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, datetime]:
    merges: dict[str, datetime] = {}
    for row in rows:
        if type(row.get("production")) is not bool:
            raise ValueError("landing lacks production classification")
        if not row["production"]:
            continue
        sha, merged = _commit(row.get("merge_sha")), _instant(row.get("merged_at"))
        if not start <= merged < end:
            continue
        if sha in merges:
            raise ValueError(f"duplicate production merge: {sha}")
        merges[sha] = merged
    if not merges:
        raise ValueError("no production landings in the requested window")
    return merges


def _serving_times(rows: list[dict[str, Any]], covered_until: datetime) -> dict[str, list[datetime]]:
    serving: dict[str, list[datetime]] = {}
    for row in rows:
        sha, instant = _commit(row.get("git_sha")), _instant(row.get("serving_at"))
        if instant > covered_until:
            raise ValueError("serving timestamp exceeds attested coverage")
        serving.setdefault(sha, []).append(instant)
    return serving


def evaluate_24h(landings_path: Path, deployments_path: Path, window_start: str) -> dict[str, Any]:
    """Match every production merge in [start, start+24h) to first later serving.

    Each JSON export is an object with complete=true, aware window_start and
    window_end covering that entire interval, and its landings/deployments list.
    Landing rows carry production (bool), merge_sha and merged_at. Deployment
    rows carry git_sha and serving_at: a measured serving instant, not a queued
    attempt or an inferred build time. Use full lowercase 40-digit commit SHAs.
    Exports may cover a larger interval so a late merge can serve after the day.
    Completeness is the exporter attestation; this reader cannot audit collection.
    """
    start = _instant(window_start)
    end = start + timedelta(hours=24)
    if end > datetime.now(UTC):
        raise ValueError("the requested 24-hour observation window has not ended")
    landings, _ = _read_evidence(landings_path, "landings", start, end)
    deployments, covered_until = _read_evidence(deployments_path, "deployments", start, end)
    merges = _production_merges(landings, start, end)
    serving = _serving_times(deployments, covered_until)
    delays: dict[str, float] = {}
    for sha, merged in merges.items():
        first = min((instant for instant in serving.get(sha, []) if instant >= merged), default=None)
        if first is None:
            raise ValueError(f"no serving evidence for production merge: {sha}")
        delays[sha] = (first - merged).total_seconds()
    maximum = max(delays.values())
    return {"status": "LIVE_24H_PASS" if maximum <= 1200 else "LIVE_24H_FAIL", "max_seconds": maximum, "production_landings": len(merges), "window_start": start.isoformat(), "window_end": end.isoformat(), "merge_to_serving_seconds": delays}


def live_24h_main(argv: list[str]) -> int:
    """Read-only evidence CLI; pass=0, breach or unverified evidence=1."""
    parser = argparse.ArgumentParser(description=evaluate_24h.__doc__)
    parser.add_argument("--live-24h", action="store_true")
    parser.add_argument("--landings-json", type=Path)
    parser.add_argument("--deployments-json", type=Path)
    parser.add_argument("--window-start")
    args = parser.parse_args(argv)
    try:
        if args.landings_json is None or args.deployments_json is None or args.window_start is None:
            raise ValueError("landings-json, deployments-json and window-start are required")
        result = evaluate_24h(args.landings_json, args.deployments_json, args.window_start)
    except (OSError, ValueError, OverflowError) as error:
        result = {"status": "LIVE_24H_UNVERIFIED", "reason": str(error)}
    print(f"{result['status']} {json.dumps(result, sort_keys=True)}")
    return int(result["status"] != "LIVE_24H_PASS")


class DeployComputeBudgetSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        snapshot = Path.home() / ".ananta/locks/host_heavy_admission/settings.json"
        self.assertTrue(snapshot.is_file(), "NEUTRAL_SETTINGS_SNAPSHOT_REQUIRED")
        self.settings = json.loads(snapshot.read_text())
        (self.root / "settings.json").write_bytes(snapshot.read_bytes())
        self.now = 0.0
        self.refusals = 0
        self.polls = 0
        self._lock_held = False
        self.admission_error = False
        self._execute_error = False
        self.notice_error = False
        self._lock_denied_at: float | None = None
        self.first_reason = "first reason"
        self.notices: list[tuple[float, dict[str, Any]]] = []
        self.args = argparse.Namespace(app_home=self.root, trigger="tick", agent_instance_id="agi-initiator", solet_name="fixture-solet", bridge="fake-bridge")
        self.ports = deploy_by_rule.Ports(self._command, Path.read_bytes, Path.resolve, lambda: [], lambda _path: False, self._lock, lambda: self.now, self._sleep, lambda: "", self._acquire, host_compute_budget.release)

    @contextmanager
    def _lock(self, _path: Path) -> Generator[bool]:
        self.assertFalse(self._lock_held)
        self._lock_held = True
        try:
            yield self._lock_denied_at is None or self.now < self._lock_denied_at
        finally:
            self._lock_held = False

    def _sleep(self, _seconds: float) -> None:
        self.assertFalse(self._lock_held, "WAIT_OUTSIDE_DEPLOY_LOCK")
        self.now += 100.0

    def _command(self, argv: list[str]) -> str:
        self.assertEqual(argv[2], deploy_by_rule.NOTICE)
        self.assertFalse(self._lock_held, "NOTICE_OUTSIDE_DEPLOY_LOCK")
        payload = json.loads(argv[3])
        self.notices.append((self.now, payload))
        if self.notice_error:
            raise ValueError("notice transport uncertain")
        return json.dumps({"status": "completed", "result": {"data": {"message_id": "arm-fixture"}}})

    def _acquire(self, job_kind: str) -> host_compute_budget.LeaseOutcome:
        self.polls += 1
        if self.polls <= self.refusals:
            return host_compute_budget.LeaseOutcome(False, self.first_reason if self.polls == 1 else "later reason", (), f"waiting-{self.polls}", self.root, os.getpid(), "fixture", job_kind, None, 0.0, 0.0)
        if self.admission_error:
            raise OSError("admission unavailable")
        return host_compute_budget.try_acquire(job_kind)

    def _execute(self, _ports: deploy_by_rule.Ports, _args: argparse.Namespace, _notes: list[str]) -> dict[str, Any]:
        rows = list((self.root / "leases").glob("*.json"))
        self.assertEqual(len(rows), 1, "DEPLOY_LEASE_HELD_DURING_EXECUTE")
        row = json.loads(rows[0].read_text())
        self.assertEqual(row["demand"]["cpu_slots"], self.settings["demands"]["deploy"]["cpu_slots"]["value"])
        self.now += 50.0  # Execution time is distinct from admission wait.
        if self._execute_error:
            raise ValueError("test failure")
        return {"decision": "deployed"}

    def _run_deploy(self) -> dict[str, Any]:
        with patch.dict(os.environ, {"HOST_HEAVY_LEASE_DIR": str(self.root)}), patch.object(deploy_by_rule, "execute", self._execute):
            return deploy_by_rule.deploy(self.ports, self.args)

    def _use(self) -> dict[str, Any]:
        rows = list((self.root / "use").glob("*.json"))
        self.assertEqual(len(rows), 1, "ONE_IMMUTABLE_USE_RECORD")
        self.assertFalse(list((self.root / "leases").glob("*.json")), "DEPLOY_LEASE_RELEASED")
        return json.loads(rows[0].read_text())

    def _wait_notices(self) -> list[tuple[float, dict[str, Any]]]:
        return [(when, payload) for when, payload in self.notices if json.loads(payload["content"]).get("decision") == "host_compute_wait"]

    def test_native_ports_bind_host_admission(self) -> None:
        ports = deploy_by_rule.native_ports()
        self.assertIs(ports.host_acquire, host_compute_budget.try_acquire)
        self.assertIs(ports.host_release, host_compute_budget.release)

    def test_wait_summary_notice_and_durable_agreement(self) -> None:
        self.refusals = 7
        result = self._run_deploy()
        self.assertEqual(result["decision"], "deployed")
        notes = [note for note in result["advisories"] if note.startswith("host_compute_wait:")]
        self.assertEqual(len(notes), 1, "ONE_TERMINAL_WAIT_ADVISORY")
        self.assertIn("700", notes[0])
        self.assertIn("first reason", notes[0])
        summary = result["host_compute_wait"]
        self.assertEqual(summary["wait_seconds"], 700.0)
        self.assertEqual(summary["wait_reason"], "first reason")
        self.assertEqual([(t, p["name"]) for t, p in self._wait_notices()], [(300.0, "Fixture-Solet-Main")], "ONE_NOTICE_AT_THRESHOLD")
        content = json.loads(self._wait_notices()[0][1]["content"])
        self.assertEqual(content["agent_instance_id"], self.args.agent_instance_id)
        self.assertEqual(content["wait_seconds"], 300.0)
        self.assertEqual(content["wait_reason"], "first reason")
        use = self._use()
        self.assertEqual((use["wait_seconds"], use["wait_reason"]), (summary["wait_seconds"], summary["wait_reason"]), "TERMINAL_USE_WAIT_AGREEMENT")

    def test_zero_wait(self) -> None:
        result = self._run_deploy()
        self.assertEqual(result["decision"], "deployed")
        self.assertEqual(result["host_compute_wait"], {"wait_seconds": 0.0, "wait_reason": None})
        self.assertFalse(self.notices)
        self.assertFalse(result["advisories"])
        use = self._use()
        self.assertEqual((use["wait_seconds"], use["wait_reason"]), (0.0, None))

    def test_admitted_at_threshold_still_notices(self) -> None:
        self.refusals = 3
        result = self._run_deploy()
        self.assertEqual(result["decision"], "deployed")
        self.assertEqual([when for when, _ in self._wait_notices()], [300.0])
        self.assertEqual(self._use()["wait_seconds"], 300.0)

    def test_failed_admission_has_no_use(self) -> None:
        self.refusals, self.admission_error = 4, True
        result = self._run_deploy()
        self.assertEqual((result["decision"], result["error"]), ("failed", "admission unavailable"))
        self.assertEqual(result["host_compute_wait"]["wait_seconds"], 400.0)
        self.assertEqual(len(self._wait_notices()), 1)
        self.assertFalse(list((self.root / "use").glob("*.json")))

    def test_lock_deferral_preserves_wait_without_use(self) -> None:
        self.refusals, self._lock_denied_at = 7, 300.0
        result = self._run_deploy()
        self.assertEqual(result["decision"], "deferred_lock")
        self.assertEqual(result["host_compute_wait"]["wait_seconds"], 300.0)
        self.assertEqual(len(self._wait_notices()), 1)
        self.assertFalse(list((self.root / "use").glob("*.json")))

    def test_first_reason_is_bounded_in_both_records(self) -> None:
        self.refusals, self.first_reason = 2, "x" * 1000
        result = self._run_deploy()
        reason = result["host_compute_wait"]["wait_reason"]
        self.assertEqual(len(reason), 240)
        self.assertEqual(reason, self._use()["wait_reason"])

    def test_notice_uncertainty_preserves_deployment(self) -> None:
        self.refusals, self.notice_error = 7, True
        result = self._run_deploy()
        self.assertEqual(result["decision"], "deployed")
        self.assertEqual(len(self._wait_notices()), 1)
        self.assertEqual(result["host_compute_wait"]["notice"]["status"], "uncertain")
        self.assertEqual(self._use()["wait_seconds"], 700.0)

    def test_execute_failure_releases_with_wait(self) -> None:
        self.refusals, self._execute_error = 2, True
        result = self._run_deploy()
        self.assertEqual((result["decision"], result["error"]), ("failed", "test failure"))
        self.assertEqual(self._use()["wait_seconds"], 200.0)
        self.assertFalse(self._wait_notices())

    def test_use_metadata_cannot_replace_another_owner(self) -> None:
        with patch.dict(os.environ, {"HOST_HEAVY_LEASE_DIR": str(self.root)}):
            lease = host_compute_budget.try_acquire("deploy")
            self.assertTrue(lease.admitted)
            annotated = host_compute_budget.with_wait(lease, 12.0, "first reason")
            path = self.root / "leases" / f"{lease.token}.json"
            row = json.loads(path.read_text())
            row["owner_start"] = "changed owner"
            path.write_text(json.dumps(row))
            with self.assertRaisesRegex(ValueError, "token_owner_mismatch"):
                host_compute_budget.release(annotated)
            self.assertFalse(list((self.root / "use").glob("*.json")))
            row["owner_start"] = lease.owner_start
            path.write_text(json.dumps(row))
            host_compute_budget.release(annotated)
            first = self._use()
            host_compute_budget.release(annotated)
            self.assertEqual(self._use(), first, "REPEATED_RELEASE_RETAINS_IMMUTABLE_HISTORY")

    def test_with_wait_correct_clear_and_invalid(self) -> None:
        with patch.dict(os.environ, {"HOST_HEAVY_LEASE_DIR": str(self.root)}):
            lease = host_compute_budget.try_acquire("deploy")
            annotated = host_compute_budget.with_wait(lease, 12.0, "reason")
            corrected = host_compute_budget.with_wait(annotated, 5.0, "corrected")
            cleared = host_compute_budget.with_wait(corrected, 0.0, None)
            self.assertEqual((annotated.wait_seconds, corrected.wait_reason, cleared.wait_reason), (12.0, "corrected", None))
            self.assertEqual(lease.wait_seconds, 0.0, "ORIGINAL_LEASE_UNCHANGED")
            for invalid in (-1.0, float("nan"), float("inf")):
                with self.assertRaisesRegex(ValueError, "invalid_wait_seconds"):
                    host_compute_budget.with_wait(lease, invalid, None)
            host_compute_budget.release(cleared)
            self.assertEqual(self._use()["wait_seconds"], 0.0)


class Live24HourEvaluatorSmoke(unittest.TestCase):
    """Exercise the public command, including its completeness boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.start = "2026-11-17T00:00:00Z"
        self.end = "2026-11-18T00:00:00Z"
        self.sha = "a" * 40
        self.landings: dict[str, Any] = {"complete": True, "window_start": self.start, "window_end": self.end, "landings": [{"production": True, "merge_sha": self.sha, "merged_at": "2026-11-17T01:00:00Z"}]}
        self.deployments: dict[str, Any] = {"complete": True, "window_start": self.start, "window_end": self.end, "deployments": [{"git_sha": self.sha, "serving_at": "2026-11-17T01:10:00Z"}]}

    def _invoke(self, *extra: str) -> subprocess.CompletedProcess[str]:
        landing_path, deployment_path = self.root / "landings.json", self.root / "deployments.json"
        landing_path.write_text(json.dumps(self.landings))
        deployment_path.write_text(json.dumps(self.deployments))
        return subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--live-24h", "--landings-json", str(landing_path), "--deployments-json", str(deployment_path), "--window-start", self.start, *extra], capture_output=True, text=True, check=False, timeout=15)

    def _assert_unverified(self) -> None:
        result = self._invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_UNVERIFIED", result.stdout)
        self.assertNotIn("LIVE_24H_PASS", result.stdout)

    def test_complete_inputs_match_each_merge_to_first_serving(self) -> None:
        self.landings["landings"].append({"production": True, "merge_sha": "b" * 40, "merged_at": "2026-11-17T02:00:00Z"})
        self.landings["landings"].append({"production": False})
        self.deployments["deployments"] = [
            {"git_sha": self.sha, "serving_at": "2026-11-17T02:10:00Z"},
            {"git_sha": self.sha, "serving_at": "2026-11-17T01:10:00Z"},
            {"git_sha": "b" * 40, "serving_at": "2026-11-17T02:15:00Z"},
        ]
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_PASS", result.stdout)
        self.assertIn('"max_seconds": 900.0', result.stdout)
        self.assertIn('"production_landings": 2', result.stdout)

    def test_complete_inputs_over_twenty_minutes_fail(self) -> None:
        self.deployments["deployments"][0]["serving_at"] = "2026-11-17T01:20:01Z"
        result = self._invoke()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_FAIL", result.stdout)
        self.assertIn('"max_seconds": 1201.0', result.stdout)

    def test_exact_twenty_minutes_passes(self) -> None:
        self.deployments["deployments"][0]["serving_at"] = "2026-11-17T01:20:00Z"
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_PASS", result.stdout)
        self.assertIn('"max_seconds": 1200.0', result.stdout)

    def test_incomplete_inputs_are_unverified(self) -> None:
        for packet in (self.landings, self.deployments):
            with self.subTest(packet=next(key for key in ("landings", "deployments") if key in packet)):
                packet["complete"] = False
                self._assert_unverified()
                packet["complete"] = True
        self.deployments["deployments"] = []
        self._assert_unverified()

    def test_partial_window_and_missing_row_fields_are_unverified(self) -> None:
        self.landings["window_end"] = "2026-11-17T23:59:59Z"
        self._assert_unverified()
        self.landings["window_end"] = self.end
        self.deployments["deployments"][0].pop("serving_at")
        self._assert_unverified()
        self.deployments["deployments"][0]["serving_at"] = "2026-11-17T01:10:00Z"
        self.landings["landings"][0].pop("production")
        self._assert_unverified()

    def test_naive_timestamps_and_duplicate_merges_are_unverified(self) -> None:
        self.deployments["deployments"][0]["serving_at"] = "2026-11-17T01:10:00"
        self._assert_unverified()
        self.deployments["deployments"][0]["serving_at"] = "2026-11-17T01:10:00Z"
        self.landings["landings"].append(dict(self.landings["landings"][0]))
        self._assert_unverified()

    def test_every_production_merge_requires_serving_evidence(self) -> None:
        self.landings["landings"].append({"production": True, "merge_sha": "b" * 40, "merged_at": "2026-11-17T02:00:00Z"})
        self._assert_unverified()

    def test_missing_files_and_invalid_json_are_unverified(self) -> None:
        result = self._invoke("--landings-json", str(self.root / "absent.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_UNVERIFIED", result.stdout)
        (self.root / "invalid.json").write_text("{")
        result = self._invoke("--deployments-json", str(self.root / "invalid.json"))
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_UNVERIFIED", result.stdout)

    def test_empty_window_and_missing_options_are_unverified(self) -> None:
        self.landings["landings"] = []
        self._assert_unverified()
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve()), "--live-24h"], capture_output=True, text=True, check=False, timeout=15)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("LIVE_24H_UNVERIFIED", result.stdout)

    def test_window_bounds_and_late_serving_coverage(self) -> None:
        self.landings["landings"][0]["merged_at"] = self.start
        self.landings["landings"].append({"production": True, "merge_sha": "b" * 40, "merged_at": self.end})
        self.deployments["deployments"] = [{"git_sha": self.sha, "serving_at": "2026-11-16T23:59:59Z"}, {"git_sha": self.sha, "serving_at": self.start}]
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"production_landings": 1', result.stdout)
        self.assertIn('"max_seconds": 0.0', result.stdout)
        self.landings["landings"][0]["merged_at"] = "2026-11-17T23:55:00Z"
        self.deployments["deployments"] = [{"git_sha": self.sha, "serving_at": "2026-11-18T00:05:00Z"}]
        self._assert_unverified()
        self.deployments["window_end"] = "2026-11-18T00:20:00Z"
        result = self._invoke()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('"max_seconds": 600.0', result.stdout)


if __name__ == "__main__":
    if "--live-24h" in sys.argv:
        raise SystemExit(live_24h_main(sys.argv[1:]))
    unittest.main()
