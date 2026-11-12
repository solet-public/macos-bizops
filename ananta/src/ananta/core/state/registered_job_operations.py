from __future__ import annotations

import json
import logging
import threading
from collections import deque
from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import wraps
from typing import TYPE_CHECKING

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, ReturnValueSchema
from ananta.core.plugins.action_execution import _create_error_response
from ananta.error_handling import AnantaError, FrameworkError

if TYPE_CHECKING:
    from ananta.core.state.async_job_manager import AsyncJobManager

logger = logging.getLogger(__name__)

THINKING_REGISTERED_OPERATIONS = frozenset(
    {
        "advance_wbs_execution",
        "create_authored_artifact",
        "create_extended_plan",
        "create_movement_design",
        "create_resolved_intake_state",
        "create_work_manifest",
        "generate_section_stem_wbs",
        "get_next_wbs_step",
        "get_plan_template",
        "graft_work_breakdown_structure_segment",
        "patch_authored_artifact",
        "patch_work_breakdown_structure",
        "patch_work_manifest",
        "process_planning_results",
        "reconcile_joseki_runs",
        "record_wbs_step_observation",
        "record_work_breakdown_structure_step_state",
        "record_work_manifest_phase_state",
        "register_authored_joseki",
        "register_authored_work_breakdown_structure",
        "resume_thinking_completion",
        "run_joseki",
        "start_wbs_execution",
        "transition_authored_joseki",
        "transition_plan_template",
        "update_plan",
        "upsert_plan",
    }
)


def _serial_thinking_method(method: Callable[..., dict[str, object]]) -> Callable[..., dict[str, object]]:
    """Keep direct provider calls in the same FIFO as registered Thinking work."""

    @wraps(method)
    def invoke(instance: object, *args: object, **kwargs: object) -> dict[str, object]:
        # Imported at call time: AsyncJobManager imports this module itself.
        from ananta.core.state.async_job_manager import AsyncJobManager

        manager = getattr(getattr(instance, "orchestrator_ref", None), "async_job_manager", None)
        if not isinstance(manager, AsyncJobManager):
            # A direct standalone provider has no registered worker to race.
            # Preserve that existing Python API; registered entry requires the
            # initialized manager before it can persist any job.
            logger.debug("Thinking direct call has no runtime job owner; executing synchronously")
            return method(instance, *args, **kwargs)
        return manager.run_registered_serial_body("thinking_service", {}, lambda _parameters: method(instance, *args, **kwargs))

    return invoke


def serial_thinking_provider[Provider: type](provider: Provider) -> Provider:
    """One declarative owner boundary, preserving every original signature."""
    for name in THINKING_REGISTERED_OPERATIONS:
        setattr(provider, name, _serial_thinking_method(getattr(provider, name)))
    return provider


REGISTERED_JOB_RETURN_SCHEMA = ReturnValueSchema(
    type=ParameterType.OBJECT,
    description="Queued owned-job receipt. Retrieve its exact id with job_service.get_job; job.result contains the complete operation response and job.error retains a failure.",
    properties={
        "job_id": ParameterMetadata(type=ParameterType.STRING, required=True),
        "status": ParameterMetadata(type=ParameterType.STRING, required=True),
    },
)


def registered_job_return_schema(operation_schema: ReturnValueSchema) -> ReturnValueSchema:
    """Describe the receipt while retaining what the completed job will return."""
    return replace(REGISTERED_JOB_RETURN_SCHEMA, description=f"{REGISTERED_JOB_RETURN_SCHEMA.description} Completed operation: {operation_schema.description}", usage_patterns=operation_schema.usage_patterns)


def _owned_error(error: Exception) -> dict[str, object]:
    outcome = _create_error_response(error)
    if isinstance(error, AnantaError):
        outcome["error"] = error.to_dict()
    return outcome


def _owned_outcome_updates(outcome: dict[str, object]) -> dict[str, object]:
    failed = bool(outcome.get("error")) or outcome.get("action_status") in {"error", "failed"}
    updates: dict[str, object] = {"status": "error" if failed else "completed", "result": outcome}
    if failed:
        error = outcome.get("error")
        if isinstance(error, dict):
            updates["error"] = error
        elif isinstance(outcome.get("error_code"), str):
            updates["error"] = {"code": outcome["error_code"], "message": str(error)}
        else:
            updates["error"] = outcome
    return updates


