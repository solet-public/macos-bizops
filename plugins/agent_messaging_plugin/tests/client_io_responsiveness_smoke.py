"""Offline concurrent-loop checks for the standalone bridge and seat helpers."""

from __future__ import annotations

import asyncio
import contextlib
import io
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from mcp.types import CallToolRequest, CallToolRequestParams

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))


from agent_messaging_plugin import seat_rotation_helper as rotation  # noqa: E402
from agent_messaging_plugin.mcp_bridge import __main__ as bridge  # noqa: E402
from agent_messaging_plugin.mcp_bridge.forwarder import BridgeHTTPError, Forwarder  # noqa: E402


class ClientResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_pool_capacity_cancellation_and_close(self) -> None:
        from ananta.core.runtime.port_manager import ClientIOExecutor

        pool = ClientIOExecutor("test-client-io", workers=1, pending=1)
        release = threading.Event()
        entered = threading.Event()
        order: list[int] = []

        def work(value: int) -> int:
            if value == 1:
                entered.set()
                release.wait()
            order.append(value)
            return value

        first = asyncio.create_task(pool.run(work, 1))
        while not entered.is_set():
            await asyncio.sleep(0.005)
        second = asyncio.create_task(pool.run(work, 2))
        third = asyncio.create_task(pool.run(work, 3))
        withdrawn = asyncio.create_task(pool.run(work, 4))
        await asyncio.sleep(0.01)
        second.cancel()
        withdrawn.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        with self.assertRaises(asyncio.CancelledError):
            await withdrawn
        pool.close()
        try:
            self.assertEqual(order, [])
            with self.assertRaisesRegex(RuntimeError, "closed"):
                await pool.run(work, 5)
        finally:
            release.set()
        self.assertEqual(await self.responsive(lambda: first), 1)
        self.assertEqual(await third, 3)
        self.assertEqual(order, [1, 2, 3])

    async def test_pool_backend_failure_releases_capacity(self) -> None:
        from ananta.core.runtime.port_manager import ClientIOExecutor

        pool = ClientIOExecutor("test-client-failure", workers=1, pending=0)

        def fail() -> None:
            time.sleep(0.3)
            raise OSError("backend failed")

        try:
            with self.assertRaisesRegex(OSError, "backend failed"):
                await self.responsive(lambda: pool.run(fail))
            self.assertEqual(await pool.run(lambda: "next"), "next")
        finally:
            pool.close()

    async def responsive[T](self, action: Callable[[], Awaitable[T]]) -> T:
        gaps: list[float] = []
        stop = asyncio.Event()

        async def ticker() -> None:
            previous = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.monotonic()
                gaps.append(now - previous)
                previous = now

        task = asyncio.create_task(ticker())
        await asyncio.sleep(0)
        try:
            return await action()
        finally:
            await asyncio.sleep(0.01)
            stop.set()
            await task
            gap = max(gaps)
            print(f"ticker {self.id()}: {gap:.4f}s")
            self.assertLess(gap, 0.1, "slow backend blocked the same-loop ticker")

    async def test_port_discovery(self) -> None:
        for slow in (False, True):

            def read(*_args: object, slow: bool = slow) -> int:
                if slow:
                    time.sleep(0.3)
                return 8123

            with self.subTest(slow=slow), patch.object(bridge, "read_port_file", read):
                self.assertEqual(await self.responsive(lambda: bridge._discover_port("stub")), 8123)
        with patch.object(bridge, "read_port_file", side_effect=OSError("port read failed")):
            with self.assertRaisesRegex(OSError, "port read failed"):
                await self.responsive(lambda: bridge._discover_port("stub"))

    def forwarder(self) -> Forwarder:
        return Forwarder(
            base_url="http://stub.invalid",
            solet_name="stub",
            agent_id="codex",
            agent_instance_id="agi-test",
            session_label="test",
            parent_pid=1,
            provides_inference=False,
        )

    async def test_tool_serialization(self) -> None:
        for mode in ("fast", "slow", "failure"):
            fw: Any = SimpleNamespace(peer_list=AsyncMock(return_value={"peers": ["stub"]}))
            server = bridge._build_server(fw)
            handler = server.request_handlers[CallToolRequest]
            request = CallToolRequest(method="tools/call", params=CallToolRequestParams(name="peer_list", arguments={}))
            original = bridge._json_dumps

            def encode(value: Any, mode: str = mode, original: Callable[..., str] = original) -> str:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise ValueError("encode failed")
                return original(value)

            with self.subTest(mode=mode), patch.object(bridge, "_json_dumps", encode):
                result: Any = await self.responsive(lambda handler=handler, request=request: handler(request))
                self.assertEqual(result.root.isError, mode == "failure")
                self.assertEqual(result.root.content[0].text, "encode failed" if mode == "failure" else '{"peers": ["stub"]}')

    async def test_composer_capture_precedes_clear(self) -> None:
        for mode in ("fast", "slow", "failure"):
            session = SimpleNamespace(async_get_screen_contents=AsyncMock(return_value=SimpleNamespace(number_of_lines=0)))
            original = rotation.json.dumps

            def encode(value: Any, mode: str = mode, original: Callable[..., str] = original) -> str:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise ValueError("capture failed")
                return original(value)

            with self.subTest(mode=mode), patch.object(rotation, "composer_content_rows", return_value=["draft"]), patch.object(rotation, "_send_slash_command_leg", AsyncMock()) as clear, patch.object(rotation.json, "dumps", encode), contextlib.redirect_stdout(io.StringIO()) as output:

                async def action(session: Any = session) -> list[str]:
                    return await rotation._send_clear_leg(session, cleared_signature="❯", send_gap_seconds=0)

                if mode == "failure":
                    with self.assertRaisesRegex(ValueError, "capture failed"):
                        await self.responsive(action)
                    clear.assert_not_awaited()
                else:
                    self.assertEqual(await self.responsive(action), ["draft"])
                    self.assertIn('{"composer_capture": ["draft"]}', output.getvalue())
                    clear.assert_awaited_once()

    async def test_response_decoding(self) -> None:
        for method in ("_get", "_post", "_fetch_events"):
            for mode in ("fast", "slow", "failure"):
                fw = self.forwarder()
                response = httpx.Response(404 if mode == "failure" else 200, json={"code": "stub", "result": [1, 2]})
                original = response.json

                def decode(mode: str = mode, original: Callable[..., Any] = original) -> Any:
                    if mode != "fast":
                        time.sleep(0.3)
                    return original()

                async def call(method: str = method, fw: Forwarder = fw) -> dict[str, Any]:
                    if method == "_get":
                        return await fw._get("/stub")
                    if method == "_post":
                        return await fw._post("/stub", {"key": "value"})
                    return await fw._fetch_events("agc-stub", 7)

                try:
                    with self.subTest(method=method, mode=mode), patch.object(response, "json", decode), patch.object(fw._client, "get", AsyncMock(return_value=response)), patch.object(fw._client, "post", AsyncMock(return_value=response)):
                        if mode == "failure":
                            with self.assertRaises(BridgeHTTPError) as raised:
                                await self.responsive(call)
                            self.assertEqual(raised.exception.response_code, "stub")
                        else:
                            self.assertEqual((await self.responsive(call))["result"], [1, 2])
                finally:
                    await fw._client.aclose()

    async def test_download(self) -> None:
        for mode in ("fast", "slow", "failure"):
            fw = self.forwarder()
            fw._bridge_id = "agc-stub"
            response = httpx.Response(200, content=b"blob", headers={"content-disposition": 'attachment; filename="sample.txt"'})
            original = Path.write_bytes

            def write(target: Path, body: bytes, mode: str = mode, original: Callable[..., int] = original) -> int:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise OSError("disk failed")
                return original(target, body)

            try:
                with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, patch.object(Path, "write_bytes", write), patch.object(fw._client, "get", AsyncMock(return_value=response)):
                    target = str(Path(directory) / "blob")

                    async def action(fw: Forwarder = fw, target: str = target) -> dict[str, Any]:
                        return await fw.download(blob_id="bmd-stub", output_path=target)

                    if mode == "failure":
                        with self.assertRaisesRegex(OSError, "disk failed"):
                            await self.responsive(action)
                    else:
                        result = await self.responsive(action)
                        self.assertEqual(result, {"status": "downloaded", "filename": "sample.txt", "size": 4, "path": target})
                        self.assertEqual(Path(target).read_bytes(), b"blob")
            finally:
                await fw._client.aclose()


if __name__ == "__main__":
    unittest.main()
