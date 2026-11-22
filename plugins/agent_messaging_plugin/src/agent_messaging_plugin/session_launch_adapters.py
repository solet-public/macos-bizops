"""Public launch admission; the existing Python operations keep final results.

A launch call is refused here, before a job row exists, when its request cannot
start (blank runtime, model, effort or first message, a workspace that is not an
existing directory, an unresolvable host, a model cell a policy rule turned off).
Nothing here reads a register, a caller authority or a work contract.
"""

from __future__ import annotations

import tempfile
from copy import deepcopy
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_factory import reject_retired_session_arguments
from ananta.error_handling import FrameworkError

from .choreography_verbs import validate_flow_session_context
from .session_hosts import QUALIFICATION_HOST
from .session_launch_jobs import _require_success
from .session_launch_store import LaunchBusyError
from .session_lifecycle_store import format_directed_by
from .session_lifecycle_verbs import (
    SpawnSessionRequest,
    VerbError,
    validate_spawn_admission,
)

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
    except VerbError as exc:
        return operations._failure_result(code=exc.code, message=exc.message)
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
    """Refuse an unstartable request before a job row exists; touch no register."""
    from . import plugin as operations

    if action_name == "qualify_fleet":
        req = qualification_request(
            spawned_by_instance_id=operations._caller_instance_id(state),
            directed_by=format_directed_by(state.get("call_context")),
        )
    else:
        req = operations._spawn_session_request_from_params(raw, state, plugin._build_session_lifecycle_policy_config())
    validate_spawn_admission(plugin._get_state_service(), req)


def qualification_request(*, spawned_by_instance_id: str, directed_by: str) -> SpawnSessionRequest:
    """The fleet qualifier's request: the bounded watch host, started like any other session.

    The qualification host runs no model and no CLI, so its model and effort
    are labels, its workspace is the system temp directory (the host takes no
    working directory) and its first message is the probe text.
    """
    return SpawnSessionRequest(
        model="qualification",
        effort="none",
        workspace=tempfile.gettempdir(),
        first_message="fleet qualification probe",
        label="qualify-fleet",
        host=QUALIFICATION_HOST,
        local_name="qualify-fleet-worker",
        spawned_by_instance_id=spawned_by_instance_id,
        directed_by=directed_by,
    )
