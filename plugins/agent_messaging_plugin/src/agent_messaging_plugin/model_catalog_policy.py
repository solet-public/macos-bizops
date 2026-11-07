"""Versioned catalog configuration with authorized transactional lifecycles.

A2 owns authorized transactional set/correct/clear/retire/reopen operations.
Each current row projects a full immutable history snapshot. Revision identity,
the expected-revision premise and predecessor survive correction and reopening;
cleared optional settings remain null. Credential values belong to the credential
service: registrations contain only a reference. Read failures propagate rather
than masquerading as absent configuration or empty history.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NS
from ananta.llm.agent_messaging.state_results import require_records
from ananta.types.schema_types import (
    ColumnDefinition,
    ColumnType,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

from .model_capability_store import CatalogError

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import (
        StateManagementInterface,
        StateTransaction,
    )

TABLE_MODEL_CATALOG_POLICY = "model_catalog_policy"
TABLE_MODEL_CATALOG_POLICY_HISTORY = "model_catalog_policy_history"
TABLE_MODEL_CATALOG_SOURCE = "model_catalog_source"
TABLE_MODEL_CATALOG_SOURCE_HISTORY = "model_catalog_source_history"


@dataclass(frozen=True, slots=True)
class CatalogRun:
    """Current coordinator generation; A6a owns transitions and lease CAS."""

    generation_id: str
    revision: int
    status: str
    owner_id: str | None
    lease_token: str | None
    expected_revision: int | None
    worker_run_id: str | None
    adapter_revisions: dict[str, str] | None
    trigger: str | None
    idempotency_key: str | None
    started_at: str | None
    finished_at: str | None
    due_at: str | None
    result_id: str | None


@dataclass(frozen=True, slots=True)
class CatalogRunEvent:
    """Append-only transition or recovery evidence for a run generation."""

    event_id: str
    generation_id: str
    revision: int
    action: str
    status: str
    recorded_at: str | None
    detail: str | None


@dataclass(frozen=True, slots=True)
class CatalogRunResult:
    """Immutable result identity, distinct from its run and event identities."""

    result_id: str
    generation_id: str
    status: str
    result: dict[str, Any] | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class CatalogSchedule:
    """Schedule generation, including replacement and successor custody."""

    generation_id: str
    revision: int
    status: str
    schedule_id: str | None
    replaced_schedule_id: str | None
    successor_schedule_id: str | None
    expected_revision: int | None
    due_at: str | None
    recorded_at: str | None


@dataclass(frozen=True, slots=True)
class CatalogRevision:
    """Identity and write premise shared by current and historical snapshots."""

    stable_id: str
    revision_id: str
    revision: int
    expected_revision: int | None
    predecessor_revision_id: str | None
    actor: str
    reason: str
    action: str
    recorded_at: str
    lifecycle_state: str


@dataclass(frozen=True, slots=True)
class CatalogPolicy:
    provenance: CatalogRevision
    dispatch_score_margin: float | None
    cost_tie_tolerance: float | None
    confidence_level: float | None
    interval_method: str | None
    interval_method_revision: str | None
    bootstrap_resamples: int | None
    outcome_window_hours: float | None
    staleness_window_hours: float | None
    refresh_interval_seconds: float | None
    refresh_dedupe_seconds: float | None


@dataclass(frozen=True, slots=True)
class CatalogSource:
    provenance: CatalogRevision
    provider_id: str
    source_id: str
    implementation_id: str
    implementation_version: str
    endpoint: str | None
    credential_ref: str | None
    metric_units: dict[str, str]
    priority: int | None
    aliases: dict[str, str]
    enabled: bool


@dataclass(frozen=True, slots=True)
class CatalogHistoryPage[T]:
    """Explicit page frontier; no truncated history is presented as complete."""

    records: tuple[T, ...]
    next_after: tuple[int, str] | None


def _column(kind: ColumnType, description: str, *, required: bool = False) -> ColumnDefinition:
    return ColumnDefinition(type=kind, not_null=required, description=description)


def _revision_columns() -> dict[str, ColumnDefinition]:
    return {
        "stable_id": _column(
            ColumnType.TEXT,
            "Stable policy or source-registration identity across revisions.",
            required=True,
        ),
        "revision_id": _column(
            ColumnType.TEXT,
            "Immutable snapshot identity, shared with its current projection.",
            required=True,
        ),
        "revision": _column(ColumnType.INTEGER, "Monotonic revision of this stable identity.", required=True),
        "expected_revision": _column(ColumnType.INTEGER, "Writer's expected prior revision; null on initial creation."),
        "predecessor_revision_id": _column(ColumnType.TEXT, "Prior immutable revision; null on initial creation."),
        "actor": _column(ColumnType.TEXT, "Authorized configuration actor identity.", required=True),
        "reason": _column(ColumnType.TEXT, "Reason for the recorded transition.", required=True),
        "action": _column(ColumnType.TEXT, "set | correct | clear | retire | reopen.", required=True),
        "recorded_at": _column(ColumnType.TEXT, "ISO-8601 transition time with UTC offset.", required=True),
        "lifecycle_state": _column(
            ColumnType.TEXT,
            "active | retired; history retains terminal and reversal snapshots.",
            required=True,
        ),
    }


def _policy_columns() -> dict[str, ColumnDefinition]:
    return {
        **_revision_columns(),
        "dispatch_score_margin": _column(ColumnType.REAL, "Optional additive score margin; null means absent."),
        "cost_tie_tolerance": _column(ColumnType.REAL, "Optional tie tolerance in the compared cost objective."),
        "confidence_level": _column(ColumnType.REAL, "Explicit joint confidence level; never seeded by this module."),
        "interval_method": _column(ColumnType.TEXT, "Explicit interval/resampling method identifier."),
        "interval_method_revision": _column(ColumnType.TEXT, "Revision of the interval method."),
        "bootstrap_resamples": _column(ColumnType.INTEGER, "Explicit bootstrap resample count, not a sample threshold."),
        "outcome_window_hours": _column(ColumnType.REAL, "Optional outcome cohort window in hours."),
        "staleness_window_hours": _column(ColumnType.REAL, "Optional accepted-reading freshness window in hours."),
        "refresh_interval_seconds": _column(ColumnType.REAL, "Optional coordinator refresh cadence in seconds."),
        "refresh_dedupe_seconds": _column(ColumnType.REAL, "Optional shared refresh deduplication window in seconds."),
    }


def _source_columns() -> dict[str, ColumnDefinition]:
    return {
        **_revision_columns(),
        "provider_id": _column(
            ColumnType.TEXT,
            "Provider identity; not an execution-runtime declaration.",
            required=True,
        ),
        "source_id": _column(ColumnType.TEXT, "Provider, benchmark or harness source identity.", required=True),
        "implementation_id": _column(ColumnType.TEXT, "Installed adapter implementation identity.", required=True),
        "implementation_version": _column(ColumnType.TEXT, "Adapter implementation version.", required=True),
        "endpoint": _column(ColumnType.TEXT, "Optional source endpoint, without embedded credentials."),
        "credential_ref": _column(ColumnType.TEXT, "Optional credential-service reference; never a secret value."),
        "metric_units": _column(
            ColumnType.JSON,
            "Metric-to-native-unit semantics; distinct price/score units stay distinct.",
            required=True,
        ),
        "priority": _column(ColumnType.INTEGER, "Optional explicit source priority."),
        "aliases": _column(
            ColumnType.JSON,
            "Provider/harness join aliases; no selector-specific branches.",
            required=True,
        ),
        "enabled": _column(
            ColumnType.BOOLEAN,
            "Explicit registration enablement, independent of catalog cell enablement.",
            required=True,
        ),
    }


def _table(name: str, prefix: str, columns: dict[str, ColumnDefinition], *, history: bool) -> TableSchema:
    indexes = [IndexDefinition(name=f"idx_{name}_revision_id", columns=["revision_id"], unique=True)]
    identity_columns = ["stable_id", "revision"] if history else ["stable_id"]
    indexes.append(IndexDefinition(name=f"idx_{name}_identity", columns=identity_columns, unique=True))
    return TableSchema(
        table_name=name,
        id_prefix=prefix,
        columns=columns,
        indexes=indexes,
        description="Append-only full revision snapshots; A2 supplies lifecycle writers." if history else "Current revision projection; A2 supplies lifecycle writers.",
    )


def _coordinator_table(name: str, prefix: str, fields: dict[str, tuple[ColumnType, bool]], identity: str) -> TableSchema:
    return TableSchema(
        table_name=name,
        id_prefix=prefix,
        columns={key: _column(kind, f"Coordinator {key.replace('_', ' ')}; no secret values.", required=required) for key, (kind, required) in fields.items()},
        indexes=[IndexDefinition(name=f"idx_{name}_{identity}", columns=[identity], unique=True)],
        description="Additive coordinator declaration; A6a supplies lifecycle writers.",
    )


def _coordinator_tables() -> dict[str, TableSchema]:
    text = ColumnType.TEXT
    integer = ColumnType.INTEGER
    json = ColumnType.JSON
    definitions = {
        "model_catalog_run": (
            "mctr",
            "generation_id",
            {
                "generation_id": (text, True),
                "revision": (integer, True),
                "status": (text, True),
                "owner_id": (text, False),
                "lease_token": (text, False),
                "expected_revision": (integer, False),
                "worker_run_id": (text, False),
                "adapter_revisions": (json, False),
                "trigger": (text, False),
                "idempotency_key": (text, False),
                "started_at": (text, False),
                "finished_at": (text, False),
                "due_at": (text, False),
                "result_id": (text, False),
            },
        ),
        "model_catalog_run_event": (
            "mctre",
            "event_id",
            {
                "event_id": (text, True),
                "generation_id": (text, True),
                "revision": (integer, True),
                "action": (text, True),
                "status": (text, True),
                "recorded_at": (text, False),
                "detail": (text, False),
            },
        ),
        "model_catalog_run_result": (
            "mctrr",
            "result_id",
            {
                "result_id": (text, True),
                "generation_id": (text, True),
                "status": (text, True),
                "result": (json, False),
                "finished_at": (text, False),
            },
        ),
        "model_catalog_schedule": (
            "mctsc",
            "generation_id",
            {
                "generation_id": (text, True),
                "revision": (integer, True),
                "status": (text, True),
                "schedule_id": (text, False),
                "replaced_schedule_id": (text, False),
                "successor_schedule_id": (text, False),
                "expected_revision": (integer, False),
                "due_at": (text, False),
                "recorded_at": (text, False),
            },
        ),
    }
    tables = {name: _coordinator_table(name, prefix, fields, identity) for name, (prefix, identity, fields) in definitions.items()}
    schedule = tables["model_catalog_schedule"]
    tables["model_catalog_schedule_history"] = TableSchema(
        table_name="model_catalog_schedule_history",
        id_prefix="mctshg",
        columns=dict(schedule.columns),
        indexes=[
            IndexDefinition(
                name="idx_model_catalog_schedule_history_revision",
                columns=["generation_id", "revision"],
                unique=True,
            )
        ],
        description="Append-only schedule generation snapshots; A6a supplies lifecycle writers.",
    )
    return tables


def get_model_catalog_policy_schema_definition() -> SchemaDefinition:
    """Declare policy and coordinator tables without changing other containers."""
    return SchemaDefinition(
        namespace=NS,
        version="1.0.0",
        description="Versioned catalog policy and source registrations; additive read-only A1 boundary.",
        tables={
            **_coordinator_tables(),
            TABLE_MODEL_CATALOG_POLICY: _table(TABLE_MODEL_CATALOG_POLICY, "mctp", _policy_columns(), history=False),
            TABLE_MODEL_CATALOG_POLICY_HISTORY: _table(TABLE_MODEL_CATALOG_POLICY_HISTORY, "mctph", _policy_columns(), history=True),
            TABLE_MODEL_CATALOG_SOURCE: _table(TABLE_MODEL_CATALOG_SOURCE, "mcts", _source_columns(), history=False),
            TABLE_MODEL_CATALOG_SOURCE_HISTORY: _table(TABLE_MODEL_CATALOG_SOURCE_HISTORY, "mctsh", _source_columns(), history=True),
        },
    )


def _provenance(row: Mapping[str, Any]) -> CatalogRevision:
    return CatalogRevision(**{name: row[name] for name in CatalogRevision.__dataclass_fields__})


def _policy(row: Mapping[str, Any]) -> CatalogPolicy:
    return CatalogPolicy(
        provenance=_provenance(row),
        **{name: row.get(name) for name in CatalogPolicy.__dataclass_fields__ if name != "provenance"},
    )


def _source(row: Mapping[str, Any]) -> CatalogSource:
    return CatalogSource(
        provenance=_provenance(row),
        provider_id=row["provider_id"],
        source_id=row["source_id"],
        implementation_id=row["implementation_id"],
        implementation_version=row["implementation_version"],
        endpoint=row.get("endpoint"),
        credential_ref=row.get("credential_ref"),
        metric_units=row["metric_units"],
        priority=row.get("priority"),
        aliases=row["aliases"],
        enabled=row["enabled"],
    )


def _current(state: StateManagementInterface, table: str, stable_id: str) -> dict[str, Any] | None:
    rows = require_records(
        state.query_ordered(
            NS,
            {
                "table": table,
                "filters": {"stable_id": stable_id, "is_deleted": 0},
                "order_by": [["revision", "asc"], ["id", "asc"]],
                "limit": 1,
            },
        )
    )
    return rows[0] if rows else None


def read_catalog_policy(state: StateManagementInterface, stable_id: str) -> CatalogPolicy | None:
    """Return current policy, including retired state, or None when unconfigured."""
    row = _current(state, TABLE_MODEL_CATALOG_POLICY, stable_id)
    return _policy(row) if row is not None else None


def read_catalog_source(state: StateManagementInterface, stable_id: str) -> CatalogSource | None:
    """Read only credential references; no credential-service lookup occurs."""
    row = _current(state, TABLE_MODEL_CATALOG_SOURCE, stable_id)
    return _source(row) if row is not None else None


def _history(
    state: StateManagementInterface,
    table: str,
    stable_id: str,
    limit: int,
    after: tuple[int, str] | None,
) -> tuple[list[dict[str, Any]], tuple[int, str] | None]:
    if isinstance(limit, bool) or limit < 1:
        raise ValueError("history limit must be a positive integer")
    query: dict[str, Any] = {
        "table": table,
        "filters": {"stable_id": stable_id},
        "order_by": [["revision", "asc"], ["id", "asc"]],
        "limit": limit + 1,
        "unbounded": True,
    }
    if after is not None:
        query["after"] = list(after)
    rows = require_records(state.query_ordered(NS, query))
    page = rows[:limit]
    cursor = (cast(int, page[-1]["revision"]), cast(str, page[-1]["id"])) if len(rows) > limit else None
    return page, cursor


def read_catalog_policy_history(
    state: StateManagementInterface,
    stable_id: str,
    *,
    limit: int,
    after: tuple[int, str] | None = None,
) -> CatalogHistoryPage[CatalogPolicy]:
    """Read immutable policy revisions in order with an explicit page size."""
    rows, cursor = _history(state, TABLE_MODEL_CATALOG_POLICY_HISTORY, stable_id, limit, after)
    return CatalogHistoryPage(tuple(_policy(row) for row in rows), cursor)


def read_catalog_source_history(
    state: StateManagementInterface,
    stable_id: str,
    *,
    limit: int,
    after: tuple[int, str] | None = None,
) -> CatalogHistoryPage[CatalogSource]:
    """Read immutable registration revisions, retaining retired/cleared history."""
    rows, cursor = _history(state, TABLE_MODEL_CATALOG_SOURCE_HISTORY, stable_id, limit, after)
    return CatalogHistoryPage(tuple(_source(row) for row in rows), cursor)


_POLICY_FIELDS = frozenset(CatalogPolicy.__dataclass_fields__) - {"provenance"}
_SOURCE_REQUIRED = frozenset(
    {
        "provider_id",
        "source_id",
        "implementation_id",
        "implementation_version",
        "metric_units",
        "aliases",
        "enabled",
    }
)
_SOURCE_OPTIONAL = frozenset({"endpoint", "credential_ref", "priority"})
_SOURCE_FIELDS = _SOURCE_REQUIRED | _SOURCE_OPTIONAL
_TEXT_FIELDS = frozenset(
    {
        "interval_method",
        "interval_method_revision",
        "provider_id",
        "source_id",
        "implementation_id",
        "implementation_version",
        "endpoint",
        "credential_ref",
    }
)


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("parameter_invalid", f"{name} must be a nonblank string.")
    return value


def _numeric(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not isfinite(value):
        raise CatalogError("parameter_invalid", f"{name} must be a finite number.")
    if name in {"priority", "bootstrap_resamples"} and not isinstance(value, int):
        raise CatalogError("parameter_invalid", f"{name} must be an integer.")
    _numeric_range(value, name)


def _numeric_range(value: float, name: str) -> None:
    if name == "confidence_level":
        valid = 0 < value < 1
    elif name in {
        "bootstrap_resamples",
        "outcome_window_hours",
        "staleness_window_hours",
        "refresh_interval_seconds",
    }:
        valid = value > 0
    else:
        valid = value >= 0
    if not valid:
        raise CatalogError("parameter_invalid", f"{name} is outside its valid range.")


def _string_map(value: object, name: str) -> None:
    if not isinstance(value, dict):
        raise CatalogError("parameter_invalid", f"{name} must be a string-to-string object; empty clears it.")
    for key, item in value.items():
        _text(key, name)
        _text(item, name)


def _validate_fields(values: dict[str, Any]) -> None:
    for name, value in values.items():
        if value is None:
            continue
        if name in _TEXT_FIELDS:
            _text(value, name)
        elif name in {"metric_units", "aliases"}:
            _string_map(value, name)
        elif name == "enabled":
            if not isinstance(value, bool):
                raise CatalogError("parameter_invalid", "enabled must be boolean.")
        else:
            _numeric(value, name)
    _validate_endpoint(values.get("endpoint"))


def _validate_endpoint(endpoint: object) -> None:
    if endpoint is None:
        return
    try:
        parsed = urlsplit(_text(endpoint, "endpoint"))
    except ValueError as exc:
        raise CatalogError("parameter_invalid", "endpoint must be a valid URL without embedded secrets.") from exc
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise CatalogError(
            "parameter_invalid",
            "endpoint must not embed authentication, query secrets or fragments; use credential_ref.",
        )


def _write_inputs(params: dict[str, Any], *, source: bool) -> tuple[str, str, int | None, str]:
    if set(params) - {
        "stable_id",
        "action",
        "expected_revision",
        "reason",
        "values",
        "clear_fields",
    }:
        raise CatalogError(
            "parameter_invalid",
            "Unknown catalog configuration argument; identity is server-derived.",
        )
    stable_id = _text(params.get("stable_id"), "stable_id")
    action = _text(params.get("action"), "action")
    reason = _text(params.get("reason"), "reason")
    if action not in {"set", "correct", "clear", "retire", "reopen"}:
        raise CatalogError("parameter_invalid", "action must be set, correct, clear, retire or reopen.")
    expected = _expected_revision(params)
    _patch_values(params, source=source)
    return stable_id, action, expected, reason


def _expected_revision(params: dict[str, Any]) -> int | None:
    if "expected_revision" not in params:
        raise CatalogError(
            "parameter_invalid",
            "expected_revision is required; explicitly null only for initial set.",
        )
    expected = params["expected_revision"]
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 1):
        raise CatalogError("parameter_invalid", "expected_revision must be a positive integer or initial null.")
    return expected


def _patch_values(params: dict[str, Any], *, source: bool) -> dict[str, Any]:
    action = params.get("action")
    values = params.get("values", {})
    allowed = _SOURCE_FIELDS if source else _POLICY_FIELDS
    if not isinstance(values, dict) or set(values) - allowed:
        raise CatalogError(
            "parameter_invalid",
            "values must contain only declared configuration fields, never secret values.",
        )
    if action in {"set", "correct"}:
        return _set_patch(params, values)
    if values or "values" in params:
        raise CatalogError("parameter_invalid", "Only set/correct accept values.")
    return _clear_patch(params, source=source)


def _set_patch(params: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    if "clear_fields" in params or any(value is None for value in values.values()):
        raise CatalogError("parameter_invalid", "Use clear with clear_fields to remove optional values.")
    if params["action"] == "correct" and not values:
        raise CatalogError("parameter_invalid", "correct requires changed values.")
    _validate_fields(values)
    return values


def _clear_patch(params: dict[str, Any], *, source: bool) -> dict[str, Any]:
    if params.get("action") != "clear":
        if "clear_fields" in params:
            raise CatalogError("parameter_invalid", "Only clear accepts clear_fields.")
        return {}
    fields = params.get("clear_fields")
    optional = _SOURCE_OPTIONAL if source else _POLICY_FIELDS
    if not isinstance(fields, list) or not fields or any(not isinstance(name, str) or name not in optional for name in fields):
        raise CatalogError(
            "parameter_invalid",
            "clear_fields must name optional fields; clear maps with correct and an empty object.",
        )
    return dict.fromkeys(fields)


def _next_values(current: dict[str, object] | None, params: dict[str, Any], *, source: bool) -> dict[str, Any]:
    fields = _SOURCE_FIELDS if source else _POLICY_FIELDS
    values = {name: current.get(name) if current is not None else None for name in fields}
    values.update(_patch_values(params, source=source))
    if source and any(values[name] is None for name in _SOURCE_REQUIRED):
        raise CatalogError(
            "parameter_invalid",
            "Initial source set requires provider_id, source_id, implementation_id, implementation_version, metric_units, aliases and enabled.",
        )
    _validate_fields(values)
    return values


def _transition(current: dict[str, object] | None, action: str, expected: int | None) -> str:
    if current is None:
        if action != "set" or expected is not None:
            raise CatalogError(
                "stale_catalog_revision",
                "Initial set requires an absent record and expected_revision=null.",
            )
        return "active"
    if expected != current["revision"] or action == "set":
        raise CatalogError(
            "stale_catalog_revision",
            "Read the current revision before changing existing configuration.",
        )
    lifecycle = cast(str, current["lifecycle_state"])
    if action == "retire":
        if lifecycle != "active":
            raise CatalogError("invalid_transition", "Only active configuration can retire.")
        return "retired"
    if action == "reopen":
        if lifecycle != "retired":
            raise CatalogError("invalid_transition", "Only retired configuration can reopen.")
        return "active"
    return lifecycle


def _append_revision(
    txn: StateTransaction,
    table: str,
    history: str,
    current: dict[str, object] | None,
    snapshot: dict[str, Any],
) -> None:
    stamp = datetime.now(UTC)
    audit: dict[str, object] = {
        "namespace": NS,
        "updated_at": stamp,
        "updated_by": snapshot["actor"],
    }
    if current is None:
        txn.write_state(
            NS,
            {
                "table": table,
                "record": {
                    "id": f"mct-{uuid4().hex}",
                    "created_at": stamp,
                    "created_by": snapshot["actor"],
                    **audit,
                    **snapshot,
                },
            },
        )
    elif (
        txn.update_state(
            NS,
            {
                "table": table,
                "filters": {
                    "id": current["id"],
                    "revision": snapshot["expected_revision"],
                    "revision_id": current["revision_id"],
                },
            },
            {**audit, **snapshot},
        )
        != 1
    ):
        raise CatalogError(
            "stale_catalog_revision",
            "Configuration changed concurrently; no projection or history committed.",
        )
    txn.write_state(
        NS,
        {
            "table": history,
            "record": {
                "id": snapshot["revision_id"],
                "created_at": stamp,
                "created_by": snapshot["actor"],
                **audit,
                **snapshot,
            },
        },
    )


def set_catalog_configuration(state: StateManagementInterface, params: dict[str, Any], *, actor: str, source: bool) -> dict[str, Any]:
    """Plugin authorizes the derived Operator; all row/history writes commit together.

    Initial creation is protected by A1's unique stable_id index. Existing
    revisions are claimed by a predicated update; a stale loser raises before
    its history insert. No secret is resolved and no selection default is read.
    """
    _text(actor, "derived actor")
    stable_id, action, expected, reason = _write_inputs(params, source=source)
    table = TABLE_MODEL_CATALOG_SOURCE if source else TABLE_MODEL_CATALOG_POLICY
    history = TABLE_MODEL_CATALOG_SOURCE_HISTORY if source else TABLE_MODEL_CATALOG_POLICY_HISTORY
    with state.transactional() as txn:
        rows = txn.query_state(NS, {"table": table, "filters": {"stable_id": stable_id}, "limit": 1})
        current = rows[0] if rows else None
        lifecycle = _transition(current, action, expected)
        snapshot = {
            **_next_values(current, params, source=source),
            "stable_id": stable_id,
            "revision_id": f"mct-rev-{uuid4().hex}",
            "revision": 1 if current is None else cast(int, current["revision"]) + 1,
            "expected_revision": expected,
            "predecessor_revision_id": None if current is None else current["revision_id"],
            "actor": actor,
            "reason": reason,
            "action": action,
            "recorded_at": datetime.now(UTC).isoformat(),
            "lifecycle_state": lifecycle,
        }
        _append_revision(txn, table, history, current, snapshot)
    return {"current": asdict(_source(snapshot) if source else _policy(snapshot))}


def _history_cursor(raw: object) -> tuple[int, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, list) or len(raw) != 2:
        raise CatalogError("parameter_invalid", "after must be the [revision, id] frontier from readback.")
    revision, identifier = raw
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise CatalogError("parameter_invalid", "after revision must be positive.")
    return revision, _text(identifier, "after id")


def read_catalog_configuration(state: StateManagementInterface, params: dict[str, Any]) -> dict[str, Any]:
    """Current and explicitly requested history page for a policy or source.

    History is not implicitly truncated: request history_limit to receive a
    page and its next_after frontier. Missing current configuration is advisory.
    """
    stable_id = _text(params.get("stable_id"), "stable_id")
    kind = params.get("kind")
    if kind not in {"policy", "source"}:
        raise CatalogError("parameter_invalid", "kind must be policy or source.")
    source = kind == "source"
    current = read_catalog_source(state, stable_id) if source else read_catalog_policy(state, stable_id)
    result: dict[str, Any] = {
        "current": asdict(current) if current is not None else None,
        "history": None,
        "next_after": None,
    }
    if current is None:
        result["warnings"] = ["Configuration absent; optional settings remain null. No numeric defaults applied."]
    _read_history_page(state, params, result, stable_id=stable_id, source=source)
    return result


def _read_history_page(
    state: StateManagementInterface,
    params: dict[str, Any],
    result: dict[str, Any],
    *,
    stable_id: str,
    source: bool,
) -> None:
    if "history_limit" in params:
        limit = params["history_limit"]
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise CatalogError("parameter_invalid", "history_limit must be a positive integer.")
        reader = read_catalog_source_history if source else read_catalog_policy_history
        page = reader(state, stable_id, limit=limit, after=_history_cursor(params.get("after")))
        result.update(
            history=[asdict(row) for row in page.records],
            next_after=list(page.next_after) if page.next_after is not None else None,
        )
    elif "after" in params:
        raise CatalogError("parameter_invalid", "after requires history_limit.")
