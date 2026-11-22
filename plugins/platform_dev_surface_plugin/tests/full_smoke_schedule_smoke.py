#!/usr/bin/env python3
"""Tier U: actual late boot hook, durable worker, exclusion and campaign history."""

# ruff: noqa: E402
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from inspect import signature
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_dev_surface_plugin/src", ROOT / "plugins/default_scheduling_plugin/src"):
    sys.path.insert(0, str(source))
from ananta.interfaces.quality_service_interface import QualityServiceInterface
from ananta.services.quality_service.interfaces.public import QualityServicePublicAPI
from full_smoke_schedule_doubles import CapturingScheduler, MemoryScheduler
from platform_dev_surface_plugin.plugin import PlatformDevSurfacePlugin
from platform_dev_surface_plugin.quality import full_battery
from platform_dev_surface_plugin.quality.full_battery import SYSTEM_STATE, FullBatteryProducer

from quality_gates.full_smoke_campaign import CampaignStore
from quality_gates.smoke_identity import FrozenTree


def _present[T](value: T | None) -> T:
    """Narrow an optional read to its value; absence fails the test at the read."""
    assert value is not None
    return value


TAGS = ("quality-full-smokes-six-hour", "quality-full-smokes-due-retry")


def _legacy_schedules(scheduler: MemoryScheduler | CapturingScheduler) -> None:
    for tag, cron, trigger in ((TAGS[0], "0 */6 * * *", "schedule"), (TAGS[1], "*/5 * * * *", "due_retry")):
        scheduler.create_cron_schedule(
            cron_expression=cron,
            action_definitions=[{"process_key": "service_interface::quality_service::trigger_full_smoke_battery", "arguments": {"trigger": trigger}, "result_processor": None, "result_processor_kind": None}],
            label=tag,
            tags=[tag],
            state=SYSTEM_STATE,
        )


class RetireTests(unittest.TestCase):
    def test_a_real_scheduler_retires_owned_tags_only(self) -> None:
        scheduler = MemoryScheduler()
        _legacy_schedules(scheduler)
        scheduler.create_cron_schedule(cron_expression="0 0 * * *", action_definitions=[{"process_key": "service_interface::quality_service::trigger_full_smoke_battery", "arguments": {"trigger": "manual"}, "result_processor": None, "result_processor_kind": None}], label="other", tags=["other"], state=SYSTEM_STATE)
        self.assertEqual(full_battery.retire_full_smoke_schedules(scheduler), {"retired": 2, "uncertain": []})
        self.assertEqual([len(scheduler.get_schedules_by_tag(tag)["data"]["schedules"]) for tag in TAGS], [0, 0])
        self.assertEqual(len(scheduler.get_schedules_by_tag("other")["data"]["schedules"]), 1)

    def test_b_late_hook_clears_without_creation_twice(self) -> None:
        scheduler = CapturingScheduler()
        _legacy_schedules(scheduler)
        scheduler.creates = 0
        plugin = PlatformDevSurfacePlugin()
        plugin._quality = SimpleNamespace(full_battery=SimpleNamespace(active=True))
        plugin.orchestrator_ref = SimpleNamespace(get_service=lambda _name: scheduler)
        plugin.set_action_factory(cast(Any, SimpleNamespace()))
        self.assertEqual((scheduler.rows, scheduler.creates), ([], 0))
        plugin.set_action_factory(cast(Any, SimpleNamespace()))
        self.assertEqual((scheduler.rows, scheduler.creates), ([], 0))

    def test_c_incomplete_read_clears_visible_rows_and_names_uncertainty(self) -> None:
        scheduler = CapturingScheduler()
        _legacy_schedules(scheduler)
        scheduler.creates = 0
        scheduler.complete = False
        with self.assertLogs(full_battery.__name__, level="WARNING"):
            result = full_battery.retire_full_smoke_schedules(scheduler)
        self.assertEqual(result, {"retired": 2, "uncertain": list(TAGS)})
        self.assertEqual((scheduler.rows, scheduler.creates), ([], 0))

    def test_d_missing_scheduler_is_advisory(self) -> None:
        self.assertEqual(full_battery.retire_full_smoke_schedules(None), {"retired": 0, "advisory": "scheduler unavailable before late action-factory injection"})

    def test_e_ensure_surface_is_removed(self) -> None:
        self.assertFalse(hasattr(full_battery, "ensure_full_smoke_schedule"))
        self.assertFalse(hasattr(PlatformDevSurfacePlugin, "ensure_full_smoke_schedule"))

    def test_f_trigger_defaults_are_manual(self) -> None:
        for owner, method in ((FullBatteryProducer, "trigger"), (QualityServicePublicAPI, "trigger_full_smoke_battery"), (QualityServiceInterface, "trigger_full_smoke_battery"), (PlatformDevSurfacePlugin, "trigger_full_smoke_battery")):
            with self.subTest(owner=owner):
                self.assertEqual(signature(getattr(owner, method)).parameters["trigger"].default, "manual")

    def test_g_interface_version_is_two(self) -> None:
        self.assertEqual(QualityServiceInterface.INTERFACE_VERSION, "2.0.0")

    def test_h_only_deploy_and_manual_triggers_remain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(Path(directory)))
            producer.active = False
            for trigger in ("schedule", "due_retry", "hub"):
                with self.subTest(trigger=trigger), self.assertRaises(ValueError):
                    producer.trigger(trigger)
            for trigger in ("deploy", "manual"):
                self.assertEqual(producer.trigger(trigger)["status"], "inactive")
        self.assertFalse(hasattr(FullBatteryProducer, "_clock_due"))
        self.assertFalse(hasattr(FullBatteryProducer, "_last_sample"))


