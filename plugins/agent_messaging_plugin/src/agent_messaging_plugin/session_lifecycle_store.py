"""Fleet session-management Phase B, D1 (§3.2/§3.3, AMEND 2a/2b) — the
``managed_session`` lifecycle ledger + its append-only ``session_transition``
audit trail. The state-layer primitives the L1 verb surface (§4) is a thin
wrapper over.

TWO GUARDS, TWO FAILURE MODES (do not conflate them — this is the whole
point of the split):

* An ILLEGAL edge (a caller asking for a transition the §3.2 matrix does not
  allow, e.g. ``live`` straight to ``retired``) is a Python-level check
  against :data:`schema.LIFECYCLE_TRANSITIONS`, BEFORE any state write. It
  raises :class:`IllegalLifecycleTransitionError` — a caller bug, never
  retried.
* A LEGAL edge that LOSES A RACE (the sweep and the steward both moving the
  same row) is caught by the predicated ``update_state`` — ``rows_affected
  == 0`` means another writer moved the row first. It raises
  :class:`StaleLifecycleStateError` — the AMEND-2b error token
  ``stale_lifecycle_state``, a race outcome, not a caller bug.

If both checks fed the same error token, a test driving an illegal edge
would never exercise the CAS predicate at all — the exact
``reference_a_second_guard_makes_the_first_guards_legs_vacuous`` trap. Each
guard owns its own token; each has its own test.

ORDERING (ledger write BEFORE audit insert, never the reverse): the
predicated ``managed_session.lifecycle_state`` write happens FIRST; the
``session_transition`` audit row is inserted ONLY once that write reports
``rows_affected == 1``. Inserting the audit row first and losing the ledger
race after would record a transition that never happened — the audit trail
lying is worse than a crash between the two steps losing one audit row for a
transition that DID happen (a lost race here still leaves the transition
undocumented for THAT window, but never invents one).

``directed_by`` is the server-built ``CallContext`` principal
(``state.get("call_context")`` in every ``@platform_process`` method) — a
COARSE authorization discriminator (``operator`` / ``operator_equivalent`` /
``external`` / ``plugin``), NOT the spawning session's identity. It answers
"who was authorized to direct this," never "which peer requested it" — that
provenance lives in ``managed_session.spawned_by_instance_id`` /
``spawned_by_role`` (lineage columns, §7). Do not conflate the two.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)

from .schema import (
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TRANSITIONS,
    SESSION_VISIBILITY_HEADLESS,
    TABLE_LANE_CHARTER,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_TRANSITION,
)
from .session_hosts import OPERATOR_HOST

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_IS_DELETED = "is_deleted"
_COL_LIFECYCLE_STATE = "lifecycle_state"

# Single source of truth (session_lifecycle_verbs.py's _rearm_report_by
# imports this rather than redefining it) — interim fixed window, pending
# the §6 rule 1 per-work_class config substrate (deferred to a later D-step).
DEFAULT_REPORT_BY_SECONDS = 300


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def format_directed_by(call_context: CallContext | None) -> str:
    """The server-built principal as an audit string — ``"kind:id"`` or
    ``"kind:plugin"``, whichever identifier the context actually carries.
    Empty when there is no context at all (never fabricated)."""
    if call_context is None:
        return ""
    ident = call_context.principal_id or call_context.calling_plugin or ""
    return f"{call_context.principal_kind}:{ident}" if ident else call_context.principal_kind


class SessionNotFoundError(Exception):
    """No ``managed_session`` row exists for the given ``agent_instance_id``."""

    def __init__(self, agent_instance_id: str) -> None:
        self.agent_instance_id = agent_instance_id
        super().__init__(f"session_not_found: no managed_session row for {agent_instance_id!r}")


class IllegalLifecycleTransitionError(Exception):
    """The requested edge is not in the §3.2 transition matrix — a caller
    bug (asking for a transition that was never legal), never a race."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal_lifecycle_transition: {from_state!r} -> {to_state!r} is not "
            "a legal edge in the §3.2 transition matrix.",
        )


