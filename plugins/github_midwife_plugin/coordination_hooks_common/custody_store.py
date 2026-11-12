"""Content-addressed custody packets, shared by lanes and working-tree recovery.

This canonical module is materialized verbatim into consumers. It has no local
imports and runs on Python 3.11. Packets contain exactly a canonical manifest
and a directory of SHA-256 payloads; publication never replaces an existing
packet. Recovery may share payload inodes through a pool. Retention decisions
and their append-only audit history belong to the caller, not this byte store.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager, nullcontext, suppress
from dataclasses import dataclass
from functools import partial
from pathlib import Path

MANIFEST_NAME = "manifest.json"
PAYLOAD_DIRECTORY = "payload"
STAGING_PREFIX = ".staging-"
REGULAR_MODE = 0o100644
EXECUTABLE_MODE = 0o100755
SYMLINK_MODE = 0o120000
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_READ_CHUNK = 1 << 20
_SHA256 = re.compile(r"[0-9a-f]{64}")
Sink = Callable[[bytes], None]


class CustodyStoreError(RuntimeError):
    """A named integrity or filesystem failure, including any retained staging."""

    def __init__(self, code: str, detail: str, incomplete: tuple[tuple[str, str], ...] = (), *, staging: str = "") -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.incomplete = incomplete
        self.staging = staging


@dataclass(frozen=True, slots=True)
class PacketWriter:
    """Injectable filesystem steps; all payload and manifest bytes use these."""

    write: Callable[[int, bytes], int] = os.write
    fsync: Callable[[int], None] = os.fsync
    rename: Callable[..., None] = os.rename
    link: Callable[..., None] = os.link


DEFAULT_WRITER = PacketWriter()


@dataclass(frozen=True, slots=True)
class LocalEntry:
    """One Git-shaped path; absent and unreadable entries are distinct."""

    mode: int | None
    sha256: str | None = None
    oid: str | None = None
    readable: bool = True
    size: int | None = None


ABSENT_ENTRY = LocalEntry(None)
UNREADABLE_ENTRY = LocalEntry(None, readable=False)


@dataclass(frozen=True, slots=True)
class Payload:
    """A payload's identities and a streaming reader that must reproduce them."""

    sha256: str
    oid: str
    read: Callable[[Sink], LocalEntry]
    mode: int | None = None

    @classmethod
    def from_bytes(cls, data: bytes, object_format: str) -> Payload:
        digest, oid = hashlib.sha256(data).hexdigest(), blob_oid(data, object_format)
        return cls(digest, oid, partial(_held, data, digest, oid))

    @classmethod
    def from_path(cls, root: Path, relative: str, object_format: str, *, sha256: str, oid: str, mode: int | None = None) -> Payload:
        return cls(sha256, oid, partial(_read_path, root, relative, object_format), mode)


@dataclass(frozen=True, slots=True)
class Publication:
    """The verified publication, with explicit copy fallback and staging evidence."""

    packet_id: str
    created: bool
    staging: str = ""
    copied_payloads: tuple[str, ...] = ()


