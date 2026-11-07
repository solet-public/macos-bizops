"""Smoke: session teardown and launch run on their own lane, off the serial dispatch loop (iss_bc364208).

Re-measured on 2026-11-07: ``retire_session`` held the serial drain loop for
18.9 to 23.8 s on every retire, and the ``spawn_session`` submission for 10.1 to
16.1 s, against a 25 s bridge long poll. Every bridge call queued behind them
waited that long. These tests drive the REAL ``ActionQueuePoller._poll_once``
and ``_process_action``, with handlers on the real action worker threads. Only
the queue read, the claim and the terminal-write seams are replaced.

The timing test scales time 1:50 from the shipped
``long_poll_timeout_seconds`` (read from the plugin source, not restated), so
the hold is 1.0 s. It checks the ratio: a lifecycle handler holds for twice the
long poll while a bridge caller keeps submitting short calls, and every call
must finish inside one long poll. The same hold on a serial key is the control
and must make a call wait past it, which shows the measurement can see a drop.

Project policy: unittest, no pytest.

Run:
    .venv/bin/python ananta/tests/core/actions/session_lifecycle_lane_smoke.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

# Imports follow path selection so the shared venv never substitutes master's modules.
from ananta.core.actions import _measured_action_lane as lane  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402

assert Path(lane.__file__).resolve().is_relative_to(REPO_ROOT), lane.__file__

RETIRE = "plugin::agent_messaging_plugin::retire_session"
SPAWN = "plugin::agent_messaging_plugin::spawn_session"
SERIAL_CONTROL = "plugin::agent_messaging_plugin::rotate_session"
BRIDGE_CALL = "plugin::agent_messaging_plugin::peer_list"
FAST = "plugin::agent_messaging_plugin::report_alive"
SEED = "plugin::seed_factory_plugin::verify_seed_bundle_status"
TIME_SCALE = 50.0


def _shipped_long_poll_seconds() -> float:
    """The bridge's shipped ``long_poll_timeout_seconds`` default, read from source."""
    source = REPO_ROOT / "plugins/agent_messaging_plugin/src/agent_messaging_plugin/plugin.py"
    for node in ast.walk(ast.parse(source.read_text())):
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "long_poll_timeout_seconds" and isinstance(node.value, ast.Constant):
            return float(cast(int, node.value.value))
    raise AssertionError("long_poll_timeout_seconds default not found in the bridge config")


def _action(action_id: str, process_key: str) -> QueuedAction:
    return QueuedAction(id=action_id, process_key=process_key, parameters="{}", notes="", created_at="")


class _Poller:
    """A real poller whose handlers run on the real action workers; storage is in memory."""

    def __init__(self, execute: Any) -> None:
        self.queue: list[QueuedAction] = []
        self.claimed: list[str] = []
        self.completed: dict[str, float] = {}
        self.failed: dict[str, tuple[str, object]] = {}
        p = cast(Any, ActionQueuePoller.__new__(ActionQueuePoller))

        async def get_queued() -> list[QueuedAction]:
            return list(self.queue)

        def claim(action_id: str) -> bool:
            self.claimed.append(action_id)
            self.queue = [a for a in self.queue if a.id != action_id]
            return True

        def completed(action_id: str, _result: object, _token: object = None, on_status_written: Any = None) -> None:
            self.completed[action_id] = time.monotonic()
            if on_status_written is not None:
                on_status_written()

        def failed(action_id: str, message: str, *_args: object, error_detail: object = None, **_kwargs: object) -> None:
            self.failed[action_id] = (message, error_detail)

        p._get_queued_actions = get_queued
        p._mark_action_processing = claim
        p._is_terminated_flow_sibling = lambda _action: False
        p._prepare_action_for_execution = lambda _action: True
        p._resolve_io_context = lambda _action: None
        p._mark_action_completed = completed
        p._mark_action_failed = failed
        p.action_processor = type("Processor", (), {"execute_action": staticmethod(execute)})()
        p.total_actions_processed = 0
        p.total_poll_cycles = 0
        p._last_observed_queue_depth = 0
        self.poller = p

    async def cycle(self) -> None:
        await self.poller._poll_once()

    def slot_task(self, lane_name: str) -> asyncio.Task[None]:
        task = lane._states[self.poller].slots[lane_name].task
        assert task is not None
        return task


