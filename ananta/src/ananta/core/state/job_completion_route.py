"""Where a finished job's completion should be DELIVERED, read off its flow.

Companion to :mod:`ananta.core.state.job_completion_reach`, which records how a
completion *could* be reached. This module answers the next question: when the
originating dispatch named a durable role, the completion is pushed into that
role's inbox instead of being left for the drain.

**Why a push exists at all.** A ``solet call`` bridge mints a fresh session per
open, so a job's ``session_id`` is one-shot and useless for durable delivery.
A FLEET caller, though, exports ``AGENT_SESSION_ID``; the bridge rides it,
the server resolves it against the peer registry, and
``PlatformSurface._build_process_call_trigger_data`` stamps the resolved durable
role onto the flow as :data:`COMPLETION_ROUTE_ROLE_KEY`. That stamp is this
module's only input — recovered at completion time from the same flow row the
reach stamp already reads.

**Coverage is partial by construction.** Fleet callers only. A bare-shell CLI
call carries no key, resolves to no role, and leaves the flow unstamped; those
completions keep the pull path and the unreached marker. Any claim of full
coverage would be false.

**Presence is permission.** The writer omits the key entirely for a roleless
caller rather than writing "", so a present key always names a role that was
actually resolved. Nothing here guesses a destination.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Final
from weakref import WeakValueDictionary

from ananta.constants import CONTEXT_KEY_FLOW_ID, CONTEXT_KEY_SESSION_ID, FRAMEWORK_ASYNC_JOBS_TABLE
from ananta.core.domain.enums import ActionStatus
from ananta.core.domain.status import is_status_match
from ananta.interfaces.state_service_protocol import StateServiceProtocol

logger = logging.getLogger(__name__)

FLOWS_TABLE: Final[str] = "flows"
"""Core flow-ledger table carrying the routing stamp."""

COMPLETION_ROUTE_ROLE_KEY: Final[str] = "completion_route_role"
"""``trigger_data`` key naming the durable role to deliver this completion to.