def canonical_bytes(value: object) -> bytes:
    """The original sorted-key, ASCII-escaped, whitespace-free manifest encoding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def blob_oid(data: bytes, object_format: str) -> str:
    oid = hashlib.new(object_format, b"blob %d\0" % len(data))
    oid.update(data)
    return oid.hexdigest()


def open_store_directory(root: Path, parts: Sequence[str], stack: ExitStack, *, create: bool = False, dir_mode: int = 0o700) -> int:
    """Walk descriptors with O_NOFOLLOW; create missing parts durably when asked."""
    _require_parts(parts)
    descriptor = os.open(root, _DIRECTORY_FLAGS)
    stack.callback(os.close, descriptor)
    for part in parts:
        if create:
            with suppress(FileExistsError):
                os.mkdir(part, dir_mode, dir_fd=descriptor)
                os.fsync(descriptor)
        descriptor = _open_directory_at(descriptor, part, stack)
    return descriptor


def _open_directory_at(parent: int, name: str, stack: ExitStack) -> int:
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    except FileNotFoundError as exc:
        raise CustodyStoreError("custody_packet_missing", f"{name} does not exist in the custody store") from exc
    except OSError as exc:
        raise CustodyStoreError("custody_store_unsafe", f"{name} is not a plain directory: {exc}") from exc
    stack.callback(os.close, descriptor)
    return descriptor


def _require_parts(parts: Sequence[str]) -> None:
    if any(not part or part in {".", ".."} or "/" in part or "\0" in part for part in parts):
        raise CustodyStoreError("custody_store_unsafe", f"invalid store path components: {parts!r}")


def _listing(root: Path, parts: Sequence[str]) -> set[str]:
    with ExitStack() as stack, os.scandir(open_store_directory(root, parts, stack)) as entries:
        return {entry.name for entry in entries}


def read_verified(root: Path, parts: Sequence[str], *, packet_id: str, object_format: str) -> bytes:
    """Read the exact manifest, enforcing its packet digest and two-entry layout."""
    if not _SHA256.fullmatch(packet_id):
        raise CustodyStoreError("custody_packet_id_invalid", f"{packet_id!r} is not a packet digest")
    if _listing(root, parts) != {MANIFEST_NAME, PAYLOAD_DIRECTORY}:
        raise CustodyStoreError("custody_packet_corrupt", f"packet {packet_id} does not hold exactly its manifest and payload")
    return read_payload(root, "/".join((*parts, MANIFEST_NAME)), object_format, (packet_id, None))


def read_payload(root: Path, relative: str, object_format: str, expected: tuple[str, str | None]) -> bytes:
    """Read a regular stored file only when both its byte identities agree."""
    chunks: list[bytes] = []
    entry = read_entry(root, relative, object_format=object_format, digest=True, sink=chunks.append)
    digest, oid = expected
    if entry.mode != REGULAR_MODE or entry.sha256 != digest or oid not in {None, entry.oid}:
        raise CustodyStoreError("custody_packet_corrupt", f"{relative} is not the exact regular file its packet names")
    return b"".join(chunks)


def verify_payload_set(root: Path, parts: Sequence[str], expected: Mapping[str, tuple[str, int]], *, object_format: str) -> None:
    """Require exactly the manifest's payload set and verify every regular file."""
    names = _listing(root, (*parts, PAYLOAD_DIRECTORY))
    expected_names = set(expected)
    if names != expected_names:
        raise CustodyStoreError("custody_packet_corrupt", f"payload set disagrees: missing {sorted(expected.keys() - names)}, unexpected {sorted(names - expected.keys())}")
    for digest, (oid, _) in expected.items():
        entry = read_entry(root, "/".join((*parts, PAYLOAD_DIRECTORY, digest)), object_format=object_format, digest=True)
        if (entry.mode, entry.sha256, entry.oid) != (REGULAR_MODE, digest, oid):
            raise CustodyStoreError("custody_packet_corrupt", f"payload {digest} is not the exact regular file its packet names")


def _held(data: bytes, digest: str, oid: str, sink: Sink) -> LocalEntry:
    sink(data)
    return LocalEntry(REGULAR_MODE, digest, oid, size=len(data))


def _read_path(root: Path, relative: str, object_format: str, sink: Sink) -> LocalEntry:
    return read_entry(root, relative, object_format=object_format, digest=True, sink=sink)


def _write_all(writer: PacketWriter, descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = writer.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "payload write made no progress")
        offset += written


def _write_file(directory: int, name: str, writer: PacketWriter, read: Callable[[Sink], LocalEntry], file_mode: int) -> LocalEntry:
    descriptor = os.open(name, _CREATE_FLAGS, file_mode, dir_fd=directory)
    try:
        os.fchmod(descriptor, file_mode)
        entry = read(partial(_write_all, writer, descriptor))
        writer.fsync(descriptor)
    finally:
        os.close(descriptor)
    return entry


def _copy_payload(directory: int, payload: Payload, writer: PacketWriter, file_mode: int) -> None:
    entry = _write_file(directory, payload.sha256, writer, payload.read, file_mode)
    if not entry.readable or (entry.sha256, entry.oid) != (payload.sha256, payload.oid) or payload.mode not in {None, entry.mode}:
        raise CustodyStoreError("custody_source_changed", f"payload {payload.sha256} no longer matches the inventory; nothing was published")


