"""Control, liveness, and terminal lifecycle verbs split from spawn handling."""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal, Protocol, runtime_checkable

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
)
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)

from .lane_worktree_evidence import PacketCustody, RetirementCustodyContext, RetirementEvidenceReader
from .lane_worktrees import (
    LaneWorktreeError,
    active_lane_worktree_inventory,
    sweep_orphaned_lane_worktrees,
)
from .managed_dispatch import (
    DispatchError,
    read_managed_dispatch,
)
from .park_drive import (
    drive_session_channel,
    interrupt_parked_channel,
    send_delivery_notice,
)
from .register_unit_client import VerbError
from .schema import (
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_RETIRED,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_TRANSITION,
)
from .session_hosts import (
    DEFAULT_AGENT_RUNTIME,
    AgentRuntimeNotSupportedError,
    ClearVerifyingDriverChannel,
    DriverChannelSendError,
    DriveVerifyingDriverChannel,
    HostCannotSpawnError,
    HostMechanismMissingError,
    HostNotDeclaredError,
    ParkInterruptingDriverChannel,
    resolve_host_driver,
)
from .session_lifecycle_store import (
    DEFAULT_REPORT_BY_SECONDS,
    IllegalLifecycleTransitionError,
    SessionNotFoundError,
    StaleLifecycleStateError,
    list_managed_sessions,
    read_managed_session,
    transition_lifecycle_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .composer_preservation import DrainReceipt
    from .models import BridgeBinding
    from .peer_registry import PeerRegistry
    from .session_hosts import DriverChannel, HostDriver


from . import session_lifecycle_verbs as spawn_lifecycle

logger = logging.getLogger(__name__)
CLAUDE_PICKUP_PENDING: Final[str] = "durable mail retained; waiting for existing hook/next-turn inbox pickup"

_TERMINAL_STATES = frozenset({LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED})


def _probe_host_liveness(  # pyright: ignore[reportUnusedFunction]
    row: Mapping[str, Any],
) -> tuple[str, str]:
    """Probe alive/dead/unknown without folding faults into either fact."""
    if row.get("host") == "operator":
        return "unknown", "operator host is unobservable; stop the process manually"
    host_ref = str(row.get("host_ref") or "")
    if not host_ref:
        return "unknown", "host_ref unavailable"
    try:
        driver, _resolved = resolve_host_driver(
            str(row.get("host") or ""),
            str(row.get("agent_runtime") or DEFAULT_AGENT_RUNTIME),
        )
        alive = driver.alive(host_ref)
    except Exception as exc:  # noqa: BLE001 — probe faults are durable unknown evidence
        return "unknown", f"{type(exc).__name__}: {exc}"
    if alive is True:
        return "alive", "driver.alive returned true"
    if alive is False:
        return "dead", "driver.alive returned false"
    return "unknown", "driver.alive returned no boolean observation"


_HOST_ATTEMPT_SCHEMA = "managed-session.host-attempt/v1"


@dataclass(frozen=True)
class HostAttempt:
    """Immutable host evidence carried by one successful transition's rationale."""

    agent_instance_id: str
    host: str
    host_ref: str
    action: str
    remedy: str | None
    observation: str
    observed_at: str
    provenance: str

    def reason(self, human_reason: str) -> str:
        return json.dumps(
            {"schema": _HOST_ATTEMPT_SCHEMA, "human_reason": human_reason, "attempt": dataclasses.asdict(self)},
            sort_keys=True,
        )


def _decode_host_attempt(reason: str, instance: str) -> HostAttempt:
    envelope = json.loads(reason)
    if not isinstance(envelope, dict) or envelope.get("schema") != _HOST_ATTEMPT_SCHEMA:
        raise ValueError("unversioned host attempt")
    if not isinstance(envelope.get("human_reason"), str):
        raise ValueError("missing human rationale")
    attempt = _host_attempt_fields(envelope.get("attempt"))
    _validate_host_attempt(attempt, instance)
    return attempt


def _host_attempt_fields(fields: object) -> HostAttempt:
    if not isinstance(fields, dict) or set(fields) != {field.name for field in dataclasses.fields(HostAttempt)}:
        raise ValueError("malformed host attempt fields")
    if any(not isinstance(value, str) for key, value in fields.items() if key != "remedy"):
        raise ValueError("host attempt text fields required")
    if fields["remedy"] is not None and not isinstance(fields["remedy"], str):
        raise ValueError("invalid remedy")
    return HostAttempt(**fields)


def _validate_host_attempt(attempt: HostAttempt, instance: str) -> None:
    if attempt.agent_instance_id != instance or not attempt.host or not attempt.provenance:
        raise ValueError("host attempt identity or provenance missing")
    if attempt.action not in {"terminated", "none_available", "not_attempted"}:
        raise ValueError("unknown host action")
    if attempt.observation not in {"alive", "dead", "unknown"}:
        raise ValueError("unknown observation")
    _validate_host_observation(attempt)


def _validate_host_observation(attempt: HostAttempt) -> None:
    if attempt.action == "none_available" and not attempt.remedy:
        raise ValueError("missing manual remedy")
    if attempt.observation != "unknown" and not attempt.host_ref:
        raise ValueError("observed host requires exact ref")
    if datetime.fromisoformat(attempt.observed_at).utcoffset() is None:
        raise ValueError("host observation requires aware time")


def read_host_attempt(state: StateManagementInterface, agent_instance_id: str) -> dict[str, Any]:
    """Read the latest termination rationale; never search past a newer unknown.

    Legacy and unreadable evidence remain readable/advisory. They cannot be
    converted to proof, and history is never corrected in place.
    """
    result: dict[str, Any] = {"prior_host_outcome": None, "prior_host_reason": None, "prior_host_advisory": None}
    try:
        rows = require_records(
            state.query_ordered(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_SESSION_TRANSITION,
                    "filters": {"agent_instance_id": agent_instance_id, "to_state": LIFECYCLE_TERMINATED, "is_deleted": 0},
                    "order_by": [["occurred_at", "desc"], ["id", "desc"]],
                    "limit": 1,
                },
            )
        )
    except Exception as exc:  # noqa: BLE001 -- evidence unavailable is an explicit advisory
        result["prior_host_advisory"] = f"prior_host_evidence_unreadable: {type(exc).__name__}: {exc}"
        return result
    if not rows:
        result["prior_host_advisory"] = "prior_host_evidence_unknown: no recorded termination attempt"
        return result
    row = rows[0]
    reason = row.get("reason")
    result["prior_host_reason"] = reason
    try:
        attempt = _decode_host_attempt(str(reason or ""), agent_instance_id)
    except (TypeError, ValueError) as exc:
        result["prior_host_advisory"] = f"prior_host_evidence_unknown: {exc}"
        return result
    result["prior_host_outcome"] = {**dataclasses.asdict(attempt), "transition_id": row.get("id")}
    return result


def refuse_if_local_name_held(
    state: StateManagementInterface,
    *,
    local_name: str,
    role_name: str,
    directed_by: str = "plugin:spawn_session",
) -> None:
    """W6 OPERATOR RULING (2026-08-14): a second spawn for a role that is
    already held is REFUSED, LOUDLY. Never a silent uniquifying suffix, never
    a silent eviction.

    A suffix would give the second process a different local name than the
    role it was requested under, so ``Git-Controller-2`` would answer to a
    name that no caller asked for. And a silent eviction would make a routine
    act destructive. So the only non-colliding answer is to refuse and name the
    incumbent.

    The incumbent is a non-terminal ``managed_session`` row sharing this
    ``local_name`` — the ledger, not the role binding, because the collision
    being prevented is two LOCAL PROCESSES answering to one name on one
    machine. That choice also keeps crash succession cheap for free: a
    crashed holder's row is observed and CAS-transitioned to ``terminated``
    here, after which the complete same-name set is read again. This
    mirrors the claim path's own posture, where a DEAD holder is deliberately
    still claimable (``role_claim.py``) — the refusal is about live
    collisions, not about corpses.
    """
    if not local_name:
        return
    # A stale observation is discarded in full; every retry rereads all rows
    # and probes again. Bounded churn remains the existing name collision.
    for _ in range(3):
        rows = _same_name_rows(state, local_name)
        if not rows:
            return
        _repair_dead_incumbents(state, rows, local_name, role_name, directed_by)
    raise VerbError("local_name_already_held", f"{local_name!r}: unresolved collision; incumbents changed during observation; retry with fresh evidence.")


def _same_name_rows(state: StateManagementInterface, local_name: str) -> list[dict[str, Any]]:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {"local_name": local_name, "is_deleted": 0},
        },
    )
    return [row for row in require_records(result) if row.get("lifecycle_state") not in _TERMINAL_STATES]


def _repair_dead_incumbents(
    state: StateManagementInterface,
    rows: list[dict[str, Any]],
    local_name: str,
    role_name: str,
    directed_by: str,
) -> None:
    for row in rows:
        expected = {key: row.get(key) for key in ("host", "host_ref", "lifecycle_state", "last_transition_at", "last_heartbeat_at")}
        observation, detail = _probe_host_liveness(row)
        if observation != "dead":
            _refuse_name_collision(row, local_name, role_name, observation, detail)
        attempt = HostAttempt(
            agent_instance_id=str(row["agent_instance_id"]),
            host=str(expected["host"]),
            host_ref=str(expected["host_ref"]),
            action="not_attempted",
            remedy=None,
            observation="dead",
            observed_at=datetime.now(UTC).isoformat(),
            provenance="spawn_collision_observed_dead",
        )
        try:
            transition_lifecycle_state(
                state,
                agent_instance_id=attempt.agent_instance_id,
                from_state=str(expected["lifecycle_state"]),
                to_state=LIFECYCLE_TERMINATED,
                directed_by=directed_by,
                reason=attempt.reason("same-name incumbent observed_dead"),
                expected_observation=expected,
            )
        except StaleLifecycleStateError:
            return
        except IllegalLifecycleTransitionError as exc:
            _refuse_name_collision(row, local_name, role_name, "unknown", str(exc))


