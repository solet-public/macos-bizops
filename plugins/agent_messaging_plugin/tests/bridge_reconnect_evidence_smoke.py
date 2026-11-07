"""Fixture-only attributable lifecycle evidence, retention and transport isolation."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import io
import json
import multiprocessing
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import AsyncMock, patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.services.store import open_store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from agent_messaging_plugin.bridge_diagnostics import MAX_FILE_BYTES, BridgeDiagnostics  # noqa: E402
from agent_messaging_plugin.bridge_lifecycle import BridgeLifecycleSweeper  # noqa: E402
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import register_routes  # noqa: E402
from agent_messaging_plugin.mcp_bridge import __main__ as entry  # noqa: E402
from agent_messaging_plugin.mcp_bridge.forwarder import (  # noqa: E402
    POLL_RETRY_DELAY_S,
    BridgeHTTPError,
    Forwarder,
)
from agent_messaging_plugin.mcp_streamable.auth import BearerClaim  # noqa: E402
from agent_messaging_plugin.mcp_streamable.session import StreamableSessionManager  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.schema import PEER_BINDING_NAMESPACE, get_peer_binding_schema  # noqa: E402

_SENTINEL = "SECRET_TOKEN_BODY_SENTINEL\nBearer private?key=secret"


# Read all fixed rotations, so privacy and correlation assertions cover retained
# history as well as the newest file. No live directory is read by this fixture.
def _rows(root: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for path in sorted(root.glob("evidence.jsonl*")) for line in path.read_text().splitlines()]


def _matching(root: Path, **fields: Any) -> list[dict[str, Any]]:
    return [row for row in _rows(root) if all(row[key] == value for key, value in fields.items())]


def _expect(row: dict[str, Any], **fields: Any) -> None:
    assert {key: row[key] for key in fields} == fields


def _assert_private(root: Path) -> None:
    assert "SECRET_TOKEN_BODY_SENTINEL" not in "".join(path.read_text() for path in root.glob("evidence.jsonl*")), "raw exception retained"


# Constructor injection keeps manager fixtures compatible and makes lifecycle
# allocation real while filesystem custody remains entirely fixture-owned.
def _manager(sink: BridgeDiagnostics) -> BridgeSessionManager:
    return BridgeSessionManager(session_id_factory=lambda _name: "fixture-session", idle_timeout_s=60, max_pending_events=20, long_poll_timeout_s=1, diagnostics=sink)


def _registry() -> PeerRegistry:
    return PeerRegistry(bindings_store=open_store(get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory"))


# The HTTP application owns production lifecycle handlers. Its registry uses
# the shipped in-memory Store, so no database or peer session is contacted.
class World:
    """Real HTTP routes and manager with fixture-owned state and file sink."""

    def __init__(self, root: Path) -> None:
        self.sink = BridgeDiagnostics("example", directory=root)
        self.manager = _manager(self.sink)
        self.registry = _registry()
        self.calls: list[str] = []
        app = FastAPI()
        register_routes(
            app,
            bridge_manager=self.manager,
            peer_registry=self.registry,
            platform_surface=cast("Any", object()),
            agent_messaging_service=object(),
            config={"long_poll_timeout_seconds": 1},
            inference_provider_clear=lambda _bid: (self.calls.append("clear"), 1)[1],
            autonomic_on_close=lambda _bid: (self.calls.append("autonomic"), "not_holder")[1],
        )
        self.client = TestClient(app)
        unregister = self.registry.unregister

        def tracked_unregister(bridge_id: str) -> int:
            self.calls.append("unregister")
            return unregister(bridge_id)

        self.registry.unregister = tracked_unregister
        self.forwarder = Forwarder("http://fixture", "example", agent_id="codex", agent_instance_id="agi-evidence", session_label="", parent_pid=123, provides_inference=False, diagnostics=self.sink)
        self.forwarder._post = self.post  # noqa: SLF001

    async def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(path, json=body)
        assert response.status_code == 200, response.text
        return cast("dict[str, Any]", response.json())

    async def open(self) -> str:
        await self.forwarder._open_with_retry()  # noqa: SLF001
        assert await self.forwarder._register_identity() is not None  # noqa: SLF001
        assert self.forwarder.bridge_id is not None
        return self.forwarder.bridge_id


# This test began as the exact unmodified-production red proof. It now joins
# failed poll, open, accepted registration and terminal evidence on real IDs.
async def test_stdio_drop_evidence() -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        world = World(root)
        bridge_id = await world.open()
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            result = await world.forwarder._handle_poll_failure(  # noqa: SLF001
                BridgeHTTPError(_SENTINEL, status_code=503), 0, request_bridge_id=bridge_id, request_generation=1, request_lifecycle_id=world.manager.server_lifecycle_id
            )
            assert result == 1 and sleep.call_args.args == (POLL_RETRY_DELAY_S,)
        await world.forwarder.close(client_reported_reason="stdio_eof")
        close_rows = _matching(root, event="close", source="server")
        assert len(close_rows) == 1, "missing server close diagnostic"
        _assert_close(root, close_rows[0], bridge_id, world.manager.server_lifecycle_id)
        _assert_failed_poll(root, bridge_id, world.manager.server_lifecycle_id)
        assert world.calls == ["clear", "autonomic", "unregister"]
        _assert_private(root)
        assert not world.manager.list_active()


# A server-observed explicit request and a client-reported EOF are separate
# facts; neither is allowed to substitute for the other in the retained row.
def _assert_close(root: Path, close: dict[str, Any], bridge_id: str, lifecycle: str) -> None:
    _expect(close, reason="explicit_close", client_reported_reason="stdio_eof")
    assert close["agent_instance_id"] == "agi-evidence", "missing accepted close identity"
    _expect(close, bridge_id=bridge_id, server_lifecycle_id=lifecycle)
    _assert_bound(root, close, bridge_id)


# Opening precedes registration. Earlier evidence must stay identity-unknown
# while the later accepted binding carries the same lifecycle and bridge key.
def _assert_bound(root: Path, close: dict[str, Any], bridge_id: str) -> None:
    bound = _matching(root, event="identity_bound", source="server")
    assert len(bound) == 1 and bound[0]["agent_instance_id"] == "agi-evidence", "missing identity_bound"
    opened = _matching(root, event="open", source="server")[0]
    assert opened["agent_instance_id"] is None
    for row in [opened, *bound]:
        _expect(row, server_lifecycle_id=close["server_lifecycle_id"], bridge_id=bridge_id)


# Exception strings deliberately contain a secret sentinel. Retained status
# and finite category prove useful failure evidence without retaining prose.
def _assert_failed_poll(root: Path, bridge_id: str, lifecycle: str) -> None:
    failed = _matching(root, event="failed_poll")
    assert len(failed) == 1 and failed[0]["count"] == 1, "missing correlated failed poll"
    _expect(failed[0], category="http", status=503, request_bridge_id=bridge_id, request_generation=1, request_lifecycle_id=lifecycle)


# The real entry point classifies normal return versus an escaping stdio
# exception. Fake streams and HTTP isolate host control and network activity.
async def _stdio_entry_case(raises: bool, reason: str) -> None:
    """Exercise the actual entry-point EOF/exception final-close classification."""
    with TemporaryDirectory() as scratch:
        world = World(Path(scratch))
        opened = asyncio.Event()

        async def opener(_forwarder: Forwarder) -> None:
            await world.open()
            opened.set()

        class Server:
            def get_capabilities(self, **_kwargs: Any) -> dict[str, Any]:
                return {}

            async def run(self, *_args: Any) -> None:
                await opened.wait()
                if raises:
                    raise RuntimeError(_SENTINEL)

        @contextlib.asynccontextmanager
        async def stdio() -> Any:
            yield (object(), object())

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict("os.environ", {"SOLET_NAME": "example"}))
            for name, value in {
                "enforce_no_legacy_agent_env": lambda: None,
                "_enforce_session_id_for_managed_registration": lambda **_kw: None,
                "_discover_port": AsyncMock(return_value=1),
                "_build_server": lambda _f: Server(),
                "Forwarder": lambda **_kw: world.forwarder,
                "stdio_server": stdio,
                "BridgeDiagnostics": lambda _name: world.sink,
                "_open_bridge_safely": opener,
            }.items():
                stack.enter_context(patch.object(entry, name, value))
            if raises:
                try:
                    await entry._run()  # noqa: SLF001
                except RuntimeError as exc:
                    assert str(exc) == _SENTINEL
                else:
                    raise AssertionError("stdio exception swallowed")
            else:
                await entry._run()  # noqa: SLF001
        terminal = _matching(Path(scratch), source="server", event="close")
        assert len(terminal) == 1 and terminal[0]["client_reported_reason"] == reason


async def test_stdio_entry() -> None:
    await _stdio_entry_case(False, "stdio_eof")
    await _stdio_entry_case(True, "stdio_error")


# An older server omits additive metadata. It must remain usable, and the
# resulting unknown lifecycle is explicit rather than an invented identity.
# The refusal fixture reaches the existing pre-registration cache assignment
# and proves that cache is not evidence of a successful identity binding.
async def test_unknown_and_refused_identity() -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        world = World(root)

        async def old_server(path: str, body: dict[str, Any]) -> dict[str, Any]:
            payload = await world.post(path, body)
            payload.pop("server_lifecycle_id", None)
            return payload

        world.forwarder._post = old_server  # noqa: SLF001
        await world.open()
        assert _matching(root, event="lifecycle_unknown", server_lifecycle_id=None)
        bridge = world.manager.open("example")
        from fastapi.responses import JSONResponse

        with patch("agent_messaging_plugin.http_routes._session_id_conflict", return_value=JSONResponse({"error": "fixture-refusal"}, status_code=409)):
            response = world.client.post(f"/api/v1/bridge/{bridge.bridge_id}/peer/register", json={"agent_id": "codex", "agent_instance_id": "agi-refused"})
        assert response.status_code == 409
        world.manager.close(bridge.bridge_id)
        terminal = _matching(root, event="close", bridge_id=bridge.bridge_id)
        assert terminal[0]["agent_instance_id"] is None, "refused identity retained"
        assert not _matching(root, event="identity_bound", bridge_id=bridge.bridge_id)
        await world.forwarder.close()


# Abrupt disappearance makes no close request. Advancing only fixture time
# lets the existing sweeper observe expiry without fabricating an EOF cause.
def test_server_lifecycle() -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        sink = BridgeDiagnostics("example", directory=root)
        manager = _manager(sink)
        bridge = manager.open("example")
        manager.record_identity_bound(bridge.bridge_id, "agi-evidence")
        bridge.last_seen_at = (datetime.now(UTC) - timedelta(minutes=2)).isoformat()
        cleaned: list[str] = []
        sweeper = BridgeLifecycleSweeper(bridge_manager=manager, cleanup=lambda bid: (cleaned.append(bid), 1)[1], interval_seconds=60)
        assert sweeper.sweep_once() == [bridge.bridge_id]
        close = _matching(root, event="close")[0]
        assert close["reason"] == "idle_timeout", "wrong idle close reason"
        assert close["client_reported_reason"] is None
        assert cleaned == [bridge.bridge_id]
        _assert_concurrent_close(root, manager)
        _assert_streamable(root, sink, manager.server_lifecycle_id)


# Repeat callers race the registry pop. The successful first caller alone
# owns terminal evidence, regardless of which thread wins the mutex.
def _assert_concurrent_close(root: Path, manager: BridgeSessionManager) -> None:
    bridge = manager.open("example")
    manager.record_identity_bound(bridge.bridge_id, "agi-evidence")
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(manager.close, [bridge.bridge_id] * 16)) == 1
    terminal = _matching(root, event="close", bridge_id=bridge.bridge_id)
    assert len(terminal) == 1, "duplicate terminal evidence"
    assert not manager.close(bridge.bridge_id)


# A validated bearer claim is an independent identity-establishment path.
# Only its stable identity is retained, never a bearer credential or client body.
def _assert_streamable(root: Path, sink: BridgeDiagnostics, previous_lifecycle: str) -> None:
    second = _manager(sink)
    assert second.server_lifecycle_id != previous_lifecycle
    claim = BearerClaim("claude_phone", "agi-evidence", datetime.now(UTC), client_id="fixture")
    sessions = StreamableSessionManager(bridge_manager=second, peer_registry=_registry())
    first = sessions.allocate(claim)
    assert sessions.close(first.mcp_session_id)
    assert not sessions.close(first.mcp_session_id)
    remaining = sessions.allocate(claim)
    assert sessions.close_all() == 1 and sessions.close_all() == 0
    _assert_streamable_rows(root, second.server_lifecycle_id, first.bridge_id, remaining.bridge_id)


# Explicit streamable close and process shutdown are distinct finite reasons.
# Both retain the injected solet name even though open_bridge passes an empty name.
def _assert_streamable_rows(root: Path, lifecycle: str, first_id: str, remaining_id: str) -> None:
    for bid, reason in [(first_id, "streamable_close"), (remaining_id, "streamable_shutdown")]:
        terminal = _matching(root, event="close", bridge_id=bid)
        assert len(terminal) == 1 and terminal[0]["reason"] == reason
        _expect(terminal[0], agent_instance_id="agi-evidence", solet_name="example", server_lifecycle_id=lifecycle)
        assert _matching(root, event="identity_bound", bridge_id=bid, agent_instance_id="agi-evidence")


# Retry decisions and delay counts remain the baseline three-failure policy.
# The final failed request deliberately belongs to the replaced generation.
async def test_reconnect_and_sink_failure() -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        world = World(root)
        old = await world.open()
        old_lifecycle = world.manager.server_lifecycle_id
        with patch("asyncio.sleep", new_callable=AsyncMock) as sleep:
            for count in range(3):
                result = await world.forwarder._handle_poll_failure(BridgeHTTPError(_SENTINEL, status_code=503), count, request_bridge_id=old, request_generation=1, request_lifecycle_id=old_lifecycle)  # noqa: SLF001
            assert result == 0 and sleep.call_count == 3
            assert all(call.args == (POLL_RETRY_DELAY_S,) for call in sleep.call_args_list)
            await world.forwarder._handle_poll_failure(BridgeHTTPError(_SENTINEL), 0, request_bridge_id=old, request_generation=1, request_lifecycle_id=old_lifecycle)  # noqa: SLF001
        failed = _matching(root, event="failed_poll")[-1]
        assert failed["request_bridge_id"] == old and failed["bridge_id"] != old
        _expect(failed, request_generation=1, generation=2)
        attempts = 0

        async def tool() -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise BridgeHTTPError(_SENTINEL, status_code=404, path=f"/api/v1/bridge/{world.forwarder.bridge_id}/process/call", response_code="bridge_not_found")
            return {"ok": True}

        assert await world.forwarder._call_with_reconnect("fixture", tool) == {"ok": True}  # noqa: SLF001
        assert {"reconnect_poll", "reconnect_tool", "reconnected"} <= {row["event"] for row in _rows(root)}
        await _assert_sink_failure(root, world, attempts)


# A regular file in place of a directory forces a genuine filesystem error.
# Reconnect and cleanup still succeed; only diagnostics are permitted to fail.
async def _assert_sink_failure(root: Path, world: World, attempts: int) -> None:
    # A filesystem failure only drops evidence; HTTP and cleanup still run.
    blocked = root / "blocked"
    blocked.write_text("fixture")
    world.forwarder._diagnostics = BridgeDiagnostics("example", directory=blocked)  # noqa: SLF001
    world.manager._diagnostics = world.forwarder._diagnostics  # noqa: SLF001
    stderr = io.StringIO()
    with patch.object(sys, "__stderr__", stderr):
        try:
            await world.forwarder._reconnect()  # noqa: SLF001
            await world.forwarder.close()
        except Exception as exc:
            raise AssertionError("sink failure changed transport") from exc
    assert "diagnostic evidence unavailable; record dropped" in stderr.getvalue(), "missing safe sink advisory"
    assert attempts == 2 and world.calls[-3:] == ["clear", "autonomic", "unregister"], "sink failure changed transport"
    _assert_private(root)


# Spawned writers construct their own recorder, sharing only a scratch path.
# This exercises independent file descriptors rather than a thread-only lock.
def _writer(directory: str) -> None:
    sink = BridgeDiagnostics("example", directory=Path(directory))
    for _ in range(2500):
        sink.emit("start", source="client", agent_instance_id="agi-evidence")


# The boundary prefill uses valid JSONL so every later read also checks that
# rotation and interprocess append preserve complete parseable record lines.
def test_retention() -> None:
    with TemporaryDirectory() as scratch:
        root = Path(scratch)
        sink = BridgeDiagnostics("example", directory=root)
        # Fill a valid current file exactly to the boundary using JSON rows.
        line = b"{}\n"
        (root / "evidence.jsonl").write_bytes(line * (MAX_FILE_BYTES // len(line)))
        sink.emit("start", source="client")
        assert (root / "evidence.jsonl.1").exists(), "rotation boundary missing"
        # Independent processes exercise the shared sidecar, inode reopen and cap.
        ctx = multiprocessing.get_context("spawn")
        processes = [ctx.Process(target=_writer, args=(str(root),)) for _ in range(4)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(30)
            assert process.exitcode == 0
        files = _assert_files(root)
        with (root / "evidence.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = {path: path.read_bytes() for path in files}
            stderr = io.StringIO()
            with patch.object(sys, "__stderr__", stderr):
                sink.emit("start", source="client", agent_instance_id=_SENTINEL)
            assert "record dropped" in stderr.getvalue()
            assert all(path.read_bytes() == value for path, value in before.items())
        sink.emit("start", source="client", agent_instance_id=_SENTINEL, client_reported_reason=_SENTINEL)
        assert "SECRET_TOKEN_BODY_SENTINEL" not in "".join(path.read_text() for path in files), "unsafe field retained"


# Fixed names and per-file bounds jointly imply the aggregate retention cap.
# Parsing every line also detects interleaved writes or stale rotated inodes.
def _assert_files(root: Path) -> list[Path]:
    files = list(root.glob("evidence.jsonl*"))
    assert len(files) <= 4 and sum(path.stat().st_size for path in files) <= 4 * MAX_FILE_BYTES, "aggregate byte cap exceeded"
    assert all(path.stat().st_size <= MAX_FILE_BYTES for path in files)
    assert {path.name for path in root.iterdir()} <= {"evidence.lock", "evidence.jsonl", "evidence.jsonl.1", "evidence.jsonl.2", "evidence.jsonl.3"}
    for path in files:
        for row in path.read_bytes().splitlines():
            assert len(row) + 1 <= 2048
            json.loads(row)
    return files


async def main() -> None:
    await test_stdio_drop_evidence()
    await test_stdio_entry()
    await test_unknown_and_refused_identity()
    test_server_lifecycle()
    await test_reconnect_and_sink_failure()
    test_retention()
    print("PASS bridge reconnect evidence: six fixture groups")


if __name__ == "__main__":
    asyncio.run(main())
