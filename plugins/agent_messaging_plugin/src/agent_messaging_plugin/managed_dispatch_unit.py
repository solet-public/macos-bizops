"""Resolve existing registered Units and freeze assignments before dispatch.

The assignment never follows a subsequently edited plan or Unit binding.
Corrections require an explicit cancellation and a new dispatch; retries
verify the pinned revision and current Unit eligibility without replacing it.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_records

from .lane_worktrees import LaneWorktreeRepoRootError, resolve_lane_repo_root
from .managed_dispatch_errors import DispatchError
from .register_unit_client import (
    UNIT_DISPATCHABLE_STATES,
    RegisterActor,
    RegisterUnitClient,
    RegisterUnitError,
    UnitAssignment,
    UnitReadback,
    assignment_from_record,
)
from .schema import TABLE_MANAGED_DISPATCH

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

NEXT_SPAWN_CURRENT_ATTEMPT = "spawn_current_attempt"
EVENT_REGISTER_ANNOTATION_FAILED = "register_annotation_failed"


def fail_start_updates(row: Mapping[str, Any], error: DispatchError) -> dict[str, Any]:
    return {
        "state": "failed_start",
        "terminal_reason": f"{error.code}: {error.message}",
        "attempt_failure": {"code": error.code, "message": error.message},
        "next_required_action": "decide_retry_or_cancel",
        "responsible_role": str(row["spawned_by_role"]),
    }


def lane_root(row: Mapping[str, Any]) -> Path:
    repository_root = str(row.get("repository_root") or "")
    app_home = os.environ.get("APP_HOME", "").strip()
    if not repository_root.strip() and not app_home:
        raise DispatchError("lane_worktree_app_home_required", "An explicit APP_HOME-derived checkout is required.")
    try:
        return resolve_lane_repo_root(repository_root, app_home)
    except LaneWorktreeRepoRootError as exc:
        raise DispatchError(exc.code, str(exc)) from exc


def _register_call[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except RegisterUnitError as exc:
        raise DispatchError(exc.code, exc.detail) from exc


def require_assignment(row: Mapping[str, Any]) -> UnitAssignment:
    return _register_call(lambda: assignment_from_record(row))


def require_resolution_assignment(row: Mapping[str, Any], action: str) -> None:
    """Historical rows may only close; malformed native assignments still refuse."""
    if row.get("assignment") is None and action == "cancel":
        return
    require_assignment(row)


def resolution_actor_role(row: Mapping[str, Any], historical_cancel_role: str) -> str:
    if row.get("assignment") is not None:
        return str(row["spawned_by_role"])
    role = historical_cancel_role.strip()
    if not role:
        raise DispatchError("historical_cancel_role_required", "Plugin config key historical_cancel_role is unset.")
    return role


def _verify_unit(unit: UnitReadback, row: Mapping[str, Any], repository_id: str) -> None:
    if unit.is_deleted or unit.unit_id != row.get("unit_id"):
        raise DispatchError("unit_not_found", "The register has no live Unit with the requested identity.")
    if unit.state not in UNIT_DISPATCHABLE_STATES:
        raise DispatchError("unit_not_dispatchable", f"Unit {unit.unit_id} is {unit.state or 'stateless'}.")
    if unit.repository_id != repository_id:
        raise DispatchError("unit_repository_mismatch", f"Unit {unit.unit_id} belongs to a different repository.")
    if unit.kind != row.get("dispatch_kind"):
        raise DispatchError("unit_kind_mismatch", "Unit kind differs from dispatch_kind.")
    if not unit.revision_id:
        raise DispatchError("unit_revision_missing", "The Unit has no registered brief revision.")


def _assignment_premises(row: Mapping[str, Any], register: RegisterUnitClient) -> UnitReadback:
    unit_id = str(row.get("unit_id") or "")
    if not unit_id.startswith("unt_"):
        raise DispatchError("unit_id_required", "The full registered Unit id is the required launch address.")
    repository_id = _register_call(lambda: register.resolve_repository(lane_root(row)))
    expected_repository = str(row.get("repository_id") or "")
    if expected_repository and expected_repository != repository_id:
        raise DispatchError("unit_repository_mismatch", "The declared repository differs from the lane repository.")
    unit = _register_call(lambda: register.show_unit(unit_id))
    _verify_unit(unit, row, repository_id)
    expected_revision = str(row.get("expected_revision_id") or "")
    if expected_revision and expected_revision != unit.revision_id:
        raise DispatchError("unit_revision_stale", "The expected revision is no longer bound to this Unit.")
    return unit


def resolve_assignment(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    register: RegisterUnitClient,
) -> UnitAssignment:
    """Read before any ledger/session write; refuse a concurrently changed binding."""
    unit = _assignment_premises(row, register)
    unit_id, repository_id = unit.unit_id, unit.repository_id
    _require_no_other_live_dispatch(state, row)
    revision = _register_call(lambda: register.read_brief(unit))
    if revision.revision_id != unit.revision_id:
        raise DispatchError("unit_revision_stale", "Brief revision changed during assignment resolution.")
    confirmed = _register_call(lambda: register.show_unit(unit_id))
    _verify_unit(confirmed, row, repository_id)
    if confirmed != unit:
        raise DispatchError("unit_revision_stale", "Unit binding changed during assignment resolution.")
    assignment = UnitAssignment(unit.unit_id, unit.unit_key, repository_id, revision)
    _register_call(assignment.to_record)
    return assignment


def verify_retry_assignment(row: Mapping[str, Any], register: RegisterUnitClient) -> None:
    assignment = require_assignment(row)
    repository_id = _register_call(lambda: register.resolve_repository(lane_root(row)))
    if repository_id != assignment.repository_id:
        raise DispatchError("unit_repository_mismatch", "The retry checkout no longer resolves to the assigned repository.")
    unit = _register_call(lambda: register.show_unit(assignment.unit_id))
    _verify_unit(unit, row, assignment.repository_id)
    revision = _register_call(lambda: register.read_revision(assignment.revision.revision_id))
    if revision != assignment.revision:
        raise DispatchError("unit_revision_mismatch", "Pinned assignment bytes differ from the registered revision.")


def _require_no_other_live_dispatch(state: StateManagementInterface, row: Mapping[str, Any]) -> None:
    from .managed_dispatch import DISPATCH_TERMINAL_STATES  # noqa: PLC0415

    rows = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_DISPATCH, "filters": {"unit_id": str(row["unit_id"]), "is_deleted": 0}},
        )
    )
    live = [other for other in rows if other.get("dispatch_id") != row["dispatch_id"] and other.get("state") not in DISPATCH_TERMINAL_STATES]
    if live:
        raise DispatchError("unit_already_dispatched", f"Unit already has live dispatch {live[0].get('dispatch_id')}.")


def link_event_detail(row: Mapping[str, Any], event: str, extra: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "dispatch_id": str(row["dispatch_id"]),
        "attempt_number": int(row["attempt_number"]),
        "assignment_sha256": str(row["assignment_sha256"]),
        "event": event,
        "agent_instance_id": str(row.get("current_agent_instance_id") or ""),
        "model": str(row["model"]),
        "effort": str(row["effort"]),
        "host": str(row["host"]),
        "observed_at": datetime.now(UTC).isoformat(),
        **(extra or {}),
    }


def annotate_unit(
    register: RegisterUnitClient | None,
    row: Mapping[str, Any],
    actor: RegisterActor,
    event: str,
    extra: Mapping[str, Any] | None = None,
) -> RegisterUnitError | None:
    try:
        if register is None:
            raise RegisterUnitError("unit_register_unavailable", "No register client is bound to this call.")
        register.record_unit_event(str(row["unit_id"]), link_event_detail(row, event, extra), actor)
    except RegisterUnitError as exc:
        return exc
    return None


@dataclass(frozen=True, slots=True)
class AnnotationSource:
    """Authenticated historical provenance, immutable within one launch attempt."""

    dispatch_id: str
    unit_id: str
    assignment_sha256: str
    attempt_number: int
    actor: RegisterActor

    def to_record(self) -> dict[str, Any]:
        return asdict(self)


def capture_annotation_source(row: Mapping[str, Any], actor: RegisterActor) -> dict[str, Any]:
    return AnnotationSource(
        str(row["dispatch_id"]),
        str(row["unit_id"]),
        str(row["assignment_sha256"]),
        int(row["attempt_number"]),
        actor,
    ).to_record()


def read_annotation_source(row: Mapping[str, Any]) -> AnnotationSource:
    receipt = row.get("attempt_receipt")
    value = receipt.get("annotation_source") if isinstance(receipt, Mapping) else None
    if not isinstance(value, Mapping):
        raise RegisterUnitError("annotation_source_unavailable", "Attempt has no retained annotation source.")
    actor = _read_source_actor(value)
    if any(value.get(key) != row.get(key) for key in ("dispatch_id", "unit_id", "assignment_sha256", "attempt_number")):
        raise RegisterUnitError("annotation_source_mismatch", "Retained source differs from causal dispatch/Unit/assignment/attempt.")
    return AnnotationSource(
        str(value["dispatch_id"]),
        str(value["unit_id"]),
        str(value["assignment_sha256"]),
        int(value["attempt_number"]),
        actor,
    )


def annotate_transition_unit(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    register: RegisterUnitClient | None,
    actor: RegisterActor,
    event: str,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Append a best-effort register link event; a failure is recorded, never raised."""
    if not str(row.get("unit_id") or ""):
        return
    failure = annotate_unit(register, row, actor, event, extra)
    if failure is not None:
        record_annotation_failure(state, row, actor, event, failure)


