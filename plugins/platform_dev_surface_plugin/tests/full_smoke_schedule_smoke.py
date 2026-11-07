#!/usr/bin/env python3
"""Tier U: actual late boot hook, durable worker, busy and filing response loss."""

# ruff: noqa: E402
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_dev_surface_plugin/src", ROOT / "plugins/default_scheduling_plugin/src"):
    sys.path.insert(0, str(source))
from default_scheduling_plugin.plugin import SchedulingPlugin
from full_smoke_schedule_doubles import CapturingScheduler, MemoryScheduler, NativeRegisterFixture, RegisterDouble
from platform_dev_surface_plugin.plugin import PlatformDevSurfacePlugin
from platform_dev_surface_plugin.quality import full_battery
from platform_dev_surface_plugin.quality.full_battery import SYSTEM_STATE, TARGET, FullBatteryProducer, ensure_full_smoke_schedule

from quality_gates.full_smoke_campaign import CampaignStore
from quality_gates.smoke_identity import FrozenTree


class ScheduleReadbackTests(unittest.TestCase):
    def test_real_scheduler_canonical_readback_and_idempotency(self) -> None:
        self.assertEqual(Path(sys.modules[SchedulingPlugin.__module__].__file__).resolve(), ROOT / "plugins/default_scheduling_plugin/src/default_scheduling_plugin/plugin.py")
        scheduler = MemoryScheduler()
        first = ensure_full_smoke_schedule(scheduler)
        self.assertTrue(first["installed"], first)
        again = ensure_full_smoke_schedule(scheduler)
        self.assertTrue(again["installed"], again)
        self.assertEqual([row["schedule_id"] for row in first["schedules"]], [row["schedule_id"] for row in again["schedules"]])
        for tag, _cron, _trigger in full_battery._SCHEDULES:
            rows = scheduler.get_schedules_by_tag(tag)["data"]["schedules"]
            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["action_definitions"][0]["result_processor"])
            self.assertIsNone(rows[0]["action_definitions"][0]["result_processor_kind"])

    def test_helper_origin_and_exact_created_readback(self) -> None:
        self.assertEqual(Path(full_battery.__file__).resolve(), ROOT / "plugins/platform_dev_surface_plugin/src/platform_dev_surface_plugin/quality/full_battery.py")
        scheduler = CapturingScheduler()
        installed = ensure_full_smoke_schedule(scheduler)
        self.assertTrue(installed["installed"])
        self.assertFalse(installed["commissioned"])
        self.assertEqual([row["status"] for row in installed["schedules"]], ["created", "created"])
        self.assertEqual({row["action_definitions"][0]["arguments"]["trigger"] for row in scheduler.rows}, {"schedule", "due_retry"})
        again = ensure_full_smoke_schedule(scheduler)
        self.assertTrue(again["installed"])
        self.assertEqual([row["status"] for row in again["schedules"]], ["already_present", "already_present"])
        self.assertEqual(scheduler.creates, 2)

    def test_created_identity_mismatch_never_installed_and_next_ensure_repairs(self) -> None:
        corruptions = (
            {"cron_expression": "* * * * *"},
            {"action_definitions": [{"process_key": "wrong::target", "arguments": {"trigger": "schedule"}}]},
            {"action_definitions": [{"process_key": TARGET, "arguments": {"trigger": "manual"}}]},
            {"flow_id": "wrong-flow"},
            {"session_id": "wrong-session"},
            {"status": "paused"},
            {"status": "cancelled"},
            {"tags": ["unowned"]},
        )
        for tag, _cron, _trigger in full_battery._SCHEDULES:
            for corruption in corruptions:
                with self.subTest(tag=tag, corruption=corruption):
                    scheduler = CapturingScheduler()
                    scheduler.corrupt_tag = tag
                    scheduler.created_override = corruption
                    result = ensure_full_smoke_schedule(scheduler)
                    self.assertFalse(result["installed"], "returned id alone must not prove installation")
                    self.assertIn("advisory", result)
                    scheduler.created_override = {}
                    self.assertTrue(ensure_full_smoke_schedule(scheduler)["installed"])

    def test_created_duplicates_never_installed_and_repair_preserves_unowned(self) -> None:
        for tag, _cron, _trigger in full_battery._SCHEDULES:
            with self.subTest(tag=tag):
                scheduler = CapturingScheduler()
                scheduler.rows.append({"schedule_id": "unrelated", "tags": ["other"]})
                scheduler.corrupt_tag = tag
                scheduler.duplicate = True
                self.assertFalse(ensure_full_smoke_schedule(scheduler)["installed"])
                scheduler.duplicate = False
                self.assertTrue(ensure_full_smoke_schedule(scheduler)["installed"])
                self.assertEqual(len(scheduler.rows), 3)
                self.assertNotIn("unrelated", scheduler.clears)

    def test_incomplete_created_readback_recovers_without_recreation(self) -> None:
        scheduler = CapturingScheduler()
        scheduler.incomplete_after_create = True
        first = ensure_full_smoke_schedule(scheduler)
        self.assertFalse(first["installed"])
        self.assertIn("advisory", first)
        self.assertEqual(scheduler.creates, 1)
        self.assertEqual(scheduler.clears, [])
        scheduler.incomplete_after_create = False
        scheduler.complete = True
        self.assertTrue(ensure_full_smoke_schedule(scheduler)["installed"])
        self.assertEqual(scheduler.creates, 2)
        self.assertEqual(scheduler.clears, [])

    def test_created_readback_must_match_returned_identity(self) -> None:
        scheduler = CapturingScheduler()
        scheduler.report_wrong_id = True
        self.assertFalse(ensure_full_smoke_schedule(scheduler)["installed"])
        scheduler.report_wrong_id = False
        self.assertTrue(ensure_full_smoke_schedule(scheduler)["installed"])
        self.assertEqual(scheduler.creates, 2)

    def test_incomplete_readback_is_advisory_and_late_hook_recovers(self) -> None:
        for complete in (False, None):
            with self.subTest(complete=complete):
                scheduler = CapturingScheduler()
                scheduler.complete = complete
                plugin = PlatformDevSurfacePlugin()
                plugin._quality = SimpleNamespace(full_battery=SimpleNamespace(active=True))
                plugin.orchestrator_ref = SimpleNamespace(get_service=lambda _name, current=scheduler: current)
                with self.assertLogs(full_battery.__name__, level="WARNING") as logged:
                    plugin.set_action_factory(SimpleNamespace())
                self.assertIn("schedule installation unverified", logged.output[0])
                result = plugin.ensure_full_smoke_schedule()
                self.assertFalse(result["installed"])
                self.assertIn("advisory", result)
                self.assertEqual(scheduler.creates, 0, "unknown absence cannot justify duplicate creation")
                scheduler.complete = True
                plugin.set_action_factory(SimpleNamespace())
                self.assertTrue(plugin.ensure_full_smoke_schedule()["installed"])
                self.assertEqual(scheduler.creates, 2)
                scheduler.complete = complete
                self.assertFalse(plugin.ensure_full_smoke_schedule()["installed"])
                self.assertEqual(scheduler.clears, [], "incomplete knowledge must not clear known or unknown rows")
                scheduler.complete = True
                self.assertTrue(plugin.ensure_full_smoke_schedule()["installed"])
                self.assertEqual(scheduler.creates, 2)


