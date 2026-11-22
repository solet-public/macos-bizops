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
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)
from ananta.services.state_service.bounded_read import (
    PagedReadError,
    ReadCeilingError,
)

from .schema import (
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    LIFECYCLE_TRANSITIONS,
    SESSION_VISIBILITY_HEADLESS,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_TRANSITION,
)
from .session_end_reason import UNKNOWN, require_end_reason
from .session_hosts import DEFAULT_AGENT_RUNTIME, OPERATOR_HOST

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext
    from ananta.interfaces.state_management_interface import StateManagementInterface


logger = logging.getLogger(__name__)

_COL_AGENT_INSTANCE_ID = "agent_instance_id"
_COL_IS_DELETED = "is_deleted"

#: Rows :func:`list_managed_sessions` will walk before refusing. NOT a claim that
#: the fleet ledger is small — it is one row per managed session ever spawned and
#: nothing prunes it, which is exactly how it crossed the 100-row cap (measured
#: 106 live rows, 2026-08-16). It is a claim about this call site: a fleet list
#: that has walked a million rows is not a list anyone can read, and the caller
#: wants a filter rather than a longer walk.
_MANAGED_SESSION_WALK_CEILING = 1_000_000

_MANAGED_SESSION_CEILING_REASON = "one row per managed session ever spawned; the ledger is append-mostly and is not pruned (106 live rows measured 2026-08-16)."
_COL_LIFECYCLE_STATE = "lifecycle_state"

# These deliberately mirror bounded_read's ordered cursor contract. Managed
# sessions are lifecycle authority: an absent ``data.records`` envelope or a
# non-row member cannot be silently interpreted as proof that no active owner
# exists. Keep the strict decoder local rather than weakening the shared
# helper's compatibility contract for unrelated readers.
_MANAGED_SESSION_PAGE_ROWS = 100
_MANAGED_SESSION_ORDER_BY = [["created_at", "asc"], ["id", "asc"]]
_COL_CREATED_AT = "created_at"
_COL_ID = "id"

# Single source of truth (session_lifecycle_verbs.py's _rearm_report_by
# imports this rather than redefining it) — the fixed default report window
# for a spawn that requests none.
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


class ManagedSessionFilterRequiredError(ValueError):
    """A default fleet-ledger read was attempted without a predicate."""


