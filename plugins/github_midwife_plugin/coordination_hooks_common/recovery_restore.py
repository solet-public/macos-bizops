"""Recovery restore: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.

Existing targets are atomically parked in a private sibling directory, then
inspected and replaced only through no-replace publication. Parked inodes are
retained even on success: an already-open writer may still change that inode.
CLI outcomes and append-only restore history expose preserved_original paths.
Users may inspect them and explicitly remove them after writers have closed;
no automatic cleanup discards these possible newer edits.
"""

from __future__ import annotations

import hashlib
import os
import stat
import time
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path

import custody_store as store
from recovery_git import Git, index_info, objects_exist, quote_path
from recovery_metadata import safe_path, write_all
from recovery_packets import Recovery
from recovery_rebuild import rebuild
from recovery_types import Json, RecoveryError


def _restore_entry(recovery: Recovery, parts: tuple[str, ...], manifest: Json, folder: Path, entry: Json, empty: bool) -> Json:
    path = entry["path"]
    if entry["mode"] is None:
        return {"path": path, "outcome": "saved deletion; nothing deleted"}
    if entry["mode"] == 0o160000:
        return {"path": path, "outcome": "submodule pointer recorded; contents not restored"}
    data = recovery.payload(parts, manifest, entry)
    try:
        return {"path": path, **_restore_path(folder, entry, data, manifest["binding"]["object_format"], empty=empty)}
    except (OSError, RecoveryError, store.CustodyStoreError) as exc:
        destination = saved_copy(recovery, parts[-1], entry, data)
        result = {"path": path, "outcome": "kept your newer file or unsafe parent; saved copy", "saved_copy": str(destination), "detail": str(exc)}
        if isinstance(exc, _RestoreConflictError):
            result["preserved_original"] = str(folder.joinpath(*safe_path(path)[:-1], exc.preserved))
        return result


def _stage_entries(recovery: Recovery, git: Git, parts: tuple[str, ...], manifest: Json) -> list[Json]:
    indexed = [entry for entry in manifest["entries"] if entry["layer"] == "index"]
    try:
        write_blobs(recovery, git, parts, manifest, indexed)
        git.read(["update-index", "-z", "--index-info"], request=index_info(indexed, manifest["binding"]["object_format"]))
        return [{"path": entry["path"], "outcome": "index restored", "stage": entry["stage"]} for entry in indexed]
    except (OSError, RecoveryError) as exc:
        return [{"path": "index", "outcome": f"index unchanged or partially restored: {exc}"}]


def _restore_refs(recovery: Recovery, git: Git, parts: tuple[str, ...], manifest: Json) -> list[Json]:
    outcomes: list[Json] = []
    for reference in manifest["refs"]:
        try:
            outcomes.append(restore_ref(recovery, git, parts, manifest, reference))
        except (OSError, RecoveryError) as exc:
            outcomes.append({"path": reference["name"], "outcome": f"reference not restored: {exc}; saved paths remain available"})
    return outcomes


def restore(recovery: Recovery, save_id: str, *, into: Path | None = None, stage: bool = False, refs: bool = False) -> list[Json]:
    parts, manifest, _state = recovery.find(save_id)
    folder = into or Path(manifest["binding"]["folder"])
    if into is not None:
        into.mkdir(mode=0o700, parents=True, exist_ok=True)
        if any(into.iterdir()):
            raise RecoveryError("restore_into_not_empty")
    entries = _view_entries(manifest)
    outcomes = [_restore_entry(recovery, parts, manifest, folder, entry, into is not None) for entry in entries.values()]
    git = Git(Path(manifest["binding"]["folder"]), time.monotonic() + 60.0)
    if stage:
        outcomes.extend(_stage_entries(recovery, git, parts, manifest))
    if refs:
        outcomes.extend(_restore_refs(recovery, git, parts, manifest))
    recovery.log("restore", parts[-1], outcomes=outcomes, into=str(into) if into else "", stage=stage, refs=refs)
    return outcomes


