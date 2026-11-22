"""Recovery content: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import hashlib
import stat
import time

import custody_store as store
from command_walk import Discard
from recovery_git import Git, object_sizes
from recovery_metadata import entry_record, safe_path, stat_path
from recovery_packets import Recovery
from recovery_types import CaptureContent, HookEvent, Json, RecoveryError


class ContentCapture:
    """ContentCapture: one responsibility in the shared recovery implementation."""

    def __init__(self, recovery: Recovery, event: HookEvent, discard: Discard, trigger: str, deadline: float) -> None:
        self.recovery, self.event, self.discard, self.trigger = recovery, event, discard, trigger
        self.folder = discard.folder.resolve()
        self.content = CaptureContent()
        self.git = Git(self.folder, deadline)
        self.entries: list[Json] = []
        self.refs: list[Json] = []
        self.unsaved: list[Json] = []
        self.counts: dict[str, int] = {}
        self.advisories: list[str] = list(discard.advisories)
        self.cache: Json = {}
        self.object_format = "sha1"
        self.head: dict[str, tuple[int, str]] = {}
        self.index: dict[tuple[str, int], tuple[int, str]] = {}
        self.folder_key = ""
        self.common = ""

    def skip(self, path: str, reason: str, size: int = 0) -> None:
        self.counts[reason] = self.counts.get(reason, 0) + 1
        if len(self.unsaved) < 200:
            self.unsaved.append({"path": path, "reason": reason, "size": size})

    def pooled(self, digest: str) -> bool:
        return bool(digest) and (digest in self.content.payloads or (self.recovery.root / "pool" / digest).is_file())

    def stop(self, path: str, reason: str, size: int) -> bool:
        self.content.stopped = reason
        self.skip(path, reason, size)
        return False

    def budget(self, path: str, size: int, digest: str = "", *, check_bytes: bool = True) -> bool:
        limits = self.recovery.limits
        if self.content.stopped:
            return self.stop(path, self.content.stopped, size)
        if time.monotonic() >= self.git.deadline:
            return self.stop(path, "deadline", size)
        if len(self.entries) >= limits.cap_files:
            return self.stop(path, "cap_files", size)
        if size > limits.cap_file_size:
            self.skip(path, "cap_file_size", size)
            return False
        if check_bytes and not self.pooled(digest) and self.content.new_bytes + size > limits.cap_bytes:
            return self.stop(path, "cap_bytes", size)
        return True

    def _identity(self, data: bytes | None, cached: Json | None) -> tuple[str, str, int]:
        if data is not None:
            return hashlib.sha256(data).hexdigest(), store.blob_oid(data, self.object_format), len(data)
        if cached:
            return cached["sha256"], cached["oid"], int(cached["size"])
        return "", "", 0

    def _payload(self, digest: str, oid: str, data: bytes | None, size: int) -> None:
        if digest in self.content.payloads:
            return
        if self.pooled(digest):
            payload = store.Payload.from_path(self.recovery.root, f"pool/{digest}", self.object_format, sha256=digest, oid=oid)
        else:
            assert data is not None
            payload = store.Payload.from_bytes(data, self.object_format)
            self.content.new_bytes += len(data)
        self.content.payloads[digest] = payload
        self.content.objects[oid] = {"sha256": digest, "oid": oid, "size": size}

    def add(self, entry: Json, data: bytes | None = None, cached: Json | None = None) -> None:
        identity = (entry["layer"], entry["path"], entry["stage"], entry["reference"])
        if identity in self.content.seen:
            return
        digest, oid, size = self._identity(data, cached)
        if not self.budget(entry["path"], size, digest):
            return
        if digest:
            entry.update({"sha256": digest, "oid": oid, "size": size})
            self._payload(digest, oid, data, size)
        self.content.seen.add(identity)
        self.entries.append(entry)

    def _pending_objects(self, entries: list[Json]) -> list[Json]:
        pending: list[Json] = []
        for entry in entries:
            if entry["mode"] in {None, 0o160000}:
                self.add(entry)
            else:
                pending.append(entry)
        return pending

    def _admit_objects(self, pending: list[Json], sizes: dict[str, int]) -> dict[str, int]:
        admitted: dict[str, int] = {}
        planned_bytes, planned_files = self.content.new_bytes, len(self.entries)
        for entry in pending:
            oid = entry["oid"]
            if oid not in sizes:
                self.skip(entry["path"], "unreadable")
                continue
            if not self.budget(entry["path"], sizes[oid], check_bytes=False):
                continue
            if planned_files >= self.recovery.limits.cap_files:
                self.skip(entry["path"], "cap_files", sizes[oid])
                continue
            new_size = 0 if oid in admitted else sizes[oid]
            if planned_bytes + new_size > self.recovery.limits.cap_bytes:
                self.skip(entry["path"], "cap_bytes", sizes[oid])
                continue
            admitted[oid] = sizes[oid]
            planned_files += 1
            planned_bytes += new_size
        return admitted

    def _store_objects(self, pending: list[Json], admitted: dict[str, int]) -> None:
        if not admitted:
            return
        result = self.git.blobs(admitted)
        for entry in pending:
            if entry["oid"] not in admitted:
                continue
            data = result.objects.get(entry["oid"])
            if data is None:
                self.skip(entry["path"], "deadline" if result.timed_out else "unreadable")
            elif store.blob_oid(data, self.object_format) != entry["oid"]:
                self.skip(entry["path"], "unreadable")
            else:
                self.add(entry, data)

    def _reuse_objects(self, entries: list[Json]) -> list[Json]:
        pending: list[Json] = []
        for entry in entries:
            cached = self.content.objects.get(entry["oid"])
            if cached and self.pooled(cached["sha256"]):
                self.add(entry, cached=cached)
            else:
                pending.append(entry)
        return pending

    def object_entries(self, entries: list[Json]) -> None:
        pending = self._reuse_objects(self._pending_objects(entries))
        ids = list(dict.fromkeys(entry["oid"] for entry in pending))[: self.recovery.limits.cap_files]
        try:
            sizes = object_sizes(self.git, ids)
        except RecoveryError as exc:
            for entry in pending:
                self.skip(entry["path"], "deadline" if exc.code == "deadline" else "unreadable")
            return
        self._store_objects(pending, self._admit_objects(pending, sizes))

    def _disk_entry(self, path: str, mode: int | None, oid: str | None) -> Json:
        base = self.head.get(path)
        index = self.index.get((path, 0))
        return entry_record("worktree", path, mode, oid, base_oid=base[1] if base else None, index_oid=index[1] if index else None, base_mode=base[0] if base else None, index_mode=index[0] if index else None)

    def _cached_disk(self, path: str, state: tuple[int, ...]) -> Json | None:
        cached = self.cache.get(path)
        if cached and cached.get("stat") == list(state) and self.pooled(cached["sha256"]):
            return cached
        return None

    def _store_disk(self, path: str, actual: store.LocalEntry, chunks: list[bytes], after: tuple[int, ...] | None) -> None:
        if actual.mode is None:
            self.add(self._disk_entry(path, None, None))
        elif self.index.get((path, 0)) != (actual.mode, actual.oid):
            self.add(self._disk_entry(path, actual.mode, actual.oid), b"".join(chunks))
            self.cache[path] = {"stat": list(after or ()), "sha256": actual.sha256, "oid": actual.oid, "mode": actual.mode, "size": actual.size}

    def _read_disk(self, path: str) -> None:
        for _attempt in range(2):
            before = stat_path(self.folder, path)
            chunks: list[bytes] = []
            actual = store.read_entry(self.folder, path, object_format=self.object_format, digest=True, sink=chunks.append)
            after = stat_path(self.folder, path)
            if actual.readable and before == after:
                self._store_disk(path, actual, chunks, after)
                return
        self.skip(path, "unreadable")

    def disk(self, path: str) -> None:
        if ("worktree", path, 0, "") in self.content.seen:
            return
        safe_path(path)
        state = stat_path(self.folder, path)
        if state is None:
            self.add(self._disk_entry(path, None, None))
            return
        if not stat.S_ISREG(state[-1]) and not stat.S_ISLNK(state[-1]):
            self.skip(path, "special_file")
            return
        if not self.budget(path, state[2], check_bytes=False):
            return
        cached = self._cached_disk(path, state)
        if cached:
            self.add(self._disk_entry(path, cached["mode"], cached["oid"]), cached=cached)
        else:
            self._read_disk(path)