class IllegalLifecycleTransitionError(Exception):
    """The requested edge is not in the §3.2 transition matrix — a caller
    bug (asking for a transition that was never legal), never a race."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"illegal_lifecycle_transition: {from_state!r} -> {to_state!r} is not a legal edge in the §3.2 transition matrix.",
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
            f"stale_lifecycle_state: {agent_instance_id!r} is no longer in {from_state!r} (requested {from_state!r} -> {to_state!r}).",
        )


@dataclass(frozen=True, slots=True)
class ManagedSessionSpec:
    """The ``spawn_session`` ledger-row-before-host-dispatch record (§4)."""

    agent_instance_id: str
    host: str
    agent_runtime: str = DEFAULT_AGENT_RUNTIME
    spawned_by_instance_id: str = ""
    spawned_by_role: str = ""
    role_name: str = ""
    # W6 (#13 §44.3): the name the worker answers to on its own machine.
    # Empty means "derive it" — insert_managed_session falls back to
    # role_name.
    local_name: str = ""
    # W4A item 3: an EXPLICIT operator choice to spawn onto a host whose
    # preflight found worker hooks unable to run. Default off.
    degraded_hooks_acknowledged: bool = False
    visibility: str = SESSION_VISIBILITY_HEADLESS
    model: str = ""
    effort: str = ""
    report_by_seconds: int = 0
    directed_by: str = ""
    # Opaque caller text, stored and reported, never parsed.
    label: str = ""
    # The session's working directory, used verbatim as the host cwd; nothing
    # is created or removed in it.
    workspace: str = ""
    session_origin: Literal["spawned", "registered", "historical_read_only", "synthetic", "unknown"] = "unknown"


def insert_managed_session(
    state: StateManagementInterface,
    spec: ManagedSessionSpec,
) -> dict[str, Any]:
    """Write the ``managed_session`` ledger row in ``spawning`` state BEFORE
    host dispatch (§4 — a half-failed spawn stays visible by construction).
    ``agent_instance_id`` is UNIQUE, so a caller retrying a failed dispatch
    with the SAME id would conflict here rather than double-insert; callers
    mint a fresh id per spawn attempt."""
    now = _now_iso()
    record: dict[str, Any] = {
        _COL_AGENT_INSTANCE_ID: spec.agent_instance_id,
        "host": spec.host,
        "agent_runtime": spec.agent_runtime,
        "spawned_by_instance_id": spec.spawned_by_instance_id,
        "spawned_by_role": spec.spawned_by_role,
        # W6: role_name was a ManagedSessionSpec field that this writer never
        # persisted and no caller ever passed — an inert knob. It is
        # load-bearing now (it is what the incumbent refusal names), so it is
        # written, and an empty local_name is derived from it.
        "role_name": spec.role_name,
        "local_name": spec.local_name or spec.role_name,
        "degraded_hooks_acknowledged": spec.degraded_hooks_acknowledged,
        "visibility": spec.visibility,
        "model": spec.model,
        "effort": spec.effort,
        "capability_report": {},
        _COL_LIFECYCLE_STATE: LIFECYCLE_SPAWNING,
        "last_transition_at": now,
        "directed_by": spec.directed_by,
        "label": spec.label,
        "workspace": spec.workspace,
        "session_origin": spec.session_origin,
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
    # a spawn-to-first-report window with no report_by at all, which reads
    # as "no contract" (the correct read for an operator row, which has
    # none by design). Every non-operator host
    # now gets a contract from the moment the row is visible, defaulted to
    # the SAME fallback _rearm_report_by already uses, so the row's own
    # report_by_seconds stays self-describing for every later re-arm.
    effective_report_by_seconds = spec.report_by_seconds
    if not effective_report_by_seconds and spec.host != OPERATOR_HOST:
        effective_report_by_seconds = DEFAULT_REPORT_BY_SECONDS
    record["report_by_seconds"] = effective_report_by_seconds
    if effective_report_by_seconds:
        record["report_by"] = (datetime.now(UTC) + timedelta(seconds=effective_report_by_seconds)).isoformat()
    require_completed(
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_SESSION, "record": record},
        ),
        "insert managed_session",
    )
    return record


def read_managed_session(
    state: StateManagementInterface,
    agent_instance_id: str,
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


def _iter_managed_session_rows(
    state: StateManagementInterface,
    *,
    filters: dict[str, Any],
    ceiling: int,
    reason: str,
) -> Iterator[dict[str, Any]]:
    """The one soft-delete-aware, complete managed-session keyset walk."""
    query_filters = dict(filters)
    requested_is_deleted = query_filters.pop(_COL_IS_DELETED, 0)
    include_deleted = requested_is_deleted != 0
    if include_deleted:
        query_filters[_COL_IS_DELETED] = requested_is_deleted
    after: list[object] | None = None
    seen = 0
    while True:
        query: dict[str, object] = {
            "table": TABLE_MANAGED_SESSION,
            "filters": query_filters,
            "order_by": _MANAGED_SESSION_ORDER_BY,
            "limit": _MANAGED_SESSION_PAGE_ROWS,
            "include_deleted": include_deleted,
        }
        if after is not None:
            query["after"] = after
        records = _complete_managed_session_page(
            state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query),
        )
        if not records:
            return
        for record in records:
            seen += 1
            if seen > ceiling:
                raise ReadCeilingError(
                    f"paged walk of table {TABLE_MANAGED_SESSION!r} passed its declared ceiling of {ceiling} rows, so it was refused rather than run to completion. The ceiling was justified as: {reason}",
                )
            yield record
        if len(records) < _MANAGED_SESSION_PAGE_ROWS:
            return
        last = records[-1]
        if _COL_CREATED_AT not in last or _COL_ID not in last:
            raise PagedReadError(
                f"paged read of table {TABLE_MANAGED_SESSION!r}: a row is missing the cursor columns {_COL_CREATED_AT!r}/{_COL_ID!r}, so the walk cannot advance without risking skipped or repeated rows: {last!r}",
            )
        after = [last[_COL_CREATED_AT], last[_COL_ID]]


def _complete_managed_session_page(result: object) -> list[dict[str, Any]]:
    """Decode lifecycle authority pages without discarding incomplete evidence."""
    records = _managed_session_page_records(result)
    if not all(_managed_session_cursor_is_complete(record) for record in records):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: a row has missing cursor values {_COL_CREATED_AT!r}/{_COL_ID!r}: {records!r}",
        )
    return records


def _managed_session_page_records(result: object) -> list[dict[str, Any]]:
    """Require the completed result envelope and its homogeneous row list."""
    if not isinstance(result, dict):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: state result is not a dict: {result!r}",
        )
    if str(result.get("action_status", "")) != "completed":
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r} did not complete (action_status={result.get('action_status')!r}): {result!r}",
        )
    data = result.get("data")
    records = data.get("records") if isinstance(data, dict) else None
    if not isinstance(records, list):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: data.records is not a list: {records!r}",
        )
    if not all(isinstance(record, dict) for record in records):
        raise PagedReadError(
            f"paged read of table {TABLE_MANAGED_SESSION!r}: data.records contains a non-row: {records!r}",
        )
    return records


def _managed_session_cursor_is_complete(record: dict[str, Any]) -> bool:
    """A full page may advance only from durable, nonempty cursor columns."""
    return all(str(record.get(column) or "").strip() for column in (_COL_CREATED_AT, _COL_ID))


def iter_managed_sessions_unbounded(
    state: StateManagementInterface,
    *,
    reason: str,
    include_deleted: bool = False,
) -> Iterator[dict[str, Any]]:
    """Deliberately walk the whole managed-session ledger.

    The name and required reason make full-ledger consent visible at every call
    site. ``_MANAGED_SESSION_WALK_CEILING`` remains a last-resort safety fuse;
    "unbounded" distinguishes this from the ordinary filter-required API, not
    from the repository-wide paged-read ceiling discipline.
    """
    if not reason.strip():
        raise ValueError("iter_managed_sessions_unbounded requires a non-empty reason")
    yield from _iter_managed_session_rows(
        state,
        filters={_COL_IS_DELETED: [0, 1]} if include_deleted else {},
        ceiling=_MANAGED_SESSION_WALK_CEILING,
        reason=f"{reason} {_MANAGED_SESSION_CEILING_REASON}",
    )


def list_managed_sessions(
    state: StateManagementInterface,
    filters: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Materialise managed-session rows matching a required predicate.

    Empty filters refuse. A caller that genuinely needs the entire append-mostly
    ledger must opt in through :func:`iter_managed_sessions_unbounded`, where the
    full walk and its reason are visible at the call site.

    The soft-delete override is preserved: ``is_deleted=1`` opts into deleted
    rows and applies that value explicitly, while the ordinary path relies on
    ``iter_table_rows``' default live-row predicate.
    """
    return list(iter_managed_sessions(state, filters))