def saved_copy(recovery: Recovery, save_id: str, entry: Json, data: bytes, *, prefix: str = "") -> Path:
    relative = "/".join(part for part in (prefix, entry["path"]) if part)
    root = recovery.root / "restored" / save_id
    with ExitStack() as stack:
        store.open_store_directory(recovery.root, ("restored", save_id), stack, create=True)
    copy = {**entry, "path": relative}
    try:
        _restore_path(root, copy, data, "sha1", empty=True, private=True)
    except (OSError, RecoveryError, store.CustodyStoreError):
        relative += "." + uuid.uuid4().hex
        _restore_path(root, {**copy, "path": relative}, data, "sha1", empty=True, private=True)
    return root / relative


def write_blobs(recovery: Recovery, git: Git, parts: tuple[str, ...], manifest: Json, entries: list[Json]) -> None:
    blobs = {entry["sha256"]: entry for entry in entries if entry["sha256"]}
    if not blobs:
        return
    for entry in blobs.values():
        recovery.payload(parts, manifest, entry)
    request = b"".join(quote_path(recovery.root.joinpath(*parts, "payload", digest)) + b"\n" for digest in blobs)
    output = git.read(["hash-object", "-w", "--no-filters", "--stdin-paths"], request=request).splitlines()
    if output != [entry["oid"].encode("ascii") for entry in blobs.values()]:
        raise RecoveryError("restore_blob_identity_mismatch")


def _fallback_ref(recovery: Recovery, parts: tuple[str, ...], manifest: Json, reference: Json, entries: list[Json]) -> Json:
    copies = [str(saved_copy(recovery, parts[-1], entry, recovery.payload(parts, manifest, entry), prefix=f"refs/{reference['tip']}/{entry['layer']}")) for entry in entries if entry["sha256"]]
    return {"path": reference["name"], "outcome": "base commit gone; saved paths copied", "saved_copies": copies}


def _record_ref(git: Git, reference: Json, tip: str, save_id: str) -> str:
    if reference["kind"] == "stash":
        git.read(["stash", "store", "-m", reference["message"], tip])
        return reference["name"]
    name = reference["name"]
    if git.text(["rev-parse", "--verify", "-q", f"refs/heads/{name}"], allow=(0, 1, 128)):
        name += f"-recovered-{save_id[:8]}"
    git.read(["branch", "--", name, tip])
    return name


def restore_ref(recovery: Recovery, git: Git, parts: tuple[str, ...], manifest: Json, reference: Json) -> Json:
    entries = [entry for entry in manifest["entries"] if entry["reference"] == reference["tip"]]
    ids = list(dict.fromkeys([reference["tip"], *reference["parents"], *(entry["oid"] for entry in entries if entry["oid"])]))
    existing = objects_exist(git, ids)
    tip = reference["tip"]
    if not existing:
        if _base_missing(git, reference):
            return _fallback_ref(recovery, parts, manifest, reference, entries)
        write_blobs(recovery, git, parts, manifest, entries)
        tip = rebuild(recovery, git, reference, entries)
    name = _record_ref(git, reference, tip, parts[-1])
    return {"path": name, "outcome": "reference restored exactly" if existing else "reference rebuilt from visible saved paths", "oid": tip}


def _leaf(parent: int, name: str, object_format: str) -> tuple[store.LocalEntry, tuple[int, ...] | None]:
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return store.ABSENT_ENTRY, None
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    if stat.S_ISLNK(info.st_mode):
        data = os.fsencode(os.readlink(name, dir_fd=parent))
        mode = store.SYMLINK_MODE
    elif stat.S_ISREG(info.st_mode):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != identity[:2]:
                raise RecoveryError("restore_target_changed")
            chunks: list[bytes] = []
            while chunk := os.read(descriptor, 1 << 20):
                chunks.append(chunk)
            data = b"".join(chunks)
            after = os.fstat(descriptor)
            if identity != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise RecoveryError("restore_target_changed")
        finally:
            os.close(descriptor)
        mode = store.EXECUTABLE_MODE if info.st_mode & stat.S_IXUSR else store.REGULAR_MODE
    else:
        raise RecoveryError("restore_target_special")
    return store.LocalEntry(mode, hashlib.sha256(data).hexdigest(), store.blob_oid(data, object_format), size=len(data)), identity


