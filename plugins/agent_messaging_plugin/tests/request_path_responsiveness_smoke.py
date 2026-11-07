"""F1 backend requests: successful/failing slow calls leave the same loop live.

Standalone unittest smoke; all backends are in-memory fixtures, never live DB,
vault, pairing, or vendor credentials. The ticker starts before each request.
"""

from __future__ import annotations

# ruff: noqa: B023 -- every callback is drained before its subtest advances.
import asyncio
import sys
import threading
import time
import unittest
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.requests import Request

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "src"))

# ruff: noqa: E402 -- use this checkout, including in a shipped profile.
from ananta.core.runtime import loop_executors
from ananta.core.runtime.loop_executors import ExecutorFamily, close_loop_executors, loop_executor_status

from agent_messaging_plugin import bridge_sessions as bridge_work
from agent_messaging_plugin import http_routes, route_activity
from agent_messaging_plugin.bridge_diagnostics import BridgeDiagnostics
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager

SLOW_S = 0.3
MAX_GAP_S = 0.1  # 50ms target; generous CI margin explicitly allowed by brief.


async def measured[T](operation: Awaitable[T]) -> tuple[T, float]:
    """Measure the full gap, including a ticker whose own wakeup was delayed."""
    gaps: list[float] = []
    done = False

    async def ticker() -> None:
        previous = time.perf_counter()
        while True:
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - previous)
            previous = now
            if done:
                return

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0)
    try:
        result = await operation
    finally:
        done = True
        await task
    return result, max(gaps)


def manager(factory: Callable[[str], str] = lambda _name: "session-f1") -> BridgeSessionManager:
    return BridgeSessionManager(session_id_factory=factory, idle_timeout_s=3600, max_pending_events=50, long_poll_timeout_s=1)


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://f1.test")


