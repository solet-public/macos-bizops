"""Durable schema for the solet's own record of its work folders.

Two ``core``-namespace tables owned by the lifecycle-management service:

- ``work_folder`` — one row per scratch folder a creator opened: where it is,
  what it is for, what it is bound to, how it is harvested, and which state of
  its life it is in.
- ``work_folder_event`` — append-only history of every change of those rows
  (opened, corrected, closed, harvested, reopened, retired, restored, deleted,
  removal_failed).

A row is the solet's record of what it created, what ended it and what removed
it. ``version`` is the compare-and-set guard for every change, and every event
carries the version the row had after it, so the history is gap-checkable.

``dev`` and ``ino`` are stored as decimal text: an inode number can exceed the
32-bit ``INTEGER`` the database gives the logical integer type.

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

WORK_FOLDER_NAMESPACE = "core"
TABLE_WORK_FOLDER = "work_folder"
TABLE_WORK_FOLDER_EVENT = "work_folder_event"

COL_FOLDER_ID = "folder_id"
COL_PATH = "path"
COL_PARENT_DIR = "parent_dir"
COL_DEV = "dev"
COL_INO = "ino"
COL_PURPOSE = "purpose"
COL_BOUND_KIND = "bound_kind"
COL_BOUND_REF = "bound_ref"
COL_HARVEST_KIND = "harvest_kind"
COL_OWNER = "owner"
COL_STATE = "state"
COL_HARVEST_RECEIPT = "harvest_receipt"
COL_VERSION = "version"

COL_ORDINAL = "ordinal"
COL_KIND = "kind"
COL_PRIOR_VERSION = "prior_version"
COL_ACTOR_INSTANCE = "actor_instance"
COL_ACTOR_SESSION = "actor_session"
COL_ACTOR_MODEL = "actor_model"
COL_ACTOR_EFFORT = "actor_effort"
COL_DETAIL = "detail"
COL_RECORDED_AT = "recorded_at"

HARVEST_REGENERABLE = "regenerable"
HARVEST_EVIDENCE_COPY = "evidence_copy"
HARVEST_CUSTODY_PACKET = "custody_packet"
HARVEST_KINDS = (HARVEST_REGENERABLE, HARVEST_EVIDENCE_COPY, HARVEST_CUSTODY_PACKET)

STATE_OPEN = "open"
STATE_DUE = "due"
STATE_HARVESTED = "harvested"
STATE_DELETED = "deleted"
STATE_RETIRED = "retired"
STATES = (STATE_OPEN, STATE_DUE, STATE_HARVESTED, STATE_DELETED, STATE_RETIRED)

KIND_OPENED = "opened"
KIND_CORRECTED = "corrected"
KIND_CLOSED = "closed"
KIND_HARVESTED = "harvested"
KIND_REOPENED = "reopened"
KIND_RETIRED = "retired"
KIND_RESTORED = "restored"
KIND_DELETED = "deleted"
KIND_REMOVAL_FAILED = "removal_failed"
EVENT_KINDS = (
    KIND_OPENED,
    KIND_CORRECTED,
    KIND_CLOSED,
    KIND_HARVESTED,
    KIND_REOPENED,
    KIND_RETIRED,
    KIND_RESTORED,
    KIND_DELETED,
    KIND_REMOVAL_FAILED,
)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def get_work_folder_schema() -> SchemaDefinition:
    """Declarative schema for ``core__work_folder`` and its event table."""
    return SchemaDefinition(
        namespace=WORK_FOLDER_NAMESPACE,
        tables={
            TABLE_WORK_FOLDER: TableSchema(
                table_name=TABLE_WORK_FOLDER,
                id_prefix="wkf",
                description="One row per scratch folder this solet opened: where it is, what it is for, what it is bound to and where it is in its life.",
                columns={
                    COL_FOLDER_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Row key (wkf-...). UNIQUE; the event table keys on it.",
                    ),
                    COL_PATH: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Canonical real path of the folder. At most one row that is not deleted holds a path.",
                    ),
                    COL_PARENT_DIR: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Directory that contains the folder; lets a creator list its own siblings.",
                    ),
                    COL_DEV: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="st_dev of the directory when it was opened, decimal text (the sweep re-checks it before removal).",
                    ),
                    COL_INO: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="st_ino of the directory when it was opened, decimal text (the sweep re-checks it before removal).",
                    ),
                    COL_PURPOSE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="What the folder is for, in the creator's words.",
                    ),
                    COL_BOUND_KIND: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="What kind of thing the folder is bound to (for example a release run); free text.",
                    ),
                    COL_BOUND_REF: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Identifier of the bound thing, within its kind.",
                    ),
                    COL_HARVEST_KIND: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=_in_list(COL_HARVEST_KIND, HARVEST_KINDS),
                        description="What must be kept before removal: nothing (regenerable), an evidence copy, or a custody packet.",
                    ),
                    COL_OWNER: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="The creator's session or process label; recorded, not an authority.",
                    ),
                    COL_STATE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=_in_list(COL_STATE, STATES),
                        description="open, due, harvested, deleted (terminal) or retired; every change is a compare-and-set on version.",
                    ),
                    COL_HARVEST_RECEIPT: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="JSON text of the current harvest receipt; empty when none. Reopening clears it; the history keeps it.",
                    ),
                    COL_VERSION: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        check=f"{COL_VERSION} >= 1",
                        description="Starts at 1; every event adds 1. The compare-and-set guard.",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        name="idx_work_folder_folder_id",
                        columns=[COL_FOLDER_ID],
                        unique=True,
                    ),
                    IndexDefinition(
                        name="idx_work_folder_live_path",
                        columns=[COL_PATH],
                        unique=True,
                        where=f"{COL_STATE} <> '{STATE_DELETED}'",
                    ),
                    IndexDefinition(
                        name="idx_work_folder_parent_state",
                        columns=[COL_PARENT_DIR, COL_STATE],
                    ),
                    IndexDefinition(
                        name="idx_work_folder_state_path",
                        columns=[COL_STATE, COL_PATH, COL_FOLDER_ID],
                    ),
                ],
            ),
            TABLE_WORK_FOLDER_EVENT: TableSchema(
                table_name=TABLE_WORK_FOLDER_EVENT,
                id_prefix="wfe",
                description="Append-only history of every change of a work folder. Rows are written once and never updated.",
                columns={
                    COL_FOLDER_ID: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="The work_folder row this event belongs to.",
                    ),
                    COL_ORDINAL: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="1-based position in the folder's history; equals the row version after the event.",
                    ),
                    COL_KIND: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        check=_in_list(COL_KIND, EVENT_KINDS),
                        description="What happened to the folder.",
                    ),
                    COL_PRIOR_VERSION: ColumnDefinition(
                        type=ColumnType.INTEGER,
                        not_null=True,
                        description="The row version this event was applied on top of (0 for the first).",
                    ),
                    COL_ACTOR_INSTANCE: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Agent instance that caused the event; empty when none applies.",
                    ),
                    COL_ACTOR_SESSION: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Agent session that caused the event; empty when none applies.",
                    ),
                    COL_ACTOR_MODEL: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Model of the acting session; empty when none applies.",
                    ),
                    COL_ACTOR_EFFORT: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="Effort tier of the acting session; empty when none applies.",
                    ),
                    COL_DETAIL: ColumnDefinition(
                        type=ColumnType.TEXT,
                        not_null=True,
                        description="JSON text with what the event changed or why (the reason, the changed fields, the receipt, the removal outcome).",
                    ),
                    COL_RECORDED_AT: ColumnDefinition(
                        type=ColumnType.DATETIME,
                        not_null=True,
                        description="When the event was written, UTC.",
                    ),
                },
                indexes=[
                    IndexDefinition(
                        name="idx_work_folder_event_position",
                        columns=[COL_FOLDER_ID, COL_ORDINAL],
                        unique=True,
                    ),
                ],
            ),
        },
    )
