"""Plugin-owned serial handoff through the platform's persistent job ledger.

Only locally admitted jobs run here; a second runtime cannot replay effects.
The worker retires when its queue drains. Read outcomes with job_service.get_job.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, ReturnValueSchema
from ananta.core.plugins.action_execution import _create_error_response
from ananta.core.plugins.plugin_base import PluginBase

from .constants import PLUGIN_NAME, ErrorCode

JOB_RETURN_SCHEMA = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Queued job receipt; job.result contains the former response envelope.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, required=True),
        "status": ParameterMetadata(type=ParameterType.STRING, required=True),
    },
)

type Operation = Callable[[dict[str, Any]], dict[str, Any]]
# Public registration selection is finite and independently checked against core.
REGISTERED_MEMORY_OPERATIONS = frozenset(
    {
        "audit_pinned_notes",
        "cleanup_orphaned_vectors",
        "consolidate",
        "consolidate_cron",
        "delete_memories_by_ids",
        "delete_memories_by_tag",
        "forget",
        "get_memories_by_tag",
        "get_recent_memory",
        "get_recent_memory_structured",
        "import_memories",
        "ingest_session",
        "learn",
        "list_memories",
        "list_memorizing",
        "memorize",
        "memorize_by_tag",
        "memory_stats",
        "process_memorization_queue",
        "process_memorization_queue_cron",
        "purge_memories",
        "recall",
        "recompute_strengths",
        "recompute_strengths_cron",
        "reindex_orphaned_vectors",
        "remember",
        "review_blocked_intents",
        "store_compaction_summary",
    }
)
# These two registered consumers still require a full synchronous response.
_SERIAL_MEMORY_OPERATIONS = REGISTERED_MEMORY_OPERATIONS | {"export_memories", "upsert_memory_by_tag"}


def _manager(plugin: PluginBase) -> Any:
    manager = getattr(plugin.orchestrator_ref, "async_job_manager", None)
    if manager is None:
        raise RuntimeError(f"{plugin.name}: async_job_manager unavailable")
    return manager


def _serial_method(plugin: PluginBase, jobs: AsyncJobs, method: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    @wraps(method)
    def invoke(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return jobs.call(plugin, lambda: method(*args, **kwargs))

    return invoke


class AsyncJobs:
    """Adapt existing plugin actions and hosted Python bodies to one owner FIFO.

    Only the admission descriptor is persistent. The local closure retains the
    original body and payload references, so admission never walks or encodes a
    transcript, content string or import body. Losing that closure requires
    reconciliation; a request read is never authority to replay effects.
    """

    def __init__(self) -> None:
        self._bound = False
        self._body_lock = threading.RLock()

    def bind_serial_api(self, plugin: PluginBase) -> None:
        """Bind hosted service bodies once; standalone backend APIs stay synchronous."""
        if not self._bound:
            for name in _SERIAL_MEMORY_OPERATIONS:
                setattr(plugin, name, _serial_method(plugin, self, getattr(plugin, name)))
            self._bound = True

    def _run(self, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._body_lock:
            return operation()

    def call(self, plugin: PluginBase, operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """Keep full Python results, including startup before ServiceManager exists."""
        manager = getattr(plugin.orchestrator_ref, "async_job_manager", None)
        if manager is None:
            # handle_clean_restart runs before init_service_manager. Its existing
            # synchronous purge must finish; no registered job is admissible yet.
            # The same body lock fences a concurrent first admission if startup
            # hands ownership to the manager while this body is still running.
            logging.getLogger(__name__).info("%s synchronous memory body before AsyncJobManager initialization", plugin.name)
            return self._run(operation)
        return manager.run_registered_serial_body(plugin.name, {}, lambda _descriptor: self._run(operation))

    def submit(
        self,
        plugin: PluginBase,
        action_name: str,
        parameters: dict[str, Any],
        state: dict[str, Any],
        operation: Operation,
    ) -> dict[str, Any]:
        """Commit a bounded descriptor, then hand the complete body to its owner."""
        try:
            for name in ("session_id", "flow_id"):
                if not isinstance(state.get(name), str) or not state[name]:
                    raise ValueError(f"{name} missing from state context")
            manager = _manager(plugin)
            owner = manager._operation_owner(plugin.name)
            descriptor = {"operation": action_name, "payload": "local_closure", "replayable": False}
            receipt = manager.create_job(plugin_name=plugin.name, action_name=action_name, request_data={"notes": f"{plugin.name}.{action_name}", "parameters": descriptor}, job_metadata={"session_id": state["session_id"], "flow_id": state["flow_id"]})
            if receipt.get("action_status") != "completed":
                raise RuntimeError(f"Job creation failed: {receipt.get('error')}")
            return owner.accept_committed(receipt, lambda _descriptor: self._run(lambda: operation(parameters)))
        except Exception as exc:
            return _dispatcher_error(exc)


def _dispatcher_error(exc: Exception) -> dict[str, Any]:
    """Return the complete action envelope required by plugin dispatch."""
    return {
        **_create_error_response(exc),
        "data": {},
        "actions": [],
        "timestamp": datetime.now(UTC).isoformat(),
    }


def run_memory_action(plugin: Any, action_name: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Run the unchanged synchronous backend contract on the memory worker."""
    service = plugin._get_backend()
    if action_name == "learn_action":
        result = service.learn(
            path=parameters.get("path", ""),
            pattern=parameters.get("pattern", "*.md"),
            recursive=parameters.get("recursive", True),
            memorize=parameters.get("memorize", False),
            tags=parameters.get("tags", []),
        )
    elif action_name == "import_memories_action":
        result = service.import_memories(file_path=parameters.get("file_path", ""))
    else:
        result = service.purge_memories(confirm=parameters.get("confirm", False))
    if "error" in result:
        return plugin._build_response("error", {}, {"type": "plugin_error", "code": ErrorCode.OPERATION_FAILED, "message": result["error"], "plugin_name": PLUGIN_NAME})
    return plugin._build_response("completed", result)
