#!/usr/bin/env python3
"""Offline actual handler-entry invariant smoke; no live solet or database."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import call, patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta/src"))
sys.path.insert(0, str(REPO_ROOT / "plugins/agent_messaging_plugin/src"))

import httpx  # noqa: E402
from ananta.core.actions.action_execution_engine import ActionExecutionEngine  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import QueuedAction  # noqa: E402
from ananta.core.plugins.action_execution import _execute_action_method  # noqa: E402
from ananta.core.plugins.plugin_base import PluginBase  # noqa: E402
from ananta.core.runtime import loop_invariant, loop_lag  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from agent_messaging_plugin.http_routes import register_routes  # noqa: E402

# Registered identities, with isolated stand-in bodies and no live service.
PROCESS_KEY = "service_interface::knowledge_service::search"
LIMIT_SETTINGS = (
    "ANANTA_LOOP_INVARIANT_ENTRY_CAPACITY",
    "ANANTA_LOOP_INVARIANT_STACK_FRAME_LIMIT",
    "ANANTA_LOOP_INVARIANT_STACK_CHAR_LIMIT",
)


class _Action(QueuedAction):
    source_plugin: str | None = None


class _BlockingService:
    def __init__(self) -> None:
        self.threads: list[int] = []

    def block(self) -> dict[str, object]:
        self.threads.append(threading.get_ident())
        print(f"HANDLER_BODY_REACHED process_key={PROCESS_KEY} thread={self.threads[-1]}")
        time.sleep(0.005)
        return {"reached": True}


def _invoke(service: _BlockingService) -> dict[str, object]:
    dispatcher = ActionProcessor.__new__(ActionProcessor)
    dispatcher.process_registry = {"processes": {PROCESS_KEY: {"function_name": "block"}}}
    action = _Action(id="invariant-probe", process_key=PROCESS_KEY, parameters="{}", notes="", created_at="")
    with (
        patch.object(dispatcher, "_resolve_service", return_value=service),
        patch.object(dispatcher, "_filter_and_inject_arguments", return_value={}),
    ):
        return dispatcher._execute_service_interface_action("knowledge_service", "search", {}, action)


def _health_app(bridge_manager: object | None = None) -> FastAPI:
    app = FastAPI()
    stub = object()
    register_routes(
        app,
        bridge_manager=bridge_manager or stub,  # type: ignore[arg-type]
        peer_registry=stub,  # type: ignore[arg-type]
        platform_surface=stub,  # type: ignore[arg-type]
        agent_messaging_service=stub,
        config={"long_poll_timeout_seconds": 25},
    )
    return app


class LoopInvariantSmoke(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = loop_lag.LoopLagRegistry(loop_lag.LoopLagConfig(interval_ms=1, summary_s=0.02))
        self.recorder = loop_invariant.HandlerEntryRecorder({(f"route::agent_messaging_plugin::{name}", "route"): reason for name, reason in loop_invariant.DISPATCH_ONLY_ROUTES.items()}, config=loop_invariant.HandlerEntryConfig())
        self.patches = [
            patch.object(loop_lag, "LOOP_LAG", self.registry),
            patch("agent_messaging_plugin.http_routes.LOOP_LAG", self.registry),
            patch.object(loop_invariant, "HANDLER_ENTRIES", self.recorder),
            patch.object(asyncio.BaseEventLoop, "run_forever", loop_lag._run_monitored_loop),
        ]
        for item in self.patches:
            item.start()
        for item in self.patches:
            self.addCleanup(item.stop)
        self.addCleanup(self.registry.shutdown)

    def test_registered_handler_on_loop_is_visible_in_health(self) -> None:
        asyncio.run(self._probe())

    async def _probe(self) -> None:
        service = _BlockingService()
        self.assertEqual(_invoke(service), {"reached": True})
        self.assertEqual(service.threads, [threading.get_ident()])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_health_app()), base_url="http://probe") as client:
            response = await client.get("/api/v1/bridge/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json().get("violations", {}).get("count"),
            1,
            "a registered handler body reached on the loop must produce one violation",
        )
        violation = response.json()["violations"]
        self.assertEqual(violation["last_process_key"], PROCESS_KEY)
        self.assertIn("_execute_standard_service_method", violation["last_stack"])
        self.assertEqual(violation["recent"][-1]["thread_id"], threading.get_ident())
        print(f"VIOLATION_COUNT={violation['count']} process_key={violation['last_process_key']}")

    def test_same_registered_handler_in_worker_has_zero_violations(self) -> None:
        asyncio.run(self._worker_probe())

    async def _worker_probe(self) -> None:
        service = _BlockingService()
        self.assertEqual(await asyncio.to_thread(_invoke, service), {"reached": True})
        self.assertNotEqual(service.threads, [threading.get_ident()])
        snapshot = self.recorder.snapshot()
        self.assertEqual(snapshot["violations"]["count"], 0)
        self.assertEqual(snapshot["recent_entries"][-1]["thread_id"], service.threads[-1])
        self.assertFalse(snapshot["recent_entries"][-1]["on_loop"])
        print("WORKER_VIOLATION_COUNT=0")

    def test_undeclared_real_route_on_loop_records_violation_and_still_runs(self) -> None:
        # open_bridge is declared dispatch-only, so this proof uses a recorder
        # with that one declaration removed: an undeclared on-loop route is a violation.
        undeclared = loop_invariant.HandlerEntryRecorder(
            {(f"route::agent_messaging_plugin::{name}", "route"): reason for name, reason in loop_invariant.DISPATCH_ONLY_ROUTES.items() if name != "open_bridge"},
            config=loop_invariant.HandlerEntryConfig(),
        )
        with patch.object(loop_invariant, "HANDLER_ENTRIES", undeclared):
            asyncio.run(self._route_probe(undeclared))

    def test_declared_open_bridge_on_loop_has_zero_violations_and_sets_attribute(self) -> None:
        asyncio.run(self._route_probe(self.recorder))

    async def _route_probe(self, recorder: loop_invariant.HandlerEntryRecorder) -> None:
        opened: list[SimpleNamespace] = []

        def _open(**_kwargs: object) -> SimpleNamespace:
            bridge = SimpleNamespace(bridge_id="probe", session_id="probe")
            opened.append(bridge)
            return bridge

        manager = SimpleNamespace(open=_open, server_lifecycle_id="probe")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=_health_app(manager)), base_url="http://probe") as client:
            response = await client.post("/api/v1/bridge/open", json={"caller_agent_session_id": "  caller  "})
        self.assertEqual(response.status_code, 200, "the route must not refuse")
        self.assertEqual(response.json(), {"bridge_id": "probe", "session_id": "probe", "server_lifecycle_id": "probe", "long_poll_timeout_seconds": 25})
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].caller_agent_session_id, "caller")
        snapshot = recorder.snapshot()["violations"]
        if recorder is self.recorder:
            self.assertEqual(snapshot["count"], 0, "a declared dispatch-only route on the loop is not a violation")
        else:
            self.assertEqual(snapshot["count"], 1)
            self.assertEqual(snapshot["last_process_key"], "route::agent_messaging_plugin::open_bridge")
            self.assertIn("open_bridge", snapshot["last_stack"] or "")

    def test_other_actual_handler_boundaries(self) -> None:
        asyncio.run(self._other_boundaries())

    async def _other_boundaries(self) -> None:
        service = _BlockingService()
        engine = ActionExecutionEngine.__new__(ActionExecutionEngine)
        result = await engine._execute_service_function(service, service.block, "search", {}, "knowledge_service")
        self.assertEqual(result, {"reached": True})

        async def async_body() -> dict[str, object]:
            return {"async_reached": True}

        self.assertEqual(
            await engine._execute_service_function(service, async_body, "search", {}, "knowledge_service"),
            {"async_reached": True},
        )
        plugin = SimpleNamespace(name="agent_messaging_plugin", report_alive=lambda _params: {"plugin_reached": True})
        self.assertEqual(await _execute_action_method(cast(PluginBase, plugin), "report_alive", {}), {"plugin_reached": True})

        async def async_plugin(_params: dict[str, object]) -> dict[str, object]:
            return {"plugin_async_reached": True}

        plugin.report_alive = async_plugin
        self.assertEqual(await _execute_action_method(cast(PluginBase, plugin), "report_alive", {}), {"plugin_async_reached": True})

        dispatcher = ActionProcessor.__new__(ActionProcessor)
        action = _Action(id="entry-probe", process_key="plugin::agent_messaging_plugin::report_alive", parameters="{}", notes="", created_at="")
        plugin.report_alive = lambda **_kwargs: {"plugin_dispatch_reached": True}
        with (
            patch.object(dispatcher, "_setup_plugin_context"),
            patch.object(dispatcher, "_build_call_context", return_value=None),
            patch.object(dispatcher, "_lift_inference_vertex_identity"),
        ):
            self.assertEqual(dispatcher._execute_plugin_method(plugin, "report_alive", {}, action), {"plugin_dispatch_reached": True})
        vertex = SimpleNamespace(process_results=lambda **_kwargs: {"vertex_reached": True})
        action.process_key = "service_interface::inference_service::process_results"
        self.assertEqual(dispatcher._execute_vertex_inference(vertex, "process_results", {}, action), {"vertex_reached": True})
        self.assertEqual(self.recorder.snapshot()["violations"]["count"], 6)

    def test_exceptions_are_exact_reasoned_and_empty_by_default(self) -> None:
        asyncio.run(self._declarations_probe())

    async def _declarations_probe(self) -> None:
        key = "plugin::agent_messaging_plugin::report_alive"
        default = loop_invariant.HandlerEntryRecorder()
        declared = loop_invariant.HandlerEntryRecorder({(key, "handler"): "Only schedules worker work."})
        declared.record(key, "handler")
        self.assertEqual(declared.snapshot()["violations"]["count"], 0)
        self.assertTrue(declared.snapshot()["recent_entries"][-1]["dispatch_only_reason"])
        declared.record(key, "route")
        self.assertEqual(declared.snapshot()["violations"]["count"], 1, "a declaration is exact in key and kind")
        default.record(key, "handler")
        self.assertEqual(default.snapshot()["violations"]["count"], 1, "removing a declaration restores observation")

    def test_default_limits_when_environment_is_unset(self) -> None:
        with patch.dict(os.environ):
            for key in LIMIT_SETTINGS:
                os.environ.pop(key, None)
            recorder = loop_invariant.HandlerEntryRecorder()
        self.assertEqual(recorder.snapshot()["capacity"], 128)
        self.assertEqual((recorder.config.entry_capacity, recorder.config.stack_frame_limit, recorder.config.stack_char_limit), (128, 12, 8192))

    def test_environment_limits_change_retention_and_stack(self) -> None:
        with patch.dict(os.environ, dict(zip(LIMIT_SETTINGS, ("2", "2", "128"), strict=True))):
            recorder = loop_invariant.HandlerEntryRecorder()
        self.assertEqual(recorder.snapshot()["capacity"], 2)
        with (
            patch.object(loop_invariant.traceback, "format_stack", return_value=["outer\n", "inner\n" + "s" * 512]) as stack,
            patch.object(loop_invariant.logger, "error"),
        ):
            asyncio.run(self._configured_records(recorder, 5))
        self.assertEqual(stack.call_args_list, [call(limit=2)] * 5)
        snapshot = recorder.snapshot()
        self.assertEqual(snapshot["invocations"], 5)
        self.assertEqual(len(snapshot["recent_entries"]), 2)
        self.assertEqual(snapshot["violations"]["count"], 5)
        self.assertEqual(len(snapshot["violations"]["recent"]), 2)
        self.assertEqual(snapshot["violations"]["last_stack"], "s" * 128)

    async def _configured_records(self, recorder: loop_invariant.HandlerEntryRecorder, count: int) -> None:
        for _ in range(count):
            recorder.record(PROCESS_KEY, "handler")

    def test_invalid_environment_limits_warn_and_use_defaults(self) -> None:
        for value in ("malformed", "0", "-1", "1.5", "nan", "inf", str(sys.maxsize + 1)):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, dict.fromkeys(LIMIT_SETTINGS, value)),
                self.assertLogs(loop_invariant.logger, level=logging.WARNING) as captured,
            ):
                recorder = loop_invariant.HandlerEntryRecorder()
            self.assertEqual((recorder.config.entry_capacity, recorder.config.stack_frame_limit, recorder.config.stack_char_limit), (128, 12, 8192))
            self.assertEqual(len(captured.output), 3)
            for key in LIMIT_SETTINGS:
                self.assertTrue(any(key in line and "using default" in line for line in captured.output))

    def test_positive_limits_below_minimum_are_clamped_with_advisory(self) -> None:
        with (
            patch.dict(os.environ, dict.fromkeys(LIMIT_SETTINGS, "1")),
            self.assertLogs(loop_invariant.logger, level=logging.WARNING) as captured,
        ):
            recorder = loop_invariant.HandlerEntryRecorder()
        self.assertEqual((recorder.config.entry_capacity, recorder.config.stack_frame_limit, recorder.config.stack_char_limit), (1, 1, 128))
        self.assertEqual(len(captured.output), 1)
        self.assertIn("ANANTA_LOOP_INVARIANT_STACK_CHAR_LIMIT", captured.output[0])
        self.assertIn("using 128", captured.output[0])

    def test_bounded_history_counters_concurrency_and_snapshot_isolation(self) -> None:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda index: self.recorder.record(f"worker::{index}", "handler"), range(256)))
        snapshot = self.recorder.snapshot()
        self.assertEqual(snapshot["invocations"], 256)
        self.assertEqual(len(snapshot["recent_entries"]), self.recorder.config.entry_capacity)
        self.assertEqual(snapshot["violations"]["count"], 0)
        snapshot["recent_entries"].clear()
        self.assertEqual(len(self.recorder.snapshot()["recent_entries"]), self.recorder.config.entry_capacity)
        asyncio.run(self._bounded_violations())

    async def _bounded_violations(self) -> None:
        count = self.recorder.config.entry_capacity + 3
        with patch.object(loop_invariant.logger, "error") as log:
            for _ in range(count):
                self.recorder.record(PROCESS_KEY, "handler")
            self.assertEqual(log.call_count, count)
        violations = self.recorder.snapshot()["violations"]
        self.assertEqual(violations["count"], count)
        self.assertEqual(len(violations["recent"]), self.recorder.config.entry_capacity)
        self.assertLessEqual(len(violations["last_stack"] or ""), self.recorder.config.stack_char_limit)

    def test_summary_retains_violation_count(self) -> None:
        with self.assertLogs(loop_lag.logger, level=logging.INFO) as captured:
            asyncio.run(self._summary_probe())
        summaries = [line for line in captured.output if "LOOP_LAG_SUMMARY" in line]
        self.assertTrue(summaries)
        self.assertTrue(any("'count': 1" in line and PROCESS_KEY in line for line in summaries))

    async def _summary_probe(self) -> None:
        _invoke(_BlockingService())
        await asyncio.sleep(0.050)


if __name__ == "__main__":
    unittest.main()