class ScheduleTests(unittest.TestCase):
    def test_full_battery_producer_has_no_register_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(Path(directory)))
        self.assertNotIn("register", signature(FullBatteryProducer).parameters)
        self.assertFalse(hasattr(producer, "register"))

    def test_launch_failure_releases_slot_and_next_trigger_runs_queued_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            clean = {"result": "passed", "exit_code": 0, "summary": {"total": 1, "passed": 1, "retried_count": 0}}
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, runner=lambda *_: clean)
            request = {"commit": "fixture", "tree": "fixture", "trigger": "schedule", "window": 1}
            immediate = SimpleNamespace(start=producer._background)
            with (
                patch.object(producer, "_request", return_value=request),
                patch("platform_dev_surface_plugin.quality.full_battery.threading.Thread", side_effect=[RuntimeError("fixture launch unavailable"), immediate]),
                patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})),
            ):
                queued = producer.trigger()
                self.assertEqual(queued["status"], "queued")
                self.assertFalse(producer._slot.locked())
                self.assertEqual(_present(store.get(queued["campaign_id"]))["state"], "queued")
                started = producer.trigger()
            self.assertEqual(started["campaign_id"], queued["campaign_id"])
            self.assertFalse(producer._slot.locked())
            self.assertEqual(_present(store.get(queued["campaign_id"]))["state"], "closed")
            self.assertEqual(len(_present(store.get(queued["campaign_id"]))["attempts"]), 1)

    def test_due_retry_restarts_closed_incomplete_hub_campaign(self) -> None:
        complete = {"result": "passed", "exit_code": 0, "registered_count": 1, "summary": {"total": 1, "passed": 1}}
        incomplete = (
            RuntimeError("runner interrupted"),
            {"result": "interrupted", "exit_code": None},
            {"result": "failed", "exit_code": 1, "registered_count": 1, "summary": {"total": 1, "not_run": 1}},
            {**complete, "timed_out": True},
        )
        for outcome in incomplete:
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as directory:
                store = CampaignStore(Path(directory))
                now = datetime.now(UTC)
                baseline = store.enqueue({"commit": "old", "tree": "old", "trigger": "schedule", "window": 1})
                baseline.update(state="closed", finished_at=(now - timedelta(minutes=10)).isoformat(), result=complete)
                store.write(baseline["campaign_id"], baseline, expected_version=baseline["version"], reason="earlier complete sample")
                runs: list[int] = []

                def runner(campaign: dict[str, Any], _root: Path, *, observed: list[int] = runs, first: Exception | dict[str, Any] = outcome) -> dict[str, Any]:
                    observed.append(campaign["attempts"][-1]["number"])
                    if len(observed) == 1:
                        if isinstance(first, Exception):
                            raise first
                        return first
                    return complete

                producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, runner=runner)
                with (
                    patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "new-tree", {})),
                    patch("platform_dev_surface_plugin.quality.full_battery.threading.Thread") as thread,
                ):
                    thread.return_value = SimpleNamespace(start=producer._background)
                    first = producer.trigger("deploy")
                    terminal = _present(store.get(first["campaign_id"]))
                    self.assertEqual(terminal["state"], "closed")
                    self.assertEqual(len(terminal["attempts"]), 1)
                    restarted = FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(Path(directory)), runner=runner)
                    thread.return_value = SimpleNamespace(start=restarted._background)
                    retried = restarted.trigger("manual")
                    self.assertEqual(retried["campaign_id"], first["campaign_id"])
                    self.assertEqual(runs, [1, 2])
                    final = _present(store.get(first["campaign_id"]))
                    self.assertEqual(final["attempts"][0], terminal["attempts"][0])
                    self.assertEqual(final["attempts"][1]["previous_attempt"], 1)
                    self.assertEqual(final["state"], "closed")
                    history = store.journal.history(first["campaign_id"])
                    self.assertEqual(history[terminal["version"] - 1]["payload"]["result"], terminal["result"])
                    restarted.trigger("manual")
                    self.assertEqual(restarted.trigger("deploy")["status"], "have_result")
                    self.assertEqual(runs, [1, 2])

    def test_actual_full_worker_argv_is_complete_register_and_cache_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "quality_gates").mkdir()
            (root / "quality_gates/gate_smokes.txt").write_text("suite/one_smoke.py\nsuite/two_smoke.py\n")
            store = CampaignStore(root / "campaigns")
            producer = FullBatteryProducer(root, "fixture-solet", store=store)
            campaign = {"campaign_id": "a" * 64, "request": {"commit": "b" * 40}, "attempts": [{"number": 1}], "attribution": {}}
            calls: list[list[str]] = []

            def physical(argv: list[str], **kwargs: Any) -> Any:
                self.assertIsNone(kwargs["timeout"], "scheduled battery must have no outer kill deadline")
                calls.append(argv)
                return SimpleNamespace(exit_code=124, timed_out=True, output="TIMEOUT", truncated=False, output_chars_total=7)

            with (
                patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("b" * 40, "c" * 40, {})),
                patch("platform_dev_surface_plugin.quality.full_battery.verify_worktree"),
                patch("platform_dev_surface_plugin.quality.full_battery.frozen_export", return_value=unittest.mock.MagicMock(__enter__=lambda _self: root)),
                patch("platform_dev_surface_plugin.quality.full_battery.prepare_full_proofs", return_value={"fixture": "prepared"}) as begin,
                patch("platform_dev_surface_plugin.quality.full_battery.finish_full_proofs", return_value=[]) as finish,
                patch("platform_dev_surface_plugin.quality.full_battery.run_bounded", side_effect=physical),
            ):
                result = json.loads(json.dumps(producer._run_battery(campaign, store.root)))
            self.assertEqual(calls[0][calls[0].index("--register") + 1], str(root / "quality_gates/gate_smokes.txt"))
            self.assertEqual(result["registered_count"], 2)
            self.assertTrue(result["cache_ignored"])
            self.assertTrue(result["timed_out"])
            self.assertEqual(result["runner_exit_code"], 124)
            self.assertEqual(result["result"], "interrupted")
            self.assertIn("no outer subprocess deadline", result["budget_advisory"])
            begin.assert_called_once()
            finish.assert_called_once()
            # No results file was published: the row says its harness defects are unknown.
            self.assertEqual(finish.call_args.args[2], {"result": "interrupted", "exit_code": 124, "harness_defects": None})
            self.assertIn("harness_defects", result)
            self.assertIsNone(result["harness_defects"])
            self.assertEqual(result["results_sha256"], full_battery.digest(finish.call_args.args[2]))
            self.assertEqual(result["proofs"], [])

    def test_campaign_row_carries_the_runner_harness_defects(self) -> None:
        from quality_gates import run_smokes
        from quality_gates.smoke_proof import campaign_results

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "quality_gates").mkdir()
            (root / "quality_gates/gate_smokes.txt").write_text("suite/one_smoke.py\nsuite/two_smoke.py\n")
            store = CampaignStore(root / "campaigns")
            # The worker's exclusion has already created the store root it writes into.
            store.root.mkdir(parents=True)
            producer = FullBatteryProducer(root, "fixture-solet", store=store)
            campaign = {"campaign_id": "a" * 64, "request": {"commit": "b" * 40}, "attempts": [{"number": 1}], "attribution": {}}

            def physical(argv: list[str], **_kwargs: Any) -> Any:
                # The runner's own campaign writer, as the real child publishes it.
                entries = ["suite/one_smoke.py", "suite/two_smoke.py"]
                campaign_results(Path(argv[argv.index("--results-output") + 1]), run_smokes._initial_smoke_results(entries), "passed", 0, run_smokes, harness=entries[:1])
                output = f"harness: ({entries[0]}) ADVISORY"
                return SimpleNamespace(exit_code=0, timed_out=False, output=output, truncated=False, output_chars_total=len(output))

            with (
                patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("b" * 40, "c" * 40, {})),
                patch("platform_dev_surface_plugin.quality.full_battery.verify_worktree"),
                patch("platform_dev_surface_plugin.quality.full_battery.frozen_export", return_value=unittest.mock.MagicMock(__enter__=lambda _self: root)),
                patch("platform_dev_surface_plugin.quality.full_battery.prepare_full_proofs", return_value={"fixture": "prepared"}),
                patch("platform_dev_surface_plugin.quality.full_battery.finish_full_proofs", return_value=[]) as finish,
                patch("platform_dev_surface_plugin.quality.full_battery.run_bounded", side_effect=physical),
            ):
                result = json.loads(json.dumps(producer._run_battery(campaign, store.root)))
            self.assertEqual((result["result"], result["harness_defects"], finish.call_args.args[2]["harness_defects"]), ("passed", ["suite/one_smoke.py"], ["suite/one_smoke.py"]))

    def test_failed_battery_is_recorded_only_in_its_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            failure = {"result": "failed", "exit_code": 1, "original_red": "assertion failed"}
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, runner=lambda *_: failure)
            campaign = store.enqueue({"commit": "fixture", "tree": "fixture", "trigger": "hub", "window": 1})
            with patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})):
                closed = producer.run_due()[0]
            self.assertEqual(closed["state"], "closed")
            self.assertEqual(closed["result"]["original_red"], "assertion failed")
            self.assertIn("list_full_smoke_campaigns", closed["result"]["advisory"])
            self.assertNotIn("filing", closed)
            self.assertNotIn("filing", _present(store.get(campaign["campaign_id"])))
            self.assertFalse(hasattr(producer, "register"))

    def test_failed_battery_completes_with_empty_home_and_no_project_environment(self) -> None:
        prefix = "_".join(("PROJECT", "SOLET")) + "_"
        environment = {key: value for key, value in os.environ.items() if not key.startswith(prefix)}
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            store = CampaignStore(Path(directory))
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, runner=lambda *_: {"result": "failed", "exit_code": 1})
            immediate = SimpleNamespace(start=producer._background)
            with (
                patch.dict(os.environ, {**environment, "HOME": home}, clear=True),
                patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})),
                patch("platform_dev_surface_plugin.quality.full_battery.threading.Thread", return_value=immediate),
            ):
                started = producer.trigger("manual")
            closed = _present(store.get(started["campaign_id"]))
            self.assertEqual(started["status"], "started")
            self.assertEqual(closed["state"], "closed")
            self.assertEqual(closed["result"]["result"], "failed")
            self.assertIn("list_full_smoke_campaigns", closed["result"]["advisory"])
            self.assertNotIn("filing", closed)

    def test_queued_old_master_is_superseded_without_losing_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, runner=lambda *_: {"result": "failed", "exit_code": 1})
            original = store.enqueue({"commit": "old", "tree": "old", "manifest_sha256": "old"})
            with patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "new-tree", {})):
                successor = producer.run_due()[0]
            retained = _present(store.get(original["campaign_id"]))
            self.assertEqual(retained["result"]["result"], "superseded")
            self.assertEqual(retained["request"]["commit"], "old")
            self.assertEqual(retained["result"]["successor_campaign"], successor["campaign_id"])
            self.assertEqual(successor["request"]["commit"], "new")
            self.assertNotIn("supersedes_campaign", successor["request"])
            self.assertNotIn("filing", successor)

    def test_interrupted_attempt_is_linked_before_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, runner=lambda *_: {"result": "failed", "exit_code": 1})
            row = store.enqueue({"commit": "fixture", "tree": "fixture", "trigger": "schedule"})
            row.update(state="running", attempts=[{"number": 1, "started_at": "prior"}])
            store.write(row["campaign_id"], row, expected_version=row["version"], reason="fixture prior interrupted worker")
            with patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})):
                result = producer.run_due()[0]
                self.assertEqual(result["attempts"][0]["result"]["result"], "interrupted")
                self.assertEqual(result["attempts"][1]["previous_attempt"], 1)
                self.assertNotIn("filing", result)