class ScheduleTests(unittest.TestCase):
    def test_actual_late_hook_and_repeated_injection(self) -> None:
        scheduler = CapturingScheduler()
        ready = {"late": False}
        plugin = PlatformDevSurfacePlugin()
        plugin._quality = SimpleNamespace(full_battery=SimpleNamespace(active=True))
        plugin.orchestrator_ref = SimpleNamespace(get_service=lambda _name: scheduler if ready["late"] else None)
        self.assertFalse(plugin.ensure_full_smoke_schedule()["installed"])
        ready["late"] = True
        plugin.set_action_factory(SimpleNamespace())
        plugin.set_action_factory(SimpleNamespace())
        self.assertEqual(scheduler.creates, 2)
        self.assertEqual({row["cron_expression"] for row in scheduler.rows}, {"0 */6 * * *", "*/5 * * * *"})
        for row in scheduler.rows:
            self.assertEqual(row["action_definitions"][0]["process_key"], TARGET)
            self.assertEqual({key: row[key] for key in SYSTEM_STATE}, SYSTEM_STATE)
        scheduler.rows[0]["action_definitions"][0]["process_key"] = "wrong::target"
        plugin.set_action_factory(SimpleNamespace())
        self.assertEqual(scheduler.creates, 3)
        self.assertEqual(len(scheduler.rows), 2)

    def test_launch_failure_releases_slot_and_next_trigger_runs_queued_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            clean = {"result": "passed", "exit_code": 0, "summary": {"total": 1, "passed": 1, "retried_count": 0}}
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=RegisterDouble(), runner=lambda *_: clean)
            request = {"commit": "fixture", "tree": "fixture", "trigger": "schedule", "window": 1}
            immediate = SimpleNamespace(start=producer._background)
            with (
                patch.object(producer, "_request", return_value=request),
                patch("platform_dev_surface_plugin.quality.full_battery.threading.Thread", side_effect=[RuntimeError("fixture launch unavailable"), immediate]),
                patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})),
                patch("platform_dev_surface_plugin.quality.full_battery.battery_busy", return_value=False),
            ):
                queued = producer.trigger()
                self.assertEqual(queued["status"], "queued")
                self.assertFalse(producer._slot.locked())
                self.assertEqual(store.get(queued["campaign_id"])["state"], "queued")
                started = producer.trigger()
            self.assertEqual(started["campaign_id"], queued["campaign_id"])
            self.assertFalse(producer._slot.locked())
            self.assertEqual(store.get(queued["campaign_id"])["state"], "closed")
            self.assertEqual(len(store.get(queued["campaign_id"])["attempts"]), 1)

    def test_due_retry_names_hub_and_elapsed_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(Path(directory)), register=RegisterDouble())
            previous = {"finished_at": datetime.now(UTC).isoformat(), "request": {"commit": "old"}}
            with patch.object(producer, "_last_sample", return_value=previous), patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "tree", {})), patch("platform_dev_surface_plugin.quality.full_battery.git", return_value=b"quality_gates/run_smokes.py\n") as changed:
                self.assertEqual(producer._request("due_retry")["trigger"], "hub")
                changed.return_value = b"docs/ordinary.md\n"
                self.assertIsNone(producer._request("due_retry"))
                previous["finished_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
                self.assertEqual(producer._request("due_retry")["trigger"], "schedule")

    def test_nonclean_complete_sample_still_bounds_retry_cadence(self) -> None:
        for outcome, passed, failed, skipped, retried in (("failed", 1, 1, 0, 0), ("passed", 1, 0, 1, 0), ("passed_with_retries", 2, 0, 0, 1)):
            with self.subTest(outcome=outcome, skipped=skipped), tempfile.TemporaryDirectory() as directory:
                store = CampaignStore(Path(directory))
                producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=RegisterDouble())
                sample = store.enqueue({"commit": "old", "tree": "old", "trigger": "schedule", "window": 1})
                sample.update(state="closed", finished_at=datetime.now(UTC).isoformat(), result={"result": outcome, "exit_code": int(bool(failed)), "registered_count": 2, "summary": {"total": 2, "passed": passed, "failed": failed, "skipped": skipped, "missing": 0, "not_run": 0, "retried_count": retried}})
                sample = store.write(sample["campaign_id"], sample, expected_version=sample["version"], reason="completed nonclean physical fixture")
                self.assertIsNone(producer._last_success())
                with patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "tree", {})), patch("platform_dev_surface_plugin.quality.full_battery.git", return_value=b"docs/ordinary.md\n") as changed:
                    self.assertIsNone(producer._request("due_retry"))
                    changed.return_value = b"quality_gates/run_smokes.py\n"
                    self.assertEqual(producer._request("due_retry")["trigger"], "hub")
                    sample["finished_at"] = (datetime.now(UTC) - timedelta(hours=7)).isoformat()
                    store.write(sample["campaign_id"], sample, expected_version=sample["version"], reason="older physical fixture")
                    changed.return_value = b"docs/ordinary.md\n"
                    self.assertEqual(producer._request("due_retry")["trigger"], "schedule")

    def test_incomplete_sample_does_not_suppress_bootstrap(self) -> None:
        for outcome, total, not_run, timed_out in (("interrupted", 2, 0, False), ("failed", 2, 1, False), ("failed", 2, 0, True), ("passed", 0, 0, False)):
            with self.subTest(outcome=outcome, not_run=not_run, timed_out=timed_out), tempfile.TemporaryDirectory() as directory:
                store = CampaignStore(Path(directory))
                producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=RegisterDouble())
                sample = store.enqueue({"commit": "old", "tree": "old", "trigger": "schedule", "window": 1})
                sample.update(state="closed", finished_at=datetime.now(UTC).isoformat(), result={"result": outcome, "exit_code": 1, "registered_count": total, "timed_out": timed_out, "summary": {"total": total, "passed": 0, "failed": total - not_run, "skipped": 0, "missing": 0, "not_run": not_run}})
                store.write(sample["campaign_id"], sample, expected_version=sample["version"], reason="incomplete physical fixture")
                with patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "tree", {})):
                    self.assertEqual(producer._request("due_retry")["trigger"], "due_retry")

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

                register = RegisterDouble()
                register.available = not isinstance(outcome, RuntimeError)
                producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=register, runner=runner)
                with (
                    patch("platform_dev_surface_plugin.quality.full_battery.datetime", wraps=datetime) as clock,
                    patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "new-tree", {})),
                    patch("platform_dev_surface_plugin.quality.full_battery.git", return_value=b"quality_gates/run_smokes.py\n") as changed,
                    patch("platform_dev_surface_plugin.quality.full_battery.battery_busy", return_value=False),
                    patch("platform_dev_surface_plugin.quality.full_battery.threading.Thread") as thread,
                ):
                    clock.now.return_value = now
                    thread.return_value = SimpleNamespace(start=producer._background)
                    first = producer.trigger("hub")
                    terminal = store.get(first["campaign_id"])
                    self.assertEqual(terminal["state"], "closed")
                    self.assertEqual(len(terminal["attempts"]), 1)
                    restarted = FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(Path(directory)), register=register, runner=runner)
                    thread.return_value = SimpleNamespace(start=restarted._background)
                    retried = restarted.trigger("due_retry")
                    self.assertEqual(retried["campaign_id"], first["campaign_id"])
                    self.assertEqual(runs, [1, 2])
                    final = store.get(first["campaign_id"])
                    self.assertEqual(final["attempts"][0], terminal["attempts"][0])
                    self.assertEqual(final["attempts"][1]["previous_attempt"], 1)
                    self.assertEqual(final["state"], "closed")
                    history = store.journal.history(first["campaign_id"])
                    self.assertEqual(history[terminal["version"] - 1]["payload"]["result"], terminal["result"])
                    changed.return_value = b""
                    restarted.trigger("due_retry")
                    restarted.trigger("hub")
                    self.assertEqual(runs, [1, 2])

    def test_native_shapes_paging_idempotence_and_guards(self) -> None:
        register = NativeRegisterFixture()
        self.assertTrue(register.controller_busy()["busy"])
        problem = register.reconcile("campaign-marker")
        self.assertEqual(problem, {"issue_id": "iss_fixture", "problem_id": "prb_fixture"})
        register.attribute_problem(problem, "campaign-marker", "campaign-marker detail", ["chg_fixture"])
        register.attribute_problem(problem, "campaign-marker", "campaign-marker detail", ["chg_fixture"])
        events = [args for args in register.calls if args[:3] == ("db", "change", "event")]
        links = [args for args in register.calls if args[:4] == ("db", "problem", "relation", "add")]
        self.assertEqual(len(events), 1)
        self.assertEqual(len(links), 1)
        self.assertEqual(events[0][events[0].index("--expected-sequence") + 1], "2")
        self.assertEqual(links[0][links[0].index("--expected-version") + 1], "3")
        self.assertEqual(links[0][links[0].index("--kind") + 1], "investigated_by")
        self.assertFalse(any(args[:2] == ("db", "group") for args in register.calls))

    def test_typed_parent_filing_flags_and_reconciliation(self) -> None:
        register = NativeRegisterFixture()
        register.issue_exists = False
        self.assertEqual(register.file_problem("campaign-marker", "campaign-marker detail"), {"issue_id": "iss_fixture", "problem_id": "prb_fixture"})
        self.assertEqual(register.file_problem("campaign-marker", "campaign-marker detail"), {"issue_id": "iss_fixture", "problem_id": "prb_fixture"})
        filings = [args for args in register.calls if args[:1] == ("filed",)]
        self.assertEqual(len(filings), 1, "existing marker must not file a second issue")
        for flag in ("--new-parent-kind", "--new-parent-title", "--new-parent-scope", "--new-parent-problem-statement", "--new-parent-cause-text"):
            self.assertIn(flag, filings[0])
        self.assertFalse(any(flag.startswith("--new-group-") for flag in filings[0]))
        self.assertFalse(any(args[:2] == ("db", "group") for args in register.calls))

    def test_actual_full_worker_argv_is_complete_register_and_cache_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "quality_gates").mkdir()
            (root / "quality_gates/gate_smokes.txt").write_text("suite/one_smoke.py\nsuite/two_smoke.py\n")
            store = CampaignStore(root / "campaigns")
            producer = FullBatteryProducer(root, "fixture-solet", store=store, register=RegisterDouble())
            campaign = {"campaign_id": "a" * 64, "request": {"commit": "b" * 40}, "attempts": [{"number": 1}], "attribution": {}}
            calls: list[list[str]] = []

            def physical(argv: list[str], **kwargs: Any) -> Any:
                self.assertIsNone(kwargs["timeout"], "scheduled battery must have no outer kill deadline")
                calls.append(argv)
                return SimpleNamespace(exit_code=124, timed_out=True, output="TIMEOUT", truncated=False, output_chars_total=7)

            with (
                patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("b" * 40, "c" * 40, {})),
                patch("platform_dev_surface_plugin.quality.full_battery.verify_worktree"),
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
            producer = FullBatteryProducer(root, "fixture-solet", store=store, register=RegisterDouble())
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
                patch("platform_dev_surface_plugin.quality.full_battery.prepare_full_proofs", return_value={"fixture": "prepared"}),
                patch("platform_dev_surface_plugin.quality.full_battery.finish_full_proofs", return_value=[]) as finish,
                patch("platform_dev_surface_plugin.quality.full_battery.run_bounded", side_effect=physical),
            ):
                result = json.loads(json.dumps(producer._run_battery(campaign, store.root)))
            self.assertEqual((result["result"], result["harness_defects"], finish.call_args.args[2]["harness_defects"]), ("passed", ["suite/one_smoke.py"], ["suite/one_smoke.py"]))

    def test_busy_queue_then_real_worker_failure_outbox_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            register = RegisterDouble()
            register.busy = True
            register.lose_response = True
            runs: list[str] = []

            def runner(campaign: dict[str, Any], _root: Path) -> dict[str, Any]:
                runs.append(campaign["campaign_id"])
                return {"result": "failed", "exit_code": 1, "original_red": "assertion failed"}

            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=register, runner=runner)
            campaign = store.enqueue({"commit": "fixture", "tree": "fixture", "trigger": "hub", "window": 1})
            with patch("platform_dev_surface_plugin.quality.full_battery.battery_busy", return_value=False), patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})):
                self.assertEqual(producer.run_due()[0]["state"], "queued")
                self.assertFalse(runs)
                register.busy = False
                failed = producer.run_due()[0]
                self.assertEqual(failed["filing"]["status"], "uncertain")
                self.assertEqual(register.filings, 1)
                producer.run_due()
            final = store.get(campaign["campaign_id"])
            self.assertEqual(final["filing"]["status"], "confirmed")
            self.assertEqual(register.filings, 1)
            self.assertEqual(final["result"]["original_red"], "assertion failed")
            self.assertTrue(final["filing"]["evidence"]["change_evidence"])

    def test_queued_old_master_is_superseded_without_losing_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            register = RegisterDouble()
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=register, runner=lambda *_: {"result": "failed", "exit_code": 1})
            original = store.enqueue({"commit": "old", "tree": "old", "trigger": "hub", "window": 1})
            with patch("platform_dev_surface_plugin.quality.full_battery.battery_busy", return_value=False), patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("new", "new-tree", {})):
                successor = producer.run_due()[0]
            retained = store.get(original["campaign_id"])
            self.assertEqual(retained["result"]["result"], "superseded")
            self.assertEqual(retained["request"]["commit"], "old")
            self.assertEqual(retained["result"]["successor_campaign"], successor["campaign_id"])
            self.assertEqual(successor["request"]["commit"], "new")
            self.assertEqual(successor["request"]["supersedes_campaign"], original["campaign_id"])
            self.assertEqual(successor["filing"]["status"], "confirmed")

    def test_interrupted_attempt_is_linked_and_pending_unavailable_register(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = CampaignStore(Path(directory))
            register = RegisterDouble()
            register.available = False
            producer = FullBatteryProducer(ROOT, "fixture-solet", store=store, register=register, runner=lambda *_: {"result": "failed", "exit_code": 1})
            row = store.enqueue({"commit": "fixture", "tree": "fixture", "trigger": "schedule"})
            row.update(state="running", attempts=[{"number": 1, "started_at": "prior"}])
            store.write(row["campaign_id"], row, expected_version=row["version"], reason="fixture prior interrupted worker")
            with patch("platform_dev_surface_plugin.quality.full_battery.battery_busy", return_value=False), patch("platform_dev_surface_plugin.quality.full_battery.read_tree", return_value=FrozenTree("fixture", "fixture", {})):
                result = producer.run_due()[0]
                self.assertEqual(result["attempts"][0]["result"]["result"], "interrupted")
                self.assertEqual(result["attempts"][1]["previous_attempt"], 1)
                self.assertEqual(result["filing"]["status"], "uncertain")
                register.available = True
                producer.run_due()
            self.assertEqual(store.get(row["campaign_id"])["filing"]["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
