"""FIFO and cancellation regressions for every local F5 admission boundary."""

from __future__ import annotations

import asyncio
import threading
import unittest
from collections.abc import Callable, Coroutine
from types import ModuleType
from typing import Any, cast

from plugins.agent_messaging_plugin.tests.f5_loop_responsiveness_test import CLASSES, load


def runner(module: ModuleType) -> Callable[[Callable[[], str]], Coroutine[Any, Any, str]]:
    return cast(Callable[[Callable[[], str]], Coroutine[Any, Any, str]], module._run_f5_blocking)


async def wait_started(started: threading.Event) -> None:
    for _ in range(1000):
        if started.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("backend worker did not start")


async def completion_arrival_order(module: ModuleType) -> list[str]:
    run = runner(module)
    started, release = threading.Event(), threading.Event()
    order: list[str] = []

    def first() -> str:
        order.append("A")
        started.set()
        release.wait(3)
        return "A"

    a = asyncio.create_task(run(first))
    await wait_started(started)
    b = asyncio.create_task(run(lambda: order.append("B") or "B"))
    await asyncio.sleep(0.02)
    assert order == ["A"] and not b.done(), "B must be queued before the later arrival"
    loop = asyncio.get_running_loop()
    third_created = asyncio.Event()
    third: list[asyncio.Task[str]] = []

    def arrival() -> None:
        third.append(asyncio.create_task(run(lambda: order.append("C") or "C")))
        third_created.set()

    # Arrive after the worker completes but before existing awaiters have
    # finished their cross-thread future/gather/shield scheduling round trips.
    active = module._f5_in_flight
    assert active is not None
    active.add_done_callback(lambda _: loop.call_soon_threadsafe(arrival))
    try:
        release.set()
        assert await a == "A"
        assert await b == "B"
        await third_created.wait()
        assert await third[0] == "C"
        return order
    finally:
        release.set()


async def cancelled_queued_order(module: ModuleType) -> list[str]:
    run = runner(module)
    started, release = threading.Event(), threading.Event()
    order: list[str] = []

    def first() -> str:
        order.append("A")
        started.set()
        release.wait(3)
        return "A"

    a = asyncio.create_task(run(first))
    await wait_started(started)
    b = asyncio.create_task(run(lambda: order.append("B") or "B"))
    c = asyncio.create_task(run(lambda: order.append("C") or "C"))
    try:
        await asyncio.sleep(0.02)
        b.cancel()
        try:
            await b
        except asyncio.CancelledError:
            pass
        assert not c.done() and order == ["A"], "a queued cancellation must not release the active owner"
        release.set()
        assert await a == "A"
        assert await c == "C"
        return order
    finally:
        release.set()


class AdmissionOrderTest(unittest.IsolatedAsyncioTestCase):
    async def test_completion_arrival_cannot_overtake_queued_caller(self) -> None:
        for name in CLASSES:
            with self.subTest(plugin=name):
                module, _ = load(name)
                order = await completion_arrival_order(module)
                self.assertEqual(order, ["A", "B", "C"], "queued caller B was overtaken by later caller C")

    async def test_queued_cancellation_retains_active_owner_and_next_turn(self) -> None:
        for name in CLASSES:
            with self.subTest(plugin=name):
                module, _ = load(name)
                self.assertEqual(await cancelled_queued_order(module), ["A", "C"])


if __name__ == "__main__":
    unittest.main()
