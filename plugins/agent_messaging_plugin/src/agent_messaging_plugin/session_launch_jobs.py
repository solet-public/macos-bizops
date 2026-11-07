"""Bounded submission adapters for the session-launch family only."""

import logging
import os
import secrets
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from ananta.core.state.async_job_manager import AsyncJobManager
from ananta.interfaces.state_service_protocol import StateServiceProtocol

from .constants import PLUGIN_NAME
from .session_launch_store import (
    LaunchBusyError,
    bind_launch_job,
    owner_alive,
    recorded_owner_pid,
    release_launch,
    reserve_launch,
)
from .session_lifecycle_verbs import VerbError

logger = logging.getLogger(__name__)
LaunchOperation = Callable[[], dict[str, object]]
RestartOperation = Callable[[dict[str, object]], dict[str, object]]
TERMINAL_JOB_STATUSES = frozenset({"completed", "error", "cancelled"})


def _require_success(result: dict[str, object]) -> dict[str, object]:
    data = result.get("data")
    if result.get("action_status") != "completed" or not isinstance(data, dict):
        raise RuntimeError(f"Launch job persistence failed: {result!r}")
    return data


def _terminal_outcome(result: dict[str, object]) -> dict[str, object]:
    failed = result.get("action_status") in {"error", "failed"} or result.get("success") is False
    updates: dict[str, object] = {"status": "error" if failed else "completed", "result": result}
    if failed:
        error = result.get("error")
        updates["error"] = error if isinstance(error, dict) else {"message": str(error)}
    return updates


def finish_launch(
    manager: AsyncJobManager,
    state: StateServiceProtocol,
    operation_id: str,
    job_id: str,
    updates: dict[str, object],
) -> None:
    """Release only after observing durable terminal data, even if push failed."""
    updated = manager.update_job(job_id, updates)
    data = updated.get("data")
    if not isinstance(data, dict) or data.get("outcome_committed") is not True:
        raise RuntimeError(f"Launch {operation_id} retained: terminal outcome not committed: {updated!r}")
    release_launch(state, operation_id, os.getpid())
    if updated.get("action_status") != "completed":
        logger.error("Launch %s terminal outcome persisted; completion delivery failed: %r", operation_id, updated)


def owner_is_gone(manager: AsyncJobManager, owner: dict[str, object], alive: bool | None) -> str:
    """The release reason when the owner provably cannot finish, else "".

    Gone means its bound job is terminal, or its recorded process no longer
    runs (``alive`` is False). An owner with no recorded process and a live job
    stays unproven. The caller reads liveness once and passes it in.
    """
    job_id = owner.get("job_id")
    if isinstance(job_id, str) and job_id:
        data = manager.get_job(job_id).get("data")
        job = data.get("job") if isinstance(data, dict) else None
        if isinstance(job, dict) and job.get("status") in TERMINAL_JOB_STATUSES:
            return "terminal_job"
    return "dead_owner" if alive is False else ""


def reserve_reconciling(
    manager: AsyncJobManager,
    state: StateServiceProtocol,
    operation_id: str,
    job_id: str = "",
) -> None:
    """Reserve, releasing a provably gone owner once; a second conflict raises LaunchBusyError."""
    try:
        reserve_launch(state, operation_id, job_id)
        return
    except LaunchBusyError as exc:
        owner = exc.owner
        alive = owner_alive(owner)
        reason = owner_is_gone(manager, owner, alive)
        if not reason:
            if alive is None:
                logger.warning(
                    "Launch reservation kept: operation=%s job=%s reason=owner_unproven",
                    owner.get("operation_id"),
                    owner.get("job_id"),
                )
            raise
    release_launch(state, str(owner.get("operation_id")), recorded_owner_pid(owner))
    logger.warning(
        "Launch reservation released: operation=%s job=%s reason=%s",
        owner.get("operation_id"),
        owner.get("job_id"),
        reason,
    )
    reserve_launch(state, operation_id, job_id)


def _reserve_restart(
    manager: AsyncJobManager,
    state: StateServiceProtocol,
    operation_id: str,
    job_id: str,
) -> bool:
    try:
        reserve_reconciling(manager, state, operation_id, job_id)
    except LaunchBusyError as exc:
        # A rejected selector owns no outcome: another color may admit this
        # job as soon as the observed owner releases the slot.
        logger.debug("Restart %s admission deferred; observed owner=%r", job_id, exc.owner)
        return False
    job = _require_success(manager.get_job(job_id)).get("job")
    if not isinstance(job, dict):
        raise RuntimeError("Restart job is unreadable; reservation retained")
    if job.get("status") in TERMINAL_JOB_STATUSES:
        # A second color may have selected queued work before its owner finished.
        release_launch(state, operation_id, os.getpid())
        return False
    return True


def _restart_request(manager: AsyncJobManager, job_id: str) -> dict[str, object]:
    payload = manager.get_job_payload(job_id, "request")
    if payload.get("action_status") != "completed":
        raise VerbError("request_payload_missing", "could not read the job's own request payload")
    data = payload.get("data")
    request = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(request, dict):
        raise VerbError("request_payload_invalid", "job request payload was not an object")
    return request


