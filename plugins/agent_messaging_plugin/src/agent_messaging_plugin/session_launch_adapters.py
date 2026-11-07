"""Public launch admission; the existing Python operations keep final results."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_factory import reject_retired_session_arguments
from ananta.error_handling import FrameworkError

from .choreography_verbs import validate_flow_session_context
from .managed_dispatch import (
    DispatchError,
    _require_coordinator_authority,
    prepare_managed_dispatch,
    read_managed_dispatch,
    validate_resolution_admission,
)
from .managed_dispatch_unit import resolve_assignment, verify_retry_assignment
from .register_unit_client import UnitAssignment
from .session_launch_jobs import _require_success
from .session_launch_store import LaunchBusyError
from .session_lifecycle_store import (
    format_directed_by,
)
from .session_lifecycle_verbs import SpawnSessionRequest, VerbError, validate_spawn_admission

if TYPE_CHECKING:
    from .plugin import AgentMessagingPlugin


def _with_provider_env(raw: dict[str, Any], provider_env: Any) -> dict[str, Any]:
    """Re-attach the in-process provider overlay to a copy of the parameters.

    The copy keeps the persisted ``raw`` free of the overlay. A missing overlay
    returns ``raw`` unchanged, so ordinary spawns see no difference.
    """
    if provider_env is None:
        return raw
    return {**raw, "provider_env": provider_env}


def submit_launch(
    plugin: AgentMessagingPlugin,
    action_name: str,
    params: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Only server-injected context reaches admission; no host work runs here."""
    from . import plugin as operations

    try:
        raw = deepcopy(params.get("parameters", params))
        # A per-spawn provider overlay may carry credentials. The job payload
        # is persisted (request_data), so the overlay is taken out of the
        # persisted parameters here and re-attached only to the in-process
        # operation closure below. No recovery path re-runs a job from its
        # persisted parameters, so the overlay is never replayed from the row.
        provider_env = raw.pop("provider_env", None) if action_name == "spawn_session" else None
        context = deepcopy(state)
        session_id, flow_id = validate_flow_session_context(context)
        reject_retired_session_arguments(f"plugin::agent_messaging_plugin::{action_name}", raw)
        state_service = plugin._get_state_service()
        manager = plugin._try_acquire_async_job_manager()
        if state_service is None or manager is None:
            raise RuntimeError("Launch submission requires StateService and AsyncJobManager")
        _preflight(plugin, action_name, raw, context)
        operation = getattr(plugin, action_name)
        durable_context = _completion_context(context, session_id, flow_id)
        receipt = plugin._launch_jobs.submit(
            manager=manager,
            state=state_service,
            action_name=action_name,
            parameters=raw,
            context=durable_context,
            operation=lambda: operation(_with_provider_env(raw, provider_env), context),
        )
        return operations._success_result(data=_require_success(receipt))
    except LaunchBusyError as exc:
        return operations._failure_result(code="launch_busy", message=str(exc), data=exc.owner)
    except (DispatchError, VerbError) as exc:
        return operations._failure_result(code=exc.code, message=exc.message, data=exc.data if isinstance(exc, DispatchError) else None)
    except FrameworkError as exc:
        return operations._failure_result(code=str(exc.error_code), message=str(exc))
    except (RuntimeError, ValueError, TypeError) as exc:
        return operations._failure_result(code="launch_submission_failed", message=str(exc))


def _completion_context(context: dict[str, Any], session_id: str, flow_id: str) -> dict[str, object]:
    captured: dict[str, object] = {
        "session_id": session_id,
        "flow_id": flow_id,
        "directed_by": format_directed_by(context.get("call_context")),
    }
    for key in ("context_id", "inference_vertex_id", "inference_vertex_session_id", "caller_attribution_instance_id"):
        value = context.get(key)
        if isinstance(value, str) and value:
            captured[key] = value
    return captured


def _preflight(
    plugin: AgentMessagingPlugin,
    action_name: str,
    raw: dict[str, Any],
    state: dict[str, Any],
) -> None:
    reject_retired_launch_fields(raw)
    if action_name == "qualify_fleet":
        bind_bare_assignment(plugin, qualification_request(raw, state), raw, state)
        return
    if action_name == "provision_role_session":
        _provision_preflight(plugin, raw, state)
    elif action_name == "resolve_managed_dispatch":
        _resolution_preflight(plugin, raw, state)
    else:
        _spawn_preflight(plugin, action_name, raw, state)


def _resolution_preflight(plugin: AgentMessagingPlugin, raw: dict[str, Any], state: dict[str, Any]) -> None:
    row = read_managed_dispatch(plugin._get_state_service(), str(raw.get("dispatch_id") or ""))
    validate_resolution_admission(
        plugin._get_state_service(),
        dispatch_id=str(raw.get("dispatch_id") or ""),
        event_id=str(raw.get("event_id") or ""),
        action=str(raw.get("action") or ""),
        actor=plugin._dispatch_actor_from_state(state),
        historical_cancel_role=plugin._build_session_lifecycle_policy_config().historical_cancel_role,
    )
    if raw.get("action") == "request_retry":
        verify_retry_assignment(row, plugin._register_unit_client())