def iter_managed_sessions(
    state: StateManagementInterface,
    filters: dict[str, Any] | None,
) -> Iterator[dict[str, Any]]:
    """Stream filtered, strictly decoded pages without materialising a fleet list."""
    if not filters:
        raise ManagedSessionFilterRequiredError("list_managed_sessions requires a non-empty filter; use iter_managed_sessions_unbounded with a reason for a deliberate full-ledger walk")
    return _iter_managed_session_rows(
        state,
        filters=filters,
        ceiling=_MANAGED_SESSION_WALK_CEILING,
        reason=_MANAGED_SESSION_CEILING_REASON,
    )


def list_managed_sessions_bounded(
    state: StateManagementInterface,
    filters: dict[str, Any],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Return at most ``limit`` matches, refusing when match ``limit + 1`` exists."""
    if not filters:
        raise ManagedSessionFilterRequiredError("list_managed_sessions_bounded requires a non-empty filter")
    return list(
        _iter_managed_session_rows(
            state,
            filters=filters,
            ceiling=limit,
            reason=(f"list_sessions has a hard result limit of {limit}; the caller must narrow its fleet predicate rather than receive a truncated roster."),
        )
    )


def read_managed_sessions_page(
    state: StateManagementInterface,
    filters: dict[str, Any],
    *,
    limit: int,
    after: list[str] | None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read one tie-safe ascending managed-session page.

    ``managed_session`` is append-mostly, so an operator roster must page
    rather than make a claim about a maximum live population.  The cursor is
    the immutable ``(created_at, id)`` pair used by :func:`iter_table_rows`;
    keeping the id tie-break prevents same-timestamp rows from disappearing at
    a page boundary.
    """
    if not filters:
        raise ManagedSessionFilterRequiredError("read_managed_sessions_page requires a non-empty filter")
    query: dict[str, Any] = {
        "table": TABLE_MANAGED_SESSION,
        "filters": filters,
        "order_by": [["created_at", "asc"], ["id", "asc"]],
        # The extra row proves that a next page exists.  A caller may request
        # up to 250 rows, above the state service's default 100-row page cap,
        # so this explicit flag is consent to this bounded page, not a scan.
        "limit": limit + 1,
        "unbounded": True,
    }
    if after is not None:
        query["after"] = after
    rows = [dict(row) for row in require_records(state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query))]
    return rows[:limit], len(rows) > limit


