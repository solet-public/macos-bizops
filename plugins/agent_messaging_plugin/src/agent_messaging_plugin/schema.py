"""Schema declarations co-located with ``agent_messaging_plugin``.

This module defines the routing schema for peer bindings.  The durable
agent-messaging tables (agent_thread, agent_message, etc.) live under
``ananta.llm.agent_messaging.schema`` and are returned from the
plugin's ``get_schema_definitions()`` — they go through the standard
``install_plugin_schema`` lifecycle.

The peer-binding schema rides the platform ``Store`` abstraction.
``PeerRegistry`` opens it with ``backend="postgres"`` (see
``workbench/2026-06-01_local_reconnect_ux_design.md`` §4) so the
``session_label`` set via ``/rename`` survives solet restarts —
restart-as-refresh is the local model and an in-memory backend would
wipe the label on every restart cycle.
"""

from __future__ import annotations

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_ID,
    COL_AGENT_INSTANCE_ID,
    COL_AGENT_SESSION_ID,
    COL_CLAIM_EPOCH,
    COL_CLAIMED_AT,
    COL_DESCRIPTION,
    COL_HOLDER_IDENTITY,
    COL_HOLDER_KIND,
    COL_MEMORY_ID,
    COL_ORIGIN,
    COL_PROPERTIES,
    COL_ROLE,
    COL_ROLE_CLASS,
    COL_SESSION_LABEL,
    HOLDER_KIND_SESSION,
    INDEX_AGENT_ROLE_BINDING_INSTANCE,
    INDEX_ROLE_BINDING_INSTANCE,
    ROLE_CLASS_DEFAULT,
    ROLE_ORIGIN_USER,
    TABLE_AGENT_ROLE_BINDING,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
)
from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

from .constants import PLUGIN_NAME

PEER_BINDING_TABLE = "peer_binding"
PEER_BINDING_ID_PREFIX = "pbn"

AGENT_ROLE_BINDING_ID_PREFIX = "arb"

# Role-model v4 fresh tables (design §4/§9).
ROLE_ID_PREFIX = "rol"
ROLE_BINDING_ID_PREFIX = "rbn"

# Fleet session-management Phase B, D1 (workbench
# 2026-08-03_fleet_session_management_phase_b_design_coordinator_dawn.md §3;
# Architect memo 2026-08-03_phase_b_architect_review_architect.md). Fresh tables
# in this plugin's own namespace — core is not expected to read any of them
# (§3.3); if that changes, the reader ships the AMEND-1a contract-module
# treatment then, not preemptively.
TABLE_SESSION_ROLE_CLAIM = "session_role_claim"
SESSION_ROLE_CLAIM_ID_PREFIX = "src"

TABLE_MANAGED_SESSION = "managed_session"
MANAGED_SESSION_ID_PREFIX = "mgs"

TABLE_SESSION_TRANSITION = "session_transition"
SESSION_TRANSITION_ID_PREFIX = "sxn"

TABLE_SESSION_DEPENDENCY = "session_dependency"
SESSION_DEPENDENCY_ID_PREFIX = "sdp"

# T1 usage-capture lane (2026-08-05, workbench
# the 2026-08-05 usage-capture ruling) — the agent_instance_id <->
# claude_session_id mapping. OWN table, not a managed_session column and not
# external_id (the SchemaStandardizer's platform-wide UNIQUE conflict-key
# slot with typed prefixes, never a free identity field): a worker's
# managed_session row is ONE-TO-MANY over its lifetime against Claude
# session UUIDs (a /clear or /resume rotates the UUID without touching the
# managed_session row), so this is its own append-friendly table, not a
# column that can only ever hold one value.
TABLE_SESSION_CLAUDE_MAPPING = "session_claude_mapping"
SESSION_CLAUDE_MAPPING_ID_PREFIX = "scm"

# maintenance-verbs M1 (workbench
# 2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3, shape (a),
# coordinator-seat ruling on Q3 same day): the hook-fed cache `session_context_status`
# reads from. ONE row per agent_instance_id (latest snapshot, always
# overwritten — never a history), decoupled from managed_session (the ONLY
# way this ever covers host=operator: that ledger structurally excludes those
# rows, but this table has no FK to it at all, same posture as
# session_claude_mapping). report_context_status is a plain upsert on this
# table; nothing here computes fraction/rotation_due at write time — those
# derive at READ time from rotation_thresholds' live constants, so a future
# threshold-fraction change never requires a backfill.
TABLE_SESSION_CONTEXT_STATUS = "session_context_status"
SESSION_CONTEXT_STATUS_ID_PREFIX = "scx"
TABLE_SESSION_CONTEXT_STATUS_HISTORY = "session_context_status_history"
SESSION_CONTEXT_STATUS_HISTORY_ID_PREFIX = "scxh"

# GAU-21 (2026-08-19) — the DURABLE record that a gauge notice fired. The
# sweep's notices are appended as in-memory bridge events only: nothing
# persists them, a restart loses every un-drained one, reading them REMOVES
# them (`BridgeSessionState.events_after` rebinds the queue), no surface is
# keyed on event type, and an unbound steward means the alarm is never emitted
# anywhere. So a missed alarm and an alarm that never fired are the same
# silence — the GAU-01 shape one level up, applied to the instrument's own
# output. This table is written at the EMIT site, before and independently of
# the steward binding, so the fact of firing survives a delivery that never
# happened.
#
# SCOPED DELIBERATELY TO THE TWO GAUGE FAMILIES. `notice_type` is a plain
# string and the seven sibling families (rotation_due, ttl_overdue, the spawn
# and registration notices, session_overdue, dependency wake) share the same
# durability defect and could be folded in unchanged. They are NOT in this
# landing: each carries its own evidence columns and its own emit-site
# posture, and folding seven unmeasured families into a table built for the
# two with measured evidence would trade the verified for the unverified.
TABLE_GAUGE_NOTICE_RECORD = "gauge_notice_record"
GAUGE_NOTICE_RECORD_ID_PREFIX = "gnr"

# GAU-15 item 4 (2026-08-19) — the TAMPER CANARY's two tables.
#
# `gauge_canary_registry` is constraint (c): the canary's provenance mark lives
# at the store plane in ITS OWN TABLE, deliberately NOT as a column on the
# gauge row. A column on the row is exactly the flag constraint (b) forbids —
# the detector reads that row, so a mark there is a mark the detection path
# could learn to read, and a detector that can tell it is being tested has
# stopped being the thing under test. Operational consumers (rotation notices,
# steward interrupts, economics) filter canaries out by JOINING this table
# deliberately; the detector never joins it.
#
# `gauge_canary_tamper` is constraint (d): every arrest is an audited row
# carrying who directed it, the window requested, and the alarm expected. This
# is what makes an alarm MECHANICALLY ATTRIBUTABLE — an alarm inside a logged
# window is scheduled, an alarm outside one is real. Without it a canary
# normalises alarms and trains stewards to ignore the instrument, which is
# worse than having no canary.
TABLE_GAUGE_CANARY_REGISTRY = "gauge_canary_registry"
GAUGE_CANARY_REGISTRY_ID_PREFIX = "gcr"

TABLE_GAUGE_CANARY_TAMPER = "gauge_canary_tamper"
GAUGE_CANARY_TAMPER_ID_PREFIX = "gct"

# `gauge_notice_record.delivery_outcome` domain. NOT a boolean, and that is
# the count-4 fix rather than a taste preference: "no steward was bound" and
# "the append raised" are different faults with different owners, and today
# both present to a reader as the same silence.
NOTICE_DELIVERY_APPENDED = "appended"
NOTICE_DELIVERY_NO_STEWARD_BINDING = "no_steward_binding"
NOTICE_DELIVERY_APPEND_FAILED = "append_failed"
NOTICE_DELIVERY_OUTCOMES = (
    NOTICE_DELIVERY_APPENDED,
    NOTICE_DELIVERY_NO_STEWARD_BINDING,
    NOTICE_DELIVERY_APPEND_FAILED,
)

# fleet-watch-transport-migration phase 2 slice 6 (2026-08-06, design
# check-in ruling item 3) — the operator's verbatim founding words for a
# lane, driven byte-exact as a spawned worker's literal first turn.
TABLE_LANE_CHARTER = "lane_charter"
LANE_CHARTER_ID_PREFIX = "lch"

# R1 held-authorization queue (2026-08-17, seat GO ruling). Git-Controller
# writes a row at REFUSAL time (declining a peer's citation of an
# authorization it cannot verify first-party in its own inbox) — never the
# requesting lane, so the entry exists mechanically regardless of any seat's
# memory. `retired_at IS NULL` = still awaiting the matching first-party
# authorization. No TTL: a queue that silently forgets is worse than no
# queue, so staleness stays visible via `created_at` rather than expiring.
TABLE_HELD_AUTHORIZATION = "held_authorization"
HELD_AUTHORIZATION_ID_PREFIX = "hau"

# ``session_claude_mapping.capture_source`` domain (ruling 2(c)) — carries
# the SessionStart hook's own ``source`` field through (the rotation story:
# a ``hook:clear`` row explains a new UUID on a surviving worker) vs
# ``init_event`` for the headless stream-json init-event cross-check.
CAPTURE_SOURCE_HOOK_STARTUP = "hook:startup"
CAPTURE_SOURCE_HOOK_CLEAR = "hook:clear"
CAPTURE_SOURCE_HOOK_RESUME = "hook:resume"
CAPTURE_SOURCE_INIT_EVENT = "init_event"

