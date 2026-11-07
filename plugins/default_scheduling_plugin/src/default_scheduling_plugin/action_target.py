"""Copied action projections and destination-only corrections.

Only declared messaging argument contracts are addressable. Execution context
and arbitrary action text are never interpreted as a destination.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .models import ActionData, ScheduleData

RELOAD_SAFE = True
_MESSAGING = "plugin::agent_messaging_plugin::"
_DESTINATION_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    _MESSAGING + "peer_send_by_name": (("role", "name"),),
    _MESSAGING + "send_peer_message": (("instance", "peer_agent_instance_id"), ("session", "peer_agent_session_id")),
    _MESSAGING + "drive_session": (("instance", "agent_instance_id"),),
}


class ScheduleConflictError(ValueError):
    """A current schedule does not satisfy a mutation's integrity premises."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def actions_for(schedule: ScheduleData) -> list[ActionData]:
    """Use the executor's existing legacy single-action semantics."""
    if schedule.actions:
        return schedule.actions
    if schedule.action_name:
        return [ActionData(name=schedule.action_name, parameters=schedule.action_parameters, result_processor=None, result_processor_kind=None)]
    return []


def action_definitions(schedule: ScheduleData) -> list[dict[str, Any]]:
    """Return canonical, independent copies of every persisted action value."""
    return [
        {
            "process_key": action.name,
            "arguments": deepcopy(action.parameters),
            "result_processor": deepcopy(action.result_processor),
            "result_processor_kind": action.result_processor_kind,
        }
        for action in actions_for(schedule)
    ]


def target_for(action: ActionData, index: int) -> dict[str, Any]:
    """Classify an actual process contract; ambiguous destinations stay readable."""
    result: dict[str, Any] = {"action_index": index, "mechanism": action.name, "kind": "process", "value": action.name, "supported": False}
    fields = _DESTINATION_FIELDS.get(action.name, ())
    if not fields:
        return result
    values = [(kind, key, action.parameters.get(key)) for kind, key in fields if action.parameters.get(key)]
    if len(values) != 1 or not isinstance(values[0][2], str):
        result.update(kind="ambiguous" if len(values) > 1 else "unknown", value=None, advisory="destination_not_addressable")
        return result
    kind, key, value = values[0]
    if kind == "session":
        # send_peer_message resolves peer_id first; this field is only a fallback.
        # Changing it cannot guarantee a change to the next dispatch recipient.
        result.update(kind="unknown", value=None, advisory="session_hint_is_fallback")
        return result
    result.update(kind=kind, value=value, argument=key, supported=True)
    return result


def targets_for(schedule: ScheduleData) -> list[dict[str, Any]]:
    """Keep action ordinals stable, including non-messaging actions."""
    return [target_for(action, index) for index, action in enumerate(actions_for(schedule))]


def rebind_target(schedule: ScheduleData, index: int, kind: str, value: str) -> ScheduleData:
    """Correct one destination without changing any other persisted value."""
    actions = actions_for(schedule)
    if isinstance(index, bool) or index < 0 or index >= len(actions):
        raise ScheduleConflictError("invalid_action_index")
    target = target_for(actions[index], index)
    if not target["supported"] or target["kind"] != kind or not value:
        raise ScheduleConflictError("unsupported_destination")
    updated = schedule.model_copy(deep=True)
    if updated.actions:
        arguments = deepcopy(updated.actions[index].parameters)
        arguments[target["argument"]] = value
        updated.actions[index] = updated.actions[index].model_copy(update={"parameters": arguments})
    else:
        updated.action_parameters[target["argument"]] = value
    return updated
