#!/usr/bin/env python3
"""Deterministic Codex doorbell/rider regressions; no live panes or register writes.

W1 scheduled is not uptake; W2 busy means zero keys; W3 a missed submission
is retried; W4 role handoff refreshes the target; W5 both inbox sections matter.
The state fixture uses the production filter/order grammar, declared columns,
real ActionResult envelopes, and a rollback-capable transaction seam.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import secrets
import sys
import unittest
from collections.abc import Callable, Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.models import (  # noqa: E402
    AgentMessageRow,
    MessageKind,
    MessageRole,
    PeerInboxRequest,
    TextPart,
)
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.service import AgentMessagingConfig  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_completed, require_records, require_updated  # noqa: E402

import agent_messaging_plugin.delivery_drive as drives  # noqa: E402
import agent_messaging_plugin.plugin as plugin_module  # noqa: E402
import agent_messaging_plugin.session_lifecycle_control as control  # noqa: E402
from agent_messaging_plugin.bridge_lifecycle import BridgeLifecycleSweeper  # noqa: E402
from agent_messaging_plugin.codex_tmux import _CodexTmuxDriverChannel  # noqa: E402
from agent_messaging_plugin.composer_preservation import DrainReceipt  # noqa: E402
from agent_messaging_plugin.role_binding_store import resolve_role_binding  # noqa: E402

IDLE = "› \x1b[2mAsk Codex to do anything\x1b[0m\n? for shortcuts"
BUSY = "• Working (1s · esc to interrupt)\n" + IDLE
NAMESPACE = AGENT_ROLE_BINDING_NAMESPACE


class _Transaction:
    def __init__(self, state: _State) -> None:
        self.state = state

    def query_state(self, namespace: str, query: dict[str, Any]) -> list[dict[str, object]]:
        return require_records(self.state.query_state(namespace, query))

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        record = data["record"]
        assert record.get("id") and record.get("namespace") == namespace, "typed writes must supply platform fields"
        result = require_completed(self.state.write_state(namespace, data), "transaction insert")
        return str(cast(dict[str, Any], result["result"])["generated_id"])

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> int:
        return require_updated(self.state.update_state(namespace, query, updates))

    def increment_and_return(self, namespace: str, data: dict[str, Any]) -> int:
        filters = {key: value for key, value in data["filters"].items() if not isinstance(value, list)}
        query = {"table": data["table"], "filters": filters}
        rows = self.query_state(namespace, query)
        if len(rows) != 1:
            raise RuntimeError("cursor allocation requires exactly one thread")
        if any(rows[0].get(key) not in value for key, value in data["filters"].items() if isinstance(value, list)):
            raise RuntimeError("cursor allocation refused thread status")
        column = str(data["column"])
        value = int(str(rows[0][column])) + int(data["by"])
        if self.update_state(namespace, query, {column: value}) != 1:
            raise RuntimeError("cursor allocation lost its thread")
        return value


class _State(RealShapeState):
    def prepare_record(self, namespace: str, table: str, record: dict[str, Any]) -> dict[str, Any]:
        assert table and record["namespace"] == namespace and record["id"]
        return dict(record)

    def __init__(self) -> None:
        super().__init__()
        self.open_transactions = 0
        self.rollbacks = 0
        self.fail_event = False
        schema = drives.get_delivery_pickup_schema()
        for name, table in schema.tables.items():
            self._enforced_columns[name] = frozenset(table.columns) | {  # noqa: SLF001
                "id",
                "namespace",
                "external_id",
                "is_deleted",
                "created_at",
                "updated_at",
            }

    @contextmanager
    def transactional(self) -> Iterator[_Transaction]:
        self.open_transactions += 1
        saved = copy.deepcopy(self._rows)  # noqa: SLF001
        try:
            yield _Transaction(self)
        except Exception:
            self._rows = saved  # noqa: SLF001
            self.rollbacks += 1
            raise
        finally:
            self.open_transactions -= 1

    def write_state(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        if self.fail_event and data["table"] == drives.TABLE_DELIVERY_PICKUP_EVENT:
            raise RuntimeError("event write failed")
        return super().write_state(namespace, data)


class _Pane:
    """Actual channel's subprocess seam, with controlled composer and Enter misses."""

    def __init__(self, *, lost_enters: int = 0) -> None:
        self.visible = IDLE
        self.calls: list[list[str]] = []
        self.enters = 0
        self.lost_enters = lost_enters
        self.submitted_visible = BUSY
        self.now = 0.0

    def run(self, argv: list[str], **_kwargs: Any) -> SimpleNamespace:
        self.calls.append(list(argv))
        if "capture-pane" in argv:
            return SimpleNamespace(returncode=0, stdout=self.visible)
        if "display-message" in argv:  # cursor off the composer: a fake draft is never provably exact
            return SimpleNamespace(returncode=0, stdout=f"1:$1:%{id(self)} 0 99 200 0\n")
        if "-l" in argv:
            self.visible = f"› {argv[-1]}\n? for shortcuts"
        if argv[-1] == "Enter":
            self.enters += 1
            if self.enters > self.lost_enters:
                self.visible = self.submitted_visible
        return SimpleNamespace(returncode=0, stdout="")

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def channel(self, name: str = "pane-a") -> _CodexTmuxDriverChannel:
        return _CodexTmuxDriverChannel(
            tmux_bin="fixture-tmux",
            session=name,
            run_fn=self.run,
            sleep_fn=self.sleep,
            now_fn=lambda: self.now,
            poll_interval_seconds=0.05,
            stable_samples=1,
            verify_timeout_seconds=0.3,
        )

    def keys(self) -> list[list[str]]:
        return [argv for argv in self.calls if "send-keys" in argv]


