"""Schema declarations co-located with ``agent_messaging_plugin``.

This module defines the routing schema for peer bindings.  The durable
agent-messaging tables (agent_thread, agent_message, etc.) live under
``ananta.llm.agent_messaging.schema`` and are returned from the
plugin's ``get_schema_definitions()`` — they go through the standard
``install_plugin_schema`` lifecycle.

The peer-binding schema rides the platform ``Store`` abstraction.
``PeerRegistry`` opens it with ``backend="postgres"`` (see
``workbench/2026-06-01_local_reconnect_ux_design.md`` §4) so the
``session_label`` set via ``/rename`` survives homunculus restarts —
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

# fleet-watch-transport-migration phase 2 slice 6 (2026-08-06, design
# check-in ruling item 3) — the operator's verbatim founding words for a
# lane, driven byte-exact as a spawned worker's literal first turn.
TABLE_LANE_CHARTER = "lane_charter"
LANE_CHARTER_ID_PREFIX = "lch"

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
                    "recipient with no such surface (stock Codex's bridge), "
                    "which routes dispatch to tee its deliveries into the "
                    "wake_waiter spool instead. Fixed 2026-08-08 "
                    "(fleet-wake-integrity Task 1): this column was missing "
                    "from the persisted schema, so every registration's "
                    "declared value was silently dropped and every resolved "
                    "binding read back the dataclass default (True)."
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
                    "validated against reserved names (<homunculus>-Main, sys:*) "
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
            "lane_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="The lane this session was spawned for (provenance, anti-laundering).",
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
        },
        indexes=[
            IndexDefinition(
                name="idx_session_context_status_instance",
                columns=[COL_AGENT_INSTANCE_ID],
                unique=True,
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


def get_session_lifecycle_schema_definition() -> SchemaDefinition:
    """Wrap the D1 L0 schema deltas (§3.2-3.4 + the AMEND-4b cardinality row)
    for ``get_schema_definitions``. Same namespace as the rest of this plugin's
    role/session tables; installed via the standard ``install_plugin_schema``
    lifecycle. Fresh CREATEs, zero existing consumers (Architect Focus 5:
    D1 is land-able alone)."""
    return SchemaDefinition(
        namespace=AGENT_ROLE_BINDING_NAMESPACE,
        version="1.1.0",
        description=(
            "Fleet session-management Phase B, D1 — L0 schema deltas. "
            "+1.1.0: session_context_status (maintenance-verbs M1)."
        ),
        tables={
            TABLE_SESSION_ROLE_CLAIM: get_session_role_claim_schema(),
            TABLE_MANAGED_SESSION: get_managed_session_schema(),
            TABLE_SESSION_TRANSITION: get_session_transition_schema(),
            TABLE_SESSION_DEPENDENCY: get_session_dependency_schema(),
            TABLE_SESSION_CLAUDE_MAPPING: get_session_claude_mapping_schema(),
            TABLE_LANE_CHARTER: get_lane_charter_schema(),
            TABLE_SESSION_CONTEXT_STATUS: get_session_context_status_schema(),
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
    "PEER_BINDING_ID_PREFIX",
    "PEER_BINDING_NAMESPACE",
    "PEER_BINDING_TABLE",
    "ROLE_BINDING_ID_PREFIX",
    "ROLE_ID_PREFIX",
    "SESSION_CLAUDE_MAPPING_ID_PREFIX",
    "SESSION_CONTEXT_STATUS_ID_PREFIX",
    "SESSION_DEPENDENCY_ID_PREFIX",
    "SESSION_HOST_HEADLESS",
    "SESSION_HOST_OPERATOR",
    "SESSION_HOST_TMUX",
    "SESSION_ROLE_CLAIM_ID_PREFIX",
    "SESSION_TRANSITION_ID_PREFIX",
    "SESSION_VISIBILITY_HEADLESS",
    "SESSION_VISIBILITY_VISIBLE",
    "TABLE_LANE_CHARTER",
    "TABLE_MANAGED_SESSION",
    "TABLE_SESSION_CLAUDE_MAPPING",
    "TABLE_SESSION_CONTEXT_STATUS",
    "TABLE_SESSION_DEPENDENCY",
    "TABLE_SESSION_ROLE_CLAIM",
    "TABLE_SESSION_TRANSITION",
    "WORK_CLASS_ANALYSIS_DELIVERABLE",
    "WORK_CLASS_PRODUCTION_MUTATION",
    "WORK_CLASS_READ_ONLY",
    "get_agent_role_binding_schema",
    "get_agent_role_binding_schema_definition",
    "get_lane_charter_schema",
    "get_managed_session_schema",
    "get_peer_binding_schema",
    "get_peer_binding_schema_definition",
    "get_role_binding_schema",
    "get_role_model_schema_definition",
    "get_role_schema",
    "get_session_claude_mapping_schema",
    "get_session_context_status_schema",
    "get_session_dependency_schema",
    "get_session_lifecycle_schema_definition",
    "get_session_role_claim_schema",
    "get_session_transition_schema",
    "session_role_claim_external_id",
]
