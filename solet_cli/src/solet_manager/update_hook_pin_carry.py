"""Carry exact installer hook pins across a changed manifest (iss_c1a7df20).

The approval binds the changed paths and both digests. Durable backups precede
restoring HEAD bytes; the existing source_applying journal covers the fast-forward.
Every completion rechecks/re-pins the new HEAD before recording its local-state
revision. The plugin cache is untouched until the later cache-refresh operation.
"""

from __future__ import annotations

import hashlib
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from solet_setup_contracts.hook_interpreter_pin import HOOK_MANIFEST_PATHS, HookManifestError, instance_interpreter, pin_hook_interpreter

from .create_applied_edits import head_mode_matches
from .errors import StateError, UpdateBlockedError
from .installer_pins import BlobReader, read_target_blob, worktree_bytes
from .managed_artifact_backup import backup_exists, read_backup, restore_artifact, write_backup
from .models import InstanceInventoryRecordV2, JsonValue
from .paths import ManagerPaths
from .state_io import atomic_replace_bytes
from .update_journal import record_local_state_revision, write_update_journal

ACTION = "target.carry_hook_interpreter_pin"


@dataclass(frozen=True, slots=True)
class PinCarry:
    """One approved exact pin transition; paths outside this set remain preserved."""

    path: str
    before_sha256: str
    after_sha256: str

    def row(self) -> dict[str, JsonValue]:
        return {"path": self.path, "before_sha256": self.before_sha256, "after_sha256": self.after_sha256}


def plan_pin_carry(target: Path, pins: tuple[str, ...], transition: tuple[tuple[str, str], ...], read_candidate: BlobReader, candidate_commit: str) -> tuple[PinCarry, ...]:
    changes = {path for status, path in transition if status == "M"}
    rows: list[PinCarry] = []
    for path in sorted(changes.intersection(pins)):
        before = worktree_bytes(target, path)
        if before is None:
            raise _refused(path, "the recognized installer pin disappeared")
        after = _pinned(target, path, read_candidate(f"{candidate_commit}:{path}"))
        rows.append(PinCarry(path, _sha(before), _sha(after)))
    return tuple(rows)


def with_pins(planned: tuple[str, ...], carries: tuple[PinCarry, ...]) -> tuple[str, ...]:
    return (*planned, ACTION) if carries else planned


def pin_rows(carries: tuple[PinCarry, ...]) -> list[JsonValue]:
    return [item.row() for item in carries]


def pin_disclosure(carries: tuple[PinCarry, ...], surfaces: tuple[str, ...]) -> dict[str, JsonValue]:
    if not carries:
        return {}
    return {"hook_pin_carry": pin_rows(carries), "non_touch_surfaces": list(pin_untouched(carries, surfaces))}


def pin_untouched(carries: tuple[PinCarry, ...], surfaces: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(item for item in surfaces if not (carries and item == "tracked_local_modifications"))


def set_pins_aside(paths: ManagerPaths, record: InstanceInventoryRecordV2, operation_id: str, carries: tuple[PinCarry, ...]) -> None:
    """Back up and validate the complete batch before putting committed bytes back."""
    target = Path(record.target.canonical_path)
    prepared: list[tuple[Path, bytes, int]] = []
    for item in carries:
        backup = write_backup(paths, record.instance_id, operation_id, _backup_id(item.path, item.before_sha256), target / item.path)
        if backup.sha256 != f"sha256:{item.before_sha256}" or backup.mode is None:
            raise _refused(item.path, "changed after the approved preview")
        raw = read_target_blob(target)(f"{record.source_release.commit}:{item.path}")
        prepared.append((target / item.path, raw, backup.mode))
    for destination, raw, mode in prepared:
        atomic_replace_bytes(destination, raw, mode=mode)


def put_pins_back(paths: ManagerPaths, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue]) -> tuple[str, ...]:
    """Recover the baseline pins on resume, refused fast-forward, or abandon."""
    target = Path(record.target.canonical_path)
    operation = cast(str, journal["operation_id"])
    baseline = cast(str, cast(dict[str, JsonValue], journal["baseline"])["commit"])
    prepared: list[tuple[str, str, str]] = []
    for path, digest in _journal_digests(journal).items():
        artifact = _backup_id(path, digest)
        if not backup_exists(paths, record.instance_id, operation, artifact):
            continue
        backup = read_backup(paths, record.instance_id, operation, artifact)
        _saved_mode(target, path, backup.mode)
        current = worktree_bytes(target, path)
        if current is not None and f"sha256:{_sha(current)}" == backup.sha256:
            continue
        bare = read_target_blob(target)(f"{baseline}:{path}")
        if current != bare:
            raise _refused(path, "holds neither the saved pin nor the baseline bytes")
        prepared.append((path, artifact, f"sha256:{_sha(bare)}"))
    for _path, artifact, digest in prepared:
        restore_artifact(paths, record.instance_id, operation, artifact, expected_after_sha256=digest)
    return tuple(path for path, _, _ in prepared)


