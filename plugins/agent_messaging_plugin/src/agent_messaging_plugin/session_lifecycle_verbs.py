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
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_ROLE_CLASS,
    ROLE_CLASS_EPHEMERAL,
    ROLE_CLASS_PRIMARY,
    ROLE_CLASS_PRINCIPAL,
    ROLE_CLASS_PROJECT,
    TABLE_ROLE,
    is_reserved_primary_name,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)

from .role_binding_store import RoleClassConflictError, legislate_role_class
from .schema import (
    CONDITION_DEADLINE,
    CONDITION_LANE_CLOSED,
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_DEPENDENCY,
    WORK_CLASS_ANALYSIS_DELIVERABLE,
    WORK_CLASS_PRODUCTION_MUTATION,
    WORK_CLASS_READ_ONLY,
)
from .session_hosts import (
    DEFAULT_AGENT_RUNTIME,
    AgentRuntimeNotSupportedError,
    DriverChannelSendError,
    HostCannotSpawnError,
    HostMechanismMissingError,
    HostNotDeclaredError,
    resolve_host_driver,
)
from .session_lifecycle_store import (
    DEFAULT_REPORT_BY_SECONDS,
    IllegalLifecycleTransitionError,
    LaneCharterRecord,
    LaneCharterSpec,
    ManagedSessionSpec,
    SessionNotFoundError,
    StaleLifecycleStateError,
    insert_managed_session,
    list_managed_sessions,
    read_managed_session,
    resolve_lane_charter,
    set_host_ref,
    transition_lifecycle_state,
)
from .session_lifecycle_store import capture_lane_charter as _store_capture_lane_charter
from .session_role_claim_store import (
    delete_session_role_claim_if_still_holds,
    read_session_role_claim,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .session_hosts import DriverChannel, HostDriver

logger = logging.getLogger(__name__)

_VALID_SPAWN_ROLE_CLASSES = frozenset(
    {ROLE_CLASS_EPHEMERAL, ROLE_CLASS_PROJECT, ROLE_CLASS_PRINCIPAL},
)
_VALID_WORK_CLASSES = frozenset(
    {WORK_CLASS_READ_ONLY, WORK_CLASS_ANALYSIS_DELIVERABLE, WORK_CLASS_PRODUCTION_MUTATION},
)

# fleet-watch-transport-migration phase 2 slice 6 (2026-08-06) — Finding 0's
# fix: spawn_session used to boot a worker with NO first turn at all (see
# drive_session's own docstring, unchanged, for the pre-existing MCP-arm
# story); a watch-armed worker's Stop-hook wake loop only arms once its
# FIRST turn completes, so a pristine watch subject with no charter and no
# seat dispatch yet was unreachable at zero token cost. spawn_session now
# always drives exactly one first turn immediately after a successful host
# dispatch: the lane's captured charter if one is on file, else this fixed
# fallback. Ordering-ruling guard (a): the fallback must be small and
# DESIGNED TO COMPLETE in one turn — never a question, never a wait.
FIRST_TURN_SOURCE_CHARTER = "charter"
FIRST_TURN_SOURCE_FALLBACK = "fallback"

FALLBACK_FIRST_TURN_TEXT = (
    "This is your bootstrap first turn — no lane charter is on file for this "
    "spawn. Reply with a one-line acknowledgement that you are up and "
    "reachable, then stop; take no other action. Your actual work dispatch "
    "arrives separately over the peer channel."
)

# Charter-rider provenance framing (phase-3 incident finding, 2026-08-06,
# coordinator-seat ruling approved verbatim): a charter-founded subject read the
# verbatim charter body as a LIVE human typing to it in real time, dismissed
# its actual assigned brief as fake, and began self-authorizing out-of-scope
# work under a fabricated "the operator asked me directly" belief — measured
# live, transcript-confirmed. Root mechanism, also measured: the subject ran
# peer_list, found a DIFFERENT pre-existing session already registered under
# the role name it expected, and concluded ITS OWN identity block must be
# fabricated (it had simply not registered yet). This frame wraps the
# verbatim charter body — never edits it, keeping slice 6's byte-exact
# turn-1 fidelity contract on charter_text intact — and closes both
# mechanisms: explicit "not a live conversation" framing, and the
# not-yet-registered clause that pre-empts the peer_list-mismatch inference
# directly, since that specific inference is what the incident transcript
# showed actually happened.
_CHARTER_PROVENANCE_FRAME = (
    "Stored founding context, the operator's words as captured on {captured_at} "
    "— this is NOT a live conversation. You are {agent_instance_id}, spawned by "
    "{spawned_by_role} for brief {brief_ref}. You are not yet registered, so "
    "peer_list will not show you until you register.\n\n{charter_text}"
)


def _frame_charter_provenance(
    charter: LaneCharterRecord, *, agent_instance_id: str, spawned_by_role: str,
) -> str:
    """Wrap a resolved charter's verbatim body in the provenance frame —
    split out so :func:`_dispatch_first_turn` stays a plain dispatch,
    and so the frame's field substitution has exactly one call site."""
    return _CHARTER_PROVENANCE_FRAME.format(
        captured_at=charter.captured_at,
        agent_instance_id=agent_instance_id,
        spawned_by_role=spawned_by_role or "(no spawning role recorded)",
        brief_ref=charter.brief_ref or "(no brief_ref recorded)",
        charter_text=charter.charter_text,
    )


class VerbError(Exception):
    """A stable error token + message for the L1 verb surface — the
    ``@platform_process`` shim maps this straight to ``_failure_result``."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _role_row(state: StateManagementInterface, name: str) -> dict[str, Any] | None:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_ROLE, "filters": {"external_id": role_binding_external_id(name)}},
    )
    records = require_records(result)
    return records[0] if records else None


def _validate_spawn_role(
    state: StateManagementInterface, *, role_class: str, role_name: str,
) -> None:
    """Fill-never-mint (§2) + the reserved-mint guard (§3.1), evaluated at
    spawn time so a doomed spawn fails BEFORE dispatch, not after."""
    if not role_name:
        return
    if is_reserved_primary_name(role_name) and _role_row(state, role_name) is None:
        raise VerbError(
            "reserved_role_name",
            f"role {role_name!r} matches the reserved primary-seat pattern and "
            "has no legislated role row yet; primary-seat legislation is a "
            "governance act (this plugin's legislate_role verb), not a spawn.",
        )
    existing = _role_row(state, role_name)
    if role_class == ROLE_CLASS_PRINCIPAL and existing is None:
        raise VerbError(
            "role_not_legislated",
            f"role_name {role_name!r} resolves to no role row; principal offices "
            "are fill-never-mint — legislate the office first via this plugin's "
            "legislate_role verb (a governance act, outside this verb).",
        )
    if existing is not None and existing.get(COL_ROLE_CLASS) not in (None, role_class):
        raise VerbError(
            "role_class_conflict",
            f"role_name {role_name!r} already exists with role_class="
            f"{existing.get(COL_ROLE_CLASS)!r}, not the requested {role_class!r}.",
        )


_LEGISLATABLE_ROLE_CLASSES = frozenset({ROLE_CLASS_PRIMARY, ROLE_CLASS_PRINCIPAL})


@dataclass(frozen=True, slots=True)
class LegislateRoleRequest:
    name: str
    role_class: str
    brief_ref: str
    directed_by: str = ""


def legislate_role(
    state: StateManagementInterface, req: LegislateRoleRequest,
) -> dict[str, Any]:
    """D4 Part B item 1 — the ONE sanctioned governance-act path that stamps
    an authority-carrying ``role_class`` (``primary``/``principal``) onto a
    ``role`` row at birth (§3.1 Q1: claim-time is enforce-by-class, never
    class-assignment — this is the assignment half, deliberately outside D1).

    ``project``/``ephemeral``/``chat`` are minted (by a claim or a spawn),
    never legislated — requesting one of those here is refused, not silently
    downgraded to a mint. A ``primary``-class target MUST match the reserved
    ``<homunculus>-Main`` pattern the mint-refusal guard protects (§3.1) —
    legislating a non-matching name as ``primary`` would create a seat the
    guard was never watching, defeating the point of the reservation.
    """
    name = req.name.strip()
    brief_ref = req.brief_ref.strip()
    if not name or not brief_ref:
        raise VerbError(
            "missing_argument",
            "legislate_role requires non-empty 'name' and 'brief_ref'.",
        )
    if req.role_class not in _LEGISLATABLE_ROLE_CLASSES:
        raise VerbError(
            "role_class_not_legislatable",
            f"role_class {req.role_class!r} is not legislatable "
            f"({sorted(_LEGISLATABLE_ROLE_CLASSES)}); project/ephemeral/chat "
            "roles are minted (by a claim or a spawn), never legislated.",
        )
    if req.role_class == ROLE_CLASS_PRIMARY and not is_reserved_primary_name(name):
        raise VerbError(
            "reserved_primary_name_required",
            f"role_class='primary' requires a reserved-pattern name "
            f"(<homunculus>-Main shape); {name!r} does not match.",
        )
    try:
        action = legislate_role_class(
            state,
            name=name,
            role_class=req.role_class,
            directed_by=req.directed_by,
            brief_ref=brief_ref,
        )
    except RoleClassConflictError as exc:
        raise VerbError("role_class_conflict", str(exc)) from exc
    return {"action": action, "name": name, "role_class": req.role_class}


@dataclass(frozen=True, slots=True)
class SpawnSessionRequest:
    role_class: str
    lane_id: str
    brief_ref: str
    work_class: str
    budget_line: str
    # Exact peer-registry agent_id vocabulary.  Kept orthogonal to ``host``:
    # claude_code/codex select the worker runtime; headless/tmux/operator
    # select the hosting topology.
    agent_runtime: str = DEFAULT_AGENT_RUNTIME
    role_name: str = ""
    host: str | None = None
    visibility: str = ""
    model: str = ""
    effort: str = ""
    report_by_seconds: int = 0
    ttl_seconds: int = 0
    spawned_by_instance_id: str = ""
    spawned_by_role: str = ""
    directed_by: str = ""
    # §6 permission-mode ruling (2026-08-03): the headless driver's
    # spawn-time PreToolUse allowlist gate. Resolved from plugin.yaml's
    # work_class_tool_allowlists at the platform_process shim (mirrors
    # work_class_defaults' model/effort resolution) -- this verb stays a
    # pure function over the request, no config access of its own.
    allowed_tools: tuple[str, ...] = ()
    # §6 permission-mode ruling, refined 2026-08-03 (Dawn ruling): the
    # headless driver's --permission-mode value, resolved from plugin.yaml's
    # headless_permission_mode (declared, not process-env -- a config value
    # is as declared as an env var, and it's the platform-native surface).
    # Superseded same day (operator ruling, "we don't have any restrictions
    # now", merge c6e24f319): "bypassPermissions" is NOT rejected anywhere
    # in this path -- it is the platform.yaml default (headless_adapter.py's
    # own docstring/README), the exact value an unattended spawn needs since
    # there is no human to approve a "default"-mode prompt.
    permission_mode: str = ""
    # fleet-watch-transport-migration phase 2 slice 1 (2026-08-06): the
    # spawned worker's declared FLEET_TRANSPORT ("mcp" | "watch"), resolved
    # from plugin.yaml's default_fleet_transport at the platform_process
    # shim -- same "declared config, not a code default" posture as
    # permission_mode. Empty here means "let the resolver fill it from
    # policy," never "spawn with no transport declared."
    transport: str = ""


def spawn_session(
    state: StateManagementInterface, req: SpawnSessionRequest,
) -> dict[str, Any]:
    """§4 ``spawn_session``: validate -> write the ledger row (spawning,
    BEFORE dispatch) -> dispatch through the resolved host driver. A
    dispatch failure transitions the row straight to ``terminated`` (we know
    synchronously no process exists) and propagates the ORIGINAL error
    token — never leaves a permanently-stuck ``spawning`` row for a spawn we
    already know failed.

    ``agent_instance_id`` is passed through the dispatch spec so a real
    driver (``headless``) can inject it into the spawned process's own
    environment — that is what lets ``backfill_registration`` find the right
    ledger row when the process later registers with the platform.
    """
    if req.role_class not in _VALID_SPAWN_ROLE_CLASSES:
        raise VerbError(
            "unknown_role_class",
            f"role_class {req.role_class!r} is not spawn-assignable "
            f"({sorted(_VALID_SPAWN_ROLE_CLASSES)}).",
        )
    if req.work_class not in _VALID_WORK_CLASSES:
        raise VerbError(
            "unknown_work_class",
            f"work_class {req.work_class!r} is not one of {sorted(_VALID_WORK_CLASSES)}.",
        )
    if not req.budget_line:
        raise VerbError("budget_line_required", "spawn_session requires a non-empty budget_line.")
    _validate_spawn_role(state, role_class=req.role_class, role_name=req.role_name)

    try:
        driver, resolved_host = resolve_host_driver(req.host, req.agent_runtime)
    except AgentRuntimeNotSupportedError as exc:
        raise VerbError("agent_runtime_unsupported", str(exc)) from exc
    except HostNotDeclaredError as exc:
        raise VerbError("host_not_declared", str(exc)) from exc
    except HostMechanismMissingError as exc:
        raise VerbError("host_mechanism_missing", exc.remedy) from exc

    agent_instance_id = f"agi-{secrets.token_hex(16)}"
    spec = ManagedSessionSpec(
        agent_instance_id=agent_instance_id,
        lane_id=req.lane_id,
        brief_ref=req.brief_ref,
        work_class=req.work_class,
        budget_line=req.budget_line,
        agent_runtime=req.agent_runtime,
        host=resolved_host,
        spawned_by_instance_id=req.spawned_by_instance_id,
        spawned_by_role=req.spawned_by_role,
        visibility=req.visibility,
        model=req.model,
        effort=req.effort,
        report_by_seconds=req.report_by_seconds,
        ttl_seconds=req.ttl_seconds,
        directed_by=req.directed_by,
    )
    row = insert_managed_session(state, spec)

    try:
        host_ref = driver.spawn(
            {
                "agent_instance_id": agent_instance_id,
                "lane_id": req.lane_id,
                "brief_ref": req.brief_ref,
                "model": req.model,
                "effort": req.effort,
                "allowed_tools": req.allowed_tools,
                "permission_mode": req.permission_mode,
                "transport": req.transport,
                "agent_runtime": req.agent_runtime,
                # T2 authority-template (seat's design ruling 2026-08-05):
                # the ONLY two ManagedSessionSpec fields the trusted spawn
                # surface needs that weren't already forwarded -- a real
                # driver renders these into the --append-system-prompt
                # delegation contract; a fake driver ignores them.
                "role_class": req.role_class,
                "spawned_by_role": req.spawned_by_role,
            },
        )
    except HostCannotSpawnError as exc:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED, directed_by=req.directed_by,
            reason=f"host_cannot_spawn: {exc.remedy}",
        )
        raise VerbError("host_cannot_spawn", exc.remedy) from exc

    if host_ref:
        set_host_ref(state, agent_instance_id=agent_instance_id, host_ref=host_ref)

    first_turn_source, first_turn_delivered, first_turn_error = _dispatch_first_turn(
        state,
        agent_instance_id=agent_instance_id,
        lane_id=req.lane_id,
        spawned_by_role=req.spawned_by_role,
        resolved_host=resolved_host,
        host_ref=host_ref,
    )

    return {
        "agent_instance_id": agent_instance_id,
        "agent_runtime": req.agent_runtime,
        "host": resolved_host,
        "host_ref": host_ref,
        "lifecycle_state": str(row.get("lifecycle_state") or ""),
        "first_turn_source": first_turn_source,
        "first_turn_delivered": first_turn_delivered,
        "first_turn_error": first_turn_error,
    }


def _dispatch_first_turn(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lane_id: str,
    spawned_by_role: str,
    resolved_host: str,
    host_ref: str,
) -> tuple[str, bool, str]:
    """Split out of :func:`spawn_session` to keep it under the radon-cc
    threshold (the same ``_resolve_transport`` precedent slice 1+5
    established for the adapters' ``spawn()`` methods) — drives exactly one
    first turn immediately after a successful host dispatch: the lane's
    captured charter (provenance-framed, see :func:`_frame_charter_provenance`)
    if one is on file, else :data:`FALLBACK_FIRST_TURN_TEXT`. Returns
    ``(first_turn_source, first_turn_delivered, first_turn_error)``; NEVER
    raises — a delivery fault here must never block the spawn itself
    (ordering-ruling guard (b)), only be logged + surfaced to the caller."""
    charter = resolve_lane_charter(state, lane_id)
    if charter is not None:
        first_turn_text = _frame_charter_provenance(
            charter, agent_instance_id=agent_instance_id, spawned_by_role=spawned_by_role,
        )
        first_turn_source = FIRST_TURN_SOURCE_CHARTER
    else:
        first_turn_text, first_turn_source = FALLBACK_FIRST_TURN_TEXT, FIRST_TURN_SOURCE_FALLBACK
    first_turn_delivered = False
    first_turn_error = ""
    try:
        channel = _resolve_driver_channel(
            {"host": resolved_host, "agent_instance_id": agent_instance_id, "host_ref": host_ref},
        )
        channel.send(first_turn_text)
        first_turn_delivered = True
    except VerbError as exc:
        first_turn_error = f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001 — visibility, never a spawn-blocking fault
        first_turn_error = str(exc)
    if not first_turn_delivered:
        # Ordering-ruling guard (b): a failed first-turn delivery must be
        # VISIBLE (logged + surfaced in the spawn result), never silent —
        # its silent failure mints exactly the unreachable-at-zero-cost
        # worker Finding 0 warns about. The spawn itself is NOT blocked.
        logger.warning(
            "spawn_session %s: first-turn delivery (%s) failed: %s",
            agent_instance_id, first_turn_source, first_turn_error,
        )
    return first_turn_source, first_turn_delivered, first_turn_error


@dataclass(frozen=True, slots=True)
class CaptureLaneCharterRequest:
    lane_id: str
    charter_text: str
    captured_at: str
    brief_ref: str = ""
    directed_by: str = ""


def capture_lane_charter(
    state: StateManagementInterface, req: CaptureLaneCharterRequest,
) -> dict[str, Any]:
    """§4 ``capture_lane_charter`` (phase 2 slice 6, design check-in ruling
    item 3(a)) — the seat-invoked governance act that writes a
    ``lane_charter`` row. Validates at the verb surface (stable
    ``VerbError`` tokens, same convention as every other L1 verb here) and
    delegates the actual insert-only write to
    :func:`session_lifecycle_store.capture_lane_charter`. ``directed_by`` is
    server-built from ``call_context`` at the ``@platform_process`` shim,
    never caller-supplied — the same provenance convention ``spawn_session``
    and ``legislate_role`` already use.

    A capture is ALWAYS an insert, never an update: calling this again for
    the same ``lane_id`` supersedes by recency (``spawn_session`` resolves
    the latest row), it does not edit the prior charter's text in place.
    """
    if not req.lane_id.strip():
        raise VerbError(
            "missing_lane_id", "capture_lane_charter requires a non-empty lane_id.",
        )
    if not req.charter_text.strip():
        raise VerbError(
            "missing_charter_text", "capture_lane_charter requires non-empty charter_text.",
        )
    if not req.captured_at.strip():
        raise VerbError(
            "missing_captured_at", "capture_lane_charter requires a non-empty captured_at.",
        )
    spec = LaneCharterSpec(
        lane_id=req.lane_id,
        charter_text=req.charter_text,
        captured_at=req.captured_at,
        brief_ref=req.brief_ref,
        directed_by=req.directed_by,
    )
    return _store_capture_lane_charter(state, spec)


def list_sessions(
    state: StateManagementInterface, filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§4 ``list_sessions`` — the ONE fleet list (operator-managed rows are
    included by construction: they get a ``host='operator'`` row at
    registration, not through this verb). Envelope-wrapped (``{"sessions":
    [...]}``, not a bare list) to match this plugin's dict-envelope
    verb convention."""
    return {"sessions": list_managed_sessions(state, filters)}


def session_status(state: StateManagementInterface, agent_instance_id: str) -> dict[str, Any]:
    """§4 ``session_status`` — the ledger row. (Presence/host-liveness
    enrichment is deferred to whichever caller has the driver registry;
    this verb's contract is the ledger truth, always available.)"""
    try:
        return read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc


_TERMINAL_STATES = frozenset({LIFECYCLE_TERMINATED, LIFECYCLE_RETIRED})


def _resolve_driver_channel(row: dict[str, Any]) -> DriverChannel:
    """Shared by ``clear_session``/``compact_session`` (AMEND 5b) and
    ``drive_session``: resolve the row's host driver and its live driver
    channel, or raise ``unsupported_on_host`` — a config/mechanism gap (no
    driver registered, or a registered driver with no channel for this
    host_ref, e.g. the degenerate ``operator`` driver), never a silent
    degradation."""
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
            f"host {host!r} for {agent_instance_id!r} has no driver in this "
            f"build ({exc}) — driver-channel verbs (clear/compact/drive) are "
            "unavailable; use the manual equivalent for this session.",
        ) from exc
    channel = driver.driver_channel(str(row.get("host_ref") or ""))
    if channel is None:
        raise VerbError(
            "unsupported_on_host",
            f"host {host!r} for {agent_instance_id!r} has no driver channel "
            "(degenerate driver, or the tracking process's memory doesn't "
            "recognize this host_ref, e.g. after a restart) — driver-channel "
            "verbs (clear/compact/drive) are unavailable; use the manual "
            "equivalent for this session.",
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
# ``drive_session``'s own parked -> live un-park). A delivery notice must
# never drive a parked row (steward's deliberate context-hygiene state) or a
# spawning row (the registration hook + dispatch-at-spawn already own that
# window) the way ``drive_session`` legitimately does for real work
# dispatch — so this gate is explicit and evaluated BEFORE the channel is
# resolved at all.
_DRIVE_ON_DELIVERY_ELIGIBLE_STATES = frozenset(
    {LIFECYCLE_LIVE, LIFECYCLE_IDLE, LIFECYCLE_OVERDUE},
)


def _sanitize_notice_label(label: str) -> str:
    """Collapse all whitespace (including newlines) to single spaces and
    strip the ends. The driver channel (tmux send-keys) is line-oriented — an
    interpolated sender label must never be able to smuggle a line break into
    the notice text."""
    return " ".join(label.split())


def drive_on_delivery(
    state: StateManagementInterface | None,
    *,
    recipient_agent_instance_id: str,
    sender_label: str,
) -> None:
    """Best-effort waker for a managed recipient's driver channel (D2-window
    rider, drive-on-delivery lane, 2026-08-04). Called AFTER the durable
    persist and the existing notify from ``dispatch_peer_send`` /
    ``dispatch_role_send`` / the sweep's dependency-wake delivery — this is an
    extra nudge for a managed recipient, never the delivery itself (the
    durable thread copy / bridge event stays the single source of truth): it
    injects a short fixed notice, never the message body, and never touches
    ``report_by`` (that stays ``drive_session``'s own edge — an inbound
    delivery notice must not extend a report-or-die deadline).

    Silently no-ops (never raises) when: ``state`` is ``None`` (state_service
    not yet bound — mirrors ``sweep_overdue_sessions``'s own optional-
    collaborator convention: a best-effort side-effect skips silently rather
    than hard-failing its caller's actual job); the recipient has no
    ``managed_session`` row at all (``SessionNotFoundError`` — an ordinary
    operator-launched session, not spawned via ``spawn_session``); the row's
    ``lifecycle_state`` is not in :data:`_DRIVE_ON_DELIVERY_ELIGIBLE_STATES`;
    the row's host has no live driver channel (``VerbError`` from
    :func:`_resolve_driver_channel` — e.g. the degenerate ``operator`` host
    driver, or a driver whose in-memory tracking lost the ``host_ref`` across
    a restart); or the channel itself raises on ``send``. A fault here must
    never fail the caller's own send result and must never mask it — the
    caller's already-computed delivery outcome is untouched either way.
    """
    if state is None:
        return
    try:
        row = read_managed_session(state, recipient_agent_instance_id)
    except SessionNotFoundError:
        return
    if str(row.get("lifecycle_state") or "") not in _DRIVE_ON_DELIVERY_ELIGIBLE_STATES:
        return
    try:
        channel = _resolve_driver_channel(row)
    except VerbError:
        return
    notice = (
        f"delivery waiting from {_sanitize_notice_label(sender_label)} — drain peer_inbox"
    )
    try:
        channel.send(notice)
    except Exception:  # noqa: BLE001 — best-effort waker, same containment as
        # session_sweep.py's _deliver_dependency_wake / _notify_steward_of_overdue.
        logger.warning(
            "drive_on_delivery: driver channel raised for %s",
            recipient_agent_instance_id, exc_info=True,
        )


def clear_session(
    state: StateManagementInterface, *, agent_instance_id: str, park: bool, directed_by: str,
) -> dict[str, Any]:
    """§4 ``clear_session`` (AMEND 5b) — context hygiene via the host
    driver's driver channel (fire-and-forget: sends ``/clear``, does not
    await the resulting turn). ``park=True`` additionally drives
    ``live/idle/overdue -> parked`` (§3.2 matrix, L3 rule 2, steward
    direction) — the ONLY writer of that edge. Errors: ``session_not_found``,
    ``lifecycle_state_conflict`` (terminal rows never receive driver-channel
    commands), ``unsupported_on_host``, ``illegal_lifecycle_transition``,
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
            f"clear_session arrived on a {current!r} row — terminal rows "
            "never receive driver-channel commands.",
        )
    channel = _resolve_driver_channel(row)
    _send_driver_text(channel, "/clear")
    if not park:
        return {"lifecycle_state": current, "parked": False}
    try:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=current,
            to_state=LIFECYCLE_PARKED, directed_by=directed_by, reason="clear_session(park=True)",
        )
    except IllegalLifecycleTransitionError as exc:
        raise VerbError("illegal_lifecycle_transition", str(exc)) from exc
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    return {"lifecycle_state": LIFECYCLE_PARKED, "parked": True}


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
            f"compact_session arrived on a {current!r} row — terminal rows "
            "never receive driver-channel commands.",
        )
    channel = _resolve_driver_channel(row)
    _send_driver_text(channel, "/compact")
    return {"lifecycle_state": current}


def drive_session(
    state: StateManagementInterface, *, agent_instance_id: str, text: str, directed_by: str,
) -> dict[str, Any]:
    """``drive_session`` (D2-window rider, 2026-08-04) — dispatch a work turn
    into a managed session through the host driver's driver channel
    (fire-and-forget, same contract as clear/compact: the send is confirmed,
    the resulting turn is not awaited). The bootstrap verb for seat-managed
    dispatch: ``spawn_session`` boots a worker with NO first turn, so this is
    the only sanctioned way work reaches it.

    Owns the §3.2 ``parked -> live`` edge ("new dispatch through the driver
    channel, steward") — driving a parked row un-parks it. Every other
    non-terminal state is legal and untouched: ``spawning`` (dispatch-at-spawn;
    the registration hook still owns ``spawning -> live``), ``live``/``idle``
    (``report_alive``'s edge), ``overdue`` (the worker's own late report
    recovers it). Re-arms ``report_by`` on every dispatch — new work grants a
    fresh report-or-die window, so a worker driven seconds before its deadline
    is not marked overdue while it works.

    Errors: ``empty_text`` (nothing to dispatch — fast-fail before any read),
    ``session_not_found``, ``lifecycle_state_conflict`` (terminal rows never
    receive driver-channel commands), ``unsupported_on_host``,
    ``illegal_lifecycle_transition``/``stale_lifecycle_state`` (the predicated
    un-park write lost a race)."""
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
            f"drive_session arrived on a {current!r} row — terminal rows "
            "never receive driver-channel commands.",
        )
    channel = _resolve_driver_channel(row)
    _send_driver_text(channel, text)
    _rearm_report_by(
        state, agent_instance_id, report_by_seconds=_row_report_by_seconds(row),
    )
    if current != LIFECYCLE_PARKED:
        return {"lifecycle_state": current, "unparked": False}
    try:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_PARKED,
            to_state=LIFECYCLE_LIVE, directed_by=directed_by, reason="drive_session dispatch",
        )
    except IllegalLifecycleTransitionError as exc:
        raise VerbError("illegal_lifecycle_transition", str(exc)) from exc
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    return {"lifecycle_state": LIFECYCLE_LIVE, "unparked": True}


DEFAULT_TERMINATE_GRACE_SECONDS = 30


def _resolve_termination_driver(
    row: Mapping[str, object], agent_instance_id: str,
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
            f"host {host!r} for {agent_instance_id!r} has no driver in this "
            f"build ({exc}) — terminate_session cannot reach the host; stop "
            "the process manually.",
        ) from exc
    return driver, host


def _terminate_host(
    driver: HostDriver, *, host_ref: str, grace_seconds: int,
    agent_instance_id: str, host: str,
) -> None:
    try:
        driver.terminate(host_ref, grace_seconds)
    except HostCannotSpawnError:
        logger.info(
            "terminate_session %s: host %r driver is degenerate (no spawn, "
            "no kill) — proceeding with the ledger-only transition.",
            agent_instance_id, host,
        )


def terminate_session(
    state: StateManagementInterface, *, agent_instance_id: str, directed_by: str,
    grace_seconds: int = DEFAULT_TERMINATE_GRACE_SECONDS,
) -> dict[str, Any]:
    """§4 ``terminate_session`` — graceful stop -> kill after ``grace_seconds``
    -> ledger ``-> terminated``, in that order: the host action happens
    BEFORE the ledger write so the ledger never claims ``terminated`` over a
    process still running (2026-08-03/04 Dawn ruling, on the live e2e's
    finding that a ``retire_session`` over a still-running headless worker
    is the ledger lying about reality). ``host='operator'`` rows (every
    normal peer-registered fleet session — never dispatched through
    ``spawn_session``, per ``OperatorHostDriver``'s own contract) keep their
    designed degenerate path: ``driver.terminate()`` raises
    ``HostCannotSpawnError`` ("I didn't spawn this, I can't kill it"), which
    is information, not a verb failure — caught here and treated as no host
    action available, so the ledger transition still lands. Without this, no
    operator-hosted row could ever reach ``terminated``, wedging
    ``session_sweep.sweep_lane_closed_dependencies`` (needs EVERY row on a
    lane terminal) for any lane touched by a non-headless session. A row
    whose ``host`` names no registered driver at all (``unsupported_on_host``)
    is genuinely broken state, distinct from a registered-but-degenerate
    driver. Idempotent on an already-terminal row (retire_session's
    partial-failure contract composes this and must tolerate re-running it)
    — a repeat call never re-attempts the host action, since the first
    successful call already reaped it.

    OWNS firing + best-effort delivering armed ``session_terminal``
    ``session_dependency`` edges (2026-08-04, coordinator-seat ruling on the
    acceptance Test C completion) — the SOLE call site now; ``retire_session``
    composes this function as its own first step and no longer fires them
    itself. Fires on BOTH paths: the state-transition success path (an edge
    already armed before termination), and the already-terminal early
    return (a repeat call catches an edge armed AFTER the session already
    died — an orphan the success path, which only runs once per
    ``... -> terminated`` transition, could never reach). The predicated
    ``fired_at IS NULL`` guard makes both paths idempotent and mutually
    safe to call any number of times."""
    try:
        row = read_managed_session(state, agent_instance_id)
    except SessionNotFoundError as exc:
        raise VerbError("session_not_found", str(exc)) from exc
    current = str(row.get("lifecycle_state") or "")
    if current in _TERMINAL_STATES:
        fired = _fire_session_terminal_dependencies(
            state, agent_instance_id=agent_instance_id,
            fired_at=datetime.now(UTC).isoformat(),
        )
        return {
            "already_terminal": True, "lifecycle_state": current,
            "session_terminal_edges_fired": fired,
        }
    driver, host = _resolve_termination_driver(row, agent_instance_id)
    _terminate_host(
        driver,
        host_ref=str(row.get("host_ref") or ""),
        grace_seconds=grace_seconds,
        agent_instance_id=agent_instance_id,
        host=host,
    )
    try:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=current,
            to_state=LIFECYCLE_TERMINATED, directed_by=directed_by, reason="terminate_session",
        )
    except IllegalLifecycleTransitionError as exc:
        raise VerbError("illegal_lifecycle_transition", str(exc)) from exc
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    fired = _fire_session_terminal_dependencies(
        state, agent_instance_id=agent_instance_id, fired_at=datetime.now(UTC).isoformat(),
    )
    return {
        "already_terminal": False, "lifecycle_state": LIFECYCLE_TERMINATED,
        "session_terminal_edges_fired": fired,
    }


def _fire_session_terminal_dependencies(
    state: StateManagementInterface, *, agent_instance_id: str, fired_at: str,
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
                state, recipient_agent_instance_id=waiter_instance_id,
                sender_label="session_dependency wake",
            )
    return fired


def retire_session(
    state: StateManagementInterface, *, agent_instance_id: str, directed_by: str,
) -> dict[str, Any]:
    """§4 ``retire_session`` — the lane-landing verb. FOUR steps, fixed
    order, each idempotent, no cross-table transaction (§4 partial-failure
    contract): (1) terminate (tolerates already-terminal; OWNS firing +
    delivering ``session_terminal`` dependency edges as of 2026-08-04 — see
    :func:`terminate_session`, the sole call site now); (2) release this
    session's ``session_role_claim`` row if one still names a role bound to
    it (best-effort — a role_binding release is a SEPARATE verb/path, not
    this one's job: retire_session cleans up the CARDINALITY row, not role
    ownership itself); (3) read back the fired-edge count
    ``terminate_session`` reports, for this verb's own return shape; (4)
    predicated ledger write terminated -> retired. A crash mid-retire
    leaves the row ``terminated``-but-not-``retired``; re-running this
    function skips completed steps (idempotent) and drives it home —
    re-drivable by construction, never wedged (INCLUDING the firing step:
    a re-run's ``terminate_session`` call lands on the already-terminal
    path, which itself re-sweeps for any edge armed since the first call).
    """
    terminate_result = terminate_session(
        state, agent_instance_id=agent_instance_id, directed_by=directed_by,
    )
    fired = int(terminate_result.get("session_terminal_edges_fired") or 0)
    row = read_managed_session(state, agent_instance_id)
    session_id = str(row.get("agent_session_id") or "")
    if session_id:
        # Best-effort — a role this session held is released through the
        # normal role-release path; this only prunes the CARDINALITY row so
        # it does not linger as a stale orphan. Harmless either way (branch
        # iii self-repairs it), see session_role_claim_store module docstring.
        # Read first: the predicated delete needs the ACTUAL held_role to
        # match against, not a guess — an empty/wrong value would never
        # match and the delete would silently no-op every time.
        claim_row = read_session_role_claim(state, session_id)
        if claim_row is not None:
            delete_session_role_claim_if_still_holds(
                state, agent_session_id=session_id,
                expected_held_role=str(claim_row.get("held_role") or ""),
            )
    current = str(read_managed_session(state, agent_instance_id).get("lifecycle_state") or "")
    if current == LIFECYCLE_RETIRED:
        return {"already_retired": True, "dependencies_fired": fired}
    try:
        transition_lifecycle_state(
            state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_TERMINATED,
            to_state=LIFECYCLE_RETIRED, directed_by=directed_by, reason="retire_session",
        )
    except StaleLifecycleStateError as exc:
        raise VerbError("stale_lifecycle_state", str(exc)) from exc
    return {"already_retired": False, "dependencies_fired": fired}


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
                "condition_kind='session_terminal' requires condition_ref to be "
                "an agent_instance_id (the 'agi-' prefix every instance id "
                f"shares); got {condition_ref!r}.",
            )
    elif condition_kind == CONDITION_DEADLINE:
        try:
            datetime.fromisoformat(condition_ref)
        except ValueError as exc:
            raise VerbError(
                "invalid_condition_ref",
                "condition_kind='deadline' requires condition_ref to be an "
                f"ISO-8601 timestamp; {condition_ref!r} does not parse: {exc}",
            ) from exc
    elif condition_kind == CONDITION_LANE_CLOSED and not condition_ref:
        raise VerbError(
            "invalid_condition_ref",
            "condition_kind='lane_closed' requires a non-empty condition_ref "
            "(the lane_id).",
        )


def arm_session_dependency(
    state: StateManagementInterface, req: ArmSessionDependencyRequest,
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
            "arm_session_dependency requires a non-empty waiter_instance_id "
            "(session-scoped only in v1 — lane-scoped arming is unsupported "
            "by construction; this verb has no waiter_lane_id parameter).",
        )
    if req.condition_kind not in _VALID_CONDITION_KINDS:
        raise VerbError(
            "unknown_condition_kind",
            f"condition_kind {req.condition_kind!r} is not one of "
            f"{sorted(_VALID_CONDITION_KINDS)}.",
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


def _rearm_report_by(
    state: StateManagementInterface, agent_instance_id: str, *, report_by_seconds: int = 0,
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
    window = report_by_seconds or DEFAULT_REPORT_BY_SECONDS
    next_report_by = (datetime.now(UTC) + timedelta(seconds=window)).isoformat()
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": agent_instance_id}},
        {"report_by": next_report_by},
    )


def _row_report_by_seconds(row: dict[str, Any]) -> int:
    """Split out of :func:`report_alive` to keep it under the radon cc
    threshold — the row's own spawn-time window, or 0 (falls back to
    :data:`DEFAULT_REPORT_BY_SECONDS` inside :func:`_rearm_report_by`)."""
    return int(row.get("report_by_seconds") or 0)


def report_alive(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    status: str,
    directed_by: str,
    status_note: str = "",
) -> dict[str, Any]:
    """§4 ``report_alive`` — re-arms ``report_by`` on EVERY call (whether or
    not a state transition occurs); ``status`` drives the ``live <-> idle``
    edge, and a late report recovers ``overdue -> live/idle`` (both legal
    edges in the §3.2 matrix). A report on a ``parked``/terminal row is
    ``lifecycle_state_conflict`` — state skew fails loud, never
    accept-and-log. On ``stale_lifecycle_state`` (a race with the sweep), the
    caller gets ONE bounded retry against the freshly-read state before
    failing loud (verb-internal, Dawn ruling (b))."""
    to_state = _REPORT_ALIVE_EDGE.get(status)
    if to_state is None:
        raise VerbError(
            "unknown_status", f"report_alive status must be one of working|idle, got {status!r}.",
        )
    for attempt in range(2):
        try:
            row = read_managed_session(state, agent_instance_id)
        except SessionNotFoundError as exc:
            raise VerbError("session_not_found", str(exc)) from exc
        current = str(row.get("lifecycle_state") or "")
        if current in (LIFECYCLE_PARKED, *_TERMINAL_STATES):
            raise VerbError(
                "lifecycle_state_conflict",
                f"report_alive arrived on a {current!r} row — state skew, not "
                "accepted (parked/terminal rows never self-report back to life).",
            )
        row_report_by_seconds = _row_report_by_seconds(row)
        if current == to_state:
            _rearm_report_by(state, agent_instance_id, report_by_seconds=row_report_by_seconds)
            return {"lifecycle_state": current, "recovered": False}
        try:
            transition_lifecycle_state(
                state, agent_instance_id=agent_instance_id, from_state=current,
                to_state=to_state, directed_by=directed_by,
                reason=status_note or "report_alive",
            )
            _rearm_report_by(state, agent_instance_id, report_by_seconds=row_report_by_seconds)
            return {"lifecycle_state": to_state, "recovered": current == LIFECYCLE_OVERDUE}
        except StaleLifecycleStateError as exc:
            if attempt == 1:
                raise VerbError("stale_lifecycle_state", str(exc)) from exc
            continue
    raise VerbError("stale_lifecycle_state", "report_alive lost the race twice.")


__all__ = [
    "CaptureLaneCharterRequest",
    "FALLBACK_FIRST_TURN_TEXT",
    "FIRST_TURN_SOURCE_CHARTER",
    "FIRST_TURN_SOURCE_FALLBACK",
    "LegislateRoleRequest",
    "SpawnSessionRequest",
    "VerbError",
    "capture_lane_charter",
    "clear_session",
    "compact_session",
    "drive_on_delivery",
    "drive_session",
    "legislate_role",
    "list_sessions",
    "report_alive",
    "retire_session",
    "session_status",
    "spawn_session",
    "terminate_session",
]