# ``managed_session.host`` domain (§5).
SESSION_HOST_TMUX = "tmux"
SESSION_HOST_HEADLESS = "headless"
SESSION_HOST_OPERATOR = "operator"

# ``managed_session.visibility`` domain (§7).
SESSION_VISIBILITY_VISIBLE = "visible"
SESSION_VISIBILITY_HEADLESS = "headless"

# ``managed_session.work_class`` domain (§6 rule 1).
WORK_CLASS_READ_ONLY = "read_only"
WORK_CLASS_ANALYSIS_DELIVERABLE = "analysis_deliverable"
WORK_CLASS_PRODUCTION_MUTATION = "production_mutation"

# ``managed_session.lifecycle_state`` domain + the AMEND-2b legal-transition
# matrix (design §3.2 table). Every write is a predicated ``update_state`` on
# the expected prior state; ``rows_affected == 0`` -> ``stale_lifecycle_state``.
LIFECYCLE_SPAWNING = "spawning"
LIFECYCLE_LIVE = "live"
LIFECYCLE_IDLE = "idle"
LIFECYCLE_OVERDUE = "overdue"
LIFECYCLE_PARKED = "parked"
LIFECYCLE_TERMINATED = "terminated"
LIFECYCLE_RETIRED = "retired"

# {from_state: {to_state, ...}} — non-terminal states may reach ``retired`` only
# via ``terminated`` (retire_session composes terminate + release + edges +
# ledger, §4); this matrix intentionally omits a direct non-terminated->retired
# edge to keep the enumerated set exactly the design's table, one row per edge.
LIFECYCLE_TRANSITIONS: dict[str, frozenset[str]] = {
    LIFECYCLE_SPAWNING: frozenset({LIFECYCLE_LIVE, LIFECYCLE_TERMINATED}),
    LIFECYCLE_LIVE: frozenset(
        {LIFECYCLE_IDLE, LIFECYCLE_OVERDUE, LIFECYCLE_PARKED, LIFECYCLE_TERMINATED},
    ),
    LIFECYCLE_IDLE: frozenset(
        {LIFECYCLE_LIVE, LIFECYCLE_OVERDUE, LIFECYCLE_PARKED, LIFECYCLE_TERMINATED},
    ),
    LIFECYCLE_OVERDUE: frozenset(
        {LIFECYCLE_LIVE, LIFECYCLE_IDLE, LIFECYCLE_PARKED, LIFECYCLE_TERMINATED},
    ),
    LIFECYCLE_PARKED: frozenset({LIFECYCLE_LIVE, LIFECYCLE_TERMINATED}),
    LIFECYCLE_TERMINATED: frozenset({LIFECYCLE_RETIRED}),
    LIFECYCLE_RETIRED: frozenset(),
}

# ``session_dependency.condition_kind`` domain (§3.4). ``lane_closed`` replaced
# the original spec's ``lane_landed`` (Dawn ruling 2026-08-03, arm-124065ee):
# no canonical "lane landed" observable exists platform-side (an ancestor
# check would bind the platform to a dev git checkout — seed instances are
# not checkouts, violating C1 universality), so v1 defines it as what the
# registry can actually observe — every managed_session row carrying the
# lane_id is terminal. Work-product landing proper needs a lane ENTITY with
# an explicit audited declaration — deferred to Phase C (the lane-office
# questions).
CONDITION_LANE_CLOSED = "lane_closed"
CONDITION_SESSION_TERMINAL = "session_terminal"
CONDITION_DEADLINE = "deadline"

# The AMEND-4b session-keyed cardinality row (Dawn ruling arm-87976ca719,
# 2026-08-03): one row per SESSION (not per role), keyed on the claimant's
# server-built ``agent_session_id`` — never the caller-asserted one (AMEND 4c).
# ``external_id = "session_role:{agent_session_id}"`` is the platform-UNIQUE
# conflict key the claim gate wins via ``upsert_state(on_conflict='do_nothing')``
# BEFORE the per-role binding CAS (AMEND 4b) — cardinality becomes a uniqueness
# fact, not a read-then-act check. ``held_role`` is read back on an insert
# conflict to disambiguate refresh / cardinality_conflict / stale-orphan
# self-repair (Dawn ruling gap 2). Displacement deletes the LOSER's row
# (``external_id = "session_role:{loser_session_id}"``), predicated on it still
# naming the displaced role (Dawn ruling gap 1) — never this claimant's own row.


def session_role_claim_external_id(agent_session_id: str) -> str:
    """Deterministic UNIQUE conflict key for the AMEND-4b cardinality row."""
    return f"session_role:{agent_session_id}"


def get_peer_binding_schema() -> TableSchema:
    """Shape of one entry in :class:`PeerRegistry`.

    Columns:

    * ``agent_id`` — agent kind ("claude_code", "codex", ...); reused
      across instances, so NOT unique.
    * ``agent_instance_id`` — durable per-instance routing key minted
      by the bridge subprocess.  ``unique`` so the cross-bucket sweep
      that ``register()`` performs (and the dispatch path's
      ``touch({"agent_instance_id": ...})``) target exactly one row.
    * ``bridge_id`` — live bridge handle.  ``unique`` so the close-time
      sweep ``unregister(bridge_id)`` matches at most one binding (and
      so an attempted re-register on the same bridge can't shadow an
      existing instance).
    * ``session_label`` — mutable human-facing display label; not a
      routing key.
    * ``parent_pid`` — OS PID of the bridge subprocess host; used by
      the native-wake adapter to pair sibling bridges.  Optional.

    Standard fields auto-injected by :class:`SchemaStandardizer`:
    ``id``, ``external_id``, ``namespace``, ``created_at`` (the
    ex-``registered_at``), ``updated_at`` (the "last active"
    timestamp), ``is_deleted``, ``created_by``, ``updated_by``, ``name``.
    """
    return TableSchema(
        table_name=PEER_BINDING_TABLE,
        description="Live MCP-bridge peer routing table",
        id_prefix=PEER_BINDING_ID_PREFIX,
        columns={
            "agent_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Agent kind (e.g., claude_code, codex).",
            ),
            "agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="Durable per-instance routing key.",
            ),
            "bridge_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="Live MCP bridge handle owning this binding.",
            ),
            "session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Human-facing display label; not a routing key.",
            ),
            "parent_pid": ColumnDefinition(
                type=ColumnType.INTEGER,
                description=(
                    "OS PID of the bridge subprocess host; used by the "
                    "native-wake adapter to pair sibling bridges."
                ),
            ),
            "agent_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "S1: STABLE per-logical-session key (survives reconnect / "
                    "agent_instance_id rotation). Surfaced via current_identity; "
                    "the reconnect state-table role self-refresh keys on it. "
                    "DATA CAPTURE only (no lookup / routing / dedup here). "
                    "Optional (empty for streamable / older clients)."
                ),
            ),
            "last_model_activity_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "REL-05: mirror of BridgeSessionState.last_model_activity_at "
                    "— the last time this instance invoked a MODEL-INITIATED "
                    "bridge route (peer/send, process/*, agent/*, ...), "
                    "NEVER a forwarder/infra route (open, events, drain, "
                    "delivered, register — F1, close, health). The consumption "
                    "reconciler reads it (from the live session, or here for the "
                    "server-side escalation sweep) to decide whether an owed "
                    "IMPORTANT send entered a turn context. Distinct from "
                    "``updated_at`` (which every dispatch/inbox/register bumps). "
                    "NULL until the instance's first model-initiated route."
                ),
            ),
            "wake_capable": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                default=1,
                description=(
                    "codex-watch-migration wake_capable design (2026-08-06): "
                    "declared by the registering bridge on every peer/register "
                    "call (never probed), matching BridgeBinding.wake_capable's "
                    "own default. True when this binding's transport has a "
                    "native turn-injection wake path (Claude Code); False for a "
                    "recipient with no such surface (stock Codex's bridge). "
                    "codex-0147-dead-spool-retirement (2026-08-13): persisted "
                    "COMPATIBILITY METADATA only — the dispatch-side spool tee "
                    "this flag used to gate is retired (stock Codex's Stop "
                    "hook cannot consume it), so nothing currently branches on "
                    "a False value at dispatch time; a Codex recipient reaches "
                    "delivery through the durable inbox / watch read path "
                    "instead. Fixed 2026-08-08 (fleet-wake-integrity Task 1): "
                    "this column was missing from the persisted schema, so "
                    "every registration's declared value was silently dropped "
                    "and every resolved binding read back the dataclass "
                    "default (True)."
                ),
            ),
            "watcher_declared": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                default=0,
                description=(
                    "MSG-04/identity-unification (2026-08-20): declared by "
                    "`solet watch` on every peer/register call (never "
                    "probed), matching BridgeBinding.watcher_declared's own "
                    "default. True when this binding is a no-MCP `watch` "
                    "subprocess registering under its ledger AGENT_INSTANCE_ID "
                    "— a caller that no longer carries the legacy "
                    "`agi-watch-` prefix BridgeBinding.is_watcher used to "
                    "infer this from. False (the default) for every other "
                    "registration path; is_watcher still also recognizes the "
                    "legacy prefix, so a manual/no-ledger-id watch (which "
                    "still mints the derived, prefixed identity) is "
                    "unaffected."
                ),
            ),
        },
    )


PEER_BINDING_NAMESPACE = PLUGIN_NAME