@contextmanager
def _locked(descriptor: int) -> Generator[None]:
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


def publish_packet(
    root: Path,
    parts: Sequence[str],
    manifest_bytes: bytes,
    payloads: Mapping[str, Payload],
    writer: PacketWriter = DEFAULT_WRITER,
    *,
    dir_mode: int = 0o700,
    file_mode: int = 0o400,
    pool: Sequence[str] | None = None,
    validate: Callable[[str], None] | None = None,
    retain_failed_staging: bool = False,
) -> Publication:
    """Stage, verify and atomically publish; default failures roll back and clean.

    ``validate`` receives the staging component before promotion. Lanes use it
    for source/history/index checks and explicitly retain failed staging. Pool
    operations serialize with lifecycle/sweep on the root descriptor; the
    original unpooled concurrent lane publication remains rename-arbitrated.
    """
    with ExitStack() as stack:
        root_fd = open_store_directory(root, (), stack)
        with _locked(root_fd) if pool is not None else nullcontext():
            publication = _Publication(root, tuple(parts), manifest_bytes, payloads, writer, dir_mode, file_mode, pool)
            return publication.run(stack, validate, retain_failed_staging)


class _Publication:
    """One publication's owned temporary names; rollback touches only their inodes."""

    def __init__(self, root: Path, parts: tuple[str, ...], raw: bytes, payloads: Mapping[str, Payload], writer: PacketWriter, dir_mode: int, file_mode: int, pool: Sequence[str] | None) -> None:
        self.root, self.parts, self.raw, self.payloads = root, parts, raw, payloads
        self.writer, self.dir_mode, self.file_mode, self.pool = writer, dir_mode, file_mode, pool
        self.packet_id = hashlib.sha256(raw).hexdigest()
        self.staging = f"{STAGING_PREFIX}{self.packet_id[:16]}-{uuid.uuid4().hex}"
        self.pool_owned: list[tuple[int, str, str, tuple[int, int]]] = []
        self.copied: list[str] = []

    def run(self, stack: ExitStack, validate: Callable[[str], None] | None, retain: bool) -> Publication:
        store = open_store_directory(self.root, self.parts, stack, create=True, dir_mode=self.dir_mode)
        os.mkdir(self.staging, self.dir_mode, dir_fd=store)
        identity = _identity(store, self.staging)
        try:
            self._stage(store, stack)
            if validate is not None:
                validate(self.staging)
            created = self._promote(store)
        except Exception as exc:
            self._rollback(store, identity, retain)
            staged = "/".join((*self.parts, self.staging)) if retain else ""
            if isinstance(exc, CustodyStoreError):
                exc.staging = staged
                raise
            if isinstance(exc, OSError):
                raise CustodyStoreError("custody_publication_failed", str(exc), staging=staged) from exc
            raise
        return Publication(self.packet_id, created, copied_payloads=tuple(self.copied))

    def _stage(self, store: int, stack: ExitStack) -> None:
        directory = _open_directory_at(store, self.staging, stack)
        os.mkdir(PAYLOAD_DIRECTORY, self.dir_mode, dir_fd=directory)
        payload_directory = _open_directory_at(directory, PAYLOAD_DIRECTORY, stack)
        pool_fd = open_store_directory(self.root, self.pool, stack, create=True, dir_mode=self.dir_mode) if self.pool is not None else None
        for digest, payload in self.payloads.items():
            if digest != payload.sha256 or not _SHA256.fullmatch(digest):
                raise CustodyStoreError("custody_packet_corrupt", f"invalid payload key {digest!r}")
            if pool_fd is None:
                _copy_payload(payload_directory, payload, self.writer, self.file_mode)
            else:
                self._pooled_copy(pool_fd, payload_directory, payload)
        self.writer.fsync(payload_directory)
        _write_file(directory, MANIFEST_NAME, self.writer, partial(_held, self.raw, "", ""), self.file_mode)
        self.writer.fsync(directory)
        self._verify(self.staging)

    def _verify(self, directory: str) -> None:
        # OID length carries the Git object format, even for an empty packet.
        object_format = "sha256" if any(len(payload.oid) == 64 for payload in self.payloads.values()) else "sha1"
        parts = (*self.parts, directory)
        read_verified(self.root, parts, packet_id=self.packet_id, object_format=object_format)
        verify_payload_set(self.root, parts, {digest: (payload.oid, 0) for digest, payload in self.payloads.items()}, object_format=object_format)

    def _pooled_copy(self, pool_fd: int, destination: int, payload: Payload) -> None:
        digest = payload.sha256
        try:
            os.stat(digest, dir_fd=pool_fd, follow_symlinks=False)
        except FileNotFoundError:
            temporary = f"{STAGING_PREFIX}{uuid.uuid4().hex}"
            # Record the name before writing: a write/fsync fault must clean it too.
            self.pool_owned.append((pool_fd, temporary, digest, (0, 0)))
            entry = _write_file(pool_fd, temporary, self.writer, payload.read, self.file_mode)
            self.pool_owned[-1] = (pool_fd, temporary, digest, _identity(pool_fd, temporary))
            if not entry.readable or (entry.sha256, entry.oid) != (digest, payload.oid) or payload.mode not in {None, entry.mode}:
                raise CustodyStoreError("custody_source_changed", f"payload {digest} changed before pooling") from None
            self.writer.rename(temporary, digest, src_dir_fd=pool_fd, dst_dir_fd=pool_fd)
            self.writer.fsync(pool_fd)
        object_format = "sha256" if len(payload.oid) == 64 else "sha1"
        entry = _regular_at(pool_fd, digest, object_format)
        if (entry.mode, entry.sha256, entry.oid) != (REGULAR_MODE, digest, payload.oid):
            raise CustodyStoreError("custody_packet_corrupt", f"pool payload {digest} is corrupt")
        try:
            self.writer.link(digest, digest, src_dir_fd=pool_fd, dst_dir_fd=destination, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in {errno.EPERM, errno.EXDEV}:
                raise
            _copy_payload(destination, payload, self.writer, self.file_mode)
            self.copied.append(digest)

    def _promote(self, store: int) -> bool:
        if _stat_at(store, self.packet_id) is not None:
            self._verify(self.packet_id)
            _remove_tree(store, self.staging)
            return False
        try:
            self.writer.rename(self.staging, self.packet_id, src_dir_fd=store, dst_dir_fd=store)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            self._verify(self.packet_id)
            _remove_tree(store, self.staging)
            return False
        self.writer.fsync(store)
        return True

    def _rollback(self, store: int, identity: tuple[int, int], retain: bool) -> None:
        for name in (self.staging, self.packet_id):
            if _identity(store, name) != identity:
                continue
            if retain:
                if name != self.staging:
                    os.rename(name, self.staging, src_dir_fd=store, dst_dir_fd=store)
            else:
                _remove_tree(store, name)
        for directory, temporary, digest, owned in self.pool_owned:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            info = _stat_at(directory, digest)
            if info is not None and (info.st_dev, info.st_ino) == owned and info.st_nlink == 1:
                os.unlink(digest, dir_fd=directory)


def _regular_at(directory: int, name: str, object_format: str) -> LocalEntry:
    with ExitStack() as stack:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return UNREADABLE_ENTRY
        return _regular_entry(name, directory, info, REGULAR_MODE, object_format, stack, None)


def _stat_at(directory: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _identity(directory: int, name: str) -> tuple[int, int]:
    info = _stat_at(directory, name)
    return (info.st_dev, info.st_ino) if info is not None else (0, 0)


def _remove_tree(parent: int, name: str) -> None:
    """Remove one owned tree through descriptors, never following an entry's link."""
    with ExitStack() as stack:
        directory = _open_directory_at(parent, name, stack)
        for child in os.listdir(directory):
            info = os.stat(child, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_tree(directory, child)
            else:
                os.unlink(child, dir_fd=directory)
    os.rmdir(name, dir_fd=parent)


@dataclass(frozen=True, slots=True)
class PacketObservation:
    """A current-state read token; inode/ctime prevent an ABA stale transition."""

    parts: tuple[str, ...]
    state: str
    identity: tuple[int, int, int]


def observe_packet(root: Path, parts: Sequence[str], *, trash_parts: Sequence[str] = ("trash",)) -> PacketObservation:
    """Read present/trashed/absent state and its physical identity without mutation."""
    _require_parts(parts)
    _require_parts(trash_parts)
    if not parts or not _SHA256.fullmatch(parts[-1]):
        raise CustodyStoreError("custody_packet_id_invalid", f"invalid packet path {parts!r}")
    with ExitStack() as stack:
        descriptor = open_store_directory(root, (), stack)
        with _locked(descriptor):
            return _observe(root, tuple(parts), tuple(trash_parts))


def _directory_observation(root: Path, parts: tuple[str, ...]) -> tuple[int, int, int] | None:
    try:
        with ExitStack() as stack:
            info = os.fstat(open_store_directory(root, parts, stack))
            return info.st_dev, info.st_ino, info.st_ctime_ns
    except CustodyStoreError as exc:
        if exc.code != "custody_packet_missing":
            raise
        return None


def _observe(root: Path, parts: tuple[str, ...], trash_parts: tuple[str, ...]) -> PacketObservation:
    present = _directory_observation(root, parts)
    trashed = _directory_observation(root, (*trash_parts, parts[-1]))
    if present is not None and trashed is not None:
        raise CustodyStoreError("recovery_stale_state", "packet is both present and trashed; read its state before retrying")
    return PacketObservation(parts, "present" if present else "trashed" if trashed else "absent", present or trashed or (0, 0, 0))


def trash(root: Path, parts: Sequence[str], *, expected: PacketObservation, trash_parts: Sequence[str] = ("trash",)) -> PacketObservation:
    """Retire a currently observed present packet; the caller owns grace policy."""
    return _transition(root, tuple(parts), tuple(trash_parts), expected, "present", "trash")


def untrash(root: Path, parts: Sequence[str], *, expected: PacketObservation, trash_parts: Sequence[str] = ("trash",)) -> PacketObservation:
    """Reverse retirement only while the observed trashed packet is still current."""
    return _transition(root, tuple(parts), tuple(trash_parts), expected, "trashed", "untrash")


def purge(root: Path, parts: Sequence[str], *, expected: PacketObservation, trash_parts: Sequence[str] = ("trash",)) -> PacketObservation:
    """Terminally remove the exact observed trashed packet, never a replacement."""
    return _transition(root, tuple(parts), tuple(trash_parts), expected, "trashed", "purge")


def _transition(root: Path, parts: tuple[str, ...], trash_parts: tuple[str, ...], expected: PacketObservation, required: str, action: str) -> PacketObservation:
    _require_parts(parts)
    _require_parts(trash_parts)
    if not parts or not _SHA256.fullmatch(parts[-1]):
        raise CustodyStoreError("custody_packet_id_invalid", f"invalid packet path {parts!r}")
    with ExitStack() as stack:
        descriptor = open_store_directory(root, (), stack)
        with _locked(descriptor):
            current = _observe(root, parts, trash_parts)
            if expected != current or current.state != required:
                raise CustodyStoreError("recovery_stale_state", f"packet is now {current.state} with identity {current.identity}; reread before {action}")
            _apply_transition(root, parts, trash_parts, action, stack)
            return _observe(root, parts, trash_parts)


def _apply_transition(root: Path, parts: tuple[str, ...], trash_parts: tuple[str, ...], action: str, stack: ExitStack) -> None:
    live = open_store_directory(root, parts[:-1], stack, create=True)
    retired = open_store_directory(root, trash_parts, stack, create=True)
    name = parts[-1]
    if action == "purge":
        _remove_tree(retired, name)
    else:
        source, destination = (live, retired) if action == "trash" else (retired, live)
        os.rename(name, name, src_dir_fd=source, dst_dir_fd=destination)
    os.fsync(live)
    os.fsync(retired)


def sweep_pool(root: Path, pool: Sequence[str] = ("pool",)) -> tuple[str, ...]:
    """Remove only regular content files with no packet/staging hard links left."""
    removed: list[str] = []
    with ExitStack() as stack:
        descriptor = open_store_directory(root, (), stack)
        with _locked(descriptor):
            directory = open_store_directory(root, pool, stack)
            for name in os.listdir(directory):
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if _SHA256.fullmatch(name) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    os.unlink(name, dir_fd=directory)
                    removed.append(name)
            os.fsync(directory)
    return tuple(sorted(removed))


def read_entry(
    root: Path,
    relative: str,
    *,
    object_format: str,
    digest: bool,
    sink: Callable[[bytes], None] | None = None,
) -> LocalEntry:
    """Read one path beneath ``root`` as Git records it, following no symlink.

    A parent that is missing, a file or a symlink means Git sees no entry here,
    so the path reads as absent.  Anything else Git cannot record as a blob is
    unreadable, and a file that changes size while it is hashed is unreadable.
    With ``digest``, ``sink`` receives the raw bytes or link text as they are
    hashed; a caller must discard them unless the returned entry is readable.
    """
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return UNREADABLE_ENTRY
    with ExitStack() as stack:
        located = _stat_beneath(root, parts, stack)
        if isinstance(located, LocalEntry):
            return located
        directory, info = located
        if stat.S_ISLNK(info.st_mode):
            return _link_entry(parts[-1], directory, object_format=object_format, digest=digest, sink=sink)
        if not stat.S_ISREG(info.st_mode):
            return UNREADABLE_ENTRY
        mode = EXECUTABLE_MODE if info.st_mode & stat.S_IXUSR else REGULAR_MODE
        if not digest:
            return LocalEntry(mode)
        return _regular_entry(parts[-1], directory, info, mode, object_format, stack, sink)


def _stat_beneath(root: Path, parts: list[str], stack: ExitStack) -> tuple[int, os.stat_result] | LocalEntry:
    """Open each parent with no-follow, then lstat the leaf in its directory."""
    try:
        directory = os.open(root, _DIRECTORY_FLAGS)
        stack.callback(os.close, directory)
        for part in parts[:-1]:
            directory = os.open(part, _DIRECTORY_FLAGS, dir_fd=directory)
            stack.callback(os.close, directory)
        return directory, os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
    except (FileNotFoundError, NotADirectoryError):
        return ABSENT_ENTRY
    except OSError as exc:
        return ABSENT_ENTRY if exc.errno == errno.ELOOP else UNREADABLE_ENTRY


def _link_entry(
    name: str,
    directory: int,
    *,
    object_format: str,
    digest: bool,
    sink: Callable[[bytes], None] | None,
) -> LocalEntry:
    """A symlink's blob is its uninterpreted target, never what it points at."""
    if not digest:
        return LocalEntry(SYMLINK_MODE)
    try:
        target = os.fsencode(os.readlink(name, dir_fd=directory))
    except OSError:
        return UNREADABLE_ENTRY
    oid = hashlib.new(object_format, b"blob %d\0" % len(target))
    oid.update(target)
    if sink is not None:
        sink(target)
    return LocalEntry(SYMLINK_MODE, hashlib.sha256(target).hexdigest(), oid.hexdigest(), size=len(target))


def _regular_entry(
    name: str,
    directory: int,
    info: os.stat_result,
    mode: int,
    object_format: str,
    stack: ExitStack,
    sink: Callable[[bytes], None] | None,
) -> LocalEntry:
    """Hash raw bytes in Git's blob framing and in SHA-256, with no filter applied."""
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    except OSError:
        return UNREADABLE_ENTRY
    stack.callback(os.close, descriptor)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
        return UNREADABLE_ENTRY
    sha256 = hashlib.sha256()
    oid = hashlib.new(object_format, b"blob %d\0" % opened.st_size)
    consumed = 0
    while chunk := os.read(descriptor, _READ_CHUNK):
        consumed += len(chunk)
        sha256.update(chunk)
        oid.update(chunk)
        if sink is not None:
            sink(chunk)
    if consumed != opened.st_size:
        return UNREADABLE_ENTRY
    return LocalEntry(mode, sha256.hexdigest(), oid.hexdigest(), size=consumed)
