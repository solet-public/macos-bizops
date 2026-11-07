"""Router helper slow-I/O checks; never start a deployment or tunnel child."""

from __future__ import annotations

import asyncio
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

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "macos_self_deployment_plugin" / "src"))

from macos_self_deployment_plugin.blue_green_router import mcp_ingress as ingress  # noqa: E402
from macos_self_deployment_plugin.blue_green_router import router, router_mgmt, tunnel_supervisor  # noqa: E402


class RouterResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_permissions_precede_accepting_clients(self) -> None:
        for mode in ("fast", "slow", "failure"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                server = router_mgmt.MgmtServer(Path(directory) / "auth.sock", AsyncMock(return_value={}))
                entered = threading.Event()
                original = Path.chmod

                def chmod(path: Path, value: int, mode: str = mode, entered: threading.Event = entered, original: Callable[..., None] = original) -> None:
                    entered.set()
                    if mode != "fast":
                        time.sleep(0.3)
                    if mode == "failure":
                        raise PermissionError("chmod failed")
                    original(path, value)

                async def start(server: router_mgmt.MgmtServer = server, mode: str = mode, entered: threading.Event = entered) -> None:
                    task = asyncio.create_task(server.start())
                    try:
                        if mode != "fast":
                            while not entered.is_set():
                                await asyncio.sleep(0.005)
                            self.assertIsNotNone(server._server)
                            assert server._server is not None
                            self.assertFalse(server._server.is_serving())
                        await task
                        self.assertEqual(server._socket_path.stat().st_mode & 0o777, 0o600)
                    finally:
                        await server.stop()

                with patch.object(Path, "chmod", chmod):
                    if mode == "failure":
                        with self.assertRaisesRegex(PermissionError, "chmod failed"):
                            await self.responsive(start)
                    else:
                        await self.responsive(start)

    def writer(self, output: list[bytes]) -> Any:
        return SimpleNamespace(write=output.append, drain=AsyncMock(), close=lambda: None, wait_closed=AsyncMock(), get_extra_info=lambda _key: None)

    async def test_ingress_port_read(self) -> None:
        for mode in ("fast", "slow", "failure"):
            output: list[bytes] = []
            reader = asyncio.StreamReader()
            reader.feed_eof()
            upstream = asyncio.StreamReader()
            upstream.feed_data(b"proxied")
            upstream.feed_eof()

            def read(_path: Path, mode: str = mode) -> int:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise RuntimeError("port missing")
                return 8123

            with self.subTest(mode=mode), patch.object(ingress, "read_router_port", read), patch.object(ingress.asyncio, "open_connection", AsyncMock(return_value=(upstream, self.writer([])))) as connect:
                await self.responsive(lambda reader=reader, output=output: ingress.handle_connection(reader, self.writer(output), router_port_file=Path("/unused")))
                if mode == "failure":
                    self.assertTrue(b"".join(output).startswith(b"HTTP/1.1 503"))
                    connect.assert_not_awaited()
                else:
                    self.assertEqual(b"".join(output), b"proxied")
                    connect.assert_awaited_once_with("127.0.0.1", 8123)

    async def test_router_startup_publication(self) -> None:
        for mode in ("fast", "slow", "failure"):
            writes: list[int] = []

            def write(_solet: str, port: int, _runtime: Path | None, mode: str = mode, writes: list[int] = writes) -> None:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise OSError("startup publication failed")
                writes.append(port)

            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, patch.object(router, "_write_port_discovery_files", write):
                ready = asyncio.Event()

                async def run(directory: str = directory, ready: asyncio.Event = ready, mode: str = mode, writes: list[int] = writes) -> None:
                    task = asyncio.create_task(router.run_router(solet="stub", public_port=0, runtime_dir=Path(directory), ready_event=ready))
                    if mode == "failure":
                        with self.assertRaisesRegex(OSError, "startup publication failed"):
                            await task
                        self.assertFalse(ready.is_set())
                    else:
                        try:
                            await ready.wait()
                            self.assertEqual(writes, [0])
                        finally:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task

                await self.responsive(run)

    async def test_management_socket_lifecycle(self) -> None:
        for mode in ("fast", "slow", "failure"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "router.sock"
                server = router_mgmt.MgmtServer(target, AsyncMock(return_value={}))
                original = Path.stat

                def stat(path: Path, *, follow_symlinks: bool = True, mode: str = mode, original: Callable[..., Any] = original) -> Any:
                    if path.name != "router.sock":
                        return original(path, follow_symlinks=follow_symlinks)
                    if mode != "fast":
                        time.sleep(0.3)
                    if mode == "failure":
                        raise OSError("stat unavailable")
                    return original(path, follow_symlinks=follow_symlinks)

                async def lifecycle(server: router_mgmt.MgmtServer = server) -> None:
                    await server.start()
                    await server.stop()

                with patch.object(Path, "stat", stat):
                    if mode == "failure":
                        with self.assertRaisesRegex(OSError, "stat unavailable"):
                            await self.responsive(lifecycle)
                    else:
                        await self.responsive(lifecycle)
                if mode != "failure":
                    self.assertFalse(target.exists())

    async def test_management_probe(self) -> None:
        async def reply(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await reader.readline()
            writer.write(b'{"active":"blue"}\n')
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        for mode in ("fast", "slow", "failure"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "probe.sock"
                listener = await asyncio.start_unix_server(reply, str(target))
                server = router_mgmt.MgmtServer(target, AsyncMock())
                original = router_mgmt.json.loads

                def decode(value: Any, mode: str = mode, original: Callable[..., Any] = original) -> Any:
                    if mode != "fast":
                        time.sleep(0.3)
                    if mode == "failure":
                        raise router_mgmt.json.JSONDecodeError("bad probe", "", 0)
                    return original(value)

                try:
                    with patch.object(router_mgmt.json, "loads", decode):
                        self.assertEqual(await self.responsive(server._probe_status), None if mode == "failure" else {"active": "blue"})
                finally:
                    listener.close()
                    await listener.wait_closed()

    async def test_management_reply_order(self) -> None:
        for mode in ("fast", "slow", "failure"):
            output: list[bytes] = []
            reader = asyncio.StreamReader()
            reader.feed_data(b"first\nsecond\n")
            reader.feed_eof()
            server = router_mgmt.MgmtServer(Path("/unused"), AsyncMock())
            original = router_mgmt.json.dumps

            def encode(value: Any, mode: str = mode, original: Callable[..., str] = original) -> str:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise ValueError("reply encoding failed")
                return original(value)

            with self.subTest(mode=mode), patch.object(server, "_process_line", AsyncMock(side_effect=[{"value": 1}, {"value": 2}])), patch.object(router_mgmt.json, "dumps", encode):

                async def action(server: router_mgmt.MgmtServer = server, reader: asyncio.StreamReader = reader, output: list[bytes] = output) -> None:
                    await server._handle_client(reader, self.writer(output))

                if mode == "failure":
                    with self.assertRaisesRegex(ValueError, "reply encoding failed"):
                        await self.responsive(action)
                    self.assertEqual(output, [])
                else:
                    await self.responsive(action)
                    self.assertEqual(output, [b'{"value": 1}\n', b'{"value": 2}\n'])

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

    async def test_ingress_port_publication(self) -> None:
        for mode in ("fast", "slow", "failure"):
            original = ingress._write_port_file

            def write(target: Path, port: int, mode: str = mode, original: Callable[..., None] = original) -> None:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise OSError("publish failed")
                original(target, port)

            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory, patch.object(ingress, "_write_port_file", write):
                target = Path(directory) / "ingress.port"

                async def start(target: Path = target) -> int:
                    server = await ingress.start_ingress_server(solet="stub", listen_host="127.0.0.1", listen_port=0, ingress_port_file=target)
                    try:
                        self.assertIsNotNone(server.sockets)
                        return int(target.read_text())
                    finally:
                        server.close()
                        await server.wait_closed()

                if mode == "failure":
                    with self.assertRaisesRegex(OSError, "publish failed"):
                        await self.responsive(start)
                else:
                    self.assertGreater(await self.responsive(start), 0)

    async def test_tunnel_port_discovery(self) -> None:
        supervisor = tunnel_supervisor.TunnelSupervisor(solet="stub", tunnel_client_path=Path("/unused"), tunnel_id="stub", control_plane_api_key="stub", ingress_port_file=Path("/unused"), health_url_file=Path("/unused"), poll_interval_seconds=0.001)
        for mode in ("fast", "slow", "failure"):

            def read(_target: Path, mode: str = mode) -> int:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    supervisor.request_stop()
                    raise RuntimeError("port unavailable")
                return 8123

            with self.subTest(mode=mode), patch.object(tunnel_supervisor, "read_router_port", read):
                self.assertEqual(await self.responsive(supervisor._wait_for_ingress_port), 0 if mode == "failure" else 8123)

    async def test_watchdog(self) -> None:
        for failure in (False, True):
            writes: list[None] = []

            def write(*_args: object, failure: bool = failure, writes: list[None] = writes) -> None:
                time.sleep(0.3)
                writes.append(None)
                if failure:
                    raise OSError("disk failed")

            async def run(writes: list[None] = writes) -> None:
                task = asyncio.create_task(router._bridge_port_watchdog("stub", 8123, interval=0.005))
                try:
                    while not writes:
                        await asyncio.sleep(0.005)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

            with self.subTest(failure=failure), patch.object(router, "_write_port_discovery_files", write):
                await self.responsive(run)
                self.assertEqual(len(writes), 1)

    async def test_management_json_and_dispatch(self) -> None:
        for mode in ("fast", "slow", "failure"):
            dispatch = AsyncMock(return_value={"active": "blue"})
            server = router_mgmt.MgmtServer(Path("/unused"), dispatch)
            original = router_mgmt.json.loads

            def decode(value: Any, mode: str = mode, original: Callable[..., Any] = original) -> Any:
                if mode != "fast":
                    time.sleep(0.3)
                return original(value)

            line = b"invalid" if mode == "failure" else b'{"verb":"status","args":{}}'
            with self.subTest(mode=mode), patch.object(router_mgmt.json, "loads", decode):
                result = await self.responsive(lambda server=server, line=line: server._process_line(line))
                if mode == "failure":
                    self.assertEqual(result["error"], "bad_json")
                    dispatch.assert_not_awaited()
                else:
                    self.assertEqual(result, {"active": "blue"})
                    dispatch.assert_awaited_once_with("status", {})

    async def test_socket_reclaim(self) -> None:
        for mode in ("fast", "slow", "failure"):
            server = router_mgmt.MgmtServer(Path("/unused"), AsyncMock())

            def exists(_target: Path, mode: str = mode) -> bool:
                if mode != "fast":
                    time.sleep(0.3)
                if mode == "failure":
                    raise OSError("stat failed")
                return False

            with self.subTest(mode=mode), patch.object(Path, "exists", exists):
                if mode == "failure":
                    with self.assertRaisesRegex(OSError, "stat failed"):
                        await self.responsive(server._reclaim_socket_path)
                else:
                    await self.responsive(server._reclaim_socket_path)


if __name__ == "__main__":
    unittest.main()