class StaleLifecycleStateError(Exception):
    """The predicated write lost its race — another writer (sweep, steward,
    a concurrent verb call) already moved this row off ``from_state``. A
    legal edge, a lost race — never a caller bug, and never silently
    retried by this module (the CALLER decides whether to re-read + retry)."""

    def __init__(self, agent_instance_id: str, from_state: str, to_state: str) -> None:
        self.agent_instance_id = agent_instance_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"stale_lifecycle_state: {agent_instance_id!r} is no longer in "
            f"{from_state!r} (requested {from_state!r} -> {to_state!r}).",
        )


@dataclass(frozen=True, slots=True)
class ManagedSessionSpec:
    """The ``spawn_session`` ledger-row-before-host-dispatch record (§4)."""

    agent_instance_id: str
    lane_id: str
    brief_ref: str
    work_class: str
    budget_line: str
    host: str
    spawned_by_instance_id: str = ""
    spawned_by_role: str = ""
    role_name: str = ""
    visibility: str = SESSION_VISIBILITY_HEADLESS
    model: str = ""
    effort: str = ""
    report_by_seconds: int = 0
    ttl_seconds: int = 0
    directed_by: str = ""


def insert_managed_session(
    state: StateManagementInterface, spec: ManagedSessionSpec,
) -> dict[str, Any]:
    """Write the ``managed_session`` ledger row in ``spawning`` state BEFORE
    host dispatch (§4 — a half-failed spawn stays visible by construction).
    ``agent_instance_id`` is UNIQUE, so a caller retrying a failed dispatch
    with the SAME id would conflict here rather than double-insert; callers
    mint a fresh id per spawn attempt."""
    now = _now_iso()
    record: dict[str, Any] = {
        _COL_AGENT_INSTANCE_ID: spec.agent_instance_id,
        "lane_id": spec.lane_id,
        "brief_ref": spec.brief_ref,
        "work_class": spec.work_class,
        "budget_line": spec.budget_line,
        "host": spec.host,
        "spawned_by_instance_id": spec.spawned_by_instance_id,
        "spawned_by_role": spec.spawned_by_role,
        "visibility": spec.visibility,
        "model": spec.model,
        "effort": spec.effort,
        "capability_report": {},
        _COL_LIFECYCLE_STATE: LIFECYCLE_SPAWNING,
        "last_transition_at": now,
        "directed_by": spec.directed_by,
    }
    # Always persisted (even 0) — the WINDOW LENGTH itself, distinct from
    # "report_by" (the computed deadline below). This is what lets
    # _rearm_report_by (session_lifecycle_verbs.py) re-arm from the spawn's
    # OWN requested window instead of a hardcoded default on every later
    # report_alive/drive_session call.
    #
    # A4 Slice 0 (measured gap, not assumed): a caller-omitted
    # report_by_seconds (0) used to leave NON-operator rows with no
    # report_by at all until their first report_alive/drive_session call —
    # a spawn-to-first-report window invisible to sweep_overdue_sessions,
    # which reads report_by IS NULL as "no contract" (the correct read for
    # an operator row, which has none by design). Every non-operator host
    # now gets a contract from the moment the row is visible, defaulted to
    # the SAME fallback _rearm_report_by already uses, so the row's own
    # report_by_seconds stays self-describing for every later re-arm.
    effective_report_by_seconds = spec.report_by_seconds
    if not effective_report_by_seconds and spec.host != OPERATOR_HOST:
        effective_report_by_seconds = DEFAULT_REPORT_BY_SECONDS
    record["report_by_seconds"] = effective_report_by_seconds
    if effective_report_by_seconds:
        record["report_by"] = (
            datetime.now(UTC) + timedelta(seconds=effective_report_by_seconds)
        ).isoformat()
    if spec.ttl_seconds:
        record["expires_at"] = (
            datetime.now(UTC) + timedelta(seconds=spec.ttl_seconds)
        ).isoformat()
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "record": record},
        ),
        "insert managed_session",
    )
    return record