@dataclass
class RegisteredOperationContext:
    """One admission lineage, copied into its worker and explicit child jobs."""

    action_id: str
    flow_id: str | None
    session_id: str | None
    root_job_id: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def group_id(self) -> str:
        return f"registered:{self.action_id}"

    def stamp(self, record: dict[str, object]) -> None:
        metadata = json.loads(str(record.get("metadata", "{}")))
        metadata.update({key: value for key, value in (("flow_id", self.flow_id), ("session_id", self.session_id)) if value})
        if self.root_job_id is None:
            metadata["registered_action_id"] = self.action_id
        else:
            metadata.pop("registered_action_id", None)
            metadata["registered_root_job_id"] = self.root_job_id
        record.update(group_id=self.group_id, metadata=json.dumps(metadata))


_REGISTERED_ACTION: ContextVar[RegisteredOperationContext | None] = ContextVar("registered_owner_action", default=None)
type OwnedOperation = Callable[[dict[str, object]], dict[str, object]]
type RegisteredCompletion = Callable[[str, str, dict[str, object] | None, dict[str, object] | None], None]


def require_registered_job_flow_token(flow_token_id: str | None, provider_name: str) -> None:
    """Reject an owned job before construction when its action token is absent."""
    owner = _REGISTERED_ACTION.get()
    if owner is not None and not flow_token_id:
        raise FrameworkError(
            message=f"Registered action {owner.action_id} attempted to create a job without a flow token",
            error_code="registered_owner.missing_flow_token",
            details={"action_id": owner.action_id, "provider_name": provider_name},
        )


@contextmanager
def registered_action_context(action_id: str | None, flow_id: str | None = None, session_id: str | None = None) -> Generator[None]:
    """Bind server-owned action identity only while entering registered code."""
    context = RegisteredOperationContext(action_id, flow_id, session_id) if action_id else None
    token = _REGISTERED_ACTION.set(context)
    try:
        yield
    finally:
        _REGISTERED_ACTION.reset(token)


@contextmanager
def registered_job_record(record: dict[str, object]) -> Generator[RegisteredOperationContext | None]:
    """Serialize lineage stamping with its existing atomic job/request commit."""
    owner = _REGISTERED_ACTION.get()
    if owner is None:
        yield None
        return
    token = record.get("flow_token_id")
    require_registered_job_flow_token(token if isinstance(token, str) else None, str(record.get("provider_name")))
    with owner.lock:
        owner.stamp(record)
        yield owner


def registered_job_owner_key(job: dict[str, object]) -> str:
    """Only server-owned admission groups join; ordinary jobs own themselves."""
    group = job.get("group_id")
    return group if isinstance(group, str) and group.startswith("registered:") else str(job["id"])


def registered_job_owner_filters(job: dict[str, object]) -> dict[str, object]:
    key = registered_job_owner_key(job)
    if key == job["id"]:
        return {"id": key}
    return {"group_id": key, "flow_token_id": job["flow_token_id"]}


