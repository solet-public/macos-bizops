#!/usr/bin/env python3
"""Hermetic real checker/registry/scheduler-route proof; no live services or PG."""

from __future__ import annotations

import copy
import os
import sys
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for entry in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_health_plugin/src", ROOT / "plugins/default_scheduling_plugin/src", Path(__file__).resolve().parent):
    sys.path.insert(0, str(entry))

import host_disk_guard_smoke as host  # noqa: E402
import platform_health_plugin.main_seat_heartbeat as heartbeat  # noqa: E402
import platform_health_plugin.plugin as plugin_module  # noqa: E402
from default_scheduling_plugin.factories.schedule_factory import ScheduleFactory  # noqa: E402
from default_scheduling_plugin.validation import validate_cron_action_def  # noqa: E402


def completed(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "error": None}


class Memory:
    def __init__(self) -> None:
        self.kv: dict[str, Any] = {}
        self.messages: list[dict[str, Any]] = []

    def get_key_value(self, namespace: str, key: str) -> dict[str, Any]:  # noqa: ARG002
        return completed({"found": key in self.kv, "value": copy.deepcopy(self.kv.get(key))})

    def set_key_value(self, namespace: str, key: str, value: Any) -> dict[str, Any]:  # noqa: ARG002
        self.kv[key] = copy.deepcopy(value)
        return completed({})

    def query_ordered(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return completed({"records": copy.deepcopy(self.messages)})


class Messaging:
    def __init__(self, memory: Memory) -> None:
        self.memory = memory
        self.calls: list[dict[str, Any]] = []
        self.sessions: list[dict[str, Any]] = []
        self.uncertain = False

    def fleet_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return completed({"sessions": self.sessions, "sessions_truncated": 0, "owed_messages_truncated": 0, "legacy_direct_delivery_unknown": True})

    def peer_list(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return completed({"agent_ids": ["codex"], "instances": {"codex": []}})

    def peer_send_by_name(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(params)
        assert self.memory.kv[heartbeat.MARKER_KEY]["send_intent"]["outcome"] == "intent", "intent precedes effect"
        message_id = f"arm-fixture-{len(self.calls)}"
        self.memory.messages.append({"id": message_id, "message_id": message_id, "content": [{"text": params["content"]}], "created_at": "2026-11-10T00:00:00", "delivered": True, "consumed": False})
        if self.uncertain:
            raise OSError("lost response after persistence")
        return completed({"message_id": message_id, "delivery": "queued_wake"})


class Register:
    def __init__(self) -> None:
        self.units: list[dict[str, Any]] = [{"unit_id": "unt-test", "state": "working", "state_observed_at": "state-1"}]
        self.owned: list[dict[str, Any]] = [{"unit_id": "unt-test", "coverage": "active-owned", "actor": "agi-test"}]
        self.candidates: list[dict[str, Any]] = []
        self.verdicts: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.landings: list[dict[str, Any]] = []
        self.partial_resume = False
        self.failed_units = False
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: tuple[str, ...]) -> object:
        self.calls.append(argv)
        if argv[0] == "resume":
            return {"complete": not self.partial_resume}
        if argv[:3] == ("db", "unit", "list"):
            if self.failed_units:
                raise OSError("unit read unavailable")
            return copy.deepcopy(self.units) if argv[argv.index("--offset") + 1] == "0" else []
        if argv[:3] == ("db", "unit", "who-has-work"):
            return {"rows": copy.deepcopy(self.owned), "returned_count": len(self.owned), "has_more": False, "next_cursor": None}
        if argv[:3] == ("db", "unit", "events"):
            return {"records": copy.deepcopy(self.events), "returned_count": len(self.events), "has_more": False, "next_cursor": None}
        if argv[:3] == ("db", "candidate-revision", "list"):
            return copy.deepcopy(self.candidates)
        if argv[:3] == ("db", "verdict", "list"):
            return copy.deepcopy(self.verdicts)
        if argv[:3] == ("db", "landing", "list"):
            return copy.deepcopy(self.landings)
        raise AssertionError(f"unexpected CLI read {argv}")


class Scheduling:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self.fail_create = False

    def get_schedules_by_tag(self, tag: str, state: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG002
        return completed({"complete": True, "schedules": copy.deepcopy(self.rows)})

    def get_schedule_binding_history(self, schedule_id: str, after: list[object] | None = None, state: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(f"history:{schedule_id}")
        return completed({"events": [{"schedule_id": schedule_id}], "next_cursor": None})

    def create_cron_schedule(self, cron_expression: str, actions: list[dict[str, Any]], label: str, tags: list[str], state: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append("create")
        if self.fail_create:
            raise OSError("creation failed")
        self.rows.append({"schedule_id": "sch-new", "cron_expression": cron_expression, "tags": tags, "label": label, "status": "scheduled", "revision": 0, "action_definitions": [{**action, "result_processor": None, "result_processor_kind": None} for action in actions]})
        return completed({"schedule_id": "sch-new"})

    def clear_scheduled_action(self, schedule_id: str, state: dict[str, Any] | None = None) -> dict[str, Any]:  # noqa: ARG002
        self.calls.append(f"clear:{schedule_id}")
        self.rows = [row for row in self.rows if row["schedule_id"] != schedule_id]
        return completed({"cancelled": True})


class HeartbeatSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.memory = Memory()
        self.messaging = Messaging(self.memory)
        self.register = Register()
        self.clock = datetime(2026, 11, 10, tzinfo=UTC)
        self.orchestrator: Any = SimpleNamespace(get_service=lambda _name: self.memory, plugin_manager=SimpleNamespace(get_plugin=lambda _name: self.messaging))

    def tick(self) -> dict[str, Any]:
        self.clock += timedelta(minutes=20)
        return heartbeat.run_heartbeat(self.orchestrator, state={"agent_instance_id": "agi-test"}, role="Main-Seat", solet="fixture", read=self.register, clock=lambda: self.clock)

    def test_quiet_hour_and_transition_reversal(self) -> None:
        for _ in range(4):
            self.assertEqual(self.tick()["status"], "quiet")
        self.assertEqual(self.messaging.calls, [])
        self.register.units[0]["state"] = "landed"
        self.register.landings = [{"landing_id": "lnd-test"}]
        self.tick()
        self.tick()
        self.assertEqual(len(self.messaging.calls), 1)
        self.assertIn("lnd-test", self.messaging.calls[0]["content"])
        self.register.units[0]["state"] = "working"
        self.tick()
        self.tick()
        self.assertEqual(len(self.messaging.calls), 2)
        self.assertEqual(self.messaging.calls[0]["name"], "Main-Seat")

    def test_delivery_stop_overdue_and_review_each_once(self) -> None:
        self.tick()
        self.register.candidates = [{"revision_id": "crv-test", "state": "delivered", "history_version": 1}]
        self.tick()
        self.tick()
        self.assertEqual(len(self.messaging.calls), 1)
        self.register.events = [{"event_id": "uev-stop", "effective_detail": "STOP: measured blocker"}]
        self.tick()
        self.tick()
        self.assertEqual(len(self.messaging.calls), 2)
        self.messaging.sessions = [{"agent_instance_id": "agi-overdue", "lane_id": "lane-test", "classification": "STALLED"}]
        self.tick()
        self.tick()
        self.assertEqual(len(self.messaging.calls), 3)
        self.register.verdicts = [{"verdict_id": "uvd-test", "passed": False}]
        self.tick()
        self.tick()
        self.assertEqual(len(self.messaging.calls), 4)

    def test_first_actionable_tick_and_uncertain_effect_restart(self) -> None:
        self.register.candidates = [{"revision_id": "crv-pending", "state": "delivered", "history_version": 1}]
        self.messaging.uncertain = True
        first = self.tick()
        self.assertEqual(first["send_intent"]["outcome"], "uncertain")
        recovered = self.tick()
        self.assertEqual(recovered["send_intent"]["message_id"], "arm-fixture-1")
        self.assertEqual(recovered["send_intent"]["outcome"], "persisted")
        self.assertEqual(len(self.messaging.calls), 1)

    def test_recovered_second_send_persists_notified_digest_without_replay(self) -> None:
        self.tick()
        self.register.units[0]["state"] = "landed"
        first = self.tick()
        first_digest = first["last_notified_digest"]
        self.messaging.uncertain = True
        self.register.units[0]["state"] = "working"
        uncertain = self.tick()
        recovered_digest = uncertain["send_intent"]["digest"]
        self.assertNotEqual(recovered_digest, first_digest)
        self.assertEqual(uncertain["last_notified_digest"], first_digest)
        recovered = self.tick()
        self.assertEqual(recovered["send_intent"]["message_id"], "arm-fixture-2")
        self.assertEqual(recovered["send_intent"]["outcome"], "persisted")
        self.assertEqual(len(self.messaging.calls), 2)
        durable = self.memory.kv[heartbeat.MARKER_KEY]
        self.assertEqual(durable["last_notified_digest"], recovered_digest)
        self.tick()
        self.assertEqual(len(self.messaging.calls), 2)
        self.assertEqual(self.memory.kv[heartbeat.MARKER_KEY]["last_notified_digest"], recovered_digest)

    def test_intent_without_known_effect_is_never_replayed(self) -> None:
        self.tick()
        self.register.units[0]["state"] = "landed"
        with patch.object(heartbeat, "_send_peer_alert", side_effect=OSError("unknown effect")):
            first = self.tick()
        self.assertEqual(first["send_intent"]["outcome"], "uncertain")
        self.tick()
        self.assertEqual(self.messaging.calls, [])

    def test_partial_resume_falls_back_and_failed_domain_is_not_quiet(self) -> None:
        self.tick()
        self.register.partial_resume = True
        fallback = self.tick()
        self.assertTrue(fallback["complete"])
        self.assertFalse(fallback["resume_complete"])
        self.register.failed_units = True
        partial = self.tick()
        self.assertEqual(partial["status"], "unmeasured")
        self.assertEqual(partial["last_successful_tick"], fallback["last_successful_tick"])
        self.register.failed_units = False
        self.assertEqual(self.tick()["status"], "quiet")
        self.assertEqual(self.messaging.calls, [])

    def test_marker_correct_clear_reinitialize_and_history(self) -> None:
        first = self.tick()
        corrected = heartbeat.maintain_marker(self.orchestrator, {"operation": "correct", "tokens": [], "reason": "repair baseline", "expected_record_id": first["record_id"]})["marker"]
        with self.assertRaisesRegex(ValueError, "stale marker"):
            heartbeat.maintain_marker(self.orchestrator, {"operation": "clear", "reason": "old owner", "expected_record_id": first["record_id"]})
        retired = heartbeat.maintain_marker(self.orchestrator, {"operation": "clear", "reason": "retire", "expected_record_id": corrected["record_id"]})["marker"]
        self.assertTrue(retired["retired"])
        historical = heartbeat.maintain_marker(self.orchestrator, {"operation": "read", "record_id": first["record_id"]})
        self.assertEqual(historical["marker"]["record_id"], first["record_id"])
        self.assertFalse(self.tick()["retired"])
        self.assertEqual(self.messaging.calls, [])

    def test_paging_and_malformed_receipt(self) -> None:
        calls: list[tuple[str, ...]] = []

        def paged(argv: tuple[str, ...]) -> object:
            calls.append(argv)
            more = "--after" not in argv
            return {"rows": [{"unit_id": str(len(calls))}], "returned_count": 1, "has_more": more, "next_cursor": "opaque" if more else None}

        self.assertEqual(len(heartbeat._pages(paged, ("db", "unit", "who-has-work"), records="rows")), 2)  # noqa: SLF001
        self.assertIn("opaque", calls[-1])
        with self.assertRaisesRegex(ValueError, "receipt"):
            heartbeat._pages(lambda _argv: {"rows": []}, ("db", "unit", "who-has-work"), records="rows")  # noqa: SLF001

    def test_unknown_configuration_is_advisory_and_valid_configuration_passes(self) -> None:
        with patch.dict(os.environ, {"MAIN_SEAT_HEARTBEAT_PAGE_SIZE": "unknown"}):
            with self.assertLogs(heartbeat.logger, level="WARNING") as captured:
                self.assertEqual(heartbeat._integer_setting("MAIN_SEAT_HEARTBEAT_PAGE_SIZE", 100, maximum=100), 100)  # noqa: SLF001
            self.assertIn("using default 100", captured.output[0])
        with patch.dict(os.environ, {"MAIN_SEAT_HEARTBEAT_PAGE_SIZE": "20"}):
            self.assertEqual(heartbeat._integer_setting("MAIN_SEAT_HEARTBEAT_PAGE_SIZE", 100, maximum=100), 20)  # noqa: SLF001

    def test_registered_scheduler_fire_quiet_and_single_flight(self) -> None:
        scheduling = Scheduling()
        heartbeat.ensure_schedule(scheduling, role="Main-Seat")
        row = scheduling.rows[0]
        actions, _, _ = ScheduleFactory.parse_actions_from_params({"actions": row["action_definitions"]})
        validate_cron_action_def(actions[0])
        registry = host._real_registry()  # noqa: SLF001
        entry = registry["processes"][heartbeat.CHECK_KEY]  # type: ignore[index]
        self.assertEqual(entry["processor_policy_category"], host.ProcessorPolicyCategory.EDGE_SINK)
        _, recorder, outcome, template = host._fire(registry, actions[0], {"flow_id": "flow-seat-heartbeat", "session_id": "sess-seat-heartbeat"})  # noqa: SLF001
        self.assertEqual(outcome, (True, None))
        self.assertEqual(template.calls, 0)
        self.assertEqual(recorder.stored[0]["process_key"], heartbeat.CHECK_KEY)
        plugin = plugin_module.PlatformHealthPlugin()
        plugin.orchestrator_ref = self.orchestrator
        entered, release = threading.Event(), threading.Event()

        def run(orchestrator: object, *, state: dict[str, Any], role: str) -> dict[str, Any]:  # noqa: ARG001
            self.assertEqual(role, "Main-Seat")
            entered.set()
            self.assertTrue(release.wait(5))
            return self.tick()

        with patch.object(plugin_module, "_run_seat_heartbeat", side_effect=run):
            self.assertEqual(plugin.check_main_seat_heartbeat_cron(dict(actions[0].parameters), {})["data"]["dispatch"], "started")
            self.assertTrue(entered.wait(5))
            self.assertEqual(plugin.check_main_seat_heartbeat_cron({}, {})["data"]["dispatch"], "already_running")
            release.set()
            with plugin._seat_heartbeat_lock:  # noqa: SLF001 -- join the single flight
                pass
        self.assertEqual(self.messaging.calls, [])
        plugin.set_active(False)
        self.assertEqual(plugin.check_main_seat_heartbeat_cron({}, {})["data"]["dispatch"], "inactive")

    def test_installer_preserves_history_and_creates_before_clear(self) -> None:
        scheduling = Scheduling()
        scheduling.rows = [{"schedule_id": "sch-old", "revision": 0, "cron_expression": heartbeat.CRON, "action_definitions": [{"process_key": "plugin::agent_messaging_plugin::peer_send_by_name", "arguments": {"name": "Main-Seat", "content": "old prompt"}}]}]
        result = heartbeat.ensure_schedule(scheduling, role="Main-Seat", expected_revision=99)
        self.assertEqual(scheduling.calls, ["history:sch-old", "create", "clear:sch-old"])
        self.assertEqual(result["prior_definitions"][0]["schedule_id"], "sch-old")
        self.assertTrue(any("stale" in item for item in result["advisories"]))
        self.assertEqual(scheduling.rows[0]["cron_expression"], "7,27,47 * * * *")
        self.assertEqual(heartbeat.ensure_schedule(scheduling, role="Main-Seat")["status"], "already_present")
        scheduling = Scheduling()
        scheduling.rows = [{"schedule_id": "sch-old"}]
        scheduling.fail_create = True
        self.assertEqual(heartbeat.ensure_schedule(scheduling, role="Main-Seat")["status"], "unmeasured")
        self.assertEqual(scheduling.calls, ["history:sch-old", "create"])


if __name__ == "__main__":
    unittest.main()
