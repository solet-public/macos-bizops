"""Offline ledger and production queued-action assertions for plugin-owned jobs."""

from __future__ import annotations

import threading
import time
from contextlib import ExitStack
from copy import deepcopy
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from ananta.core.actions.action_processor import ActionProcessor
from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.services.job_service.service import JobService

CONTEXT = {"session_id": "fixture-session", "flow_id": "fixture-flow"}


class Ledger:
    """AsyncJobManager envelopes, payloads and terminal updates, without a DB."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.payload_manager = AsyncJobManager.__new__(AsyncJobManager)

    def create_job(self, **kwargs: Any) -> dict[str, Any]:
        with self.lock:
            job_id = f"job-{len(self.rows) + 1}"
            self.rows[job_id] = {"id": job_id, "provider_name": f"{kwargs['plugin_name']}.{kwargs['action_name']}", "status": "queued"}
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
            ledger_updates, result, error = self.payload_manager._extract_update_fields(updates)
            self.rows[job_id].update(deepcopy(ledger_updates))
            for kind, payload in (("result", result), ("error", error)):
                if payload is not None:
                    self.rows[job_id][kind] = deepcopy(payload)
        return {"action_status": "completed", "data": {"outcome_committed": True}}

    def list_jobs(self, *, provider_name: str, status: str, **_kwargs: Any) -> dict[str, Any]:
        with self.lock:
            rows = [deepcopy(row) for row in self.rows.values() if row["provider_name"] == provider_name and row["status"] == status]
        return {"action_status": "completed", "data": {"jobs": rows}}


def normalized(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<time>" if key == "timestamp" or key.endswith("_at") else normalized(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [normalized(item) for item in value]
    return value


def execute(plugin: Any, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Use the outer ActionProcessor route, including its ActionResult guard."""
    plugin.get_available_actions()  # Validate the same registered signatures as boot.
    processor = ActionProcessor.__new__(ActionProcessor)
    processor.plugin_manager = cast("Any", SimpleNamespace(get_plugin=lambda name: plugin if name == plugin.name else None))
    action = SimpleNamespace(id="fixture-action", session_id="fixture-session", flow_id="fixture-flow", context_id=None, flow_token_id=None, process_key=f"plugin::{plugin.name}::{verb}")
    with ExitStack() as stack:
        stack.enter_context(patch.object(processor, "_prepare_arguments", return_value=params))
        stack.enter_context(patch.object(processor, "_resolve_placeholders", side_effect=lambda arguments, _action: arguments))
        for name in ("_store_result_in_context", "_setup_plugin_context", "_lift_inference_vertex_identity", "_build_call_context"):
            stack.enter_context(patch.object(processor, name, return_value=None))
        return processor.execute_action(cast("Any", action))