def record_annotation_failure(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    actor: RegisterActor,
    event: str,
    error: RegisterUnitError,
) -> None:
    from .managed_dispatch import _write_event  # noqa: PLC0415 - append-only ledger writer

    logger.warning("managed dispatch %s register annotation %s failed: %s", row["dispatch_id"], event, error)
    _write_event(
        state,
        dispatch_id=str(row["dispatch_id"]),
        event_id=f"{EVENT_REGISTER_ANNOTATION_FAILED}:{event}:{secrets.token_hex(8)}",
        event_kind=EVENT_REGISTER_ANNOTATION_FAILED,
        attempt_agent_instance_id=str(row.get("current_agent_instance_id") or ""),
        actor_role=actor.role_name if actor.agent_instance_id else "platform-supervisor",
        actor_instance_id=actor.agent_instance_id,
        prior_version=int(row["version"]),
        observed_at=datetime.now(UTC),
        payload={
            "unit_id": str(row.get("unit_id") or ""),
            "event": event,
            "code": error.code,
            "detail": error.detail,
            "dispatch_id": str(row["dispatch_id"]),
            "attempt_number": int(row["attempt_number"]),
            "assignment_sha256": str(row["assignment_sha256"]),
            "source_actor": asdict(actor),
            "annotation_source": retained_annotation_source(row),
        },
        accepted=True,
    )


