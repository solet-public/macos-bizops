#!/usr/bin/env python3
"""Fake-only attribution for reconstructed fleet tools/call envelopes.

Exercises native stdio dispatch and Streamable router error boundaries. This
is diagnostic coverage, not reproduction of the historical Codex -32603.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin.mcp_bridge.__main__ import _dispatch_tool  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    DispatchContext,
    JsonRpcRequest,
)
from agent_messaging_plugin.mcp_streamable.router import (  # noqa: E402
    _dispatch_and_build_response,
)
from agent_messaging_plugin.platform_surface import (  # noqa: E402
    BridgeError,
    PlatformSurface,
)
from agent_messaging_plugin.process_exposure import ProcessExportPolicy  # noqa: E402

KEY = "plugin::agent_messaging_plugin::record_fleet_liveness_run"
SECRET = "SENSITIVE-token-cookie-reason-payload"
BRIDGE = "agc-0123456789ab"


class Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.fields: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        fields = getattr(record, "mcp_failure", None)
        if isinstance(fields, dict):
            self.fields.append(fields)


class Discovery:
    def __init__(self, error: Exception | None) -> None:
        self.error = error
        self.attempts = 0

    def get_process_schema(self, process_key: str) -> dict[str, Any]:
        self.attempts += 1
        if self.error is not None:
            raise self.error
        return {"action_status": "completed", "data": {"process_key": process_key}}


class Surface(PlatformSurface):
    def __init__(self, error: Exception | None = None) -> None:
        bridge = SimpleNamespace(
            bridge_id=BRIDGE,
            closed=False,
            client_id="",
            process_export_allowlist=(),
            touch=lambda: None,
        )
        self.discovery = Discovery(error)
        self.error = error
        self.attempts = 0
        super().__init__(
            action_factory=object(),
            flow_manager=object(),
            compilation_context_builder=object(),
            bridge_manager=cast("Any", SimpleNamespace(get=lambda _: bridge)),
            discovery_service=self.discovery,
            export_policy=ProcessExportPolicy(allow_patterns=("plugin::*",)),
        )

    def _submit_process_call(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        self.attempts += 1
        if self.error is not None:
            raise self.error
        return {"status": "queued", "action_id": "act-fake"}


class Forwarder:
    """In-process stand-in; never connects to a server or appends a fleet run."""

    bridge_id: str | None = BRIDGE

    def __init__(self, surface: Surface) -> None:
        self.surface = surface

    async def process_schema(self, *, process_key: str) -> dict[str, Any]:
        return self.surface.process_schema(process_key, bridge_id=self.bridge_id)

    async def process_call(
        self,
        *,
        process_key: str,
        arguments: dict[str, Any],
        reason: object = None,
    ) -> dict[str, Any]:
        return self.surface.process_call(
            process_key,
            arguments,
            trigger_data={"bridge_id": BRIDGE, "session_id": "sess-fake", "reason": reason},
        )


def stream(surface: Surface, tool: object, arguments: object, request_id: Any = SECRET) -> tuple[int, dict[str, Any]]:
    envelope = JsonRpcRequest(
        method="tools/call",
        params={"name": tool, "arguments": arguments},
        id=request_id,
    )
    session = SimpleNamespace(
        bridge_id=BRIDGE,
        session_id="sess-fake",
        mcp_session_id="mcp-fake",
    )
    context = DispatchContext(
        bridge_manager=cast("Any", None),
        peer_registry=cast("Any", None),
        platform_surface=surface,
        agent_messaging_service=None,
    )
    response = _dispatch_and_build_response(envelope, cast("Any", session), context)
    return response.status_code, json.loads(bytes(cast("Any", response).body))


def diagnostic(capture: Capture, transport: str, stage: str, tool: str = "process_schema") -> dict[str, Any]:
    matches = [row for row in capture.fields if row.get("tool") == tool and row.get("process_key") == KEY and row.get("transport") == transport and row.get("stage") == stage]
    assert matches, "missing fleet schema attribution"
    return matches[-1]


def check_stdio_schema_attr(capture: Capture) -> None:
    error = RuntimeError(SECRET)
    surface = Surface(error)
    try:
        asyncio.run(_dispatch_tool(cast("Any", Forwarder(surface)), "process_schema", {"process_key": KEY}))
    except RuntimeError as caught:
        assert caught is error, "original stdio exception identity"
    else:
        raise AssertionError("original stdio exception must propagate")
    assert surface.discovery.attempts == 1, "one discovery attempt"
    row = diagnostic(capture, "stdio", "schema_discovery")
    assert row["exception_type"] == "RuntimeError"
    assert row["bridge_id"] == BRIDGE
    assert row["request_id_digest"] == "unknown"
    diagnostic(capture, "stdio", "forwarder_dispatch")


def check_stream_schema_attr(capture: Capture) -> None:
    surface = Surface(RuntimeError(SECRET))
    status, body = stream(surface, "process_schema", {"process_key": KEY})
    assert status == 500 and body["error"]["code"] == -32603, "existing unexpected error contract"
    assert surface.discovery.attempts == 1
    row = diagnostic(capture, "streamable", "schema_discovery")
    assert row["exception_type"] == "RuntimeError"
    assert row["bridge_id"] == BRIDGE
    assert row["request_id_digest"] != "unknown" and len(row["request_id_digest"]) == 64
    diagnostic(capture, "streamable", "tool_dispatch")


def check_known_error(capture: Capture) -> None:
    error = BridgeError("bridge.discovery_unavailable", SECRET)
    surface = Surface(error)
    status, body = stream(surface, "process_schema", {"process_key": KEY})
    assert status == 200 and body["error"]["code"] == -32000, "known JSON-RPC code unchanged"
    assert body["error"]["data"]["code"] == error.code, "known BridgeError code unchanged"
    row = diagnostic(capture, "streamable", "tool_dispatch")
    assert row["exception_type"] == "JsonRpcError"
    assert row["code"] == -32000
    assert row["bridge_code"] == error.code


def check_calls(capture: Capture) -> None:
    surface = Surface()
    status, body = stream(surface, "process_schema", {"process_key": KEY})
    assert status == 200 and KEY in body["result"]["content"][0]["text"]
    start = len(capture.fields)
    result = asyncio.run(_dispatch_tool(cast("Any", Forwarder(surface)), "process_call", {"process_key": KEY, "arguments": {"secret": SECRET}}))
    assert result == {"status": "queued", "action_id": "act-fake"}
    assert surface.attempts == 1, "successful call submitted once"
    assert len(capture.fields) == start, "no success diagnostics"
    error = RuntimeError(SECRET)
    failing = Surface(error)
    try:
        asyncio.run(_dispatch_tool(cast("Any", Forwarder(failing)), "process_call", {"process_key": KEY, "arguments": {"secret": SECRET}, "reason": SECRET}))
    except RuntimeError as caught:
        assert caught is error, "original call exception identity"
    else:
        raise AssertionError("original call exception must propagate")
    assert failing.attempts == 1, "failed call submitted once; no retry"
    diagnostic(capture, "stdio", "call_submission", "process_call")
    status, body = stream(Surface(error), "process_call", {"process_key": KEY, "arguments": {"secret": SECRET}})
    assert status == 500 and body["error"]["code"] == -32603
    diagnostic(capture, "streamable", "call_submission", "process_call")


def check_preservation(capture: Capture) -> None:
    surface = Surface()
    result = asyncio.run(_dispatch_tool(cast("Any", Forwarder(surface)), "process_schema", {"process_key": KEY}))
    assert result == {"process_key": KEY}, "successful schema unchanged"
    status, body = stream(surface, "process_call", {"process_key": KEY, "arguments": {}})
    assert status == 200 and body["result"]["isError"] is False
    assert surface.attempts == 1, "streamable success submitted once"
    error = BridgeError("bridge.discovery_unavailable", SECRET)
    try:
        asyncio.run(_dispatch_tool(cast("Any", Forwarder(Surface(error))), "process_schema", {"process_key": KEY}))
    except BridgeError as caught:
        assert caught is error and caught.code == error.code, "stdio known error unchanged"
    else:
        raise AssertionError("stdio known error must propagate")


def check_identity(capture: Capture) -> None:
    error = BridgeError("bridge.discovery_unavailable", SECRET)
    # Outside dispatch there is no transport/request provenance to infer.
    try:
        Surface(RuntimeError(SECRET)).process_schema(KEY)
    except RuntimeError:
        pass
    assert capture.fields[-1]["transport"] == "unknown", "transport context restored"
    for request_id in (None, {"credential": SECRET}, SECRET * 200):
        status, body = stream(Surface(error), "process_schema", {"process_key": KEY}, request_id=request_id)
        assert status == 200 and body["error"]["code"] == -32000
        assert capture.fields[-1]["request_id_digest"] == "unknown", "unsafe identity unknown"


def check_malformed(capture: Capture) -> None:
    # Diagnostic extraction cannot replace the pre-existing KeyError/TypeError
    # or invalid-params response, even for malformed tool/key/argument shapes.
    for tool, args in [(SECRET, {}), ("process_schema", {}), ("process_schema", []), ("process_schema", {"process_key": SECRET}), ("process_schema", {"process_key": [SECRET]})]:
        status, body = stream(Surface(), tool, args)
        assert status in (200, 500) and "error" in body, "original malformed route failure"
        try:
            asyncio.run(_dispatch_tool(cast("Any", Forwarder(Surface())), tool, cast("Any", args)))
        except (ValueError, KeyError, TypeError, BridgeError):
            pass
        else:
            raise AssertionError("original malformed stdio failure")
    fields = capture.fields[-1]
    assert fields["process_key"] == "unknown"
    fw = Forwarder(Surface(RuntimeError(SECRET)))
    fw.bridge_id = None
    try:
        asyncio.run(_dispatch_tool(cast("Any", fw), "process_schema", {"process_key": KEY}))
    except RuntimeError:
        pass
    assert capture.fields[-1]["bridge_id"] == "unknown", "absent identity explicit unknown"


class BrokenLog(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        del record
        raise RuntimeError("diagnostic sink unavailable")


def check_log_failure() -> None:
    error = RuntimeError(SECRET)
    root = logging.getLogger("agent_messaging_plugin")
    handler = BrokenLog()
    root.addHandler(handler)
    try:
        try:
            Surface(error).process_schema(KEY)
        except RuntimeError as caught:
            assert caught is error, "diagnostic sink cannot replace original exception"
        else:
            raise AssertionError("original exception retained with broken logging")
    finally:
        root.removeHandler(handler)


def check_redaction(capture: Capture) -> None:
    assert SECRET not in json.dumps(capture.fields), "sensitive sentinel absent from added diagnostic fields"
    allowed = {"tool", "process_key", "transport", "stage", "exception_type", "code", "bridge_code", "bridge_id", "request_id_digest"}
    assert capture.fields and all(set(row) == allowed for row in capture.fields), "safe field inventory"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", choices=("stdio", "streamable"))
    args = parser.parse_args()
    capture = Capture()
    root = logging.getLogger("agent_messaging_plugin")
    root.addHandler(capture)
    try:
        if args.case == "stdio":
            check_stdio_schema_attr(capture)
        elif args.case == "streamable":
            check_stream_schema_attr(capture)
        else:
            check_stdio_schema_attr(capture)
            check_stream_schema_attr(capture)
            check_known_error(capture)
            check_calls(capture)
            check_preservation(capture)
            check_identity(capture)
            check_malformed(capture)
            check_log_failure()
        check_redaction(capture)
    finally:
        root.removeHandler(capture)
    print("PASS fleet liveness MCP attribution (fake-only reconstructed requests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
