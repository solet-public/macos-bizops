from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING

from ananta.constants import (
    FRAMEWORK_ASYNC_JOBS_TABLE,
    FRAMEWORK_NAMESPACE,
    NOTES_MAX_LENGTH,
)
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.core.domain.types import ActionResult
from ananta.core.state.async_job_persistence import create_job_and_request, update_job_and_payloads
from ananta.core.state.execution_token_context import get_current_flow_token_id
from ananta.core.state.job_completion_reach import record_completion_reach
from ananta.core.state.job_completion_route import (
    JobWaiterLocks,
    bridge_wait_attached,
    build_completion_action,
    build_completion_arguments,
    clear_bridge_waiter,
    enrich_prompt_block,
    populate_state_args,
    route_completion_to_role,
    stamp_bridge_waiter,
)
from ananta.core.state.registered_job_operations import (
    REGISTERED_JOB_RETURN_SCHEMA as REGISTERED_JOB_RETURN_SCHEMA,
)
from ananta.core.state.registered_job_operations import (
    OwnedOperation as OwnedOperation,
)
from ananta.core.state.registered_job_operations import (
    RegisteredCompletion as RegisteredCompletion,
)
from ananta.core.state.registered_job_operations import (
    RegisteredOperationOwner,
    registered_job_owner_filters,
    registered_job_owner_key,
    registered_job_record,
    require_registered_job_flow_token,
)
from ananta.core.state.registered_job_operations import (
    registered_action_context as registered_action_context,
)
from ananta.core.state.registered_job_operations import (
    registered_job_return_schema as registered_job_return_schema,
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol

if TYPE_CHECKING:
    from ananta.core.actions.action_factory import ActionFactory
    from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph

logger = logging.getLogger(__name__)


def _optional_str(source: dict[str, object], key: str) -> str | None:
    """A non-empty string field, or None — "" and a wrong type read the same."""
    value = source.get(key)
    return value if isinstance(value, str) and value else None


class AsyncJobManager:
    def __init__(
        self,
        state_service: StateServiceProtocol,
        flow_runtime_graph: FlowRuntimeGraph | None = None,
    ) -> None:
        self.state_service = state_service
        self._namespace = FRAMEWORK_NAMESPACE
        self._table = FRAMEWORK_ASYNC_JOBS_TABLE
        # FlowRuntimeGraph is set after initialization via ServiceManager
        # (due to initialization order - AsyncJobManager created before FRG)
        self._flow_runtime_graph: FlowRuntimeGraph | None = flow_runtime_graph
        self._action_factory: ActionFactory | None = None
        self._registered_completion: RegisteredCompletion | None = None
        self._owners_lock = threading.Lock()
        self._owners: dict[str, RegisteredOperationOwner] = {}
        self._completion_lock = threading.RLock()
        self._waiter_locks = JobWaiterLocks()
        self._routing_owners: dict[str, int] = {}
        self._pending_token_resolutions: dict[str, str] = {}

    def set_registered_completion_handler(self, handler: RegisteredCompletion) -> None:
        """Wire the existing poller route before registered admission starts."""
        self._registered_completion = handler

    def submit_registered_operation(self, owner: str, action_name: str, parameters: dict[str, object], operation: OwnedOperation) -> dict[str, object]:
        """Admit an explicit operation without changing its internal callable."""
        return self._operation_owner(owner).submit(action_name, parameters, operation)

    def run_registered_serial_body(self, owner: str, parameters: dict[str, object], operation: OwnedOperation) -> dict[str, object]:
        """Preserve an existing synchronous fence without creating another job."""
        return self._operation_owner(owner).call(parameters, operation)

    def _operation_owner(self, owner: str) -> RegisteredOperationOwner:
        with self._owners_lock:
            return self._owners.setdefault(owner, RegisteredOperationOwner(self, owner))

    def create_job(
        self,
        plugin_name: str,
        action_name: str,
        request_data: dict[str, object] | None = None,
        job_id: str | None = None,
        name: str | None = None,
        description: str | None = None,
        flow_id_trace: str | None = None,
        job_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Create a new job in the unified job ledger with optional payload.

        Args:
            plugin_name: Name of the plugin creating the job
            action_name: Name of the action being executed
            request_data: Optional request payload data
            job_id: Optional external job identifier (maps to external_id column)
            name: Optional user-friendly job name (defaults to ID if not provided)
            description: Optional job description (defaults to auto-generated)
            flow_id_trace: Optional flow ID for tracing

        Returns:
            Result dict with job_id and status
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        request_payload = request_data or {}
        notes_error = self._validate_notes(request_payload)
        if notes_error:
            return notes_error

        provider_name = f"{plugin_name}.{action_name}"
        job_record = self._build_job_record(provider_name, request_payload, job_id, name, description, flow_id_trace, job_metadata)

        try:
            return self._create_job_with_payload(provider_name, job_record, request_payload)
        except Exception as e:
            logger.error("Error creating async job: %s", e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception creating job: {e!s}"},
            }

    def _validate_notes(self, request_payload: dict[str, object]) -> dict[str, object] | None:
        """Validate notes field in request payload. Returns error dict or None."""
        notes_value = ""
        notes_candidate = request_payload.get("notes")
        if isinstance(notes_candidate, str):
            notes_value = notes_candidate.strip()

        if not notes_value:
            return {
                "action_status": "error",
                "error": {"message": "AsyncJobManager.create_job requires a non-empty notes field"},
            }

        if len(notes_value) > NOTES_MAX_LENGTH:
            return {
                "action_status": "error",
                "error": {
                    "message": f"notes exceeds maximum length of {NOTES_MAX_LENGTH} characters",
                    "details": {"notes_length": len(notes_value)},
                },
            }

        # Normalize notes value in payload
        request_payload["notes"] = notes_value

        return None

    def _build_job_record(
        self,
        provider_name: str,
        request_payload: dict[str, object],
        job_id: str | None,
        name: str | None,
        description: str | None,
        flow_id_trace: str | None,
        job_metadata: dict[str, object] | None = None,
    ) -> dict[str, object]:
        """Build job record for insertion."""
        notes_value = request_payload.get("notes", "")
        job_record: dict[str, object] = {
            "provider_type": "plugin",
            "provider_name": provider_name,
            "description": description or f"Async job for {provider_name}",
            "status": "queued",
            "progress_percent": 0,
            "notes": notes_value,
        }

        if job_id:
            job_record["external_id"] = job_id
        if name:
            job_record["name"] = name
        if flow_id_trace:
            job_record["flow_id_trace"] = flow_id_trace

        # Link to FRG token from execution context
        flow_token_id = get_current_flow_token_id()
        require_registered_job_flow_token(flow_token_id, provider_name)
        if flow_token_id:
            job_record["flow_token_id"] = flow_token_id

        metadata = dict(job_metadata or {})
        if metadata:
            try:
                job_record["metadata"] = json.dumps(metadata)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"job_metadata must be JSON serializable: {exc}") from exc

        return job_record

    def _create_job_with_payload(
        self,
        provider_name: str,
        job_record: dict[str, object],
        request_payload: dict[str, object],
    ) -> dict[str, object]:
        """Create job record and write payload. Assumes state_service is available."""
        assert self.state_service is not None  # Caller must verify

        with registered_job_record(job_record) as owner:
            stamp_bridge_waiter(self.state_service, self._namespace, job_record)
            generated_id = create_job_and_request(self.state_service, job_record, request_payload)
            if owner is not None and owner.root_job_id is None:
                owner.root_job_id = generated_id
        logger.debug("Created async job %s for %s", generated_id, provider_name)
        return {
            "action_status": "completed",
            "data": {"job_id": generated_id, "status": "queued"},
        }

    def get_job(self, job_id: str) -> dict[str, object]:
        """Get a job by ID.

        Returns:
            Result dict with job record in data.job on success.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}

            data = result.get("data")
            if not isinstance(data, dict):
                return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}

            # State service returns records directly in data, not nested under result
            records = data.get("records")
            if not isinstance(records, list) or not records:
                return {"action_status": "error", "error": {"message": f"Job not found: {job_id}"}}

            job = records[0]
            if not isinstance(job, dict):
                return {
                    "action_status": "error",
                    "error": {"message": f"Invalid job record: {job_id}"},
                }

            return {"action_status": "completed", "data": {"job": job}}

        except Exception as e:
            logger.error("Error getting async job %s: %s", job_id, e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception getting job: {e!s}"},
            }

    def get_job_payload(self, job_id: str, payload_type: str = "request") -> dict[str, object]:
        """Get payload data for a job.

        Args:
            job_id: The job ID to fetch payload for.
            payload_type: Type of payload to fetch ('request', 'result', or 'error').

        Returns:
            Result dict with payload data in data.payload on success.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": "job_payload",
                    "filters": {"job_id": job_id, "payload_type": payload_type},
                    "order_by": "sequence DESC",
                    "limit": 1,
                },
            )

            payload_record = self._extract_first_record(result)
            if payload_record is None:
                return {
                    "action_status": "error",
                    "error": {"message": f"No {payload_type} payload found for job {job_id}"},
                }

            payload_data = self._parse_payload_data(payload_record, job_id)
            return {"action_status": "completed", "data": {"payload": payload_data}}

        except Exception as e:
            logger.error("Error getting payload for job %s: %s", job_id, e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception getting payload: {e!s}"},
            }

    def _extract_first_record(self, result: ActionResult) -> dict[str, object] | None:
        """Extract first record from state service result, or None if not found."""
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return None

        data = result.get("data")
        if not isinstance(data, dict):
            return None

        # State service returns records directly in data, not nested under result
        records = data.get("records")
        if not isinstance(records, list) or not records:
            return None

        first_record = records[0]
        return first_record if isinstance(first_record, dict) else None

    def _parse_payload_data(self, payload_record: dict[str, object], job_id: str) -> dict[str, object]:
        """Parse payload_data field from a payload record."""
        payload_data_raw = payload_record.get("payload_data", "{}")

        if isinstance(payload_data_raw, str):
            try:
                parsed = json.loads(payload_data_raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.error("Invalid JSON in payload for job %s", job_id)
                return {}

        return payload_data_raw if isinstance(payload_data_raw, dict) else {}

    def _build_job_filters(
        self,
        plugin_name: str | None,
        action_name: str | None,
        status: str | None,
    ) -> dict[str, object]:
        """Build filters dict for job queries."""
        filters: dict[str, object] = {}

        if plugin_name and action_name:
            filters["provider_name"] = f"{plugin_name}.{action_name}"
        elif plugin_name or action_name:
            logger.error("Partial plugin/action filtering not supported. Provide both plugin_name and action_name, or use status filter only.")

        if status:
            filters["status"] = status

        return filters

    def list_jobs(
        self,
        status: str | None = None,
        provider_name: str | None = None,
        limit: int = 10,
        order_by: str = "created_at DESC",
    ) -> dict[str, object]:
        """List jobs matching the specified filters.

        Args:
            status: Filter by job status (e.g., 'queued', 'processing', 'completed').
            provider_name: Filter by provider name (e.g., 'plugin_name.action_name').
            limit: Maximum number of jobs to return.
            order_by: Sort order (default: 'created_at DESC').

        Returns:
            Result dict with list of jobs in data.jobs on success.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        filters: dict[str, object] = {}
        if status:
            filters["status"] = status
        if provider_name:
            filters["provider_name"] = provider_name

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": self._table,
                    "filters": filters,
                    "order_by": order_by,
                    "limit": limit,
                },
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                return {
                    "action_status": "error",
                    "error": {"message": "Failed to query jobs", "details": result},
                }

            data = result.get("data")
            if not isinstance(data, dict):
                return {"action_status": "completed", "data": {"jobs": []}}

            # State service returns records directly in data, not nested under result
            records = data.get("records")
            if not isinstance(records, list):
                return {"action_status": "completed", "data": {"jobs": []}}

            # Filter to valid job dicts only
            jobs = [r for r in records if isinstance(r, dict)]
            return {"action_status": "completed", "data": {"jobs": jobs}}

        except Exception as e:
            logger.error("Error listing jobs: %s", e)
            return {
                "action_status": "error",
                "error": {"message": f"Exception listing jobs: {e!s}"},
            }

    def update_job(self, job_id: str, updates: dict[str, object]) -> dict[str, object]:
        """Serialize this job's outcome route with release, preserving sibling writes."""
        with self._waiter_locks.for_job(job_id):
            return self._update_job(job_id, updates)

    def release_bridge_waiter(self, job_id: str, *, bridge_id: str, session_id: str) -> dict[str, object]:
        """Clear one owner binding; a suppressed terminal result now routes once."""
        with self._waiter_locks.for_job(job_id):
            changed, job = clear_bridge_waiter(self.state_service, self._namespace, job_id, bridge_id=bridge_id, session_id=session_id)
            status = str(job.get("status"))
            if changed and status in {"completed", "error"}:
                result = self._released_job_payload(job_id, "result")
                error = self._released_job_payload(job_id, "error")
                if not self._route_completion_to_role(job_id, status, result, error):
                    self._handle_completion_actions(job_id, status, result, error)
            return {"job_id": job_id, "released": changed, "bridge_wait_attached": False}

    def _released_job_payload(self, job_id: str, kind: str) -> dict[str, object] | None:
        result = self.get_job_payload(job_id, kind)
        data = result.get("data")
        payload = data.get("payload") if isinstance(data, dict) else None
        return payload if isinstance(payload, dict) else None

    def _update_job(self, job_id: str, updates: dict[str, object]) -> dict[str, object]:
        """Update job in unified ledger with support for payload fields."""
        outcome_committed = False
        routing_token: str | None = None
        try:
            if not self.state_service:
                return {
                    "action_status": "error",
                    "error": {"message": "State service not available"},
                }

            if updates.get("status") in {"completed", "error", "cancelled"}:
                routing_token = self._begin_terminal_route(job_id)
            ledger_updates, result_payload, error_payload = self._extract_update_fields(updates)

            update_job_and_payloads(
                self.state_service,
                job_id,
                ledger_updates,
                result_payload,
                error_payload,
            )
            new_status = updates.get("status")
            outcome_committed = new_status in ("completed", "error", "cancelled")
            if new_status in ("completed", "error", "cancelled"):
                # Stamped BEFORE the continuation is submitted, deliberately:
                # _handle_completion_actions can raise (a missing flow_id in
                # metadata is the concrete case), and a job whose continuation
                # never ran is exactly the one a reader most needs to find.
                #
                # Lane W: this pre-stamp is ALSO the push path's failure
                # fallback. It says "unreached" until the delivery verb
                # measures a successful hand-off and upgrades it, so a push
                # that never lands leaves the drain marker exactly where a
                # reader will find it. Losing both the push and the marker is
                # the one outcome that must be impossible.
                try:
                    record_completion_reach(
                        self.state_service,
                        self._namespace,
                        self._table,
                        job_id,
                        self._fetch_job_metadata(job_id),
                    )
                    if not self._route_completion_to_role(job_id, str(new_status), result_payload, error_payload):
                        self._handle_completion_actions(
                            job_id,
                            str(new_status),
                            result_payload,
                            error_payload,
                        )
                finally:
                    # Delivery failure stays drainable; it cannot strand the token.
                    self._end_terminal_route(routing_token)
                    routing_token = None
                    self._resolve_job_token(job_id, success=(new_status == "completed"))

            return {
                "action_status": "completed",
                "data": {
                    "job_id": job_id,
                    "updated": True,
                    "outcome_committed": outcome_committed,
                },
            }

        except Exception as e:
            logger.error(f"Error updating async job {job_id}: {e}")
            return {
                "action_status": "error",
                "data": {"job_id": job_id, "outcome_committed": outcome_committed},
                "error": {"message": f"Exception updating job: {str(e)}"},
            }
        finally:
            self._end_terminal_route(routing_token)

    def _begin_terminal_route(self, job_id: str) -> str | None:
        """Register routing before the outcome commit can be observed by a sibling."""
        job = self._job_for_token_resolution(job_id)
        owner_key = registered_job_owner_key(job) if job.get("flow_token_id") else None
        if owner_key:
            with self._completion_lock:
                self._routing_owners[owner_key] = self._routing_owners.get(owner_key, 0) + 1
        return owner_key

    def _end_terminal_route(self, owner_key: str | None) -> None:
        pending_job: str | None = None
        if owner_key:
            with self._completion_lock:
                count = self._routing_owners[owner_key] - 1
                if count:
                    self._routing_owners[owner_key] = count
                else:
                    del self._routing_owners[owner_key]
                    pending_job = self._pending_token_resolutions.pop(owner_key, None)
        if pending_job is not None:
            try:
                self._resolve_job_token(pending_job)
            except Exception:
                assert owner_key is not None
                with self._completion_lock:
                    self._pending_token_resolutions[owner_key] = pending_job
                logger.exception("Committed job %s still needs token reconciliation", pending_job)

    def _owner_jobs_exist(self, job: dict[str, object], statuses: list[str]) -> bool:
        result = self.state_service.query_state(self._namespace, {"table": self._table, "filters": {**registered_job_owner_filters(job), "status": statuses}, "limit": 1})
        data = result.get("data")
        records = data.get("records") if isinstance(data, dict) else None
        if result.get("action_status") != "completed" or not isinstance(records, list):
            raise RuntimeError(f"Cannot read linked job states for owner {job['id']}")
        return bool(records)

    def _extract_update_fields(self, updates: dict[str, object]) -> tuple[dict[str, object], dict[str, object] | None, dict[str, object] | None]:
        """Separate ledger updates from payload data."""
        ledger_updates: dict[str, object] = {}
        result_payload: dict[str, object] | None = None
        error_payload: dict[str, object] | None = None

        ledger_fields = ("status", "status_reason", "progress_percent", "completed_at")
        for field in ledger_fields:
            if field in updates:
                ledger_updates[field] = updates[field]

        if "result" in updates and isinstance(updates["result"], dict):
            result_payload = updates["result"]
        if "error" in updates and isinstance(updates["error"], dict):
            error_payload = updates["error"]

        return ledger_updates, result_payload, error_payload

    def set_action_factory(self, action_factory: ActionFactory) -> None:
        """Inject ActionFactory for automatic result routing."""
        self._action_factory = action_factory
        logger.debug("AsyncJobManager: ActionFactory reference injected")

    def _decode_metadata(self, metadata_value: object) -> dict[str, object]:
        if isinstance(metadata_value, dict):
            return metadata_value
        if isinstance(metadata_value, str) and metadata_value.strip():
            try:
                parsed = json.loads(metadata_value)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                logger.error("AsyncJobManager: Invalid metadata JSON; ignoring")
        return {}

    def _fetch_job_metadata(self, job_id: str) -> dict[str, object]:
        """Load job metadata dict from database."""
        result = self.state_service.read_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
        )
        record = self._extract_first_record(result)
        if record is None:
            return {}

        return self._decode_metadata(record.get("metadata"))

    def _fetch_job_provider_name(self, job_id: str) -> str:
        """The job's ``provider_name`` (``plugin.verb``), or "" when unreadable.

        Envelope provenance only — it names what produced the result in the
        delivered message. Never load-bearing for routing, so an unreadable row
        degrades to "" instead of suppressing a delivery.
        """
        result = self.state_service.read_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
        )
        record = self._extract_first_record(result)
        if record is None:
            return ""
        provider_name = record.get("provider_name")
        return provider_name if isinstance(provider_name, str) else ""

    def _get_completion_handler(self, job_id: str, status: str) -> tuple[dict[str, object], dict[str, object], str] | None:
        """Retrieve completion handler from job metadata.

        Returns tuple of (handler, metadata, handler_key) or None if not found.
        """
        metadata = self._fetch_job_metadata(job_id)
        if not metadata:
            return None

        handlers = metadata.get("completion_handlers")
        if not isinstance(handlers, dict):
            return None

        handler_key = "result" if status == "completed" else "error"
        handler = handlers.get(handler_key)
        if not isinstance(handler, dict):
            return None

        process_key = handler.get("process_key")
        if not isinstance(process_key, str) or not process_key:
            return None

        return handler, metadata, handler_key

    def _submit_completion_action(self, action_def: dict[str, object], handler_key: str, job_id: str, process_key: str) -> None:
        """Submit action definition to action factory."""
        try:
            assert self._action_factory is not None
            self._action_factory.submit_action_definition(action_def)
            logger.debug(
                "AsyncJobManager: Submitted %s handler for job %s via %s",
                handler_key,
                job_id,
                process_key,
            )
        except Exception as exc:
            logger.error(
                "AsyncJobManager: Failed to submit completion handler for job %s: %s",
                job_id,
                exc,
                exc_info=True,
            )
            raise

    def _route_completion_to_role(self, job_id: str, status: str, result_payload: dict[str, object] | None, error_payload: dict[str, object] | None) -> bool:
        """Consume attached-root delivery or replace its continuation with a role push."""
        factory = self._action_factory
        return route_completion_to_role(self.state_service, self._namespace, self._fetch_job_metadata(job_id), job_id=job_id, provider_name=self._fetch_job_provider_name(job_id) if factory else "", status=status, result=result_payload, error=error_payload, submit=factory.submit_action_definition if factory else None)

    def _route_registered_completion(self, job_id: str, status: str, result_payload: dict[str, object] | None, error_payload: dict[str, object] | None) -> bool:
        """Consume an attached root or route the registered original action."""
        metadata = self._fetch_job_metadata(job_id)
        if bridge_wait_attached(metadata):
            return True
        action_id = _optional_str(metadata, "registered_action_id")
        if action_id and self._registered_completion is not None and "completion_handlers" not in metadata:
            self._registered_completion(action_id, status, result_payload, error_payload)
            return True
        return False

    def _handle_completion_actions(
        self,
        job_id: str,
        status: str,
        result_payload: dict[str, object] | None,
        error_payload: dict[str, object] | None,
    ) -> None:
        """Submit configured completion or error actions when jobs finish."""
        if status not in {"completed", "error"}:
            return

        if self._route_registered_completion(job_id, status, result_payload, error_payload):
            return

        if not self._action_factory:
            return

        handler_data = self._get_completion_handler(job_id, status)
        if handler_data is None:
            return

        handler, metadata, handler_key = handler_data
        process_key = handler.get("process_key")
        assert isinstance(process_key, str)  # Validated in _get_completion_handler

        arguments = build_completion_arguments(handler, job_id, status, result_payload, error_payload)
        _, session_id, flow_id = populate_state_args(arguments, metadata, job_id)

        # Fail fast: flow_id is required for all actions
        if not flow_id:
            raise ValueError(f"Async job {job_id} completion handler missing flow_id in metadata - all actions require flow context")

        action_def = build_completion_action(handler, process_key, job_id, status, arguments, session_id, flow_id)

        params = arguments.get("params", {})
        if isinstance(params, dict):
            enrich_prompt_block(params, job_id, status, result_payload, error_payload)

        self._submit_completion_action(action_def, handler_key, job_id, process_key)

    def _resolve_job_token(self, job_id: str, success: bool = True) -> None:
        """Resolve the FRG token linked to this job when it reaches terminal state.

        Token resolution triggers flow completion check via FlowRuntimeGraph.
        Fails fast if FRG not available or job not linked to token.
        """
        from ananta.error_handling import FrameworkError

        if not self._flow_runtime_graph:
            raise FrameworkError(
                message="FlowRuntimeGraph not initialized - cannot resolve job token",
                error_code="async_job_manager.frg_not_initialized",
                details={"job_id": job_id},
            )

        job = self._job_for_token_resolution(job_id)
        flow_token_id = job.get("flow_token_id")
        if not flow_token_id:
            # Job not linked to a token - this is valid for jobs created
            # outside action context (e.g., direct API calls)
            return

        token_id = str(flow_token_id)
        if self._owner_jobs_exist(job, ["queued", "processing"]):
            return
        success = success and not self._owner_jobs_exist(job, ["error", "cancelled"])

        # Resolve the token (triggers flow completion check)
        result_summary = {"job_id": job_id, "success": success}
        owner_key = registered_job_owner_key(job)
        with self._completion_lock:
            if self._routing_owners.get(owner_key, 0):
                self._pending_token_resolutions[owner_key] = job_id
                return
            self._flow_runtime_graph.complete_token(token_id, success=success, result_summary=result_summary)

    def _job_for_token_resolution(self, job_id: str) -> dict[str, object]:
        """Read and validate the exact durable job before changing its token."""
        from ananta.error_handling import FrameworkError

        # Get this job's flow_token_id
        job_result = self.state_service.read_state(
            namespace=self._namespace,
            query={"table": self._table, "filters": {"id": job_id}, "limit": 1},
        )

        if not is_status_match(job_result.get("action_status"), ActionStatus.COMPLETED):
            raise FrameworkError(
                message=f"Failed to read job {job_id} for token resolution",
                error_code="async_job_manager.job_read_failed",
                details={"job_id": job_id, "result": dict(job_result)},
            )

        data = job_result.get("data")
        if not isinstance(data, dict):
            raise FrameworkError(
                message=f"Invalid job data structure for job {job_id}",
                error_code="async_job_manager.invalid_job_data",
                details={"job_id": job_id, "data": data},
            )

        # State service returns records directly in data, not nested under result
        records = data.get("records")
        if not isinstance(records, list) or not records:
            raise FrameworkError(
                message=f"Job {job_id} not found in database",
                error_code="async_job_manager.job_not_found",
                details={"job_id": job_id},
            )

        job = records[0]
        if not isinstance(job, dict):
            raise FrameworkError(
                message=f"Invalid job record format for job {job_id}",
                error_code="async_job_manager.invalid_record",
                details={"job_id": job_id, "record": job},
            )

        return job

    def _extract_count(self, result: dict[str, object]) -> int | None:
        """Extract count from SQL query result, returns None on error."""
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            return None

        data = result.get("data", {})
        if not isinstance(data, dict):
            return None

        records = data.get("records", [])
        if not isinstance(records, list) or not records:
            return None

        if not isinstance(records[0], list | tuple) or len(records[0]) == 0:
            return None

        count = records[0][0]
        return int(count) if count is not None else 0

    def get_most_recent_job(self) -> str | None:
        """Get the ID of the most recently created job."""
        if not self.state_service:
            logger.error("State service not available for job ID resolution")
            return None

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": self._table,
                    "filters": {},
                    "order_by": "created_at DESC",
                    "limit": 1,
                },
            )

            if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
                logger.error("Failed to query job ledger table: %s", result)
                return None

            data = result.get("data")
            if not isinstance(data, dict):
                logger.error("No jobs found in job ledger table")
                return None

            # State service returns records directly in data, not nested under result
            records = data.get("records")
            if not isinstance(records, list) or not records:
                logger.error("No jobs found in job ledger table")
                return None

            job = records[0]
            if not isinstance(job, dict):
                return None

            job_id = job.get("id")
            if not isinstance(job_id, str):
                logger.error("Job record missing id field: %s", job)
                return None

            return job_id

        except Exception as e:
            logger.error("Error getting most recent job: %s", e)
            return None

    def get_latest_job(
        self,
        plugin_name: str | None = None,
        action_name: str | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        """Return the most recently created job matching the optional filters.

        Note: plugin_name and action_name are combined into provider_name filter.
        """
        if not self.state_service:
            return {
                "action_status": "error",
                "error": {"message": "State service not available"},
            }

        filters = self._build_job_filters(plugin_name, action_name, status)

        try:
            result = self.state_service.read_state(
                namespace=self._namespace,
                query={
                    "table": self._table,
                    "filters": filters,
                    "order_by": "created_at DESC",
                    "limit": 1,
                },
            )

            job_record = self._extract_first_record(result)
            if job_record is None:
                return {
                    "action_status": "error",
                    "error": {
                        "message": "No jobs found matching filters",
                        "details": {
                            "plugin_name": plugin_name,
                            "action_name": action_name,
                            "status": status,
                        },
                    },
                }

            return {"action_status": "completed", "data": {"job": job_record}}

        except Exception as exc:
            logger.error("Error querying latest job: %s", exc)
            return {
                "action_status": "error",
                "error": {"message": f"Exception retrieving latest job: {exc}"},
            }