def annotate_causal_transition(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    register: RegisterUnitClient | None,
    event: str,
    extra: Mapping[str, Any],
    actor: RegisterActor | None = None,
) -> None:
    source_actor = actor or RegisterActor("", "", "platform-supervisor", str(row["lane_id"]))
    try:
        source = read_annotation_source(row)
        source_actor = source.actor
        if event == "uptake_uncertain" and actor is None:
            raise RegisterUnitError("annotation_source_unavailable", "First-turn authenticated actor context is unavailable.")
        if actor is not None and actor != source.actor:
            raise RegisterUnitError("annotation_source_mismatch", "First-turn context differs from retained authenticated source.")
    except RegisterUnitError as exc:
        record_annotation_failure(state, row, source_actor, event, exc)
        return
    annotate_transition_unit(state, row, register, source_actor, event, {**extra, "annotation_source": source.to_record()})


def fail_start(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    error: DispatchError,
) -> DispatchError:
    """Persist ``failed_start`` and return the caller-facing error."""
    from .managed_dispatch import _update_dispatch  # noqa: PLC0415 - existing CAS writer

    failed = _update_dispatch(state, row, fail_start_updates(row, error))
    return failed_attempt_error(failed, error.code, error.message)


def failed_attempt_error(row: Mapping[str, Any], code: str, message: str) -> DispatchError:
    return DispatchError(
        code,
        message,
        data={
            "dispatch_id": str(row["dispatch_id"]),
            "state": "failed_start",
            "version": int(row["version"]),
            "attempt_number": int(row["attempt_number"]),
            "next_required_action": str(row["next_required_action"]),
            "unit_id": str(row["unit_id"]),
        },
    )


def first_turn_already_recorded(row: Mapping[str, Any], agent_instance_id: str) -> bool:
    current = str(row.get("current_agent_instance_id") or "")
    if current == agent_instance_id and row.get("first_turn_at"):
        return True
    if current and current != agent_instance_id:
        raise DispatchError("attempt_conflict", "A different current attempt already exists.")
    if str(row["state"]) != "preparing":
        raise DispatchError("dispatch_not_preparing", "First-turn evidence requires preparing.")
    return False


def _read_source_actor(value: Mapping[str, Any]) -> RegisterActor:
    actor = value.get("actor")
    if not isinstance(actor, Mapping) or type(value.get("attempt_number")) is not int:
        raise RegisterUnitError("annotation_source_unavailable", "Retained annotation source is malformed.")
    fields = ("agent_instance_id", "agent_session_id", "role_name", "lane_id")
    if any(not isinstance(actor.get(key), str) or not actor[key].strip() for key in fields):
        raise RegisterUnitError("annotation_source_unavailable", "Retained annotation actor is malformed.")
    return RegisterActor(*(str(actor[key]) for key in fields))


def retained_annotation_source(row: Mapping[str, Any]) -> Any:
    """Read opaque historical source without treating a corrupt container as a mapping."""
    receipt = row.get("attempt_receipt")
    return receipt.get("annotation_source") if isinstance(receipt, Mapping) else None