def terminal(plugin: Any, ledger: Ledger, job_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        row = ledger.get_job(job_id)["data"]["job"]
        if row["status"] in ("completed", "error"):
            if plugin.name == "agent_messaging_plugin":
                status = plugin.check_choreography_job_status({"job_id": job_id}, {})["data"]
                assert status["status"] == row["status"]
                assert normalized(status["result"] if row["status"] == "completed" else status["error"]) == normalized(row["result"] if row["status"] == "completed" else row["error"])
            else:
                # The real status service attaches result/error payloads. Its
                # query port is replaced, while get_job's public path is real.
                service = JobService.__new__(JobService)
                with patch.object(service, "_query_job_by_id", return_value=row):
                    readback = service.get_job(job_id)["data"]["result"]["job"]
                assert readback == row
            return row
        time.sleep(0.005)
    raise AssertionError(f"Job never became terminal: {ledger.rows}")


def _assert_outcome(row: dict[str, Any], expected: dict[str, Any], failed: bool) -> None:
    assert normalized(row["result"]) == expected, (row["result"], expected)
    if failed:
        error = expected.get("error")
        preserved = error if isinstance(error, dict) else expected
        assert isinstance(row["error"], dict), row
        assert normalized(row["error"]) == preserved, (row["error"], preserved)


def _assert_receipt(receipt: dict[str, Any], ledger: Ledger) -> str:
    """Require a usable outer-route handle for exactly one admitted job."""
    assert receipt.get("success") is True and receipt.get("action_status") == "completed" and receipt.get("data", {}).get("status") == "queued", receipt
    job_id = receipt["data"].get("job_id")
    assert isinstance(job_id, str) and job_id, receipt
    assert len(ledger.rows) == 1 and job_id in ledger.rows, ledger.rows
    return job_id


def measure_case(module: ModuleType, verb: str, params: dict[str, Any], failed: bool, goldens: dict[str, Any], fixture_factory: Any) -> None:
    with fixture_factory(module, verb, delay=0.3, failed=failed) as (plugin, ledger, touches):
        if plugin.name == "agent_messaging_plugin":
            plugin._start_choreography_worker()
        try:
            caller = threading.get_ident()
            started = time.perf_counter()
            receipt = execute(plugin, verb, params)
            milliseconds = (time.perf_counter() - started) * 1000
            job_id = _assert_receipt(receipt, ledger)
            assert caller not in touches, (verb, touches, caller)
            assert milliseconds < 50, (verb, milliseconds)
            row = terminal(plugin, ledger, job_id)
            assert len(ledger.rows) == 1, ledger.rows
            assert touches and caller not in touches, (verb, touches, caller)
            assert row["status"] == ("error" if failed else "completed"), row
            key = f"{plugin.name}.{verb}.{'error' if failed else 'success'}"
            _assert_outcome(row, goldens["outputs"][key], failed)
            print(f"PASS {key}: dispatch={milliseconds:.3f}ms outer_route=true success=true jobs=1 worker_touches={len(touches)} caller_touches=0 base_equal=true payload_filter=production")
        finally:
            if plugin.name == "agent_messaging_plugin":
                plugin._stop_choreography_worker()


def check_wrapper(plugin: Any, ledger: Ledger, verb: str, params: dict[str, Any]) -> None:
    if plugin.name == "agent_messaging_plugin":
        plugin._start_choreography_worker()
    try:
        outcome = execute(plugin, verb, params)
        assert outcome.get("success") is True and outcome.get("data", {}).get("status") == "queued", outcome
        terminal(plugin, ledger, outcome["data"]["job_id"])
    finally:
        if plugin.name == "agent_messaging_plugin":
            plugin._stop_choreography_worker()


def serial_worker(plugin: Any, ledger: Ledger) -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def operation(params: dict[str, Any]) -> dict[str, Any]:
        calls.append(params["number"])
        if params["number"] == 1:
            entered.set()
            assert release.wait(3), "First operation must be released"
        return {"action_status": "completed", "data": params}

    jobs = plugin._jobs
    first = jobs.submit(plugin, "fixture", {"number": 1}, CONTEXT, operation)
    assert entered.wait(2), "Worker must reach its operation"
    second = jobs.submit(plugin, "fixture", {"number": 2}, CONTEXT, operation)
    cancelled = jobs.submit(plugin, "fixture", {"number": 3}, CONTEXT, operation)
    ledger.update_job(cancelled["data"]["job_id"], {"status": "cancelled"})
    assert calls == [1], "Only one worker may execute this plugin's effects"
    release.set()
    terminal(plugin, ledger, first["data"]["job_id"])
    terminal(plugin, ledger, second["data"]["job_id"])
    deadline = time.monotonic() + 2
    while jobs._thread is not None and time.monotonic() < deadline:
        time.sleep(0.005)
    assert jobs._thread is None and calls == [1, 2], "Drain must skip cancellation and retire"
    restarted = jobs.submit(plugin, "fixture", {"number": 4}, CONTEXT, operation)
    terminal(plugin, ledger, restarted["data"]["job_id"])
    assert calls == [1, 2, 4], "A later submission must restart the sole worker"
