"""Guarded execute-path jobs for macos_coding_agent_session_plugin.

Run: .venv/bin/python plugins/macos_coding_agent_session_plugin/tests/subprocess_verbs_smoke.py
Only this plugin and platform code are imported, including in shipped seeds.
Every subprocess and network boundary is guarded; publication uses a local fake.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "macos_coding_agent_session_plugin" / "src"))
# ruff: noqa: E402, SLF001 -- explicit isolated plugin fixtures
from ananta.core.actions.action_processor import ActionProcessor
from ananta.core.state.async_job_manager import AsyncJobManager
from macos_coding_agent_session_plugin.bridge_tracker import BridgeTracker
from macos_coding_agent_session_plugin.plugin import MacosCodingAgentSessionPlugin

CONTEXT = {"session_id": "fixture-session", "flow_id": "fixture-flow"}
VERBS = (("macos_coding_agent_session_plugin", "spawn_bridge"),)
FAILURE_IS_DATA = True


class Ledger:
    """Ledger port with production result/error field extraction."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.manager = AsyncJobManager.__new__(AsyncJobManager)

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            job_id = f"fixture-job-{len(self.rows) + 1}"
            self.rows[job_id] = {"id": job_id, "provider_name": f"{kwargs['plugin_name']}.{kwargs['action_name']}", "status": "queued", "metadata": kwargs.get("job_metadata", {})}
            self.requests[job_id] = deepcopy(kwargs["request_data"])
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            return {"action_status": "completed", "data": {"job": deepcopy(self.rows[job_id])}}

    def get_job_payload(self, job_id: str, payload_type: str) -> dict[str, Any]:
        with self.lock:
            payload = self.requests[job_id] if payload_type == "request" else self.rows[job_id].get(payload_type)
            return {"action_status": "completed", "data": {"payload": deepcopy(payload)}}

    def update_job(self, job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            fields, result, error = self.manager._extract_update_fields(updates)
            self.rows[job_id].update(deepcopy(fields))
            for kind, payload in (("result", result), ("error", error)):
                if payload is not None:
                    self.rows[job_id][kind] = deepcopy(payload)
        return {"action_status": "completed", "data": {"outcome_committed": True}}

    def list_jobs(self, *, provider_name: str, status: str, **_kwargs: Any) -> dict[str, Any]:
        with self.lock:
            rows = [deepcopy(row) for row in self.rows.values() if row["provider_name"] == provider_name and row["status"] == status]
        return {"action_status": "completed", "data": {"jobs": rows}}


def execute(plugin: Any, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Exercise the real registered action lookup and outer ActionResult guard."""
    processor = ActionProcessor.__new__(ActionProcessor)
    processor.plugin_manager = cast("Any", SimpleNamespace(get_plugin=lambda _name: plugin))
    action = SimpleNamespace(id="fixture-action", session_id=CONTEXT["session_id"], flow_id=CONTEXT["flow_id"], context_id=None, flow_token_id=None, process_key=f"plugin::{plugin.name}::{verb}")
    with ExitStack() as stack:
        stack.enter_context(patch.object(processor, "_prepare_arguments", return_value=params))
        stack.enter_context(patch.object(processor, "_resolve_placeholders", side_effect=lambda arguments, _action: arguments))
        for name in ("_store_result_in_context", "_setup_plugin_context", "_lift_inference_vertex_identity", "_build_call_context"):
            stack.enter_context(patch.object(processor, name, return_value=None))
        return processor.execute_action(cast("Any", action))


def terminal(ledger: Ledger, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = ledger.get_job(job_id)["data"]["job"]
        if row["status"] in {"completed", "error", "cancelled"}:
            return row
        time.sleep(0.005)
    raise AssertionError(f"job did not finish: {ledger.rows}")


@contextmanager
def fixture(plugin_name: str, verb: str, *, failed: bool = False) -> Any:
    """Replace leaf boundaries, keeping dispatch and job workers real."""
    ledger = Ledger()
    touches: list[int] = []
    forwarded: list[dict[str, Any]] = []
    with ExitStack() as stack:

        def boundary(argv: list[str], **_kwargs: Any) -> Any:
            assert argv == ["fixture-subprocess"], f"unguarded subprocess: {argv!r}"
            touches.append(threading.get_ident())
            time.sleep(0.3)
            if failed:
                raise OSError("guarded subprocess failed")
            return SimpleNamespace(pid=424242, stdout="fixture", returncode=0)

        stack.enter_context(patch("subprocess.run", side_effect=boundary))
        stack.enter_context(patch("subprocess.Popen", side_effect=boundary))
        for name in ("socket.socket.connect", "socket.create_connection", "socket.getaddrinfo"):
            stack.enter_context(patch(name, side_effect=AssertionError("network forbidden")))
        plugin: Any = MacosCodingAgentSessionPlugin()

        def spawn(agent_instance_id: str, solet_name: str) -> Any:
            assert agent_instance_id == "agi-local-fixture" and solet_name == "local-fixture"
            return subprocess.Popen(["fixture-subprocess"])

        plugin._tracker = BridgeTracker(logger=logging.getLogger("fixture"), spawn_fn=spawn)
        params: dict[str, Any] = {"agent_instance_id": "agi-local-fixture", "solet_name": "local-fixture"}
        plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger)
        if hasattr(plugin, "_async_job_manager"):
            plugin._async_job_manager = ledger
        plugin.get_available_actions()
        try:
            yield plugin, ledger, params, touches, forwarded
        finally:
            # Keep every guard installed until this fixture's admitted work ends.
            for job_id, request in tuple(ledger.requests.items()):
                if request.get("notes") != "prior runtime request":
                    terminal(ledger, job_id)


def check_case(plugin_name: str, verb: str, *, failed: bool = False) -> None:
    with fixture(plugin_name, verb, failed=failed) as (plugin, ledger, params, touches, forwarded):
        caller = threading.get_ident()
        started = time.perf_counter()
        receipt = execute(plugin, verb, params)
        elapsed = time.perf_counter() - started
        data = _queued_receipt(verb, receipt)
        assert elapsed < 0.05, f"{verb}: handler blocked {elapsed * 1000:.1f}ms"
        assert caller not in touches, f"{verb}: subprocess ran on caller"
        job_id = data["job_id"]
        row = terminal(ledger, job_id)
        assert row["provider_name"] == f"{plugin_name}.{verb}", row
        assert touches and caller not in touches and len(touches) == 1, touches
        _assert_terminal(row, failed)
        if not failed:
            _assert_result(plugin_name, verb, row["result"], params, forwarded)
        print(f"PASS {plugin_name}.{verb} failed={failed}: dispatch_ms={elapsed * 1000:.3f} worker_boundary=1 caller_boundary=0 terminal={row['status']}")


def _assert_terminal(row: dict[str, Any], failed: bool) -> None:
    if failed and not FAILURE_IS_DATA:
        assert row["status"] == "error" and row["error"], row
    else:
        assert row["status"] == "completed" and row["result"], row
    if failed and FAILURE_IS_DATA:
        assert row["result"]["status"] == "failed", row


def _queued_receipt(verb: str, receipt: dict[str, Any]) -> dict[str, Any]:
    data = receipt.get("data", {})
    assert data.get("job_id") and data.get("status") == "queued", f"{verb}: expected queued job receipt after guarded boundary, got {receipt!r}"
    return data


def check_queued_cancellation(plugin_name: str, verb: str) -> None:
    with fixture(plugin_name, verb) as (plugin, ledger, params, touches, _forwarded):
        first = execute(plugin, verb, params)["data"]["job_id"]
        deadline = time.monotonic() + 3
        while not touches and time.monotonic() < deadline:
            time.sleep(0.005)
        assert touches, "first job must own the delayed subprocess"
        second = execute(plugin, verb, params)["data"]["job_id"]
        ledger.update_job(second, {"status": "cancelled"})
        third = execute(plugin, verb, params)["data"]["job_id"]
        assert terminal(ledger, first)["status"] == "completed"
        assert terminal(ledger, third)["status"] == "completed"
        assert terminal(ledger, second)["status"] == "cancelled"
        assert len(touches) == 2, "cancelled queued job must not reach its subprocess"
        print(f"PASS {plugin_name}.{verb}: queued cancellation skips effects, later job completes")


def _assert_result(_plugin_name: str, _verb: str, result: dict[str, Any], _params: dict[str, Any], _forwarded: list[dict[str, Any]]) -> None:
    assert result["pid"] == 424242
    assert result["status"] == "success"


def check_job_creation_failure(plugin_name: str, verb: str) -> None:
    with fixture(plugin_name, verb) as (plugin, ledger, params, touches, _forwarded):
        with patch.object(ledger, "create_job", return_value={"action_status": "error", "error": {"message": "guarded ledger refusal"}}):
            result = execute(plugin, verb, params)
        assert result.get("success") is False, result
        assert not touches and not ledger.rows, "a failed job creation must run no effects"
        print(f"PASS {plugin_name}.{verb}: job creation failure preserves the error and runs no effect")


def main() -> int:
    failures: list[str] = []
    for plugin_name, verb in VERBS:
        for failed in (False, True):
            try:
                check_case(plugin_name, verb, failed=failed)
            except AssertionError as exc:
                failures.append(f"{plugin_name}.{verb} failed={failed}: {exc}")
                print(f"FAIL {failures[-1]}", flush=True)
    assert not failures, "\n".join(failures)
    for plugin_name, verb in VERBS:
        check_queued_cancellation(plugin_name, verb)
        check_job_creation_failure(plugin_name, verb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
