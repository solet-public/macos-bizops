#!/usr/bin/env python3
"""Offline plugin-path handler wall-time smoke; no live solet or database.

Drives real plugin actions through ``action_execution.execute`` against a
fresh, real ``HandlerEntryRecorder`` (nothing about the recorder is mocked) and
reads the advisory ``handler_wall_time`` evidence back from its snapshot:

  (a) a synchronous action sleeping 0.2 s records count 1 and max >= 0.2 s;
  (b) an async action awaiting 0.1 s records likewise;
  (c) a raising action still records its time and returns the same
      standardized error response;
  (d) a key with handler entries but no timed call keeps the existing entry
      shape and gains no invented wall-time row;
  (e) more timed keys than the recorder's bound neither raise nor change the
      response; the least-recently-recorded key is evicted and counted.

Run: .venv/bin/python3 ananta/tests/core/plugins/handler_wall_time_smoke.py
"""

from __future__ import annotations

import asyncio
import sys
import time
import unittest
from pathlib import Path
from typing import cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta/src"))

from ananta.core.domain.enums import ErrorSeverity  # noqa: E402
from ananta.core.plugins import action_execution  # noqa: E402
from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.core.plugins.plugin_contracts import ActionStatus  # noqa: E402
from ananta.core.runtime import loop_invariant  # noqa: E402
from ananta.error_handling import PluginError  # noqa: E402

PLUGIN_NAME = "wall_time_probe_plugin"
SYNC_SLEEP_S = 0.2
ASYNC_SLEEP_S = 0.1
# asyncio fires a timer up to one clock-resolution tick early; perf_counter can
# then read a hair under the requested delay. Synchronous sleep has no slack.
ASYNC_TIMER_SLACK_S = 0.001
# A sanity ceiling that catches a unit error (milliseconds recorded as seconds).
SANE_CEILING_S = 5.0
ENTRY_FIELDS = {"sequence", "process_key", "kind", "thread_id", "on_loop", "recorded_at", "dispatch_only_reason", "stack"}
SNAPSHOT_FIELDS = {"invocations", "capacity", "recent_entries", "violations", "handler_wall_time"}
WALL_TIME_FIELDS = {"count", "total_s", "max_s", "last_s"}


def _key(action_name: str) -> str:
    return f"plugin::{PLUGIN_NAME}::{action_name}"


class _ProbePlugin:
    """Stand-in plugin carrying only what ``action_execution.execute`` reads."""

    name = PLUGIN_NAME
    validation_registry = None

    def __init__(self) -> None:
        self._current_action_name: str | None = None
        self.raised: PluginError | None = None

    def slow_sync(self, _parameters: dict[str, object]) -> dict[str, object]:
        time.sleep(SYNC_SLEEP_S)
        return {"slept_s": SYNC_SLEEP_S}

    async def slow_async(self, _parameters: dict[str, object]) -> dict[str, object]:
        await asyncio.sleep(ASYNC_SLEEP_S)
        return {"awaited_s": ASYNC_SLEEP_S}

    def raises(self, _parameters: dict[str, object]) -> dict[str, object]:
        time.sleep(0.05)
        self.raised = PluginError(message="probe failure", error_code="plugin.probe_failed", details={"plugin": PLUGIN_NAME}, severity=ErrorSeverity.ERROR)
        raise self.raised

    def quick_a(self, _parameters: dict[str, object]) -> dict[str, object]:
        return {"reached": "a"}

    def quick_b(self, _parameters: dict[str, object]) -> dict[str, object]:
        return {"reached": "b"}

    def quick_c(self, _parameters: dict[str, object]) -> dict[str, object]:
        return {"reached": "c"}


def _execute(plugin: _ProbePlugin, action_name: str) -> dict[str, object]:
    return asyncio.run(action_execution.execute(cast(PluginBase, plugin), action_name, {}))