def get_peer_binding_schema_definition() -> SchemaDefinition:
    """Wrap :func:`get_peer_binding_schema` so the plugin's
    ``get_schema_definitions()`` can return it for installation via the
    standard ``install_plugin_schema`` lifecycle.

    Required because ``PeerRegistry`` opens a ``backend="postgres"``
    :class:`~ananta.services.store.Store` over this table at
    ``start_interface``; without the install step the underlying table
    doesn't exist and every ``peer_register`` / ``peer_send`` 500s.
    """
    return SchemaDefinition(
        namespace=PEER_BINDING_NAMESPACE,
        version="1.0.0",
        description="Live MCP-bridge peer routing table.",
        tables={PEER_BINDING_TABLE: get_peer_binding_schema()},
    )


def get_agent_role_binding_schema() -> TableSchema:
    """Shape of the ``agent_role_binding`` collection (v10 Control #2).

    One row per ROLE — the sole writable resolution + compare-and-set
    authority for role ownership, retiring the address-book ``agent_role``
    entries. Distinct from :func:`get_peer_binding_schema` (one row per
    bridge/instance registration): different cardinality and purpose.

    The schema is DECLARED here now (de-risk split so the v10 role-inbox
    section's enumeration read is testable); the claim / CAS-refresh / cutover
    logic that WRITES it lands with Control #2 and inherits this exact shape.

    Business columns (the standardizer auto-adds ``id`` PK, ``external_id``
    UNIQUE, ``created_at`` / ``updated_at`` / ``is_deleted``):

    * ``role`` — the role name; the load-bearing durable addressing key. Not
      unique on its own — one-row-per-role is enforced by the standard
      ``external_id = "role:{role}"`` UNIQUE constraint (no custom PK).
    * ``agent_id`` / ``agent_instance_id`` — the current holder's kind +
      per-bridge routing id (the enumeration read filters on
      ``agent_instance_id``, so it carries a non-unique index).
    * ``agent_session_id`` — the stable logical-session id the CAS self-refresh
      compares against (``update_state(WHERE agent_session_id=expected)``).
    * ``session_label`` — cosmetic display label; not a routing key.
    * ``claimed_at`` — explicit claim/refresh timestamp; a DISTINCT business
      column (the CAS writes it explicitly), NOT the standard ``updated_at``.
    """
    return TableSchema(
        table_name=TABLE_AGENT_ROLE_BINDING,
        description=(
            "Single writable role-ownership authority — one row per role, "
            "idempotent on external_id='role:{role}'. Backs resolve_role + "
            "the single-row update_state CAS self-refresh (Control #2)."
        ),
        id_prefix=AGENT_ROLE_BINDING_ID_PREFIX,
        columns={
            COL_ROLE: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Role name; the durable addressing key.",
            ),
            COL_AGENT_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Current holder's agent kind (claude_code, codex).",
            ),
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Current holder's per-bridge instance id.",
            ),
            COL_AGENT_SESSION_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Stable logical-session id the single-row CAS self-refresh "
                    "compares against (WHERE agent_session_id=expected)."
                ),
            ),
            COL_SESSION_LABEL: ColumnDefinition(
                type=ColumnType.TEXT,
                description="Human-facing display label; not a routing key.",
            ),
            COL_CLAIMED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "When the role was last claimed/refreshed. Distinct "
                    "business column the CAS writes explicitly."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                # The role-inbox enumeration hits query_state(filters=
                # {agent_instance_id:X}) on every inbox call. Perf only —
                # a seq-scan is correct; the lifecycle reconciles this index.
                name=INDEX_AGENT_ROLE_BINDING_INSTANCE,
                columns=[COL_AGENT_INSTANCE_ID],
            ),
        ],
    )


def get_agent_role_binding_schema_definition() -> SchemaDefinition:
    """Wrap :func:`get_agent_role_binding_schema` for ``get_schema_definitions``.

    Installed via the standard ``install_plugin_schema`` lifecycle (which runs
    the standardizer + reconciles the declared index) in the plugin's own
    namespace — the analog of :func:`get_peer_binding_schema_definition`.
    """
    return SchemaDefinition(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        version="1.0.0",
        description="Single writable role-ownership authority (Control #2).",
        tables={TABLE_AGENT_ROLE_BINDING: get_agent_role_binding_schema()},
    )


def get_role_schema() -> TableSchema:
    """Shape of the first-class ``role`` ENTITY (role-model v4, §4.1).

    One row per role name — the extensible + discoverable identity, auto-created
    on first claim. NEVER read on the resolve hot path (§4.3); the discriminated
    ownership authority is :func:`get_role_binding_schema`. Standardizer auto-adds
    ``id`` / ``external_id`` (UNIQUE, ``role:{role}``) / timestamps / ``is_deleted``.
    """
    return TableSchema(
        table_name=TABLE_ROLE,
        description=(
            "First-class role entity — extensible (properties JSON) + "
            "discoverable (memory-ingested); one row per role name, UNIQUE on "
            "external_id='role:{role}'. Never on the resolve hot path."
        ),
        id_prefix=ROLE_ID_PREFIX,
        columns={
            COL_ROLE: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Role name / slot identity; the durable addressing key.",
            ),
            COL_ORIGIN: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default=ROLE_ORIGIN_USER,
                description=(
                    "Provenance: 'user' (arbitrary operator name) | 'system' "
                    "(reserved-namespace slot, §6). Set from the prefix check."
                ),
            ),
            COL_DESCRIPTION: ColumnDefinition(
                type=ColumnType.TEXT,
                description="Human/operator description (nullable).",
            ),
            COL_PROPERTIES: ColumnDefinition(
                type=ColumnType.JSON,
                default="{}",
                description="Extensible property hang-point (P7) — no ALTER per property.",
            ),
            COL_MEMORY_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "The memory_service row this entity is ingested as (§7); "
                    "nullable — ingest is best-effort, never gates a claim."
                ),
            ),
            COL_ROLE_CLASS: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default=ROLE_CLASS_DEFAULT,
                description=(
                    "Fleet session-management Phase B (§2) taxonomy: primary | "
                    "principal | project | ephemeral | chat. Existing rows backfill "
                    "to 'project' (one-shot reconcile, role_class_backfill.py); "
                    "validated against reserved names (<solet>-Main, sys:*) "
                    "at claim time — class validation, not keyspace rejection."
                ),
            ),
        },
    )


def get_role_binding_schema() -> TableSchema:
    """Shape of the discriminated ``role_binding`` authority (role-model v4, §4.2).

    One row per role — the sole writable resolution + compare-and-set authority
    (Control-#2 continuity), discriminated by ``holder_kind``. The two session
    identity columns are NULLABLE (a provider holder leaves them NULL) — the
    reason this is a FRESH table, not an evolve of ``agent_role_binding`` (§9).
    Only the two ``query_state``-filterable identity fields are columns; the rest
    of the holder identity is typed-parsed JSON (§4.6), so a new holder kind needs
    no schema change. Standardizer auto-adds id / external_id (UNIQUE) / timestamps
    / is_deleted.
    """
    return TableSchema(
        table_name=TABLE_ROLE_BINDING,
        description=(
            "Discriminated role-ownership authority — one row per role, UNIQUE "
            "on external_id='role:{role}'. holder_kind session|inference_provider; "
            "predicated-CAS on claim_epoch; kind-appropriate holder_identity JSON."
        ),
        id_prefix=ROLE_BINDING_ID_PREFIX,
        columns={
            COL_ROLE: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Role name; the durable addressing key.",
            ),
            COL_HOLDER_KIND: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default=HOLDER_KIND_SESSION,
                description=(
                    "Discriminator: 'session' | 'inference_provider' (extensible). "
                    "Code semantics, NEVER a role name."
                ),
            ),
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Session holder's per-bridge instance id — the reverse-lookup "
                    "/ drain-fence filter (indexed). NULL for non-session holders."
                ),
            ),
            COL_AGENT_SESSION_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Session holder's stable logical-session id — the CAS "
                    "self-refresh predicate. NULL for non-session holders."
                ),
            ),
            COL_HOLDER_IDENTITY: ColumnDefinition(
                type=ColumnType.JSON,
                default="{}",
                description=(
                    "Kind- & transport-appropriate identity, typed-parsed once on "
                    "resolve (§4.6). session: {agent_id, session_label}; "
                    "inference_provider: {provider_kind, provider_ref, display_name}."
                ),
            ),
            COL_CLAIM_EPOCH: ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=0,
                description=(
                    "Predicated-CAS token for claim/displace (§5.1); bumped each "
                    "claim/displace, NOT by self-refresh. ABA-safe, kind-agnostic."
                ),
            ),
            COL_CLAIMED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Explicit last claim/refresh timestamp (distinct business column).",
            ),
        },
        indexes=[
            IndexDefinition(
                name=INDEX_ROLE_BINDING_INSTANCE,
                columns=[COL_AGENT_INSTANCE_ID],
            ),
        ],
    )


def get_role_model_schema_definition() -> SchemaDefinition:
    """Wrap the v4 ``role`` + ``role_binding`` tables for ``get_schema_definitions``.

    Both live in the plugin's own namespace (analog of the ``agent_role_binding``
    definition); installed via the standard ``install_plugin_schema`` lifecycle.
    Additive — the live ``agent_role_binding`` authority is untouched until the
    §9 migration switches readers + writer to ``role_binding``.
    """
    return SchemaDefinition(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        version="1.0.0",
        description="Role-model v4 — first-class role entity + discriminated binding.",
        tables={
            TABLE_ROLE: get_role_schema(),
            TABLE_ROLE_BINDING: get_role_binding_schema(),
        },
    )


