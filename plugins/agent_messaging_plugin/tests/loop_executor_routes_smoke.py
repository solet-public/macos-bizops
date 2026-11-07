"""Real route/action boundaries preserve results on named workers and a live loop.

The slow backend sleeps for 300 ms while a ticker runs on the serving loop.
Run with unittest or directly; no platform, database or tmux is required.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from collections.abc import Awaitable
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins/agent_messaging_plugin/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import async_route_blocking_class_smoke as fixtures  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.runtime.loop_executors import ExecutorFamily, close_loop_executors, run_blocking  # noqa: E402

from agent_messaging_plugin import delivery_drive  # noqa: E402
from agent_messaging_plugin.mcp_streamable import router  # noqa: E402
from agent_messaging_plugin.mcp_streamable.auth import BearerVerifier  # noqa: E402


class Backend:
    """One deterministic success/failure seam with an observable worker identity."""

    def __init__(self, delay: float, fail: bool) -> None:
        self.delay = delay
        self.error = RuntimeError("backend failed") if fail else None
        self.names: list[str] = []
        self.result: Any = {"success": True, "data": {"marker": "kept"}}

    def call(self, *_args: object, **_kwargs: object) -> Any:
        self.names.append(threading.current_thread().name)
        time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


class LoopExecutorRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await close_loop_executors()

    async def measure(self, operation: Awaitable[Any]) -> Any:
        gaps: list[float] = []

        async def ticker() -> None:
            previous = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        tick = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            return await operation
        finally:
            await asyncio.sleep(0.01)
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
            maximum = max(gaps)
            print(f"{self.id()}: maximum same-loop ticker gap {maximum:.4f}s")
            self.assertLess(maximum, 0.1)

    async def test_bridge_success_slow_and_failure(self) -> None:
        for delay, fail in ((0.0, False), (0.3, False), (0.3, True)):
            with self.subTest(delay=delay, fail=fail):
                backend = Backend(delay, fail)
                manager = fixtures._bridge_manager()
                registry = fixtures._Registry()
                registry.list_agent_ids = backend.call
                backend.result = {}
                app = fixtures._build_app(manager, registry)
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url=fixtures.BASE_URL) as client:
                    operation = client.get("/api/v1/bridge/test/peer/list")
                    if fail:
                        with self.assertRaises(RuntimeError) as caught:
                            await self.measure(operation)
                        self.assertIs(caught.exception, backend.error)
                    else:
                        response = await self.measure(operation)
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json(), {"agent_ids": [], "instances": {}})
                self.assertTrue(backend.names)
                self.assertTrue(all(name.startswith("solet-bridge-") for name in backend.names), backend.names)

    async def test_streamable_authorization_success_slow_and_failure(self) -> None:
        for delay, fail in ((0.0, False), (0.3, False), (0.3, True)):
            with self.subTest(delay=delay, fail=fail):
                backend = Backend(delay, fail)
                claim = object()
                backend.result = claim
                operation = router._authorize_off_loop(
                    Request({"type": "http", "headers": []}),
                    cast("BearerVerifier", SimpleNamespace(verify=backend.call)),
                    resource_metadata_url="",
                    solet_name="executor-test",
                )
                if fail:
                    with self.assertRaises(RuntimeError) as caught:
                        await self.measure(operation)
                    self.assertIs(caught.exception, backend.error)
                else:
                    self.assertIs(await self.measure(operation), claim)
                self.assertTrue(backend.names)
                self.assertTrue(all(name.startswith("solet-streamable-") for name in backend.names), backend.names)

    async def test_streamable_post_dispatch_success_slow_and_failure(self) -> None:
        for delay, fail in ((0.0, False), (0.3, False), (0.3, True)):
            with self.subTest(delay=delay, fail=fail):
                backend = Backend(delay, fail)
                backend.result = JSONResponse({"jsonrpc": "2.0", "id": 7, "result": {"kept": True}})
                with (
                    patch.object(router, "_validate_post_preconditions", AsyncMock(return_value=(object(), object()))),
                    patch.object(router, "_resolve_and_dispatch", backend.call),
                ):
                    operation = router._handle_post(
                        Request({"type": "http", "headers": []}),
                        session_manager=cast("Any", object()),
                        bearer_verifier=cast("Any", object()),
                        dispatch_ctx=cast("Any", object()),
                        allowed_origins=(),
                    )
                    if fail:
                        with self.assertRaises(RuntimeError) as caught:
                            await self.measure(operation)
                        self.assertIs(caught.exception, backend.error)
                    else:
                        self.assertIs(await self.measure(operation), backend.result)
                self.assertTrue(backend.names)
                self.assertTrue(all(name.startswith("solet-streamable-") for name in backend.names), backend.names)

    async def test_streamable_delete_authorizes_before_close(self) -> None:
        for delay, fail in ((0.0, False), (0.3, False), (0.3, True)):
            with self.subTest(delay=delay, fail=fail):
                backend = Backend(delay, fail)
                order: list[str] = []
                identity = SimpleNamespace(agent_instance_id="agi-executor-test")

                def verify(_header: str | None, order: list[str] = order, identity: Any = identity) -> Any:
                    order.append("authorize")
                    return identity

                def close(session_id: str, backend: Backend = backend, order: list[str] = order) -> None:
                    self.assertEqual(session_id, "session-executor-test")
                    order.append("close")
                    backend.call()

                operation = router._handle_delete(
                    Request({"type": "http", "headers": [(b"mcp-session-id", b"session-executor-test")]}),
                    session_manager=cast("Any", SimpleNamespace(get=lambda _key, identity=identity: identity, close=close)),
                    bearer_verifier=cast("Any", SimpleNamespace(verify=verify)),
                    allowed_origins=(),
                )
                if fail:
                    with self.assertRaises(RuntimeError) as caught:
                        await self.measure(operation)
                    self.assertIs(caught.exception, backend.error)
                else:
                    response = await self.measure(operation)
                    self.assertEqual(response.status_code, 204)
                    self.assertEqual(response.body, b"")
                self.assertEqual(order, ["authorize", "close"])
                self.assertTrue(backend.names)
                self.assertTrue(all(name.startswith("solet-streamable-") for name in backend.names), backend.names)

    async def test_existing_drive_source_and_sink_pressure_preserves_route_capacity(self) -> None:
        drives = fixtures._BlockingDrives()
        state = fixtures._managed_state()
        service = fixtures._MessagingService()

        async def pressure() -> None:
            try:
                outcomes = await run_blocking(
                    ExecutorFamily.BRIDGE,
                    lambda: [fixtures._dispatch_send(state, service) for _ in range(40)],
                )
                await asyncio.sleep(0.3)
                snapshot = delivery_drive.drive_queue_snapshot()
                self.assertEqual(snapshot, {"pending": 32, "limit": 32, "workers": 4})
                self.assertEqual(sum(outcome.drive_on_delivery == "driver_scheduled" for outcome in outcomes), 32)
                self.assertEqual(sum(outcome.drive_on_delivery == "driver_queue_full" for outcome in outcomes), 8)
                with drives.lock:
                    self.assertEqual(drives.peak, 4)
                    self.assertTrue(all(name.startswith("peer-delivery-drive") for name in drives.thread_names))
                name = await run_blocking(ExecutorFamily.STREAMABLE, lambda: threading.current_thread().name)
                self.assertTrue(name.startswith("solet-streamable-"), name)
            finally:
                drives.release.set()
            finished = await run_blocking(
                ExecutorFamily.BRIDGE,
                lambda: [delivery_drive.finished_drive_outcome(outcome.message_id, timeout_s=2) for outcome in outcomes],
            )
            self.assertEqual(sum(item is not None and item[0] == "driver_sent" for item in finished), 32)

        try:
            await self.measure(pressure())
        finally:
            drives.close()

    async def test_existing_registration_lock_and_composer_fixes_remain_responsive(self) -> None:
        original_failures = list(fixtures._failed)
        for case in (
            fixtures.case_d_register_lock_not_held_across_handover_drive,
            fixtures.case_e_streamable_writes_share_the_register_lock,
            fixtures.case_g_paste_wait_ends_on_landed_composer_row,
        ):
            with self.subTest(case=case.__name__):
                await self.measure(case())
                self.assertEqual(fixtures._failed, original_failures)

    async def test_poller_action_families_keep_completion_and_failure(self) -> None:
        for key, family in (
            ("plugin::agent_messaging_plugin::report_alive", "actions"),
            ("service_interface::embedding_service::generate_embeddings", "embedding"),
            ("plugin::seed_factory_plugin::assemble_seed", "seed"),
            ("service_interface::session_ledger_service::ingest_raw_chunk", "actions"),
        ):
            for delay, fail in ((0.0, False), (0.3, False), (0.3, True)):
                with self.subTest(key=key, delay=delay, fail=fail):
                    backend = Backend(delay, fail)
                    completed: list[tuple[str, Any]] = []
                    failed: list[str] = []

                    def complete(
                        action_id: str,
                        result: Any,
                        *_args: Any,
                        completed: list[tuple[str, Any]] = completed,
                        **kwargs: Any,
                    ) -> None:
                        completed.append((action_id, result))
                        kwargs["on_status_written"]()

                    def failure(
                        _action_id: str,
                        message: str,
                        *_args: Any,
                        failed: list[str] = failed,
                        **_kwargs: Any,
                    ) -> None:
                        failed.append(message)

                    poller: Any = SimpleNamespace(
                        _is_terminated_flow_sibling=lambda _action: False,
                        _prepare_action_for_execution=lambda _action: True,
                        _resolve_io_context=lambda _action: None,
                        _requires_main_thread=lambda _key: False,
                        action_processor=SimpleNamespace(execute_action=backend.call),
                        _mark_action_completed=complete,
                        _mark_action_failed=failure,
                    )
                    action = QueuedAction(id="test-action", process_key=key, parameters="{}", notes="", created_at="")
                    await self.measure(ActionQueuePoller._process_action(poller, action))
                    if fail:
                        self.assertEqual(failed, ["backend failed"])
                        self.assertEqual(completed, [])
                    else:
                        self.assertEqual(completed, [(action.id, backend.result)])
                        self.assertEqual(failed, [])
                    self.assertTrue(backend.names)
                    self.assertTrue(all(name.startswith(f"solet-{family}-") for name in backend.names), backend.names)


if __name__ == "__main__":
    unittest.main()