class HandlerWallTimeSmoke(unittest.TestCase):
    def _use_recorder(self, capacity: int = loop_invariant.DEFAULT_ENTRY_CAPACITY) -> loop_invariant.HandlerEntryRecorder:
        recorder = loop_invariant.HandlerEntryRecorder(config=loop_invariant.HandlerEntryConfig(entry_capacity=capacity))
        replacement = patch.object(loop_invariant, "HANDLER_ENTRIES", recorder)
        replacement.start()
        self.addCleanup(replacement.stop)
        return recorder

    def _row(self, recorder: loop_invariant.HandlerEntryRecorder, action_name: str) -> dict[str, object]:
        snapshot = cast(dict[str, object], recorder.snapshot())
        wall_time = cast(dict[str, object], snapshot.get("handler_wall_time", {}))
        rows = cast(dict[str, dict[str, object]], wall_time.get("by_process_key", {}))
        row = rows.get(_key(action_name))
        self.assertIsNotNone(row, f"no handler wall-time row recorded for {_key(action_name)}")
        return cast(dict[str, object], row)

    def _assert_single_call(self, row: dict[str, object], floor_s: float) -> None:
        self.assertEqual(set(row), WALL_TIME_FIELDS)
        self.assertEqual(row["count"], 1)
        max_s = cast(float, row["max_s"])
        self.assertGreaterEqual(max_s, floor_s)
        self.assertLess(max_s, SANE_CEILING_S)
        self.assertEqual(row["total_s"], max_s)
        self.assertEqual(row["last_s"], max_s)

    def test_a_sync_action_wall_time_is_recorded(self) -> None:
        recorder = self._use_recorder()
        response = _execute(_ProbePlugin(), "slow_sync")
        self.assertEqual(response, {"slept_s": SYNC_SLEEP_S, "action_status": ActionStatus.COMPLETED.value})
        row = self._row(recorder, "slow_sync")
        self._assert_single_call(row, SYNC_SLEEP_S)
        print(f"SYNC_WALL_TIME process_key={_key('slow_sync')} row={row}")

    def test_b_async_action_wall_time_is_recorded(self) -> None:
        recorder = self._use_recorder()
        response = _execute(_ProbePlugin(), "slow_async")
        self.assertEqual(response, {"awaited_s": ASYNC_SLEEP_S, "action_status": ActionStatus.COMPLETED.value})
        row = self._row(recorder, "slow_async")
        self._assert_single_call(row, ASYNC_SLEEP_S - ASYNC_TIMER_SLACK_S)
        print(f"ASYNC_WALL_TIME process_key={_key('slow_async')} row={row}")

    def test_c_raising_action_records_time_and_keeps_error_response(self) -> None:
        recorder = self._use_recorder()
        plugin = _ProbePlugin()
        response = _execute(plugin, "raises")
        self.assertIsNotNone(plugin.raised)
        raised = cast(PluginError, plugin.raised)
        self.assertEqual(response, action_execution._create_error_response(raised))
        self.assertEqual(
            response,
            {"error": str(raised), "action_status": ActionStatus.ERROR.value, "error_code": "plugin.probe_failed", "error_type": raised.error_type},
        )
        row = self._row(recorder, "raises")
        self._assert_single_call(row, 0.05)
        print(f"RAISING_WALL_TIME process_key={_key('raises')} row={row}")

    def test_d_untouched_key_keeps_existing_shape(self) -> None:
        recorder = self._use_recorder()
        untouched = "route::wall_time_probe::untouched"
        loop_invariant.record_handler_entry(untouched, "route")
        _execute(_ProbePlugin(), "quick_a")
        snapshot = recorder.snapshot()
        self.assertEqual(set(snapshot), SNAPSHOT_FIELDS)
        self.assertEqual(set(snapshot["violations"]), {"count", "last_process_key", "last_stack", "recent"})
        entries = [entry for entry in snapshot["recent_entries"] if entry["process_key"] == untouched]
        self.assertEqual(len(entries), 1)
        self.assertEqual(set(entries[0]), ENTRY_FIELDS)
        self.assertEqual(entries[0]["kind"], "route")
        self.assertNotIn(untouched, snapshot["handler_wall_time"]["by_process_key"])
        self.assertEqual(set(snapshot["handler_wall_time"]["by_process_key"]), {_key("quick_a")})
        self.assertEqual(snapshot["handler_wall_time"]["evicted_keys"], 0)

    def test_e_keys_beyond_bound_do_not_raise_or_change_response(self) -> None:
        recorder = self._use_recorder(capacity=2)
        plugin = _ProbePlugin()
        responses = [_execute(plugin, name) for name in ("quick_a", "quick_b", "quick_c")]
        self.assertEqual(responses, [{"reached": letter, "action_status": ActionStatus.COMPLETED.value} for letter in ("a", "b", "c")])
        wall_time = recorder.snapshot()["handler_wall_time"]
        self.assertEqual(list(wall_time["by_process_key"]), [_key("quick_b"), _key("quick_c")])
        self.assertEqual(wall_time["evicted_keys"], 1)
        _execute(plugin, "quick_b")
        wall_time = recorder.snapshot()["handler_wall_time"]
        self.assertEqual(list(wall_time["by_process_key"]), [_key("quick_c"), _key("quick_b")], "a re-recorded key becomes most recent")
        self.assertEqual(wall_time["by_process_key"][_key("quick_b")]["count"], 2)
        self.assertEqual(wall_time["evicted_keys"], 1)
        print(f"BOUNDED_WALL_TIME keys={list(wall_time['by_process_key'])} evicted={wall_time['evicted_keys']}")


if __name__ == "__main__":
    unittest.main()