def get_session_role_claim_schema() -> TableSchema:
    """AMEND-4b session-keyed cardinality row (Phase B §2, one row per SESSION).

    ``external_id = "session_role:{agent_session_id}"`` (UNIQUE) is the race
    primitive the claim gate wins via ``upsert_state(on_conflict='do_nothing')``
    BEFORE the per-role ``role_binding`` CAS — the at-most-one-named-role
    invariant becomes a uniqueness fact, not a TOCTOU check-then-claim. Keyed on
    the SERVER-BUILT ``agent_session_id`` (AMEND 4c) — never a caller-asserted
    argument. ``held_role`` is read back on an insert conflict so the claim path
    can disambiguate refresh vs. cardinality_conflict vs. a stale orphan
    (Dawn ruling, gap 2); displacement predicated-deletes the LOSER's row by
    this same external_id (gap 1).
    """
    return TableSchema(
        table_name=TABLE_SESSION_ROLE_CLAIM,
        description=(
            "One row per session — the AMEND-4b cardinality gate. UNIQUE on "
            "external_id='session_role:{agent_session_id}'; held_role names the "
            "single role this session currently holds."
        ),
        id_prefix=SESSION_ROLE_CLAIM_ID_PREFIX,
        columns={
            COL_AGENT_SESSION_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Server-built stable logical-session id (never caller-"
                    "asserted, AMEND 4c) — the row's own identity, mirrored "
                    "into external_id."
                ),
            ),
            "held_role": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The single named role this session currently holds.",
            ),
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description="Current per-bridge instance id for this session (diagnostic).",
            ),
            COL_CLAIMED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description="When this session's held_role was last claimed/refreshed.",
            ),
        },
    )


def get_managed_session_schema() -> TableSchema:
    """The lifecycle ledger (§3.2) — one row per platform-spawned session,
    plus a ``host='operator'`` row for operator-managed sessions on
    registration (so the fleet is ONE list, §4 ``list_sessions``)."""
    return TableSchema(
        table_name=TABLE_MANAGED_SESSION,
        description=(
            "Session lifecycle ledger — identity, lineage, dispatch config, "
            "host, contract (report-or-die + TTL), and the current-state "
            "lifecycle_state projection (AMEND 2b; full history in "
            "session_transition)."
        ),
        id_prefix=MANAGED_SESSION_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description="Durable per-instance identity; one managed_session row per instance.",
            ),
            COL_AGENT_SESSION_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description="Stable logical-session id (empty until first registration).",
            ),
            COL_AGENT_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description="Agent kind (claude_code, codex, ...).",
            ),
            "spawned_by_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Lineage: the spawner's agent_instance_id (empty for operator rows).",
            ),
            "spawned_by_role": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Lineage: the spawner's role name at spawn time, if any.",
            ),
            "role_name": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "The durable role this session was spawned to fill, if any. "
                    "Recording it does NOT claim the binding — spawning never claims "
                    "a role as a side effect (operator ruling 2026-08-14); the worker "
                    "claims it explicitly. This column is the spawn's stated INTENT, "
                    "which is what makes the W6 incumbent refusal legible."
                ),
            ),
            "lane_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="The lane this session was spawned for (provenance, anti-laundering).",
            ),
            "local_name": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "W6 (#13 §44.3): the name the spawned worker answers to on its "
                    "OWN machine — the headless driver's --name, the tmux label the "
                    "session name derives from. Defaulted by spawn_session to "
                    "role_name for a project-class role and lane_id otherwise. "
                    "Persisted because it is the COLLISION KEY: the Git-Controller "
                    "mutation guard resolves the caller by reading the local session "
                    "file's 'name' field and comparing it EXACTLY "
                    "(.claude/hooks/git_controller_gate.py find_session_name / "
                    "session_name == controller), so two non-terminal rows sharing a "
                    "local_name are two processes that both pass the same guard. No "
                    "uniquifying suffix is available precisely because that compare "
                    "is exact."
                ),
            ),
            "brief_ref": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Workbench path or dispatch id backing the spawn (provenance).",
            ),
            "model": ColumnDefinition(
                type=ColumnType.TEXT, description="Dispatch model override.",
            ),
            "effort": ColumnDefinition(
                type=ColumnType.TEXT, description="Dispatch effort override.",
            ),
            "work_class": ColumnDefinition(
                type=ColumnType.TEXT,
                description="read_only | analysis_deliverable | production_mutation (§6 rule 1).",
            ),
            "budget_line": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Foreign key the token-budget ledger (§3.5, deferred to L3) hangs "
                "from.",
            ),
            "visibility": ColumnDefinition(
                type=ColumnType.TEXT,
                default=SESSION_VISIBILITY_HEADLESS,
                description="visible | headless — spawn-time operator/primary parameter (§7, R6).",
            ),
            "host": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="tmux | headless | operator (§5 adapter contract).",
            ),
            "agent_runtime": ColumnDefinition(
                type=ColumnType.TEXT,
                default="claude_code",
                description=(
                    "Exact peer-registry agent_id vocabulary for the worker runtime: "
                    "claude_code | codex. Orthogonal to host topology; nullable for "
                    "declarative migration of pre-runtime rows, whose read floor is "
                    "claude_code."
                ),
            ),
            "host_ref": ColumnDefinition(
                type=ColumnType.TEXT,
                description="tmux session name / driver pid / null (§5).",
            ),
            "capability_report": ColumnDefinition(
                type=ColumnType.JSON,
                default="{}",
                description="JSON snapshot returned by the host adapter at spawn (§5).",
            ),
            "report_by": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="report-or-die deadline; re-armed on each report_alive call.",
            ),
            "report_by_seconds": ColumnDefinition(
                type=ColumnType.INTEGER,
                default=0,
                description=(
                    "Spawn-time report-or-die WINDOW LENGTH (not the deadline — "
                    "that's report_by), persisted so report_alive/drive_session can "
                    "re-arm from the spawn's own requested window instead of a "
                    "hardcoded default. 0 means the spawn didn't request a custom "
                    "window; re-arm falls back to DEFAULT_REPORT_BY_SECONDS."
                ),
            ),
            "expires_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="TTL from spawn_session's ttl_seconds, if any.",
            ),
            "lifecycle_state": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default=LIFECYCLE_SPAWNING,
                description=(
                    "Current-state projection (AMEND 2b): spawning | live | idle "
                    "| overdue | parked | terminated | retired. Every write is a "
                    "predicated update_state on the expected prior state — "
                    "rows_affected==0 -> stable error token stale_lifecycle_state."
                ),
            ),
            "last_transition_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="Timestamp of the most recent lifecycle_state write.",
            ),
            "registration_overdue_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "W4A (#8 §43.1): when the registration watchdog observed this "
                    "row STILL in 'spawning' past its registration bound. A FIELD, "
                    "not a lifecycle state, deliberately (see "
                    "session_sweep.sweep_unregistered_spawning_sessions): the row is "
                    "still genuinely spawning AND now registration-overdue, which are "
                    "two facts a single state column would collapse. Null means never "
                    "observed overdue; a late registration clears it."
                ),
            ),
            "registration_overdue_reason": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "W4A: the attribution that goes with registration_overdue_at — "
                    "what the platform could actually observe at the seam, never an "
                    "inference from any policy blob's shape."
                ),
            ),
            "degraded_hooks_acknowledged": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                default=0,
                description=(
                    "W4A item 3: this spawn EXPLICITLY opted to proceed on a host "
                    "whose preflight found worker hooks unable to run. Default 0 — "
                    "running degraded is a stated operator choice recorded at spawn, "
                    "never something discovered later from the silence."
                ),
            ),
            "directed_by": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Server-built principal (CallContext, never caller-supplied) "
                    "that made the most recent mutating transition; full audit "
                    "trail is session_transition, this is the fast-read mirror."
                ),
            ),
        },
        indexes=[
            IndexDefinition(name="idx_managed_session_lane", columns=["lane_id"]),
            IndexDefinition(name="idx_managed_session_state", columns=["lifecycle_state"]),
            # W6: the incumbent lookup behind the second-spawn refusal is
            # "non-terminal row with this local_name", run on the spawn path
            # before dispatch — indexed so the guard costs one index probe
            # rather than a fleet scan on every spawn.
            IndexDefinition(name="idx_managed_session_local_name", columns=["local_name"]),
        ],
    )


def get_session_transition_schema() -> TableSchema:
    """AMEND-2a append-only audit trail — replaces the draft's lossy/unqueryable
    JSON ``transitions`` log column. Insert-only via ``write_state``; every
    column is a filterable scalar so "what did principal P direct" / "transitions
    in window T" are one-filter reads under the sanctioned grammar."""
    return TableSchema(
        table_name=TABLE_SESSION_TRANSITION,
        description=(
            "Append-only session lifecycle audit trail (AMEND 2a). Insert-only "
            "— concurrent writers (sweep, steward, report_alive, terminate) "
            "never contend, so no audit record can be lost to a row race."
        ),
        id_prefix=SESSION_TRANSITION_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The managed_session this transition belongs to.",
            ),
            "from_state": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "to_state": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "directed_by": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Server-built principal (CallContext) that directed this transition.",
            ),
            "reason": ColumnDefinition(type=ColumnType.TEXT, description="Free-text rationale."),
            "occurred_at": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
        },
        indexes=[
            IndexDefinition(
                name="idx_session_transition_instance",
                columns=[COL_AGENT_INSTANCE_ID],
            ),
        ],
    )


