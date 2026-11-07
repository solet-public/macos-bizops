"""Named, bounded worker queues owned separately by each serving event loop.

Admission yields while the worker queue is full; it never rejects work or
blocks the loop. Queued calls wait on the loop and cancellation removes them
before native submission. Submitted calls retain capacity until the native
future finishes. Route context propagation
matches ``asyncio.to_thread``; action dispatch retains ``run_in_executor``'s
context behavior. This module does not change action scheduling or the serial
session-ledger ingestion fence.

``SOLET_LOOP_EXECUTOR_WORKERS`` (default 4) and
``SOLET_LOOP_EXECUTOR_QUEUE_LIMIT`` (default 32 queued, excluding workers) apply
when each pool is created. Invalid values produce an advisory and use the
named default. Read ``loop_executor_status``; drain/clear with
``close_loop_executors`` before changing settings and recreating pools.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import threading
import weakref
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import Final

logger = logging.getLogger(__name__)
DEFAULT_WORKERS: Final[int] = 4
DEFAULT_QUEUE_LIMIT: Final[int] = 32


class ExecutorFamily(StrEnum):
    ACTIONS = "actions"
    EMBEDDING = "embedding"
    SEED = "seed"
    BRIDGE = "bridge"
    STREAMABLE = "streamable"


@dataclass(frozen=True)
class ExecutorStatus:
    family: ExecutorFamily
    workers: int
    queue_limit: int
    pending: int
    waiting: int


def _setting(name: str, default: int, minimum: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
        if value >= minimum:
            return value
    except ValueError:
        pass
    logger.warning("Invalid %s=%r; using documented default %d", name, raw, default)
    return default


class _LoopExecutor:
    """Loop-confined admission; only native completion crosses threads.

    Waiters hold no executor queue slot. A completed worker transfers its slot
    directly to the oldest live waiter. The weak loop reference and emptied
    waiter deque let a closed loop be collected and its worker pool finalized.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, family: ExecutorFamily) -> None:
        self.family = family
        self.workers = _setting("SOLET_LOOP_EXECUTOR_WORKERS", DEFAULT_WORKERS, 1)
        self.queue_limit = _setting("SOLET_LOOP_EXECUTOR_QUEUE_LIMIT", DEFAULT_QUEUE_LIMIT, 0)
        self._loop_ref = weakref.ref(loop)
        self._executor = ThreadPoolExecutor(
            max_workers=self.workers,
            thread_name_prefix=f"solet-{family}-{id(loop):x}",
        )
        self._finalizer = weakref.finalize(loop, self._executor.shutdown, wait=False)
        self._pending = 0
        self._workers_in_use = 0
        self._worker_waiters: deque[asyncio.Future[None]] = deque()
        self._waiters: deque[asyncio.Future[None]] = deque()
        self._idle: asyncio.Future[None] | None = None

    def status(self) -> ExecutorStatus:
        return ExecutorStatus(self.family, self.workers, self.queue_limit, self._pending, len(self._waiters))

    async def _acquire(self) -> None:
        if self._pending < self.workers + self.queue_limit:
            self._pending += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if not waiter.cancelled():
                self._release()  # Completion transferred a slot before cancellation.
            elif waiter in self._waiters:
                self._waiters.remove(waiter)
            raise

    def _release(self) -> None:
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._pending -= 1
        if self._pending == 0 and self._idle is not None and not self._idle.done():
            self._idle.set_result(None)

    async def _acquire_worker(self) -> None:
        if self._workers_in_use < self.workers:
            self._workers_in_use += 1
            return
        waiter = asyncio.get_running_loop().create_future()
        self._worker_waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if not waiter.cancelled():
                self._release_worker()  # Completion already transferred a worker.
            elif waiter in self._worker_waiters:
                self._worker_waiters.remove(waiter)
            raise

    def _release_worker(self) -> None:
        while self._worker_waiters:
            waiter = self._worker_waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self._workers_in_use -= 1

    def _finished(self) -> None:
        self._release_worker()
        self._release()

    def _completed(self, _future: object) -> None:
        loop = self._loop_ref()
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._finished)
            except RuntimeError:
                logger.warning("Loop closed during %s worker completion", self.family)

    async def run[T](self, work: Callable[[], T]) -> T:
        await self._acquire()
        # Keep queued calls removable on the loop. ThreadPoolExecutor retains
        # cancelled native WorkItems until a worker dequeues them, so submitting
        # them early would let repeated cancellation bypass the queue bound.
        try:
            await self._acquire_worker()
        except BaseException:
            self._release()
            raise
        try:
            future = self._executor.submit(work)
        except BaseException:
            self._finished()
            raise
        future.add_done_callback(self._completed)
        # Cancellation of an awaiting caller must not release a submitted
        # worker slot or create a cancelled item in the native queue.
        return await asyncio.shield(asyncio.wrap_future(future))

    async def close(self) -> None:
        if self._pending:
            self._idle = asyncio.get_running_loop().create_future()
            await self._idle
            self._idle = None
        # All native work is finished; shutdown cannot wait on a backend here.
        self._executor.shutdown(wait=False)
        self._finalizer.detach()


_pools: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[ExecutorFamily, _LoopExecutor]] = weakref.WeakKeyDictionary()
_registry_lock: Final[threading.Lock] = threading.Lock()


def _pool(family: ExecutorFamily) -> _LoopExecutor:
    loop = asyncio.get_running_loop()
    with _registry_lock:
        pools = _pools.setdefault(loop, {})
        pool = pools.get(family)
        if pool is not None:
            return pool
        pool = _LoopExecutor(loop, family)
        pools[family] = pool
    logger.info("LOOP_EXECUTOR_CREATED family=%s loop=%x workers=%d queue_limit=%d pending_limit=%d", family, id(loop), pool.workers, pool.queue_limit, pool.workers + pool.queue_limit)
    return pool


async def run_blocking[**P, T](
    family: ExecutorFamily,
    function: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Route offload, retaining caller context, exact value and exceptions."""
    work = partial(function, *args, **kwargs)
    return await _pool(family).run(partial(contextvars.copy_context().run, work))


async def run_action[**P, T](
    process_key: str,
    function: Callable[P, T],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Isolate heavy handler families without changing poller ordering/claims."""
    family = ExecutorFamily.ACTIONS
    if process_key.startswith(("service_interface::embedding_service::", "service_interface::knowledge_service::")):
        family = ExecutorFamily.EMBEDDING
    elif process_key.startswith("plugin::seed_factory_plugin::"):
        family = ExecutorFamily.SEED
    return await _pool(family).run(partial(function, *args, **kwargs))


def loop_executor_status() -> tuple[ExecutorStatus, ...]:
    """Read the current loop's pools without creating a resource."""
    with _registry_lock:
        pools = tuple(_pools.get(asyncio.get_running_loop(), {}).values())
    return tuple(pool.status() for pool in pools)


async def close_loop_executors() -> None:
    """Drain and retire this loop's pools; the next call creates fresh pools.

    Call after stopping route/action admission. Cancellation of the close
    caller does not cancel the drain or release running worker capacity early.
    Closed-loop garbage collection also retires pools without a blocking join.
    """
    with _registry_lock:
        pools = _pools.pop(asyncio.get_running_loop(), {})
    await asyncio.shield(asyncio.gather(*(pool.close() for pool in pools.values())))
