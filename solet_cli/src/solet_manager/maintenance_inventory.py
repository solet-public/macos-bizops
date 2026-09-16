"""Read-only compatibility projection for create registries and v2 inventories."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .errors import StateError
from .models import JsonValue
from .registry import _INSTANCE_KEYS, _LEGACY_INSTANCE_KEYS

CREATE_REGISTRY_SCHEMA_VERSION = 1
MAINTENANCE_INVENTORY_SCHEMA_VERSION = 2
_V1_KEYS = frozenset({"schema_version", "instances"})
_V2_KEYS = frozenset({"schema_version", "records"})
_V2_RECORD_KEYS = frozenset(
    {
        "name",
        "management_origin",
        "management_state",
        "active_operation",
        "update_eligibility",
        "verified_release",
        "source_release",
        "runtime_release",
        "contract_identities",
    }
)


@dataclass(frozen=True)
class LegacyCreateInventoryProjection:
    """Lossless v1 record with explicit unavailable maintenance fields."""

    create_record: dict[str, JsonValue]
    management_origin: str = "create"
    management_state: str = "unknown"
    active_operation: None = None
    update_eligibility: None = None
    verified_release: None = None
    source_release: None = None
    runtime_release: None = None
    contract_identities: None = None


@dataclass(frozen=True)
class MaintenanceInventoryRecord:
    name: str
    management_origin: str
    management_state: str
    active_operation: JsonValue
    update_eligibility: JsonValue
    verified_release: JsonValue
    source_release: JsonValue
    runtime_release: JsonValue
    contract_identities: JsonValue


def read_maintenance_inventory(
    path: Path,
) -> tuple[LegacyCreateInventoryProjection | MaintenanceInventoryRecord, ...]:
    """Read one closed document; this function never creates or changes state."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"maintenance inventory is unreadable: {exc}") from exc
    if not isinstance(raw, dict):
        raise StateError("maintenance inventory must be an object")
    if raw.get("schema_version") == CREATE_REGISTRY_SCHEMA_VERSION:
        return _project_v1(cast(dict[str, object], raw))
    if raw.get("schema_version") == MAINTENANCE_INVENTORY_SCHEMA_VERSION:
        return _read_v2(cast(dict[str, object], raw))
    raise StateError("maintenance inventory schema_version must be exactly 1 or 2")


def _project_v1(raw: dict[str, object]) -> tuple[LegacyCreateInventoryProjection, ...]:
    if frozenset(raw) != _V1_KEYS or not isinstance(raw["instances"], dict):
        raise StateError("create registry does not match the closed v1 shape")
    projections: list[LegacyCreateInventoryProjection] = []
    for name, value in sorted(raw["instances"].items()):
        if (
            not isinstance(name, str)
            or not isinstance(value, dict)
            or frozenset(value) not in {_INSTANCE_KEYS, _LEGACY_INSTANCE_KEYS}
            or value.get("name") != name
        ):
            raise StateError("create registry has an invalid v1 instance")
        projections.append(
            LegacyCreateInventoryProjection(cast(dict[str, JsonValue], value.copy()))
        )
    return tuple(projections)


def _read_v2(raw: dict[str, object]) -> tuple[MaintenanceInventoryRecord, ...]:
    if frozenset(raw) != _V2_KEYS or not isinstance(raw["records"], list):
        raise StateError("maintenance inventory does not match the closed v2 shape")
    records: list[MaintenanceInventoryRecord] = []
    for value in raw["records"]:
        if not isinstance(value, dict) or frozenset(value) != _V2_RECORD_KEYS:
            raise StateError("maintenance inventory record does not match the closed v2 shape")
        name = value["name"]
        origin = value["management_origin"]
        state = value["management_state"]
        if not all(isinstance(item, str) and item for item in (name, origin, state)):
            raise StateError("maintenance inventory identity fields must be non-empty strings")
        records.append(
            MaintenanceInventoryRecord(
                cast(str, name),
                cast(str, origin),
                cast(str, state),
                cast(JsonValue, value["active_operation"]),
                cast(JsonValue, value["update_eligibility"]),
                cast(JsonValue, value["verified_release"]),
                cast(JsonValue, value["source_release"]),
                cast(JsonValue, value["runtime_release"]),
                cast(JsonValue, value["contract_identities"]),
            )
        )
    return tuple(sorted(records, key=lambda record: record.name))
