"""Recovery metadata: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
import uuid
from collections.abc import Generator, Sequence
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import cast

import custody_store as store
from recovery_git import Git
from recovery_types import _DIRECTORY, Json, Limits, RecoveryError


def content_key(value: object) -> str:
    return hashlib.sha256(store.canonical_bytes(value)).hexdigest()


def safe_path(relative: str) -> list[str]:
    parts = relative.split("/")
    if any(part in {"", ".", "..", ".git"} or "\0" in part for part in parts) or relative.startswith("/"):
        raise RecoveryError("unsafe_path", relative)
    return parts


def _json_bytes(raw: bytes) -> Json:
    value: object = json.loads(raw)
    if not isinstance(value, dict):
        raise RecoveryError("metadata_invalid")
    return cast(Json, value)


def read_json(root: Path, relative: str) -> Json:
    chunks: list[bytes] = []
    entry = store.read_entry(root, relative, object_format="sha1", digest=True, sink=chunks.append)
    if entry.mode is None:
        return {}
    if not entry.readable or entry.mode != store.REGULAR_MODE:
        raise RecoveryError("metadata_unreadable", relative)
    return _json_bytes(b"".join(chunks))


def write_all(descriptor: int, raw: bytes) -> None:
    view = memoryview(raw)
    while view:
        count = os.write(descriptor, view)
        if count <= 0:
            raise OSError(errno.EIO, "write made no progress")
        view = view[count:]


def write_json(root: Path, parts: Sequence[str], value: object) -> None:
    with ExitStack() as stack:
        parent = store.open_store_directory(root, parts[:-1], stack, create=True)
        name = ".metadata-" + uuid.uuid4().hex
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=parent)
        try:
            write_all(descriptor, store.canonical_bytes(value))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(name, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=parent)


def append_event(root: Path, parts: Sequence[str], event: object) -> None:
    with ExitStack() as stack:
        parent = store.open_store_directory(root, parts[:-1], stack, create=True)
        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        stack.callback(os.close, descriptor)
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise RecoveryError("metadata_unreadable", parts[-1])
        # One write per record; flock protects against short-write interleaving.
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        try:
            write_all(descriptor, store.canonical_bytes(event) + b"\n")
            os.fsync(descriptor)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)


def read_lines(root: Path, relative: str) -> list[Json]:
    chunks: list[bytes] = []
    entry = store.read_entry(root, relative, object_format="sha1", digest=True, sink=chunks.append)
    if entry.mode is None:
        return []
    if not entry.readable or entry.mode != store.REGULAR_MODE:
        raise RecoveryError("metadata_unreadable", relative)
    return [_json_bytes(line) for line in b"".join(chunks).splitlines() if line]


def physically_inside(directory: Path, root: Path) -> bool:
    """Physical ancestry, including symlinks/case/Unicode filesystem aliases."""
    try:
        identity = root.stat()
        resolved = directory.resolve()
        return any((info.st_dev, info.st_ino) == (identity.st_dev, identity.st_ino) for path in (resolved, *resolved.parents) if path.exists() for info in (path.stat(),))
    except OSError:
        return directory.resolve().is_relative_to(root.resolve())


def _default_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/Solet/Recovery/v1"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "Solet/Recovery/v1"


def _root_unsafe(candidate: Path, cwd: Path, deadline: float) -> bool:
    forbidden = [Path.home() / name for name in ("Documents", "Desktop", "Downloads", ".codex", ".claude", "Library/Mobile Documents", "Library/CloudStorage")]
    probe = candidate
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    repository = Git(probe, deadline).text(["rev-parse", "--show-toplevel"], allow=(0, 128))
    protected = any(physically_inside(candidate, path) if path.exists() else candidate.is_relative_to(path) for path in forbidden)
    return bool(repository) or protected


def _store_marker(descriptor: int) -> None:
    try:
        marker = os.open(".metadata_never_index", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400, dir_fd=descriptor)
    except FileExistsError:
        if not stat.S_ISREG(os.stat(".metadata_never_index", dir_fd=descriptor, follow_symlinks=False).st_mode):
            raise RecoveryError("store_marker_unsafe") from None
    else:
        os.close(marker)


def _provision_root(candidate: Path) -> None:
    with ExitStack() as stack:
        descriptor = os.open(candidate.anchor, _DIRECTORY)
        stack.callback(os.close, descriptor)
        for part in candidate.parts[1:]:
            with suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=descriptor)
            descriptor = os.open(part, _DIRECTORY, dir_fd=descriptor)
            stack.callback(os.close, descriptor)
        if os.fstat(descriptor).st_uid != os.getuid():
            raise RecoveryError("store_root_unowned")
        os.fchmod(descriptor, 0o700)
        _store_marker(descriptor)


def prepare_root(requested: Path | None, cwd: Path, deadline: float, *, read_only: bool = False) -> tuple[Path, tuple[str, ...]]:
    candidate = requested or Path(os.environ.get("SOLET_RECOVERY_ROOT", str(_default_root())))
    candidate = candidate.expanduser().resolve()
    advisory: tuple[str, ...] = ()
    if read_only:
        return candidate, advisory
    if _root_unsafe(candidate, cwd, deadline):
        candidate, advisory = _default_root().resolve(), ("recovery_root_override_ignored",)
        if _root_unsafe(candidate, cwd, deadline):
            raise RecoveryError("default_recovery_root_unsafe", "configure a root outside repositories and synced folders")
    _provision_root(candidate)
    return candidate, advisory


def load_limits(root: Path) -> tuple[Limits, tuple[str, ...]]:
    try:
        values = read_json(root, "config.json")
        defaults = asdict(Limits())
        clean: Json = {}
        for name, default in defaults.items():
            value = values.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < float("inf"):
                raise RecoveryError("config_invalid", name)
            clean[name] = int(value) if isinstance(default, int) else float(value)
        return Limits(**clean), ()
    except (ValueError, OSError, RecoveryError) as exc:
        print(f"[recovery] config unreadable; using default limits: {exc}", file=sys.stderr)
        return Limits(), ("config_defaults_used",)


@contextmanager
def folder_lock(root: Path, key: str, deadline: float) -> Generator[None]:
    with ExitStack() as stack:
        parent = store.open_store_directory(root, ("folders", key), stack, create=True)
        descriptor = os.open("lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        stack.callback(os.close, descriptor)
        until = min(deadline, time.monotonic() + 1.0)
        acquired = False
        while time.monotonic() < until:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                time.sleep(min(0.01, max(0.0, until - time.monotonic())))
        try:
            yield
        finally:
            if acquired:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def stat_path(folder: Path, path: str) -> tuple[int, ...] | None:
    try:
        info = (folder / path).lstat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode
    except FileNotFoundError:
        return None


def entry_record(layer: str, path: str, mode: int | None, oid: str | None, *, base_oid: str | None = None, index_oid: str | None = None, base_mode: int | None = None, index_mode: int | None = None, stage: int = 0, reference: str = "") -> Json:
    safe_path(path)
    return {"layer": layer, "path": path, "mode": mode, "oid": oid, "base_oid": base_oid, "index_oid": index_oid, "base_mode": base_mode, "index_mode": index_mode, "stage": stage, "reference": reference, "sha256": None, "size": 0}


def write_rule(directory: Path, path: str, data: bytes) -> None:
    parts = safe_path(path)
    with ExitStack() as stack:
        parent = store.open_store_directory(directory, parts[:-1], stack, create=True)
        descriptor = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        stack.callback(os.close, descriptor)
        write_all(descriptor, data)
