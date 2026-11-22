#!/usr/bin/env python3
"""plugins/agent_messaging_plugin/tests/peer_send_outcome_smoke.py

The one read by message id that says what happened to a peer send
(``plugin::agent_messaging_plugin::peer_send_outcome``, prb_8989586c).

A send returns ``driver_scheduled`` before the recipient's pane is touched, so a
sender took that for delivered. ``read_peer_send_outcome`` joins the two records
the platform already keeps: the finished wake attempt (in-process) and the
durable pickup row. Each test below is one of T1-T8 of plan
r74-m2-a1-outcome-read, named for its number; the three tests after T8 cover
contract branches the busy-pane cases do not reach. Running this file is T9:
it exits 0 and prints ``ALL CHECKS PASSED`` as its last line only when every
test passed.

    .venv/bin/python3 plugins/agent_messaging_plugin/tests/peer_send_outcome_smoke.py

The fixture is ``codex_eventual_pickup_smoke._PickupFixture``: a real-shape state,
a fake pane with a controlled composer, and an executor the test flushes by hand,
so "the drive has not run yet" and "the drive has finished" are both readable.
"""

from __future__ import annotations

import json
import sys
import unittest
from concurrent.futures import Future
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.llm.agent_messaging.models import PeerInboxRequest, TextPart  # noqa: E402
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.service import AgentMessagingConfig  # noqa: E402
from codex_eventual_pickup_smoke import BUSY, NAMESPACE, _PickupFixture, drives  # noqa: E402  # pyright: ignore[reportMissingImports]

import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
from agent_messaging_plugin.peer_send_outcome import read_peer_send_outcome  # noqa: E402
from agent_messaging_plugin.session_hosts import DriverChannelSendError  # noqa: E402
from agent_messaging_plugin.session_lifecycle_control import CLAUDE_PICKUP_PENDING  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
PROCESS_JSON = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes" / "peer_send_outcome.json"
PROCESS_KEY = "plugin::agent_messaging_plugin::peer_send_outcome"

OUTCOME_KEYS = frozenset(
    {
        "message_id",
        "delivery_state",
        "wake_attempt_outcome",
        "wake_attempt_detail",
        "evidence_source",
        "pickup",
        "pickup_unavailable",
        "explanation",
    },
)
PICKUP_KEYS = frozenset({"phase", "reason", "pending", "attempts", "version", "checked_at"})

# The contract's fixed text per state. ``{d}`` is the detail, or the outcome when the detail is None.
EXPLANATIONS = {
    "read_by_recipient": "The recipient's inbox read returned this message. That proves the read, not that the work was done.",
    "wake_typed": "A wake notice was typed into the recipient's pane. The recipient has not yet read the message from its inbox.",
    "wake_attempt_running": "The wake attempt has not finished. Read again in a few seconds.",
    "not_delivered": "NOT DELIVERED: nothing reached the recipient's pane ({d}). The message is stored and the recipient reads it at its next inbox read. Do not resend it.",
    "no_driver_wake": "The driver does not wake this recipient ({d}). Whether it has seen the message depends on the delivery value of the send receipt. Do not resend it.",
    "not_confirmed": "No wake-attempt record for this message exists in the running process (it restarted, more than 512 sends followed, or the id is wrong). Delivery is not confirmed.",
    "channel_queued": "The message was queued on the recipient's live channel, which carries it to the session; no wake notice was typed. Delivery to the model is not observed here. Do not resend it.",
}

# Every outcome word the read can meet, and the state the contract maps it to.
STATE_BY_OUTCOME = {
    "driver_sent": "wake_typed",
    "driver_turn_observed": "wake_typed",
    "driver_scheduled": "wake_attempt_running",
    "driver_not_needed": "channel_queued",
    "queued": "wake_attempt_running",
    "not_managed": "no_driver_wake",
    "ineligible_state": "no_driver_wake",
    "driver_unavailable": "not_delivered",
    "driver_queue_full": "not_delivered",
    "driver_error": "not_delivered",
    "driver_pending": "not_delivered",
}


class _RefusingClaudeChannel:
    """A Claude channel whose send fails the way a busy composer fails it."""

    def send(self, text: str) -> None:
        raise DriverChannelSendError("Claude pane showed no empty composer within 10.0s; nothing was sent.")