def read_managed_session(
    state: StateManagementInterface, agent_instance_id: str,
) -> dict[str, Any]:
    """The live ``managed_session`` row, or :class:`SessionNotFoundError`."""
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
    )
    records = require_records(result)
    if not records:
        raise SessionNotFoundError(agent_instance_id)
    return records[0]


@dataclass(frozen=True, slots=True)
class LaneCharterSpec:
    """The seat-captured operator charter for a lane (fleet-watch-transport-
    migration phase 2 slice 6, design check-in ruling item 3) — driven
    byte-exact as a spawned worker's literal first turn."""

    lane_id: str
    charter_text: str
    captured_at: str
    brief_ref: str = ""
    directed_by: str = ""


def capture_lane_charter(
    state: StateManagementInterface, spec: LaneCharterSpec,
) -> dict[str, Any]:
    """Insert-only capture: ALWAYS writes a NEW ``lane_charter`` row, never
    updates a prior one — the exact ``session_transition`` append-only
    shape. A later charter for the same ``lane_id`` supersedes by recency
    (:func:`resolve_lane_charter` reads the latest row by ``captured_at``
    desc); there is no update path for ``charter_text`` anywhere in this
    codebase, which is what makes "the stored words are write-once" true
    rather than merely documented."""
    if not spec.lane_id:
        raise ValueError("capture_lane_charter requires a non-empty lane_id.")
    if not spec.charter_text:
        raise ValueError("capture_lane_charter requires non-empty charter_text.")
    record: dict[str, Any] = {
        "lane_id": spec.lane_id,
        "charter_text": spec.charter_text,
        "brief_ref": spec.brief_ref,
        "captured_at": spec.captured_at,
        "directed_by": spec.directed_by,
    }
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_LANE_CHARTER, "record": record},
        ),
        "insert lane_charter",
    )
    return record


@dataclass(frozen=True, slots=True)
class LaneCharterRecord:
    """The latest captured charter for a lane, as :func:`resolve_lane_charter`
    resolves it — widened beyond a bare ``charter_text`` string (2026-08-06,
    phase-3 charter-rider provenance framing) so a caller can build the
    provenance frame (captured_at + brief_ref) around the verbatim body
    without a second read."""

    charter_text: str
    captured_at: str
    brief_ref: str


def resolve_lane_charter(
    state: StateManagementInterface, lane_id: str,
) -> LaneCharterRecord | None:
    """The latest ``lane_charter`` row for ``lane_id``, or ``None`` if none
    is on file (an ordinary lane with no captured charter — never a fault).
    The ``(captured_at desc, id desc)`` order mirrors the platform's own
    latest-row precedent (``agent_messaging/repository.py``'s peer-thread
    lookup) — ``query_ordered``'s >=2-order-col contract forces a
    deterministic pick among same-instant captures rather than leaving the
    tie undefined."""
    if not lane_id:
        return None
    result = state.query_ordered(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_LANE_CHARTER,
            "filters": {"lane_id": lane_id},
            "order_by": [["captured_at", "desc"], ["id", "desc"]],
            "limit": 1,
        },
    )
    records = require_records(result)
    if not records:
        return None
    row = records[0]
    return LaneCharterRecord(
        charter_text=str(row.get("charter_text") or ""),
        captured_at=str(row.get("captured_at") or ""),
        brief_ref=str(row.get("brief_ref") or ""),
    )


def list_managed_sessions(
    state: StateManagementInterface, filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """All live ``managed_session`` rows matching ``filters`` (class/lane/
    state/host — §4 ``list_sessions``, the ONE fleet list)."""
    query_filters: dict[str, Any] = {_COL_IS_DELETED: 0}
    query_filters.update(filters or {})
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_SESSION, "filters": query_filters},
    )
    return require_records(result)