def get_session_dependency_schema() -> TableSchema:
    """Registry-visible wake edges (§3.4) — "when X lands, wake Y", replacing
    lifecycle state that today dies with a coordinator /clear. A platform sweep
    (same residency as the existing serve-timeout sweep) evaluates armed edges
    (``fired_at IS NULL``) and delivers the wake through normal messaging — the
    edge is state, the wake is a message; no new transport."""
    return TableSchema(
        table_name=TABLE_SESSION_DEPENDENCY,
        description=(
            "Armed wake edges ('when X lands, wake Y'). fired_at NULL = armed; "
            "the platform sweep evaluates and delivers through normal messaging."
        ),
        id_prefix=SESSION_DEPENDENCY_ID_PREFIX,
        columns={
            "waiter_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="The waiting session's agent_instance_id, if session-scoped.",
            ),
            "waiter_lane_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="The waiting lane id, if lane-scoped rather than session-scoped.",
            ),
            "condition_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="lane_closed | session_terminal | deadline.",
            ),
            "condition_ref": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The lane id / agent_instance_id / timestamp the condition watches.",
            ),
            "fired_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="NULL = armed; set once the sweep fires the wake (never re-fired).",
            ),
        },
        indexes=[
            IndexDefinition(name="idx_session_dependency_armed", columns=["fired_at"]),
        ],
    )


def get_session_claude_mapping_schema() -> TableSchema:
    """The agent_instance_id <-> claude_session_id mapping (T1 usage-capture
    lane, ruling 2026-08-05). One row per SessionStart firing (or per
    headless init-event cross-check observation) — NOT one row per worker,
    since a worker's Claude session UUID rotates across /clear|/resume
    without a new managed_session row. The (agent_instance_id,
    claude_session_id, captured_at) triple is this table's natural conflict
    key: it is exactly what the file-per-firing spool's own filename encodes
    (``<captured_at>__<agent_instance_id>__<claude_session_id>.json``), so
    re-ingesting the same not-yet-deleted spool file after a crash upserts
    the SAME row rather than duplicating it."""
    return TableSchema(
        table_name=TABLE_SESSION_CLAUDE_MAPPING,
        description=(
            "agent_instance_id <-> claude_session_id mapping, ONE-TO-MANY "
            "over a worker's lifetime (SessionStart hook capture + headless "
            "init-event cross-check)."
        ),
        id_prefix=SESSION_CLAUDE_MAPPING_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The managed_session this mapping row observes.",
            ),
            "claude_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The Claude Code session_id captured for this firing.",
            ),
            "captured_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="When this firing was captured (hook payload's own timestamp).",
            ),
            "capture_source": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="hook:startup | hook:clear | hook:resume | init_event.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_session_claude_mapping_instance",
                columns=[COL_AGENT_INSTANCE_ID],
            ),
            IndexDefinition(
                name="idx_session_claude_mapping_conflict",
                columns=[COL_AGENT_INSTANCE_ID, "claude_session_id", "captured_at"],
                unique=True,
            ),
        ],
    )


def get_session_context_status_schema() -> TableSchema:
    """The `session_context_status` cache table (maintenance-verbs M1, shape
    (a)). One row per `agent_instance_id`, always overwritten by the latest
    report — the freshness bound is whatever cadence the reporting hook
    runs at, not this table's own concern. `report_context_status` upserts
    on the `agent_instance_id` unique index; `session_context_status` reads
    it back as-is."""
    return TableSchema(
        table_name=TABLE_SESSION_CONTEXT_STATUS,
        description=(
            "Latest known context-window occupancy per agent_instance_id — "
            "the cache session_context_status reads, fed by report_context_status."
        ),
        id_prefix=SESSION_CONTEXT_STATUS_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The reporting session — worker ledger id or the seat's own id.",
            ),
            "claude_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The Claude Code session_id this snapshot was measured against.",
            ),
            "model": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Transcript message.model at measurement time (bare alias).",
            ),
            "current_tokens": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description=(
                    "input_tokens + cache_creation_input_tokens + "
                    "cache_read_input_tokens from the most recent assistant turn."
                ),
            ),
            "ceiling": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="rotation_thresholds.resolve_ceiling(model) at measurement time.",
            ),
            "measured_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="When the reporting hook computed this snapshot (its own clock).",
            ),
            # GAU-14 (D3), 2026-08-19 -- THE SECOND CLOCK. `measured_at` above is
            # the OBSERVER's; this is the READING's. They are not the same
            # quantity and conflating them is a measured defect, not a
            # theoretical one: on 2026-08-19 the seat's two notice paths
            # reported 164,118 "measured 01:19:31Z" and 153,682 "measured
            # ~01:21Z" -- later-but-LOWER -- and both numbers are real lines of
            # the SAME monotone transcript in the CORRECT order (01:18:57.127Z
            # and 01:17:10.380Z respectively). Nothing disagreed about the
            # measurement; the notices reported WHEN THEY LOOKED and called it
            # when it was measured.
            #
            # ★ CONSUMERS OF THIS COLUMN, READ THE HISTORY TABLE'S OWN
            # DECLARATION TOO (`get_session_context_status_history_schema`).
            # The history row is copied from the dict handed to this table's
            # upsert, so the two must state the same semantics or a reader will
            # trust whichever one they happened to open.
            "reading_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=False,
                description=(
                    "When the READING ITSELF was produced — the source "
                    "transcript assistant line's own timestamp, the line whose "
                    "usage block yielded current_tokens. NOT an observer "
                    "clock: measured_at MINUS reading_at IS the observation "
                    "lag. NULL = NOT REPORTED (a reporter predating the "
                    "column), never a synonym for 'same as measured_at' — "
                    "defaulting it would fabricate zero lag exactly where the "
                    "lag is unknown. NON-MONOTONE BY CONSTRUCTION: two rows "
                    "minutes apart legitimately share one reading_at when the "
                    "transcript did not advance, so it plateaus on healthy "
                    "data and must never be used for ordering — that is "
                    "measured_at's job. Mirrors the history table's column of "
                    "the same name, which is copied from this one."
                ),
            ),
            # CACHE STATE (2026-08-16). The economic rotation policy's cold
            # trigger needs to know whether the prompt cache is live, and only
            # the reporting hook can see that -- it reads the transcript, which
            # no verb does. Without these columns the policy's cold branch
            # exists in code and can never fire.
            #
            # All three describe THE MOST RECENT ASSISTANT CALL, the same call
            # current_tokens is summed from. They are nullable because a report
            # from a pre-2026-08-16 hook carries none of them, and a NULL here
            # means "not reported", never "cache is warm" -- the read-back verb
            # surfaces that distinction rather than defaulting it away.
            "cache_read_tokens": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description=(
                    "cache_read_input_tokens on the most recent assistant call. "
                    "0 means that call read NOTHING from cache and paid full "
                    "price; NULL means the reporting hook did not report it."
                ),
            ),
            "cache_cold": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description=(
                    "1 when the reporter classified the prompt cache as expired, "
                    "0 warm, NULL not reported. Classified by "
                    "rotation_thresholds.classify_cache_state, which EXCLUDES "
                    "the first call after a /clear -- that call is cold by "
                    "construction because the clear rewrites the prefix, and "
                    "counting it would make every rotation recommend another."
                ),
            ),
            "cache_overage_signature": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description=(
                    "1 when REPEATED cold calls across sub-TTL gaps indicate the "
                    "cache is not surviving its nominal window -- what usage "
                    "overage looks like from outside, since the account state "
                    "itself is not observable to this platform. 0 no, NULL not "
                    "reported. A single cold call after a long idle gap is "
                    "ordinary expiry and does NOT set this."
                ),
            ),
            # REPORTER ATTRIBUTION (2026-08-16). Several COPIES of the
            # reporting hook can be registered on the same event at once (the
            # repo's own .claude/hooks copy and an INSTALLED plugin-cache copy
            # both bind PostToolUse, and settings sources merge rather than
            # override). They serialize on a shared throttle marker that
            # carries no record of which copy wrote it, so exactly one copy
            # serves each tick and NOTHING in the resulting row said which.
            #
            # That made a missing field ambiguous in a way no reader could
            # resolve: absent cache state could mean the verbs are not
            # deployed, OR that a stale copy served the tick. These two
            # columns make a row attributable, on two INDEPENDENT axes --
            # a current-generation hook running from the wrong surface and a
            # stale-generation hook running from the right one are different
            # failures, and one composite value would blur them.
            #
            # Nullable for the same reason as the cache columns above, and
            # with more force: a reporter predating this widening sends
            # neither, so NULL here positively identifies a pre-attribution
            # reporter. Absence is the signal, not an absence of signal.
            "reporter_surface": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "Which registered COPY of the reporting hook served this "
                    "tick, as a path CLASS rather than a machine-specific "
                    "path: 'checkout' for a hook under the repo's own "
                    ".claude/hooks (including a subdirectory of it), "
                    "'plugin_cache' for an installed plugin-cache copy, "
                    "'vendored' for the in-repo vendored source an install "
                    "copies FROM, 'release' for that same source inside a "
                    "deployed release tree, and 'unknown' when the hook could "
                    "not classify its own location. NULL means the reporter "
                    "predates this column and is therefore a stale copy by "
                    "construction. 'vendored'/'release' were added 2026-08-17 "
                    "after a row was observed ALTERNATING between 'checkout' "
                    "and 'unknown' on one session -- the shared-throttle race "
                    "between two copies that both carry this field, which the "
                    "original collapsed bucket could not name."
                ),
            ),
            # THE ROUTING JOIN (2026-08-18). This table is keyed on the
            # reporting session's LEDGER id, while a watcher-held worker's live
            # `peer_binding` row is keyed on its WATCH id (`agi-watch-<hash>`).
            # Those are different strings for the same session and no stored
            # join related them, so a consumer holding a gauge row could not
            # find the session's bridge -- measured live 2026-08-17: 3 of 4
            # lanes unroutable for exactly this reason, while the one
            # bridge-held session resolved fine.
            #
            # This column stores the session's STABLE `agent_session_id`, which
            # `peer_registry.resolve_by_agent_session_id` reverse-looks-up
            # against the live binding regardless of which id that binding is
            # keyed on. That is the whole point: the join is resolved through
            # the registry, never derived from the shape of the id.
            #
            # ★ DO NOT reconstruct this value from `agent_instance_id`. It
            # currently happens to look like "ases-" + the ledger id, and that
            # is a CONVENTION of one launcher, not a join. Code that slices the
            # prefix would appear to work, pass tests, and then route to the
            # wrong session (or nowhere) the moment a session id is minted any
            # other way -- and its test would have verified the convention
            # while never once exercising the routing.
            #
            # Nullable, and NULL means NOT REPORTED -- never "this session has
            # no bridge". A reporter predating this widening sends nothing, and
            # a consumer must be able to tell that apart from a session it
            # genuinely could not route, exactly as the cache columns above
            # keep "not measured" distinct from "measured false". Nullable is
            # also what makes the migration safe on a populated table: the
            # state layer reconciles this as ALTER TABLE ADD COLUMN, which is
            # instant for a nullable column and fails for a NOT NULL one with
            # no default.
            "agent_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "The reporting session's STABLE agent_session_id "
                    "($AGENT_SESSION_ID), used to reverse-resolve its live "
                    "bridge binding through "
                    "peer_registry.resolve_by_agent_session_id when the "
                    "binding is keyed on a watch id rather than this row's "
                    "ledger id. NULL means the reporter predates this column "
                    "-- NOT that the session has no bridge. Never derive this "
                    "from agent_instance_id: the 'ases-' + ledger-id shape is "
                    "one launcher's convention, not a join."
                ),
            ),
            "reporter_generation": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description=(
                    "The reporting hook's own content-generation constant, "
                    "bumped in lockstep whenever its reporting content "
                    "changes. Deliberately NOT a git sha -- a hook cannot "
                    "know the commit it was copied from, and inferring one "
                    "would promise precision the reporter does not have. "
                    "Lets a reader tell a current copy from an older one "
                    "that is still being served. NULL means the reporter "
                    "predates this column."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_session_context_status_instance",
                columns=[COL_AGENT_INSTANCE_ID],
                unique=True,
            ),
        ],
    )