def _refuse_name_collision(row: Mapping[str, Any], local_name: str, role_name: str, observation: str, detail: str) -> None:
    raise VerbError(
        "local_name_already_held",
        f"refusing to spawn a second session named {local_name!r}"
        f"{f' (role {role_name!r})' if role_name else ''}: "
        f"{row.get('agent_instance_id')} retains that name "
        f"(lifecycle_state={row.get('lifecycle_state')!r}, "
        f"host={row.get('host')!r}, host_ref={row.get('host_ref')!r}, "
        f"lane_id={row.get('lane_id')!r}, observed={observation!r}, detail={detail!r}). Two sessions answering to one "
        "local name collide silently — the git mutation guard matches the "
        "name exactly, so both would pass it. Terminate the incumbent "
        "first (this plugin's terminate_session verb, agent_instance_id="
        f"{row.get('agent_instance_id')}), then spawn the replacement. "
        "Spawning does NOT claim the durable role binding either way; the "
        "new worker claims it explicitly once it is up.",
    )


def _resolve_driver_channel(row: dict[str, Any]) -> DriverChannel:
    """Shared by ``clear_session``/``compact_session`` (AMEND 5b) and
    ``drive_session``: resolve the row's host driver and its live driver
    channel, or raise ``unsupported_on_host`` — a config/mechanism gap (no
    driver registered, or a registered driver with no channel for this
    host_ref, e.g. the degenerate ``operator`` driver), never a silent
    degradation."""
    if row.get("brief_ref") and not row.get("assignment"):
        raise VerbError("unit_assignment_historical", "This path-based session is read-only history; it cannot receive new work.")
    host = str(row.get("host") or "")
    agent_runtime = str(row.get("agent_runtime") or DEFAULT_AGENT_RUNTIME)
    agent_instance_id = str(row.get("agent_instance_id") or "")
    try:
        driver, _resolved_host = resolve_host_driver(host, agent_runtime)
    except (
        AgentRuntimeNotSupportedError,
        HostNotDeclaredError,
        HostMechanismMissingError,
    ) as exc:
        raise VerbError(
            "unsupported_on_host",
            f"host {host!r} for {agent_instance_id!r} has no driver in this build ({exc}) — driver-channel verbs (clear/compact/drive) are unavailable; use the manual equivalent for this session.",
        ) from exc
    channel = driver.driver_channel(str(row.get("host_ref") or ""))
    if channel is None:
        # A top-level import would close the cycle control -> delivery_drive ->
        # session_lifecycle_verbs -> control: delivery_drive imports names from
        # session_lifecycle_verbs at load, session_lifecycle_verbs imports names
        # from this module at load (its late re-export block), and this module
        # imports session_lifecycle_verbs at its own top.
        from .delivery_drive import OPERATOR_SEAT_MANUAL_WAKE  # noqa: PLC0415 -- lazy: see the cycle above

        raise VerbError(
            "unsupported_on_host",
            f"host {host!r} for {agent_instance_id!r} has no driver channel (degenerate driver, or the tracking process's memory doesn't recognize this host_ref, e.g. after a restart) — driver-channel verbs (clear/compact/drive) are unavailable; use the manual equivalent for this session: {OPERATOR_SEAT_MANUAL_WAKE}.",
        )
    return channel


def _send_driver_text(channel: DriverChannel, text: str) -> None:
    """Map an acknowledgement-capable channel failure to a stable verb error."""
    try:
        channel.send(text)
    except DriverChannelSendError as exc:
        raise VerbError("driver_delivery_failed", str(exc)) from exc


# Drive-on-delivery lane (2026-08-04): the ONLY states a delivery-driven
# notice may reach. Deliberately NOT delegated to ``_resolve_driver_channel``
# — that helper resolves host/driver/channel only and performs no
# lifecycle-state check of its own (parked/spawning/terminal rows all have a
# perfectly live channel; each *verb* owns its own state gate today, e.g.
# ``clear_session``/``drive_session``'s shared ``_TERMINAL_STATES`` check and
# ``drive_session``'s own parked -> live un-park). A generic delivery notice
# never drives a parked row; the sole narrow exception is a channel declaring
# the measured Codex parked-pane interrupt capability below.
_DRIVE_ON_DELIVERY_ELIGIBLE_STATES = frozenset(
    {LIFECYCLE_LIVE, LIFECYCLE_IDLE, LIFECYCLE_OVERDUE},
)

DriveOnDeliveryOutcome = Literal[
    "not_managed",
    "ineligible_state",
    "driver_unavailable",
    "driver_scheduled",
    "driver_queue_full",
    "driver_sent",
    "driver_error",
    "driver_pending",
    "driver_turn_observed",
]
DRIVE_NOT_MANAGED: Final[DriveOnDeliveryOutcome] = "not_managed"
DRIVE_INELIGIBLE_STATE: Final[DriveOnDeliveryOutcome] = "ineligible_state"
DRIVE_DRIVER_UNAVAILABLE: Final[DriveOnDeliveryOutcome] = "driver_unavailable"
# iss_ab680764: the managed-row lookup passed and the channel half of the drive
# was handed to the delivery-drive executor; it has NOT run yet. Never reported
# as ``driver_sent``, which only a finished channel send may claim.
DRIVE_DRIVER_SCHEDULED: Final[DriveOnDeliveryOutcome] = "driver_scheduled"
# iss_ab680764 (review D1): the delivery-drive queue was at its cap, so the
# drive was refused rather than queued behind a backlog it would outlive.
DRIVE_DRIVER_QUEUE_FULL: Final[DriveOnDeliveryOutcome] = "driver_queue_full"
DRIVE_DRIVER_SENT: Final[DriveOnDeliveryOutcome] = "driver_sent"
DRIVE_DRIVER_ERROR: Final[DriveOnDeliveryOutcome] = "driver_error"
DRIVE_DRIVER_PENDING: Final[DriveOnDeliveryOutcome] = "driver_pending"
DRIVE_DRIVER_TURN_OBSERVED: Final[DriveOnDeliveryOutcome] = "driver_turn_observed"


@runtime_checkable
class CodexDrainChannel(Protocol):
    """Delivery capability that preserves the composer around a non-interrupting doorbell."""

    def send_drain_notice(self, text: str) -> DrainReceipt: ...


def _sanitize_notice_label(label: str) -> str:
    """Collapse all whitespace (including newlines) to single spaces and
    strip the ends. The driver channel (tmux send-keys) is line-oriented — an
    interpolated sender label must never be able to smuggle a line break into
    the notice text."""
    return " ".join(label.split())


def _resolve_delivery_managed_session(
    state: StateManagementInterface,
    *,
    recipient_agent_instance_id: str,
    recipient_agent_session_id: str,
) -> tuple[dict[str, Any] | None, DriveOnDeliveryOutcome | None]:
    """Resolve a delivery target and retain why no row was selectable."""
    try:
        return read_managed_session(state, recipient_agent_instance_id), None
    except SessionNotFoundError:
        if not recipient_agent_session_id:
            return None, DRIVE_NOT_MANAGED
    except Exception:  # noqa: BLE001 — telemetry must not fail the actual send
        logger.warning(
            "drive_on_delivery: managed-session lookup failed for %s",
            recipient_agent_instance_id,
            exc_info=True,
        )
        return None, DRIVE_DRIVER_UNAVAILABLE
    try:
        matches = list_managed_sessions(
            state,
            {"agent_session_id": recipient_agent_session_id},
        )
    except Exception:  # noqa: BLE001 — optional best-effort side effect
        logger.warning(
            "drive_on_delivery: stable-session lookup failed for %s",
            recipient_agent_session_id,
            exc_info=True,
        )
        return None, DRIVE_DRIVER_UNAVAILABLE
    if len(matches) == 1:
        return matches[0], None
    if len(matches) > 1:
        logger.warning(
            "drive_on_delivery: stable session %s matched %d managed rows; refusing to guess",
            recipient_agent_session_id,
            len(matches),
        )
        return None, DRIVE_DRIVER_UNAVAILABLE
    return None, DRIVE_NOT_MANAGED


def _record_driver_error_detail(
    detail_sink: Callable[[str], None] | None,
    channel: DriverChannel,
    exc: Exception,
) -> None:
    """Preserve a driver-channel diagnostic for a payload-owning caller."""
    if detail_sink is not None:
        detail_sink(f"{type(channel).__name__}: {exc}")


