#!/usr/bin/env python3
"""Offline loop-lag smoke: real blocking owner, health, bounded ring and teardown.

Run with unittest or directly. Uses no PostgreSQL or live solet. Every import
is rooted in this checkout rather than a shared editable-install source tree.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import threading
import time
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import Mock, patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta/src"))
sys.path.insert(0, str(REPO_ROOT / "plugins/agent_messaging_plugin/src"))

import httpx  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.runtime import loop_lag  # noqa: E402
from ananta.core.runtime.loop_lag import (  # noqa: E402
    CURRENT_PROCESS_KEY,
    MAX_SAMPLES,
    LoopLagConfig,
    LoopLagMonitor,
    LoopLagRegistry,
    LoopLagSnapshot,
)
from fastapi import FastAPI  # noqa: E402

from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from ananta import cli  # noqa: E402

PROCESS_KEY = "plugin::agent_messaging_plugin::report_alive"
PREIMPORTED_LOOP_FACTORY = asyncio.new_event_loop


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _blocking_sleep(_action: QueuedAction) -> dict[str, object]:
    print("BLOCKING_PATH_REACHED process_key=" + str(CURRENT_PROCESS_KEY.get()), flush=True)
    # One 10 ms sampling interval plus a deliberate 200 ms delay ensures the
    # lateness (which excludes that interval) is at least 200 ms.
    time.sleep(0.210)
    return {"success": True}


def _poller() -> ActionQueuePoller:
    return ActionQueuePoller.__new__(ActionQueuePoller)


def _health_app() -> FastAPI:
    app = FastAPI()
    stub = object()
    register_routes(
        app,
        bridge_manager=stub,  # type: ignore[arg-type]
        peer_registry=stub,  # type: ignore[arg-type]
        platform_surface=stub,  # type: ignore[arg-type]
        agent_messaging_service=stub,
        config={"long_poll_timeout_seconds": 25},
    )
    return app


class LoopLagSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_run_forever = asyncio.BaseEventLoop.run_forever
        self.run_patch = patch.object(asyncio.BaseEventLoop, "run_forever", self.previous_run_forever)
        self.run_patch.start()
        self.registry = LoopLagRegistry()
        self.registry_patch = patch.object(loop_lag, "LOOP_LAG", self.registry)
        self.registry_patch.start()
        self.health_patch = patch("agent_messaging_plugin.http_routes.LOOP_LAG", self.registry)
        self.health_patch.start()
        loop_lag.install_loop_lag_monitor()

    def tearDown(self) -> None:
        self.registry.shutdown()
        self.run_patch.stop()
        self.health_patch.stop()
        self.registry_patch.stop()

    def test_runtime_lag_owner_and_health(self) -> None:
        capture = _Capture()
        loop_lag.logger.addHandler(capture)
        try:
            clean, delayed, health = asyncio.run(self._runtime_probe())
        finally:
            loop_lag.logger.removeHandler(capture)
        self.assertTrue(clean, "startup loop hook must publish a loop-lag gauge")
        before = next(iter(clean.values()))
        after = next(iter(delayed.values()))
        self.assertGreater(before["samples"], 5)
        self.assertLess(cast(float, before["p99_ms"]), 50.0)
        self.assertGreaterEqual(cast(float, after["max_ms"]), 200.0)
        self.assertGreaterEqual(after["over_budget_count"], 1)
        warnings = [message for message in capture.messages if message.startswith("LOOP_LAG loop=")]
        self.assertEqual(len(warnings), 2, warnings)
        for warning in warnings:
            self.assertIn(PROCESS_KEY, warning)
            self.assertIn("task=loop-lag-probe-owner", warning)
            self.assertIn("_blocking_sleep", warning)
            self.assertIn("loop=platform/", warning)
        self.assertIn("phase=stalled", warnings[0])
        self.assertIn("phase=recovered", warnings[1])
        self.assertIn("loop_lag", health)
        fields = {"p50_ms", "p99_ms", "max_ms", "over_budget_count", "window_s", "samples", "threads", "thread_cpu_status", "thread_cpu_window_s", "py_switch_interval_ms"}
        for row in cast(dict[str, dict[str, object]], health["loop_lag"]).values():
            self.assertTrue(fields <= row.keys())
        self.assertEqual(self.registry.snapshot(), {}, "stopped loops must retire their windows")
        print(f"CLEAN {before}; BLOCKED {after}; WARNING {warnings[0]}")

    async def _runtime_probe(self) -> tuple[dict[str, LoopLagSnapshot], dict[str, LoopLagSnapshot], dict[str, object]]:
        task = asyncio.current_task()
        assert task is not None
        task.set_name("loop-lag-probe-owner")
        await asyncio.sleep(0.150)
        clean = self.registry.snapshot()
        poller = _poller()
        action = QueuedAction(id="lag-probe", process_key=PROCESS_KEY, parameters="{}", notes="", created_at="")
        with (
            patch.object(poller, "_is_terminated_flow_sibling", return_value=False),
            patch.object(poller, "_prepare_action_for_execution", return_value=True),
            patch.object(poller, "_resolve_io_context"),
            patch.object(poller, "_requires_main_thread", return_value=True),
            patch.object(poller, "action_processor", Mock(execute_action=_blocking_sleep), create=True),
            patch.object(poller, "_mark_action_completed"),
        ):
            await poller._process_action(action)
        self.assertIsNone(CURRENT_PROCESS_KEY.get(), "completed action must clear process attribution")
        await asyncio.sleep(0.025)
        delayed = self.registry.snapshot()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_health_app()), base_url="http://test") as client:
            response = await client.get("/api/v1/bridge/health")
        self.assertEqual(response.status_code, 200, "lag is advisory, never a readiness refusal")
        return clean, delayed, cast(dict[str, object], response.json())

    def test_thread_loop_stop_restart_and_close(self) -> None:
        loop = asyncio.new_event_loop()
        observations: list[dict[str, LoopLagSnapshot]] = []

        async def probe() -> None:
            await asyncio.sleep(0.050)
            observations.append(self.registry.snapshot())
            loop.stop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            for _ in range(2):
                loop.create_task(probe())
                loop.run_forever()
                self.assertFalse(asyncio.all_tasks(loop), "monitoring must not leave pending tasks")
                self.assertEqual(self.registry.snapshot(), {})
            loop.close()

        thread = threading.Thread(target=run, name="agent_messaging_plugin-server")
        thread.start()
        thread.join()
        self.assertEqual(len(observations), 2)
        for observed in observations:
            self.assertEqual(len(observed), 1)
            self.assertTrue(next(iter(observed)).startswith("agent_messaging_plugin-server/"))
        self.assertNotEqual(next(iter(observations[0])), next(iter(observations[1])))
        self.assertTrue(loop.is_closed())

    def test_stop_preserves_deferred_application_callbacks_until_restart(self) -> None:
        def observe(run_forever: Callable[[asyncio.BaseEventLoop], None]) -> list[list[str]]:
            events: list[str] = []
            snapshots: list[list[str]] = []
            loop = asyncio.SelectorEventLoop()

            def stop_and_defer() -> None:
                events.append("stop")
                loop.call_soon(events.append, "deferred")
                loop.stop()

            def restart_and_stop() -> None:
                events.append("restart")
                loop.stop()

            try:
                with patch.object(asyncio.BaseEventLoop, "run_forever", run_forever):
                    loop.call_soon(stop_and_defer)
                    loop.run_forever()
                    snapshots.append(events.copy())
                    loop.call_soon(restart_and_stop)
                    loop.run_forever()
                    snapshots.append(events.copy())
            finally:
                loop.close()
            return snapshots

        baseline = observe(self.previous_run_forever)
        monitored = observe(asyncio.BaseEventLoop.run_forever)
        self.assertEqual(baseline, [["stop"], ["stop", "deferred", "restart"]])
        self.assertEqual(monitored, baseline, "monitor cleanup must never execute deferred application work")
        self.assertEqual(self.registry.snapshot(), {})
        print(f"STOP_RESTART baseline={baseline}; monitored={monitored}")

    def test_ring_window_and_config_lifecycle(self) -> None:
        loop = asyncio.new_event_loop()
        try:
            monitor = LoopLagMonitor(loop, "bounded", LoopLagConfig())
            for index in range(100_000):
                monitor.record(index / 1000, float(index % 100))
            self.assertEqual(len(monitor.samples), MAX_SAMPLES)
            snapshot = monitor.snapshot(now=100.0)
            self.assertEqual(snapshot["samples"], MAX_SAMPLES)
            self.assertEqual((snapshot["p50_ms"], snapshot["p99_ms"], snapshot["max_ms"]), (49.0, 98.0, 99.0))
            self.assertEqual(snapshot["over_budget_count"], 4900)
            self.assertEqual(monitor.snapshot(now=1000.0)["samples"], 0)
            self.assertIsNone(monitor.snapshot(now=1000.0)["max_ms"])
            monitor.record(1000.0, 2.0)
            self.assertEqual(monitor.snapshot(now=1000.0)["max_ms"], 2.0)
        finally:
            loop.close()
        with patch.dict("os.environ", {"ANANTA_LOOP_LAG_BUDGET_MS": "25", "ANANTA_LOOP_LAG_WINDOW_S": "5"}):
            configured = LoopLagConfig.from_environment()
            self.assertEqual((configured.budget_ms, configured.window_s), (25.0, 5.0))
        with patch.dict("os.environ", {"ANANTA_LOOP_LAG_BUDGET_MS": "nan"}):
            with self.assertLogs(loop_lag.logger, level="WARNING"):
                self.assertEqual(LoopLagConfig.from_environment().budget_ms, 50.0)
        with patch.dict("os.environ", {"ANANTA_LOOP_LAG_INTERVAL_MS": "0.01", "ANANTA_LOOP_LAG_SUMMARY_S": "0.01"}):
            with self.assertLogs(loop_lag.logger, level="WARNING"):
                bounded = LoopLagConfig.from_environment()
                self.assertEqual((bounded.interval_ms, bounded.summary_s), (1.0, 1.0))
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(LoopLagConfig.from_environment(), LoopLagConfig())

    def test_startup_installs_before_plugin_loops(self) -> None:
        asyncio.BaseEventLoop.run_forever = self.previous_run_forever
        phases: list[bool] = []

        def initialize(**_kwargs: object) -> Mock:
            async def inspect_hook() -> None:
                phases.append(bool(self.registry.snapshot()))

            asyncio.run(inspect_hook())
            return Mock()

        args = argparse.Namespace(log_level="info", log_outputs=None, actions="", plugin_config={}, max_consecutive_errors=3, max_actions_per_cycle=1, default_inference_provider=None)
        with (
            patch.object(cli, "_initialize_logging_sync", return_value=logging.getLogger(__name__)),
            patch.object(cli, "_initialize_orchestrator_sync", side_effect=initialize),
            patch.object(cli, "_create_bootstrap_plugin_manager"),
            patch.object(cli, "_execute_infrastructure_setup"),
        ):
            cli.initialize_components_sync(Path("unused"), args)
        self.assertEqual(phases, [True])
        run_forever = asyncio.BaseEventLoop.run_forever
        loop_lag.install_loop_lag_monitor()
        self.assertIs(asyncio.BaseEventLoop.run_forever, run_forever)

    def test_preimported_factory_direct_loop_and_runner(self) -> None:
        async def inspect_hook() -> None:
            self.assertEqual(len(self.registry.snapshot()), 1)

        for factory in (PREIMPORTED_LOOP_FACTORY, asyncio.SelectorEventLoop):
            loop = factory()
            try:
                loop.run_until_complete(inspect_hook())
            finally:
                loop.close()
            self.assertEqual(self.registry.snapshot(), {})
        with asyncio.Runner() as runner:
            runner.run(inspect_hook())
        self.assertEqual(self.registry.snapshot(), {})

    def test_failed_action_clears_owner(self) -> None:
        async def exercise() -> None:
            poller = _poller()
            action = QueuedAction(id="failed-probe", process_key=PROCESS_KEY, parameters="{}", notes="", created_at="")
            with (
                patch.object(poller, "_is_terminated_flow_sibling", return_value=False),
                patch.object(poller, "_prepare_action_for_execution", return_value=True),
                patch.object(poller, "_resolve_io_context", side_effect=ValueError("probe failure")),
                patch.object(poller, "_mark_action_failed") as failed,
            ):
                await poller._process_action(action)
                failed.assert_called_once()
            self.assertIsNone(CURRENT_PROCESS_KEY.get())

        asyncio.run(exercise())

    @unittest.skipUnless(sys.platform == "darwin", "macOS kernel counter pass path")
    def test_thread_cpu_census_and_cached_health(self) -> None:
        stop = threading.Event()

        def burn() -> None:
            while not stop.is_set():
                sum(range(10_000))

        worker = threading.Thread(target=burn, name="census-cpu-burner")
        worker.start()
        try:

            async def probe() -> None:
                await asyncio.sleep(1.2)
                row = next(iter(self.registry.snapshot().values()))
                self.assertIn("threads", row, "health must expose a cached per-thread CPU census")
                threads = row["threads"]
                self.assertGreater(len(threads), 0)
                self.assertLessEqual(len(threads), 8)
                self.assertEqual(threads[0]["name"], worker.name)
                self.assertEqual(threads[0]["id"], worker.native_id)
                self.assertGreater(threads[0]["cpu_s"], 0.05)
                self.assertEqual(row["thread_cpu_status"], "ok")

            asyncio.run(probe())
        finally:
            stop.set()
            worker.join()

    def test_thread_census_window_retirement_and_read_failure(self) -> None:
        registry = LoopLagRegistry(LoopLagConfig(window_s=2.0))
        reader = Mock(status="ok")
        reader.read.side_effect = [
            {1: ("old", 10.0), 2: ("busy", 20.0)},
            {1: ("old", 10.0), 2: ("busy", 21.0)},
            {2: ("busy", 21.5), 3: ("new", 0.2)},
            {2: ("busy", 21.6), 3: ("new", 0.3)},
        ]
        for now in (0.0, 1.0, 2.0, 3.0):
            registry._sample_thread_cpu(reader, now)
        loop = asyncio.new_event_loop()
        try:
            monitor = LoopLagMonitor(loop, "census-test", registry.config)
            registry._monitors[monitor.name] = monitor
            row = registry.snapshot()[monitor.name]
            self.assertEqual([item["id"] for item in row["threads"]], [2, 3])
            self.assertAlmostEqual(row["threads"][0]["cpu_s"], 0.6)
            self.assertEqual(row["thread_cpu_window_s"], 2.0)
            reader.read.side_effect = OSError("probe denied")
            with (
                patch.object(loop_lag, "_DarwinThreadCpu", return_value=reader),
                patch.object(registry._stop, "wait", side_effect=[False, True]),
            ):
                registry._watch()
            self.assertIn("unavailable", registry.snapshot()[monitor.name]["thread_cpu_status"])
            self.assertEqual(registry.snapshot()[monitor.name]["threads"], [])
        finally:
            registry.shutdown()
            loop.close()

    def test_blocking_callback_attribution_survives_recovery(self) -> None:
        def synthetic_blocking_callback() -> None:
            time.sleep(0.15)

        async def probe() -> None:
            asyncio.get_running_loop().call_soon(synthetic_blocking_callback)
            await asyncio.sleep(0.18)

        with self.assertLogs(loop_lag.logger, level="INFO") as capture:
            asyncio.run(probe())
        recovered = [line for line in capture.output if "phase=recovered" in line]
        self.assertEqual(len(recovered), 1, "recovery must retain during-stall callback attribution")
        self.assertIn("synthetic_blocking_callback", recovered[0])
        self.assertIn("task=callback", recovered[0])

    def test_switch_interval_startup_and_invalid_config(self) -> None:
        original = sys.getswitchinterval()
        try:
            asyncio.BaseEventLoop.run_forever = self.previous_run_forever
            with patch.dict("os.environ", {"ANANTA_PY_SWITCH_INTERVAL_MS": "0.5"}):
                loop_lag.install_loop_lag_monitor()
            self.assertAlmostEqual(sys.getswitchinterval(), 0.0005)
            with patch.dict("os.environ", {"ANANTA_PY_SWITCH_INTERVAL_MS": "2"}):
                loop_lag.install_loop_lag_monitor()
            self.assertAlmostEqual(sys.getswitchinterval(), 0.0005, msg="installation must be idempotent")
            asyncio.BaseEventLoop.run_forever = self.previous_run_forever
            with patch.dict("os.environ", {}, clear=True):
                loop_lag.install_loop_lag_monitor()
            self.assertAlmostEqual(sys.getswitchinterval(), 0.0005, msg="unset must preserve the interpreter setting")
            for value in ("nan", "0", "-1", "invalid", "inf"):
                asyncio.BaseEventLoop.run_forever = self.previous_run_forever
                with patch.dict("os.environ", {"ANANTA_PY_SWITCH_INTERVAL_MS": value}):
                    with self.assertLogs(loop_lag.logger, level="WARNING") as capture:
                        loop_lag.install_loop_lag_monitor()
                self.assertIn("LOOP_LAG_CONFIG", capture.output[0])
                self.assertAlmostEqual(sys.getswitchinterval(), 0.0005)
        finally:
            sys.setswitchinterval(original)


if __name__ == "__main__":
    print(f"MONITOR_SOURCE {loop_lag.__file__}")
    unittest.main()