def _finished(outcome: str, detail: str | None) -> Future[tuple[str, str | None]]:
    future: Future[tuple[str, str | None]] = Future()
    future.set_result((outcome, detail))
    return future


class PeerSendOutcomeTests(_PickupFixture):
    def _outcome_of(self, message_id: str) -> dict[str, Any]:
        return cast("dict[str, Any]", read_peer_send_outcome(self.state, message_id))

    def _explanation(self, outcome: dict[str, Any]) -> str:
        detail = outcome["wake_attempt_detail"]
        return EXPLANATIONS[outcome["delivery_state"]].format(d=detail if detail is not None else outcome["wake_attempt_outcome"])

    def _flush_quietly(self) -> None:
        """Run the queued drives with their log output captured: a refused send logs its channel failure."""
        with self.assertLogs(level="INFO"):
            self.executor.flush()

    def _send_busy_codex(self, message_id: str = "arm-busy") -> None:
        self.panes["pane-a"].visible = BUSY
        self._send(message_id)

    def _send_refused_claude(self, message_id: str, *, instance: str = "agi-a", session: str = "ases-a", pane: str = "pane-a") -> str:
        """Make ``instance`` a Claude recipient whose pane refuses the notice, then send."""
        self.fake.update_state(NAMESPACE, {"table": "managed_session", "filters": {"external_id": instance}}, {"agent_runtime": "claude_code"})
        refusing = _RefusingClaudeChannel()
        previous = self.channel_mock.side_effect
        self.channel_mock.side_effect = lambda row: refusing if row["host_ref"] == pane else previous(row)
        return drives.schedule_drive_on_delivery(
            self.state,
            recipient_agent_instance_id=instance,
            recipient_agent_session_id=session,
            recipient_agent_id="claude_code",
            sender_label="Sender",
            message_id=message_id,
            channel_delivered=False,
        )

    def test_t1_busy_codex_pane_reads_not_delivered(self) -> None:
        self._send_busy_codex()
        self.executor.flush()
        outcome = self._outcome_of("arm-busy")
        print("T1 busy Codex pane:", json.dumps(outcome, sort_keys=True, default=str))
        self.assertEqual(set(outcome), OUTCOME_KEYS, "exactly the eight keys")
        self.assertEqual(outcome["message_id"], "arm-busy")
        self.assertEqual(outcome["delivery_state"], "not_delivered")
        self.assertIn("pending_busy", outcome["wake_attempt_detail"])
        self.assertTrue(outcome["explanation"].startswith("NOT DELIVERED"))
        self.assertEqual(outcome["explanation"], self._explanation(outcome))
        self.assertEqual(outcome["evidence_source"], "wake_attempt_record")
        self.assertFalse(outcome["pickup_unavailable"])
        self.assertEqual(set(outcome["pickup"]), PICKUP_KEYS)
        self.assertIsInstance(outcome["pickup"]["checked_at"], str)
        self.assertEqual(self.panes["pane-a"].keys(), [], "the read and the drive typed nothing into a busy pane")

    def test_t2_busy_claude_pane_reads_not_delivered_and_the_placeholder_is_not_an_outcome(self) -> None:
        self.assertEqual(self._send_refused_claude("arm-claude-busy"), "driver_scheduled")
        self._flush_quietly()
        outcome = self._outcome_of("arm-claude-busy")
        print("T2 busy Claude pane:", json.dumps(outcome, sort_keys=True, default=str))
        self.assertEqual(outcome["delivery_state"], "not_delivered")
        self.assertIn("nothing was sent", outcome["wake_attempt_detail"])
        self.assertEqual(outcome["wake_attempt_outcome"], "driver_error")
        self.assertEqual(outcome["evidence_source"], "wake_attempt_record")
        self.assertEqual(outcome["explanation"], self._explanation(outcome))
        # The pickup row for a Claude recipient never records the finished attempt: it keeps the placeholder.
        self.assertEqual(outcome["pickup"]["phase"], "driver_pending")
        self.assertEqual(outcome["pickup"]["reason"], CLAUDE_PICKUP_PENDING)
        self.assertNotEqual(outcome["wake_attempt_detail"], CLAUDE_PICKUP_PENDING)

    def test_t3_a_send_read_before_the_drive_runs_is_running(self) -> None:
        self._send_busy_codex()
        outcome = self._outcome_of("arm-busy")
        self.assertEqual(outcome["delivery_state"], "wake_attempt_running")
        self.assertEqual(outcome["wake_attempt_outcome"], "driver_scheduled")
        self.assertIsNone(outcome["wake_attempt_detail"])
        self.assertEqual(outcome["evidence_source"], "wake_attempt_record")
        self.assertEqual(outcome["explanation"], EXPLANATIONS["wake_attempt_running"])
        self.assertEqual(self.panes["pane-a"].keys(), [])
        self.executor.flush()
        self.assertEqual(self._outcome_of("arm-busy")["delivery_state"], "not_delivered", "the same id reads differently once the drive has finished")

    def test_t4_an_idle_codex_pane_after_the_drive_reads_wake_typed(self) -> None:
        self._send("arm-idle")
        self.executor.flush()
        outcome = self._outcome_of("arm-idle")
        self.assertEqual(outcome["delivery_state"], "wake_typed")
        self.assertIn(outcome["wake_attempt_outcome"], {"driver_sent", "driver_turn_observed"})
        self.assertEqual(outcome["explanation"], EXPLANATIONS["wake_typed"])
        self.assertTrue(self.panes["pane-a"].keys(), "the wake notice reached the pane")

    def test_t5_after_the_recipient_inbox_read_returns_the_id_it_reads_read_by_recipient(self) -> None:
        self._send("arm-idle")
        self.executor.flush()
        self.assertEqual(self._outcome_of("arm-idle")["delivery_state"], "wake_typed")
        page = self._service([self._message("arm-idle")]).peer_inbox(PeerInboxRequest(recipient_agent_id="codex", recipient_agent_instance_id="agi-a"))
        self.assertEqual([entry.message.id for entry in page.entries], ["arm-idle"])
        outcome = self._outcome_of("arm-idle")
        self.assertEqual(outcome["delivery_state"], "read_by_recipient")
        self.assertEqual(outcome["evidence_source"], "pickup_row", "the inbox read is recorded on the pickup row, not on the wake attempt")
        self.assertEqual(outcome["wake_attempt_outcome"], "driver_turn_observed", "the finished wake attempt is still reported as picked")
        self.assertFalse(outcome["pickup"]["pending"])
        self.assertEqual(outcome["pickup"]["phase"], "inbox_returned")
        self.assertEqual(outcome["explanation"], EXPLANATIONS["read_by_recipient"])

    def test_t6_with_the_in_process_records_gone_the_pickup_row_answers_and_claude_is_not_confirmed(self) -> None:
        self._send_busy_codex()
        self._send_refused_claude("arm-claude-busy", instance="agi-b", session="ases-b", pane="pane-b")
        self._flush_quietly()
        with patch.dict(drives._DRIVE_RECORDS, clear=True):  # noqa: SLF001 -- the restart the test models
            codex = self._outcome_of("arm-busy")
            claude = self._outcome_of("arm-claude-busy")
            unknown = self._outcome_of("arm-never-sent")
        self.assertEqual(codex["delivery_state"], "not_delivered")
        self.assertEqual(codex["evidence_source"], "pickup_row")
        self.assertEqual(codex["wake_attempt_outcome"], "driver_pending")
        self.assertEqual(codex["explanation"], self._explanation(codex))
        for outcome in (claude, unknown):
            self.assertEqual(outcome["delivery_state"], "not_confirmed")
            self.assertIsNone(outcome["wake_attempt_outcome"])
            self.assertIsNone(outcome["wake_attempt_detail"])
            self.assertEqual(outcome["evidence_source"], "none")
            self.assertEqual(outcome["explanation"], EXPLANATIONS["not_confirmed"])
        self.assertEqual(claude["pickup"]["reason"], CLAUDE_PICKUP_PENDING, "the placeholder is shown as the pickup, never as the outcome")
        self.assertIsNone(unknown["pickup"])

    def test_t7_a_recipient_with_no_driver_reads_no_driver_wake(self) -> None:
        scheduled = drives.schedule_drive_on_delivery(
            self.state,
            recipient_agent_instance_id="agi-nobody",
            recipient_agent_session_id="",
            recipient_agent_id="operator",
            sender_label="Sender",
            message_id="arm-unmanaged",
            channel_delivered=False,
        )
        self.assertEqual(scheduled, "not_managed")
        outcome = self._outcome_of("arm-unmanaged")
        self.assertEqual(outcome["delivery_state"], "no_driver_wake")
        self.assertEqual(outcome["wake_attempt_outcome"], "not_managed")
        self.assertEqual(outcome["explanation"], self._explanation(outcome))
        self.assertTrue(outcome["explanation"].startswith("The driver does not wake this recipient ("))

    def test_t8_the_verb_returns_the_read_and_refuses_only_an_empty_id(self) -> None:
        self._send_busy_codex()
        self.executor.flush()
        plugin = object.__new__(plugin_module.AgentMessagingPlugin)
        plugin._get_state_service = lambda: self.state  # type: ignore[method-assign]  # noqa: SLF001 -- harness wiring
        for params in ({"message_id": "arm-busy"}, {"parameters": {"message_id": "arm-busy"}}):
            with self.subTest(params=params):
                result = plugin.peer_send_outcome(params, {})
                self.assertEqual(result["action_status"], "completed")
                self.assertEqual(result["data"], self._outcome_of("arm-busy"))
                self.assertEqual(result["data"]["delivery_state"], "not_delivered")
        refused = plugin.peer_send_outcome({"message_id": ""}, {})
        self.assertEqual(refused["action_status"], "failed")
        self.assertEqual(refused["error"]["code"], "missing_message_id")
        absent = plugin.peer_send_outcome({}, {})
        self.assertEqual(absent["error"]["code"], "missing_message_id")
        unknown = plugin.peer_send_outcome({"message_id": "arm-never-sent"}, {})
        self.assertEqual(unknown["action_status"], "completed", "an unknown id is a named state, not a refusal")
        self.assertEqual(unknown["data"]["delivery_state"], "not_confirmed")
        self.assertIn("peer_send_outcome", plugin_module.AgentMessagingPlugin.get_edge_process_definitions(plugin))
        self.assertEqual(json.loads(PROCESS_JSON.read_text(encoding="utf-8"))["process_key"], PROCESS_KEY)

    def test_t5_a_claude_send_to_a_busy_pane_is_stored_not_delivered_yet_and_its_read_says_not_delivered(self) -> None:
        import drive_on_delivery_smoke as sibling  # noqa: PLC0415  # pyright: ignore[reportMissingImports]

        from agent_messaging_plugin.peer_dispatch import dispatch_peer_send  # noqa: PLC0415

        self.fake.update_state(NAMESPACE, {"table": "managed_session", "filters": {"external_id": "agi-a"}}, {"agent_runtime": "claude_code"})
        refusing = _RefusingClaudeChannel()
        previous = self.channel_mock.side_effect
        self.channel_mock.side_effect = lambda row: refusing if row["host_ref"] == "pane-a" else previous(row)
        registry = sibling._FakePeerRegistry(sibling._binding(agent_instance_id="agi-a", agent_session_id="ases-a"))  # noqa: SLF001
        common: dict[str, Any] = {
            "bridge_manager": sibling._FakeBridgeManager(),
            "peer_registry": registry,
            "agent_messaging_service": drives.PickupTrackingMessagingService(repository=AgentMessagingRepository(self.state), state_service=self.state, config=AgentMessagingConfig()),
            "state_service": self.state,
            "sender_bridge_id": "agc-sender",
            "sender_agent_id": "codex",
            "sender_agent_instance_id": "agi-sender",
            "sender_session_label": "Sender",
            "sender_parent_pid": None,
            "content": [TextPart(type="text", text="exact durable body")],
        }
        outcome = dispatch_peer_send(**common, peer_id="claude_code", peer_agent_instance_id="agi-a")
        payload = outcome.to_payload()
        print("T5 receipt:", json.dumps(payload, sort_keys=True, default=str))
        self.assertEqual(payload["send_status"], "stored_not_delivered_yet")
        self.assertEqual(payload["outcome_read"], {"process": PROCESS_KEY, "parameters": {"message_id": payload["message_id"]}})
        self._flush_quietly()
        read = self._outcome_of(payload["outcome_read"]["parameters"]["message_id"])
        self.assertEqual(read["delivery_state"], "not_delivered")
        print(f"DONE-TEST busy pane: receipt={payload['send_status']} read={read['delivery_state']}")

    def test_every_outcome_word_maps_to_the_contract_state(self) -> None:
        for outcome_word, state in STATE_BY_OUTCOME.items():
            with self.subTest(outcome=outcome_word), patch.dict(drives._DRIVE_RECORDS, {f"arm-{outcome_word}": _finished(outcome_word, None)}):  # noqa: SLF001
                outcome = cast("dict[str, Any]", read_peer_send_outcome(None, f"arm-{outcome_word}"))
                self.assertEqual(outcome["delivery_state"], state)
                self.assertEqual(outcome["wake_attempt_outcome"], outcome_word)
                self.assertEqual(outcome["explanation"], self._explanation(outcome), "with no detail the explanation names the outcome")
                if state in {"not_delivered", "no_driver_wake"}:
                    self.assertIn(f"({outcome_word})", outcome["explanation"])

    def test_a_channel_carried_message_reads_channel_queued_and_the_inbox_read_still_wins(self) -> None:
        """prb_57c118ad: ``driver_not_needed`` says the live channel carries the message, not that no one was reached."""
        with patch.dict(drives._DRIVE_RECORDS, {"arm-channel": _finished("driver_not_needed", "queued on the live channel")}):  # noqa: SLF001
            outcome = self._outcome_of("arm-channel")
        self.assertEqual(outcome["delivery_state"], "channel_queued")
        self.assertEqual(outcome["wake_attempt_outcome"], "driver_not_needed")
        self.assertIn("Do not resend", outcome["explanation"])
        self.assertNotIn("NOT DELIVERED", outcome["explanation"], "a channel-carried message is not reported as undelivered")
        # An inbox read that already returned the id is stronger evidence than the send-time outcome.
        self._send("arm-channel-read")
        self.executor.flush()
        with patch.dict(drives._DRIVE_RECORDS, {"arm-channel-read": _finished("driver_not_needed", None)}):  # noqa: SLF001
            self._service([self._message("arm-channel-read")]).peer_inbox(PeerInboxRequest(recipient_agent_id="codex", recipient_agent_instance_id="agi-a"))
            read = self._outcome_of("arm-channel-read")
        self.assertEqual(read["delivery_state"], "read_by_recipient")
        self.assertEqual(read["pickup"]["phase"], "inbox_returned")

    def test_a_failed_pickup_read_is_named_and_the_wake_attempt_still_answers(self) -> None:
        self._send_busy_codex()
        self.executor.flush()
        with (
            patch.object(drives.CodexPickupStore, "read", side_effect=RuntimeError("pickup read failed")),
            self.assertLogs("agent_messaging_plugin.peer_send_outcome", level="ERROR") as captured,
        ):
            outcome = self._outcome_of("arm-busy")
        self.assertTrue(outcome["pickup_unavailable"])
        self.assertIsNone(outcome["pickup"])
        self.assertEqual(outcome["delivery_state"], "not_delivered", "the in-process record alone still answers")
        self.assertTrue(any("pickup read failed" in line for line in captured.output), "the read failure is logged with its traceback")
        self.assertEqual(self._outcome_of("arm-busy")["pickup_unavailable"], False, "a healthy read clears the flag")

    def test_no_state_reads_the_wake_attempt_alone(self) -> None:
        self._send_busy_codex()
        self.executor.flush()
        outcome = cast("dict[str, Any]", read_peer_send_outcome(None, "arm-busy"))
        self.assertIsNone(outcome["pickup"])
        self.assertEqual(outcome["delivery_state"], "not_delivered")
        self.assertEqual(outcome["evidence_source"], "wake_attempt_record")

    def test_the_read_writes_nothing(self) -> None:
        self._send_busy_codex()
        self.executor.flush()
        before = (self.store.read("arm-busy"), len(self.store.history("arm-busy")))
        for _ in range(3):
            self._outcome_of("arm-busy")
            self._outcome_of("arm-never-sent")
        self.assertEqual((self.store.read("arm-busy"), len(self.store.history("arm-busy"))), before, "reading changes neither the row nor its history")


def run_checks() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(PeerSendOutcomeTests)
    return unittest.TextTestRunner(stream=sys.stdout, verbosity=2).run(suite).wasSuccessful()


if __name__ == "__main__":
    passed = run_checks()
    print("ALL CHECKS PASSED" if passed else "CHECKS FAILED")
    sys.exit(0 if passed else 1)