def drive_on_delivery(
    state: StateManagementInterface | None,
    *,
    recipient_agent_instance_id: str,
    recipient_agent_session_id: str = "",
    sender_label: str,
    detail_sink: Callable[[str], None] | None = None,
) -> DriveOnDeliveryOutcome:
    """Best-effort waker for a managed recipient's driver channel (D2-window
    rider, drive-on-delivery lane, 2026-08-04). Called AFTER the durable
    persist and the existing notify from ``dispatch_peer_send`` /
    ``dispatch_role_send`` / the sweep's dependency-wake delivery — this is an
    extra nudge for a managed recipient, never the delivery itself (the
    durable thread copy / bridge event stays the single source of truth): it
    injects a short fixed notice, never the message body, and never touches
    ``report_by`` (that stays ``drive_session``'s own edge — an inbound
    delivery notice must not extend a report-or-die deadline).

    Returns one sender-visible discriminator without changing or failing the
    durable send: ``not_managed``, ``ineligible_state``,
    ``driver_unavailable``, ``driver_sent``, or ``driver_error``.

    Best-effort no-ops (never raises) when: ``state`` is ``None`` (state_service
    not yet bound — mirrors ``sweep_overdue_sessions``'s own optional-
    collaborator convention: a best-effort side-effect skips silently rather
    than hard-failing its caller's actual job); the recipient has no
    ``managed_session`` row at all (``SessionNotFoundError`` — a registration
    gap or legacy row, not an ordinary hand-launched session). A watcher
    registration deliberately has a different ``agent_instance_id`` from its
    spawn record, so a direct miss may reconcile by the exact stable
    ``agent_session_id`` backfilled at registration; zero or multiple matches
    still no-op rather than guessing. The row's
    ``lifecycle_state`` is not in :data:`_DRIVE_ON_DELIVERY_ELIGIBLE_STATES`;
    the row's host has no live driver channel (``VerbError`` from
    :func:`_resolve_driver_channel` — e.g. the degenerate ``operator`` host
    driver, or a driver whose in-memory tracking lost the ``host_ref`` across
    a restart); or the channel itself raises on ``send``. A fault here must
    never fail the caller's own send result and must never mask it — the
    caller's already-computed delivery outcome is untouched either way.
    """
    if state is not None:
        managed, _reason = _resolve_delivery_managed_session(
            state,
            recipient_agent_instance_id=recipient_agent_instance_id,
            recipient_agent_session_id=recipient_agent_session_id,
        )
        if managed is not None and managed.get("agent_runtime") == "claude_code" and _has_retained_claude_pickup(state, managed):
            if detail_sink is not None:
                detail_sink(CLAUDE_PICKUP_PENDING)
            return DRIVE_DRIVER_PENDING
    target = _look_up_delivery_target(
        state,
        recipient_agent_instance_id=recipient_agent_instance_id,
        recipient_agent_session_id=recipient_agent_session_id,
    )
    if not isinstance(target, _DeliveryTarget):
        return target
    return _drive_delivery_target(
        target,
        recipient_agent_instance_id=recipient_agent_instance_id,
        notice=_delivery_notice(sender_label),
        detail_sink=detail_sink,
    )


@dataclass(frozen=True, slots=True)
class _DeliveryTarget:
    """A managed row that passed the drive gate, with its live channel."""

    row: dict[str, Any]
    channel: DriverChannel
    parked: bool


def _delivery_notice(sender_label: str) -> str:
    return f"delivery waiting from {_sanitize_notice_label(sender_label)} — drain peer_inbox"


def _has_retained_claude_pickup(state: StateManagementInterface, row: Mapping[str, Any]) -> bool:
    """Guard legacy automatic callers that have no per-message drive reference.

    Their durable notifications stay unchanged. Current role bindings also
    protect a successor whose original pickup routing names the old holder.
    """
    from .delivery_drive import PICKUP_PAGE_LIMIT, TABLE_DELIVERY_PICKUP  # noqa: PLC0415

    routes = [("recipient_instance", str(row["agent_instance_id"]))]
    session = str(row.get("agent_session_id") or "")
    if session:
        routes.append(("recipient_session", session))
    for field, value in routes:
        if require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP,
                    "filters": {"pending": True, field: value},
                    "limit": 1,
                },
            )
        ):
            return True
    role_routes = [("agent_instance_id", str(row["agent_instance_id"]))]
    if session:
        role_routes.append(("agent_session_id", session))
    return any(_route_has_pending_role(state, field, value, PICKUP_PAGE_LIMIT) for field, value in role_routes)


def _route_has_pending_role(state: StateManagementInterface, field: str, value: str, limit: int) -> bool:
    from .delivery_drive import TABLE_DELIVERY_PICKUP  # noqa: PLC0415

    roles = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": "role_binding",
                "filters": {field: value},
                "limit": limit,
            },
        )
    )
    for role in roles:
        if require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_DELIVERY_PICKUP,
                    "filters": {"pending": True, "role_name": role["role"]},
                    "limit": 1,
                },
            )
        ):
            return True
    return len(roles) == limit


def _look_up_delivery_target(
    state: StateManagementInterface | None,
    *,
    recipient_agent_instance_id: str,
    recipient_agent_session_id: str,
) -> _DeliveryTarget | DriveOnDeliveryOutcome:
    """Read the managed row, apply the lifecycle gate, resolve the channel.

    A generic delivery notice never drives a parked row; the sole exception is
    a channel declaring the measured parked-pane interrupt capability.
    """
    if state is None:
        return DRIVE_DRIVER_UNAVAILABLE
    row, resolution_outcome = _resolve_delivery_managed_session(
        state,
        recipient_agent_instance_id=recipient_agent_instance_id,
        recipient_agent_session_id=recipient_agent_session_id,
    )
    if row is None:
        return resolution_outcome or DRIVE_DRIVER_UNAVAILABLE
    lifecycle_state = str(row.get("lifecycle_state") or "")
    parked = lifecycle_state == LIFECYCLE_PARKED
    if not parked and lifecycle_state not in _DRIVE_ON_DELIVERY_ELIGIBLE_STATES:
        return DRIVE_INELIGIBLE_STATE
    try:
        channel = _resolve_driver_channel(row)
    except VerbError:
        return DRIVE_DRIVER_UNAVAILABLE
    if parked and not isinstance(channel, ParkInterruptingDriverChannel):
        return DRIVE_INELIGIBLE_STATE
    return _DeliveryTarget(row=row, channel=channel, parked=parked)


def _drive_delivery_target(
    target: _DeliveryTarget,
    *,
    recipient_agent_instance_id: str,
    notice: str,
    detail_sink: Callable[[str], None] | None,
) -> DriveOnDeliveryOutcome:
    """Interrupt a parked pane if due, then send the notice; never raises."""
    if target.row.get("agent_runtime") == "codex":
        if not notice.startswith("Solet delivery doorbell (message reference "):
            notice = "Solet delivery doorbell (managed delivery). Run solet-bridge inbox to drain both direct and role sections. Read task authority from the durable inbox."
        return _send_codex_drain_notice(target.channel, notice, detail_sink)
    park_detail: str | None = None
    if target.parked:
        try:
            park_detail = interrupt_parked_channel(target.channel)
        except Exception as exc:  # noqa: BLE001 -- preserve the sender's durable delivery
            _record_driver_error_detail(detail_sink, target.channel, exc)
            return DRIVE_DRIVER_ERROR
    return send_delivery_notice(
        target.channel,
        row=target.row,
        recipient_agent_instance_id=recipient_agent_instance_id,
        notice=notice,
        detail_sink=detail_sink,
        park_detail=park_detail,
        driver_sent=DRIVE_DRIVER_SENT,
        driver_error=DRIVE_DRIVER_ERROR,
        record_error_detail=lambda sink, exc: _record_driver_error_detail(
            sink,
            target.channel,
            exc,
        ),
        logger=logger,
    )


def _send_codex_drain_notice(
    channel: DriverChannel,
    notice: str,
    detail_sink: Callable[[str], None] | None,
) -> DriveOnDeliveryOutcome:
    if not isinstance(channel, CodexDrainChannel):
        if detail_sink is not None:
            detail_sink("pending: current Codex driver has no safe idle drain capability")
        return DRIVE_DRIVER_UNAVAILABLE
    try:
        receipt = channel.send_drain_notice(notice)
    except Exception as exc:  # noqa: BLE001 -- retain the caller's durable mail
        _record_driver_error_detail(detail_sink, channel, exc)
        return DRIVE_DRIVER_ERROR
    if detail_sink is not None:
        detail_sink(receipt.detail())
    evidence = receipt.evidence
    if evidence == "turn_observed":
        return DRIVE_DRIVER_TURN_OBSERVED
    if evidence == "submitted":
        return DRIVE_DRIVER_SENT
    if evidence == "submitted_no_turn":
        return DRIVE_DRIVER_ERROR
    return DRIVE_DRIVER_PENDING


@dataclass(frozen=True, slots=True)
class DeliveryDrive:
    """A drive-on-delivery whose send-time lookup passed, to run later.

    iss_ab680764: :func:`resolve_delivery_drive` answers the sender at once;
    :meth:`run` does what can take seconds — the parked-pane interrupt and the
    channel send with its paste wait — on the delivery-drive executor. A
    queued drive may run seconds after the send, so :meth:`run` repeats the
    WHOLE lookup (managed row, lifecycle gate, parked capability, channel)
    immediately before it touches the pane: a recipient parked, retired or
    gone while the drive waited gets that lookup's non-driven outcome, never
    ``driver_sent``.
    """

    state: StateManagementInterface
    recipient_agent_instance_id: str
    recipient_agent_session_id: str
    notice: str
    recipient_role_name: str = ""
    pickup_message_id: str = ""
    initial_delivery: bool = True

    def run(
        self,
        detail_sink: Callable[[str], None] | None = None,
    ) -> DriveOnDeliveryOutcome:
        """Re-check the gate, then interrupt/send; never raises."""
        recipient = _current_delivery_recipient(self)
        if recipient is None:
            if detail_sink is not None:
                detail_sink("durable mail retained; current role recipient unresolved")
            return DRIVE_DRIVER_PENDING
        instance, session = recipient
        managed, _reason = _resolve_delivery_managed_session(
            self.state,
            recipient_agent_instance_id=instance,
            recipient_agent_session_id=session,
        )
        if _claude_drive_pending(self, managed):
            if detail_sink is not None:
                detail_sink(CLAUDE_PICKUP_PENDING)
            return DRIVE_DRIVER_PENDING
        target = _look_up_delivery_target(
            self.state,
            recipient_agent_instance_id=instance,
            recipient_agent_session_id=session,
        )
        if not isinstance(target, _DeliveryTarget):
            if detail_sink is not None:
                detail_sink(f"run-time re-check refused the queued drive: {target}")
            return target
        return _drive_delivery_target(
            target,
            recipient_agent_instance_id=instance,
            notice=self.notice,
            detail_sink=detail_sink,
        )

    def _claim_first_claude_doorbell(self) -> bool:
        """CAS before input: a crash or explicit reopen never repeats the doorbell."""
        if not self.initial_delivery:
            return False
        if not self.pickup_message_id:
            row, _reason = _resolve_delivery_managed_session(
                self.state,
                recipient_agent_instance_id=self.recipient_agent_instance_id,
                recipient_agent_session_id=self.recipient_agent_session_id,
            )
            return row is not None and not _has_retained_claude_pickup(self.state, row)
        from .delivery_drive import CodexPickupStore  # noqa: PLC0415 -- shared pickup lifecycle

        store = CodexPickupStore(self.state)
        row = store.read(self.pickup_message_id)
        return bool(row is not None and row["pending"] and row["version"] == 1 and store.change(row, phase="driver_pending", reason=CLAUDE_PICKUP_PENDING, lease_until=None) is not None)