class RequestResponsiveness(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.dict("os.environ", SOLET_LOOP_EXECUTOR_WORKERS="4", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="32")
        settings.start()
        self.addCleanup(settings.stop)

    async def asyncTearDown(self) -> None:
        await close_loop_executors()

    def responsive(self, gap: float, label: str) -> None:
        print(f"F1 ticker {label}: max_gap={gap:.6f}s", flush=True)
        self.assertLess(gap, MAX_GAP_S, f"{label}: blocking backend stalled the serving loop")

    async def test_bridge_open(self) -> None:
        for delay, fail in ((0.0, False), (SLOW_S, False), (SLOW_S, True)):
            with self.subTest(delay=delay, fail=fail):
                names: list[str] = []

                def factory(_name: str) -> str:
                    names.append(threading.current_thread().name)
                    time.sleep(delay)
                    if fail:
                        raise RuntimeError("session DB unavailable")
                    return "session-f1"

                mgr = manager(factory)
                app = FastAPI()
                http_routes._register_bridge_lifecycle_routes(app, bridge_manager=mgr, peer_registry=MagicMock(), agent_messaging_service=MagicMock(), long_poll_timeout_s=1)  # noqa: SLF001
                async with client(app) as http:
                    response, gap = await measured(http.post("/api/v1/bridge/open", json={"parent_pid": 42, "caller_agent_session_id": " caller "}))
                self.assertEqual(response.status_code, 500 if fail else 200)
                if not fail:
                    body = response.json()
                    self.assertEqual(set(body), {"bridge_id", "session_id", "server_lifecycle_id", "long_poll_timeout_seconds"})
                    self.assertEqual(body["session_id"], "session-f1")
                    bridge = mgr.get(body["bridge_id"])
                    assert bridge is not None
                    self.assertEqual((bridge.parent_pid, bridge.caller_agent_session_id), (42, "caller"))
                self.assertTrue(names and all(name.startswith("solet-bridge-") for name in names), names)
                self.responsive(gap, f"open fail={fail} delay={delay}")

    async def test_activity_mirror(self) -> None:
        for delay, fail in ((0.0, False), (SLOW_S, False), (SLOW_S, True)):
            with self.subTest(delay=delay, fail=fail):
                mgr = manager()
                bridge = mgr.open("")
                bridge.agent_instance_id = "agi-f1"
                stamps: list[str] = []
                registry = MagicMock()

                def mirror(_identity: str, stamp: str) -> int:
                    time.sleep(delay)
                    stamps.append(stamp)
                    if fail:
                        raise RuntimeError("mirror DB unavailable")
                    return 1

                registry.stamp_model_activity.side_effect = mirror
                middleware = route_activity.make_model_activity_middleware(mgr, registry)
                request = Request({"type": "http", "path": f"/api/v1/bridge/{bridge.bridge_id}/peer/inbox", "headers": []})

                async def handler(_request: Request) -> JSONResponse:
                    return JSONResponse({"kept": True}, status_code=207)

                async def invoke() -> None:
                    started = time.perf_counter()
                    response = await middleware(request, handler)
                    elapsed = time.perf_counter() - started
                    self.assertEqual(response.status_code, 207)
                    self.assertEqual(response.body, b'{"kept":true}')
                    self.assertTrue(bridge.last_model_activity_at)
                    self.assertLess(elapsed, MAX_GAP_S, "durable mirror delayed the response")
                    stamp = bridge.last_model_activity_at
                    deadline = time.monotonic() + 2
                    while not stamps and time.monotonic() < deadline:
                        await asyncio.sleep(0.005)
                    self.assertEqual(stamps, [stamp])

                _, gap = await measured(invoke())
                self.responsive(gap, f"activity fail={fail} delay={delay}")

    async def test_diagnostics_never_hold_registry_lock(self) -> None:
        mgr = manager()
        bridge = mgr.open("")
        for event in ("open", "identity_bound", "close"):
            with self.subTest(event=event):
                entered = threading.Event()
                diagnostics = MagicMock(spec=BridgeDiagnostics)

                def emit(*_args: object, **_kwargs: object) -> None:
                    entered.set()
                    time.sleep(SLOW_S)

                diagnostics.emit.side_effect = emit
                mgr._diagnostics = diagnostics  # noqa: SLF001
                operation = {"open": lambda: mgr.open(""), "identity_bound": lambda: mgr.record_identity_bound(bridge.bridge_id, "agi-f1"), "close": lambda: mgr.close(bridge.bridge_id)}[event]

                async def probe() -> None:
                    task = asyncio.create_task(asyncio.to_thread(operation))
                    while not entered.is_set():
                        await asyncio.sleep(0.001)
                    mgr.get(bridge.bridge_id)
                    await task

                _, gap = await measured(probe())
                self.responsive(gap, f"diagnostic {event}")

    async def test_bounded_pools_wait_and_keep_cancelled_mutations(self) -> None:
        for run, workers, prefix in ((bridge_work.run_bridge_work, 4, "solet-bridge-"),):
            with self.subTest(pool=prefix):
                release = threading.Event()
                guard = threading.Lock()
                names: set[str] = set()
                active = 0
                peak = 0
                completed = 0
                identity: ContextVar[str] = ContextVar("f1-test-identity", default="missing")
                identity.set("request-identity")

                def blocking() -> str:
                    nonlocal active, peak, completed
                    with guard:
                        names.add(threading.current_thread().name)
                        active += 1
                        peak = max(peak, active)
                    release.wait(2)
                    with guard:
                        active -= 1
                        completed += 1
                    return identity.get()

                async def burst() -> None:
                    tasks = [asyncio.create_task(run(blocking)) for _ in range(workers + 40)]
                    try:
                        await asyncio.sleep(0.05)
                        # Cancel a running caller: its mutation and admission slot
                        # remain until actual completion. Other callers still wait.
                        tasks[0].cancel()
                        await asyncio.sleep(0.05)
                        (status,) = loop_executor_status()
                        self.assertEqual((status.family, status.pending, status.waiting), (ExecutorFamily.BRIDGE, 36, 8))
                        self.assertFalse(tasks[-1].done(), "full queue must wait, never refuse")
                        release.set()
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        self.assertIsInstance(results[0], asyncio.CancelledError)
                        self.assertEqual(results[1:], ["request-identity"] * (len(tasks) - 1))
                    finally:
                        release.set()
                        await asyncio.gather(*tasks, return_exceptions=True)

                _, gap = await measured(burst())
                self.assertEqual(completed, workers + 40)
                self.assertLessEqual(peak, workers)
                self.assertTrue(names and all(name.startswith(prefix) for name in names))
                self.responsive(gap, f"bounded {prefix}")

    async def test_activity_capture_order_and_executor_failure(self) -> None:
        mgr = manager()
        bridge = mgr.open("")
        bridge.agent_instance_id = "agi-captured"
        registry = MagicMock()
        mirrored: list[str] = []

        def mirror(_identity: str, stamp: str) -> int:
            time.sleep(SLOW_S)
            mirrored.append(stamp)
            return 1

        registry.stamp_model_activity.side_effect = mirror
        middleware = route_activity.make_model_activity_middleware(mgr, registry)
        request = Request({"type": "http", "path": f"/api/v1/bridge/{bridge.bridge_id}/peer/send", "headers": []})

        async def handler(_request: Request) -> JSONResponse:
            return JSONResponse({"accepted": True})

        async def invoke() -> None:
            await middleware(request, handler)
            first = bridge.last_model_activity_at
            await asyncio.sleep(0.001)
            await middleware(request, handler)
            second = bridge.last_model_activity_at
            self.assertEqual(bridge.prev_model_activity_at, first)
            self.assertNotEqual(first, second)
            deadline = time.monotonic() + 2
            while len(mirrored) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            self.assertEqual(mirrored, [first, second], "mirror order and captured times must be exact")

        _, gap = await measured(invoke())
        self.responsive(gap, "ordered captured activity")
        pool = loop_executors._pool(ExecutorFamily.BRIDGE)  # noqa: SLF001
        with patch.object(pool._executor, "submit", side_effect=RuntimeError("executor unavailable")), self.assertLogs(route_activity.logger, level="WARNING") as logs:  # noqa: SLF001
            response = await middleware(request, handler)
            await asyncio.sleep(0.01)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(bridge.last_model_activity_at)
        self.assertIn("in-memory stamp kept", str(logs.output))

    async def test_diagnostic_disk_failure_keeps_open_response(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail):
                mgr = manager()
                diagnostics = BridgeDiagnostics("test")
                mgr._diagnostics = diagnostics  # noqa: SLF001

                def append(_encoded: bytes) -> None:
                    time.sleep(SLOW_S)
                    if fail:
                        raise OSError("diagnostic disk unavailable")

                app = FastAPI()
                http_routes._register_bridge_lifecycle_routes(app, bridge_manager=mgr, peer_registry=MagicMock(), agent_messaging_service=MagicMock(), long_poll_timeout_s=1)  # noqa: SLF001
                with patch.object(diagnostics, "_append", side_effect=append):
                    async with client(app) as http:
                        response, gap = await measured(http.post("/api/v1/bridge/open", json={}))
                self.assertEqual(response.status_code, 200)
                self.responsive(gap, f"diagnostic disk fail={fail}")


class ExistingBodyOffloads(unittest.IsolatedAsyncioTestCase):
    """Exercise the existing body seams after switching their pool selection."""

    async def asyncTearDown(self) -> None:
        await close_loop_executors()

    async def test_platform_and_peer_bodies(self) -> None:
        cases = (
            ("POST", "process/search", {"query": "query"}, "_process_search_blocking"),
            ("POST", "process/schema", {"process_key": "key"}, "_process_schema_blocking"),
            ("POST", "process/call", {"process_key": "key", "arguments": {}}, "_process_call_blocking"),
            ("GET", "process/result/action", None, "_process_result_blocking"),
            ("GET", "download/blob", None, "_download_blocking"),
            ("POST", "peer/send", {"peer_id": "codex", "content": [{"type": "text", "text": "message"}]}, "_peer_send_impl"),
            ("POST", "peer/send_by_name", {"name": "Fixture", "content": "message"}, "_peer_send_by_name_impl"),
            ("POST", "peer/claim_role", {"name": "Fixture"}, "_peer_claim_role_impl"),
        )
        for method, suffix, body, implementation in cases:
            for delay, fail in ((0.0, False), (SLOW_S, False), (SLOW_S, True)):
                with self.subTest(path=suffix, delay=delay, fail=fail):
                    names: list[str] = []
                    app = FastAPI()
                    mgr = manager()
                    http_routes.register_routes(app, bridge_manager=mgr, peer_registry=MagicMock(), platform_surface=MagicMock(), agent_messaging_service=MagicMock(), config={"long_poll_timeout_seconds": 1})

                    def backend(*_args: object, **_kwargs: object) -> JSONResponse:
                        names.append(threading.current_thread().name)
                        time.sleep(delay)
                        return JSONResponse({"backend": implementation, "failed": fail}, status_code=503 if fail else 207)

                    with patch.object(http_routes, implementation, side_effect=backend):
                        async with client(app) as http:
                            response, gap = await measured(http.request(method, f"/api/v1/bridge/fixture/{suffix}", json=body))
                    self.assertEqual(response.status_code, 503 if fail else 207)
                    self.assertEqual(response.json(), {"backend": implementation, "failed": fail})
                    self.assertTrue(names and all(name.startswith("solet-bridge-") for name in names), names)
                    self.assertLess(gap, MAX_GAP_S, f"{suffix}: body stalled loop")
                    print(f"F1 ticker {suffix} fail={fail} delay={delay}: max_gap={gap:.6f}s", flush=True)


if __name__ == "__main__":
    unittest.main()
