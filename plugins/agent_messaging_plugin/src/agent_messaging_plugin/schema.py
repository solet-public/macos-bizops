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
    COL_SESSION_LABEL,
    HOLDER_KIND_SESSION,
    INDEX_AGENT_ROLE_BINDING_INSTANCE,
    INDEX_ROLE_BINDING_INSTANCE,
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


__all__ = [
    "AGENT_ROLE_BINDING_ID_PREFIX",
    "PEER_BINDING_ID_PREFIX",
    "PEER_BINDING_NAMESPACE",
    "PEER_BINDING_TABLE",
    "ROLE_BINDING_ID_PREFIX",
    "ROLE_ID_PREFIX",
    "get_agent_role_binding_schema",
    "get_agent_role_binding_schema_definition",
    "get_peer_binding_schema",
    "get_peer_binding_schema_definition",
    "get_role_binding_schema",
    "get_role_model_schema_definition",
    "get_role_schema",
]
