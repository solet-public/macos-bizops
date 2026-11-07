"""Bounded per-loop workers: pressure, cancellation, context, errors and retirement.

Offline unittest coverage; native worker sleeps/events run with a concurrent
same-loop ticker. No live platform, database or session is used.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import sys
import threading
import time
import unittest
import weakref
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ananta/src"))

from ananta.core.runtime import loop_executors as executors  # noqa: E402
from ananta.core.runtime.loop_executors import (  # noqa: E402
    ExecutorFamily,
    close_loop_executors,
    loop_executor_status,
    run_action,
    run_blocking,
)


class Payload:
    def __init__(self) -> None:
        self.data = bytearray(65536)


class LoopExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.dict("os.environ", SOLET_LOOP_EXECUTOR_WORKERS="1", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="1")
        settings.start()
        self.addCleanup(settings.stop)

    async def asyncTearDown(self) -> None:
        await close_loop_executors()

    async def until(self, predicate: Callable[[], bool]) -> None:
        for _ in range(1000):
            if predicate():
                return
            await asyncio.sleep(0.001)
        self.fail("worker did not reach the named observation")

    async def test_pressure_bounds_worker_queue_fifo_and_keeps_loop_live(self) -> None:
        release = threading.Event()
        entered: list[int] = []
        gaps: list[float] = []

        def work(number: int) -> int:
            entered.append(number)
            release.wait(2)
            return number

        async def ticker() -> None:
            previous = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        tick = asyncio.create_task(ticker())
        tasks = [asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, work, number)) for number in range(9)]
        try:
            await self.until(lambda: bool(entered))
            await asyncio.sleep(0.3)
            (status,) = loop_executor_status()
            self.assertEqual((status.workers, status.queue_limit, status.pending, status.waiting), (1, 1, 2, 7))
            self.assertEqual(entered, [0])
            self.assertEqual(await asyncio.to_thread(lambda: "default free"), "default free")
        finally:
            release.set()
            results = await asyncio.gather(*tasks)
            await asyncio.sleep(0.01)
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)
        self.assertEqual(results, list(range(9)))
        self.assertEqual(entered, list(range(9)))
        maximum = max(gaps)
        print(f"pressure: maximum same-loop ticker gap {maximum:.4f}s")
        self.assertLess(maximum, 0.1)
        (status,) = loop_executor_status()
        self.assertEqual((status.pending, status.waiting), (0, 0))

    async def test_cancelled_running_work_keeps_capacity_until_native_completion(self) -> None:
        with patch.dict("os.environ", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="0"):
            entered = threading.Event()
            release = threading.Event()

            def work() -> None:
                entered.set()
                release.wait(2)

            first = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, work))
            await self.until(entered.is_set)
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, lambda: 7))
            try:
                await asyncio.sleep(0.02)
                (status,) = loop_executor_status()
                self.assertEqual((status.pending, status.waiting), (1, 1))
                self.assertFalse(second.done())
            finally:
                release.set()
            self.assertEqual(await second, 7)

    async def test_cancelled_waiter_does_not_leak_a_slot(self) -> None:
        with patch.dict("os.environ", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="0"):
            release = threading.Event()
            first = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, release.wait, 2))
            await self.until(lambda: bool(loop_executor_status()))
            waiting = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, lambda: 5))
            try:
                await self.until(lambda: loop_executor_status()[0].waiting == 1)
                waiting.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await waiting
                self.assertEqual(loop_executor_status()[0].waiting, 0)
            finally:
                release.set()
                await first
            self.assertEqual(await run_blocking(ExecutorFamily.BRIDGE, lambda: 6), 6)

    async def test_cancelled_queued_work_never_runs_and_returns_capacity(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        called: list[str] = []

        def work() -> None:
            entered.set()
            release.wait(2)

        first = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, work))
        await self.until(entered.is_set)
        queued = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, called.append, "cancelled"))
        try:
            await self.until(lambda: loop_executor_status()[0].pending == 2)
            queued.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await queued
            await self.until(lambda: loop_executor_status()[0].pending == 1)
        finally:
            release.set()
            await first
        self.assertEqual(called, [])
        self.assertEqual(await run_blocking(ExecutorFamily.BRIDGE, lambda: "next"), "next")

    async def test_native_submit_and_backend_failures_preserve_error_and_capacity(self) -> None:
        failure = RuntimeError("native submit failed")
        pool = executors._pool(ExecutorFamily.BRIDGE)
        with patch.object(pool._executor, "submit", side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                await run_blocking(ExecutorFamily.BRIDGE, lambda: None)
        self.assertIs(caught.exception, failure)
        self.assertEqual(loop_executor_status()[0].pending, 0)

        def broken() -> None:
            time.sleep(0.3)
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            await run_blocking(ExecutorFamily.BRIDGE, broken)
        self.assertIs(caught.exception, failure)
        self.assertEqual(await run_blocking(ExecutorFamily.BRIDGE, lambda: 9), 9)

    async def test_repeated_queued_cancellation_removes_work_and_payloads(self) -> None:
        started, release = threading.Event(), threading.Event()
        retained: weakref.WeakSet[Payload] = weakref.WeakSet()
        called: list[int] = []
        gaps: list[float] = []

        def slow_backend() -> str:
            started.set()
            release.wait()
            return "successful slow result"

        def consume(payload: Payload) -> int:
            called.append(len(payload.data))
            return len(payload.data)

        async def ticker() -> None:
            previous = time.monotonic()
            while True:
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        tick = asyncio.create_task(ticker())
        first = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, slow_backend))
        try:
            await self.until(started.is_set)
            pool = executors._pool(ExecutorFamily.BRIDGE)
            await asyncio.sleep(0.3)
            for _ in range(64):
                payload = Payload()
                retained.add(payload)
                queued = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, consume, payload))
                await self.until(lambda: loop_executor_status()[0].pending == 2)
                queued.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await queued
                await self.until(lambda: loop_executor_status()[0].pending == 1)
                del queued, payload
            # Let the cancelled task's final wakeup release its local frame.
            await asyncio.sleep(0)
            (status,) = loop_executor_status()
            native_queued = pool._executor._work_queue.qsize()
            print(f"cancelled queue: pending={status.pending} waiting={status.waiting} native_queued={native_queued} retained_payloads={len(retained)} ticker_max={max(gaps):.4f}s")
            self.assertEqual((status.pending, status.waiting), (1, 0))
            self.assertLess(max(gaps), 0.1)
            self.assertEqual(native_queued, 0, "Cancelled native work items must not retain payloads after admission returns")
            self.assertEqual(len(retained), 0)
            self.assertEqual(called, [])
        finally:
            release.set()
            self.assertEqual(await first, "successful slow result")
            tick.cancel()
            await asyncio.gather(tick, return_exceptions=True)

    async def test_route_context_is_copied_and_action_context_is_not(self) -> None:
        context = contextvars.ContextVar("test-request", default="unset")
        token = context.set("request")
        try:
            self.assertEqual(await run_blocking(ExecutorFamily.BRIDGE, context.get), "request")
            self.assertEqual(await run_action("plugin::agent_messaging_plugin::report_alive", context.get), "unset")
        finally:
            context.reset(token)

    async def test_heavy_families_do_not_consume_control_or_route_capacity(self) -> None:
        release = threading.Event()
        slow = [
            asyncio.create_task(run_action(key, release.wait, 2))
            for key in (
                "service_interface::embedding_service::generate_embeddings",
                "plugin::seed_factory_plugin::assemble_seed",
            )
        ]
        try:
            await self.until(lambda: len(loop_executor_status()) == 2)
            for family in (ExecutorFamily.BRIDGE, ExecutorFamily.STREAMABLE):
                name = await run_blocking(family, lambda: threading.current_thread().name)
                self.assertTrue(name.startswith(f"solet-{family}-"), name)
            name = await run_action("service_interface::session_ledger_service::ingest_raw_chunk", lambda: threading.current_thread().name)
            self.assertTrue(name.startswith("solet-actions-"), name)
        finally:
            release.set()
            await asyncio.gather(*slow)

    async def test_creation_logged_once_drain_clear_recreate_and_settings(self) -> None:
        with self.assertLogs(executors.logger, level="INFO") as logs:
            await run_blocking(ExecutorFamily.BRIDGE, lambda: 1)
            await run_blocking(ExecutorFamily.BRIDGE, lambda: 2)
        self.assertEqual(len(logs.output), 1)
        self.assertIn("workers=1 queue_limit=1 pending_limit=2", logs.output[0])
        await close_loop_executors()
        self.assertEqual(loop_executor_status(), ())
        with patch.dict("os.environ", SOLET_LOOP_EXECUTOR_WORKERS="2", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="3"):
            with self.assertLogs(executors.logger, level="INFO") as logs:
                await run_blocking(ExecutorFamily.BRIDGE, lambda: None)
            (status,) = loop_executor_status()
            self.assertEqual((status.workers, status.queue_limit), (2, 3))
        self.assertEqual(len(logs.output), 1)

    async def test_cancelled_close_still_drains_accepted_work(self) -> None:
        release = threading.Event()
        work = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, release.wait, 2))
        await self.until(lambda: bool(loop_executor_status()))
        close = asyncio.create_task(close_loop_executors())
        try:
            await asyncio.sleep(0.01)
            self.assertFalse(close.done())
            close.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await close
        finally:
            release.set()
            self.assertTrue(await work)
            await asyncio.sleep(0.01)
        self.assertEqual(loop_executor_status(), ())

    async def test_invalid_settings_are_advisory_and_success_path_remains_open(self) -> None:
        with patch.dict("os.environ", SOLET_LOOP_EXECUTOR_WORKERS="odd", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="-1"):
            with self.assertLogs(executors.logger, level="WARNING") as logs:
                self.assertEqual(await run_blocking(ExecutorFamily.BRIDGE, lambda: "handled"), "handled")
        self.assertEqual(len(logs.output), 2)
        (status,) = loop_executor_status()
        self.assertEqual((status.workers, status.queue_limit), (4, 32))


class SeparateLoopTests(unittest.TestCase):
    def test_closed_contended_loop_is_collected_and_retires_its_pool(self) -> None:
        loop = asyncio.new_event_loop()
        loop_ref = weakref.ref(loop)
        release = threading.Event()

        async def run() -> None:
            first = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, release.wait, 2))
            second = asyncio.create_task(run_blocking(ExecutorFamily.BRIDGE, lambda: "queued"))
            try:
                for _ in range(1000):
                    status = loop_executor_status()
                    if status and status[0].pending == 2:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(loop_executor_status()[0].pending, 2)
            finally:
                release.set()
                self.assertEqual(await asyncio.gather(first, second), [True, "queued"])

        with patch.dict("os.environ", SOLET_LOOP_EXECUTOR_WORKERS="1", SOLET_LOOP_EXECUTOR_QUEUE_LIMIT="1"):
            try:
                loop.run_until_complete(run())
            finally:
                loop.close()
        del loop
        gc.collect()
        try:
            self.assertIsNone(loop_ref(), "A drained worker queue must not retain its closed event loop")
        finally:
            # Retire a pool even if the regression detects strong retention.
            remaining = loop_ref()
            if remaining is not None:
                for pool in executors._pools.pop(remaining, {}).values():
                    pool._finalizer()

    def test_two_serving_loops_own_distinct_pools(self) -> None:
        barrier = threading.Barrier(2)
        names: list[str] = []
        errors: list[BaseException] = []

        def work() -> str:
            barrier.wait(2)
            return threading.current_thread().name

        async def run() -> None:
            try:
                names.append(await run_blocking(ExecutorFamily.BRIDGE, work))
            finally:
                await close_loop_executors()

        def thread() -> None:
            try:
                asyncio.run(run())
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=thread) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join(3)
        self.assertEqual(errors, [])
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])
        self.assertTrue(all(name.startswith("solet-bridge-") for name in names))


if __name__ == "__main__":
    unittest.main()