_SPAWN_AGENT_SESSION_ID_PREFIX = "ases-"
"""``tmux_adapter.py``/``headless_adapter.py`` both mint
``agent_session_id = f"ases-{agent_instance_id}"`` for every ``spawn_session``
lineage, exactly once, never re-derived (both adapters' own comments say so
verbatim). Confirmed by direct read of both construction sites (only two
spawn/host-driver paths in this repo) — spawn/registration-gaps findings,
2026-08-08, "the embedding IS guaranteed by construction, for spawn lineage
only." An operator-launched session's ``agent_session_id`` (e.g. the seat's
own ``ases-<epoch>-<pid>-<random>``, minted independently in ``~/.zshrc``)
can share the prefix but does not recover an existing spawn row. The
registration route treats that explicit no-match signal as the trigger for an
honest no-contract operator inventory row."""


def _recover_spawn_instance_id(agent_session_id: str) -> str | None:
    """The spawn-time ``agent_instance_id`` this ``agent_session_id`` embeds,
    or ``None`` if it doesn't have the spawn-lineage shape at all. A pure
    string-parse of a DETERMINISTIC, guaranteed-by-construction embedding
    (see :data:`_SPAWN_AGENT_SESSION_ID_PREFIX`) — recovers the exact
    original key, never a fuzzy or ambiguous match."""
    if not agent_session_id.startswith(_SPAWN_AGENT_SESSION_ID_PREFIX):
        return None
    recovered = agent_session_id[len(_SPAWN_AGENT_SESSION_ID_PREFIX) :]
    return recovered or None