T1, T2, T3 = "2026-01-01T00:00:00+00:00", "2026-01-01T01:00:00+00:00", "2026-01-01T02:00:00+00:00"
CLEAN_SUMMARY = {"total": 1, "passed": 1, "retried_count": 0}


def _closed(store: CampaignStore, request: dict[str, Any], finished_at: str, result: dict[str, Any], *, filing: dict[str, Any] | None) -> dict[str, Any]:
    """Close one campaign record; a filing mapping models a historical record."""
    campaign = store.enqueue(request)
    campaign.update(state="closed", finished_at=finished_at, result=result)
    if filing is None:
        campaign.pop("filing", None)
    else:
        campaign["filing"] = filing
    return store.write(campaign["campaign_id"], campaign, expected_version=campaign["version"], reason="closed fixture campaign")


class ListCampaignsTests(unittest.TestCase):
    """The read-only list of closed full-battery campaign records."""

    def fixture(self, directory: str) -> tuple[FullBatteryProducer, CampaignStore, list[dict[str, Any]]]:
        store = CampaignStore(Path(directory))
        producer = FullBatteryProducer(ROOT, "fixture-solet", store=store)
        clean = _closed(store, {"commit": "c1", "tree": "t1", "trigger": "schedule", "window": 1}, T1, {"result": "passed", "exit_code": 0, "summary": CLEAN_SUMMARY, "results_sha256": "sha-one"}, filing={"status": "not_needed"})
        failed = _closed(store, {"commit": "c2", "tree": "t2", "trigger": "hub", "window": 2}, T2, {"result": "failed", "exit_code": 1, "summary": {"total": 2, "passed": 1, "failed": 1}, "results_sha256": "sha-two"}, filing={"status": "confirmed", "marker": "recorded-by-the-outbox"})
        superseded = _closed(store, {"commit": "c3", "tree": "t3", "trigger": "manual", "window": 3}, T3, {"result": "superseded", "successor_campaign": "next", "reason": "master advanced while work was queued"}, filing=None)
        store.enqueue({"commit": "queued", "tree": "queued", "trigger": "schedule", "window": 4})
        running = store.enqueue({"commit": "running", "tree": "running", "trigger": "schedule", "window": 5})
        running.update(state="running", attempts=[{"number": 1, "started_at": T3}])
        store.write(running["campaign_id"], running, expected_version=running["version"], reason="running fixture campaign")
        requeued = _closed(store, {"commit": "requeued", "tree": "requeued", "trigger": "due_retry", "window": 6}, T3, {"result": "interrupted", "exit_code": None}, filing={"status": "pending"})
        requeued.update(state="queued")  # a closed campaign put back to work keeps its old finished_at
        store.write(requeued["campaign_id"], requeued, expected_version=requeued["version"], reason="requeued fixture campaign")
        return producer, store, [clean, failed, superseded]

    def test_only_closed_campaigns_come_back_oldest_first_with_every_contract_field(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer, _store, (clean, failed, superseded) = self.fixture(directory)
            listed = producer.list_campaigns()
            self.assertEqual(set(listed), {"campaigns", "truncated", "next_finished_after"})
            self.assertFalse(listed["truncated"])
            self.assertIsNone(listed["next_finished_after"])
            rows = listed["campaigns"]
            self.assertEqual([row["campaign_id"] for row in rows], [clean["campaign_id"], failed["campaign_id"], superseded["campaign_id"]])
            self.assertEqual(
                rows[0],
                {
                    "campaign_id": clean["campaign_id"],
                    "marker": f"smoke-campaign-{clean['campaign_id']}",
                    "request": {"commit": "c1", "tree": "t1"},
                    "finished_at": T1,
                    "outcome": "passed",
                    "clean": True,
                    "superseded": False,
                    "summary": CLEAN_SUMMARY,
                    "results_sha256": "sha-one",
                    "previous_clean_finished_at": None,
                    "filing_status": "not_needed",
                },
            )
            self.assertEqual([row["clean"] for row in rows], [True, False, False])
            self.assertEqual([row["superseded"] for row in rows], [False, False, True])
            self.assertEqual([row["outcome"] for row in rows], ["passed", "failed", "superseded"])
            self.assertEqual([row["previous_clean_finished_at"] for row in rows], [None, T1, T1])
            self.assertEqual([row["filing_status"] for row in rows], ["not_needed", "confirmed", None])
            self.assertIsNone(rows[2]["summary"])
            self.assertIsNone(rows[2]["results_sha256"])
            self.assertEqual(rows[1]["marker"], f"smoke-campaign-{failed['campaign_id']}")

    def test_limit_truncates_with_a_cursor_that_is_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer, _store, (clean, failed, superseded) = self.fixture(directory)
            first = producer.list_campaigns(limit=2)
            self.assertEqual([row["campaign_id"] for row in first["campaigns"]], [clean["campaign_id"], failed["campaign_id"]])
            self.assertTrue(first["truncated"])
            self.assertEqual(first["next_finished_after"], first["campaigns"][1]["finished_at"])
            rest = producer.list_campaigns(finished_after=first["next_finished_after"], limit=2)
            self.assertEqual([row["campaign_id"] for row in rest["campaigns"]], [superseded["campaign_id"]])
            self.assertFalse(rest["truncated"])
            self.assertIsNone(rest["next_finished_after"])
            self.assertEqual(rest["campaigns"][0]["previous_clean_finished_at"], T1, "the previous clean one is read from the whole record set, not only this page")
            self.assertEqual(producer.list_campaigns(finished_after=T3)["campaigns"], [])
            exact = producer.list_campaigns(limit=3)
            self.assertFalse(exact["truncated"], "a page that holds every closed campaign is not truncated")

    def test_bad_limit_or_cursor_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer, _store, _rows = self.fixture(directory)
            for limit in (0, 101, -1, True, "x", 2.5):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    producer.list_campaigns(limit=limit)
            for cursor in ("not-a-timestamp", "2026-01-01T00:00:00", "", 5):
                with self.subTest(finished_after=cursor), self.assertRaises(ValueError):
                    producer.list_campaigns(finished_after=cursor)
            self.assertEqual(len(producer.list_campaigns(limit=100)["campaigns"]), 3)
            self.assertEqual(len(producer.list_campaigns(limit=1)["campaigns"]), 1)

    def test_the_read_writes_nothing_and_asks_no_register(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer, store, _rows = self.fixture(directory)
            before = {path.name: path.read_bytes() for path in sorted(Path(directory).rglob("*")) if path.is_file()}
            self.assertFalse(hasattr(producer, "register"))
            producer.list_campaigns()
            producer.list_campaigns(finished_after=T1, limit=1)
            after = {path.name: path.read_bytes() for path in sorted(Path(directory).rglob("*")) if path.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(len(store.all()), 6)


if __name__ == "__main__":
    unittest.main()