def run_reserved_restart(
    manager: AsyncJobManager,
    state: StateServiceProtocol,
    job_id: str,
    operation: RestartOperation,
) -> None:
    """Own request reads, effects, and outcomes under the existing job's reservation."""
    if not job_id:
        raise ValueError("Restart job id is required before reservation admission")
    operation_id = f"restart:{job_id}"
    if not _reserve_restart(manager, state, operation_id, job_id):
        return
    try:
        try:
            _require_success(manager.update_job(job_id, {"status": "processing"}))
            updates: dict[str, object] = {"status": "completed", "result": operation(_restart_request(manager, job_id))}
        except VerbError as exc:
            updates = {"status": "error", "error": {"code": exc.code, "message": exc.message}}
        except Exception as exc:
            updates = {"status": "error", "error": {"code": "internal_error", "message": str(exc)}}
        finish_launch(manager, state, operation_id, job_id, updates)
    except Exception:
        # Do not overwrite an unpersisted success with an error or admit replay.
        logger.exception("Restart %s needs reconciliation; reservation retained", job_id)


class SessionLaunchJobs:
    """One running operation, zero pending backlog, no stale-job replay."""

    def __init__(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-launch")
        self._admission = threading.BoundedSemaphore(1)
        self._control = threading.Lock()
        self._active_owner: dict[str, object] = {}
        self._stopped = threading.Event()
        self.future: Future[None] | None = None

    def start(self) -> None:
        with self._control:
            if self._stopped.is_set():
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-launch")
                self._stopped.clear()

    def stop(self) -> None:
        """Stop admission. An outstanding worker keeps its durable ownership."""
        with self._control:
            self._stopped.set()
            if self._active_owner:
                logger.warning("Launch worker stopping with outstanding ownership: %r", self._active_owner)
            self._executor.shutdown(wait=False, cancel_futures=False)

    def submit(
        self,
        *,
        manager: AsyncJobManager,
        state: StateServiceProtocol,
        action_name: str,
        parameters: dict[str, object],
        context: dict[str, object],
        operation: LaunchOperation,
    ) -> dict[str, object]:
        """Validate context, reserve, persist, hand off, and return the canonical id."""
        for name in ("session_id", "flow_id"):
            if not isinstance(context.get(name), str) or not str(context[name]).strip():
                raise ValueError(f"Launch submission requires server-injected {name}")
        operation_id = f"launch-{secrets.token_hex(16)}"
        with self._control:
            if self._stopped.is_set():
                raise RuntimeError("Session launch admission is stopped")
            if not self._admission.acquire(blocking=False):
                raise LaunchBusyError(dict(self._active_owner))
            self._active_owner = {"operation_id": operation_id, "job_id": ""}
        try:
            reserve_reconciling(manager, state, operation_id)
        except Exception:
            self._release_local()
            raise
        return self._submit_reserved(manager, state, operation_id, action_name, parameters, context, operation)

    def _release_local(self) -> None:
        with self._control:
            self._active_owner = {}
            self._admission.release()

    def _submit_reserved(
        self,
        manager: AsyncJobManager,
        state: StateServiceProtocol,
        operation_id: str,
        action_name: str,
        parameters: dict[str, object],
        context: dict[str, object],
        operation: LaunchOperation,
    ) -> dict[str, object]:
        job_id = ""
        try:
            receipt = manager.create_job(
                PLUGIN_NAME,
                action_name,
                request_data={"notes": f"Session launch: {action_name}", "parameters": parameters, "context": context, "operation_id": operation_id},
                job_metadata={**context, "operation_id": operation_id},
            )
            data = _require_success(receipt)
            value = data.get("job_id")
            if not isinstance(value, str) or not value:
                raise RuntimeError("Launch job creation returned no canonical job id")
            job_id = value
            bind_launch_job(state, operation_id, job_id)
            with self._control:
                self._active_owner["job_id"] = job_id
                if self._stopped.is_set():
                    raise RuntimeError("Session launch admission stopped before worker submission")
                self.future = self._executor.submit(self._run, manager, state, operation_id, job_id, operation)
            return receipt
        except Exception as exc:
            try:
                if job_id:
                    finish_launch(
                        manager,
                        state,
                        operation_id,
                        job_id,
                        {
                            "status": "error",
                            "error": {"code": "launch_admission_failed", "message": str(exc)},
                        },
                    )
                else:
                    release_launch(state, operation_id, os.getpid())
            finally:
                self._release_local()
            raise

    def _run(
        self,
        manager: AsyncJobManager,
        state: StateServiceProtocol,
        operation_id: str,
        job_id: str,
        operation: LaunchOperation,
    ) -> None:
        try:
            try:
                _require_success(manager.update_job(job_id, {"status": "processing"}))
                updates: dict[str, object] = _terminal_outcome(operation())
            except Exception as exc:
                updates = {
                    "status": "error",
                    "error": {
                        "code": "launch_operation_failed",
                        "message": str(exc),
                    },
                }
            finish_launch(manager, state, operation_id, job_id, updates)
        except Exception:
            # The durable reservation survives this process and blocks replay.
            logger.exception("Launch %s needs reconciliation; reservation retained", operation_id)
        finally:
            self._release_local()