def _write_restore(parent: int, temporary: str, entry: Json, data: bytes, private: bool) -> None:
    if entry["mode"] == store.SYMLINK_MODE:
        os.symlink(os.fsdecode(data), temporary, dir_fd=parent)
        return
    executable = entry["mode"] == store.EXECUTABLE_MODE
    modes = {(False, False): 0o644, (True, False): 0o755, (False, True): 0o600, (True, True): 0o700}
    mode = modes[(executable, private)]
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
    try:
        write_all(descriptor, data)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class _RestoreConflictError(RecoveryError):
    """A parked original remains readable after a conflicting publication."""

    def __init__(self, preserved: str, cause: BaseException) -> None:
        super().__init__("restore_target_changed", str(cause))
        self.preserved = preserved


def _put_back(parent: int, parked: int, name: str) -> None:
    # Never replace a path recreated by another writer. The original inode
    # stays linked in the private parking directory in either case.
    with suppress(OSError):
        os.link("original", name, src_dir_fd=parked, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)


def _park_target(parent: int, name: str) -> tuple[str, int]:
    directory = ".recovery-displaced-" + uuid.uuid4().hex
    os.mkdir(directory, 0o700, dir_fd=parent)
    try:
        parked = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except BaseException:
        with suppress(OSError):
            os.rmdir(directory, dir_fd=parent)
        raise
    moved = False
    try:
        os.rename(name, "original", src_dir_fd=parent, dst_dir_fd=parked)
        moved = True
        os.fsync(parked)
        os.fsync(parent)
    except BaseException as exc:
        if moved:
            _put_back(parent, parked, name)
        os.close(parked)
        if moved and isinstance(exc, (OSError, RecoveryError)):
            raise _RestoreConflictError(directory + "/original", exc) from exc
        with suppress(OSError):
            os.rmdir(directory, dir_fd=parent)
        raise
    return directory, parked


def _publish_existing(parent: int, temporary: str, name: str, verified: tuple[store.LocalEntry, tuple[int, ...]], object_format: str) -> str:
    directory, parked = _park_target(parent, name)
    try:
        moved, identity = _leaf(parked, "original", object_format)
        # Rename itself may change ctime. Compare the inode, size, mtime,
        # Git mode and content digest of the entry actually moved instead.
        if identity is None or identity[:4] != verified[1][:4] or moved != verified[0]:
            raise RecoveryError("restore_target_changed_after_verification")
        os.link(temporary, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.fsync(parent)
    except BaseException as exc:
        _put_back(parent, parked, name)
        if isinstance(exc, (OSError, RecoveryError)):
            raise _RestoreConflictError(directory + "/original", exc) from exc
        raise
    finally:
        os.close(parked)
    return directory + "/original"


def _restore_path(root: Path, entry: Json, data: bytes, object_format: str, *, empty: bool = False, private: bool = False) -> Json:
    parts = safe_path(entry["path"])
    with ExitStack() as stack:
        parent = store.open_store_directory(root, parts[:-1], stack, create=True)
        current, identity = _leaf(parent, parts[-1], object_format)
        if current.sha256 == hashlib.sha256(data).hexdigest() and current.mode == entry["mode"]:
            return {"outcome": "already restored"}
        known = {(entry["base_mode"], entry["base_oid"]), (entry["index_mode"], entry["index_oid"])}
        if current.mode is not None and (empty or (current.mode, current.oid) not in known):
            raise RecoveryError("restore_target_newer_edit")
        temporary = ".recovery-" + uuid.uuid4().hex
        result: Json = {"outcome": "restored"}
        try:
            _write_restore(parent, temporary, entry, data, private)
            latest, latest_identity = _leaf(parent, parts[-1], object_format)
            if latest_identity != identity:
                raise RecoveryError("restore_target_changed")
            if identity is None:
                os.link(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            else:
                preserved = _publish_existing(parent, temporary, parts[-1], (latest, identity), object_format)
                result["preserved_original"] = str(root.joinpath(*parts[:-1], preserved))
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)
    return result


def _view_entries(manifest: Json) -> dict[str, Json]:
    entries: dict[str, Json] = {}
    for layer in ("index", "worktree"):
        entries.update({entry["path"]: entry for entry in manifest["entries"] if entry["layer"] == layer and not entry["stage"]})
    return entries


def _base_missing(git: Git, reference: Json) -> bool:
    base = reference["base"]
    if not base:
        return reference["kind"] == "stash"
    return not objects_exist(git, [base])