Restated here rather than imported: the WRITER is
``agent_messaging_plugin.platform_surface`` (which owns the canonical
definition), and core does not import from a plugin. Same split, and the same
reason, as ``job_completion_reach.BRIDGE_PROCESS_CALL_TRIGGER``.
"""

BRIDGE_WAIT_ATTACHED_KEY: Final[str] = "bridge_wait_attached"
BRIDGE_WAIT_OWNER_KEY: Final[str] = "bridge_wait_owner"
BRIDGE_WAIT_HISTORY_KEY: Final[str] = "bridge_wait_history"


class JobWaiterLocks:
    """Serialize one job's release/outcome route without parking sibling jobs.

    Locks survive while a caller holds or awaits them. Weak values retire the
    entry after the last caller leaves; historical jobs do not grow this map.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: WeakValueDictionary[str, threading.RLock] = WeakValueDictionary()

    def for_job(self, job_id: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(job_id)
            if lock is None:
                lock = threading.RLock()
                self._locks[job_id] = lock
            return lock


def _waiter_owner(trigger: dict[str, object], flow_id: str, session_id: object) -> dict[str, object] | None:
    if trigger.get(BRIDGE_WAIT_ATTACHED_KEY) is not True:
        return None
    owner = {"flow_id": flow_id, "bridge_id": trigger.get("bridge_id"), "session_id": trigger.get("session_id")}
    if not all(isinstance(value, str) and value for value in owner.values()) or owner["session_id"] != session_id:
        logger.warning("Flow %s has invalid bridge waiter provenance; normal completion routing remains enabled", flow_id)
        return None
    return owner


def _record_object(result: Mapping[str, object], field: str) -> dict[str, object]:
    data = result.get("data")
    rows = data.get("records") if isinstance(data, dict) else None
    if isinstance(rows, list) and rows and isinstance(rows[0], dict):
        return _decode_trigger_data(rows[0].get(field))
    return {}


def _flow_trigger(state: StateServiceProtocol, namespace: str, flow_id: str) -> dict[str, object]:
    result = state.read_state(namespace, {"table": FLOWS_TABLE, "filters": {"id": flow_id}, "limit": 1})
    if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
        logger.warning("Flow %s waiter intent read failed; normal completion routing remains enabled", flow_id)
        return {}
    return _record_object(result, "trigger_data")


def stamp_bridge_waiter(state: StateServiceProtocol, namespace: str, record: dict[str, object]) -> None:
    """Copy server-owned flow intent before the job/request commit and worker start.

    Registered children keep their internal continuations: the attached caller
    waits for the root job, not every child that root needs to finish.
    """
    metadata = _decode_trigger_data(record.get("metadata"))
    flow_id = metadata.get("flow_id")
    if not isinstance(flow_id, str) or not flow_id or metadata.get("registered_root_job_id"):
        return
    try:
        owner = _waiter_owner(_flow_trigger(state, namespace, flow_id), flow_id, metadata.get("session_id"))
        if owner is None:
            return
        metadata.update({BRIDGE_WAIT_ATTACHED_KEY: True, BRIDGE_WAIT_OWNER_KEY: owner, BRIDGE_WAIT_HISTORY_KEY: [{"action": "attached", "at": datetime.now(UTC).isoformat()}]})
        record["metadata"] = json.dumps(metadata)
    except Exception:
        logger.warning("Flow %s waiter intent unreadable; normal completion routing remains enabled", flow_id, exc_info=True)


def bridge_wait_attached(metadata: dict[str, object]) -> bool:
    """Only a complete durable binding suppresses caller delivery; corruption advises."""
    if BRIDGE_WAIT_ATTACHED_KEY not in metadata or metadata[BRIDGE_WAIT_ATTACHED_KEY] is False:
        return False
    owner = metadata.get(BRIDGE_WAIT_OWNER_KEY)
    if metadata[BRIDGE_WAIT_ATTACHED_KEY] is True and isinstance(owner, dict):
        if all(isinstance(owner.get(key), str) and owner[key] for key in ("flow_id", "bridge_id", "session_id")) and owner.get("flow_id") == metadata.get("flow_id") and owner.get("session_id") == metadata.get("session_id"):
            return True
    logger.warning("Malformed bridge waiter marker; normal completion routing remains enabled")
    return False


def registered_root_wait_attached(state: StateServiceProtocol, namespace: str, metadata: dict[str, object]) -> bool:
    """A child inherits only caller-role suppression, never its own waiter flag.

    Consult the root at completion time so the single root release also covers
    children created later. Child continuations remain the normal fallback.
    """
    root_id = metadata.get("registered_root_job_id")
    if not isinstance(root_id, str) or not root_id:
        return False
    try:
        result = state.read_state(namespace, {"table": FRAMEWORK_ASYNC_JOBS_TABLE, "filters": {"id": root_id}, "limit": 1})
        if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
            raise RuntimeError("Root job read failed")
        root = _record_object(result, "metadata")
        if not root:
            logger.warning("Root job %s waiter metadata missing; normal completion routing remains enabled", root_id)
            return False
        if any(root.get(key) != metadata.get(key) for key in ("flow_id", "session_id")):
            logger.warning("Root job %s has different child lineage; normal completion routing remains enabled", root_id)
            return False
        return bridge_wait_attached(root)
    except Exception:
        logger.warning("Root job %s waiter unreadable; normal completion routing remains enabled", root_id, exc_info=True)
        return False


def _require_waiter_owner(metadata: dict[str, object], bridge_id: str, session_id: str) -> None:
    owner = metadata.get(BRIDGE_WAIT_OWNER_KEY)
    expected = {"flow_id": metadata.get("flow_id"), "bridge_id": bridge_id, "session_id": session_id}
    if owner != expected or metadata.get("session_id") != session_id:
        raise PermissionError("Only the originating bridge and session may release this waiter")


def clear_bridge_waiter(state: StateServiceProtocol, namespace: str, job_id: str, *, bridge_id: str, session_id: str) -> tuple[bool, dict[str, object]]:
    """Owner-only, idempotent clear with metadata CAS and retained correction history.

    The manager serializes this transaction with its terminal-routing section.
    A stale metadata snapshot changes nothing; callers can read it back and
    reconcile, rather than replaying either the operation or this release.
    """
    with state.transactional() as transaction:
        rows = transaction.query_state(namespace, {"table": FRAMEWORK_ASYNC_JOBS_TABLE, "filters": {"id": job_id}, "limit": 1})
        if not rows:
            raise ValueError(f"Job {job_id} not found")
        job = rows[0]
        metadata = _decode_trigger_data(job.get("metadata"))
        _require_waiter_owner(metadata, bridge_id, session_id)
        if metadata.get(BRIDGE_WAIT_ATTACHED_KEY) is False:
            return False, job
        if not bridge_wait_attached(metadata):
            logger.warning("Job %s has no valid attached waiter to release", job_id)
            return False, job
        history = metadata.get(BRIDGE_WAIT_HISTORY_KEY)
        events = list(history) if isinstance(history, list) else []
        metadata.update({BRIDGE_WAIT_ATTACHED_KEY: False, BRIDGE_WAIT_HISTORY_KEY: [*events, {"action": "released", "at": datetime.now(UTC).isoformat(), "bridge_id": bridge_id, "session_id": session_id}]})
        encoded = json.dumps(metadata)
        count = transaction.update_state(namespace, {"table": FRAMEWORK_ASYNC_JOBS_TABLE, "filters": {"id": job_id, "metadata": job.get("metadata")}}, {"metadata": encoded})
        if count != 1:
            raise RuntimeError("Bridge waiter metadata changed during release; read back the job to reconcile")
        return True, {**job, "metadata": encoded}


COMPLETION_DELIVERY_PROCESS_KEY: Final[str] = "plugin::agent_messaging_plugin::deliver_job_completion"
"""The verb that performs the delivery and stamps the delivered reach value.

Core submits it; it does NOT deliver inline. The outcome of a role dispatch is
only knowable where the dispatch happens, so the verb owns both the send and
the stamp that attests it — an action SUBMISSION would otherwise be recorded as
a delivery it cannot have observed.
"""


def read_completion_route_role(state_service: StateServiceProtocol, namespace: str, flow_id: str | None) -> str:
    """The durable role this flow's completion routes to, or "" for none.

    Degrade-silent and loud in the log: an unreadable flow, an unparseable
    ``trigger_data``, or an absent key all mean "no push route", which falls
    back to the existing continuation + drain behaviour. A routing decision is
    never inferred from a failed read.
    """
    if not flow_id:
        return ""
    try:
        result = state_service.read_state(
            namespace=namespace,
            query={"table": FLOWS_TABLE, "filters": {"id": flow_id}, "limit": 1},
        )
    except Exception:  # noqa: BLE001 — no route is the safe answer; never break a completion
        logger.warning(
            "flow %s unreadable for completion routing; no push route",
            flow_id,
            exc_info=True,
        )
        return ""
    if not is_status_match(result.get("action_status"), ActionStatus.COMPLETED):
        return ""
    data = result.get("data")
    if not isinstance(data, dict):
        return ""
    records = data.get("records")
    if not isinstance(records, list) or not records:
        return ""
    record = records[0]
    if not isinstance(record, dict):
        return ""
    trigger = _decode_trigger_data(record.get("trigger_data"))
    role = trigger.get(COMPLETION_ROUTE_ROLE_KEY)
    return role if isinstance(role, str) else ""


def _decode_trigger_data(raw: object) -> dict[str, object]:
    """``trigger_data`` as a dict — it is a TEXT column holding JSON."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("trigger_data is not valid JSON; no push route")
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def resolve_route(
    state_service: StateServiceProtocol,
    namespace: str,
    metadata: dict[str, object],
    status: str,
) -> tuple[str, str] | None:
    """``(role, flow_id)`` when this completion should be pushed, else None.

    Lives here rather than on ``AsyncJobManager`` for the reason its sibling
    module records: that class is the tree's standing maintainability-index
    debt, and this lane declined to deepen it.

    None covers every "no push" reason — a non-routable status, an unreadable
    job, a flow with no routing stamp — because they all take the same
    fallback: the pre-existing continuation. ``cancelled`` is deliberately NOT
    routable: it is terminal but has no continuation to replace, so pushing one
    would be new behaviour rather than a redirection of existing behaviour.
    """
    if status not in {"completed", "error"} or not metadata:
        return None
    flow_id = metadata.get("flow_id")
    if not isinstance(flow_id, str) or not flow_id:
        return None
    role = read_completion_route_role(state_service, namespace, flow_id)
    return (role, flow_id) if role else None


def build_delivery_action(
    *,
    role: str,
    job_id: str,
    provider_name: str,
    status: str,
    payload: dict[str, object] | None,
    flow_id: str,
    session_id: str | None,
) -> dict[str, object]:
    """The action definition that delivers this completion to ``role``.

    The envelope carries ``job_id``, the originating ``provider_name``
    (``plugin.verb``), the terminal ``status`` and the attached payload, so the
    recipient can act on the message itself. Names route, content binds: a
    notification that only said "a job finished" would force the reader into
    exactly the second lookup this lane exists to remove.
    """
    return {
        "name": f"deliver_job_completion_{job_id}",
        "description": f"Deliver job {job_id} completion to role {role}",
        "process_key": COMPLETION_DELIVERY_PROCESS_KEY,
        "process": {
            "provider_type": "plugin",
            "provider": "agent_messaging_plugin",
            "function_name": "deliver_job_completion",
        },
        "arguments": {
            "name": role,
            "job_id": job_id,
            "provider_name": provider_name,
            "status": status,
            "payload": payload or {},
        },
        "notes": f"Async job {job_id} {status} routed to role {role}",
        "flow_id": flow_id,
        **({"session_id": session_id} if session_id else {}),
    }


def _metadata_session_id(metadata: dict[str, object]) -> str | None:
    value = metadata.get("session_id")
    return value if isinstance(value, str) and value else None


def route_completion_to_role(
    state: StateServiceProtocol,
    namespace: str,
    metadata: dict[str, object],
    *,
    job_id: str,
    provider_name: str,
    status: str,
    result: dict[str, object] | None,
    error: dict[str, object] | None,
    submit: Callable[[dict[str, object]], object] | None,
) -> bool:
    """Replace caller delivery for an attached root, or submit its role push.

    An attached root consumes its caller continuation. A child checks that
    root at completion time and skips only the role push, leaving its internal
    continuation to the manager. Missing routes retain ordinary continuations.
    """
    if bridge_wait_attached(metadata):
        logger.debug("Job %s has an attached bridge waiter; no caller completion delivery", job_id)
        return True
    if registered_root_wait_attached(state, namespace, metadata):
        logger.debug("Job %s has an attached root waiter; preserve child continuation without caller role push", job_id)
        return False
    if submit is None:
        return False
    if provider_name in {f"agent_messaging_plugin.{name}" for name in ("deliver_job_completion", "deliver_error", "deliver_result")}:
        logger.debug("Delivery job %s is terminal; no further role delivery", job_id)
        return False
    route = resolve_route(state, namespace, metadata, status)
    if route is None:
        return False
    role, flow_id = route
    action = build_delivery_action(role=role, job_id=job_id, provider_name=provider_name, status=status, payload=result if status == "completed" else error, flow_id=flow_id, session_id=_metadata_session_id(metadata))
    try:
        submit(action)
    except Exception:
        logger.error("Failed to submit role delivery for job %s to role %s; falling back to its completion continuation and leaving the unreached stamp in place", job_id, role, exc_info=True)
        return False
    logger.info("Job %s completion routed to role %s (continuation replaced)", job_id, role)
    return True


def _parse_process_key(process_key: str) -> tuple[str, str, str]:
    """Parse process_key into provider parts."""
    parts = process_key.split("::")
    if len(parts) != 3:
        raise ValueError(f"Invalid process_key '{process_key}'. Expected provider_type::provider::function_name")
    return parts[0], parts[1], parts[2]


def build_completion_arguments(
    handler: dict[str, object],
    job_id: str,
    status: str,
    result_payload: dict[str, object] | None,
    error_payload: dict[str, object] | None,
) -> dict[str, object]:
    """Build arguments dict from handler template."""
    template = handler.get("template", {})
    if not isinstance(template, dict):
        template = {}

    arguments = deepcopy(template)
    params = _ensure_dict_field(arguments, "params")
    arguments["params"] = params

    payload_key = "job_payload" if status == "completed" else "job_error"
    payload_value = result_payload if status == "completed" else error_payload
    params[payload_key] = payload_value or {}
    params["job_id"] = job_id
    params["job_status"] = status

    return arguments


def _ensure_dict_field(container: dict[str, object], field: str) -> dict[str, object]:
    """Ensure field in container is a dict, return it."""
    value = container.setdefault(field, {})
    if not isinstance(value, dict):
        value = {}
        container[field] = value
    return value


def populate_state_args(
    arguments: dict[str, object],
    metadata: dict[str, object],
    job_id: str,
) -> tuple[dict[str, object], str | None, str | None]:
    """Populate state arguments from metadata. Returns (state_args, session_id, flow_id)."""
    state_args = _ensure_dict_field(arguments, "state")
    arguments["state"] = state_args

    session_id: str | None = None
    flow_id: str | None = None

    session_id_raw = metadata.get("session_id")
    if isinstance(session_id_raw, str) and session_id_raw:
        session_id = session_id_raw
        state_args.setdefault("session_id", session_id)

    flow_id_raw = metadata.get("flow_id")
    if isinstance(flow_id_raw, str) and flow_id_raw:
        flow_id = flow_id_raw
        state_args.setdefault("flow_id", flow_id)

    state_args.setdefault("job_id", job_id)
    return state_args, session_id, flow_id


def build_completion_action(
    handler: dict[str, object],
    process_key: str,
    job_id: str,
    status: str,
    arguments: dict[str, object],
    session_id: str | None,
    flow_id: str,
) -> dict[str, object]:
    """Build action definition from handler and arguments.

    Args:
        handler: Handler definition with name, description, notes
        process_key: The process key for the action
        job_id: The async job ID
        status: Job status (completed, failed, etc.)
        arguments: Action arguments
        session_id: Optional session ID
        flow_id: Required flow ID (all actions require flow context)

    Returns:
        Complete action definition with flow_id
    """
    provider_type, provider, function_name = _parse_process_key(process_key)
    action_def: dict[str, object] = {
        "name": handler.get("name") or f"{function_name}_{job_id}",
        "description": handler.get("description") or f"Auto-routing for job {job_id}",
        "process_key": process_key,
        "process": {
            "provider_type": provider_type,
            "provider": provider,
            "function_name": function_name,
        },
        "arguments": arguments,
        "notes": handler.get("notes") or f"Async job {job_id} {status}",
        CONTEXT_KEY_FLOW_ID: flow_id,
    }

    if session_id:
        action_def[CONTEXT_KEY_SESSION_ID] = session_id

    return action_def


def _enrich_result_data(user_prompt: dict[str, object], job_id: str, status: str) -> None:
    """Enrich result_data section in user prompt."""
    result_section = user_prompt.setdefault("result_data", {})
    if isinstance(result_section, dict):
        result_section.setdefault("job_id", job_id)
        result_section.setdefault("status", status)


def _enrich_action_result(
    user_prompt: dict[str, object],
    status: str,
    payload_value: dict[str, object] | None,
) -> None:
    """Enrich action_result section in user prompt."""
    action_status = "COMPLETED" if status == "completed" else status.upper()
    action_result = user_prompt.setdefault("action_result", {})
    if not isinstance(action_result, dict):
        return

    action_result.setdefault("action_status", action_status)
    data_section = action_result.setdefault("data", {})
    if isinstance(data_section, dict) and payload_value:
        for key, value in payload_value.items():
            data_section.setdefault(key, value)


def enrich_prompt_block(
    params: dict[str, object],
    job_id: str,
    status: str,
    result_payload: dict[str, object] | None,
    error_payload: dict[str, object] | None,
) -> None:
    """Enrich prompt block with job data.

    Populates both prompt.user (for ad-hoc inference templates) and
    prompt.observation (for the standard observation pipeline used by
    plan continuation). The observation dict enables plan step matching
    when async jobs complete.
    """
    prompt_block = params.get("prompt")
    if not isinstance(prompt_block, dict):
        return

    user_prompt = prompt_block.setdefault("user", {})
    if not isinstance(user_prompt, dict):
        return

    payload_value = result_payload if status == "completed" else error_payload

    _enrich_result_data(user_prompt, job_id, status)
    _enrich_action_result(user_prompt, status, payload_value)

    if payload_value:
        user_prompt.setdefault("job_payload", payload_value)
    if error_payload and status == "error":
        user_prompt.setdefault("job_error", error_payload)

    # Enrich observation dict if present (enables plan step matching)
    observation = prompt_block.get("observation")
    if isinstance(observation, dict):
        _enrich_action_result(observation, status, payload_value)
