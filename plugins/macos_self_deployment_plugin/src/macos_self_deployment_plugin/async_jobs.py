"""Plugin-owned job handoff using the platform ledger and one serial worker.

Only this instance's submissions are executed: another runtime color cannot
replay queued effects. The daemon exits when its local handoff queue drains.
Read terminal results through service_interface::job_service::get_job.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable
from contextvars import Context, copy_context
from datetime import UTC, datetime
from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, ReturnValueSchema
from ananta.core.plugins.action_execution import _create_error_response
from ananta.core.plugins.plugin_base import PluginBase

JOB_RETURN_SCHEMA = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Queued job receipt; retrieve the former response from job.result.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, required=True),
        "status": ParameterMetadata(type=ParameterType.STRING, required=True),
    },
)

type Operation = Callable[[dict[str, Any]], dict[str, Any]]
type Pending = tuple[Any, str, Operation, Context]


class AsyncJobs:
    """One worker per plugin instance; the existing ledger owns job history."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: deque[Pending] = deque()
        self._thread: threading.Thread | None = None

    def submit(
        self,
        plugin: PluginBase,
        action_name: str,
        parameters: dict[str, Any],
        state: dict[str, Any],
        operation: Operation,
    ) -> dict[str, Any]:
        """Persist the request, hand off its id, and return without running it."""
        for name in ("session_id", "flow_id"):
            if not isinstance(state.get(name), str) or not state[name]:
                raise ValueError(f"{name} missing from state context")
        manager = getattr(plugin.orchestrator_ref, "async_job_manager", None)
        if manager is None:
            raise RuntimeError(f"{plugin.name}: async_job_manager unavailable")
        receipt = manager.create_job(
            plugin_name=plugin.name,
            action_name=action_name,
            request_data={"notes": f"{plugin.name}.{action_name}", "parameters": parameters},
            job_metadata={"session_id": state["session_id"], "flow_id": state["flow_id"]},
        )
        data = receipt.get("data", {})
        if receipt.get("action_status") != "completed":
            return receipt
        if not isinstance(data.get("job_id"), str) or not data["job_id"]:
            raise RuntimeError("Job creation returned no canonical job id")
        job_id = str(data["job_id"])
        with self._lock:
            self._pending.append((manager, job_id, operation, copy_context()))
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._drain,
                    name=f"{plugin.name}-job-worker",
                    daemon=True,
                )
                try:
                    self._thread.start()
                except Exception as exc:
                    self._pending.pop()
                    self._thread = None
                    manager.update_job(job_id, {"status": "error", "error": {"message": str(exc)}})
                    raise
        return {
            "action_status": "completed",
            "data": {"job_id": job_id, "status": "queued"},
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return
                manager, job_id, operation, context = self._pending.popleft()
            try:
                context.run(self._run, manager, job_id, operation)
            except Exception:
                # A failed ledger write is not authorization to replay an effect.
                logging.getLogger(__name__).exception("Job %s needs reconciliation", job_id)

    @staticmethod
    def _run(manager: Any, job_id: str, operation: Operation) -> None:
        current = manager.get_job(job_id)
        job = current.get("data", {}).get("job", {})
        if current.get("action_status") != "completed" or job.get("status") != "queued":
            return
        marked = manager.update_job(job_id, {"status": "processing"})
        if marked.get("action_status") != "completed":
            raise RuntimeError(f"Could not mark job processing: {marked!r}")
        try:
            request = manager.get_job_payload(job_id, "request")
            if request.get("action_status") != "completed":
                raise RuntimeError("Could not load job request")
            parameters = request["data"]["payload"]["parameters"]
            outcome = operation(parameters)
        except Exception as exc:
            outcome = _create_error_response(exc)
        terminal = manager.update_job(job_id, _outcome_updates(outcome))
        if terminal.get("action_status") != "completed":
            raise RuntimeError(f"Could not persist job outcome: {terminal!r}")


def _outcome_updates(outcome: dict[str, Any]) -> dict[str, Any]:
    """Keep the original envelope; the platform persists only object payloads."""
    failed = bool(outcome.get("error")) or outcome.get("action_status") == "error"
    updates: dict[str, Any] = {"status": "error" if failed else "completed", "result": outcome}
    if failed:
        error = outcome.get("error")
        updates["error"] = error if isinstance(error, dict) else outcome
    return updates
