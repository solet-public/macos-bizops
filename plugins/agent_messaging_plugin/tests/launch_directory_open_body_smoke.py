"""Launch-directory wire capture and in-memory resolver correlation."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

import httpx
from fastapi import FastAPI

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta/src"))
sys.path.insert(0, str(ROOT / "plugins/agent_messaging_plugin/src"))
sys.path.insert(0, str(ROOT / "plugins/default_address_book_plugin/src"))

from ananta.file_locations.containment import Located  # noqa: E402
from default_address_book_plugin.plugin import DefaultAddressBookPlugin  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import _register_bridge_lifecycle_routes  # noqa: E402
from agent_messaging_plugin.local_cli.client import BridgeClient  # noqa: E402
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder  # noqa: E402
from agent_messaging_plugin.platform_surface import PlatformSurface  # noqa: E402


class LaunchDirectoryOpenBodySmoke(unittest.TestCase):
    def test_cli_explicit_and_cwd_fallback(self) -> None:
        bodies: list[dict[str, Any]] = []

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/open"):
                bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"bridge_id": "agc-test"})

        for explicit in ("/user/project folder", None):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("SOLET_LAUNCH_DIRECTORY", None)
                if explicit is not None:
                    os.environ["SOLET_LAUNCH_DIRECTORY"] = explicit
                with BridgeClient("http://test", transport=httpx.MockTransport(handle), caller_agent_session_id="ases-test") as client:
                    self.assertEqual(client._bridge_id, "agc-test")
                self.assertEqual(bodies[-1].get("launch_directory"), {"path": explicit or os.getcwd(), "source": "launcher" if explicit else "cwd_fallback"})
                self.assertEqual(bodies[-1]["caller_agent_session_id"], "ases-test")

    def test_mcp_explicit_and_cwd_fallback(self) -> None:
        async def exercise() -> None:
            bodies: list[dict[str, Any]] = []
            for explicit in ("/user/project folder", None):
                bodies.clear()

                def handle(request: httpx.Request) -> httpx.Response:
                    bodies.append(json.loads(request.content))
                    return httpx.Response(200, json={"bridge_id": "agc-test"})

                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("SOLET_LAUNCH_DIRECTORY", None)
                    if explicit is not None:
                        os.environ["SOLET_LAUNCH_DIRECTORY"] = explicit
                    forwarder = Forwarder("http://test", "smoke", agent_id="codex", agent_instance_id="agi-test", agent_session_id="ases-test", session_label="test", parent_pid=1, provides_inference=False)
                    await forwarder._client.aclose()
                    forwarder._client = httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handle))
                    try:
                        await forwarder._open_with_retry()
                        self.assertEqual(bodies[-1].get("launch_directory"), {"path": explicit or os.getcwd(), "source": "launcher" if explicit else "cwd_fallback"})
                        self.assertEqual(bodies[-1]["caller_agent_session_id"], "ases-test")
                    finally:
                        await forwarder.close()

        asyncio.run(exercise())

    def test_server_capture_action_readback_and_close_reopen(self) -> None:
        async def exercise(root: Path) -> None:
            sequence = iter(("session-1", "session-2", "session-3"))
            manager = BridgeSessionManager(session_id_factory=lambda _: next(sequence), idle_timeout_s=60, max_pending_events=10, long_poll_timeout_s=1)
            surface = PlatformSurface(action_factory=None, flow_manager=None, compilation_context_builder=None, bridge_manager=manager)
            app = FastAPI()
            _register_bridge_lifecycle_routes(app, bridge_manager=manager, peer_registry=cast(Any, None), agent_messaging_service=None, long_poll_timeout_s=1)
            project, clone, home = (root / name for name in ("project folder", "Solets/test", "Documents/test"))
            for folder in (project, clone, home):
                folder.mkdir(parents=True)
            plugin = DefaultAddressBookPlugin()
            messaging = SimpleNamespace(_platform_surface=surface)
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(APP_HOME=str(clone / "profile"), plugin_manager=SimpleNamespace(get_plugin=lambda _: messaging)))
            async with httpx.AsyncClient(base_url="http://test", transport=httpx.ASGITransport(app=app)) as client:
                for capture, rung in (({"path": str(project), "source": "launcher"}, "launch_directory"), ({"path": str(clone), "source": "cwd_fallback"}, "solet_home"), (None, "solet_home")):
                    response = await client.post("/api/v1/bridge/open", json={"launch_directory": capture} if capture is not None else {})
                    self.assertEqual(response.status_code, 200)
                    opened = response.json()
                    bridge = manager.get(opened["bridge_id"])
                    assert bridge is not None
                    self.assertEqual(surface.launch_directory_for_session(bridge.session_id), capture)
                    # Trigger metadata is process-agnostic; use the bound service's registered read verb.
                    trigger = surface._build_process_call_trigger_data(bridge=bridge, process_key="service_interface::address_book_service::resolve", reason="smoke", inference_vertex_role="", operator_equivalent=False)
                    self.assertNotIn("launch_directory", trigger, "capture must never be copied into persisted flow metadata")
                    with patch("default_address_book_plugin.plugin.path_addresses", return_value=Located((), ())), patch("default_address_book_plugin.plugin.default_solet_home", return_value=Located(home, ())):
                        resolved = cast(dict[str, Any], plugin.resolve_location_action({"launch_directory": str(project)}, {"session_id": bridge.session_id})["data"])
                        self.assertEqual(resolved["rung"], rung)
                        self.assertEqual(resolved["directory"], str(project if rung == "launch_directory" else home))
                        self.assertTrue(resolved["writable"])
                        if rung == "solet_home":
                            self.assertTrue(resolved["advisories"])
                        unrelated = cast(dict[str, Any], plugin.resolve_location_action({}, {"session_id": "other-session"})["data"])
                        self.assertEqual(unrelated["rung"], "solet_home")
                    snapshot = surface.launch_directory_for_session(bridge.session_id)
                    if snapshot is not None:
                        snapshot["path"] = "/changed"
                        self.assertEqual(surface.launch_directory_for_session(bridge.session_id), capture)
                    self.assertTrue(manager.close(bridge.bridge_id))
                    self.assertIsNone(surface.launch_directory_for_session(bridge.session_id))

        with tempfile.TemporaryDirectory(prefix="bridge-location-") as temporary:
            asyncio.run(exercise(Path(temporary).resolve()))


if __name__ == "__main__":
    unittest.main()