def _claude_drive_pending(drive: DeliveryDrive, row: Mapping[str, Any] | None) -> bool:
    return bool(row is not None and row.get("agent_runtime") == "claude_code" and not drive._claim_first_claude_doorbell())  # noqa: SLF001 -- same module's drive boundary


def _current_delivery_recipient(drive: DeliveryDrive) -> tuple[str, str] | None:
    if not drive.recipient_role_name:
        return drive.recipient_agent_instance_id, drive.recipient_agent_session_id
    from .role_binding_store import resolve_role_binding  # noqa: PLC0415

    try:
        role = resolve_role_binding(drive.state, drive.recipient_role_name)
        if role.agent_id in {"codex", "claude_code"}:
            return role.agent_instance_id, role.agent_session_id
    except Exception:  # noqa: BLE001 -- wrong-recipient uncertainty never touches a pane
        logger.warning("Managed delivery role refresh failed", exc_info=True)
    return None


def resolve_delivery_drive(
    state: StateManagementInterface | None,
    *,
    recipient_agent_instance_id: str,
    recipient_agent_session_id: str = "",
    sender_label: str,
    pickup_notice: str = "",
    recipient_role_name: str = "",
    pickup_message_id: str = "",
    initial_delivery: bool = True,
) -> DeliveryDrive | DriveOnDeliveryOutcome:
    """The send-time half of :func:`drive_on_delivery`.

    Returns the final outcome when the lookup already decides it
    (``driver_unavailable``, ``not_managed``, ``ineligible_state``), else the
    :class:`DeliveryDrive` to run later.
    """
    if state is None:
        return DRIVE_DRIVER_UNAVAILABLE
    target = _look_up_delivery_target(
        state,
        recipient_agent_instance_id=recipient_agent_instance_id,
        recipient_agent_session_id=recipient_agent_session_id,
    )
    if not isinstance(target, _DeliveryTarget):
        return target
    return DeliveryDrive(
        state=state,
        recipient_agent_instance_id=recipient_agent_instance_id,
        recipient_agent_session_id=recipient_agent_session_id,
        notice=pickup_notice or _delivery_notice(sender_label),
        recipient_role_name=recipient_role_name,
        pickup_message_id=pickup_message_id,
        initial_delivery=initial_delivery,
    )


def notify_steward_of_managed_dispatch(
    state: StateManagementInterface,
    *,
    peer_registry: PeerRegistry | None,
    bridge_manager: BridgeSessionManager | None,
    dispatch_id: str,
    condition: str,
    next_required_action: str,
    event_type: str,
) -> bool:
    """Best-effort one-shot steward wake for a durable dispatch notice."""
    if peer_registry is None or bridge_manager is None:
        return False
    try:
        row = read_managed_dispatch(state, dispatch_id)
    except DispatchError:
        return False
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not spawner_instance_id:
        return False
    from .steward_resolution import resolve_steward_binding

    binding = resolve_steward_binding(
        state=state,
        peer_registry=peer_registry,
        spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning(
            "managed dispatch %s condition %s: steward %s is not reachable",
            dispatch_id,
            condition,
            spawner_instance_id,
        )
        return False
    prose = f"managed_dispatch_notice: dispatch {dispatch_id} has condition {condition!r}; next_required_action={next_required_action!r}. Read managed_dispatch_status and make the named decision."
    try:
        bridge_manager.append_event(
            binding.bridge_id,
            event_type,
            prose,
            {"flow_id": f"managed-dispatch-{dispatch_id}-{condition}"},
        )
    except Exception:  # noqa: BLE001 — durable event already exists
        logger.warning(
            "managed dispatch %s notice append failed",
            dispatch_id,
            exc_info=True,
        )
        return False
    drive_on_delivery(
        state,
        recipient_agent_instance_id=spawner_instance_id,
        sender_label=event_type,
    )
    return True


def dispatch_event_age_seconds(value: object, now: datetime) -> float | None:
    """Project one durable dispatch timestamp into a bounded current age."""
    if not value:
        return None
    observed_at = datetime.fromisoformat(str(value))
    if observed_at.tzinfo is None:
        raise ValueError("dispatch event timestamp must include a timezone")
    return max(0.0, (now.astimezone(UTC) - observed_at.astimezone(UTC)).total_seconds())


CLEAR_VERIFICATION_CONFIRMED = "confirmed"
CLEAR_VERIFICATION_UNSUPPORTED = "unsupported_on_driver"


def _verify_clear_effect(channel: DriverChannel, agent_instance_id: str) -> str:
    """Ask the channel whether the ``/clear`` ACTUALLY took effect (GAU-09).

    Returns the verification token for the result envelope, or raises
    ``clear_unverified`` when a channel that CAN see its target looked and
    did not find a cleared state.

    The three-way split is the whole point, and collapsing any two of them
    reintroduces the defect:

    * verified true  -> ``confirmed``: a real measurement.
    * verified false -> RAISE ``clear_unverified``. The send happened and
      the effect did not, so a success-shaped return here would be
      precisely the GAU-09 lie (measured ``success TRUE`` for a ``/clear``
      that never happened).

      ★ UNVERIFIED IS NOT LOST, and the difference is the whole of GAU-27
      (measured 2026-08-19): a ``/clear`` that lands while the target is
      MID-TURN is queued by the target AS A COMMAND and fires at that
      turn's end -- ~2 minutes after this raise, in the measured case.
      So this one raise covers two different worlds, never-arrived and
      not-yet-executed, and NOTHING here can tell them apart: separating
      them needs a positive read of the target's input QUEUE (the pane
      shows the queued ``/clear`` and "Press up to edit queued messages"),
      which no driver-channel surface in this build exposes -- the target
      cannot see its own input queue either, so it is no help. Until a
      channel can report that state (then this becomes a four-way split
      with a distinct ``clear_deferred``), the message below carries the
      ambiguity and the external protocol that resolves it, and asserts no
      mechanism it has not measured.
    * no read-back surface -> ``unsupported_on_driver``: an honest "cannot
      know", never a quiet success. Distinct from the case above because a
      driver that never looked and a driver that looked and saw nothing are
      different facts with different fixes -- the same tri-state discipline
      this plugin already enforces on the gauge's cache columns.

    ★ NO RETRY, EVER. The failure path raises without re-sending anything.
    Each ``/clear`` fire deposits real text into a live input buffer, so a
    blind retry converts one stranded line into two and can never confirm
    itself.
    """
    if not isinstance(channel, ClearVerifyingDriverChannel):
        return CLEAR_VERIFICATION_UNSUPPORTED
    if channel.verify_cleared():
        return CLEAR_VERIFICATION_CONFIRMED
    raise VerbError(
        "clear_unverified",
        f"the /clear for {agent_instance_id!r} was DISPATCHED but its effect could "
        "not be confirmed inside the verifier's window — the driver read the target "
        "back and never observed a cleared state. UNVERIFIED IS NOT LOST: measured "
        "2026-08-19 (GAU-27), a /clear that arrives while the target is mid-turn is "
        "QUEUED BY THE TARGET AS A COMMAND and fires at that turn's end, ~2 minutes "
        "after this error in the measured case, so this error covers both "
        "never-arrived and not-yet-executed and cannot distinguish them. Do NOT "
        "re-send it: the text is already in that session's input buffer, and a blind "
        "retry deposits a second copy that ALSO fires — into the successor context, "
        "after the first one clears — while still being unable to confirm itself. "
        "The protocol that worked: read the pane EXTERNALLY (the target cannot see "
        "its own input queue). If the /clear is sitting queued — the pane shows it "
        'with "Press up to edit queued messages" — the clear is PENDING, so wait '
        "for the queue to drain and the pane to go idle; then ONE re-issue into the "
        "now-idle session verifies cleanly and takes the park edge.",
    )


DRIVE_VERIFICATION_CONFIRMED = "confirmed"
DRIVE_VERIFICATION_UNSUPPORTED = "unsupported_on_driver"


def _verify_drive_effect(
    channel: DriverChannel,
    agent_instance_id: str,
    text: str,
) -> str:
    """Ask the channel whether a ``drive_session`` dispatch was actually
    taken up as a turn (public issue #9, the ``drive_session`` sibling of
    GAU-09's ``_verify_clear_effect``).

    Same three-way split, same reason collapsing any two of them
    reintroduces the defect:

    * verified true  -> ``confirmed``: a real measurement that the driven
      text left the composer without ever being observed stranded there.
    * verified false -> RAISE ``drive_unverified``. The send happened and
      the effect did not: a success-shaped return here is precisely the
      ARMED != FIRED lie this closes.
    * ``None`` (could not determine — no positive signal either way) ->
      ``unsupported_on_driver`` for a channel with no read-back surface at
      all; a channel that COULD look but the deadline passed without
      either signal is a different, narrower case folded into the same
      raise below, since a caller cannot act on it any differently than a
      confirmed-stranded result: either way, do not assume the drive ran.

    ★ NO RETRY, EVER. Each ``drive_session`` fire deposits real text into a
    live input buffer; a blind retry converts one stranded line into two
    and can never confirm itself.
    """
    if not isinstance(channel, DriveVerifyingDriverChannel):
        return DRIVE_VERIFICATION_UNSUPPORTED
    result = channel.verify_driven(text)
    if result is True:
        return DRIVE_VERIFICATION_CONFIRMED
    raise VerbError(
        "drive_unverified",
        f"the drive for {agent_instance_id!r} was DISPATCHED but its effect could "
        "not be confirmed — the driver read the target back and never observed the "
        "driven text leaving the composer" + (" (it is sitting there stranded)." if result is False else " (deadline passed).") + " The text is already in that session's input buffer: do NOT re-send it "
        "(a blind retry deposits a second copy and still cannot confirm itself).",
    )


def _finish_drive_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    current: str,
    directed_by: str,
    submitted: bool | None,
    verification: str,
    park_detail: str | None,
) -> dict[str, Any]:
    """Return the verified drive result and take the sole parked->live edge."""
    if current != LIFECYCLE_PARKED:
        return {
            "lifecycle_state": current,
            "unparked": False,
            "dispatched": True,
            "submitted": submitted,
            "drive_verification": verification,
            "drive_on_delivery_detail": park_detail,
        }
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_PARKED,
            to_state=LIFECYCLE_LIVE,
            directed_by=directed_by,
            reason="drive_session dispatch",
        )
    except IllegalLifecycleTransitionError as exc:
        raise VerbError("illegal_lifecycle_transition", str(exc)) from exc
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    return {
        "lifecycle_state": LIFECYCLE_LIVE,
        "unparked": True,
        "dispatched": True,
        "submitted": submitted,
        "drive_verification": verification,
        "drive_on_delivery_detail": park_detail,
    }