def get_session_context_status_history_schema() -> TableSchema:
    """The `session_context_status_history` APPEND-ONLY series behind the cache
    (GAU-15, ruled 2026-08-19). One row per accepted gauge write, bounded by the
    writer itself.

    ★ WHY A SECOND TABLE RATHER THAN A WIDER CACHE. `session_context_status`
    is a CACHE — one row per `agent_instance_id`, conflict on that column
    alone, last write wins — and that is correct for the rotation decision it
    serves. It is also why the GAU-01 freeze can never be analysed after the
    fact: the frozen 487,777 reading of 2026-08-18T00:50:56Z was overwritten by
    the successor context's own write, and a `/clear` keeps the instance id, so
    ONE row silently spanned both sides of that rotation. A defect whose whole
    signature is "a timestamp stopped advancing" was being detected against a
    store that kept no record of it advancing.

    ★ THREE CLOCKS, ON PURPOSE, and they answer three different questions.
    `reading_at` is when the READING was produced (the transcript line whose
    usage block yielded the number); `measured_at` is when the reporting hook
    OBSERVED it; `recorded_at` is when the state layer STORED it. Their
    differences are the observation lag and the delivery lag, and keeping them
    apart makes both VISIBLE instead of assumed — the assumed version has
    already been measured false once: a first draft of GAU-01(b)'s threshold
    argued the healthy lag was bounded by the reporter's 120s throttle, and a
    live control measured +178.8s. ORDERING IS `recorded_at`'s JOB ALONE:
    `measured_at` is the reporter's clock and `reading_at` legitimately
    plateaus, so neither is a sort key.

    ★ WHAT THE SERIES MAKES ANSWERABLE, which one row cannot:
    * STOPPED — the series stalls while `report_by - report_by_seconds` (the
      last `report_alive`) keeps advancing: the session works and its gauge is
      dark. That is the GAU-01 class, and the series says WHEN it began and
      WHAT the last good reading was.
    * IDLE — the series and that identity stall together: nobody is driving
      the session. A normal, expected fleet state, and NOT an incident.
    * NEVER-STARTED — no history rows and no cache row.
    * ROTATED — `claude_session_id` changes while `agent_instance_id` stays:
      a `/clear` boundary, so readings either side are two series, not one.
      Measured 2026-08-19: all three of the first states presented as the same
      single alarm to the steward, which is what this table exists to end.

    Retention is the WRITER's job (`GAUGE_HISTORY_RETENTION` rows per instance
    id, hard-deleted at write time) — no reaper to arm and forget, and no
    soft-delete flag that nothing ever sets.
    """
    return TableSchema(
        table_name=TABLE_SESSION_CONTEXT_STATUS_HISTORY,
        description=(
            "Bounded append-only series of accepted session_context_status "
            "writes — the history the upsert-only cache cannot keep."
        ),
        id_prefix=SESSION_CONTEXT_STATUS_HISTORY_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The reporting session, as on the cache row.",
            ),
            "claude_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "The Claude Code session_id this reading was measured "
                    "against. THE ROTATION MARKER: a change here under a "
                    "constant agent_instance_id is a /clear boundary, which is "
                    "exactly what made the original freeze unanalysable."
                ),
            ),
            "agent_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "The session's stable agent_session_id as reported. NULL "
                    "means NOT REPORTED (a reporter predating that column), "
                    "never 'this session has no bridge'."
                ),
            ),
            "model": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Transcript message.model at measurement time.",
            ),
            "current_tokens": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="The reading itself — the occupancy this tick saw.",
            ),
            "ceiling": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description="resolve_ceiling(model) at measurement time.",
            ),
            "measured_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description=(
                    "The REPORTER's clock: when the hook computed this "
                    "snapshot. The timestamp whose arrest is the whole "
                    "signature of GAU-01."
                ),
            ),
            "recorded_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description=(
                    "The STATE LAYER's clock: when this row was appended. Kept "
                    "beside measured_at so the difference between them is "
                    "readable evidence rather than an assumption."
                ),
            ),
            "reading_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=False,
                description=(
                    "When the READING ITSELF was produced — the source "
                    "transcript assistant line's own timestamp, the line whose "
                    "usage block yielded current_tokens. NOT an observer "
                    "clock: measured_at MINUS reading_at IS the observation "
                    "lag. NULL = NOT REPORTED (a reporter predating the "
                    "column), never a synonym for 'same as measured_at' — "
                    "defaulting it would fabricate zero lag exactly where the "
                    "lag is unknown. NON-MONOTONE BY CONSTRUCTION: two rows "
                    "minutes apart legitimately share one reading_at when the "
                    "transcript did not advance, so it plateaus on healthy "
                    "data and must never be used for ordering. Carried from "
                    "birth on this table per the 2026-08-19 cross-lane column "
                    "exchange with lane-gau-notice (D3)."
                ),
            ),
            "cache_read_tokens": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description="As reported; NULL = not reported by this reporter.",
            ),
            "cache_cold": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description=(
                    "1/0 as reported; NULL = NOT REPORTED, which is a third "
                    "state and never a synonym for 'cache is warm'."
                ),
            ),
            "cache_overage_signature": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description="1/0 as reported; NULL = not reported.",
            ),
            "reporter_surface": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "Which registered COPY of the reporting hook served this "
                    "tick (checkout | plugin_cache | vendored | release | "
                    "unknown). NULL = a reporter predating the column. In a "
                    "SERIES this is worth more than on the cache row: a value "
                    "that ALTERNATES across consecutive rows is the "
                    "two-copies-racing signature, which a single row can only "
                    "hint at."
                ),
            ),
            "reporter_generation": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=False,
                description=(
                    "The reporting hook copy's generation. NULL = "
                    "pre-attribution reporter."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_scx_history_instance_recorded",
                columns=[COL_AGENT_INSTANCE_ID, "recorded_at"],
            ),
        ],
    )


