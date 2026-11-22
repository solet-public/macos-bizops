"""Fleet session-management Phase B, D1 (§4) — the L1 verb bodies:
``spawn_session``, ``list_sessions``, ``session_status``, ``clear_session``,
``compact_session``, ``drive_session``, ``terminate_session``,
``retire_session``, ``report_alive``. Pure functions over
:mod:`session_lifecycle_store` + :mod:`session_hosts` (+ role-binding lookups
for the fill-never-mint validation) — the ``@platform_process`` wiring in
``plugin.py`` is a thin transport shim, mirroring the ``role_claim.py`` /
``plugin.py`` split.

``clear_session``/``compact_session`` (AMEND 5b) and ``drive_session`` (D2
window, rotation-boundary rider ruling 2026-08-04) ride the host driver's
driver channel — fire-and-forget (send the text, do not await the resulting
turn). ``clear_session(park=True)`` additionally drives
``live/idle/overdue -> parked`` (§3.2 matrix, L3 rule 2, steward direction) —
the ONLY writer of that edge; ``compact_session`` never parks;
``drive_session`` is the §3.2 ``parked -> live`` writer ("new dispatch through
the driver channel"). All three share ``_resolve_driver_channel`` for the
``unsupported_on_host`` refusal (hosts with no driver channel, e.g.
``host='operator'``) — a config/mechanism gap, never a silent degradation.

The platform sweep that marks ``overdue`` and fires/delivers ``deadline``
``session_dependency`` edges lives in ``session_sweep.py`` (an ``on_tick``
rider, not a verb) — this module provides the primitives it calls
(``transition_lifecycle_state``, the managed_session reads).
``terminate_session`` owns firing + best-effort delivering
``session_terminal`` edges (guarded, once — 2026-08-04, drive-on-delivery
lane fix slice); ``retire_session`` composes ``terminate_session`` and no
longer fires them itself — the sweep does not duplicate either.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_records

from .model_capability_store import CatalogError
from .model_cell_enablement import decision as cell_decision
from .model_cell_enablement import read_policy as read_cell_policy
from .schema import (
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    SESSION_HOST_HEADLESS,
    SESSION_HOST_TMUX,
    SESSION_VISIBILITY_HEADLESS,
    SESSION_VISIBILITY_VISIBLE,
    TABLE_MODEL_CAPABILITY_CELL,
)
from .session_end_reason import FAILED, end_reason_of
from .session_hosts import (
    DEFAULT_AGENT_RUNTIME,
    QUALIFICATION_HOST,
    AgentRuntimeNotSupportedError,
    HostCannotSpawnError,
    HostMechanismMissingError,
    HostNotDeclaredError,
    resolve_host_driver,
)
from .session_lifecycle_store import (  # noqa: F401
    DEFAULT_REPORT_BY_SECONDS as DEFAULT_REPORT_BY_SECONDS,
)
from .session_lifecycle_store import (
    ManagedSessionSpec,
    SessionNotFoundError,
    insert_managed_session,
    persist_first_turn_evidence,
    read_managed_session,
    set_host_ref,
    transition_lifecycle_state,
)
from .session_list import (
    LIST_SESSIONS_DEFAULT_LIMIT,
    SessionListError,
    list_session_rows,
)
from .session_list import LIST_SESSIONS_MAX_LIMIT as LIST_SESSIONS_MAX_LIMIT  # noqa: F401
from .session_role_claim_store import (
    delete_session_role_claim_if_still_holds,
    read_session_role_claim,
)
from .spawn_provider_env import provider_env_host_spec, refuse_unapplied_provider_env
from .verb_errors import VerbError

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .session_hosts import HostDriver

logger = logging.getLogger(__name__)

_TERMINAL_STATES = frozenset({LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED})

# A spawned session's first turn is the caller's own text, delivered verbatim.
FIRST_TURN_SOURCE_FIRST_MESSAGE: Final = "first_message"


@dataclass(frozen=True, slots=True)
class SpawnSessionRequest:
    """One session start, described by the solet's own concepts only: the
    runtime, model and effort, a workspace directory, the first message,
    permissions and an opaque label. An empty text field is the transport
    representation of an omitted argument; admission refuses the ones a start
    cannot do without."""

    # Exact peer-registry agent_id vocabulary.  Kept orthogonal to ``host``:
    # claude_code/codex select the worker runtime; headless/tmux/operator
    # select the hosting topology.
    agent_runtime: str = DEFAULT_AGENT_RUNTIME
    model: str = ""
    effort: str = ""
    # The caller's existing directory, used verbatim as the host's working
    # directory; nothing is created in it.
    workspace: str = ""
    # Delivered byte for byte as the session's first turn.
    first_message: str = ""
    # Opaque caller text, stored and reported, never parsed.
    label: str = ""
    host: str | None = None
    visibility: str = ""
    # The name the session answers to on its own machine. Empty defaults to the
    # label when one is given, else the instance id.
    local_name: str = ""
    report_by_seconds: int = 0
    # Lineage only: the spawning session's instance id when the call carries one.
    spawned_by_instance_id: str = ""
    # Server-built provenance string from the call context, never caller-supplied.
    directed_by: str = ""
    # The headless driver's spawn-time PreToolUse allowlist, --permission-mode
    # value and declared fleet transport. For allowed_tools ``None`` means omitted
    # ("let the policy resolver fill it") and an explicit empty tuple means no
    # tool allowed; the other two fields treat empty as omitted.
    allowed_tools: tuple[str, ...] | None = None
    permission_mode: str = ""
    transport: str = ""
    # AskUserQuestion default-deny (operator ruling 2026-08-14): per-spawn escape
    # hatch; only the tmux driver reads it.
    allow_askuserquestion: bool = False
    # Per-spawn Claude provider overlay, carried ONLY on this bounded request
    # and on the host spawn spec: never written to the ledger row or any
    # record, because it may carry credentials. The host rule that accepts or
    # refuses it lives in spawn_provider_env.
    provider_env: dict[str, str] = field(default_factory=dict)
    # W4A item 3 (#8 §43.1): proceed even though the host's preflight can prove
    # the worker's hooks will not run. Default off — running degraded is a
    # stated choice, recorded on the ledger row and logged loudly.
    degraded_hooks_acknowledged: bool = False


_VISIBLE_HOSTS: Final[frozenset[str]] = frozenset({SESSION_HOST_TMUX})
_HEADLESS_HOSTS: Final[frozenset[str]] = frozenset({SESSION_HOST_HEADLESS})


def _require_visibility_matches_host(visibility: str, resolved_host: str) -> None:
    """Refuse a descriptive visibility label that disagrees with the host.

    ``visible`` is the persisted visibility vocabulary and means a tmux-hosted
    worker. ``tmux`` is accepted as the explicit spelling used by callers of
    the spawn API. An omitted visibility has no asserted topology to check.
    """
    if resolved_host not in _VISIBLE_HOSTS | _HEADLESS_HOSTS:
        return
    expected_hosts = {
        SESSION_VISIBILITY_VISIBLE: _VISIBLE_HOSTS,
        "tmux": _VISIBLE_HOSTS,
        SESSION_VISIBILITY_HEADLESS: _HEADLESS_HOSTS,
    }.get(visibility)
    if expected_hosts is not None and resolved_host in expected_hosts:
        return
    if not visibility:
        return
    expected = " or ".join(sorted(expected_hosts)) if expected_hosts else "a declared visibility"
    raise VerbError(
        "visibility_host_mismatch",
        f"visibility {visibility!r} requires {expected}; resolved host is {resolved_host!r}.",
    )


@dataclass(frozen=True, slots=True)
class DispatchActor:
    """Server-derived caller identity used for lane-custody authorization.

    The public process adapter builds this from an OAuth principal or a
    server-stamped local bridge identity, then verifies its live peer binding.
    Caller parameters never construct this type. The custody verbs still
    re-check the durable role binding at act time.
    """

    agent_instance_id: str
    agent_session_id: str
    authority_source: Literal["oauth_principal", "live_peer_binding"]


_RETIRE_CAPTURE_FIELDS: Final = frozenset({"disposition", "rationale", "predecessor_packet_id"})


def _release_retiring_session_role_claim(  # pyright: ignore[reportUnusedFunction]
    state: StateManagementInterface,
    row: Mapping[str, object],
) -> None:
    """Best-effort cardinality cleanup after the host has reached terminal state."""
    session_id = str(row.get("agent_session_id") or "")
    if not session_id:
        return
    claim_row = read_session_role_claim(state, session_id)
    if claim_row is None:
        return
    delete_session_role_claim_if_still_holds(
        state,
        agent_session_id=session_id,
        expected_held_role=str(claim_row.get("held_role") or ""),
    )


def _resolve_spawn_host(req: SpawnSessionRequest) -> tuple[HostDriver, str]:
    """Resolve and policy-check a requested host before allocating a worker identity."""
    try:
        driver, resolved_host = resolve_host_driver(req.host, req.agent_runtime)
    except AgentRuntimeNotSupportedError as exc:
        raise VerbError("agent_runtime_unsupported", str(exc)) from exc
    except HostNotDeclaredError as exc:
        raise VerbError("host_not_declared", str(exc)) from exc
    except HostMechanismMissingError as exc:
        raise VerbError("host_mechanism_missing", exc.remedy) from exc
    _require_visibility_matches_host(req.visibility, resolved_host)
    refuse_unapplied_provider_env(req, resolved_host)
    return driver, resolved_host


def _model_cell_advisories(state: StateManagementInterface, req: SpawnSessionRequest) -> list[str]:
    """Refuse a model cell that a policy rule has turned off, naming the rule.

    Pass condition: the named (runtime, model, effort) cell is enabled; the
    operator's ordered rules decide, exactly as they decide every catalog
    cell they write. A cell the catalog does not know has no rule decision to
    honour, so it starts with an advisory (rul_becb4bed), as does a cell
    whose stored policy cannot be read (decided from its stored flag).
    """
    cell_key = f"({req.agent_runtime}, {req.model}, {req.effort})"
    rows = require_records(
        state.query_ordered(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MODEL_CAPABILITY_CELL,
                "filters": {"runtime": req.agent_runtime, "model": req.model, "effort": req.effort},
                "order_by": [["updated_at", "desc"], ["id", "desc"]],
                "limit": 1,
            },
        ),
    )
    if not rows:
        return [f"model_cell_unknown: {cell_key} is not in the model capability catalog, so no cell rule decides it; the session starts."]
    advisories: list[str] = []
    try:
        enabled, provenance = cell_decision(read_cell_policy(state), rows[0])
    except CatalogError as exc:
        enabled, provenance = rows[0].get("enabled") is not False, "the cell's stored enabled flag"
        advisories.append(f"model_cell_policy_unreadable: {exc.code}: {exc.message}; decided from {provenance}.")
    if not enabled:
        raise VerbError(
            "model_cell_disabled",
            f"Model cell {cell_key} is turned off by {provenance}; name an enabled cell, or change the rule with set_model_cells_enabled.",
        )
    return advisories


def validate_spawn_admission(
    state: StateManagementInterface,
    req: SpawnSessionRequest,
) -> tuple[HostDriver, str, list[str]]:
    """Refuse a start before any row is written; return its host driver,
    resolved host and advisories.

    Pass conditions, all checked by tests/generic_spawn_smoke.py: runtime,
    model, effort and the first message are non-blank; the workspace is the
    absolute path of an existing directory; the host resolves; and no policy
    rule turned the model cell off. The caller's identity is never a condition.
    """
    values = {"agent_runtime": req.agent_runtime, "model": req.model, "effort": req.effort, "first_message": req.first_message}
    missing = [name for name, value in values.items() if not value.strip()]
    if missing:
        raise VerbError("generic_spawn_field_required", f"A spawn requires non-blank {missing}.")
    workspace = Path(req.workspace)
    if not req.workspace or not workspace.is_absolute() or not workspace.is_dir():
        raise VerbError(
            "generic_workspace_invalid",
            f"workspace must be the absolute path of an existing directory; got {req.workspace!r}.",
        )
    driver, resolved_host = _resolve_spawn_host(req)
    return driver, resolved_host, _model_cell_advisories(state, req)


def _host_spawn_spec(req: SpawnSessionRequest, agent_instance_id: str, local_name: str, resolved_host: str) -> dict[str, object]:
    """The host driver's spawn spec: the request's own fields and nothing else."""
    spec: dict[str, object] = {
        "agent_instance_id": agent_instance_id,
        "local_name": local_name,
        "model": req.model,
        "effort": req.effort,
        "allowed_tools": req.allowed_tools or (),
        "permission_mode": req.permission_mode,
        "transport": req.transport,
        "allow_askuserquestion": req.allow_askuserquestion,
        "agent_runtime": req.agent_runtime,
        "degraded_hooks_acknowledged": req.degraded_hooks_acknowledged,
        **provider_env_host_spec(req),
    }
    # The qualification host is a bounded watch process with no CLI, so it takes
    # no working directory; every other host runs its CLI in the workspace.
    if resolved_host != QUALIFICATION_HOST:
        spec["workspace"] = req.workspace
    return spec


def spawn_session(state: StateManagementInterface, req: SpawnSessionRequest) -> dict[str, Any]:
    """§4 ``spawn_session``: validate -> write the ledger row (spawning,
    BEFORE dispatch) -> launch the host in the caller's workspace -> deliver the
    caller's first message byte for byte. A host refusal transitions the row
    straight to ``terminated`` (we know synchronously no process exists) and
    propagates the original error token, never leaving a stuck ``spawning`` row.

    ``agent_instance_id`` is passed through the spawn spec so a real driver can
    inject it into the worker's environment, which is how ``backfill_registration``
    finds the ledger row when the process later registers. Nothing is created in
    the workspace, and lineage is whatever the request carries (empty when the
    caller has no session identity).
    """
    driver, resolved_host, advisories = validate_spawn_admission(state, req)
    agent_instance_id = f"agi-{secrets.token_hex(16)}"
    local_name = req.local_name or req.label or agent_instance_id
    # A name the caller chose is checked for a live holder; the instance id cannot be held.
    _refuse_if_local_name_held(state, local_name=local_name, role_name="", directed_by=req.directed_by)
    row = insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id,
            host=resolved_host,
            agent_runtime=req.agent_runtime,
            spawned_by_instance_id=req.spawned_by_instance_id,
            local_name=local_name,
            degraded_hooks_acknowledged=req.degraded_hooks_acknowledged,
            visibility=req.visibility,
            model=req.model,
            effort=req.effort,
            report_by_seconds=req.report_by_seconds,
            directed_by=req.directed_by,
            session_origin="spawned",
            label=req.label,
            workspace=req.workspace,
        ),
    )
    # A host that reports first_turn_on_launch hands the first message to the
    # CLI as its launch prompt; the others get it through the driver channel.
    carries = driver.capability_report().get("first_turn_on_launch") is True
    host_spec = {**_host_spawn_spec(req, agent_instance_id, local_name, resolved_host), **({"first_turn": req.first_message} if carries else {})}
    try:
        host_ref = driver.spawn(host_spec)
    except HostCannotSpawnError as exc:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED,
            directed_by=req.directed_by,
            reason=f"host_cannot_spawn: {exc.remedy}",
            end_reason=FAILED,
        )
        raise VerbError("host_cannot_spawn", exc.remedy) from exc
    if host_ref:
        set_host_ref(state, agent_instance_id=agent_instance_id, host_ref=host_ref)
    if carries:
        # The CLI submits the launch prompt itself, so true means handed to the
        # host at launch: no composer wait, no paste, nothing to lose.
        delivered, error = True, ""
    else:
        delivered, error = _send_first_turn(
            req.first_message,
            state=state,
            source=FIRST_TURN_SOURCE_FIRST_MESSAGE,
            agent_instance_id=agent_instance_id,
        )
    persist_first_turn_evidence(
        state,
        agent_instance_id=agent_instance_id,
        source=FIRST_TURN_SOURCE_FIRST_MESSAGE,
        delivered=delivered,
        error=error,
        observed_at=datetime.now(UTC),
    )
    return {
        "agent_instance_id": agent_instance_id,
        "agent_runtime": req.agent_runtime,
        "host": resolved_host,
        "host_ref": host_ref,
        "lifecycle_state": str(row.get("lifecycle_state") or ""),
        "first_turn_delivered": delivered,
        "first_turn_error": error,
        "label": req.label,
        "workspace": req.workspace,
        "advisories": advisories,
    }


def _send_first_turn(
    text: str,
    *,
    state: StateManagementInterface,
    source: str,
    agent_instance_id: str,
) -> tuple[bool, str]:
    """Send one first turn through the host's driver channel. Returns
    ``(delivered, error)``; NEVER raises, so a delivery fault never blocks the
    spawn (ordering-ruling guard (b)). The text itself is never logged."""
    delivered = False
    error = ""
    try:
        channel = _resolve_driver_channel(read_managed_session(state, agent_instance_id))
        channel.send(text)
        delivered = True
    except VerbError as exc:
        error = f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001 — visibility, never a spawn-blocking fault
        error = str(exc)
    if not delivered:
        # Ordering-ruling guard (b): a failed first-turn delivery must be
        # VISIBLE (logged + surfaced in the spawn result), never silent —
        # its silent failure mints exactly the unreachable-at-zero-cost
        # worker Finding 0 warns about. The spawn itself is NOT blocked.
        logger.warning(
            "spawn_session %s: first-turn delivery (%s) failed: %s",
            agent_instance_id,
            source,
            error,
        )
    return delivered, error


# The public session record is shared by the roster and the individual status
# read. The storage schema also contains private migration inputs.
_PUBLIC_SESSION_FIELDS: Final = frozenset(
    {
        "id",
        "namespace",
        "name",
        "external_id",
        "created_at",
        "created_by",
        "updated_at",
        "updated_by",
        "is_deleted",
        "agent_instance_id",
        "agent_session_id",
        "agent_id",
        "session_origin",
        "spawned_by_instance_id",
        "spawned_by_role",
        "role_name",
        "local_name",
        "label",
        "workspace",
        "agent_runtime",
        "model",
        "effort",
        "visibility",
        "directed_by",
        "host",
        "host_ref",
        "host_liveness",
        "host_liveness_observed_at",
        "host_liveness_detail",
        "last_reconciled_at",
        "capability_report",
        "first_turn_source",
        "first_turn_delivered",
        "first_turn_error",
        "first_turn_at",
        "hook_absence_checked_at",
        "hook_absence_warned_at",
        "degraded_hooks_acknowledged",
        "lifecycle_state",
        "last_transition_at",
        "registration_overdue_at",
        "registration_overdue_reason",
        "report_by",
        "report_by_seconds",
        "report_by_source",
        "status_source",
        "last_heartbeat_at",
        "heartbeat_failures_since_last",
        "heartbeat_failure_first_at",
        "heartbeat_failure_last_reason",
    }
)


def _public_session_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """One roster row: the public fields plus ``end_reason``, None while the record has not ended."""
    public = {key: row[key] for key in row if key in _PUBLIC_SESSION_FIELDS}
    public["end_reason"] = end_reason_of(row)
    return public


def list_sessions(
    state: StateManagementInterface,
    filters: dict[str, Any] | None = None,
    *,
    live_only: bool = False,
    limit: object = LIST_SESSIONS_DEFAULT_LIMIT,
    after_created_at: object = None,
    after_id: object = None,
) -> dict[str, Any]:
    """§4 filter-required, cursor-paginated fleet roster.

    ``limit`` defaults to 50 because this is an operator context payload, not
    an export; its maximum of 250 admits broad filtered coordination queries
    without reinstating a whole-ledger dump.  A full page carries a tie-safe
    ``next_cursor`` instead of refusing an append-mostly operator backlog.
    """
    try:
        result = list_session_rows(
            state,
            filters,
            live_only=live_only,
            limit=limit,
            after_created_at=after_created_at,
            after_id=after_id,
        )
    except SessionListError as exc:
        raise VerbError(exc.code, exc.message) from exc
    result["sessions"] = [_public_session_row(row) for row in result["sessions"]]
    return result


def session_status(state: StateManagementInterface, agent_instance_id: str) -> dict[str, Any]:
    """§4 aggregate ledger state plus honest tri-state native liveness."""
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    liveness, detail = _probe_host_liveness(row)
    result = _public_session_row(row)
    result.update(
        {
            "ledger_state": str(row.get("lifecycle_state") or ""),
            "host_liveness": liveness,
            "host_liveness_detail": detail,
            "host_liveness_observed_at": datetime.now(UTC).isoformat(),
            "state_consistent": not (liveness == "dead" and str(row.get("lifecycle_state") or "") not in _TERMINAL_STATES),
        },
    )
    if row.get("lifecycle_state") in _TERMINAL_STATES:
        result.update(read_host_attempt(state, agent_instance_id))
    return result


from .session_lifecycle_control import (  # noqa: E402, F401
    _REPORT_ALIVE_EDGE,  # pyright: ignore[reportUnusedImport]
    CLEAR_VERIFICATION_CONFIRMED,
    CLEAR_VERIFICATION_UNSUPPORTED,
    DRIVE_DRIVER_ERROR,
    DRIVE_DRIVER_NOT_NEEDED,
    DRIVE_DRIVER_QUEUE_FULL,
    DRIVE_DRIVER_SCHEDULED,
    DRIVE_DRIVER_SENT,
    DRIVE_DRIVER_UNAVAILABLE,
    DRIVE_INELIGIBLE_STATE,
    DRIVE_NOT_MANAGED,
    REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT,  # pyright: ignore[reportUnusedImport]
    REPORT_BY_SOURCE_OBSERVED_SPAWNING,  # pyright: ignore[reportUnusedImport]
    ArmSessionDependencyRequest,  # pyright: ignore[reportUnusedImport]
    DeliveryDrive,
    DriveOnDeliveryOutcome,
    _probe_host_liveness,
    _rearm_report_by,  # pyright: ignore[reportUnusedImport]
    _refuse_report_alive_on_ineligible_state,  # pyright: ignore[reportUnusedImport]
    _resolve_driver_channel,
    _resolve_termination_driver,  # pyright: ignore[reportUnusedImport]
    _row_report_by_seconds,  # pyright: ignore[reportUnusedImport]
    _send_driver_text,  # pyright: ignore[reportUnusedImport]
    _terminate_host,  # pyright: ignore[reportUnusedImport]
    _write_explicit_status_source,  # pyright: ignore[reportUnusedImport]
    arm_session_dependency,  # pyright: ignore[reportUnusedImport]
    clear_session,
    compact_session,
    dispatch_event_age_seconds,  # pyright: ignore[reportUnusedImport]
    drive_on_delivery,
    drive_session,
    read_host_attempt,
    report_alive,
    resolve_delivery_drive,
    retire_session,
    terminate_session,
)
from .session_lifecycle_control import (  # noqa: E402 -- same late control/spawn cycle as the reexports above
    refuse_if_local_name_held as _refuse_if_local_name_held,
)

__all__ = [
    "CLEAR_VERIFICATION_CONFIRMED",
    "CLEAR_VERIFICATION_UNSUPPORTED",
    "DRIVE_DRIVER_ERROR",
    "DRIVE_DRIVER_NOT_NEEDED",
    "DRIVE_DRIVER_QUEUE_FULL",
    "DRIVE_DRIVER_SCHEDULED",
    "DRIVE_DRIVER_SENT",
    "DRIVE_DRIVER_UNAVAILABLE",
    "DRIVE_INELIGIBLE_STATE",
    "DRIVE_NOT_MANAGED",
    "DeliveryDrive",
    "DriveOnDeliveryOutcome",
    "FIRST_TURN_SOURCE_FIRST_MESSAGE",
    "LIST_SESSIONS_DEFAULT_LIMIT",
    "LIST_SESSIONS_MAX_LIMIT",
    "SpawnSessionRequest",
    "VerbError",
    "clear_session",
    "compact_session",
    "drive_on_delivery",
    "drive_session",
    "list_sessions",
    "report_alive",
    "resolve_delivery_drive",
    "retire_session",
    "session_status",
    "spawn_session",
    "terminate_session",
]