class RegisteredOperationOwner:
    """One local serial owner; persistent requests/outcomes use the existing ledger."""

    def __init__(self, manager: AsyncJobManager, owner: str) -> None:
        self.manager = manager
        self.owner = owner
        self._lock = threading.Lock()
        self._pending: deque[tuple[str | None, Callable[[], None], Context]] = deque()
        self._thread: threading.Thread | None = None

    def submit(self, action_name: str, parameters: dict[str, object], operation: OwnedOperation) -> dict[str, object]:
        """Commit job+request before making the effect eligible to run."""
        receipt = self.manager.create_job(self.owner, action_name, {"notes": f"{self.owner}.{action_name}", "parameters": parameters})
        return self.accept_committed(receipt, operation)

    def accept_committed(self, receipt: dict[str, object], operation: OwnedOperation) -> dict[str, object]:
        """Hand off an existing manager's atomic admission without a second job."""
        if receipt.get("action_status") != "completed":
            return receipt
        data = receipt.get("data")
        job_id = data.get("job_id") if isinstance(data, dict) else None
        if not isinstance(data, dict) or not isinstance(job_id, str) or not job_id:
            raise RuntimeError("Owned admission returned no canonical job id")
        try:
            self._enqueue(job_id, lambda: self._run(job_id, operation))
        except Exception as exc:
            saved = self.manager.update_job(job_id, _owned_outcome_updates(_owned_error(exc)))
            saved_data = saved.get("data")
            if isinstance(saved_data, dict) and saved_data.get("outcome_committed"):
                data["status"] = "error"
            else:
                logger.error("Owner worker for job %s could not start and its terminal outcome is not durable; reconciliation required: %r", job_id, saved)
        return {**receipt, "actions": [], "error": None, "timestamp": datetime.now(UTC).isoformat()}

    def call(self, parameters: dict[str, object], operation: OwnedOperation) -> dict[str, object]:
        """Keep an unconverted synchronous dependency in this same FIFO fence."""
        if threading.current_thread() is self._thread:
            return operation(parameters)
        result: Future[dict[str, object]] = Future()

        def invoke() -> None:
            try:
                result.set_result(operation(parameters))
            except BaseException as exc:  # Transport the synchronous caller's failure without stranding its Future.
                result.set_exception(exc)

        self._enqueue(None, invoke)
        return result.result()

    def _enqueue(self, job_id: str | None, operation: Callable[[], None]) -> None:
        with self._lock:
            self._pending.append((job_id, operation, copy_context()))
            if self._thread is None:
                try:
                    self._start_worker()
                except Exception:
                    self._pending.pop()
                    raise

    def _start_worker(self) -> None:
        """Start only under the owner lock, including replacement after loss."""
        self._thread = threading.Thread(target=self._drain, name=f"registered-{self.owner}", daemon=True)
        try:
            self._thread.start()
        except Exception:
            self._thread = None
            raise

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return
                job_id, operation, context = self._pending.popleft()
            try:
                context.run(operation)
            except Exception:
                # An uncertain terminal write never authorizes replay of effects.
                logger.exception("Registered owner job %s needs reconciliation", job_id)
            except BaseException as exc:
                # The request was popped before effects ran. Never re-enqueue it.
                try:
                    self._record_worker_loss(job_id, exc)
                finally:
                    self._replace_lost_worker()
                return

    def _record_worker_loss(self, job_id: str | None, error: BaseException) -> None:
        """Persist uncertainty before a successor worker may enter another body."""
        if job_id is None:
            logger.error("Registered synchronous owner %s lost its worker; reconciliation required", self.owner)
            return
        outcome = _owned_error(
            FrameworkError(
                message="Owner worker terminated before reporting its outcome; effects are uncertain. Inspect the effects and never replay this request automatically.",
                error_code="registered_owner.worker_lost",
                details={"owner": self.owner, "job_id": job_id, "uncertain": True, "exception_type": type(error).__name__},
            )
        )
        saved = self.manager.update_job(job_id, {**_owned_outcome_updates(outcome), "status_reason": "owner_worker_lost_uncertain"})
        if saved.get("action_status") != "completed":
            logger.error("Lost owner job %s needs reconciliation; its uncertain error write or completion route failed: %r", job_id, saved)

    def _replace_lost_worker(self) -> None:
        """Release the dead slot; a replacement drains only untouched requests."""
        with self._lock:
            self._thread = None
            if self._pending:
                try:
                    self._start_worker()
                except Exception:
                    # Retain the FIFO. A later admission retries worker startup,
                    # never the lost request or an already-entered operation.
                    logger.exception("Registered owner %s replacement could not start; queued requests retained", self.owner)

    def _run(self, job_id: str, operation: OwnedOperation) -> None:
        marked = self.manager.update_job(job_id, {"status": "processing"})
        if marked.get("action_status") != "completed":
            raise RuntimeError(f"Could not claim owned job {job_id}: {marked!r}")
        try:
            request = self.manager.get_job_payload(job_id)
            data = request.get("data")
            payload = data.get("payload") if isinstance(data, dict) else None
            parameters = payload.get("parameters") if isinstance(payload, dict) else None
            if request.get("action_status") != "completed" or not isinstance(parameters, dict):
                raise RuntimeError(f"Owned job {job_id} request is unreadable")
            # The captured admission lineage marks explicit children without
            # giving them a second original-action completion callback.
            outcome = operation(parameters)
        except Exception as exc:
            outcome = _owned_error(exc)
        committed = self.manager.update_job(job_id, _owned_outcome_updates(outcome))
        if committed.get("action_status") != "completed":
            raise RuntimeError(f"Owned job {job_id} outcome needs reconciliation: {committed!r}")