def backfill_registration(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    agent_id: str,
    agent_session_id: str,
) -> bool:
    """The registration-hook fix (§3.2/§5, Dawn ruling arm-11511b07): a
    ``managed_session`` row spawned through ``spawn_session`` carries
    ``agent_session_id``/``agent_id`` as NULL until the spawned process
    actually registers with the platform — nothing previously wrote them.
    Call this from ``http_routes.peer_register_route`` right after
    ``peer_registry.register`` succeeds, for EVERY registration (not just the
    first): returns ``False`` when no ``managed_session`` row exists for
    either lookup key, so the registration route can create the separate
    no-contract operator inventory row. Otherwise returns ``True`` after an
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
            return False
        try:
            row = read_managed_session(state, recovered_id)
        except SessionNotFoundError:
            return False
        if str(row.get(_COL_LIFECYCLE_STATE) or "") != LIFECYCLE_SPAWNING:
            logger.warning(
                "backfill_registration: %s registered with agent_session_id %s, which recovers spawn id %s -- but that row is no longer 'spawning' (lifecycle_state=%s). Refusing to re-key a completed lineage; row left untouched.",
                agent_instance_id,
                agent_session_id,
                recovered_id,
                row.get(_COL_LIFECYCLE_STATE),
            )
            return True
        matched_instance_id = recovered_id
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_SESSION,
            "filters": {_COL_AGENT_INSTANCE_ID: matched_instance_id, _COL_IS_DELETED: 0},
        },
        {"agent_session_id": agent_session_id, "agent_id": agent_id},
    )
    # W4A: registration is the exact event the watchdog was waiting for, so a
    # registration that arrives LATE clears the mark rather than leaving a row
    # that permanently reads as deaf. Cleared loudly, not silently: a worker
    # that registered late is a different story from one that never did, and
    # the next reader needs to be able to tell them apart.
    if row.get("registration_overdue_at"):
        logger.warning(
            "registration watchdog: %s registered LATE -- it was marked registration-overdue at %s (%s) and has now completed registration. Clearing the mark; the delay itself was real.",
            matched_instance_id,
            row.get("registration_overdue_at"),
            row.get("registration_overdue_reason"),
        )
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {_COL_AGENT_INSTANCE_ID: matched_instance_id, _COL_IS_DELETED: 0},
            },
            {"registration_overdue_at": None, "registration_overdue_reason": ""},
        )
    if str(row.get(_COL_LIFECYCLE_STATE) or "") != LIFECYCLE_SPAWNING:
        return True
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id=matched_instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="registration_hook",
            reason="first registration after spawn",
        )
    except StaleLifecycleStateError:
        # Lost the race (e.g. the spawned process crashed and something else
        # already moved the row to 'terminated' before this registration
        # landed) -- the identity backfill above already happened; the
        # lifecycle edge is this function's second job, not its only one.
        logger.info(
            "backfill_registration: %s lost the spawning->live race (already moved on) -- identity columns still backfilled.",
            matched_instance_id,
        )
    return True


def set_host_ref(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    host_ref: str,
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


def persist_first_turn_evidence(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    source: str,
    delivered: bool,
    error: str,
    observed_at: datetime,
) -> None:
    """Persist the spawn result fields that were previously response-only."""
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_IS_DELETED: 0,
                },
            },
            {
                "first_turn_source": source,
                "first_turn_delivered": delivered,
                "first_turn_error": error,
                "first_turn_at": observed_at.astimezone(UTC).isoformat(),
            },
        ),
    )
    if updated != 1:
        raise SessionNotFoundError(agent_instance_id)


def persist_session_liveness(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    liveness: str,
    detail: str,
    observed_at: datetime,
) -> None:
    """Persist alive/dead/unknown without treating probe faults as death."""
    if liveness not in {"alive", "dead", "unknown"}:
        raise ValueError(f"invalid host liveness {liveness!r}")
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": {
                    _COL_AGENT_INSTANCE_ID: agent_instance_id,
                    _COL_IS_DELETED: 0,
                },
            },
            {
                "host_liveness": liveness,
                "host_liveness_observed_at": observed_at.astimezone(UTC).isoformat(),
                "host_liveness_detail": detail,
                "last_reconciled_at": observed_at.astimezone(UTC).isoformat(),
            },
        ),
    )
    if updated != 1:
        raise SessionNotFoundError(agent_instance_id)


_OBSERVATION_FIELDS = frozenset({"host", "host_ref", "lifecycle_state", "last_transition_at", "last_heartbeat_at"})


def _transition_filters(instance: str, from_state: str, observation: Mapping[str, object] | None) -> dict[str, Any]:
    filters: dict[str, Any] = {_COL_AGENT_INSTANCE_ID: instance, _COL_LIFECYCLE_STATE: from_state, _COL_IS_DELETED: 0}
    if observation is not None:
        if frozenset(observation) != _OBSERVATION_FIELDS or observation["lifecycle_state"] != from_state:
            raise ValueError("expected observation must cover the exact host, lifecycle and heartbeat snapshot")
        filters.update({key: {"op": "is_null"} if value is None else value for key, value in observation.items()})
    return filters


def transition_lifecycle_state(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    from_state: str,
    to_state: str,
    directed_by: str,
    reason: str = "",
    recorded_fields: Mapping[str, object] | None = None,
    expected_observation: Mapping[str, object] | None = None,
    end_reason: str | None = None,
) -> None:
    """Predicated ``lifecycle_state`` write (§3.2 matrix) + the AMEND-2a
    append-only audit insert. Raises :class:`IllegalLifecycleTransitionError`
    (illegal edge, checked BEFORE any write) or
    :class:`StaleLifecycleStateError` (legal edge, lost the CAS race — the
    ledger write itself, not the audit insert, is what raises). A successful
    call has ALREADY inserted the audit row when it returns.

    ``end_reason`` is the typed reason an ended record carries (see
    :mod:`session_end_reason`). It is written in the same predicated update that
    moves the record to terminated, and a terminal move that names none stores
    ``unknown``. Naming one on any other move, or naming a value outside
    ``END_REASONS``, raises ``ValueError`` before anything is written.
    """
    if to_state not in LIFECYCLE_TRANSITIONS.get(from_state, frozenset()):
        raise IllegalLifecycleTransitionError(from_state, to_state)
    if end_reason is not None and to_state != LIFECYCLE_TERMINATED:
        raise ValueError(f"end_reason is written only with a move to {LIFECYCLE_TERMINATED}, not {to_state}")
    now = _now_iso()
    transition_record: dict[str, object] = {
        _COL_LIFECYCLE_STATE: to_state,
        "last_transition_at": now,
        "directed_by": directed_by,
    }
    if recorded_fields:
        transition_record.update(recorded_fields)
    if to_state == LIFECYCLE_TERMINATED:
        transition_record["end_reason"] = require_end_reason(end_reason or UNKNOWN)
    updated = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_MANAGED_SESSION,
                "filters": _transition_filters(agent_instance_id, from_state, expected_observation),
            },
            transition_record,
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
    "ManagedSessionFilterRequiredError",
    "ManagedSessionSpec",
    "SessionNotFoundError",
    "StaleLifecycleStateError",
    "backfill_registration",
    "format_directed_by",
    "insert_managed_session",
    "iter_managed_sessions",
    "iter_managed_sessions_unbounded",
    "list_managed_sessions",
    "list_managed_sessions_bounded",
    "persist_first_turn_evidence",
    "persist_session_liveness",
    "read_managed_session",
    "set_host_ref",
    "transition_lifecycle_state",
]