_SPAWN_AGENT_SESSION_ID_PREFIX = "ases-"
"""``tmux_adapter.py``/``headless_adapter.py`` both mint
``agent_session_id = f"ases-{agent_instance_id}"`` for every ``spawn_session``
lineage, exactly once, never re-derived (both adapters' own comments say so
verbatim). Confirmed by direct read of both construction sites (only two
spawn/host-driver paths in this repo) — spawn/registration-gaps findings,
2026-08-08, "the embedding IS guaranteed by construction, for spawn lineage
only." An operator-launched session's ``agent_session_id`` (e.g. the seat's
own ``ases-<epoch>-<pid>-<random>``, minted independently in ``~/.zshrc``)
never matches this shape, so recovery below harmlessly fails to resolve a
row for it — the genuine no-op path is preserved by construction, not by a
separate branch."""


def _recover_spawn_instance_id(agent_session_id: str) -> str | None:
    """The spawn-time ``agent_instance_id`` this ``agent_session_id`` embeds,
    or ``None`` if it doesn't have the spawn-lineage shape at all. A pure
    string-parse of a DETERMINISTIC, guaranteed-by-construction embedding
    (see :data:`_SPAWN_AGENT_SESSION_ID_PREFIX`) — recovers the exact
    original key, never a fuzzy or ambiguous match."""
    if not agent_session_id.startswith(_SPAWN_AGENT_SESSION_ID_PREFIX):
        return None
    recovered = agent_session_id[len(_SPAWN_AGENT_SESSION_ID_PREFIX):]
    return recovered or None


def backfill_registration(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    agent_id: str,
    agent_session_id: str,
) -> None:
    """The registration-hook fix (§3.2/§5, Dawn ruling arm-11511b07): a
    ``managed_session`` row spawned through ``spawn_session`` carries
    ``agent_session_id``/``agent_id`` as NULL until the spawned process
    actually registers with the platform — nothing previously wrote them.
    Call this from ``http_routes.peer_register_route`` right after
    ``peer_registry.register`` succeeds, for EVERY registration (not just the
    first): a no-op when no ``managed_session`` row exists for
    ``agent_instance_id`` (an operator-launched session with no spawn
    lineage is normal, not an error — never create a row here), otherwise an
    unconditional backfill of the identity columns (self-correcting across
    reconnects, mirroring the state-table self-refresh pattern this route
    already runs). The ``spawning -> live`` lifecycle edge fires ONLY the
    first time (guarded by the row still being ``spawning``); a later
    reconnect of an already-``live``/``idle`` session must never re-fire it
    or clobber a lifecycle_state a sweep/steward has since moved on.

    **Fallback reconciliation (spawn/registration-gaps fix, 2026-08-08,
    coordinator-seat ruling — guards below are conditions, not suggestions):** a
    ``watch``-hosted worker deliberately registers under a DIFFERENT id than
    its spawn-time ``agent_instance_id`` (``_resolve_watch_identity``,
    ``local_cli/cli.py`` — a real, separate requirement, REL-07 reconnect
    survival). When the primary lookup misses, recover the spawn-time id
    from ``agent_session_id`` (guaranteed-by-construction, see
    :func:`_recover_spawn_instance_id`) and retry — but ONLY as a fallback
    (guard 1: never the primary path), ONLY when the recovered row is STILL
    ``spawning`` (guard 2: a row that already backfilled belongs to a
    lineage that completed — a later registration must never re-key it),
    and FAILING LOUD rather than silently guessing when the recovered row
    exists but isn't still spawning (guard 3). The documented genuine no-op
    (no row under either id — an operator-launched session with no spawn
    lineage) stays exactly as quiet as before (guard 4) — recovery failing
    to resolve ANY row is indistinguishable from, and handled identically
    to, never having attempted recovery at all.
    """
    try:
        row = read_managed_session(state, agent_instance_id)
        matched_instance_id = agent_instance_id
    except SessionNotFoundError:
        recovered_id = _recover_spawn_instance_id(agent_session_id)
        if recovered_id is None:
            return
        try:
            row = read_managed_session(state, recovered_id)
        except SessionNotFoundError:
            return
        if str(row.get(_COL_LIFECYCLE_STATE) or "") != LIFECYCLE_SPAWNING:
            logger.warning(
                "backfill_registration: %s registered with agent_session_id %s, "
                "which recovers spawn id %s -- but that row is no longer "
                "'spawning' (lifecycle_state=%s). Refusing to re-key a "
                "completed lineage; row left untouched.",
                agent_instance_id, agent_session_id, recovered_id,
                row.get(_COL_LIFECYCLE_STATE),
            )
            return
        matched_instance_id = recovered_id
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: matched_instance_id, _COL_IS_DELETED: 0},
        },
        {"agent_session_id": agent_session_id, "agent_id": agent_id},
    )
    if str(row.get(_COL_LIFECYCLE_STATE) or "") != LIFECYCLE_SPAWNING:
        return
    try:
        transition_lifecycle_state(
            state, agent_instance_id=matched_instance_id, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="registration_hook",
            reason="first registration after spawn",
        )
    except StaleLifecycleStateError:
        # Lost the race (e.g. the spawned process crashed and something else
        # already moved the row to 'terminated' before this registration
        # landed) -- the identity backfill above already happened; the
        # lifecycle edge is this function's second job, not its only one.
        logger.info(
            "backfill_registration: %s lost the spawning->live race (already "
            "moved on) -- identity columns still backfilled.",
            matched_instance_id,
        )


