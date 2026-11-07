"""F2: slow successful/failed state calls must leave the runtime loop responsive.

These tests exercise the shipped async entrypoints with deliberately slow I/O
seams and a ticker on the same loop. The old synchronous bodies stall it for
300 ms. Selected actions may overtake ordinary actions only under Main's
explicit allowlist ruling; ingest retains its serial completion fence.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

# Imports intentionally follow path selection: the shared editable venv must
# never silently substitute master's production modules for the tested lane.
from ananta.core.actions import _measured_action_lane as lane  # noqa: E402
from ananta.core.actions.action_definition_service import ActionDefinitionService  # noqa: E402
from ananta.core.actions.action_execution_engine import ActionExecutionEngine  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.actions.action_registration_manager import ActionRegistrationManager  # noqa: E402
from ananta.core.actions.action_stats_service import ActionStatsService  # noqa: E402
from ananta.core.orchestration.managers.process_registry_manager import ProcessRegistryManager  # noqa: E402
from ananta.core.plugins.plugin_contracts import ActionStatus  # noqa: E402
from ananta.core.state.state_manager import StateManager  # noqa: E402
from ananta.core.tracking.execution_tracking_manager import ExecutionTrackingManager  # noqa: E402
from ananta.core.tracking.performance_stats_manager import PerformanceStatsManager  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402

assert Path(lane.__file__).resolve().is_relative_to(REPO_ROOT), lane.__file__

SLOW = 0.3
HEAVY = "service_interface::embedding_service::list_models"
FAST = "plugin::agent_messaging_plugin::report_alive"
INGEST = "service_interface::session_ledger_service::ingest_raw_chunk"


class _Backend:
    """Only the synchronous state boundary is replaced, including its envelope."""

    def __init__(self, delay: float, fail: bool = False) -> None:
        self.delay, self.fail = delay, fail
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call(self, **kwargs: Any) -> dict[str, Any]:
        time.sleep(self.delay)
        self.calls.append((threading.current_thread().name, kwargs))
        if self.fail:
            raise RuntimeError("slow backend failed")
        record = {
            "action_name": "example",
            "description": "description",
            "default_parameters": "{}",
            "state_data": {"value": 1},
            "status": "completed",
            "duration_ms": 1,
            "duration_seconds": 0.001,
            "started_at": datetime.now(UTC).isoformat(),
        }
        return {
            "action_status": "completed",
            "actions": [],
            "error": None,
            "data": {"records": [record], "result": {"records": [record], "updated": 1}},
        }

    read_state = call
    write_state = call
    update_state = call
    delete_records = call

    def create_schema(self, *args: object) -> dict[str, Any]:
        return self.call(args=args)


def _action(action_id: str, key: str = FAST) -> QueuedAction:
    return QueuedAction(action_id, key, "{}", "", "")


class PollerResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def _responsive(self, operation: Callable[[], Awaitable[Any]]) -> Any:
        stopped = asyncio.Event()
        gaps: list[float] = []

        async def ticker() -> None:
            previous = time.monotonic()
            while not stopped.is_set():
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            return await operation()
        finally:
            stopped.set()
            await tick
            self.assertTrue(gaps, "ticker ran on the exercised loop")
            self.assertLess(max(gaps), 0.1, f"same-loop ticker stalled: {max(gaps):.3f}s")
            print(f"F2_TICKER_MAX_GAP={max(gaps):.6f}s")

    async def test_definition_state_success_and_failure(self) -> None:
        for delay, failed in ((0.0, False), (SLOW, False), (SLOW, True)):
            with self.subTest(delay=delay, failed=failed):
                backend = _Backend(delay, failed)
                service = ActionDefinitionService(cast(Any, backend))
                result = await self._responsive(lambda service=service: service.get_action_definition_from_registry("example"))
                if failed:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result["name"], "example")
                    self.assertEqual(result["parameters"], {})
                self.assertEqual(len(backend.calls), 1)

    async def test_state_facade_entrypoints(self) -> None:
        now = datetime.now(UTC)
        for name in (
            "register",
            "update",
            "retire",
            "registered",
            "stats_start",
            "stats_end",
            "stats_read",
            "tracking_start",
            "tracking_provider",
            "tracking_end",
            "performance",
            "provider",
            "plugin_read",
            "plugin_update",
        ):
            for delay, failed in ((0.0, False), (SLOW, False), (SLOW, True)):
                with self.subTest(entry=name, delay=delay, failed=failed):
                    backend = _Backend(delay, failed)
                    state = cast(Any, backend)
                    registration = ActionRegistrationManager(state)
                    registration.set_process_external_id_getter(lambda key: key)
                    stats = ActionStatsService(state)
                    tracking = ExecutionTrackingManager(state)
                    manager: StateManager[dict[str, object]] = StateManager(state)
                    engine = ActionExecutionEngine.__new__(ActionExecutionEngine)
                    engine.state_service = state
                    calls: dict[str, Callable[[], Awaitable[Any]]] = {
                        "register": lambda registration=registration: registration.register_action({"action_name": "example", "process": {"provider": "example"}}),
                        "update": lambda registration=registration: registration.update_action("example", {"description": "corrected"}),
                        "retire": lambda registration=registration: registration.deregister_action("example"),
                        "registered": registration.get_registered_actions,
                        "stats_start": lambda stats=stats: stats.track_action_execution_start("execution", "example", {}, now, {}),
                        "stats_end": lambda stats=stats: stats.track_action_execution_end("execution", ActionStatus.COMPLETED, {}, None, now),
                        "stats_read": stats.get_action_performance_stats,
                        "tracking_start": lambda tracking=tracking: tracking.track_action_execution_start("execution", "example", {}, now),
                        "tracking_provider": lambda tracking=tracking: tracking.update_execution_provider("execution", "plugin", "example"),
                        "tracking_end": lambda tracking=tracking: tracking.track_action_execution_end("execution", now, True),
                        "performance": PerformanceStatsManager(state).get_action_performance_stats,
                        "provider": lambda engine=engine: engine._update_execution_provider("execution", "plugin", "example"),
                        "plugin_read": lambda manager=manager: manager.get_plugin_state("example"),
                        "plugin_update": lambda manager=manager: manager.update_plugin_state("example", {"value": 2}),
                    }

                    async def invoke(name: str = name, failed: bool = failed, calls: dict[str, Callable[[], Awaitable[Any]]] = calls) -> Any:
                        try:
                            result = await calls[name]()
                        except Exception:
                            if failed and name in {"register", "update", "retire", "registered", "plugin_read", "plugin_update"}:
                                return "expected failure"
                            raise
                        if name in {"register", "update", "retire"}:
                            self.assertTrue(result)
                        if name == "registered":
                            self.assertEqual(result["records"][0]["action_name"], "example")
                        if name in {"stats_read", "performance"}:
                            self.assertIn("error" if failed else ("stats" if name == "performance" else "total_executions"), result)
                        if name == "plugin_read":
                            self.assertEqual(result, {"value": 1})
                        if name == "plugin_update":
                            self.assertEqual(result["actions"], [])
                        return result

                    await self._responsive(invoke)
                    self.assertTrue(backend.calls, "successful backend path was reached")

    def _poller(self, actions: list[QueuedAction], delay_at: str = "", fail_at: str = "", *, fail_handler: bool = False) -> tuple[Any, list[str]]:
        p = cast(Any, ActionQueuePoller.__new__(ActionQueuePoller))
        events: list[str] = []

        def boundary(name: str) -> None:
            if name == delay_at:
                time.sleep(SLOW)
            events.append(name)
            if name == fail_at:
                raise RuntimeError("slow backend failed")

        def claim(action_id: str) -> bool:
            boundary("claim")
            return action_id != "lost"

        def prepare(action: QueuedAction) -> bool:
            boundary("prepare")
            return True

        def complete(*args: object, **kwargs: Any) -> None:
            boundary("complete")
            kwargs["on_status_written"]()

        def execute(action: QueuedAction) -> dict[str, object]:
            if action.process_key in lane.HEAVY_PROCESS_KEYS:
                time.sleep(SLOW)
            boundary("handler:" + action.id)
            return {"success": not fail_handler, "data": {"id": action.id}}

        p._get_queued_actions = AsyncMock(return_value=actions)
        p._mark_action_processing = claim
        p._is_terminated_flow_sibling = lambda action: False
        p._prepare_action_for_execution = prepare
        p._resolve_io_context = lambda action: boundary("context")
        p._mark_action_completed = complete
        p._mark_action_failed = lambda *args, **kwargs: boundary("failed")
        p.action_processor = SimpleNamespace(execute_action=execute)
        p.total_actions_processed = 0
        p._last_observed_queue_depth = len(actions)
        return p, events

    async def test_poller_state_edges_success_and_failure(self) -> None:
        for phase in ("claim", "prepare", "context", "complete", "failed"):
            for failed in (False, True):
                with self.subTest(phase=phase, failed=failed):
                    p, events = self._poller([_action("action")], phase, phase if failed else "", fail_handler=phase == "failed")

                    async def run(p: Any = p, phase: str = phase, failed: bool = failed) -> None:
                        try:
                            await p._poll_once()
                        except RuntimeError:
                            if phase not in {"claim", "failed"} or not failed:
                                raise

                    await self._responsive(run)
                    self.assertIn(phase, events)
                    if not failed and phase != "failed":
                        self.assertEqual(events, ["claim", "prepare", "context", "handler:action", "complete"])

    async def test_main_thread_name_substrings_do_not_inline_handlers(self) -> None:
        p, events = self._poller([_action("action", "plugin::terminal_tools::vscode_status")], "handler:action")  # wint:negative-fixture -- adversarial name must not select inline execution
        await self._responsive(p._poll_once)
        self.assertEqual(events[-1], "complete")

    async def test_orphan_maintenance_success_and_failure(self) -> None:
        for failed in (False, True):
            p, _ = self._poller([])
            p.running = True
            p.total_poll_cycles = 299
            p.poll_interval = 0
            p._poll_once = AsyncMock()

            def reap(p: Any = p, failed: bool = failed) -> None:
                time.sleep(SLOW)
                p.running = False
                if failed:
                    raise RuntimeError("maintenance backend failed")

            p._maybe_reap_orphans = reap
            await self._responsive(p._poll_loop)
            self.assertEqual(p.total_poll_cycles, 300)

    async def test_discovery_load_order_success_and_failure(self) -> None:
        expected = ["schema", "clear", "build", "store", "count", "rebuild"]
        for phase in ("fast", *expected):
            for failed in (False, True):
                if phase == "fast" and failed:
                    continue
                with self.subTest(phase=phase, failed=failed):
                    events: list[str] = []

                    def step(name: str, events: list[str] = events, phase: str = phase, failed: bool = failed) -> None:
                        events.append(name)
                        if name == phase:
                            time.sleep(SLOW)
                            if failed:
                                raise RuntimeError("discovery backend failed")

                    def build(plugin_manager: object) -> dict[str, object]:
                        step("build")
                        return {"processes": {FAST: {"description": "example"}}}

                    manager = cast(Any, ProcessRegistryManager.__new__(ProcessRegistryManager))
                    manager.plugin_manager = object()
                    manager.schema_manager = SimpleNamespace(initialize_schemas=lambda definitions: step("schema"))
                    manager.discovery_service = SimpleNamespace(
                        clear_process_vectors=lambda: step("clear"),
                        store_process=lambda key, data: step("store"),
                        get_process_count=lambda: step("count"),
                        rebuild_index=lambda: step("rebuild"),
                    )

                    async def invoke(manager: Any = manager, failed: bool = failed) -> None:
                        if failed:
                            with self.assertRaises(FrameworkError) as caught:
                                await manager.load_into_discovery_service()
                            self.assertEqual(str(caught.exception.__cause__), "discovery backend failed")
                        else:
                            await manager.load_into_discovery_service()

                    with patch("ananta.core.orchestration.managers.process_registry_manager.build_process_registry", side_effect=build):
                        await self._responsive(invoke)
                    self.assertEqual(events, expected[: expected.index(phase) + 1] if failed else expected)

    async def test_bounded_state_admission_and_cancellation(self) -> None:
        release = threading.Event()
        invoked: list[int] = []

        def work(index: int) -> int:
            release.wait()
            invoked.append(index)
            return index

        async def invoke() -> None:
            tasks = [asyncio.create_task(lane.run_blocking(work, index)) for index in range(40)]
            try:
                await asyncio.sleep(0.02)
                self.assertEqual(lane._state_outstanding, lane.STATE_PENDING)
                for task in tasks[-4:]:
                    task.cancel()
                await asyncio.sleep(SLOW)
            finally:
                release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(results[:36], list(range(36)))
            self.assertTrue(all(isinstance(value, asyncio.CancelledError) for value in results[36:]))
            self.assertEqual(invoked, list(range(36)))
            self.assertEqual(lane._state_outstanding, 0)

        await self._responsive(invoke)

    async def test_submitted_state_write_drains_on_cancellation_with_context(self) -> None:
        entered, finished = threading.Event(), threading.Event()
        context: ContextVar[str] = ContextVar("state-test-context", default="missing")
        context.set("request identity")
        observed: list[str] = []

        def write() -> None:
            entered.set()
            time.sleep(SLOW)
            observed.append(context.get())
            finished.set()

        async def invoke() -> None:
            task = asyncio.create_task(lane.run_blocking(write))
            while not entered.is_set():
                await asyncio.sleep(0.005)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set(), "cancellation cannot abandon an in-flight write")
            self.assertEqual(observed, ["request identity"])
            self.assertEqual(lane._state_outstanding, 0)

        await self._responsive(invoke)

    async def test_heavy_bypasses_report_alive_but_ingest_stays_serial(self) -> None:
        self.assertNotIn(FAST, lane.PROCESS_KEYS)
        self.assertNotIn(INGEST, lane.PROCESS_KEYS)
        self.assertNotIn("plugin::agent_messaging_plugin::deliver_result", lane.PROCESS_KEYS)
        for key in sorted(lane.HEAVY_PROCESS_KEYS):
            with self.subTest(key=key):
                p, events = self._poller([_action("heavy", key), _action("heartbeat", FAST)])
                started = time.monotonic()
                await self._responsive(p._poll_once)
                self.assertLess(time.monotonic() - started, 0.1, "report_alive must complete before 300 ms heavy action")
                self.assertIn("handler:heartbeat", events)
                self.assertNotIn("handler:heavy", events)
                await lane.stop(p)
                self.assertEqual(events.count("handler:heavy"), 1)
                self.assertEqual(events.count("complete"), 2)
                self.assertEqual(lane.live_ids(p), frozenset())

        seal = _action("seal", "plugin::seed_factory_plugin::validate_and_seal_seed_bundle")
        publish = _action("publish", "plugin::seed_factory_plugin::publish_seed")
        p, events = self._poller([seal, publish, _action("heartbeat", FAST)])
        await p._poll_once()
        self.assertNotIn("handler:publish", events)
        self.assertEqual(events.count("claim"), 2, "held seed successor stays queued before CAS")
        await lane.stop(p)
        p._get_queued_actions = AsyncMock(return_value=[publish])
        await p._poll_once()
        await lane.stop(p)
        self.assertLess(events.index("handler:seal"), events.index("handler:publish"))

        p, events = self._poller([_action("first", INGEST), _action("next", FAST)], "handler:first")
        await self._responsive(p._poll_once)
        self.assertLess(events.index("handler:first"), events.index("handler:next"))
        self.assertEqual(events.count("complete"), 2)
        p, events = self._poller([_action("lost", HEAVY), _action("next", FAST)])
        await p._poll_once()
        self.assertNotIn("handler:lost", events, "a lost queued-status CAS must never execute")

    async def test_queue_read_and_namespaces(self) -> None:
        for phase in ("read", "namespace"):
            for failed in (False, True):
                with self.subTest(phase=phase, failed=failed):
                    p, _ = self._poller([])
                    backend = _Backend(SLOW if phase == "read" else 0, failed and phase == "read")

                    def read(namespace: str, data: dict[str, object], backend: _Backend = backend) -> dict[str, object]:
                        backend.call(namespace=namespace, data=data)
                        return {"action_status": "completed", "data": {"records": [{"id": "queued", "process_key": FAST}]}}

                    def namespaces(rows: list[dict[str, object]], phase: str = phase, failed: bool = failed) -> dict[str, str]:
                        if phase == "namespace":
                            time.sleep(SLOW)
                            if failed:
                                raise RuntimeError("namespace backend failed")
                        return {}

                    p.state_service = SimpleNamespace(query_ordered=read)
                    p._claims_own_rows_only = lambda: False
                    p._claimable = lambda row: True
                    p._held_back_behind_deploy = lambda row: False
                    p._fetch_session_namespaces = namespaces
                    p._warn_if_dispatch_starved = lambda *args: None
                    p.max_actions_per_poll = 10
                    p._build_queued_action = lambda row, namespace: _action(str(row["id"]))
                    result = await self._responsive(lambda p=p: ActionQueuePoller._get_queued_actions(p))
                    self.assertEqual([action.id for action in result], [] if failed else ["queued"])

    async def test_sync_service_function_preserves_envelope_and_error(self) -> None:
        engine = ActionExecutionEngine.__new__(ActionExecutionEngine)
        for failed in (False, True):

            def function(failed: bool = failed, **params: object) -> dict[str, object]:
                time.sleep(SLOW)
                if failed:
                    raise RuntimeError("backend failed")
                return {"data": params, "action_status": "completed", "actions": []}

            async def invoke(failed: bool = failed, function: Callable[..., dict[str, object]] = function) -> None:
                if failed:
                    with self.assertRaisesRegex(Exception, "backend failed"):
                        await engine._execute_service_function(engine, function, "example", {"value": 1}, "example")
                else:
                    result = await engine._execute_service_function(engine, function, "example", {"value": 1}, "example")
                    self.assertEqual(result, {"data": {"value": 1}, "action_status": "completed", "actions": []})

            await self._responsive(invoke)


if __name__ == "__main__":
    unittest.main()
