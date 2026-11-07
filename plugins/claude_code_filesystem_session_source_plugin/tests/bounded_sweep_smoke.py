"""Bounded Claude filesystem discovery: full population, pacing and lifecycle."""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "claude_code_filesystem_session_source_plugin" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "session_bridge_plugin" / "src"))

from claude_code_filesystem_session_source_plugin.plugin import ClaudeCodeFilesystemSessionSourcePlugin  # noqa: E402


def _make_plugin() -> ClaudeCodeFilesystemSessionSourcePlugin:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    plugin.initialize({"glob": "*.jsonl"})
    return plugin


def _plant(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")


class _SweepClock:
    def __init__(self, step: float = 0.0) -> None:
        self.value = 0.0
        self.step = step
        self.pauses: list[float] = []

    def now(self) -> float:
        self.value += self.step
        return self.value

    def pause(self, duration: float) -> None:
        self.pauses.append(duration)
        self.value += duration


class SweepBudgetTests(unittest.TestCase):
    """A full population must yield within a directory, not only between directories."""

    def test_setting_set_correct_clear_and_invalid(self) -> None:
        from unittest.mock import patch

        from claude_code_filesystem_session_source_plugin import plugin as module

        name = "SOLET_CLAUDE_SWEEP_ENTRY_LIMIT"
        with patch.dict(os.environ, {name: "64"}):
            self.assertEqual(module._sweep_setting(name, 128, integral=True), 64)
            os.environ[name] = "32"
            self.assertEqual(module._sweep_setting(name, 128, integral=True), 32)
            for invalid in ("0", "-1", "nan", "inf", "1.5", "unknown"):
                os.environ[name] = invalid
                with self.assertRaisesRegex(ValueError, name):
                    module._sweep_setting(name, 128, integral=True)
            del os.environ[name]
            self.assertEqual(module._sweep_setting(name, 128, integral=True), 128)

    def test_large_directory_yields_before_its_end(self) -> None:
        from unittest.mock import patch

        from claude_code_filesystem_session_source_plugin import plugin as module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            for index in range(513):
                (project / f"session-{index}.jsonl").write_text("{}\n")
            pauses: list[float] = []
            with (
                patch.object(module, "monotonic", return_value=0.0, create=True),
                patch.object(
                    module,
                    "sleep",
                    side_effect=pauses.append,
                    create=True,
                ),
            ):
                found = list(_make_plugin()._poll_candidates(str(root)))
            self.assertEqual(len(found), 513, "bounded discovery must retain every session")
            self.assertGreaterEqual(len(pauses), 4, "SWEEP_ENTRY_BOUND: yield at least every 128 directory entries")

    def test_elapsed_budget_and_same_root_interval(self) -> None:
        from unittest.mock import patch

        from claude_code_filesystem_session_source_plugin import plugin as module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "project" / "session.jsonl"
            _plant(path)
            plugin = _make_plugin()
            clock = _SweepClock(step=0.003)
            with patch.object(module, "monotonic", clock.now), patch.object(module, "sleep", clock.pause):
                first = list(plugin._poll_candidates(str(root)))
                self.assertTrue(clock.pauses, "elapsed budget must yield even below the entry bound")
                clock.step = 0.0
                clock.pauses.clear()
                _plant(path.parent / "appended.jsonl")
                second = list(plugin._poll_candidates(str(root)))
            self.assertEqual(len(first), 1)
            self.assertEqual(len(second), 2, "interval must not cache or hide newly created sessions")
            self.assertEqual(len(clock.pauses), 1)
            self.assertAlmostEqual(clock.pauses[0], 1.0, msg="a repeated root waits after the preceding sweep finishes")

    def test_population_matches_recursive_glob(self) -> None:
        from unittest.mock import patch

        from claude_code_filesystem_session_source_plugin import plugin as module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            names = ("project/root.jsonl", "project/root/subagents/agent-child.jsonl", "project/root/subagents/ignored.jsonl", "project/root/other/subagents/agent-deep.jsonl")
            for name in names:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n")
            (root / "linked-project").symlink_to(root / "project", target_is_directory=True)
            for pattern in ("*.jsonl", "**/*.jsonl", "*/subagents/agent-*.jsonl", "root.jsonl"):
                expected = {path.relative_to(root).as_posix() for path in root.rglob(pattern) if module._classify_session_path(path, root) is not None}
                plugin = _make_plugin()
                plugin.initialize({"glob": pattern})
                with patch.object(module, "sleep"):
                    actual = {candidate.relative_path for candidate in plugin._poll_candidates(str(root))}
                self.assertEqual(actual, expected, pattern)

    def test_partial_close_releases_sweep_and_reinitialization_clears_interval(self) -> None:
        from unittest.mock import patch

        from claude_code_filesystem_session_source_plugin import plugin as module

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _plant(root / "project" / "session.jsonl")
            plugin = _make_plugin()
            clock = _SweepClock()
            with patch.object(module, "monotonic", clock.now), patch.object(module, "sleep", clock.pause):
                iterator = plugin._poll_candidates(str(root))
                next(iterator)
                iterator.close()
                self.assertTrue(plugin._sweep_lock.acquire(blocking=False), "abandoning discovery must release its admission lock")
                plugin._sweep_lock.release()
                plugin.initialize({"glob": "*.jsonl"})
                self.assertEqual(len(list(plugin._poll_candidates(str(root)))), 1)
                self.assertFalse(clock.pauses, "a new configuration starts a fresh cadence")


class OffLoopEntryTests(unittest.TestCase):
    """Synchronous public calls must execute actual work on their dedicated slot."""

    def test_direct_poll_source_runs_off_caller(self) -> None:
        from ananta.services.session_ledger_service.service import SessionLedgerService, _build_drain_executors

        owner = threading.get_ident()
        called: list[int] = []
        names: list[str] = []

        def poll(source_id: str) -> SimpleNamespace:
            self.assertEqual(source_id, "fixture")
            called.append(threading.get_ident())
            names.append(threading.current_thread().name)
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return SimpleNamespace(sources_polled=1, sessions_seen=2, events_persisted=3, batches_failed=0)

        service = cast(Any, object.__new__(SessionLedgerService))
        service._importer = SimpleNamespace(poll_source=poll)
        service._poll_executor = _build_drain_executors(None)[2]

        async def call() -> None:
            self.assertEqual(service.poll_source("fixture")["events_persisted"], 3)

        asyncio.run(call())
        self.assertEqual(len(called), 1)
        self.assertNotEqual(called[0], owner, "POLL_OFF_LOOP: importer ran on its caller loop thread")
        self.assertEqual(names, ["ledger-poll"])

    def test_direct_spool_drain_runs_off_caller(self) -> None:
        from session_bridge_plugin.plugin import SessionBridgePlugin

        owner = threading.get_ident()
        called: list[int] = []
        names: list[str] = []

        def drain(_memory: object) -> int:
            called.append(threading.get_ident())
            names.append(threading.current_thread().name)
            with self.assertRaises(RuntimeError):
                asyncio.get_running_loop()
            return 7

        with patch.dict(os.environ, {"SOLET_NAME": "fixture"}):
            plugin = cast(Any, SessionBridgePlugin())
        plugin._memory_service = object()
        plugin._drainer = SimpleNamespace(drain_once=drain)

        async def call() -> None:
            try:
                self.assertEqual(plugin.drain_once(), 7)
            finally:
                await plugin.stop_services()

        asyncio.run(call())
        self.assertEqual(len(called), 1)
        self.assertNotEqual(called[0], owner, "SPOOL_OFF_LOOP: drainer ran on its caller loop thread")
        self.assertEqual(names, ["session-bridge-drain_0"])

    def test_busy_poll_slot_queues_then_preserves_failure_and_reentry(self) -> None:
        from ananta.services.session_ledger_service.service import _LedgerPollExecutor

        executor = _LedgerPollExecutor()
        entered, release, waiting, finished = (threading.Event() for _ in range(4))
        results: list[int] = []

        def hold() -> None:
            entered.set()
            self.assertTrue(release.wait(2))

        self.assertTrue(executor.submit(hold))
        self.assertTrue(entered.wait(2))
        self.assertFalse(executor.submit(lambda: None), "heartbeat remains an already-running no-op")
        original_wait = executor._condition.wait_for

        def wait_for(predicate: Any) -> bool:
            waiting.set()
            return original_wait(predicate)

        def call() -> None:
            results.append(executor.call(lambda: executor.call(lambda: 7)))
            finished.set()

        caller = threading.Thread(target=call, daemon=True)
        try:
            with patch.object(executor._condition, "wait_for", wait_for):
                caller.start()
                self.assertTrue(waiting.wait(2), "direct call never reached occupied-slot admission")
                self.assertFalse(finished.is_set(), "direct call ran concurrently with the heartbeat")
        finally:
            release.set()
            caller.join(2)
        self.assertFalse(caller.is_alive())
        self.assertEqual(results, [7], "busy calls and worker reentry must retain their result")

        def fail() -> None:
            raise ValueError("source failure")

        with self.assertRaisesRegex(ValueError, "source failure"):
            executor.call(fail)
        self.assertEqual(executor.call(lambda: 9), 9, "failure must release the slot")

    def test_spool_worker_stops_reopens_and_honors_inactive_color(self) -> None:
        from session_bridge_plugin.plugin import SessionBridgePlugin

        with patch.dict(os.environ, {"SOLET_NAME": "fixture"}):
            plugin = cast(Any, SessionBridgePlugin())
        called: list[int] = []

        def drain(_memory: object) -> int:
            called.append(threading.get_ident())
            return 3

        plugin._memory_service = object()
        plugin._drainer = SimpleNamespace(drain_once=drain)

        async def lifecycle() -> None:
            try:
                self.assertEqual(plugin.drain_once(), 3)
                first = plugin._drain_executor
                await plugin.stop_services()
                self.assertIsNone(plugin._drain_executor)
                self.assertEqual(plugin.drain_once(), 3)
                self.assertIsNot(plugin._drain_executor, first)
                plugin.set_active(False)
                self.assertEqual(plugin.drain_once(), 0)
                self.assertEqual(len(called), 2)
                plugin.set_active(True)
                self.assertEqual(plugin.drain_once(), 3)
                await plugin.start_services()
                await plugin.stop_services()
                self.assertFalse(plugin._services_started)
                self.assertIsNone(plugin._drain_executor)
            finally:
                await plugin.stop_services()

        asyncio.run(lifecycle())
        self.assertTrue(all(worker != threading.get_ident() for worker in called))


class SpoolEnumerationTests(unittest.TestCase):
    def test_disappearing_spool_is_an_availability_advisory(self) -> None:
        from session_bridge_plugin import janitor

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(janitor.os, "scandir", side_effect=FileNotFoundError("spool disappeared")):
                with self.assertLogs("session_bridge_plugin.janitor", level="WARNING"):
                    self.assertEqual(janitor.list_spool_files(Path(tmp)), [])

    def test_large_spool_yields_without_truncating(self) -> None:
        from session_bridge_plugin import janitor

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(513):
                (root / f"{index:08d}.jsonl").write_text("{}\n")
            pauses: list[float] = []
            with patch.object(janitor, "monotonic", return_value=0.0, create=True), patch.object(janitor, "sleep", side_effect=pauses.append, create=True):
                files = janitor.list_spool_files(root)
            self.assertEqual(len(files), 513)
            self.assertGreaterEqual(len(pauses), 4, "SPOOL_ENTRY_BOUND: yield within a large directory")

    def test_spool_configuration_and_retention_still_preserve_watermark(self) -> None:
        from session_bridge_plugin import janitor

        name = "SOLET_SESSION_SPOOL_ENTRY_LIMIT"
        with patch.dict(os.environ, {name: "64"}):
            self.assertEqual(janitor.spool_setting(name, 128, integral=True), 64)
            os.environ[name] = "32"
            self.assertEqual(janitor.spool_setting(name, 128, integral=True), 32)
            os.environ[name] = "nan"
            with self.assertRaisesRegex(ValueError, name):
                janitor.spool_setting(name, 128, integral=True)
            del os.environ[name]
            self.assertEqual(janitor.spool_setting(name, 128, integral=True), 128)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(129):
                (root / f"{index:08d}.jsonl").write_text("{}\n")
            pauses: list[float] = []
            with patch.object(janitor, "monotonic", return_value=0.0), patch.object(janitor, "sleep", side_effect=pauses.append):
                files = janitor.list_spool_files(root)
                deleted = janitor._delete_below(files, files[-1].name)
            self.assertEqual(len(deleted), 128)
            self.assertEqual(list(root.iterdir()), [files[-1]], "watermark anchor must survive paced deletion")
            self.assertGreaterEqual(len(pauses), 2, "both enumeration and deletion must yield")


if __name__ == "__main__":
    unittest.main()
