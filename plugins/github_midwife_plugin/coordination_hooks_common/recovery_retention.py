"""Recovery retention: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

import custody_store as store
from recovery_git import Git
from recovery_lifecycle import purge
from recovery_packets import Recovery
from recovery_types import Json, RecoveryError


def storage_bytes(recovery: Recovery) -> int:
    identities: set[tuple[int, int]] = set()
    size = 0
    for directory, subdirs, files in os.walk(recovery.root, followlinks=False):
        subdirs[:] = [name for name in subdirs if not (Path(directory) / name).is_symlink()]
        for name in files:
            info = (Path(directory) / name).lstat()
            identity = (info.st_dev, info.st_ino)
            if stat.S_ISREG(info.st_mode) and identity not in identities:
                identities.add(identity)
                size += info.st_size
    return size


def _delivery_git(manifest: Json, deadline: float) -> Git | None:
    binding = manifest["binding"]
    folder = Path(binding["folder"])
    if folder.exists():
        return Git(folder, deadline)
    common = Path(binding["common_dir"])
    if not binding["common_dir"] or not common.is_dir():
        return None
    return Git(common, deadline, {"GIT_DIR": str(common)})


def _refs_delivered(git: Git, refs: list[Json]) -> bool:
    if not refs:
        return True
    stashes = set(git.text(["stash", "list", "--format=%H"]).splitlines())
    for reference in refs:
        if reference["kind"] == "stash" and reference["tip"] not in stashes:
            return False
        if reference["kind"] == "branch":
            tip = git.text(["rev-parse", "--verify", "-q", f"refs/heads/{reference['name']}"], allow=(0, 1, 128))
            if tip != reference["tip"]:
                return False
    return True


def _entry_delivered(entry: Json, answer: bytes) -> bool:
    if entry["mode"] is None:
        return answer.endswith(b" missing")
    fields = answer.split()
    return len(fields) == 3 and fields[:1] == [str(entry["oid"]).encode()]


def _paths_delivered(recovery: Recovery, git: Git, entries: list[Json]) -> bool:
    tips = list(dict.fromkeys([git.text(["rev-parse", "--verify", "-q", "HEAD"], allow=(0, 1, 128)), *git.text(["for-each-ref", "--format=%(objectname)", "refs/heads/"]).splitlines()]))
    requests = _delivery_requests(entries, tips)
    if not requests or len(requests) > recovery.limits.cap_files:
        return False
    request = b"".join(os.fsencode(f"{tip}:{entry['path']}") + b"\0" for entry, tip in requests)
    result = git.read(["cat-file", "--batch-check", "-Z"], request=request).split(b"\0")
    if len(result) != len(requests) + 1:
        return False
    satisfied = {id(entry) for (entry, _tip), raw in zip(requests, result[:-1], strict=True) if _entry_delivered(entry, raw)}
    return all(id(entry) in satisfied for entry in entries)


def delivered(recovery: Recovery, manifest: Json, deadline: float) -> bool:
    git = _delivery_git(manifest, deadline)
    if git is None:
        return False
    try:
        if not _refs_delivered(git, manifest["refs"]):
            return False
        entries = [entry for entry in manifest["entries"] if entry["layer"] in {"index", "worktree"}]
        return _paths_delivered(recovery, git, entries) if entries else True
    except (OSError, RecoveryError):
        return False


def _times(history: list[Json], actions: set[str]) -> dict[str, float]:
    return {row["save_id"]: float(row["at"]) for row in history if row["action"] in actions}


class PrunePlan:
    """Lazy delivery and newest-turn protection, traversed newest first."""

    def __init__(self, recovery: Recovery, now: float, apply: bool, deadline: float) -> None:
        self.recovery, self.now, self.apply, self.deadline = recovery, now, apply, deadline
        self.observed = _times(recovery.history(), {"delivered"})
        self.newest: set[tuple[str, str, str]] = set()
        self.protected: set[str] = set()
        self.newer_delivered: set[tuple[str, str]] = set()
        self.eligible: list[tuple[float, tuple[str, ...], Json]] = []

    def protect_turn(self, packet_id: str, manifest: Json) -> None:
        session = (manifest["folder_key"], manifest["tool"], manifest["session_id"])
        if manifest["trigger"] == "turn_end" and session not in self.newest:
            self.protected.add(packet_id)
            self.newest.add(session)

    def consider(self, parts: tuple[str, ...], manifest: Json) -> None:
        packet_id = parts[-1]
        self.protect_turn(packet_id, manifest)
        keys = {(manifest["folder_key"], entry["path"]) for entry in manifest["entries"] if entry["layer"] in {"index", "worktree"}}
        inherited = bool(keys) and not manifest["refs"] and keys.issubset(self.newer_delivered)
        is_delivered = inherited or delivered(self.recovery, manifest, self.deadline)
        if not is_delivered:
            return
        first = self.observed.get(packet_id, self.now)
        if packet_id not in self.observed and self.apply:
            self.recovery.log("delivered", packet_id)
        self.newer_delivered.update(keys)
        if packet_id not in self.protected:
            self.eligible.append((first, parts, manifest))

    def retire(self, size: int) -> list[str]:
        pruned: list[str] = []
        for first, parts, manifest in sorted(self.eligible, key=lambda item: item[0]):
            if self.now - first < self.recovery.limits.grace_seconds and size <= self.recovery.limits.store_bytes:
                continue
            pruned.append(parts[-1])
            if self.apply:
                observation = store.observe_packet(self.recovery.root, parts)
                store.trash(self.recovery.root, parts, expected=observation)
                self.recovery.log("prune", parts[-1], folder_key=manifest["folder_key"])
        return pruned


def prune(recovery: Recovery, *, apply: bool = False, hourly: bool = False, deadline: float | None = None) -> Json:
    now = time.time()
    history = recovery.history()
    previous = max((float(row["at"]) for row in history if row["action"] == "prune_pass"), default=0.0)
    if hourly and now - previous < 3600:
        return {"pruned": [], "advisory": "prune already ran within the hour"}
    saved = _times(history, {"save", "correct"})
    packets = sorted(recovery.packets(), key=lambda packet: saved.get(packet[0][-1], now), reverse=True)
    plan = PrunePlan(recovery, now, apply, deadline if deadline is not None else time.monotonic() + 60.0)
    for parts, manifest, _state in packets:
        plan.consider(parts, manifest)
    size = storage_bytes(recovery)
    pruned = plan.retire(size)
    if apply:
        purge(recovery)
        recovery.log("prune_pass")
    return {"pruned": pruned, "bytes": size, "over_cap": size > recovery.limits.store_bytes, "protected_newest_turn": sorted(plan.protected), "apply": apply}


def _delivery_requests(entries: list[Json], tips: list[str]) -> list[tuple[Json, str]]:
    return [(entry, tip) for entry in entries for tip in tips if tip]