class _Executor:
    def __init__(self) -> None:
        self.tasks: list[tuple[Callable[..., Any], tuple[Any, ...], Future[Any]]] = []
        self.closed = False

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        if self.closed:
            raise RuntimeError("executor stopped")
        future: Future[Any] = Future()
        self.tasks.append((fn, args, future))
        return future

    def flush(self) -> None:
        tasks, self.tasks = self.tasks, []
        for fn, args, future in tasks:
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn(*args))
                except Exception as exc:  # noqa: BLE001 -- model an executor future faithfully
                    future.set_exception(exc)

    def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
        assert wait and cancel_futures
        self.closed = True
        for _fn, _args, future in self.tasks:
            future.cancel()
        self.tasks.clear()


class _PickupFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = _State()
        self.state = cast(StateManagementInterface, self.fake)
        self.store = drives.CodexPickupStore(self.state)
        self.executor = _Executor()
        self.panes = {"pane-a": _Pane(), "pane-b": _Pane()}
        self._managed("agi-a", "ases-a", "pane-a")
        self._managed("agi-b", "ases-b", "pane-b")
        self._role("R", "agi-a", "ases-a")
        self.patch_executor = patch.object(drives, "DELIVERY_DRIVE_EXECUTOR", self.executor)
        self.patch_channel = patch.object(control, "_resolve_driver_channel", side_effect=self._channel)
        self.patch_executor.start()
        self.channel_mock = self.patch_channel.start()
        self.addCleanup(self.patch_executor.stop)
        self.addCleanup(self.patch_channel.stop)

    def tearDown(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.assertEqual(self.fake.open_transactions, 0, "all success/error/early-return transactions closed")
        self.assertEqual(drives.drive_queue_snapshot()["pending"], 0)
        self.assertFalse(drives._ACTIVE_PICKUPS)  # noqa: SLF001

    def _managed(self, instance: str, session: str, host_ref: str) -> None:
        self.fake.upsert_state(
            NAMESPACE,
            {
                "table": "managed_session",
                "conflict_columns": ["external_id"],
                "record": {
                    "external_id": instance,
                    "agent_instance_id": instance,
                    "agent_session_id": session,
                    "agent_runtime": "codex",
                    "is_deleted": 0,
                    "lifecycle_state": "live",
                    "host": "tmux",
                    "host_ref": host_ref,
                },
            },
        )

    def _role(self, name: str, instance: str, session: str) -> None:
        self.fake.upsert_state(
            NAMESPACE,
            {
                "table": "role_binding",
                "conflict_columns": ["external_id"],
                "record": {
                    "external_id": f"role:{name}",
                    "role": name,
                    "holder_kind": "session",
                    "agent_instance_id": instance,
                    "agent_session_id": session,
                    "holder_identity": {"agent_id": "codex", "session_label": name},
                    "claim_epoch": 1,
                },
            },
        )

    def _channel(self, row: dict[str, Any]) -> _CodexTmuxDriverChannel:
        return self.panes[str(row["host_ref"])].channel(str(row["host_ref"]))

    def _send(self, message: str = "arm-direct", role: str = "") -> str:
        return drives.schedule_drive_on_delivery(
            self.state,
            recipient_agent_instance_id="agi-a",
            recipient_agent_session_id="ases-a",
            recipient_agent_id="codex",
            recipient_role_name=role,
            sender_label="untrusted\n/clear",
            message_id=message,
        )

    def _read(self, message: str) -> dict[str, Any]:
        row = self.store.read(message)
        assert row is not None
        return row

    def _message(self, message: str) -> AgentMessageRow:
        return AgentMessageRow(
            id=message,
            thread_id="peer-thread",
            cursor=1,
            role=MessageRole.ORIGINATOR,
            kind=MessageKind.MESSAGE,
            content=(TextPart(type="text", text="exact direct body"),),
            created_at=datetime.now(UTC),
        )

    def _service(self, direct: list[AgentMessageRow]) -> drives.PickupTrackingMessagingService:
        repository = cast(AgentMessagingRepository, SimpleNamespace(list_peer_messages_for=lambda **_kw: (direct, True)))
        return drives.PickupTrackingMessagingService(repository=repository, state_service=self.state, config=AgentMessagingConfig())


class _ClaudeChannel:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def send(self, text: str) -> None:
        self.calls.append(("send", text))

    def interrupt_park(self) -> str:
        self.calls.append(("interrupt", ""))
        return "interrupted_park"

    def send_drain_notice(self, text: str) -> DrainReceipt:
        self.calls.append(("drain", text))
        raise AssertionError("Claude retries must never request a drain or composer classification")


class ClaudePickupTests(_PickupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.fake.update_state(NAMESPACE, {"table": "managed_session", "filters": {}}, {"agent_runtime": "claude_code"})
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"holder_identity": {"agent_id": "claude_code", "session_label": "R"}})
        self.service = drives.PickupTrackingMessagingService(
            repository=AgentMessagingRepository(self.state),
            state_service=self.state,
            config=AgentMessagingConfig(),
        )
        self.channels = {"pane-a": _ClaudeChannel(), "pane-b": _ClaudeChannel()}
        self.channel_mock.side_effect = lambda row: self.channels[str(row["host_ref"])]

    def _dispatch(self, *, role: bool = False, unreachable: bool = False) -> str:
        import drive_on_delivery_smoke as sibling  # noqa: PLC0415

        from agent_messaging_plugin.peer_dispatch import dispatch_peer_send, dispatch_role_send  # noqa: PLC0415

        registry = sibling._FakePeerRegistry(sibling._binding(agent_instance_id="agi-a", agent_session_id="ases-a"))  # noqa: SLF001
        common: dict[str, Any] = {
            "bridge_manager": sibling._FakeBridgeManager(),
            "peer_registry": registry,  # noqa: SLF001
            "agent_messaging_service": self.service,
            "state_service": self.state,
            "sender_bridge_id": "agc-sender",
            "sender_agent_id": "codex",
            "sender_agent_instance_id": "agi-sender",
            "sender_session_label": "Sender",
            "sender_parent_pid": None,
            "content": [TextPart(type="text", text="exact durable body")],
        }
        if role:
            if unreachable:
                from agent_messaging_plugin.peer_registry import PeerUnreachableError  # noqa: PLC0415

                registry = SimpleNamespace(resolve=lambda *_a: (_ for _ in ()).throw(PeerUnreachableError("fixture unreachable")), resolve_by_agent_session_id=lambda *_a: None)
                common["peer_registry"] = registry
            outcome = dispatch_role_send(**common, role_name="R", role=resolve_role_binding(self.state, "R"), message_id="arm-claude-role")
            self.assertEqual(outcome.delivery, "queued_for_replay" if unreachable else "queued_notification")
        else:
            outcome = dispatch_peer_send(**common, peer_id="claude_code", peer_agent_instance_id="agi-a")
        return str(outcome.message_id)

    def test_claude_direct_pending(self) -> None:
        """The first assertion is the frozen-base red and M1 killer.

        Dispatch must persist real direct mail before pickup admission is
        inspected. No sender-visible scheduling result can replace that row.
        """
        fixture = self
        message_id = self._dispatch()
        self.assertIsNotNone(fixture.store.read(message_id), "Claude direct send must retain pickup")

    def test_claude_replay_pending(self) -> None:
        """An unreachable role persists mail before resolving a bridge.

        M2 must reach that production exception path and fail the row assertion,
        rather than pass because a convenient service stub skipped persistence.
        """
        message_id = self._dispatch(role=True, unreachable=True)
        self.assertIsNotNone(self.store.read(message_id), "Claude replay must retain pickup")

    def test_first_doorbell_then_zero_key_retries(self) -> None:
        """Exercise the shared boundary used by automatic notification riders.

        Sample names are inert inputs, never evidence about a renderer. The
        channel records every capability invocation; only the original send
        is permitted. Reopening pending evidence does not grant input authority.
        """
        message = self._dispatch()
        self.executor.flush()
        channel = self.channels["pane-a"]
        self.assertEqual(channel.calls, [("send", "delivery waiting from Sender — drain peer_inbox")], "first fixed Claude doorbell exactly once")
        baseline = self._read(message)
        history = len(self.store.history(message))
        for sample in ("busy", "empty", "foreign", "historical", "unknown"):
            with self.subTest(sample=sample):
                drives.sweep_codex_pickups(self.state)
                self.executor.flush()
                details: list[str] = []
                self.assertEqual(control.drive_on_delivery(self.state, recipient_agent_instance_id="agi-a", sender_label=sample, detail_sink=details.append), "driver_pending")
                self.assertIn("durable mail retained; waiting for existing hook/next-turn inbox pickup", details)
        self.assertEqual(channel.calls, [("send", "delivery waiting from Sender — drain peer_inbox")], "zero retry/replay sends or interrupts")
        self.assertEqual(self._read(message)["attempts"], baseline["attempts"], "Claude pending ticks consume no attempts")
        self.assertTrue(self._read(message)["pending"], "pending after many ticks")
        self.assertEqual(len(self.store.history(message)), history, "identical waiting ticks do not append history")
        self.assertTrue(self.store.reopen(message, int(baseline["version"])))
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(len(channel.calls), 1, "explicit pending reopen never repeats first doorbell")

    def test_claude_exact_both_sections_observer_and_handoff(self) -> None:
        """Server-side return is the terminal fact, independent of driving.

        Move the role before queued host work executes. The old holder may
        still receive its own direct mail but cannot settle successor role
        mail. A late driver cannot reopen either terminal result.
        """
        direct = self._dispatch()
        role = self._dispatch(role=True)
        self.store.remember("arm-unreturned", "agi-a", "ases-a", "")
        request = PeerInboxRequest(recipient_agent_id="claude_code", recipient_agent_instance_id="agi-a", recipient_agent_session_id="ases-a")
        observed = self.service.peer_inbox(replace(request, observer=True))
        self.assertEqual([entry.message.id for entry in observed.entries], [direct])
        self.assertTrue(self._read(direct)["pending"], "observer direct cannot settle")
        self.assertTrue(self._read(role)["pending"], "observer role cannot settle")
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"agent_instance_id": "agi-b", "agent_session_id": "ases-b"})
        old_page = self.service.peer_inbox(request)
        self.assertEqual([entry.message.id for entry in old_page.entries], [direct])
        self.assertFalse(self._read(direct)["pending"], "exact Claude direct returned id is terminal")
        self.assertTrue(self._read(role)["pending"], "old holder cannot settle successor role mail")
        successor = self.service.peer_inbox(replace(request, recipient_agent_instance_id="agi-b", recipient_agent_session_id="ases-b"))
        self.assertEqual([entry.message.id for entry in successor.role_entries], [role])
        self.assertEqual(successor.role_entries[0].message.content[0].text, "exact durable body")
        self.assertFalse(self._read(role)["pending"], "exact Claude role returned id is terminal")
        self.assertTrue(self._read("arm-unreturned")["pending"], "unreturned id cannot settle")
        self.executor.flush()
        self.assertEqual(self.channels["pane-a"].calls + self.channels["pane-b"].calls, [], "late drives never act after terminal inbox return")
        self.assertFalse(self.store.reopen(role, int(self._read(role)["version"])), "terminal reopen refused")

    def test_replay_restart_and_parked_handoff_have_no_keys(self) -> None:
        """Reconstruct the store without changing durable state or routes.

        A parked successor depends on its existing hook and next model turn.
        Repeated sweeps must neither interrupt it nor amplify evidence history.
        """
        message = self._dispatch(role=True, unreachable=True)
        original = self._read(message)
        self.fake.update_state(NAMESPACE, {"table": "managed_session", "filters": {}}, {"lifecycle_state": "parked"})
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"agent_instance_id": "agi-b", "agent_session_id": "ases-b"})
        self.store = drives.CodexPickupStore(self.state)
        rider = SimpleNamespace(_get_state_service=lambda: self.state, _autonomic_assignment=None, _run_session_lifecycle_sweep=lambda: None, _run_rotation_surface_sweep=lambda: None)
        for _tick in range(5):
            plugin_module.AgentMessagingPlugin._on_sweep_tick(rider)  # noqa: SLF001 -- actual plugin-owned sweep composition
            self.executor.flush()
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"agent_instance_id": "agi-watch-b"})
        self.assertEqual(control.drive_on_delivery(self.state, recipient_agent_instance_id="agi-watch-b", recipient_agent_session_id="ases-b", sender_label="automatic rider"), "driver_pending")
        self.assertEqual(self.channels["pane-a"].calls + self.channels["pane-b"].calls, [], "replay/parked successor sees zero sends or interrupts")
        pending = self._read(message)
        self.assertEqual(pending["attempts"], 0)
        self.assertTrue(pending["pending"])
        self.assertEqual((pending["recipient_instance"], pending["recipient_session"]), (original["recipient_instance"], original["recipient_session"]))
        self.assertEqual(len(self.store.history(message)), 2)

    def test_codex_to_claude_handoff_at_executor_time(self) -> None:
        """Sender-side Codex routing is not act-time input authority.

        The executor starts after the binding becomes Claude. A stale runtime
        must not choose Codex drain, generic send, or parked interruption.
        """
        self.fake.update_state(NAMESPACE, {"table": "managed_session", "filters": {"agent_instance_id": "agi-a"}}, {"agent_runtime": "codex"})
        self._role("R", "agi-a", "ases-a")
        self._send("arm-cross-runtime", "R")
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"agent_instance_id": "agi-b", "agent_session_id": "ases-b", "holder_identity": {"agent_id": "claude_code", "session_label": "R"}})
        self.executor.flush()
        self.assertEqual(self.channels["pane-a"].calls + self.channels["pane-b"].calls, [], "queued Codex-to-Claude handoff emits zero keys")
        self.assertEqual(self._read("arm-cross-runtime")["phase"], "driver_pending")

    def test_claude_cas_and_event_rollback(self) -> None:
        """Projection and evidence writes roll back as one transaction.

        The original version loses its authority once first-doorbell evidence
        commits. Its stale correction cannot overwrite the retained state.
        """
        message = self._dispatch()
        original = self._read(message)
        self.fake.fail_event = True
        with self.assertRaisesRegex(RuntimeError, "event write failed"):
            self.store.change(original, phase="broken")
        self.fake.fail_event = False
        self.assertEqual(self._read(message), original)
        self.executor.flush()
        self.assertIsNone(self.store.change(original, phase="stale"), "Claude stale writes refused")
        self.assertEqual(self._read(message)["phase"], "driver_pending")

    def test_claude_first_role_doorbell_refreshes_current_holder(self) -> None:
        """The initial queued doorbell must refresh role authority at act time.

        This is separate from replay: a live initial doorbell is still allowed,
        but the original immutable route never authorizes the old holder's pane.
        """
        message = self._dispatch(role=True)
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"agent_instance_id": "agi-b", "agent_session_id": "ases-b"})
        self.executor.flush()
        self.assertEqual(self.channels["pane-a"].calls, [], "old holder receives no first-doorbell input")
        self.assertEqual(self.channels["pane-b"].calls, [("send", "delivery waiting from Sender — drain peer_inbox")])
        self.assertTrue(self._read(message)["pending"])

    def test_unresolved_claude_replay_does_not_amplify_history(self) -> None:
        """Unresolved current identity retains mail and one truthful advisory.

        Identical unavailable ticks add no facts; retaining them indefinitely
        would turn an absent consumer into an unbounded history writer.
        """
        message = self._dispatch(role=True, unreachable=True)
        self.fake.update_state(NAMESPACE, {"table": "role_binding", "filters": {}}, {"holder_identity": {"agent_id": "unsupported", "session_label": "R"}})
        for _tick in range(5):
            drives.sweep_codex_pickups(self.state)
        self.assertEqual(len(self.store.history(message)), 2, "identical unresolved ticks do not append history")
        self.assertTrue(self._read(message)["pending"])
        self.assertEqual(self.channels["pane-a"].calls + self.channels["pane-b"].calls, [])


