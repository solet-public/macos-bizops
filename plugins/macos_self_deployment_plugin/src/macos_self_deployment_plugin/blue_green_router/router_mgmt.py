"""Newline-delimited JSON RPC server over a Unix-domain socket.

The mgmt plane is the control-plane surface the deployment plugin
and the spawned homunculus children talk to. Localhost-only by virtue of
being a Unix socket; the authn boundary is filesystem permissions
(0600, owner-only).

Protocol per §2.4 of `2026-06-01_local_blue_green_L3_implementation_plan.md`:
each line is a JSON object `{"verb": "<name>", "args": {...}}`; the
server replies with one JSON object per line. Unknown verbs return
`{"error": "unknown_verb"}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

logger = logging.getLogger(__name__)


DispatchFn = Callable[[str, dict[str, object]], Awaitable[dict[str, object]]]


class MgmtServer:
    """Asyncio Unix-socket server for the router's mgmt plane."""

    def __init__(
        self,
        socket_path: Path,
        dispatch: DispatchFn,
    ) -> None:
        self._socket_path = socket_path
        self._dispatch = dispatch
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        if self._socket_path.exists():
            self._socket_path.unlink()
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._server = await asyncio.start_unix_server(
            self._handle_client, path=str(self._socket_path)
        )
        self._socket_path.chmod(0o600)
        logger.info("mgmt: listening on %s", self._socket_path)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if self._socket_path.exists():
            self._socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername") or "<unix>"
        logger.debug("mgmt: client connected %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                response = await self._process_line(line)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            logger.debug("mgmt: client disconnected %s", peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _process_line(self, line: bytes) -> dict[str, object]:
        try:
            payload = json.loads(line.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("mgmt: bad request: %s", exc)
            return {"error": "bad_json", "detail": str(exc)}
        if not isinstance(payload, dict):
            return {"error": "bad_request", "detail": "top-level must be object"}
        verb = payload.get("verb")
        if not isinstance(verb, str) or not verb:
            return {"error": "bad_request", "detail": "verb missing or not string"}
        args = payload.get("args", {})
        if not isinstance(args, dict):
            return {"error": "bad_request", "detail": "args must be object"}
        try:
            return await self._dispatch(verb, args)
        except Exception as exc:
            logger.exception("mgmt: dispatch failed for verb=%s", verb)
            return {"error": "dispatch_failed", "detail": str(exc)}
