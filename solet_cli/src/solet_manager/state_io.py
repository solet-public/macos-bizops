"""Secure atomic JSON state and advisory instance locks."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO

from .errors import StateError
from .models import JsonValue
from .private_json import load_json_object

__all__ = [
    "atomic_remove",
    "atomic_replace_bytes",
    "atomic_write_json",
    "ensure_private_directory",
    "instance_lock",
    "load_json_object",
]


def atomic_remove(path: Path) -> None:
    """Remove one regular file and durably record the parent-directory update."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise StateError(f"reconciliation target is not a regular file: {path}")
    path.unlink()
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_replace_bytes(path: Path, value: bytes, *, mode: int) -> None:
    """Atomically replace one already-approved target file and fsync its parent.

    This intentionally does not create a parent or apply private-state checks:
    reconciliation uses it only for an already validated target contract file.
    """

    if not path.parent.is_dir() or path.parent.is_symlink():
        raise StateError(f"target parent is not a real directory: {path.parent}")
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        os.chmod(path, mode)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def ensure_private_directory(path: Path) -> None:
    """Create a user-owned 0700 directory and refuse unsafe existing state."""

    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
        raise StateError(f"state directory is not a real directory: {path}")
    if info.st_uid != os.getuid():
        raise StateError(f"state directory is not owned by the current user: {path}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise StateError(f"state directory permissions are broader than 0700: {path}")


def atomic_write_json(path: Path, value: dict[str, JsonValue]) -> None:
    """Write mode-0600 JSON with file and directory durability."""

    ensure_private_directory(path.parent)
    temporary = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@contextmanager
def instance_lock(path: Path, *, create: bool = True) -> Generator[IO[str] | None]:
    """Serialize mutation for one normalized instance name.

    A dry-run passes ``create=False``. If no prior lock exists, it performs no
    write and yields ``None`` because a read-only preview has no mutation to
    serialize.
    """

    if not create and not path.exists():
        yield None
        return
    ensure_private_directory(path.parent)
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o600)
    os.chmod(path, 0o600)
    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield handle
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()