def get_gauge_notice_record_schema() -> TableSchema:
    """The `gauge_notice_record` DURABLE record of a fired gauge notice
    (GAU-21, 2026-08-19). One row per notice the sweep DECIDED to emit —
    written at the emit site, whether or not delivery then happened.

    ★ WHY THIS TABLE EXISTS, measured against the source rather than argued.
    The sweep's gauge notices are appended only as in-memory bridge events, and
    four independent properties of that queue each defeat an audit:

    * NOT DURABLE — `BridgeSessionState` is documented as "In-memory state for
      one active bridge"; `pending_events` is a plain list behind a lock and
      nothing persists it, so a restart loses every un-drained notice. The
      DEPLOY a canary must traverse IS a restart.
    * DRAIN-ONCE, AND READING STEALS — `events_after` returns the acked events
      and rebinds the queue to only those after the cursor. A verifier polling
      that queue races the steward and can consume the very notice the steward
      needed: not merely unreadable, but an active harm to the operational
      path.
    * NO BY-TYPE READ — no surface is keyed on event type; the only reader is a
      cursor-ordered drain of one bridge's queue.
    * CONDITIONALLY NEVER EMITTED — the notify path resolves the steward
      binding FIRST and returns early when it is None, so on an unbound steward
      the alarm reaches nothing and leaves nothing behind.

    ★ THE CONSEQUENCE THIS ENDS. A missed alarm and an alarm that never fired
    are today the same silence. That is the GAU-01 shape one level up: the
    instrument's own output is unobservable after the moment, which is exactly
    what GAU-15 fixed for the READING and this fixes for the ALARM. It is also
    why the manual freeze-watch cannot retire when the detector merely deploys
    — a detector whose firings leave no trace cannot be audited after the fact.

    ★ `release_id` IS LOAD-BEARING, NOT PROVENANCE DECORATION. The deployed
    detector's thresholds are not master's: measured 2026-08-19, the running
    release ran gauge coverage at 300s against master's 600s, and carried no
    staleness detector at all while master had one. A recorded alarm that does
    not say which release's numbers produced it cannot be re-read later, and
    a reader reasoning from master would date the evidence wrongly in both
    directions.

    ★ EVIDENCE AS EXPLICIT COLUMNS, not one prose field. A reader who must
    parse prose to recover a threshold will not, and the prose is already
    written for a steward rather than for a query. The two gauge families
    populate different subsets and the NULLs are meaningful: the coverage
    notice has no gauge row by definition, so its gauge timestamps are absent
    rather than zero.
    """
    return TableSchema(
        table_name=TABLE_GAUGE_NOTICE_RECORD,
        description=(
            "Durable, attributable record that a gauge notice fired — the "
            "trace the in-memory, drain-once bridge event queue cannot keep."
        ),
        id_prefix=GAUGE_NOTICE_RECORD_ID_PREFIX,
        columns={
            "notice_type": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Which detector fired: gauge_stale_notice or "
                    "gauge_coverage_notice. The by-type read the bridge event "
                    "queue has no surface for."
                ),
            ),
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "The SUBJECT session the notice is about — never the "
                    "steward it was addressed to."
                ),
            ),
            "emitted_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "When the sweep DECIDED to fire, on the sweep's own clock. "
                    "Not when a steward read it: the read may never happen, "
                    "which is the point of the table."
                ),
            ),
            "steward_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "Who the notice was addressed to. NULL means NO STEWARD "
                    "WAS RESOLVED — a real and previously invisible outcome, "
                    "never 'not recorded'."
                ),
            ),
            "delivery_outcome": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "appended / no_steward_binding / append_failed. "
                    "Deliberately not a boolean: an unbound steward and a "
                    "failed append are different faults with different owners, "
                    "and both look like silence from outside."
                ),
            ),
            "release_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "The release whose detector fired this, self-identified "
                    "from the running module's own tree. NULL means the "
                    "release could not be determined (a checkout run, say) — "
                    "absent, never guessed, because a wrong release id "
                    "silently re-dates the thresholds a reader applies."
                ),
            ),
            "threshold_s": ColumnDefinition(
                type=ColumnType.REAL,
                not_null=False,
                description=(
                    "The threshold IN FORCE at emit time, recorded rather than "
                    "re-read at read time — the running release's value has "
                    "already been measured to differ from master's."
                ),
            ),
            "observed_s": ColumnDefinition(
                type=ColumnType.REAL,
                not_null=False,
                description=(
                    "What was actually measured against that threshold: the "
                    "gauge lag for a staleness notice, the live-row age for a "
                    "coverage notice."
                ),
            ),
            "last_report_alive_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "The lifecycle clock the staleness leg diverged FROM. NULL "
                    "for a coverage notice, which does not read it."
                ),
            ),
            "gauge_measured_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "The arrested gauge reading's own timestamp — the value "
                    "the upsert-only cache would otherwise overwrite. NULL for "
                    "a coverage notice, which fires precisely because there is "
                    "no gauge row to read."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_gnr_instance_type_emitted",
                columns=[COL_AGENT_INSTANCE_ID, "notice_type", "emitted_at"],
            ),
        ],
    )


def get_gauge_canary_registry_schema() -> TableSchema:
    """The `gauge_canary_registry` — which identities are CANARIES (GAU-15
    item 4, constraint (c)).

    ★ ITS OWN TABLE, AND THAT IS CONSTRAINT (b) RATHER THAN TIDINESS. The
    obvious design puts an `is_canary` column on the gauge row. That column
    would sit on the very row the staleness detector reads, which makes it a
    flag the detection path could come to read — and a detector that can tell
    it is under test has stopped being the thing under test. Constraint (b)
    binds harder than (c): the mark must exist for operational consumers and
    must NOT be reachable from the detector's own read.

    ★ WHO JOINS THIS, AND WHO MUST NEVER. Rotation notices, steward interrupts
    and economics join it deliberately, so a synthetic session never pollutes a
    fleet decision — a canary that shows up in the rotation economics is a
    canary that costs real money and real attention. The sweep's gauge legs
    never join it: to them a canary row is an ordinary session, which is the
    entire point.

    `retired_at` rather than a delete: a canary that has been stood down still
    explains the alarms it produced while it ran, and a registry that forgets
    makes its own history unattributable.
    """
    return TableSchema(
        table_name=TABLE_GAUGE_CANARY_REGISTRY,
        description=(
            "Identities that are synthetic gauge canaries — the store-plane "
            "provenance mark, deliberately not a column on the gauge row."
        ),
        id_prefix=GAUGE_CANARY_REGISTRY_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                unique=True,
                description=(
                    "The canary's own instance id, as the real reporting path "
                    "writes it. Unique: one registration per identity."
                ),
            ),
            "purpose": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Why this canary exists, in words, for whoever finds a "
                    "synthetic session in a listing and needs to know it is "
                    "deliberate."
                ),
            ),
            "registered_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="When it was registered.",
            ),
            "registered_by": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Who registered it, in the same directed_by form the "
                    "tamper log uses — a synthetic identity in the fleet is an "
                    "act someone took, never an ambient fact."
                ),
            ),
            "retired_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=False,
                description=(
                    "When it was stood down; NULL means ACTIVE. Retired rather "
                    "than deleted, because a stood-down canary still has to "
                    "explain the alarms it produced while it ran."
                ),
            ),
        },
    )


def get_gauge_canary_tamper_schema() -> TableSchema:
    """The `gauge_canary_tamper` audit log — every arrest, who ordered it, and
    what alarm it expected (GAU-15 item 4, constraint (d)).

    ★ WHAT THIS BUYS: MECHANICAL ATTRIBUTION. With this log, an alarm inside a
    recorded window is SCHEDULED and an alarm outside every window is REAL, and
    a reader derives that rather than remembering it. Without it, the canary
    emits alarms indistinguishable from genuine ones — which does not merely
    fail to help, it actively trains stewards to discount the instrument. That
    is the naive build the constraint was written to forbid.

    ★ NO AMBIENT TEST MODE, which is the other half of (d). The arrest is a
    row written by a registered verb carrying `directed_by`, not an environment
    variable and not a test flag. An env var leaves no audit trail, cannot be
    scoped to a window, and silently persists into runs nobody intended.

    ★ `expected_notice_type` IS RECORDED AT ARREST TIME, not asserted at read
    time. The verifier must be checkable against what the tamper ASKED FOR,
    otherwise it grades its own expectations after seeing the answer.
    """
    return TableSchema(
        table_name=TABLE_GAUGE_CANARY_TAMPER,
        description=(
            "Audited record of every canary arrest: who directed it, the "
            "window, and the alarm it expected — what makes an alarm "
            "attributable to a scheduled tamper rather than a real fault."
        ),
        id_prefix=GAUGE_CANARY_TAMPER_ID_PREFIX,
        columns={
            COL_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The canary this arrest applies to.",
            ),
            "directed_by": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Who ordered the tamper, in the platform's directed_by "
                    "form. An unattributable tamper is the failure mode this "
                    "column exists to make impossible."
                ),
            ),
            "arrest_from": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Start of the requested arrest window, inclusive.",
            ),
            "arrest_until": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "End of the requested arrest window, exclusive. ALWAYS "
                    "bounded: an open-ended arrest is a canary that stays "
                    "broken, and its alarms would then be attributable "
                    "forever, which is the same as not being attributable."
                ),
            ),
            "expected_notice_type": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Which alarm this arrest is expected to provoke, recorded "
                    "BEFORE the outcome is known so the verifier cannot grade "
                    "its own expectations after the fact."
                ),
            ),
            "reason": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Why this arrest was ordered, in words.",
            ),
            "recorded_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="When the arrest was ordered (the state layer's clock).",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_gct_instance_from",
                columns=[COL_AGENT_INSTANCE_ID, "arrest_from"],
            ),
        ],
    )