def clear_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    park: bool,
    directed_by: str,
) -> dict[str, Any]:
    """§4 ``clear_session`` (AMEND 5b) — context hygiene via the host
    driver's driver channel, WITH EFFECT VERIFICATION where the driver can
    provide it (GAU-09, 2026-08-18).

    ★ THIS VERB USED TO REPORT THE SEND IN A RETURN VALUE SHAPED LIKE A
    VERDICT. Measured 2026-08-18: it returned ``success TRUE
    {'lifecycle_state': 'live', 'parked': False}`` for a ``/clear`` that
    provably never happened, because ``send()`` alone can only ever mean
    "bytes left" -- ``ARMED != FIRED``. The result now separates what is
    KNOWN from what is MEASURED:

    * ``dispatched`` -- the send succeeded. Always ``True`` on a return
      (a failed send raises ``driver_delivery_failed``).
    * ``cleared`` -- ``True`` only on a POSITIVE observation of a cleared
      state; ``None`` when this driver has no read-back surface. Never
      ``False`` on a return: a driver that looked and saw nothing RAISES
      ``clear_unverified`` instead, so ``success`` can never accompany an
      unconfirmed clear.
    * ``clear_verification`` -- ``confirmed`` or ``unsupported_on_driver``,
      naming WHICH of those two states produced ``cleared``.

    WHICH DRIVERS GET WHICH, measured against this build: the ``tmux``
    channel reads its pane with ``capture-pane`` and is VERIFIED; the
    ``headless`` stream-json channel writes to a stdin pipe with no pane to
    read and is ``unsupported_on_driver``; the ``operator`` driver has no
    channel at all and still fails earlier with ``unsupported_on_host``.

    ``park=True`` additionally drives ``live/idle/overdue -> parked`` (§3.2
    matrix, L3 rule 2, steward direction) — the ONLY writer of that edge —
    and is NOT reached when verification failed, so an unproven clear can
    never leave a park behind in the ledger to outlive the return value that
    carried it. A driver that simply cannot verify still parks: refusing
    that would strand every headless session unparkable over a measurement
    that was never available.

    ★ WHAT A PARK ACTUALLY BUYS, measured 2026-08-19 on ``lane-seed-remint``
    (LIF-04) — read this before building anything that keys on it. Park writes
    a ROW, and the row governs the HEARTBEAT CONTRACT ONLY: ``report_alive``
    is refused (``lifecycle_state_conflict``) and ``report_by`` dissolves. The
    PANE does not park. The stop-hook wake path and every messaging verb stay
    live, and the parked session was observed waking, reading ``peer_inbox``
    from the parked row, self-orienting and SENDING a full report. Two
    consequences, in the directions people actually get wrong:

    * a parked lane still wakes on every message addressed to it, and each
      wake is a full-price turn, so DORMANCY IS THE SENDER'S JOB — park alone
      does not buy quiet;
    * a message arriving FROM a lane is NOT evidence that it un-parked, so no
      un-park verifier, sweep or playbook may key on that.

    The one waker this module owns already refuses parked rows
    (:data:`_DRIVE_ON_DELIVERY_ELIGIBLE_STATES`, re-verified as refusing
    during LIF-04's residual read); the wake that WAS measured came from the
    stop-hook/watcher path, which is outside this module.

    Errors: ``session_not_found``, ``lifecycle_state_conflict`` (terminal
    rows never receive driver-channel commands), ``unsupported_on_host``,
    ``driver_delivery_failed``, ``clear_unverified`` (dispatched, effect not
    observed — DO NOT RETRY, and NOT the same as lost: a mid-turn target
    queues the /clear and fires it at its own turn end, GAU-27),
    ``illegal_lifecycle_transition``,
    ``stale_lifecycle_state`` (a sweep or another verb raced the row between
    the read above and the park transition)."""
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    current = str(row.get("lifecycle_state") or "")
    if current in _TERMINAL_STATES:
        raise VerbError(
            "lifecycle_state_conflict",
            f"clear_session arrived on a {current!r} row — terminal rows never receive driver-channel commands.",
        )
    channel = _resolve_driver_channel(row)
    _send_driver_text(channel, "/clear")
    verification = _verify_clear_effect(channel, agent_instance_id)
    cleared = True if verification == CLEAR_VERIFICATION_CONFIRMED else None
    if not park:
        return {
            "lifecycle_state": current,
            "parked": False,
            "dispatched": True,
            "cleared": cleared,
            "clear_verification": verification,
        }
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=current,
            to_state=LIFECYCLE_PARKED,
            directed_by=directed_by,
            reason="clear_session(park=True)",
        )
    except IllegalLifecycleTransitionError as exc:
        raise VerbError("illegal_lifecycle_transition", str(exc)) from exc
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    return {
        "lifecycle_state": LIFECYCLE_PARKED,
        "parked": True,
        "dispatched": True,
        "cleared": cleared,
        "clear_verification": verification,
    }


def compact_session(state: StateManagementInterface, *, agent_instance_id: str) -> dict[str, Any]:
    """§4 ``compact_session`` (AMEND 5b) — context hygiene via the driver
    channel (sends ``/compact``, fire-and-forget). No park mode — only
    ``clear_session`` drives that edge (§3.2), so unlike every other mutating
    verb this one performs no lifecycle transition and takes no
    ``directed_by`` (nothing for it to audit). Errors: ``session_not_found``,
    ``lifecycle_state_conflict``, ``unsupported_on_host``."""
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    current = str(row.get("lifecycle_state") or "")
    if current in _TERMINAL_STATES:
        raise VerbError(
            "lifecycle_state_conflict",
            f"compact_session arrived on a {current!r} row — terminal rows never receive driver-channel commands.",
        )
    channel = _resolve_driver_channel(row)
    _send_driver_text(channel, "/compact")
    return {"lifecycle_state": current}


def drive_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    text: str,
    directed_by: str,
) -> dict[str, Any]:
    """``drive_session`` (D2-window rider, 2026-08-04) — dispatch a work turn
    into a managed session through the host driver's driver channel, WITH
    EFFECT VERIFICATION where the driver can provide it (public issue #9,
    2026-08-19, the sibling fix to GAU-09's ``clear_session``). The
    bootstrap verb for seat-managed dispatch: ``spawn_session`` boots a
    worker with NO first turn, so this is the only sanctioned way work
    reaches it.

    ★ THIS VERB USED TO REPORT THE SEND IN A RETURN VALUE SHAPED LIKE A
    VERDICT. Measured 2026-08-18 (backlog.md GAU-09): it returned
    ``success TRUE {'lifecycle_state': 'live', 'unparked': False}`` for a
    drive whose text sat unsubmitted in the target's input buffer, because
    ``send()`` alone can only ever mean "bytes left" -- ``ARMED != FIRED``.
    The result now separates what is KNOWN from what is MEASURED:

    * ``dispatched`` -- the send succeeded. Always ``True`` on a return (a
      failed send raises ``driver_delivery_failed``).
    * ``submitted`` -- ``True`` only on a POSITIVE observation that the
      driven text left the composer without ever being seen stranded
      there; ``None`` when this driver has no read-back surface. Never
      ``False`` on a return: a driver that looked and found it stranded
      RAISES ``drive_unverified`` instead, so ``success`` can never
      accompany an unconfirmed drive. ``submitted=True`` means the text was
      taken up as a turn BY THIS PANE, nothing more -- it is not evidence
      the model acted on it, only that delivery to the surface succeeded
      (delivery to a bridge/pane is not delivery to a model).
    * ``drive_verification`` -- ``confirmed`` or ``unsupported_on_driver``,
      naming WHICH of those two states produced ``submitted``.

    WHICH DRIVERS GET WHICH, measured against this build: the ``tmux``
    channel reads its pane with ``capture-pane -e`` (colour-preserving) and
    is VERIFIED; the ``headless`` stream-json channel writes to a stdin
    pipe with no pane to read and is ``unsupported_on_driver``; the
    ``operator`` driver has no channel at all and still fails earlier with
    ``unsupported_on_host``.

    Owns the §3.2 ``parked -> live`` edge ("new dispatch through the driver
    channel, steward") — driving a parked row un-parks it. Every other
    non-terminal state is legal and untouched: ``spawning`` (dispatch-at-spawn;
    the registration hook still owns ``spawning -> live``), ``live``/``idle``
    (``report_alive``'s edge), ``overdue`` (the worker's own late report
    recovers it). Re-arms ``report_by`` on every dispatch — new work grants a
    fresh report-or-die window, so a worker driven seconds before its deadline
    is not marked overdue while it works. The unpark transition is NOT
    reached when verification failed, so an unproven drive can never leave
    an unpark behind in the ledger to outlive the return value that carried
    it — mirroring ``clear_session``'s ``park`` posture exactly. A driver
    that simply cannot verify still unparks: refusing that would strand
    every headless/parked session over a measurement that was never
    available.

    Errors: ``empty_text`` (nothing to dispatch — fast-fail before any read),
    ``session_not_found``, ``lifecycle_state_conflict`` (terminal rows never
    receive driver-channel commands), ``unsupported_on_host``,
    ``driver_delivery_failed``, ``drive_unverified`` (dispatched, effect not
    observed — DO NOT RETRY), ``illegal_lifecycle_transition``,
    ``stale_lifecycle_state`` (the predicated un-park write lost a race)."""
    if not text.strip():
        raise VerbError("empty_text", "drive_session requires non-empty text to dispatch.")
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    current = str(row.get("lifecycle_state") or "")
    if current in _TERMINAL_STATES:
        raise VerbError(
            "lifecycle_state_conflict",
            f"drive_session arrived on a {current!r} row — terminal rows never receive driver-channel commands.",
        )
    verification, park_detail = drive_session_channel(
        _resolve_driver_channel(row),
        current=current,
        parked_state=LIFECYCLE_PARKED,
        text=text,
        agent_instance_id=agent_instance_id,
        send_driver_text=_send_driver_text,
        verify_drive_effect=_verify_drive_effect,
    )
    submitted = True if verification == DRIVE_VERIFICATION_CONFIRMED else None
    if verification == DRIVE_VERIFICATION_CONFIRMED:
        _rearm_report_by(
            state,
            agent_instance_id,
            report_by_seconds=_row_report_by_seconds(row),
            source=REPORT_BY_SOURCE_CONFIRMED_DRIVE,
        )
    return _finish_drive_session(
        state,
        agent_instance_id=agent_instance_id,
        current=current,
        directed_by=directed_by,
        submitted=submitted,
        verification=verification,
        park_detail=park_detail,
    )


