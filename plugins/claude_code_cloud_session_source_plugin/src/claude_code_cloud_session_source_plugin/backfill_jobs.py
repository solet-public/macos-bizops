"""Local cooperative single-flight jobs, not a database fence or replay queue.

The persistent inode and the SAME open descriptor span admission, execution,
payload commits and completion routing. Never unlink or rotate the lock file.
The sibling cloud plugin deliberately uses the identical path and protocol.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from ananta.core.state.async_job_manager import AsyncJobManager

logger = logging.getLogger(__name__)
PROVIDERS = (
    "codex_cloud_session_source_plugin.backfill_from_cloud",
    "claude_code_cloud_session_source_plugin.backfill_from_cloud",
)
RECONCILE_INTERVAL_SECONDS = 5.0
STOP_JOIN_SECONDS = 5.0


class PublicationError(RuntimeError):
    """A synchronous job operation failed before a known durable outcome."""


def _data(result: Mapping[str, object]) -> dict[str, object]:
    value = result.get("data")
    return dict(value) if isinstance(value, Mapping) else {}


def envelope(data: dict[str, object]) -> dict[str, object]:
    return {"action_status": "completed", "data": data, "actions": [], "error": None, "timestamp": datetime.now(UTC).isoformat()}


def _require_success(result: dict[str, object]) -> dict[str, object]:
    if result.get("action_status") != "completed":
        raise PublicationError(str(result.get("error", "job operation failed")))
    return _data(result)


class HeldBackfillLock:
    """A process-local guard around a kernel-held descriptor and one job binding."""

    def __init__(self, descriptor: int, path: Path) -> None:
        self.descriptor = descriptor
        self.path = path
        self.token = uuid.uuid4().hex
        self._released = False
        self._job_id: str | None = None
        self._mutex = threading.RLock()

    def require(self, job_id: str | None, token: str) -> None:
        if self._released or token != self.token or job_id != self._job_id:
            raise RuntimeError("released or mismatched cloud backfill handle")

    def bind(self, job_id: str) -> None:
        with self._mutex:
            self.require(self._job_id, self.token)
            if not job_id.strip():
                raise PublicationError("job operation returned no canonical job_id")
            self._job_id = job_id

    def create(self, manager: AsyncJobManager, plugin_name: str, parameters: dict[str, object], context: dict[str, object]) -> str:
        with self._mutex:
            self.require(None, self.token)
            result = manager.create_job(
                plugin_name,
                "backfill_from_cloud",
                request_data={"notes": "Cloud session backfill; retry interrupted work with a fresh force=True request", "parameters": parameters, "generation_token": self.token},
                job_metadata={key: context[key] for key in ("session_id", "flow_id") if key in context},
            )
            job_id = _require_success(result).get("job_id")
            if not isinstance(job_id, str) or not job_id.strip():
                raise PublicationError("create_job returned no canonical job_id")
            self.bind(job_id)
            return job_id

    def update(self, manager: AsyncJobManager, job_id: str, token: str, updates: dict[str, object]) -> None:
        with self._mutex:
            self.require(job_id, token)
            result = manager.update_job(job_id, updates)
            if result.get("action_status") == "completed":
                return
            if _data(result).get("outcome_committed") is True:
                logger.warning("cloud job %s outcome committed; completion delivery advisory: %s", job_id, result.get("error"))
                return
            raise PublicationError(str(result.get("error", "job publication failed")))

    def submit(self, thread: threading.Thread, job_id: str, token: str) -> None:
        with self._mutex:
            self.require(job_id, token)
            thread.start()

    def release(self) -> None:
        with self._mutex:
            if self._released:
                return
            self._released = True
            # close drops flock only after every synchronous writer has settled.
            os.close(self.descriptor)


def try_lock(path: Path) -> HeldBackfillLock | None:
    """Independent opens contend even inside one process on supported POSIX."""
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.set_inheritable(descriptor, False)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    except BaseException:
        os.close(descriptor)
        raise
    return HeldBackfillLock(descriptor, path)


def reconcile(manager: AsyncJobManager, held: HeldBackfillLock) -> int:
    """Four bounded reads/writes; retain requests/effects and never replay them."""
    count = 0
    for provider in PROVIDERS:
        for status in ("queued", "processing"):
            held.require(held._job_id, held.token)
            jobs = _require_success(manager.list_jobs(provider_name=provider, status=status, limit=1)).get("jobs")
            if not isinstance(jobs, list) or not jobs:
                continue
            job = jobs[0]
            if not isinstance(job, Mapping) or job.get("provider_name") != provider:
                raise PublicationError("unexpected cloud job in bounded reconciliation")
            job_id = job.get("id")
            if not isinstance(job_id, str):
                raise PublicationError("interrupted cloud job has no canonical id")
            held.bind(job_id)
            held.update(manager, job_id, held.token, {"status": "error", "error": {"code": "worker_interrupted", "message": "Worker interrupted; request and ingest effects retained. Submit a fresh force=True backfill; no automatic resume."}})
            count += 1
    if count:
        logger.warning("recorded %d interrupted cloud jobs; any older backlog drains on subsequent 5-second ticks", count)
    return count


def status(manager: AsyncJobManager, plugin_name: str, job_id: str) -> dict[str, object]:
    job = _require_success(manager.get_job(job_id)).get("job")
    if not isinstance(job, Mapping) or job.get("provider_name") != f"{plugin_name}.backfill_from_cloud":
        raise ValueError("job_id is not a backfill for this provider")
    raw_status = job.get("status")
    data: dict[str, object] = {"job_id": job_id, "status": raw_status, "execution_measured": False, "advisory": "Stored status only; execution ownership is not measured by this reader."}
    if raw_status in {"completed", "error"}:
        kind = "result" if raw_status == "completed" else "error"
        data[kind] = _require_success(manager.get_job_payload(job_id, kind)).get("payload")
    return envelope(data)


class BackfillRuntime:
    """No job queue: a claimant transfers its held descriptor to one worker."""

    def __init__(self, plugin_name: str, manager: Callable[[], AsyncJobManager], lock_path: Callable[[], Path], run: Callable[[dict[str, object], Callable[[], bool], Callable[[dict[str, object]], None]], dict[str, object]]) -> None:
        self.plugin_name = plugin_name
        self._manager = manager
        self._lock_path = lock_path
        self._run = run
        self._control = threading.RLock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._tick: threading.Thread | None = None

    def start(self) -> bool:
        with self._control:
            if self._stop.is_set() and self._worker is not None and self._worker.is_alive():
                return False
            self._stop.clear()
            if self._tick is None or not self._tick.is_alive():
                self._tick = threading.Thread(target=self._reconciliation_loop, name=f"{self.plugin_name}-backfill-reconciliation", daemon=False)
                self._tick.start()
            return True

    def stop(self) -> bool:
        with self._control:
            self._stop.set()
            threads = (self._worker, self._tick)
        for thread in threads:
            if thread is not None:
                thread.join(timeout=STOP_JOIN_SECONDS)
        return all(thread is None or not thread.is_alive() for thread in threads)

    def dispatch(self, parameters: dict[str, object], context: dict[str, object]) -> dict[str, object]:
        with self._control:
            if self._stop.is_set():
                return envelope({"status": "stopping", "accepted": False})
            held = try_lock(self._lock_path())
            if held is None:
                return envelope({"status": "busy", "accepted": False})
            transferred = False
            try:
                manager = self._manager()
                reconcile(manager, held)
                # Reconciliation can bind old rows. A new request has its own binding.
                held._job_id = None
                job_id = held.create(manager, self.plugin_name, parameters, context)
                thread = threading.Thread(target=self._execute, args=(manager, held, job_id, parameters), name=f"{self.plugin_name}-backfill-worker", daemon=False)
                try:
                    held.submit(thread, job_id, held.token)
                except Exception:
                    held.update(manager, job_id, held.token, {"status": "error", "error": {"code": "submission_failed", "message": "Worker submission failed; submit a fresh force=True request."}})
                    raise
                self._worker = thread
                transferred = True
                return envelope({"job_id": job_id, "status": "queued", "accepted": True})
            finally:
                if not transferred:
                    held.release()

    def _execute(self, manager: AsyncJobManager, held: HeldBackfillLock, job_id: str, parameters: dict[str, object]) -> None:
        def progress(snapshot: dict[str, object]) -> None:
            held.update(manager, job_id, held.token, {"result": snapshot})

        try:
            held.update(manager, job_id, held.token, {"status": "processing"})
            outcome = self._product_outcome(parameters, progress)
            held.update(manager, job_id, held.token, outcome)
        except PublicationError:
            # Unknown commit outcome: no further effects/writes; tick reads reality.
            logger.exception("cloud job %s publication failed; request retained for reconciliation", job_id)
        finally:
            held.release()

    def _product_outcome(self, parameters: dict[str, object], progress: Callable[[dict[str, object]], None]) -> dict[str, object]:
        try:
            result = self._run(parameters, self._stop.is_set, progress)
            if self._stop.is_set():
                return {"status": "error", "error": {"code": "worker_interrupted", "message": "Service stopped between requests; effects retained. Submit a fresh force=True request."}, "result": result}
            return {"status": "completed", "result": result}
        except PublicationError:
            raise
        except Exception as exc:  # noqa: BLE001 — durable product failure
            return {"status": "error", "error": {"code": str(getattr(exc, "code", "backfill_failed")), "message": str(exc)}}

    def poll_once(self) -> int:
        held = try_lock(self._lock_path())
        if held is None:
            return 0
        try:
            return reconcile(self._manager(), held)
        finally:
            held.release()

    def _reconciliation_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 — keep reconciliation scheduled
                logger.exception("cloud backfill reconciliation failed")
            self._stop.wait(RECONCILE_INTERVAL_SECONDS)