class EventualPickupTests(_PickupFixture):
    def test_scheduled_is_not_consumed(self) -> None:
        self.assertEqual(self._send(), "driver_scheduled")
        self.assertTrue(self._read("arm-direct")["pending"], "W1: scheduling never proves inbox uptake")
        self.assertEqual(self._read("arm-direct")["phase"], "queued")
        self.assertIsNone(drives.finished_drive_outcome("arm-direct", timeout_s=0))
        self.assertEqual(self.panes["pane-a"].keys(), [])

    def test_busy_has_no_escape_or_text_then_idle_picks_up(self) -> None:
        self.panes["pane-a"].visible = BUSY
        self._send()
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].keys(), [], "W2: busy pane sees zero Escape, paste, or Enter")
        self.assertEqual(self._read("arm-direct")["attempts"], 0)
        self.assertEqual(self._read("arm-direct")["phase"], "driver_pending")
        self.panes["pane-a"].visible = IDLE
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 1)
        self.assertEqual(self._read("arm-direct")["phase"], "driver_turn_observed")

    def test_unsubmitted_doorbell_alarms(self) -> None:
        pane = self.panes["pane-a"]
        pane.lost_enters = 99
        self._send()
        with self.assertLogs(drives.logger, level="INFO") as captured:
            self.executor.flush()
        row = self._read("arm-direct")
        self.assertEqual(row["phase"], "driver_error", "WAKE_SUBMIT_UNCONFIRMED")
        self.assertTrue(row["pending"], "WAKE_SUBMIT_UNCONFIRMED durable mail")
        self.assertEqual(pane.enters, 2, "WAKE_SUBMIT_UNCONFIRMED two Enters")
        literals = [keys[-1] for keys in pane.keys() if "-l" in keys]
        self.assertEqual(len(literals), 1, "WAKE_SUBMIT_UNCONFIRMED one paste")
        self.assertEqual(pane.visible, f"› {literals[0]}\n? for shortcuts", "WAKE_SUBMIT_UNCONFIRMED owned composer")
        self.assertTrue(any(record.levelname == "WARNING" and all(part in record.getMessage() for part in ("agi-a", "ases-a", "submitted_no_turn")) for record in captured.records), "WAKE_SUBMIT_ALARM")

    def test_role_second_enter_and_exhaustion_cap(self) -> None:
        pane = self.panes["pane-a"]
        pane.lost_enters = 1
        self._send("arm-role-pass", "R")
        self.executor.flush()
        self.assertEqual(self._read("arm-role-pass")["phase"], "driver_turn_observed")
        self.assertEqual(pane.enters, 2)
        self.assertEqual(sum("-l" in keys for keys in pane.keys()), 1)
        self.store.change(self._read("arm-role-pass"), pending=False, phase="inbox_returned")
        pane = _Pane(lost_enters=99)
        self.panes["pane-a"] = pane
        self._send("arm-cap", "R")
        self.store.change(self._read("arm-cap"), attempts=drives.PICKUP_ATTEMPT_LIMIT - 1)
        with self.assertLogs(drives.logger, level="WARNING") as captured:
            self.executor.flush()
        self.assertTrue(any("pending_retry_limit" in line for line in captured.output))
        self.assertEqual(self._read("arm-cap")["attempts"], drives.PICKUP_ATTEMPT_LIMIT)
        self.assertTrue(self._read("arm-cap")["pending"])

    def test_confirmed_submission_pass_paths(self) -> None:
        for lost, visible, phase in ((0, BUSY, "driver_turn_observed"), (1, BUSY, "driver_turn_observed"), (0, IDLE, "driver_sent")):
            with self.subTest(lost=lost, visible=visible):
                pane = _Pane(lost_enters=lost)
                pane.submitted_visible = visible
                self.panes["pane-a"] = pane
                message = f"arm-pass-{lost}-{phase}"
                self._send(message)
                self.executor.flush()
                self.assertEqual(self._read(message)["phase"], phase)
                self.assertEqual(pane.enters, lost + 1)
                self.assertEqual(sum("-l" in keys for keys in pane.keys()), 1)
                self.store.change(self._read(message), pending=False, phase="inbox_returned")

    def test_idle_scheduled_miss_is_redriven(self) -> None:
        self.panes["pane-a"].lost_enters = 2
        self._send()
        self.executor.flush()
        self.assertEqual(self._read("arm-direct")["phase"], "driver_error")
        self.assertTrue(self._read("arm-direct")["pending"])
        self.assertEqual(self.panes["pane-a"].enters, 2)
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].visible, BUSY, "W3: missed drive recovered at next eligible tick")
        self.assertEqual(sum("-l" in keys for keys in self.panes["pane-a"].keys()), 1, "stranded own notice gets Enter, never a duplicate paste")
        page = self._service([self._message("arm-direct")]).peer_inbox(
            PeerInboxRequest(recipient_agent_id="codex", recipient_agent_instance_id="agi-a"),
        )
        self.assertEqual([entry.message.id for entry in page.entries], ["arm-direct"])
        self.assertEqual(page.entries[0].message.content[0].text, "exact direct body")
        self.assertEqual(self._read("arm-direct")["phase"], "inbox_returned")

    def test_role_handoff_resolves_successor_before_touching_pane(self) -> None:
        self._send("arm-role", "R")
        self._role("R", "agi-b", "ases-b")
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].keys(), [], "W4: stale role holder pane remains untouched")
        self.assertEqual(self.panes["pane-b"].enters, 1)
        self.assertEqual(resolve_role_binding(self.state, "R").agent_instance_id, "agi-b")

    def test_both_inbox_sections_settle_only_exact_returned_ids(self) -> None:
        for message, role in (("arm-direct", ""), ("arm-role", "R"), ("arm-unreturned", "")):
            self._send(message, role)
        service = self._service([self._message("arm-direct")])
        service.persist_role_message(
            recipient_kind="role",
            recipient_key="R",
            message_id="arm-role",
            sender_agent_id="codex",
            sender_agent_instance_id="agi-sender",
            sender_session_label="Sender",
            important=True,
            content=(TextPart(type="text", text="exact role body"),),
        )
        request = PeerInboxRequest(recipient_agent_id="codex", recipient_agent_instance_id="agi-a", recipient_agent_session_id="ases-a")
        page = service.peer_inbox(replace(request, observer=True))
        self.assertTrue(self._read("arm-direct")["pending"], "observer never records model uptake")
        self.assertTrue(self._read("arm-role")["pending"])
        page = service.peer_inbox(request)
        self.assertEqual([entry.message.id for entry in page.entries], ["arm-direct"])
        self.assertEqual([entry.message.id for entry in page.role_entries], ["arm-role"])
        self.assertEqual(page.role_entries[0].message.content[0].text, "exact role body")
        self.assertFalse(self._read("arm-direct")["pending"], "W5 direct section exact return recorded")
        self.assertFalse(self._read("arm-role")["pending"], "W5 role section exact return recorded")
        self.assertTrue(self._read("arm-unreturned")["pending"], "one successful page is not a full drain")
        self.executor.flush()
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 1, "only still-pending mail drives a turn")

    def test_replay_coalesces_without_flooding_or_peer_body_injection(self) -> None:
        self._send("arm-duplicate")
        self._send("arm-duplicate")
        self._send("arm-more", "R")
        self.assertEqual(len(self.executor.tasks), 1)
        self.executor.flush()
        literal = [keys[-1] for keys in self.panes["pane-a"].keys() if "-l" in keys]
        self.assertEqual(len(literal), 1)
        self.assertIn("solet-bridge inbox", literal[0])
        self.assertIn("both direct and role", literal[0])
        self.assertNotIn("untrusted", literal[0])
        self.assertNotIn("/clear", literal[0])
        self.assertEqual(len(self.fake.rows(NAMESPACE, drives.TABLE_DELIVERY_PICKUP)), 2)

    def test_unknown_pane_and_unproven_draft_remain_untouched(self) -> None:
        for visible in ("", "› somebody else's draft", "› Ask Codex to do anything\n› foreign draft"):
            pane = _Pane()
            pane.visible = visible
            self.assertTrue(pane.channel().send_drain_notice("fixed notice").evidence.startswith("pending"))
            self.assertEqual(pane.keys(), [])

    def test_queue_full_restart_and_stopped_executor_keep_pending_mail(self) -> None:
        with patch.object(drives, "DELIVERY_DRIVE_QUEUE_LIMIT", 0):
            self.assertEqual(self._send(), "driver_queue_full")
        self.assertTrue(self._read("arm-direct")["pending"])
        self.executor.closed = True
        drives.sweep_codex_pickups(self.state)
        self.assertEqual(drives.drive_queue_snapshot()["pending"], 0)
        self.executor.closed = False
        self.assertEqual(drives.sweep_codex_pickups(self.state), 1)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 1, "durable retry survives executor restart")

    def test_stale_write_rollback_terminal_and_retry_lifecycle(self) -> None:
        original = self.store.remember("arm-lifecycle", "agi-a", "ases-a", "")
        updated = self.store.change(original, phase="pending_retry_limit", attempts=3)
        assert updated is not None
        self.assertIsNone(self.store.change(original, phase="incorrect stale overwrite"))
        self.assertFalse(self.store.reopen("arm-lifecycle", 1))
        self.assertTrue(self.store.reopen("arm-lifecycle", int(str(updated["version"]))))
        current = self._read("arm-lifecycle")
        returned = self.store.change(current, pending=False, phase="inbox_returned", lease_until=None)
        assert returned is not None
        self.assertFalse(self.store.reopen("arm-lifecycle", int(str(returned["version"]))))
        self.assertIsNone(self.store.change(current, phase="late driver result"))
        before = self.store.read("arm-lifecycle")
        self.fake.fail_event = True
        with self.assertRaisesRegex(RuntimeError, "event write failed"):
            self.store.remember("arm-rollback", "agi-a", "ases-a", "")
        self.assertIsNone(self.store.read("arm-rollback"))
        self.assertEqual(self.store.read("arm-lifecycle"), before)
        self.assertEqual(self.fake.rollbacks, 1)
        self.assertEqual([entry["version"] for entry in self.store.history("arm-lifecycle")], [1, 2, 3, 4])