def qualification_request(raw: dict[str, Any], context: dict[str, Any]) -> SpawnSessionRequest:
    return SpawnSessionRequest(
        role_class="ephemeral",
        lane_id="qualify-fleet",
        unit_id=str(raw.get("unit_id") or ""),
        repository_root=str(raw.get("repository_root") or ""),
        work_class="read_only",
        budget_line="qualification",
        dispatch_kind="infrastructure",
        spawned_by_role=str(raw.get("spawned_by_role") or ""),
        host="qualification",
        role_name="qualify-fleet-worker",
        local_name="qualify-fleet-worker",
        directed_by=format_directed_by(context.get("call_context")),
        synthetic_qualification_no_worktree=True,
    )


def _spawn_preflight(
    plugin: AgentMessagingPlugin,
    action_name: str,
    raw: dict[str, Any],
    state: dict[str, Any],
) -> None:
    from . import plugin as operations

    directed_by = format_directed_by(state.get("call_context"))
    req = operations._spawn_session_request_from_params(raw, directed_by)
    req = operations._apply_spawn_session_policy(req, plugin._build_session_lifecycle_policy_config())
    validate_spawn_admission(plugin._get_state_service(), req)
    if action_name == "spawn_session":
        if req.role_class == "project":
            raise VerbError("managed_dispatch_required", "Project work requires dispatch_managed_work")
        bind_bare_assignment(plugin, req, raw, state)
        return
    actor = plugin._dispatch_actor_from_state(state)
    if not operations.holds_role(plugin._get_state_service(), req.spawned_by_role, actor.agent_session_id):
        raise DispatchError("coordinator_authority_denied", "Authenticated caller does not hold spawned_by_role.")
    req = replace(req, spawned_by_instance_id=actor.agent_instance_id)
    spec = operations._dispatch_spec_from_params(raw, req, directed_by)
    prepare_managed_dispatch(plugin._get_state_service(), spec, register=plugin._register_unit_client(), actor=actor)


def _provision_preflight(plugin: AgentMessagingPlugin, raw: dict[str, Any], state: dict[str, Any]) -> None:
    from . import plugin as operations

    state_service = plugin._get_state_service()
    role_class, _ = operations.lifecycle_resolve_provisioned_role_class(
        state_service,
        role_name=str(raw.get("role_name") or ""),
        requested_role_class=str(raw.get("requested_role_class") or ""),
    )
    operations._require_provisioner_authority(
        state_service,
        str(raw.get("spawned_by_role") or ""),
        plugin._dispatch_actor_from_state(state),
    )

    if operations._provisioned_live_role_holder(plugin, state_service, str(raw.get("role_name") or "")) is not None:
        return

    spawn_raw = dict(raw, role_class=role_class)
    spawn_raw["local_name"] = operations.lifecycle_resolve_local_name(
        role_class=role_class,
        role_name=str(raw.get("role_name") or ""),
        lane_id=str(raw.get("lane_id") or ""),
    )
    directed_by = format_directed_by(state.get("call_context"))
    req = operations._spawn_session_request_from_params(spawn_raw, directed_by)
    req = operations._apply_spawn_session_policy(req, plugin._build_session_lifecycle_policy_config())
    actor = plugin._dispatch_actor_from_state(state)
    req = replace(req, spawned_by_instance_id=actor.agent_instance_id)
    spec = operations._dispatch_spec_from_params(spawn_raw, req, directed_by)
    prepare_managed_dispatch(state_service, spec, register=plugin._register_unit_client(), actor=actor)


def reject_retired_launch_fields(raw: dict[str, Any]) -> None:
    retired = {"brief_ref", "brief_sha256", "brief_repository_root", "unit_key", "addresses", "reference_basis", "reference_basis_reason", "assignment", "assignment_sha256"}
    present = sorted(retired.intersection(raw))
    if present:
        raise VerbError("retired_launch_argument", f"Launch arguments are retired: {present}; name an existing Unit.")


def bind_bare_assignment(
    plugin: AgentMessagingPlugin,
    req: SpawnSessionRequest,
    raw: dict[str, Any],
    context: dict[str, Any],
) -> SpawnSessionRequest:
    """Resolve before job admission; carry the same immutable value into its worker."""
    reject_retired_launch_fields(raw)
    actor = plugin._dispatch_actor_from_state(context)
    req = replace(req, spawned_by_instance_id=actor.agent_instance_id)
    record = asdict(req)
    _require_coordinator_authority(plugin._get_state_service(), record, actor)
    assignment = context.get("_registered_launch_assignment")
    if not isinstance(assignment, UnitAssignment):
        record["expected_revision_id"] = str(raw.get("expected_revision_id") or "")
        assignment = resolve_assignment(plugin._get_state_service(), record, plugin._register_unit_client())
        context["_registered_launch_assignment"] = assignment
    if assignment.unit_id != req.unit_id:
        raise VerbError("unit_assignment_mismatch", "The admitted Unit differs from the spawn request.")
    return replace(req, assignment=assignment)