DEFAULT_TERMINATE_GRACE_SECONDS = 30

# What a terminate/retire call did to the HOST process, reported so a
# ``completed`` result can never read as "the process is gone" when it is not
# (iss_7ee6fb98: 20 hand-launched lanes were retired in the ledger, returned
# ``completed``, and kept running and registered).
HOST_ACTION_TERMINATED = "terminated"
HOST_ACTION_NONE_AVAILABLE = "none_available"
HOST_ACTION_NOT_ATTEMPTED = "not_attempted"


def _host_result(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    *,
    action: str = HOST_ACTION_NOT_ATTEMPTED,
    remedy: str | None = None,
    observation: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Current-call action, fresh native observation and separate durable history."""
    liveness, detail = observation if observation is not None else _probe_host_liveness(row)
    return {
        "host": str(row.get("host") or ""),
        "host_ref": str(row.get("host_ref") or ""),
        "host_action": action,
        "host_remedy": remedy,
        "host_liveness": liveness,
        "host_liveness_detail": detail,
        "host_liveness_observed_at": datetime.now(UTC).isoformat(),
        "registry_checked": False,
        "teardown_complete": False,
        **read_host_attempt(state, str(row.get("agent_instance_id") or "")),
    }


def _resolve_termination_driver(
    row: Mapping[str, object],
    agent_instance_id: str,
) -> tuple[HostDriver, str]:
    host = str(row.get("host") or "")
    agent_runtime = str(row.get("agent_runtime") or DEFAULT_AGENT_RUNTIME)
    try:
        driver, _resolved_host = resolve_host_driver(host, agent_runtime)
    except (
        AgentRuntimeNotSupportedError,
        HostNotDeclaredError,
        HostMechanismMissingError,
    ) as exc:
        raise VerbError(
            "unsupported_on_host",
            f"host {host!r} for {agent_instance_id!r} has no driver in this build ({exc}) — terminate_session cannot reach the host; stop the process manually.",
        ) from exc
    return driver, host


def _terminate_host(
    driver: HostDriver,
    *,
    host_ref: str,
    grace_seconds: int,
    agent_instance_id: str,
    host: str,
) -> str | None:
    """End the host process; ``None`` when the driver did, else the driver's
    own remedy text for a degenerate driver that cannot (the ledger-only path).
    """
    try:
        driver.terminate(host_ref, grace_seconds)
    except HostCannotSpawnError as exc:
        logger.info(
            "terminate_session %s: host %r driver is degenerate (no spawn, no kill) — proceeding with the ledger-only transition.",
            agent_instance_id,
            host,
        )
        return exc.remedy
    return None


def terminate_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    directed_by: str,
    grace_seconds: int = DEFAULT_TERMINATE_GRACE_SECONDS,
) -> dict[str, Any]:
    """Invoke the existing driver, observe it, then transition the ledger.

    A driver return is an invocation outcome, not proof of death. Operator
    hosts retain their ledger-only path and manual remedy. The successful
    CAS appends a versioned attempt to the transition reason; failed CAS
    cannot attach evidence to another writer, and audit failure stays loud.
    Terminal repeats make no host invocation, read prior immutable evidence
    and observe liveness afresh. Unknown legacy history is advisory.

    This function alone fires and best-effort delivers session_terminal
    dependencies, both after a transition and on terminal repeats (for edges
    armed later). Their fired_at-is-null CAS preserves exactly-once delivery.
    Registry observations belong to the transport wrapper; the pure result
    leaves registry_checked and teardown_complete false until that read.
    """
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    current = str(row.get("lifecycle_state") or "")
    if current in _TERMINAL_STATES:
        fired = _fire_session_terminal_dependencies(
            state,
            agent_instance_id=agent_instance_id,
            fired_at=datetime.now(UTC).isoformat(),
        )
        return {
            "already_terminal": True,
            "lifecycle_state": current,
            "session_terminal_edges_fired": fired,
            **_host_result(state, row),
        }
    driver, host = _resolve_termination_driver(row, agent_instance_id)
    host_remedy = _terminate_host(
        driver,
        host_ref=str(row.get("host_ref") or ""),
        grace_seconds=grace_seconds,
        agent_instance_id=agent_instance_id,
        host=host,
    )
    observation = _probe_host_liveness(row)
    action = HOST_ACTION_TERMINATED if host_remedy is None else HOST_ACTION_NONE_AVAILABLE
    attempt = HostAttempt(
        agent_instance_id=agent_instance_id,
        host=host,
        host_ref=str(row.get("host_ref") or ""),
        action=action,
        remedy=host_remedy,
        observation=observation[0],
        observed_at=datetime.now(UTC).isoformat(),
        provenance="terminate_session",
    )
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=current,
            to_state=LIFECYCLE_TERMINATED,
            directed_by=directed_by,
            reason=attempt.reason("terminate_session"),
        )
    except IllegalLifecycleTransitionError as exc:
        raise VerbError("illegal_lifecycle_transition", str(exc)) from exc
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    fired = _fire_session_terminal_dependencies(
        state,
        agent_instance_id=agent_instance_id,
        fired_at=datetime.now(UTC).isoformat(),
    )
    return {
        "already_terminal": False,
        "lifecycle_state": LIFECYCLE_TERMINATED,
        "session_terminal_edges_fired": fired,
        **_host_result(state, row, action=action, remedy=host_remedy, observation=observation),
    }


def _fire_session_terminal_dependencies(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    fired_at: str,
) -> int:
    """Fire (once) every armed ``session_terminal`` dependency edge waiting
    on ``agent_instance_id`` — guarded by ``fired_at IS NULL`` so re-running
    the caller never double-fires. Best-effort ``drive_on_delivery`` per
    fired edge (2026-08-04, acceptance Test C completion, coordinator-seat ruling):
    firing WITHOUT delivery is the Phase B design's own named anti-pattern
    ("armed-but-never-evaluated is worse than no mechanism") — an edge that
    silently stamps ``fired_at`` leaves its waiter parked forever with no
    signal. ``drive_on_delivery`` (defined earlier in this module) already
    never raises by its own contract, so no extra try/except is needed
    here for the containment promise ("a delivery fault must never fail
    terminate/retire").

    The predicated update is checked (``require_updated``): a 0-row result
    means another caller already claimed this edge (a lost race, e.g. two
    concurrent ``terminate_session`` calls) — skipped, never counted,
    never double-delivered. Sole caller: :func:`terminate_session`, at
    BOTH the state-transition success path and the already-terminal
    catch-up path (an edge armed after the session already died) —
    ``retire_session`` composes ``terminate_session`` and no longer fires
    these itself (single call site, per the coordinator seat's ruling 2026-08-04).

    ``{"op": "is_null"}``, NEVER a bare ``None`` filter value: a bare
    ``None`` compiles to SQL ``col = NULL``, which the postgres provider's
    own placeholder binding renders as a literal NULL comparison — always
    UNKNOWN/false in SQL, matching ZERO rows, silently, forever. Measured
    live 2026-08-04 (acceptance Test C): this function had never actually
    fired a ``session_terminal`` edge in production before this fix — the
    query below found nothing because ``"fired_at": None`` matched no row,
    not because none were armed."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_SESSION_DEPENDENCY,
            "filters": {
                "condition_kind": CONDITION_SESSION_TERMINAL,
                "condition_ref": agent_instance_id,
                "fired_at": {"op": "is_null"},
            },
        },
    )
    fired = 0
    for edge in require_records(result):
        update_result = state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_DEPENDENCY,
                "filters": {"id": edge["id"], "fired_at": {"op": "is_null"}},
            },
            {"fired_at": fired_at},
        )
        if require_updated(update_result) == 0:
            continue
        fired += 1
        waiter_instance_id = str(edge.get("waiter_instance_id") or "")
        if waiter_instance_id:
            drive_on_delivery(
                state,
                recipient_agent_instance_id=waiter_instance_id,
                sender_label="session_dependency wake",
            )
    return fired


