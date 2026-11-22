#!/usr/bin/env python3
"""Tier U: a failed full campaign reaches the solet's main seat the same day, once, and its verdict stays failed.

Real: the FullBatteryProducer and its CampaignStore journal, in a private
directory. Stand-ins: the runner (a canned results mapping, as the neighbouring
producer smokes use) and the transport, which is the only boundary. The
behaviour cases inject a recording notifier. The installed-path cases run the
real bridge_notifier with subprocess.run patched, because solet-bridge is a
client of the running solet and a gate smoke never reaches it. The stubbed
bridge replies are the envelopes a real peer_send_by_name call returned: a
stored message, and the refusal for a role nobody holds.
"""

# ruff: noqa: E402
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/platform_dev_surface_plugin/src"):
    sys.path.insert(0, str(source))
from platform_dev_surface_plugin.quality import full_battery
from platform_dev_surface_plugin.quality.full_battery import FullBatteryProducer
from platform_dev_surface_plugin.quality.operations import QualityOperations

from quality_gates.full_smoke_campaign import CampaignStore
from quality_gates.smoke_identity import FrozenTree

_MODULE = "platform_dev_surface_plugin.quality.full_battery"
PEER_SEND = "plugin::agent_messaging_plugin::peer_send_by_name"
RECIPIENT = "Fixture-Solet-Main"
FAILING = ["suite/a_smoke.py", "suite/b_smoke.py"]
# A complete sample: every registered smoke counted, two of them red.
RED: dict[str, Any] = {"result": "failed", "exit_code": 1, "registered_count": 3, "results_sha256": "sha-red", "summary": {"total": 3, "passed": 1, "failed": 2, "skipped": 0, "failing_smokes": FAILING}}
CLEAN: dict[str, Any] = {"result": "passed", "exit_code": 0, "registered_count": 2, "results_sha256": "sha-green", "summary": {"total": 2, "passed": 2, "retried_count": 0}}
# One smoke never ran: not a complete sample, so no failure list can be announced yet.
INCOMPLETE: dict[str, Any] = {"result": "failed", "exit_code": 1, "registered_count": 3, "summary": {"total": 3, "passed": 1, "failed": 1, "skipped": 0, "not_run": 1, "failing_smokes": FAILING[:1]}}
NO_LIST: dict[str, Any] = {"result": "failed", "exit_code": 1, "registered_count": 3, "results_sha256": "sha-nolist", "summary": {"total": 3, "passed": 1, "failed": 2, "skipped": 0}}

SENT_ID = "arm-" + "ab" * 16
# The envelope solet-bridge prints for a stored peer_send_by_name (outer job row, then the action result).
SENT: dict[str, Any] = {
    "action_id": "ae-fixturesent01",
    "flow_id": "flow-fixture",
    "process_key": PEER_SEND,
    "result": {
        "action_status": "completed",
        "actions": [],
        "data": {
            "thread_id": f"role:{RECIPIENT}",
            "message_id": SENT_ID,
            "delivery": "queued_watcher",
            "drive_on_delivery": "not_managed",
            "drive_on_delivery_detail": None,
            "delivered_to_bridge_id": "agc-fixture",
            "resolved_agent_id": "claude_code",
            "resolved_agent_instance_id": "agi-fixture",
            "resolved_session_label": RECIPIENT,
            "send_status": "stored_no_wake",
            "sender_notice": f"The message is already stored as {SENT_ID}; no turn was scheduled, so the recipient will not be woken. Do NOT resend it.",
        },
        "error": None,
        "success": True,
        "timestamp": "2026-11-19T21:45:05.952302+00:00",
    },
    "result_created_at": "2026-11-19T21:45:05.955318",
    "result_source": PEER_SEND,
    "status": "completed",
}
# The envelope, and exit code 12, for a role nobody holds, captured from a real call. No success key, data is null.
REFUSED: dict[str, Any] = {
    "action_id": "ae-2rtk2nbb7ppmk",
    "error_message": "{'type': 'agent_messaging_error', 'code': 'peer_role_vacant', 'message': \"role_binding_vacant: no binding for role 'Zz-Probe-No-Such-Role'\", 'details': {}, 'severity': 'error', 'timestamp': '2026-11-19T21:58:06.464002+00:00'}",
    "flow_id": "flow-2rtk2nb55qfbi",
    "process_key": PEER_SEND,
    "result": {
        "action_status": "failed",
        "data": None,
        "error": {
            "code": "peer_role_vacant",
            "details": {},
            "message": "{'type': 'agent_messaging_error', 'code': 'peer_role_vacant', 'message': \"role_binding_vacant: no binding for role 'Zz-Probe-No-Such-Role'\", 'details': {}, 'severity': 'error', 'timestamp': '2026-11-19T21:58:06.464002+00:00'}",
            "severity": "error",
            "timestamp": "2026-11-19T21:58:06.464002+00:00",
            "type": "agent_messaging_error",
        },
    },
    "result_created_at": "2026-11-19T21:58:06.470796",
    "result_source": PEER_SEND,
    "status": "failed",
}


