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
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "g_suite_plugin" / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "src"))

# ruff: noqa: E402 -- use this checkout, including in a shipped profile.
from g_suite_plugin.oauth import oauth_routes as google

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


def client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url="http://f1.test")


class RequestResponsiveness(unittest.IsolatedAsyncioTestCase):
    def responsive(self, gap: float, label: str) -> None:
        print(f"F1 ticker {label}: max_gap={gap:.6f}s", flush=True)
        self.assertLess(gap, MAX_GAP_S, f"{label}: blocking backend stalled the serving loop")

    async def vendor(self, module: Any, exchange_name: str, path: str) -> None:
        for delay, fail in ((0.0, False), (SLOW_S, False), (SLOW_S, True)):
            with self.subTest(vendor=module.__name__, delay=delay, fail=fail):
                pending = {"nonce": "verifier"}
                order: list[str] = []
                loader = MagicMock()
                store = MagicMock()

                def load() -> SimpleNamespace:
                    self.assertNotIn("nonce", pending, "nonce must be consumed before offload")
                    order.append("load")
                    return SimpleNamespace(client_id="client", client_secret="secret", redirect_uri="http://f1.test/callback")

                def exchange(*_args: object, **kwargs: object) -> SimpleNamespace:
                    order.append("exchange")
                    time.sleep(delay)
                    self.assertEqual(kwargs["code"], "code")
                    if exchange_name == "exchange_code":
                        self.assertEqual(kwargs["code_verifier"], "verifier")
                    if fail:
                        raise RuntimeError("vendor unavailable")
                    return SimpleNamespace(refresh_token="refresh", access_token="access", expires_in=3600)

                def save(**kwargs: object) -> None:
                    order.append("store")
                    self.assertEqual(kwargs, {"refresh_token": "refresh", "access_token": "access", "expires_in": 3600})

                loader.load.side_effect = load
                store.store_initial_tokens.side_effect = save
                app = FastAPI()
                app.include_router(module.create_oauth_router(store, loader, pending))
                with patch.object(module, exchange_name, side_effect=exchange):
                    async with client(app) as http:
                        response, gap = await measured(http.get(path, params={"state": "nonce", "code": "code"}))
                        replay = await http.get(path, params={"state": "nonce", "code": "code"})
                self.assertEqual(response.status_code, 502 if fail else 200)
                self.assertEqual(replay.status_code, 400)
                self.assertEqual(order, ["load", "exchange"] if fail else ["load", "exchange", "store"])
                if not fail:
                    self.assertIn("Tokens stored.", response.text)
                self.responsive(gap, f"{module.__name__} fail={fail} delay={delay}")

    async def test_google_callback(self) -> None:
        await self.vendor(google, "exchange_code", "/oauth/google/callback")

    async def test_vendor_slow_config_and_storage_failures(self) -> None:
        for module, exchange_name, path in ((google, "exchange_code", "/oauth/google/callback"),):
            for stage in ("load", "store"):
                with self.subTest(vendor=module.__name__, stage=stage):
                    pending = {"nonce": "verifier"}
                    loader, store = MagicMock(), MagicMock()
                    loader.load.return_value = SimpleNamespace(client_id="client", client_secret="secret", redirect_uri="http://f1.test/callback")

                    def failure(**_kwargs: object) -> None:
                        time.sleep(SLOW_S)
                        error_type = module.AppConfigError if stage == "load" else module.TokenStoreError
                        raise error_type("unavailable", "backend unavailable")

                    target = loader.load if stage == "load" else store.store_initial_tokens
                    target.side_effect = failure
                    app = FastAPI()
                    app.include_router(module.create_oauth_router(store, loader, pending))
                    tokens = SimpleNamespace(refresh_token="refresh", access_token="access", expires_in=3600)
                    with patch.object(module, exchange_name, return_value=tokens):
                        async with client(app) as http:
                            response, gap = await measured(http.get(path, params={"state": "nonce", "code": "code"}))
                    self.assertEqual(response.status_code, 500)
                    self.assertNotIn("nonce", pending)
                    self.responsive(gap, f"{module.__name__} failed {stage}")

    async def test_bounded_pools_wait_and_keep_cancelled_mutations(self) -> None:
        for module, run, workers, prefix in (
            (google, google._run_request_work, 1, "google-oauth"),  # noqa: SLF001
        ):
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
                        self.assertLessEqual(module._REQUEST_EXECUTOR._work_queue.qsize(), 32)  # noqa: SLF001
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

    async def test_callback_order_while_admission_waits(self) -> None:
        pending = {"first": "verifier", "second": "verifier"}
        loader, store = MagicMock(), MagicMock()
        loader.load.return_value = SimpleNamespace(client_id="client", client_secret="secret", redirect_uri="http://f1.test/callback")
        order: list[str] = []

        async def admission(function: Callable[..., Any], *args: Any) -> Any:
            if args[0] == "first":
                await asyncio.sleep(SLOW_S)
            return function(*args)

        def exchange(*_args: object, **kwargs: object) -> SimpleNamespace:
            code = str(kwargs["code"])
            order.append(code)
            return SimpleNamespace(refresh_token="refresh", access_token=code, expires_in=3600)

        app = FastAPI()
        app.include_router(google.create_oauth_router(store, loader, pending))
        with patch.object(google, "_run_request_work", side_effect=admission), patch.object(google, "exchange_code", side_effect=exchange):
            async with client(app) as http:

                async def callbacks() -> list[httpx.Response]:
                    return list(await asyncio.gather(*(http.get("/oauth/google/callback", params={"state": code, "code": code}) for code in ("first", "second"))))

                responses, gap = await measured(callbacks())
        self.assertEqual([response.status_code for response in responses], [200, 200])
        self.assertEqual(order, ["first", "second"], "async admission must preserve callback mutation order")
        self.assertEqual(pending, {})
        self.assertLess(gap, MAX_GAP_S)
        print(f"F1 ticker ordered admission: max_gap={gap:.6f}s", flush=True)


if __name__ == "__main__":
    unittest.main()
