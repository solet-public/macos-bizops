"""Plugin-owned jobs on the platform ledger and sanctioned singleton drainer.

The platform's BoundedSummaryExecutor runs one waiting consumer. Only jobs
submitted by this instance enter its queue; a new runtime never replays an old
effect. The daemon belongs to the process, as with the existing connector
workers. Completed data and classified errors retain the former handler shape.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from queue import SimpleQueue
from threading import Lock
from typing import Any

from ananta.core.plugins.plugin_base import PluginBase
from ananta.services.session_ledger_service.summary_executor import BoundedSummaryExecutor

type Operation = Callable[[dict[str, Any]], dict[str, Any]]
type Pending = tuple[Any, str, Operation]


class AsyncJobs:
    """Persist requests and hand them to the existing platform drainer."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._pending: SimpleQueue[Pending] = SimpleQueue()
        self._lock = Lock()
        self._started = False
        self._executor = BoundedSummaryExecutor(name=f"{name}-jobs")

    def submit(self, plugin: PluginBase, verb: str, params: dict[str, Any], state: dict[str, Any], operation: Operation) -> dict[str, Any]:
        metadata = _routing_context(state)
        manager = getattr(plugin.orchestrator_ref, "async_job_manager", None)
        if manager is None:
            raise RuntimeError(f"{plugin.name}: async_job_manager unavailable")
        receipt = manager.create_job(
            plugin_name=self._name,
            action_name=verb,
            request_data={"notes": f"{self._name}.{verb}", "parameters": deepcopy(params)},
            job_metadata=metadata,
        )
        if receipt.get("action_status") != "completed":
            return {"action_status": "error", "data": {}, "actions": [], "error": receipt.get("error"), "timestamp": datetime.now(UTC).isoformat()}
        job_id = receipt.get("data", {}).get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RuntimeError("Job creation returned no canonical job id")
        with self._lock:
            if not self._started:
                if not self._executor.submit(self._drain):
                    manager.update_job(job_id, {"status": "error", "error": {"message": "Platform job drainer did not start"}})
                    raise RuntimeError("Platform job drainer did not start")
                self._started = True
            self._pending.put((manager, job_id, operation))
        return {"action_status": "completed", "data": {"job_id": job_id, "status": "queued"}, "actions": [], "error": None, "timestamp": datetime.now(UTC).isoformat()}

    def _drain(self) -> None:
        while True:
            manager, job_id, operation = self._pending.get()
            try:
                _process_job(manager, job_id, operation)
            except Exception:
                # A ledger fault is visible and never authorizes replaying an effect.
                logging.getLogger(__name__).exception("Job %s needs reconciliation", job_id)


def _process_job(manager: Any, job_id: str, operation: Operation) -> None:
    current = manager.get_job(job_id)
    if current.get("action_status") != "completed" or current.get("data", {}).get("job", {}).get("status") != "queued":
        return
    marked = manager.update_job(job_id, {"status": "processing"})
    if marked.get("action_status") != "completed":
        raise RuntimeError(f"Could not mark job processing: {marked!r}")
    try:
        request = manager.get_job_payload(job_id, "request")
        if request.get("action_status") != "completed":
            raise RuntimeError("Could not load job request")
        outcome = operation(request["data"]["payload"]["parameters"])
    except Exception as exc:
        outcome = {"action_status": "error", "data": {}, "actions": [], "error": {"message": str(exc)}, "timestamp": datetime.now(UTC).isoformat()}
    terminal = manager.update_job(job_id, _outcome_updates(outcome))
    if terminal.get("action_status") != "completed":
        raise RuntimeError(f"Could not persist job outcome: {terminal!r}")


def _routing_context(state: dict[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in ("session_id", "flow_id"):
        value = state.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} missing from state context")
        metadata[key] = value
    return metadata


def _outcome_updates(outcome: dict[str, Any]) -> dict[str, Any]:
    if outcome.get("action_status") == "completed":
        return {"status": "completed", "result": outcome.get("data") or {}}
    return {"status": "error", "error": outcome.get("error") or {}}
