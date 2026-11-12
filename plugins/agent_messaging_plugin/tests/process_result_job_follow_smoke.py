"""Receipt-following behavior across the platform and bridge transports."""

import unittest
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from fastapi import FastAPI

from agent_messaging_plugin import http_routes
from agent_messaging_plugin.bridge_sessions import BridgeSessionManager
from agent_messaging_plugin.mcp_bridge.__main__ import TOOLS
from agent_messaging_plugin.mcp_bridge.forwarder import Forwarder
from agent_messaging_plugin.platform_surface import PlatformSurface

RECEIPT = {"data": {"job_id": "job-1", "status": "queued"}}
NEXT = {"tool": "process_result", "arguments": {"action_id": "ae-1"}}
JOB_READ = {"process_key": "service_interface::job_service::get_job", "arguments": {"job_id": "job-1"}}


class FollowSmoke(unittest.IsolatedAsyncioTestCase):
    def read(self, status: str = "completed", *, receipt: dict[str, Any] = RECEIPT, follow: bool | None = None) -> dict[str, Any]:
        surface = object.__new__(PlatformSurface)
        surface._state_service = object()
        if receipt is RECEIPT:
            reader = MagicMock()
            reader.get_job.return_value = {"action_status": "error"} if status == "missing" else {"action_status": "completed", "data": {"job": {"status": status}}}
            reader.get_job_payload.side_effect = lambda _id, kind: {"action_status": "completed", "data": {"payload": {"message": "bad"} if kind == "error" else {"answer": 42}}}
            surface._async_job_manager = reader
        with patch.object(PlatformSurface, "_read_action_event", return_value={"status": "completed"}), patch.object(PlatformSurface, "_read_latest_action_result", return_value={"result_data": receipt}):
            if follow is None:
                return surface.process_result("ae-1")
            return surface.process_result("ae-1", follow_job=follow)

    def test_t1_completed(self) -> None:
        result = self.read()
        self.assertEqual(result["result"], {"answer": 42})
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["job"], {"job_id": "job-1", "status": "completed"})

    def test_t2_error(self) -> None:
        result = self.read("error")
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"], {"message": "bad"})
        self.assertEqual(result["error_message"], "bad")

    def test_t3_processing(self) -> None:
        result = self.read("processing")
        self.assertEqual(result["status"], "processing")
        self.assertEqual(result["result"], RECEIPT)
        self.assertEqual(result["next_read"], NEXT)

    def test_t4_missing(self) -> None:
        result = self.read("missing")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["result"], RECEIPT)
        self.assertEqual(result["job"], {"job_id": "job-1", "status": "unknown"})
        self.assertEqual(result["job_advisory"], "job_row_unreadable")
        self.assertEqual(result["next_read"], JOB_READ)

    def test_t5_nonreceipt(self) -> None:
        self.assertEqual(self.read(receipt={"data": {"answer": 42}})["result"], {"data": {"answer": 42}})

    def test_t6_no_follow(self) -> None:
        self.assertEqual(self.read(follow=False)["result"], RECEIPT)

    async def test_t7_http(self) -> None:
        app = FastAPI()
        surface = MagicMock()
        surface.process_result.return_value = {"status": "completed"}
        manager = BridgeSessionManager(session_id_factory=lambda _name: "session", idle_timeout_s=3600, max_pending_events=50, long_poll_timeout_s=1)
        http_routes.register_routes(app, bridge_manager=manager, peer_registry=MagicMock(), platform_surface=surface, agent_messaging_service=MagicMock(), config={"long_poll_timeout_seconds": 1})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await client.get("/api/v1/bridge/id/process/result/ae-1")
            surface.process_result.assert_called_with(action_id="ae-1", follow_job=False)
            await client.get("/api/v1/bridge/id/process/result/ae-1?follow_job=true")
            surface.process_result.assert_called_with(action_id="ae-1", follow_job=True)

    async def test_t8_forwarder(self) -> None:
        forwarder = object.__new__(Forwarder)
        forwarder._bridge_path = lambda suffix: suffix
        forwarder._get = AsyncMock(return_value={})

        async def call(_name: str, function: Any) -> dict[str, Any]:
            return await function()

        forwarder._call_with_reconnect = call
        await forwarder.process_result(action_id="ae-1")
        forwarder._get.assert_awaited_once_with("/process/result/ae-1?follow_job=true")

    def test_t9_descriptions(self) -> None:
        descriptions = {tool.name: tool.description or "" for tool in TOOLS}
        self.assertNotIn("do not poll", descriptions["process_call"])
        self.assertIn("process_result", descriptions["process_call"])
        self.assertNotIn("for diagnostics", descriptions["process_result"])
        self.assertIn("next_read", descriptions["process_result"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