class SessionLifecycleLaneTest(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        for poller in list(lane._states):
            await lane.stop(poller)

    async def _bridge_latencies(self, heavy_key: str) -> tuple[list[float], float, float]:
        """Hold ``heavy_key`` for 2x the scaled long poll while a bridge caller keeps calling."""
        long_poll = _shipped_long_poll_seconds() / TIME_SCALE
        hold = 2 * long_poll
        release = threading.Event()

        def execute(action: QueuedAction) -> dict[str, object]:
            if action.process_key == heavy_key:
                release.wait(hold)
            return {"success": True, "data": {"id": action.id}}

        h = _Poller(execute)
        h.queue.append(_action("ae-heavy", heavy_key))
        submitted: dict[str, float] = {}
        started = time.monotonic()
        polling = asyncio.create_task(self._drive(h, lambda: time.monotonic() - started > hold + long_poll))
        for n in range(8):
            call_id = f"ae-call-{n}"
            submitted[call_id] = time.monotonic()
            h.queue.append(_action(call_id, BRIDGE_CALL))
            await asyncio.sleep(hold / 8)
        await polling
        release.set()
        self.assertIn("ae-heavy", h.completed, "the held handler itself completed through the same writes")
        heavy_held = h.completed["ae-heavy"] - started
        latencies = [h.completed.get(call_id, float("inf")) - at for call_id, at in submitted.items()]
        return latencies, heavy_held, long_poll

    @staticmethod
    async def _drive(h: _Poller, done: Any) -> None:
        while not done():
            await h.cycle()
            await asyncio.sleep(0.01)
        for slot in list(lane._states.get(h.poller, lane._State()).slots.values()):
            if slot.task is not None:
                await slot.task

    async def test_bridge_calls_are_served_while_a_retire_holds_twice_the_long_poll(self) -> None:
        latencies, held, long_poll = await self._bridge_latencies(RETIRE)
        self.assertGreaterEqual(held, 2 * long_poll * 0.95, "the retire really held for twice the long poll")
        self.assertLess(max(latencies), long_poll, f"every bridge call finished inside one long poll: {latencies}")

    async def test_control_the_same_hold_on_a_serial_key_drops_a_bridge_call(self) -> None:
        self.assertNotIn(SERIAL_CONTROL, lane.PROCESS_KEYS)
        latencies, _held, long_poll = await self._bridge_latencies(SERIAL_CONTROL)
        self.assertGreater(max(latencies), long_poll, f"control: a serial hold makes a bridge call wait past the long poll: {latencies}")

    async def test_failing_detached_action_reports_its_error(self) -> None:
        detail = {"code": "worktree_retained", "message": "lane tree is dirty"}

        def execute(action: QueuedAction) -> dict[str, object]:
            if action.id == "ae-raises":
                raise RuntimeError("host refused termination")
            return {"success": False, "error": detail}

        h = _Poller(execute)
        h.queue.extend([_action("ae-refused", RETIRE), _action("ae-raises", SPAWN)])
        await h.cycle()
        await h.slot_task("session_lifecycle")
        await h.cycle()
        await h.slot_task("session_lifecycle")
        self.assertEqual(h.claimed, ["ae-refused", "ae-raises"])
        self.assertEqual(h.failed["ae-refused"][1], detail, "the typed handler error is recorded on the row")
        self.assertIn("lane tree is dirty", h.failed["ae-refused"][0])
        self.assertIn("host refused termination", h.failed["ae-raises"][0], "a raising handler is recorded, not swallowed")
        self.assertEqual(h.completed, {})
        self.assertEqual(lane.live_ids(h.poller), frozenset())

    async def test_lifecycle_rows_stay_fifo_and_lanes_are_independent(self) -> None:
        gates = {key: threading.Event() for key in (SPAWN, RETIRE, SEED)}
        entered: list[str] = []

        def execute(action: QueuedAction) -> dict[str, object]:
            entered.append(action.id)
            if action.process_key in gates:
                gates[action.process_key].wait(5)
            return {"success": True}

        h = _Poller(execute)
        h.queue.extend([_action("ae-seed", SEED), _action("ae-spawn", SPAWN), _action("ae-retire", RETIRE), _action("ae-fast-1", FAST), _action("ae-fast-2", FAST)])
        await h.cycle()
        self.assertEqual(h.claimed, ["ae-seed", "ae-spawn", "ae-fast-1", "ae-fast-2"], "the retire waits behind the earlier spawn, unclaimed")
        self.assertEqual([a.id for a in h.queue], ["ae-retire"])
        self.assertEqual(list(h.completed), ["ae-fast-1", "ae-fast-2"], "ordinary rows keep their serial order and do not wait")
        gates[SPAWN].set()
        await h.slot_task("session_lifecycle")
        await h.cycle()
        self.assertEqual(h.claimed[-1], "ae-retire", "a running seed action does not hold the lifecycle lane")
        self.assertFalse(h.slot_task("measured").done(), "the seed action is still running")
        gates[RETIRE].set()
        gates[SEED].set()
        await h.slot_task("session_lifecycle")
        await h.slot_task("measured")
        self.assertLess(entered.index("ae-spawn"), entered.index("ae-retire"))
        self.assertEqual(set(h.completed), {"ae-seed", "ae-spawn", "ae-retire", "ae-fast-1", "ae-fast-2"})

    async def test_busy_page_skips_only_the_running_lanes_rows(self) -> None:
        hold = threading.Event()

        def execute(_action: QueuedAction) -> dict[str, object]:
            hold.wait(5)
            return {"success": True}

        h = _Poller(execute)
        rows = [
            {"id": "ae-retire-2", "sequence": 1, "process_key": RETIRE},
            {"id": "ae-seed", "sequence": 2, "process_key": SEED},
            {"id": "ae-fast", "sequence": 3, "process_key": FAST},
        ]
        page = {"action_status": "completed", "data": {"records": rows}}
        h.poller.state_service = type("State", (), {"query_ordered": staticmethod(lambda _ns, _data: page)})()
        h.queue.append(_action("ae-retire-1", RETIRE))
        await h.cycle()
        try:
            result = await lane.query_ordered(h.poller, "core", {"table": "action_events"})
            eligible = [row["id"] for row in cast(dict[str, Any], result.get("data"))["records"]]
            self.assertEqual(eligible, ["ae-seed", "ae-fast"], "only the running lifecycle lane's row is skipped")
        finally:
            hold.set()
            await h.slot_task("session_lifecycle")

    async def test_lane_that_finishes_rereads_its_skipped_rows_oldest_first(self) -> None:
        """Review R1 (uev_57e4d79d): the cursor skipped an old retire while both lanes ran.

        After the lifecycle lane finishes with the seed lane still running, the old
        retire must come back before the newer one, never be left behind the cursor.
        """
        rows = [
            {"id": "ae-old-retire", "sequence": 1, "process_key": RETIRE},
            {"id": "ae-new-retire", "sequence": 2, "process_key": RETIRE},
        ]

        def query(_namespace: str, data: dict[str, object]) -> dict[str, object]:
            after = tuple(cast(list[object], data.get("after", [0, ""])))
            newer = [row for row in rows if (row["sequence"], row["id"]) > after]
            return {"action_status": "completed", "data": {"records": newer[:1]}}

        h = _Poller(lambda _action: {"success": True})
        h.poller.state_service = type("State", (), {"query_ordered": staticmethod(query)})()
        seed_release, retire_release = asyncio.Event(), asyncio.Event()

        async def running(release: asyncio.Event) -> None:
            await release.wait()

        seed_task = asyncio.create_task(running(seed_release))
        retire_task = asyncio.create_task(running(retire_release))
        state = lane._State(slots={"measured": lane._Slot(task=seed_task), "session_lifecycle": lane._Slot(task=retire_task)})
        lane._states[h.poller] = state
        try:
            first = await lane.query_ordered(h.poller, "core", {"limit": 1})
            self.assertEqual(cast(dict[str, Any], first.get("data"))["records"], [])
            self.assertEqual(state.cursor, (1, "ae-old-retire"), "the busy read paged past the running lane's row")
            retire_release.set()
            await retire_task
            lane.report(h.poller)
            self.assertFalse(seed_task.done(), "the seed lane is still running")
            second = await lane.query_ordered(h.poller, "core", {"limit": 1})
            returned = [row["id"] for row in cast(dict[str, Any], second.get("data"))["records"]]
            self.assertEqual(returned, ["ae-old-retire"], "the older lifecycle row precedes the newer one")
        finally:
            seed_release.set()
            retire_release.set()
            await asyncio.gather(seed_task, retire_task)
            lane._states.pop(h.poller, None)

    async def test_fast_only_queue_is_unchanged(self) -> None:
        order: list[str] = []

        def execute(action: QueuedAction) -> dict[str, object]:
            order.append(action.id)
            return {"success": True}

        h = _Poller(execute)
        h.queue.extend(_action(f"ae-{n}", FAST) for n in range(5))
        await h.cycle()
        self.assertEqual(order, [f"ae-{n}" for n in range(5)])
        self.assertEqual(list(h.completed), order)
        self.assertNotIn(h.poller, lane._states, "ordinary rows create no lane state")

    def test_lifecycle_keys_are_shipped_and_disjoint(self) -> None:
        declared = {json.loads(p.read_text()).get("process_key") for p in (REPO_ROOT / "plugins/agent_messaging_plugin/knowledge_base/processes").glob("*.json")}
        self.assertLessEqual(lane.SESSION_LIFECYCLE_PROCESS_KEYS, declared)
        self.assertFalse(lane.SESSION_LIFECYCLE_PROCESS_KEYS & lane.MEASURED_PROCESS_KEYS)
        self.assertNotIn("plugin::agent_messaging_plugin::deliver_result", lane.PROCESS_KEYS, "bridge delivery stays serial")
        self.assertNotIn("service_interface::memory_service::recompute_strengths_cron", lane.PROCESS_KEYS, "whole-row memory upserts stay serial")


if __name__ == "__main__":
    unittest.main()
