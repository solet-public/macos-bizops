"""Informational inspection of both Manager inventory generations."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .enrolled_target import TARGET_RECOVERY, identity_mismatches
from .errors import StateError
from .existing_install_inspection import (
    InspectionCheck,
    InspectionEffectTracker,
    InspectionStatus,
    TargetFilesystemIdentity,
    _check,
)
from .maintenance_inventory import (
    LegacyCreateInventoryProjection,
    parse_maintenance_inventory_bytes,
    parse_maintenance_inventory_v2_bytes,
)
from .models import InstanceInventoryRecordV2, JsonValue
from .paths import ManagerPaths


@dataclass(frozen=True)
class InventoryRead:
    status: InspectionStatus
    records: tuple[LegacyCreateInventoryProjection | InstanceInventoryRecordV2, ...] = ()
    error: str | None = None


def _read_regular_bytes(path: Path) -> bytes:
    """Read an existing regular file once without following a final symlink."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("inspection resource is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1_048_576)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _read(path: Path, *, v2: bool) -> InventoryRead:
    try:
        raw = _read_regular_bytes(path)
    except FileNotFoundError:
        return InventoryRead(InspectionStatus.MISSING)
    except OSError as exc:
        return InventoryRead(InspectionStatus.UNKNOWN, error=str(exc))
    except ValueError as exc:
        return InventoryRead(InspectionStatus.FAILED, error=str(exc))
    try:
        records = parse_maintenance_inventory_v2_bytes(raw) if v2 else parse_maintenance_inventory_bytes(raw)
    except (StateError, ValueError, TypeError, KeyError) as exc:
        return InventoryRead(InspectionStatus.FAILED, error=str(exc))
    return InventoryRead(InspectionStatus.VERIFIED, records)


def _enrolled(record: InstanceInventoryRecordV2, target: TargetFilesystemIdentity | None) -> JsonValue:
    reasons = None if target is None else identity_mismatches(record, target.canonical_display, target.target_inode, target.parent_inode)
    return {
        "name": record.name,
        "instance_id": record.instance_id,
        "enrolled_path": record.target.canonical_path,
        "target_identity_matches": None if reasons is None else not reasons,
        "identity_mismatch_reasons": None if reasons is None else list(reasons),
        "next_step": TARGET_RECOVERY if reasons else None,
    }


def inventory_check(paths: ManagerPaths, tracker: InspectionEffectTracker, target: TargetFilesystemIdentity | None) -> InspectionCheck:
    """Absence of the create registry never conceals an enrolled maintenance row."""
    tracker.record_resource_read("manager_inventory")
    legacy = _read(paths.registry_path, v2=False)
    maintenance = _read(paths.maintenance_inventory_path, v2=True)
    statuses = (legacy.status, maintenance.status)
    status = next(
        (item for item in (InspectionStatus.FAILED, InspectionStatus.UNKNOWN, InspectionStatus.VERIFIED) if item in statuses),
        InspectionStatus.MISSING,
    )
    reason, summary = {
        InspectionStatus.MISSING: ("inventory_absent", "Neither Manager create registry nor maintenance inventory exists."),
        InspectionStatus.UNKNOWN: ("inventory_unavailable", "Manager inventory cannot be read completely; available records are shown."),
        InspectionStatus.FAILED: ("inventory_malformed", "Manager inventory is malformed; available records are shown."),
        InspectionStatus.VERIFIED: (None, "Manager create registry and maintenance inventory read as informational context."),
    }[status]
    observed: dict[str, JsonValue] = {
        "records": len(legacy.records) + len(maintenance.records),
        "create_registry": {"status": legacy.status.value, "records": len(legacy.records), "error": legacy.error},
        "maintenance_inventory": {"status": maintenance.status.value, "records": len(maintenance.records), "error": maintenance.error},
        "enrolled_instances": [_enrolled(record, target) for record in maintenance.records if isinstance(record, InstanceInventoryRecordV2)],
    }
    return _check("manager_inventory", "inventory", False, status, reason, summary, observed, None, "manager_inventory")