def get_lane_charter_schema() -> TableSchema:
    """Append-only lane-charter capture (fleet-watch-transport-migration
    phase 2 slice 6, design check-in ruling item 3). Insert-only via
    ``write_state`` — the SAME shape ``session_transition`` uses for its own
    audit trail: capture always writes a NEW row; a later charter for the
    same ``lane_id`` SUPERSEDES by recency (``resolve_lane_charter`` reads
    the latest row by ``captured_at`` desc), never edits a prior row in
    place. Authenticity of the operator's stored words depends on
    ``charter_text`` staying write-once — there is deliberately no update
    path for it anywhere in this codebase."""
    return TableSchema(
        table_name=TABLE_LANE_CHARTER,
        description=(
            "Append-only operator-charter capture per lane_id. The latest "
            "row's charter_text is driven byte-exact as a spawned worker's "
            "literal first turn (spawn_session); a lane with no captured "
            "charter resolves to a deterministic fallback turn instead."
        ),
        id_prefix=LANE_CHARTER_ID_PREFIX,
        columns={
            "lane_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The lane this charter founds; spawn_session resolves the "
                "LATEST row for its lane_id.",
            ),
            "charter_text": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The operator's verbatim words, captured byte-exact — driven "
                "unmodified as a spawned worker's first turn.",
            ),
            "brief_ref": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Workbench path or dispatch id the charter accompanies "
                "(provenance).",
            ),
            "captured_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="When the operator spoke these words in the seat "
                "conversation — NOT the row-write time.",
            ),
            "directed_by": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Server-built principal (CallContext) that captured this "
                "charter.",
            ),
        },
        indexes=[
            IndexDefinition(name="idx_lane_charter_lane", columns=["lane_id"]),
        ],
    )


def get_held_authorization_schema() -> TableSchema:
    """R1 held-authorization queue (2026-08-17). Mechanically enumerable
    answer to "what is blocked on <role>?" for a freshly-booted session with
    no memory of the previous seat — the platform-side counterpart to the
    OBLIGATIONS running-log slot (``ananta/knowledge_base/
    seat_running_log_convention.md``), which fixes the same failure through
    seat discipline rather than a mechanism. GC writes the row at refusal
    time; GC retires it on receiving the matching first-party authorization,
    or ``owed_by_role``'s own holder retires it directly (superseded /
    withdrawn). ``retired_at`` is set exactly once — never re-fired, mirrors
    ``session_dependency.fired_at``'s NULL-armed convention."""
    return TableSchema(
        table_name=TABLE_HELD_AUTHORIZATION,
        description=(
            "Held authorization-citation refusals Git-Controller cannot act "
            "on without a first-party authorization. retired_at NULL = still "
            "owed; list_held_authorizations is the 'what is blocked on <role>?' "
            "answer for a session with no memory of a prior seat."
        ),
        id_prefix=HELD_AUTHORIZATION_ID_PREFIX,
        columns={
            "requesting_peer": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The role or instance whose commit request GC refused.",
            ),
            "owed_by_role": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "The seat/coordinator role expected to send the first-party "
                    "authorization GC is waiting on. Not hardcoded to any one role."
                ),
            ),
            "branch_or_request_ref": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The proposed branch name or other stable request identifier.",
            ),
            "reason": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Why GC refused — e.g. the citation it could not verify first-party.",
            ),
            "retired_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="NULL = still owed. Set once, never re-fired, by GC or owed_by_role.",
            ),
            "retired_reason": ColumnDefinition(
                type=ColumnType.TEXT,
                description="e.g. 'authorized', 'superseded', 'withdrawn'. Set with retired_at.",
            ),
            "retired_by": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Role or instance that called retire_held_authorization.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_held_authorization_owed_by_role", columns=["owed_by_role"],
            ),
            IndexDefinition(
                name="idx_held_authorization_requesting_peer", columns=["requesting_peer"],
            ),
            IndexDefinition(
                name="idx_held_authorization_open", columns=["retired_at"],
            ),
        ],
    )


def get_session_lifecycle_schema_definition() -> SchemaDefinition:
    """Wrap the D1 L0 schema deltas (§3.2-3.4 + the AMEND-4b cardinality row)
    for ``get_schema_definitions``. Same namespace as the rest of this plugin's
    role/session tables; installed via the standard ``install_plugin_schema``
    lifecycle. Fresh CREATEs, zero existing consumers (Architect Focus 5:
    D1 is land-able alone)."""
    return SchemaDefinition(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        version="1.5.0",
        description=(
            "Fleet session-management Phase B, D1 — L0 schema deltas. "
            "+1.1.0: session_context_status (maintenance-verbs M1). "
            "+1.2.0: held_authorization (R1 held-authorization queue). "
            "+1.3.0: session_context_status_history (GAU-15 gauge series). "
            "+1.4.0: gauge_notice_record (GAU-21 durable notice record). "
            "+1.5.0: gauge_canary_registry + gauge_canary_tamper (GAU-15 "
            "item 4 tamper canary)."
        ),
        tables={
            TABLE_SESSION_ROLE_CLAIM: get_session_role_claim_schema(),
            TABLE_MANAGED_SESSION: get_managed_session_schema(),
            TABLE_SESSION_TRANSITION: get_session_transition_schema(),
            TABLE_SESSION_DEPENDENCY: get_session_dependency_schema(),
            TABLE_SESSION_CLAUDE_MAPPING: get_session_claude_mapping_schema(),
            TABLE_LANE_CHARTER: get_lane_charter_schema(),
            TABLE_SESSION_CONTEXT_STATUS: get_session_context_status_schema(),
            TABLE_SESSION_CONTEXT_STATUS_HISTORY: (
                get_session_context_status_history_schema()
            ),
            TABLE_HELD_AUTHORIZATION: get_held_authorization_schema(),
            TABLE_GAUGE_NOTICE_RECORD: get_gauge_notice_record_schema(),
            TABLE_GAUGE_CANARY_REGISTRY: get_gauge_canary_registry_schema(),
            TABLE_GAUGE_CANARY_TAMPER: get_gauge_canary_tamper_schema(),
        },
    )


__all__ = [
    "AGENT_ROLE_BINDING_ID_PREFIX",
    "CAPTURE_SOURCE_HOOK_CLEAR",
    "CAPTURE_SOURCE_HOOK_RESUME",
    "CAPTURE_SOURCE_HOOK_STARTUP",
    "CAPTURE_SOURCE_INIT_EVENT",
    "CONDITION_DEADLINE",
    "CONDITION_LANE_CLOSED",
    "CONDITION_SESSION_TERMINAL",
    "GAUGE_CANARY_REGISTRY_ID_PREFIX",
    "GAUGE_CANARY_TAMPER_ID_PREFIX",
    "GAUGE_NOTICE_RECORD_ID_PREFIX",
    "HELD_AUTHORIZATION_ID_PREFIX",
    "LANE_CHARTER_ID_PREFIX",
    "LIFECYCLE_IDLE",
    "LIFECYCLE_LIVE",
    "LIFECYCLE_OVERDUE",
    "LIFECYCLE_PARKED",
    "LIFECYCLE_RETIRED",
    "LIFECYCLE_SPAWNING",
    "LIFECYCLE_TERMINATED",
    "LIFECYCLE_TRANSITIONS",
    "MANAGED_SESSION_ID_PREFIX",
    "NOTICE_DELIVERY_APPENDED",
    "NOTICE_DELIVERY_APPEND_FAILED",
    "NOTICE_DELIVERY_NO_STEWARD_BINDING",
    "NOTICE_DELIVERY_OUTCOMES",
    "PEER_BINDING_ID_PREFIX",
    "PEER_BINDING_NAMESPACE",
    "PEER_BINDING_TABLE",
    "ROLE_BINDING_ID_PREFIX",
    "ROLE_ID_PREFIX",
    "SESSION_CLAUDE_MAPPING_ID_PREFIX",
    "SESSION_CONTEXT_STATUS_HISTORY_ID_PREFIX",
    "SESSION_CONTEXT_STATUS_ID_PREFIX",
    "SESSION_DEPENDENCY_ID_PREFIX",
    "SESSION_HOST_HEADLESS",
    "SESSION_HOST_OPERATOR",
    "SESSION_HOST_TMUX",
    "SESSION_ROLE_CLAIM_ID_PREFIX",
    "SESSION_TRANSITION_ID_PREFIX",
    "SESSION_VISIBILITY_HEADLESS",
    "SESSION_VISIBILITY_VISIBLE",
    "TABLE_GAUGE_CANARY_REGISTRY",
    "TABLE_GAUGE_CANARY_TAMPER",
    "TABLE_GAUGE_NOTICE_RECORD",
    "TABLE_HELD_AUTHORIZATION",
    "TABLE_LANE_CHARTER",
    "TABLE_MANAGED_SESSION",
    "TABLE_SESSION_CLAUDE_MAPPING",
    "TABLE_SESSION_CONTEXT_STATUS",
    "TABLE_SESSION_CONTEXT_STATUS_HISTORY",
    "TABLE_SESSION_DEPENDENCY",
    "TABLE_SESSION_ROLE_CLAIM",
    "TABLE_SESSION_TRANSITION",
    "WORK_CLASS_ANALYSIS_DELIVERABLE",
    "WORK_CLASS_PRODUCTION_MUTATION",
    "WORK_CLASS_READ_ONLY",
    "get_agent_role_binding_schema",
    "get_agent_role_binding_schema_definition",
    "get_gauge_canary_registry_schema",
    "get_gauge_canary_tamper_schema",
    "get_gauge_notice_record_schema",
    "get_held_authorization_schema",
    "get_lane_charter_schema",
    "get_managed_session_schema",
    "get_peer_binding_schema",
    "get_peer_binding_schema_definition",
    "get_role_binding_schema",
    "get_role_model_schema_definition",
    "get_role_schema",
    "get_session_claude_mapping_schema",
    "get_session_context_status_history_schema",
    "get_session_context_status_schema",
    "get_session_dependency_schema",
    "get_session_lifecycle_schema_definition",
    "get_session_role_claim_schema",
    "get_session_transition_schema",
    "session_role_claim_external_id",
]