class PickupLifecycleTests(_PickupFixture):
    def test_sweep_rider_wiring_and_resource_shutdown(self) -> None:
        self._send()
        seen: list[str] = []
        fake_plugin = SimpleNamespace(
            _get_state_service=lambda: self.state,
            _autonomic_assignment=None,
            _run_session_lifecycle_sweep=lambda: seen.append("sessions"),
            _run_rotation_surface_sweep=lambda: seen.append("rotation"),
        )
        plugin_module.AgentMessagingPlugin._on_sweep_tick(cast(Any, fake_plugin))  # noqa: SLF001
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 1)
        self.assertEqual(seen, ["sessions", "rotation"])
        interval = plugin_module._BridgeRuntimeConfig().bridge_sweep_interval_seconds  # noqa: SLF001
        self.assertEqual(interval, 300)
        sweeper = BridgeLifecycleSweeper(
            bridge_manager=cast(Any, SimpleNamespace(sweep_idle=lambda: [])),
            cleanup=lambda _id: 0,
            interval_seconds=interval,
            on_tick=lambda: drives.sweep_codex_pickups(self.state),
        )
        try:
            sweeper.start()
            self.assertIsNotNone(sweeper._thread)  # noqa: SLF001
            sweeper.sweep_once()
        finally:
            sweeper.stop()
        self.assertIsNone(sweeper._thread)  # noqa: SLF001
        self.executor.flush()
        self._send("arm-cancelled")
        with patch.object(drives, "_executor_stopped", False):
            drives.stop_delivery_drives()
            self.assertEqual(drives.drive_queue_snapshot()["pending"], 0)
            self.assertTrue(self._read("arm-cancelled")["pending"])
            outcome = drives.finished_drive_outcome("arm-cancelled", timeout_s=0)
            self.assertIsNotNone(outcome)
            assert outcome is not None
            self.assertEqual(outcome[0], "driver_unavailable")

    def test_retry_limit_retains_mail_and_explicit_retry_uses_version(self) -> None:
        self.panes["pane-a"].lost_enters = 100
        self._send()
        self.executor.flush()
        for _tick in range(drives.PICKUP_ATTEMPT_LIMIT - 1):
            drives.sweep_codex_pickups(self.state)
            self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 6)
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        row = self._read("arm-direct")
        self.assertEqual(row["phase"], "pending_retry_limit")
        self.assertTrue(row["pending"])
        self.assertEqual(self.panes["pane-a"].enters, 6, "bounded retry never floods a missed composer")
        self.assertTrue(self.store.reopen("arm-direct", int(str(row["version"]))))
        self.assertFalse(self.store.reopen("arm-direct", int(str(row["version"]))))
        self.panes["pane-a"].lost_enters = 0
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 7)

    def test_unresolved_recipient_and_expired_lease_are_recoverable(self) -> None:
        self._send("arm-unresolved", "missing-role")
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertTrue(self._read("arm-unresolved")["pending"])
        self.assertEqual(self._read("arm-unresolved")["phase"], "pending_resolution")
        self.assertEqual(self.panes["pane-a"].keys(), [])
        self._role("missing-role", "agi-a", "ases-a")
        row = self._read("arm-unresolved")
        self.store.change(row, lease_until="2999-01-01T00:00:00+00:00")
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].keys(), [], "live lease prevents duplicate submission")
        self.store.change(self._read("arm-unresolved"), lease_until="2000-01-01T00:00:00+00:00")
        drives.sweep_codex_pickups(self.state)
        self.executor.flush()
        self.assertEqual(self.panes["pane-a"].enters, 1, "expired restart lease resumes retained mail")

    def test_act_time_handoff_after_drive_resolution_uses_current_host(self) -> None:
        self.panes["pane-c"] = _Pane()
        self._send("arm-role", "R")
        original = drives._pickup_drive  # noqa: SLF001

        def handoff(row: dict[str, object], state: StateManagementInterface) -> Any:
            drive = original(row, state)
            self._role("R", "agi-b", "ases-b")
            self._managed("agi-b", "ases-b", "pane-c")
            return drive

        with patch.object(drives, "_pickup_drive", side_effect=handoff):
            self.executor.flush()
        self.assertEqual(self.panes["pane-b"].keys(), [], "old successor host cache must not be used")
        self.assertEqual(self.panes["pane-a"].keys(), [], "stale instance pane must remain untouched")
        self.assertEqual(self.panes["pane-c"].enters, 1)
        self.assertEqual(self._read("arm-role")["phase"], "driver_turn_observed")

    def test_wrapped_own_notice_and_old_matching_transcript(self) -> None:
        row = self.store.remember("arm-wrap", "agi-a", "ases-a", "")
        notice = drives._pickup_notice(row)  # noqa: SLF001
        first, second = notice.split(" Run ", 1)
        pane = self.panes["pane-a"]
        pane.visible = f"› {first}\n  Run {second}\n? for shortcuts"
        self.assertEqual(pane.channel().send_drain_notice(notice).evidence, "turn_observed")
        self.assertEqual(len(pane.keys()), 1, "wrapped own notice gets only Enter")
        pane.calls.clear()
        pane.visible = f"› {notice}\nreply in transcript\n› someone else's draft\n? for shortcuts"
        self.assertEqual(pane.channel().send_drain_notice(notice).evidence, "pending_composer_shape")
        self.assertEqual(pane.keys(), [], "old matching transcript cannot authorize keys into a foreign draft")

    def test_pickup_history_continuation_and_schema_process_wiring(self) -> None:
        row = self.store.remember("arm-history", "agi-a", "ases-a", "")
        for index in range(drives.PICKUP_PAGE_LIMIT + 2):
            updated = self.store.change(row, reason=f"correction {index}", lease_until=None)
            assert updated is not None
            row = updated
        fake = SimpleNamespace(_get_state_service=lambda: self.state)
        page = plugin_module.AgentMessagingPlugin.session_status(fake, {"agent_instance_id": "agi-a"}, {})
        data = page["data"]["delivery_pickup"]["items"][0]
        self.assertEqual(len(data["history"]), drives.PICKUP_PAGE_LIMIT)
        self.assertEqual(data["next_history_version"], 64)
        next_page = self.store.history("arm-history", int(str(data["next_history_version"])))
        self.assertEqual([item["version"] for item in next_page], [65, 66, 67])
        self.assertFalse(page["data"]["delivery_pickup"]["scan_truncated"])
        schemas = plugin_module.AgentMessagingPlugin.get_schema_definitions(fake)
        self.assertTrue(any(drives.TABLE_DELIVERY_PICKUP in schema.tables for schema in schemas))

    def test_plugin_start_stop_uses_configured_cadence_and_owns_executor(self) -> None:
        seen: list[str] = []
        config = replace(
            plugin_module._BridgeRuntimeConfig(),  # noqa: SLF001
            bridge_sweep_interval_seconds=7,
            streamable_enabled=False,
        )
        registry = SimpleNamespace(list_agent_ids=lambda: {}, list_by_bridge=lambda _id: [], resolve_by_agent_session_id=lambda _id: None)
        event = SimpleNamespace(clear=lambda: None, wait=lambda **_kw: True)
        fake = SimpleNamespace(
            name="pickup-fixture",
            _server_thread=None,
            _server_started_event=event,
            _build_bridge_runtime_config=lambda: config,
            _mint_session_id=lambda: "fixture",
            _resolve_oauth_session_policy=lambda *_a, **_kw: None,
            _build_peer_registry=lambda: registry,
            _run_startup_backfills=lambda: None,
            _get_state_service=lambda: self.state,
            _has_live_inference_provider=lambda *_a: False,
            _send_handover_notice=lambda *_a: None,
            _forward_completion_request=lambda *_a: None,
            _resubmit_vertex=lambda *_a: None,
            _full_bridge_cleanup=lambda _id: 0,
            _build_platform_surface=lambda **_kw: None,
            _build_fastapi_app=lambda **_kw: None,
            _run_server=lambda: None,
            _router_is_declared=lambda: True,
            _teardown_streamable_server=lambda: seen.append("streamable-stop"),
            _teardown_bridge_server=lambda: seen.append("bridge-stop"),
            _run_session_lifecycle_sweep=lambda: None,
            _run_rotation_surface_sweep=lambda: None,
        )
        fake._on_sweep_tick = lambda: plugin_module.AgentMessagingPlugin._on_sweep_tick(fake)  # noqa: SLF001
        fake._shutdown_server = lambda: plugin_module.AgentMessagingPlugin._shutdown_server(fake)  # noqa: SLF001
        thread = SimpleNamespace(start=lambda: None, is_alive=lambda: False, join=lambda **_kw: None)
        with (
            patch.object(plugin_module.threading, "Thread", return_value=thread),
            patch.object(plugin_module, "find_available_port", return_value=0),
            patch.object(plugin_module, "start_delivery_drives", side_effect=lambda: seen.append("executor-start")),
            patch.object(plugin_module, "stop_delivery_drives", side_effect=lambda: seen.append("executor-stop")),
        ):
            result = plugin_module.AgentMessagingPlugin.start_interface(fake, {}, {})
            self.assertEqual(result["action_status"], "completed")
            self.assertEqual(fake._bridge_sweeper._interval_seconds, 7)  # noqa: SLF001
            self._send()
            fake._bridge_sweeper.sweep_once()  # noqa: SLF001
            self.executor.flush()
            self.assertEqual(self.panes["pane-a"].enters, 1, "startup-owned rider drives pending mail")
            plugin_module.AgentMessagingPlugin.stop_interface(fake, {}, {})
            self.assertIsNone(fake._bridge_sweeper)  # noqa: SLF001
        self.assertEqual(seen, ["executor-start", "executor-stop", "streamable-stop", "bridge-stop"])