def retire_session(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    directed_by: str,
    register: RetirementEvidenceReader | None = None,
    packet: Callable[[], PacketCustody] | None = None,
) -> dict[str, Any]:
    """§4 ``retire_session`` — the lane-landing verb. Five steps, fixed
    order, each idempotent, no cross-table transaction (§4 partial-failure
    contract): (1) adjudicate a derivable lane worktree before the irreversible
    host termination; no worktree means no adjudication. With ``register`` the
    adjudication may also cite exact custody recorded for the session's own
    assigned Unit, read afresh by step (4) before removal. ``packet`` resolves
    the caller's pinned custody packet for this call only, capturing it first
    if asked, before step (1); that packet and the whole lane are then
    verified afresh at both (1) and (4), and its id is recorded in the
    transition reason and the result; (2) terminate
    (tolerates already-terminal; OWNS firing +
    delivering ``session_terminal`` dependency edges as of 2026-08-04 — see
    :func:`terminate_session`, the sole call site now); (3) release this
    session's ``session_role_claim`` row if one still names a role bound to
    it (best-effort — a role_binding release is a SEPARATE verb/path, not
    this one's job: retire_session cleans up the CARDINALITY row, not role
    ownership itself); (4) record the non-fatal exact teardown outcome; (5)
    predicated ledger write terminated -> retired with that outcome. A crash mid-retire
    leaves the row ``terminated``-but-not-``retired``; re-running this
    function skips completed steps (idempotent) and drives it home —
    re-drivable by construction, never wedged (INCLUDING the firing step:
    a re-run's ``terminate_session`` call lands on the already-terminal
    path, which itself re-sweeps for any edge armed since the first call).
    After (5), :func:`_sweep_terminal_lane_worktrees` does opportunistic
    tidying only — it reclaims the terminal lane worktrees earlier
    retirements left on disk, never this lane's own tree, and can change
    neither the result below nor the re-drivability above.
    """
    initial_row = read_managed_session(state, agent_instance_id)
    initial_state = str(initial_row.get("lifecycle_state") or "")
    if initial_state == LIFECYCLE_RETIRED:
        return _retire_outcome(
            already_retired=True,
            fired=0,
            terminate_result=_host_result(state, initial_row),
        )
    custody = RetirementCustodyContext.for_session(initial_row, register, packet() if packet is not None else None)
    if initial_state != LIFECYCLE_TERMINATED:
        spawn_lifecycle._adjudicate_retire_lane_worktree(initial_row, custody=custody)  # noqa: SLF001

    terminate_result = terminate_session(
        state,
        agent_instance_id=agent_instance_id,
        directed_by=directed_by,
    )
    fired = int(terminate_result.get("session_terminal_edges_fired") or 0)
    row = read_managed_session(state, agent_instance_id)
    # Best-effort — a role this session held is released through the normal
    # role-release path; this only prunes the cardinality row so it does not
    # linger as a stale orphan.
    spawn_lifecycle._release_retiring_session_role_claim(state, row)  # noqa: SLF001
    terminated_row = read_managed_session(state, agent_instance_id)
    current = str(terminated_row.get("lifecycle_state") or "")
    if current == LIFECYCLE_RETIRED:
        return _retire_outcome(already_retired=True, fired=fired, terminate_result=terminate_result)
    worktree_disposition = spawn_lifecycle._retire_lane_worktree(terminated_row, custody=custody)  # noqa: SLF001
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=agent_instance_id,
            from_state=LIFECYCLE_TERMINATED,
            to_state=LIFECYCLE_RETIRED,
            directed_by=directed_by,
            reason=_retire_reason(custody.packet),
            recorded_fields={"worktree_disposition": worktree_disposition},
        )
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    _sweep_terminal_lane_worktrees(state, terminated_row)
    return _retire_outcome(already_retired=False, fired=fired, terminate_result=terminate_result, packet=(custody.packet, worktree_disposition))


def _sweep_terminal_lane_worktrees(
    state: StateManagementInterface,
    row: Mapping[str, Any],
) -> None:
    """Reclaim the terminal lane worktrees earlier retirements left on disk.

    Disposal used to happen only as a side effect of the next unrelated lane
    spawn, so with no spawn nothing was reclaimed however many terminal lane
    trees were registered.  This reuses the sweep and the guard already proven
    at :func:`session_lifecycle_verbs._provision_spawn_worktree`, and runs only
    after the terminated -> retired write, so it is pure opportunistic tidying:
    it never changes this verb's result, never affects the transition that has
    already been recorded, and never makes a re-run fail.  A retained tree is
    surfaced as the existing ``dirty_stale_worktree_skipped`` warning rather
    than a new error shape.  The retiring lane's own tree has already been
    handled, and is excluded here: step (4) adjudicated it against custody read
    afresh, while this sweep has no custody to read, so a tree deliberately
    retained by that decision must not be reconsidered without it.

    Both skip branches are advisory, never refusals (``rul_becb4bed``): an
    incomplete inventory logs ``lane_worktree_cleanup_skipped`` and sweeps
    nothing rather than acting on unknown data, and a row with no derivable
    lane root is skipped silently as the existing code already does.
    """
    if str(row.get("provisioning_mode") or "worktree") in {
        "synthetic_no_worktree",
        "operator_existing_checkout",
    }:
        return
    try:
        repo_root = spawn_lifecycle._resolve_lane_repo_root(str(row.get("lane_repo_root") or ""))  # noqa: SLF001
        own = spawn_lifecycle._retiring_lane_worktree(row)  # noqa: SLF001
        inventory = active_lane_worktree_inventory(state, repo_root)
        if not inventory.cleanup_safe:
            logger.warning(
                "lane_worktree_cleanup_skipped: incomplete_active_inventory=%s",
                "; ".join(inventory.incomplete_rows),
            )
            return
        own_path = None if own is None else own.path.resolve()
        sweep = sweep_orphaned_lane_worktrees(
            repo_root,
            terminal_paths=tuple(path for path in inventory.terminal_paths if path.resolve() != own_path),
        )
    except (LaneWorktreeError, VerbError, OSError) as exc:
        logger.warning("terminal lane worktree sweep skipped after retirement: %s", exc)
        return
    for warning in sweep.skipped:
        spawn_lifecycle._log_dirty_stale_worktree_skipped(warning)  # noqa: SLF001


def _retire_reason(packet: PacketCustody | None) -> str:
    """The audit reason; a pinned packet's id and fingerprint are its provenance, with no new column."""
    if packet is None:
        return "retire_session"
    return f"retire_session custody_packet={packet.packet_id} fingerprint={packet.expected_fingerprint} captured={packet.captured}"


def _retire_outcome(
    *,
    already_retired: bool,
    fired: int,
    terminate_result: Mapping[str, Any],
    packet: tuple[PacketCustody | None, str] = (None, ""),
) -> dict[str, Any]:
    """The retire result: the two original keys plus what happened to the host.

    Without a ``host_action`` from this call's terminate step the call touched
    no host, so it reports ``not_attempted`` rather than inheriting a claim.
    A pinned packet adds ``custody_packet`` with what removal then did, so a
    retained worktree is never read as removed.
    """
    outcome: dict[str, Any] = {
        **{key: value for key, value in terminate_result.items() if key not in {"already_terminal", "lifecycle_state", "session_terminal_edges_fired"}},
        "already_retired": already_retired,
        "dependencies_fired": fired,
        "host": str(terminate_result.get("host") or ""),
        "host_action": str(terminate_result.get("host_action") or HOST_ACTION_NOT_ATTEMPTED),
        "host_remedy": terminate_result.get("host_remedy"),
    }
    pinned, disposition = packet
    if pinned is not None:
        outcome["custody_packet"] = {"packet_id": pinned.packet_id, "fingerprint": pinned.expected_fingerprint, "captured": pinned.captured, "worktree_disposition": disposition}
    return outcome


def describe_retire_gaps(
    result: Mapping[str, Any],
    registration: BridgeBinding | None,
    *,
    registry_checked: bool,
) -> list[str]:
    """Outstanding observed/unknown facts, including unresolved prior attempts."""
    gaps: list[str] = []
    if result.get("host_liveness") != "dead":
        prior = result.get("prior_host_outcome") or {}
        same_host = prior.get("host") == result.get("host") and prior.get("host_ref") == result.get("host_ref")
        remedy = result.get("host_remedy") or (prior.get("remedy") if same_host else None)
        gaps.append(
            f"host {result.get('host')!r} process was not stopped or confirmed dead: {remedy or result.get('host_liveness_detail')}; liveness={result.get('host_liveness', 'unknown')}; {result.get('prior_host_advisory') or 'attempt recorded'}",
        )
    if not registry_checked:
        gaps.append("peer registration was not checked: the agent messaging bridge is not active")
    elif registration is not None:
        gaps.append(
            f"still registered in peer_list: bridge {registration.bridge_id}, parent_pid {registration.parent_pid}",
        )
    return gaps


def report_teardown(
    result: dict[str, Any],
    registration: BridgeBinding | None,
    *,
    registry_checked: bool,
) -> None:
    """Registration is an independent observation; never manufacture its absence."""
    result["registry_checked"] = registry_checked
    result["peer_registration"] = {"bridge_id": registration.bridge_id, "parent_pid": registration.parent_pid} if registration is not None else None
    result["teardown_complete"] = result.get("host_liveness") == "dead" and registry_checked and registration is None
    result["not_done"] = describe_retire_gaps(result, registration, registry_checked=registry_checked)


_VALID_CONDITION_KINDS = frozenset(
    {CONDITION_LANE_CLOSED, CONDITION_SESSION_TERMINAL, CONDITION_DEADLINE},
)


