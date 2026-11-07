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
from collections.abc import Awaitable
from contextvars import ContextVar
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "session_shipper_bootstrap_plugin" / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "src"))

# ruff: noqa: E402 -- use this checkout, including in a shipped profile.
from session_shipper_bootstrap_plugin import pairing_routes as pairing

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

    async def test_pairing_initiate(self) -> None:
        for delay, fail in ((0.0, False), (SLOW_S, False), (SLOW_S, True)):
            with self.subTest(delay=delay, fail=fail):
                ledger = MagicMock(spec=pairing.PairingLedgerProtocol)
                order: list[str] = []

                def get(_deployment: str) -> dict[str, object]:
                    order.append("get")
                    time.sleep(delay)
                    if fail:
                        raise RuntimeError("ledger unavailable")
                    return {"pairing_status": "pending", "machine_id": "machine"}

                def persist(**_kwargs: object) -> None:
                    order.append("persist")

                def scrypt(_token: str, _salt: bytes) -> bytes:
                    order.append("scrypt")
                    return b"hashed"

                ledger.pairing_get_deployment.side_effect = get
                ledger.pairing_persist_token.side_effect = persist
                app = FastAPI()
                pairing.register_session_ledger_pairing_routes(app, ledger=ledger)
                with patch.object(pairing, "_scrypt_hash", side_effect=scrypt):
                    async with client(app) as http:
                        response, gap = await measured(http.post(pairing.PAIRING_INITIATE_ROUTE, json={"deployment_id": "dep", "machine_id": "machine"}))
                self.assertEqual(response.status_code, 500 if fail else 200)
                if not fail:
                    self.assertEqual(set(response.json()), {"user_code", "pairing_token", "expires_in_seconds"})
                    self.assertEqual(response.json()["expires_in_seconds"], 600)
                    self.assertEqual(order, ["get", "scrypt", "persist"])
                self.responsive(gap, f"pairing initiate fail={fail} delay={delay}")

    async def test_pairing_poll_and_compensation(self) -> None:
        for delay, fail in ((0.0, False), (SLOW_S, False), (SLOW_S, True)):
            with self.subTest(delay=delay, fail=fail):
                ledger = MagicMock(spec=pairing.PairingLedgerProtocol)
                order: list[str] = []
                ledger.pairing_get_deployment.return_value = {"pairing_status": "approved"}

                def verify(**_kwargs: object) -> bool:
                    order.append("verify")
                    return True

                def mint(**_kwargs: object) -> dict[str, str]:
                    order.append("mint")
                    time.sleep(delay)
                    if fail:
                        raise pairing.CrossStoreOrphanMintError(client_id="orphan", deployment_id="dep", original=RuntimeError("state unavailable"))
                    return {"client_id": "client", "client_secret": "secret"}

                def compensate(**kwargs: object) -> str:
                    order.append("compensate")
                    self.assertEqual(kwargs["client_id"], "orphan")
                    return "revoked"

                ledger.pairing_verify_token.side_effect = verify
                ledger.pairing_mint_and_transition_to_paired.side_effect = mint
                ledger.pairing_compensate_orphan_mint.side_effect = compensate
                app = FastAPI()
                pairing.register_session_ledger_pairing_routes(app, ledger=ledger)
                async with client(app) as http:
                    response, gap = await measured(http.post(pairing.PAIRING_POLL_ROUTE, json={"deployment_id": "dep", "pairing_token": "token"}))
                self.assertEqual(response.status_code, 500 if fail else 200)
                self.assertEqual(order, ["verify", "mint", "compensate"] if fail else ["verify", "mint"])
                self.assertEqual(response.json(), {"detail": {"error": "internal_server_error"}} if fail else {"status": "paired", "client_id": "client", "client_secret": "secret"})
                self.responsive(gap, f"pairing poll fail={fail} delay={delay}")

    async def test_pairing_poll_remains_single_use(self) -> None:
        status = "approved"
        minted = 0
        ledger = MagicMock(spec=pairing.PairingLedgerProtocol)
        ledger.pairing_get_deployment.side_effect = lambda _deployment: {"pairing_status": status}
        ledger.pairing_verify_token.return_value = True

        def mint(**_kwargs: object) -> dict[str, str]:
            nonlocal status, minted
            time.sleep(SLOW_S)
            minted += 1
            status = "paired"
            return {"client_id": "once", "client_secret": "one-secret"}

        ledger.pairing_mint_and_transition_to_paired.side_effect = mint
        app = FastAPI()
        pairing.register_session_ledger_pairing_routes(app, ledger=ledger)
        async with client(app) as http:

            async def poll_twice() -> list[httpx.Response]:
                return list(await asyncio.gather(*(http.post(pairing.PAIRING_POLL_ROUTE, json={"deployment_id": "dep", "pairing_token": "token"}) for _ in range(2))))

            responses, gap = await measured(poll_twice())
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        self.assertEqual(minted, 1)
        self.responsive(gap, "single-use concurrent pairing")

    async def test_bounded_pools_wait_and_keep_cancelled_mutations(self) -> None:
        for module, run, workers, prefix in (
            (pairing, pairing._run_request_work, 1, "pairing"),  # noqa: SLF001
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


if __name__ == "__main__":
    unittest.main()
