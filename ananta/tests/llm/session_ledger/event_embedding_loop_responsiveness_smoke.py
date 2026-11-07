"""Guard the existing event-embedding worker's loop responsiveness.

This passes on the measured production implementation before and after this
tests-only change. It protects the owned-worker placement; it does not claim
to reproduce production DB contention or prove deployment-wide loop latency.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ananta" / "src"))

from ananta.services.session_ledger_service.embedding_drain import (  # noqa: E402
    _start_event_embedding_drain,
)
from ananta.services.session_ledger_service.summary_executor import (  # noqa: E402
    BoundedSummaryExecutor,
)

_BACKEND_DELAY_SECONDS = 0.3
_LOOP_GAP_LIMIT_SECONDS = 0.1
_TICK_SECONDS = 0.005
_FIXTURE_TIMEOUT_SECONDS = 2.0


class _SlowWriter:
    """A synchronous DB/native stand-in reached through the real drain worker."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.finished = threading.Event()
        self.thread_id: int | None = None
        self.page_sizes: list[int] = []

    def drain_missing_events(self, *, page_size: int) -> dict[str, int]:
        self.thread_id = threading.get_ident()
        self.entered.set()
        time.sleep(_BACKEND_DELAY_SECONDS)
        self.page_sizes.append(page_size)
        self.finished.set()
        return {"events_embedded": 1}


async def _wait_for(event: threading.Event) -> None:
    while not event.is_set():
        await asyncio.sleep(_TICK_SECONDS)


class DrainLoopResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    """A slow drain must leave a same-loop ticker and trivial action responsive."""

    async def test_slow_drain_keeps_loop_and_trivial_action_responsive(self) -> None:
        loop = asyncio.get_running_loop()
        writer = _SlowWriter()
        executor = BoundedSummaryExecutor(name="ledger-embedding-loop-guard")
        stop = asyncio.Event()
        gaps: list[float] = []

        async def ticker() -> None:
            previous = loop.time()
            while not stop.is_set():
                await asyncio.sleep(_TICK_SECONDS)
                now = loop.time()
                gaps.append(now - previous)
                previous = now

        async def trivial_action() -> str:
            await asyncio.sleep(0)
            self.assertTrue(writer.entered.is_set())
            self.assertFalse(writer.finished.is_set(), "action must finish during the slow backend call")
            return "completed"

        ticker_task = asyncio.create_task(ticker())
        try:
            # Prime the ticker before calling the synchronous action entry so
            # an accidental inline drain appears in its measured gap.
            await asyncio.sleep(0)
            started_at = loop.time()
            outcome = _start_event_embedding_drain(executor, writer, 100)
            dispatch_seconds = loop.time() - started_at
            self.assertEqual(outcome, {"drainer": "started", "page_size": 100})
            await asyncio.wait_for(_wait_for(writer.entered), _FIXTURE_TIMEOUT_SECONDS)
            action_started_at = loop.time()
            action = asyncio.create_task(trivial_action())
            self.assertEqual(await action, "completed")
            action_seconds = loop.time() - action_started_at
            self.assertEqual(
                _start_event_embedding_drain(executor, writer, 100),
                {"drainer": "already_running", "page_size": 100},
            )
            await asyncio.wait_for(_wait_for(writer.finished), _FIXTURE_TIMEOUT_SECONDS)
        finally:
            stop.set()
            await ticker_task

        self.assertEqual(writer.page_sizes, [100], "only the owned drain may reach the backend")
        self.assertNotEqual(writer.thread_id, threading.get_ident())
        self.assertTrue(gaps, "the ticker must run concurrently with the slow backend")
        self.assertLess(dispatch_seconds, _LOOP_GAP_LIMIT_SECONDS)
        self.assertLess(action_seconds, _LOOP_GAP_LIMIT_SECONDS)
        self.assertLess(max(gaps), _LOOP_GAP_LIMIT_SECONDS, f"same-loop maximum gap: {max(gaps):.6f}s")
        print(f"drain dispatch={dispatch_seconds:.6f}s action={action_seconds:.6f}s max_gap={max(gaps):.6f}s")


if __name__ == "__main__":
    unittest.main()