@dataclass(frozen=True, slots=True)
class ArmSessionDependencyRequest:
    """No ``directed_by`` field — unlike ``managed_session``, the
    ``session_dependency`` table carries no audit-provenance column (see its
    schema, ``get_session_dependency_schema``), so there is nothing for one
    to populate; adding it here would be dead weight the verb never reads."""

    waiter_instance_id: str
    condition_kind: str
    condition_ref: str


def _validate_condition_ref(condition_kind: str, condition_ref: str) -> None:
    """Per-kind shape check (§3.4) — catches an obviously wrong
    ``condition_ref`` at arm time rather than leaving a doomed-to-never-fire
    edge sitting armed forever. Deliberately light: the sweep's own fire-time
    resolution is the authority on whether the referenced session/lane is
    REAL, this only rejects a value that could not possibly be one."""
    if condition_kind == CONDITION_SESSION_TERMINAL:
        if not condition_ref.startswith("agi-"):
            raise VerbError(
                "invalid_condition_ref",
                f"condition_kind='session_terminal' requires condition_ref to be an agent_instance_id (the 'agi-' prefix every instance id shares); got {condition_ref!r}.",
            )
    elif condition_kind == CONDITION_DEADLINE:
        try:
            datetime.fromisoformat(condition_ref)
        except ValueError as exc:
            raise VerbError(
                "invalid_condition_ref",
                f"condition_kind='deadline' requires condition_ref to be an ISO-8601 timestamp; {condition_ref!r} does not parse: {exc}",
            ) from exc
    elif condition_kind == CONDITION_LANE_CLOSED and not condition_ref:
        raise VerbError(
            "invalid_condition_ref",
            "condition_kind='lane_closed' requires a non-empty condition_ref (the lane_id).",
        )


def arm_session_dependency(
    state: StateManagementInterface,
    req: ArmSessionDependencyRequest,
) -> dict[str, Any]:
    """Rider verb (drive-on-delivery lane, slice 2, 2026-08-04) — the FIRST
    caller of the D1 ``session_dependency`` wake-edge machinery (schema +
    sweep evaluation + delivery already existed; nothing armed a row until
    now — ``session_sweep.py``'s own module docstring says so).

    Session-scoped ONLY in v1 (``waiter_instance_id`` required). Lane-scoped
    arming is UNSUPPORTED BY CONSTRUCTION, not merely refused: this verb has
    no ``waiter_lane_id`` parameter at all, so there is nothing to accept or
    reject there — the sweep's own delivery has no lane -> current-holder
    mapping (``session_sweep.py::_deliver_dependency_wake`` logs a no-op for
    a lane-scoped edge today), so arming one here would create a wake nobody
    could ever receive.

    No waiter-EXISTENCE check: an unmanaged waiter (no ``managed_session``
    row — an operator-launched session, e.g. the seat) is a legal arm
    target. The sweep's own fire-time resolution already handles an
    unresolvable waiter (logged, the edge still fires as state) — refusing
    here would make this verb the one place in the platform that
    pre-validates delivery liveness instead of firing-as-state and
    resolving best-effort, the design every other edge already follows.

    Errors: ``invalid_waiter`` (empty ``waiter_instance_id``),
    ``unknown_condition_kind``, ``invalid_condition_ref`` (per-kind shape
    check).
    """
    waiter_instance_id = req.waiter_instance_id.strip()
    if not waiter_instance_id:
        raise VerbError(
            "invalid_waiter",
            "arm_session_dependency requires a non-empty waiter_instance_id (session-scoped only in v1 — lane-scoped arming is unsupported by construction; this verb has no waiter_lane_id parameter).",
        )
    if req.condition_kind not in _VALID_CONDITION_KINDS:
        raise VerbError(
            "unknown_condition_kind",
            f"condition_kind {req.condition_kind!r} is not one of {sorted(_VALID_CONDITION_KINDS)}.",
        )
    condition_ref = req.condition_ref.strip()
    _validate_condition_ref(req.condition_kind, condition_ref)
    record: dict[str, Any] = {
        "waiter_instance_id": waiter_instance_id,
        "condition_kind": req.condition_kind,
        "condition_ref": condition_ref,
        "fired_at": None,
    }
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_SESSION_DEPENDENCY, "record": record},
        ),
        "arm session_dependency",
    )
    return {
        "waiter_instance_id": waiter_instance_id,
        "condition_kind": req.condition_kind,
        "condition_ref": condition_ref,
        "armed": True,
    }


_REPORT_ALIVE_EDGE = {"working": LIFECYCLE_LIVE, "idle": LIFECYCLE_IDLE}
REPORT_BY_SOURCE_EXPLICIT_SELF_REPORT: Final = "explicit_self_report"
REPORT_BY_SOURCE_CONFIRMED_DRIVE: Final = "confirmed_drive"
REPORT_BY_SOURCE_OBSERVED_SPAWNING: Final = "observed_spawning"
ReportBySource = Literal["explicit_self_report", "confirmed_drive", "observed_spawning"]


def _rearm_report_by(
    state: StateManagementInterface,
    agent_instance_id: str,
    *,
    report_by_seconds: int = 0,
    source: ReportBySource,
) -> None:
    """Bump ``report_by`` forward — unconditioned (no predicate): a lost race
    on the re-arm timestamp itself is harmless (worst case, the NEXT report
    or the sweep resolves it), unlike ``lifecycle_state``, which is why this
    is a plain write rather than a CAS.

    ``report_by_seconds`` is the ROW's own spawn-time window (persisted at
    spawn — the D2-lane-tail fix), never re-derived from anything else;
    falls back to :data:`DEFAULT_REPORT_BY_SECONDS` only when the row never
    requested a custom window (0/absent — a legacy row spawned before this
    column existed). Re-arming to a SHORTER window than the spawn requested
    was a live-measured bug: a worker spawned with ``report_by_seconds=900``
    got its deadline silently shortened to 300s on its first report/drive."""
    row = read_managed_session(state, agent_instance_id)
    if row.get("provisioning_mode") == "operator_existing_checkout" and not report_by_seconds:
        return
    window = report_by_seconds or DEFAULT_REPORT_BY_SECONDS
    next_report_by = (datetime.now(UTC) + timedelta(seconds=window)).isoformat()
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
        {"report_by": next_report_by, "report_by_source": source},
    )


def _refuse_report_alive_on_ineligible_state(current: str) -> None:  # pyright: ignore[reportUnusedFunction]
    """Raise ``lifecycle_state_conflict`` for the two states that never
    self-report back to life — SEPARATELY, because they are different facts
    about the caller and a single shared sentence taught the wrong one.

    ★ PARKED IS NOT DEAD (LIF-04, measured 2026-08-19 on ``lane-seed-remint``).
    A park suppresses the HEARTBEAT CONTRACT ONLY: ``report_alive`` is refused
    here and ``report_by`` dissolves, while the pane, the stop-hook wake path
    and every messaging verb stay live. The measured consequence is that a
    parked session still WAKES on mail, reads its inbox and sends — which is
    exactly the caller this branch answers, and telling it "state skew" invites
    it to conclude its row is wrong and retry. It is not wrong: park is a
    steward's deliberate state and only ``drive_session`` takes the
    ``parked -> live`` edge back.

    A terminal row is the other fact entirely — the session is finished, and
    nothing it says about itself changes that.

    Same error code for both (callers key on the token, and it stays stable);
    different message, because the message is the only instrument the refused
    caller actually has. Split out of :func:`report_alive` so that verb keeps
    its shape under the radon cc gate.
    """
    if current == LIFECYCLE_PARKED:
        raise VerbError(
            "lifecycle_state_conflict",
            "report_alive arrived on a 'parked' row — a parked session never "
            "self-reports back to life; the parked -> live edge belongs to a "
            "steward's drive_session. This refusal is NOT evidence that you are "
            "dead or that your pane is gone: measured 2026-08-19 (LIF-04), a park "
            "suppresses the HEARTBEAT CONTRACT ONLY — report_alive is refused and "
            "report_by dissolves, while the pane, the stop-hook wake path and the "
            "messaging verbs all stay live, so a parked session still wakes on "
            "mail, still reads its inbox and can still send. Do not retry, and do "
            "not read your own ability to run turns as an un-park; if the work is "
            "meant to continue, your steward drives you.",
        )
    if current in _TERMINAL_STATES:
        raise VerbError(
            "lifecycle_state_conflict",
            f"report_alive arrived on a {current!r} row — state skew, not accepted (a terminal row is finished; it never self-reports back to life).",
        )


def _row_report_by_seconds(row: dict[str, Any]) -> int:
    """Split out of :func:`report_alive` to keep it under the radon cc
    threshold — the row's own spawn-time window, or 0 (falls back to
    :data:`DEFAULT_REPORT_BY_SECONDS` inside :func:`_rearm_report_by`)."""
    return int(row.get("report_by_seconds") or 0)


def _write_explicit_status_source(  # pyright: ignore[reportUnusedFunction]
    state: StateManagementInterface,
    agent_instance_id: str,
) -> None:
    """Record the source of a status write separately from liveness."""
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
        {"status_source": "explicit_self_report"},
    )


def report_alive(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    status: str,
    directed_by: str,
    status_note: str = "",
    heartbeat_failures_since_last: int = 0,
    heartbeat_failure_first_at: str | None = None,
    heartbeat_failure_last_reason: str = "",
) -> dict[str, Any]:
    """Dispatch liveness mechanics without growing the lifecycle verb module."""
    from agent_messaging_plugin.session_lifecycle_liveness import (  # noqa: PLC0415
        report_alive as report_liveness,
    )

    return report_liveness(
        state,
        agent_instance_id=agent_instance_id,
        status=status,
        directed_by=directed_by,
        status_note=status_note,
        heartbeat_failures_since_last=heartbeat_failures_since_last,
        heartbeat_failure_first_at=heartbeat_failure_first_at,
        heartbeat_failure_last_reason=heartbeat_failure_last_reason,
    )