class FixtureNotifier:
    """The transport stand-in: records every attempt, and can refuse or return a receipt of the test's choosing."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.stored = 0
        self.failure: Exception | None = None
        self.reply: dict[str, Any] | None = None
        self.probe: Callable[[], dict[str, Any]] | None = None
        self.at_send: list[dict[str, Any]] = []

    def __call__(self, recipient: str, text: str) -> dict[str, Any]:
        self.calls.append((recipient, text))
        if self.probe is not None:
            self.at_send.append(self.probe())
        if self.failure is not None:
            raise self.failure
        if self.reply is not None:
            return self.reply
        self.stored += 1
        return {"message_id": f"arm-{self.stored:032x}", "delivery": "queued_watcher"}


class NoticeCase(unittest.TestCase):
    """The producer, its journal and the recording transport every case below drives."""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name)
        self.tip = "fixture"
        read_tree = patch(f"{_MODULE}.read_tree", side_effect=lambda *_: FrozenTree(self.tip, f"{self.tip}-tree", {}))
        read_tree.start()
        self.addCleanup(read_tree.stop)
        self.fresh("main")

    def fresh(self, name: str) -> None:
        """A private journal, a recording transport and a producer over them."""
        self.directory = self.base / name
        self.directory.mkdir()
        self.results: list[dict[str, Any]] = [RED]
        self.notifier = FixtureNotifier()
        self.producer = self.restart(self.notifier)

    def restart(self, notifier: FixtureNotifier) -> FullBatteryProducer:
        """A new producer over the same journal: a restart keeps the campaign record and nothing else.

        The transport is a plain attribute, as ``active`` is, so one helper can drive the producer
        whether or not it takes the transport at construction.
        """
        producer = FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(self.directory), runner=lambda *_: self.results.pop(0))
        producer.notifier = notifier
        return producer

    def trigger(self) -> dict[str, Any]:
        """A request, with the worker run inline so the test sees the whole effect."""
        inline = SimpleNamespace(start=self.producer._background)
        with patch(f"{_MODULE}.threading.Thread", return_value=inline):
            return self.producer.trigger("manual")

    def only_campaign(self) -> dict[str, Any]:
        rows = CampaignStore(self.directory).all()
        self.assertEqual(len(rows), 1, "one commit is one campaign")
        return rows[0]


class NoticeTests(NoticeCase):
    """The notice's behaviour, with the transport injected."""

    def test_a_a_failed_campaign_sends_one_notice_that_names_its_failures(self) -> None:
        self.notifier.probe = self.only_campaign
        self.assertEqual(self.trigger()["status"], "started")
        self.assertEqual(len(self.notifier.calls), 1, "exactly one durable message for one failed campaign")
        recipient, text = self.notifier.calls[0]
        self.assertEqual(recipient, RECIPIENT)
        notice = json.loads(text)
        campaign = self.only_campaign()
        self.assertTrue(campaign["campaign_id"])
        self.assertEqual(notice["campaign_id"], campaign["campaign_id"])
        self.assertIn("dedupe key", notice["duplicate_semantics"])
        self.assertEqual((notice["event"], notice["solet"], notice["commit"]), ("full_battery_failed", "fixture-solet", "fixture"))
        self.assertEqual(notice["finished_at"], campaign["finished_at"])
        self.assertEqual(notice["failing_smokes"], FAILING)
        self.assertEqual(notice["results_sha256"], "sha-red")
        self.assertIn("list_full_smoke_campaigns", notice["read_with"])
        seen = self.notifier.at_send[0]
        self.assertEqual((seen["state"], seen["result"]["result"], seen["notice"]["status"]), ("closed", "failed", "due"), "the campaign was closed, and the notice recorded as due, before the transport ran")
        stored = campaign["notice"]
        self.assertEqual((stored["status"], stored["recipient"], stored["attempts"]), ("sent", RECIPIENT, 1))
        self.assertEqual((stored["message_id"], stored["delivery"]), (f"arm-{1:032x}", "queued_watcher"))
        self.assertTrue(stored["sent_at"])
        self.assertNotIn("advisory", stored)
        self.assertEqual((campaign["state"], campaign["result"]), ("closed", {**RED, "advisory": campaign["result"]["advisory"]}), "the failed verdict is exactly what the runner returned")
        self.assertIn("list_full_smoke_campaigns", campaign["result"]["advisory"])
        rows = self.producer.list_campaigns()["campaigns"]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["outcome"], rows[0]["clean"]), ("failed", False))
        self.assertEqual(rows[0]["notice"], stored, "readback shows the message id and the time it was sent")

    def test_b_a_passed_campaign_sends_nothing(self) -> None:
        self.results = [CLEAN]
        self.assertEqual(self.trigger()["status"], "started")
        campaign = self.only_campaign()
        self.assertEqual((campaign["state"], campaign["result"]["result"]), ("closed", "passed"))
        self.assertEqual(self.notifier.calls, [])
        self.assertNotIn("notice", campaign)
        rows = self.producer.list_campaigns()["campaigns"]
        self.assertEqual(len(rows), 1)
        self.assertNotIn("notice", rows[0], "a row carries a notice only where one exists")

    def test_c_a_superseded_campaign_sends_nothing_and_its_successor_sends_once(self) -> None:
        old = CampaignStore(self.directory).enqueue({"commit": "old", "tree": "old", "manifest_sha256": "old"})
        self.tip = "new"
        successor = self.producer.run_due()[0]
        retained = CampaignStore(self.directory).get(old["campaign_id"])
        if retained is None:
            self.fail("the superseded campaign record was lost")
        self.assertEqual(retained["result"]["result"], "superseded")
        self.assertNotIn("notice", retained)
        self.assertEqual(len(self.notifier.calls), 1)
        self.assertEqual(json.loads(self.notifier.calls[0][1])["campaign_id"], successor["campaign_id"])
        self.assertNotEqual(successor["campaign_id"], old["campaign_id"])

    def test_d_an_incomplete_result_announces_nothing_and_its_rerun_announces_once(self) -> None:
        self.results = [INCOMPLETE, RED]
        self.trigger()
        campaign = self.only_campaign()
        self.assertEqual((campaign["state"], campaign["result"]["result"]), ("closed", "failed"))
        self.assertEqual(self.notifier.calls, [], "no failure list is announced from an incomplete sample")
        self.assertNotIn("notice", campaign)
        self.trigger()
        self.assertEqual(len(self.notifier.calls), 1, "the complete rerun announces once")
        self.assertEqual(json.loads(self.notifier.calls[0][1])["failing_smokes"], FAILING)
        self.assertEqual(self.only_campaign()["notice"]["status"], "sent")

    def test_d2_a_complete_failure_without_a_list_is_announced_without_inventing_one(self) -> None:
        self.results = [NO_LIST]
        self.trigger()
        self.assertEqual(len(self.notifier.calls), 1)
        notice = json.loads(self.notifier.calls[0][1])
        self.assertEqual((notice["results_sha256"], notice["failing_smokes"]), ("sha-nolist", None))

    def test_e_a_send_that_does_not_happen_leaves_the_notice_due_and_the_campaign_red(self) -> None:
        cases: tuple[tuple[str, Exception | None, dict[str, Any] | None, str], ...] = (
            ("transport down", OSError("bridge down"), None, "bridge down"),
            ("queued without a durable id", None, {"delivery": "queued_for_replay"}, "message_id"),
            ("blank durable id", None, {"message_id": "  ", "delivery": "queued_watcher"}, "message_id"),
        )
        for label, failure, reply, fragment in cases:
            with self.subTest(label):
                self.fresh(label.replace(" ", "_"))
                self.notifier.failure, self.notifier.reply = failure, reply
                self.trigger()
                self.assertEqual(len(self.notifier.calls), 1)
                campaign = self.only_campaign()
                notice = campaign["notice"]
                self.assertEqual((notice["status"], notice["attempts"], notice["recipient"]), ("due", 1, RECIPIENT))
                self.assertIn(fragment, notice["advisory"])
                self.assertTrue(notice["last_attempt_at"])
                self.assertFalse({"message_id", "sent_at"} & set(notice))
                self.assertEqual((campaign["state"], campaign["result"]["result"], campaign["result"]["exit_code"]), ("closed", "failed", 1))
                rows = self.producer.list_campaigns()["campaigns"]
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["outcome"], rows[0]["clean"], rows[0]["notice"]["status"]), ("failed", False, "due"), "readback shows the notice is due")

    def test_f_a_pass_with_no_queued_work_replays_a_due_notice_and_never_sends_it_again(self) -> None:
        self.notifier.failure = OSError("bridge down")
        self.trigger()
        before = self.only_campaign()
        self.assertEqual(before["notice"]["status"], "due")
        self.notifier.failure = None
        self.assertEqual(self.producer.run_due(), [], "nothing is queued: the pass only replays the notice")
        after = self.only_campaign()
        notice = after["notice"]
        self.assertEqual((notice["status"], notice["attempts"], notice["message_id"]), ("sent", 2, f"arm-{1:032x}"))
        self.assertNotIn("advisory", notice, "a sent notice carries no stale failure")
        self.assertEqual((len(self.notifier.calls), self.notifier.stored), (2, 1), "two attempts, one stored message")
        self.assertEqual((after["state"], after["finished_at"], after["result"]), (before["state"], before["finished_at"], before["result"]), "replay leaves the failed outcome alone")
        for _ in range(2):
            self.producer.run_due()
        self.assertEqual(len(self.notifier.calls), 2, "a recorded notice is not sent again")

    def test_g_a_repeated_trigger_does_not_duplicate_a_sent_notice(self) -> None:
        self.trigger()
        campaign_id = self.only_campaign()["campaign_id"]
        self.assertEqual(len(self.notifier.calls), 1)
        again = self.trigger()
        self.assertEqual((again["status"], again["campaign_id"]), ("have_result", campaign_id))
        self.assertEqual(len(self.notifier.calls), 1, "a request that finds the result sends nothing more")

    def test_h_a_request_that_finds_the_result_replays_a_due_notice(self) -> None:
        self.notifier.failure = OSError("bridge down")
        self.trigger()
        self.notifier.failure = None
        again = self.trigger()
        self.assertEqual(again["status"], "have_result")
        self.assertEqual(self.only_campaign()["notice"]["status"], "sent")
        self.assertEqual((len(self.notifier.calls), self.notifier.stored), (2, 1))

    def test_i_a_restart_resends_only_what_is_not_recorded(self) -> None:
        self.notifier.failure = OSError("bridge down")
        self.trigger()
        revived = FixtureNotifier()
        self.assertEqual(self.restart(revived).run_due(), [])
        self.assertEqual((len(revived.calls), self.only_campaign()["notice"]["status"]), (1, "sent"), "the restarted producer sends the due notice")
        later = FixtureNotifier()
        self.assertEqual(self.restart(later).run_due(), [])
        self.assertEqual(later.calls, [], "a recorded notice survives a restart")
        self.assertEqual(len(self.notifier.calls), 1, "the first producer did not send again")