def finish_pin_carry(paths: ManagerPaths, record: InstanceInventoryRecordV2, journal_path: Path, journal: dict[str, JsonValue], executed: list[str]) -> dict[str, JsonValue]:
    """Re-pin on every re-entry, including after file writes but before the journal write."""
    if ACTION not in cast(list[str], journal["planned_actions"]):
        return journal
    target = Path(record.target.canonical_path)
    prepared = _prepare_pins(paths, record, journal)
    for path, _digest, pinned, mode in prepared:
        if worktree_bytes(target, path) != pinned:
            atomic_replace_bytes(target / path, pinned, mode=mode)
    local = cast(dict[str, JsonValue], journal["local_state"])
    revisions = cast(list[dict[str, JsonValue]], local["revisions"])
    if any(row.get("operation_id") == ACTION for row in revisions):
        return journal
    updated = _pin_revision(journal, prepared, target)
    write_update_journal(journal_path, journal, updated)
    executed.append(ACTION)
    return updated


def _prepare_pins(paths: ManagerPaths, record: InstanceInventoryRecordV2, journal: dict[str, JsonValue]) -> list[tuple[str, str, bytes, int]]:
    """Validate the entire batch before completing any interrupted pin write."""
    target = Path(record.target.canonical_path)
    operation = cast(str, journal["operation_id"])
    candidate = cast(str, cast(dict[str, JsonValue], journal["candidate"])["commit"])
    prepared: list[tuple[str, str, bytes, int]] = []
    for path, digest in _journal_digests(journal).items():
        if not backup_exists(paths, record.instance_id, operation, _backup_id(path, digest)):
            continue
        backup = read_backup(paths, record.instance_id, operation, _backup_id(path, digest))
        mode = _saved_mode(target, path, backup.mode)
        bare = read_target_blob(target)(f"{candidate}:{path}")
        pinned = _pinned(target, path, bare)
        current = worktree_bytes(target, path)
        if not head_mode_matches(target, path):
            raise _refused(path, "mode changed while the update was carrying its pin")
        if current not in (bare, pinned):
            raise _refused(path, "changed while the update was carrying its pin")
        prepared.append((path, digest, pinned, mode))
    if not prepared:
        raise StateError("the approved hook-pin carry has no durable backup")
    return prepared


def _saved_mode(target: Path, path: str, expected: int | None) -> int:
    """Preserve every permission bit, including changes Git does not record."""
    try:
        info = (target / path).lstat()
    except OSError as exc:
        raise _refused(path, "cannot validate the saved hook-pin mode") from exc
    if expected is None or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != expected:
        raise _refused(path, "permission bits changed while the update was carrying its pin")
    return expected


def _pin_revision(journal: dict[str, JsonValue], prepared: list[tuple[str, str, bytes, int]], target: Path) -> dict[str, JsonValue]:
    local = cast(dict[str, JsonValue], journal["local_state"])
    current = cast(dict[str, JsonValue], local["current"])
    moved = {path: {"path": path, "sha256": _sha(raw), "size": len(raw)} for path, _, raw, _ in prepared}
    clean = {path for path, _, raw, _ in prepared if raw == read_target_blob(target)(f"HEAD:{path}")}
    rows = cast(list[dict[str, JsonValue]], current["preserved_tracked_paths"])
    # Rebaseline this declared Manager write; do not pretend its digest was preserved.
    snapshot: dict[str, JsonValue] = {**current, "preserved_tracked_paths": [cast(JsonValue, moved.get(cast(str, row["path"]), row)) for row in rows if row["path"] not in clean]}
    revision: dict[str, JsonValue] = {"operation_id": ACTION, "paths": list[JsonValue](sorted(moved)), "before": {path: digest for path, digest, _, _ in prepared}, "after": {path: _sha(raw) for path, _, raw, _ in prepared}}
    return record_local_state_revision(journal, revision=revision, current=snapshot)


def _journal_digests(journal: dict[str, JsonValue]) -> dict[str, str]:
    if ACTION not in cast(list[str], journal["planned_actions"]):
        return {}
    baseline = cast(dict[str, JsonValue], cast(dict[str, JsonValue], journal["local_state"])["baseline"])
    rows = cast(list[dict[str, JsonValue]], baseline["preserved_tracked_paths"])
    return {row["path"]: cast(str, row["sha256"]) for row in rows if row["path"] in HOOK_MANIFEST_PATHS}


def _pinned(target: Path, path: str, raw: bytes) -> bytes:
    try:
        result = pin_hook_interpreter(raw, instance_interpreter(target))
    except HookManifestError as exc:
        raise _refused(path, f"the candidate manifest cannot be pinned: {exc}") from exc
    return raw if result is None else result


def _backup_id(path: str, digest: str) -> str:
    return f"hook_pin_carry.{path}.{digest[:16]}"


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _refused(path: str, detail: str) -> UpdateBlockedError:
    return UpdateBlockedError("hook_pin_uncarriable", f"{path}: {detail}", repair="Preserve the hook manifest and inspect the update journal; do not reset the checkout.")
