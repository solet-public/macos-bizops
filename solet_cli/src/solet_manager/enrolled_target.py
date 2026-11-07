"""Read-only enrollment identity evidence and a terminal recovery explanation."""

from __future__ import annotations

import shlex
from pathlib import Path

from .models import InstanceInventoryRecordV2

TARGET_RECOVERY = (
    "Preserve the inventory and locate the originally enrolled directory with its recorded filesystem identity. "
    "Copying files into a new directory does not restore that identity. "
    "If the original directory is unavailable, a supported same-path rebind is needed; "
    "the Manager does not provide that repair yet. Stop retrying import, update and reconciliation."
)


def identity_mismatches(record: InstanceInventoryRecordV2, target: Path, target_inode: int, parent_inode: int) -> tuple[str, ...]:
    """Compare the existing enrollment premises; device numbers may change across boots."""
    expected = record.target
    return tuple(
        name
        for name, matches in (
            ("enrolled_path", str(target) == expected.canonical_path),
            ("target_inode", target_inode == expected.filesystem_identity.inode),
            ("parent_inode", parent_inode == expected.parent_filesystem_identity.inode),
        )
        if not matches
    )


def observe_identity_mismatches(record: InstanceInventoryRecordV2) -> tuple[str, ...]:
    """Observe the enrolled directory without running any target code."""
    target = Path(record.target.canonical_path)
    try:
        own, parent = target.stat(), target.parent.stat()
    except OSError:
        return ("target_unavailable",)
    if target.is_symlink() or not target.is_dir():
        return ("target_not_directory",)
    return identity_mismatches(record, target, own.st_ino, parent.st_ino)


def enrollment_summary(record: InstanceInventoryRecordV2) -> str:
    return f"{record.name!r} is enrolled as {record.instance_id} at {record.target.canonical_path}"


def identity_drift_message(record: InstanceInventoryRecordV2, reasons: tuple[str, ...]) -> str:
    if "target_unavailable" in reasons:
        return f"{enrollment_summary(record)}; the enrolled target identity cannot be read."
    return f"{enrollment_summary(record)}; this directory does not match the enrolled target identity ({', '.join(reasons)})."


def identity_repair(record: InstanceInventoryRecordV2) -> str:
    target = shlex.quote(record.target.canonical_path)
    channel = shlex.quote(record.channel.channel_id)
    return f"Read the enrollment with `solet-manager inspect --target {target} --channel {channel} --json`. {TARGET_RECOVERY}"