def set_host_ref(
    state: StateManagementInterface, *, agent_instance_id: str, host_ref: str,
) -> None:
    """Persist the adapter's ``spawn()`` return value (§5) -- previously
    discarded except in the verb's own response dict, so ``session_status``
    could never show it and a later ``terminate``/``driver_channel`` call had
    no way to find the process it was supposed to act on."""
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: agent_instance_id, _COL_IS_DELETED: 0},
        },
        {"host_ref": host_ref},
    )


def transition_lifecycle_state(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    from_state: str,
    to_state: str,
    directed_by: str,
    reason: str = "",
) -> None:
    """Predicated ``lifecycle_state`` write (§3.2 matrix) + the AMEND-2a
    append-only audit insert. Raises :class:`IllegalLifecycleTransitionError`
    (illegal edge, checked BEFORE any write) or
    :class:`StaleLifecycleStateError` (legal edge, lost the CAS race — the
    ledger write itself, not the audit insert, is what raises). A successful
    call has ALREADY inserted the audit row when it returns.
    """
    if to_state not in LIFECYCLE_TRANSITIONS.get(from_state, frozenset()):
        raise IllegalLifecycleTransitionError(from_state, to_state)
    now = _now_iso()
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_LIFECYCLE_STATE: from_state,
                    _COL_IS_DELETED: 0,
                },
            },
            {_COL_LIFECYCLE_STATE: to_state, "last_transition_at": now, "directed_by": directed_by},
        ),
    )
    if updated != 1:
        raise StaleLifecycleStateError(agent_instance_id, from_state, to_state)
    # AMEND 2a: audit AFTER the ledger write succeeds — never document a
    # transition that lost its race. Insert-only; concurrent writers never
    # contend on this table.
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_TRANSITION,
                "record": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    "from_state": from_state,
                    "to_state": to_state,
                    "directed_by": directed_by,
                    "reason": reason,
                    "occurred_at": now,
                },
            },
        ),
        "insert session_transition",
    )


__all__ = [
    "IllegalLifecycleTransitionError",
    "LaneCharterRecord",
    "LaneCharterSpec",
    "ManagedSessionSpec",
    "SessionNotFoundError",
    "StaleLifecycleStateError",
    "backfill_registration",
    "capture_lane_charter",
    "format_directed_by",
    "insert_managed_session",
    "list_managed_sessions",
    "read_managed_session",
    "resolve_lane_charter",
    "set_host_ref",
    "transition_lifecycle_state",
]