class RealPostgresPickupTests(unittest.TestCase):
    """Owned private-PG regressions; reuse the repository's production-txn fixture.

    CODEX_PICKUP_TEST_PG_CONFIG must name an explicit loopback, private database
    config. This class never defaults to the shipped/live profile database.
    """

    def setUp(self) -> None:
        fixture_path = REPO_ROOT / "ananta/tests/llm/agent_messaging/repository_write_migration_live_smoke.py"
        spec = importlib.util.spec_from_file_location("pickup_live_state_fixture", fixture_path)
        assert spec is not None and spec.loader is not None
        live = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(live)
        config_data = json.loads(Path(os.environ["CODEX_PICKUP_TEST_PG_CONFIG"]).read_text())
        assert config_data["host"] == "127.0.0.1" and config_data["database"] == "pickup_rework"
        self.schema_name = f"example_test_pickup_{secrets.token_hex(6)}"
        self.provider = live.PostgresProvider(live.PostgresConfig(**config_data, pg_schema=self.schema_name))
        self.addCleanup(self.provider.close)
        self.provider.initialize()
        self.addCleanup(live._drop_schema, self.provider, self.schema_name)  # noqa: SLF001
        schema = live.SchemaStandardizer().standardize_schema(drives.get_delivery_pickup_schema())
        ops = [op for table in schema.tables.values() for op in live.emit_create_table_ops(NAMESPACE, table, self.schema_name)]
        with self.provider.get_transactional_connection() as conn, conn.cursor() as cur:
            self.provider.apply_schema_change_ops(cur, schema, ops)
        self.state = cast(StateManagementInterface, live._LiveStateAdapter(self.provider))  # noqa: SLF001
        self.store = drives.CodexPickupStore(self.state)

    def test_pg_projection_and_history_have_required_platform_fields(self) -> None:
        original = self.store.remember("pg-platform-fields", "agi-test", "ases-test", "")
        reread = self.store.read("pg-platform-fields")
        assert reread is not None
        self.assertEqual(reread["id"], original["id"])
        self.assertTrue(str(reread["id"]).startswith("dpk_"))
        self.assertEqual(reread["namespace"], NAMESPACE)
        self.assertEqual(self.store.remember("pg-platform-fields", "ignored", "ignored", ""), reread)
        changed = self.store.change(reread, phase="pending_busy", reason="owned fixture")
        assert changed is not None
        self.assertIsNone(self.store.change(reread, phase="stale"), "real CAS refuses stale writers")
        history = self.store.history("pg-platform-fields")
        self.assertEqual([row["version"] for row in history], [1, 2])
        self.assertEqual(len({row["id"] for row in history}), 2)
        for event in history:
            self.assertTrue(str(event["id"]).startswith("dpe_"))
            self.assertEqual(event["namespace"], NAMESPACE)
            self.assertEqual(event["evidence"]["id"], original["id"])
        self.assertEqual(history[0]["evidence"]["phase"], "queued")
        self.assertEqual(history[1]["evidence"]["phase"], "pending_busy")
        with patch.object(self.store, "_event", side_effect=RuntimeError("owned history fault")), self.assertRaisesRegex(RuntimeError, "owned history fault"):
            self.store.remember("pg-rollback", "agi-test", "ases-test", "")
        self.assertIsNone(self.store.read("pg-rollback"), "failed history insert rolls back real projection")
        self.assertEqual(self.store.history("pg-rollback"), [])

    def test_pg_naive_utc_lease_expiry_preserves_retry_boundary(self) -> None:
        # Explicit seed isolates DATETIME readback from the projection-insert bug.
        now = datetime.now(UTC)
        row = {
            "id": "dpk_pg-lease",
            "namespace": NAMESPACE,
            "external_id": "pickup:pg-lease",
            "message_id": "pg-lease",
            "recipient_instance": "agi-test",
            "recipient_session": "ases-test",
            "role_name": "",
            "pending": True,
            "phase": "driver_scheduled",
            "reason": "owned restart fixture",
            "attempts": 0,
            "version": 1,
            "checked_at": now.isoformat(),
            "lease_until": (now - timedelta(minutes=1)).isoformat(),
        }
        with self.state.transactional() as txn:
            txn.write_state(NAMESPACE, {"table": drives.TABLE_DELIVERY_PICKUP, "record": row})
        expired = self.store.read("pg-lease")
        assert expired is not None
        self.assertIsInstance(expired["lease_until"], datetime)
        self.assertIsNone(expired["lease_until"].tzinfo, "real platform DATETIME is naive UTC")
        self.assertTrue(drives._pickup_due(expired), "expired lease becomes retryable after restart")  # noqa: SLF001
        self.state.update_state(NAMESPACE, {"table": drives.TABLE_DELIVERY_PICKUP, "filters": {"id": row["id"]}}, {"lease_until": (now + timedelta(minutes=1)).isoformat()})
        future = self.store.read("pg-lease")
        assert future is not None
        self.assertFalse(drives._pickup_due(future), "unexpired lease still excludes concurrent drive")  # noqa: SLF001
        self.assertTrue(drives._pickup_due({**expired, "lease_until": None}))  # noqa: SLF001
        self.assertFalse(drives._pickup_due({**expired, "pending": False}))  # noqa: SLF001


def run_checks() -> bool:
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(test_class) for test_class in (EventualPickupTests, PickupLifecycleTests, ClaudePickupTests))
    if os.environ.get("CODEX_PICKUP_TEST_PG_CONFIG"):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RealPostgresPickupTests))
    else:
        print("PRIVATE-PG checks not run: set CODEX_PICKUP_TEST_PG_CONFIG to an owned loopback fixture config.")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


if __name__ == "__main__":
    sys.exit(0 if run_checks() else 1)
