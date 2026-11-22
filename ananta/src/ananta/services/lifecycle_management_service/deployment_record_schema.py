"""Durable schema for the solet's own record of its deployments.

Two ``core``-namespace tables owned by the lifecycle-management service:

- ``deployment_record`` — one row per recorded deployment, so a deploy command
  can ask what is deployed and since when without any outside service.
- ``deployment_record_event`` — append-only history of every state change of
  those rows (recorded, withdrawn, reinstated).

A deployment row states a measured fact, so it is never edited. A wrong one is
withdrawn and the right one recorded anew. ``version`` is the compare-and-set
guard for withdraw and reinstate.

Standard fields (``id``, ``created_at``, ``updated_at``, ``is_deleted`` ...) are
auto-injected by the ``SchemaStandardizer`` and are not declared here.
"""

from __future__ import annotations

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

DEPLOYMENT_RECORD_NAMESPACE = "core"
TABLE_DEPLOYMENT_RECORD = "deployment_record"
TABLE_DEPLOYMENT_RECORD_EVENT = "deployment_record_event"
ID_PREFIX_DEPLOYMENT_RECORD = "dpr"
ID_PREFIX_DEPLOYMENT_RECORD_EVENT = "dre"

COL_DEPLOYMENT_ID = "deployment_id"
COL_SOLET_NAME = "solet_name"
COL_RELEASE_ID = "release_id"
COL_GIT_SHA = "git_sha"
COL_DEPLOYED_AT = "deployed_at"
COL_TRIGGER = "trigger"
COL_MANIFEST_SHA256 = "manifest_sha256"
COL_RECORDED_BY_INSTANCE = "recorded_by_instance"
COL_RECORDED_BY_MODEL = "recorded_by_model"
COL_RECORDED_BY_EFFORT = "recorded_by_effort"
COL_STATE = "state"
COL_VERSION = "version"

COL_ORDINAL = "ordinal"
COL_KIND = "kind"
COL_PRIOR_VERSION = "prior_version"
COL_ACTOR_INSTANCE = "actor_instance"
COL_ACTOR_MODEL = "actor_model"
COL_ACTOR_EFFORT = "actor_effort"
COL_REASON = "reason"
COL_RECORDED_AT = "recorded_at"

TRIGGER_LANDING = "landing"
TRIGGER_TICK = "tick"
TRIGGERS = (TRIGGER_LANDING, TRIGGER_TICK)

STATE_RECORDED = "recorded"
STATE_WITHDRAWN = "withdrawn"

KIND_RECORDED = "recorded"
KIND_WITHDRAWN = "withdrawn"
KIND_REINSTATED = "reinstated"

GIT_SHA_LENGTH = 40


def get_deployment_record_schema() -> SchemaDefinition:
    """Declarative schema for ``core__deployment_record`` and its event table."""
    return SchemaDefinition(
        namespace=DEPLOYMENT_RECORD_NAMESPACE,
        tables={
            TABLE_DEPLOYMENT_RECORD: TableSchema(
                table_name=TABLE_DEPLOYMENT_RECORD,
                id_prefix=ID_PREFIX_DEPLOYMENT_RECORD,
                description=("One row per recorded deployment of this solet: which release and commit went live, when, and who recorded it."),
                columns={
                    COL_DEPLOYMENT_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Row key (dpr-...). UNIQUE; the event table keys on it.",
                    ),
                    COL_SOLET_NAME: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="The solet the release was deployed to.",
                    ),
                    COL_RELEASE_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="The release that was deployed.",
                    ),
                    COL_GIT_SHA: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=f"length({COL_GIT_SHA}) = {GIT_SHA_LENGTH}",
                        description=("The deployed commit, 40 lowercase hex characters (the store refuses anything else before the write)."),
                    ),
                    COL_DEPLOYED_AT: ColumnDefinition(
                        type=ColumnType.DATETIME,
                        not_null=True,
                        description="When the deployment happened, UTC.",
                    ),
                    COL_TRIGGER: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=f"{COL_TRIGGER} IN ('{TRIGGER_LANDING}', '{TRIGGER_TICK}')",
                        description="What started the deployment: a landing or a scheduled tick.",
                    ),
                    COL_MANIFEST_SHA256: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Digest of the deployed manifest; empty when none was measured.",
                    ),
                    COL_RECORDED_BY_INSTANCE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Agent instance that recorded the row (recorded, not an authority).",
                    ),
                    COL_RECORDED_BY_MODEL: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Model of the recording actor.",
                    ),
                    COL_RECORDED_BY_EFFORT: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Effort tier of the recording actor.",
                    ),
                    COL_STATE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=f"{COL_STATE} IN ('{STATE_RECORDED}', '{STATE_WITHDRAWN}')",
                        description="recorded or withdrawn; withdraw and reinstate move it by compare-and-set.",
                    ),
                    COL_VERSION: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        check=f"{COL_VERSION} >= 1",
                        description="Starts at 1; every state change adds 1. The compare-and-set guard.",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        name="idx_deployment_record_deployment_id",
                        columns=[COL_DEPLOYMENT_ID],
                        unique=True,
                    ),
                    IndexDefinition(
                        name="idx_deployment_record_identity",
                        columns=[COL_SOLET_NAME, COL_GIT_SHA, COL_RELEASE_ID],
                        unique=True,
                    ),
                    IndexDefinition(
                        name="idx_deployment_record_solet_deployed_at",
                        columns=[COL_SOLET_NAME, COL_DEPLOYED_AT],
                    ),
                ],
            ),
            TABLE_DEPLOYMENT_RECORD_EVENT: TableSchema(
                table_name=TABLE_DEPLOYMENT_RECORD_EVENT,
                id_prefix=ID_PREFIX_DEPLOYMENT_RECORD_EVENT,
                description=("Append-only history of every state change of a deployment record. Rows are written once and never updated."),
                columns={
                    COL_DEPLOYMENT_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="The deployment_record row this event belongs to.",
                    ),
                    COL_ORDINAL: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="1-based position in the row's history; equals the row version after the event.",
                    ),
                    COL_KIND: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=(f"{COL_KIND} IN ('{KIND_RECORDED}', '{KIND_WITHDRAWN}', '{KIND_REINSTATED}')"),
                        description="recorded, withdrawn or reinstated.",
                    ),
                    COL_PRIOR_VERSION: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="The row version this event was applied on top of (0 for the first).",
                    ),
                    COL_ACTOR_INSTANCE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Agent instance that caused the event.",
                    ),
                    COL_ACTOR_MODEL: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Model of the acting session.",
                    ),
                    COL_ACTOR_EFFORT: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Effort tier of the acting session.",
                    ),
                    COL_REASON: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Why the change was made; empty for the first recording.",
                    ),
                    COL_RECORDED_AT: ColumnDefinition(
                        type=ColumnType.DATETIME,
                        not_null=True,
                        description="When the event was written, UTC.",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        name="idx_deployment_record_event_position",
                        columns=[COL_DEPLOYMENT_ID, COL_ORDINAL],
                        unique=True,
                    ),
                ],
            ),
        },
    )