class InstalledPathTests(NoticeCase):
    """The installed transport, with the bridge's subprocess stubbed at the boundary."""

    def test_j_the_installed_service_path_sends_the_same_payload_through_the_native_route(self) -> None:
        self.trigger()
        self.assertEqual(len(self.notifier.calls), 1)
        fixture = json.loads(self.notifier.calls[0][1])
        with tempfile.TemporaryDirectory() as home, patch.dict(os.environ, {"RUN_SMOKES_BATTERY_RECEIPT_DIR": home}):
            producer = QualityOperations(ROOT, "fixture-solet").full_battery
            self.assertIsNotNone(producer.notifier, "the service path wires a transport into its producer")
            given = FixtureNotifier()
            self.assertIs(FullBatteryProducer(ROOT, "fixture-solet", store=CampaignStore(Path(home) / "given"), notifier=given).notifier, given, "the transport is injected at the producer boundary")
            producer.runner = lambda *_: RED
            stored = subprocess.CompletedProcess([], 0, stdout=json.dumps(SENT), stderr="")
            inline = SimpleNamespace(start=producer._background)
            with patch(f"{_MODULE}.subprocess.run", return_value=stored) as run, patch(f"{_MODULE}.threading.Thread", return_value=inline):
                self.assertEqual(producer.trigger("manual")["status"], "started")
            self.assertEqual(run.call_count, 1)
            argv = run.call_args.args[0]
            self.assertEqual(argv[0], str(ROOT / ".venv/bin/solet-bridge"))
            self.assertEqual(argv[1:3], ["call", PEER_SEND])
            self.assertIn("--wait", argv)
            arguments = json.loads(argv[3])
            self.assertEqual(arguments["name"], RECIPIENT)
            self.assertEqual(run.call_args.kwargs["env"]["SOLET_NAME"], "fixture-solet")
            self.assertIsInstance(run.call_args.kwargs["timeout"], int, "the send is bounded")
            sent = json.loads(arguments["content"])
            self.assertEqual(set(sent), set(fixture))
            self.assertEqual({key: value for key, value in sent.items() if key != "finished_at"}, {key: value for key, value in fixture.items() if key != "finished_at"}, "one payload contract for both transports")
            rows = producer.store.all()
            self.assertEqual(len(rows), 1)
            notice = rows[0]["notice"]
            self.assertEqual((notice["status"], notice["message_id"], notice["delivery"], notice["send_status"]), ("sent", SENT_ID, "queued_watcher", "stored_no_wake"))

    def test_k_bridge_replies_are_read_as_the_production_envelopes(self) -> None:
        notify = full_battery.bridge_notifier(ROOT, "fixture-solet")

        def answer(code: int, stdout: str) -> Any:
            return patch(f"{_MODULE}.subprocess.run", return_value=subprocess.CompletedProcess([], code, stdout=stdout, stderr=""))

        with answer(0, json.dumps(SENT)):
            receipt = notify(RECIPIENT, "text")
        self.assertEqual((receipt["message_id"], receipt["delivery"]), (SENT_ID, "queued_watcher"))
        cases = (
            ("a refusal", 12, json.dumps(REFUSED), "peer_role_vacant"),
            ("a completed envelope with a failing exit", 1, json.dumps(SENT), "exit 1"),
            ("output that is not an envelope", 0, "warning: something\n", "no readable envelope"),
            ("an envelope with no result", 0, "{}", "no readable envelope"),
        )
        for label, code, stdout, fragment in cases:
            with self.subTest(label), answer(code, stdout), self.assertRaises(RuntimeError) as caught:
                notify(RECIPIENT, "text")
            self.assertIn(fragment, str(caught.exception))
        with self.subTest("a timeout"), patch(f"{_MODULE}.subprocess.run", side_effect=subprocess.TimeoutExpired("solet-bridge", 30)), self.assertRaises(subprocess.TimeoutExpired):
            notify(RECIPIENT, "text")


if __name__ == "__main__":
    unittest.main()
