"""Recovery capture: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import custody_store as store
from command_walk import Discard
from recovery_git import changed_paths, ignored_paths, read_index, read_tree, unquote_path
from recovery_metadata import content_key, entry_record, folder_lock, read_json, stat_path, write_json
from recovery_packets import Recovery
from recovery_refs import RefCapture
from recovery_types import VERSION, HookEvent, Json, RecoveryError, command_text


class Capture(RefCapture):
    """Capture: one responsibility in the shared recovery implementation."""

    def _bind(self) -> Json:
        self.object_format = self.git.text(["rev-parse", "--show-object-format"])
        head = self.git.text(["rev-parse", "--verify", "-q", "HEAD"], allow=(0, 1, 128))
        self.common = str((self.folder / self.git.text(["rev-parse", "--git-common-dir"])).resolve())
        folder_name, common_name = str(self.folder), self.common
        # core.ignoreCase is Git's measured filesystem case-sensitivity setting.
        if self.git.text(["config", "--get", "core.ignoreCase"], allow=(0, 1)) == "true":
            folder_name, common_name = folder_name.casefold(), common_name.casefold()
        self.folder_key = content_key([folder_name, common_name])
        self.head, self.index = read_tree(self.git, head), read_index(self.git)
        self._load_cache()
        return {"folder": str(self.folder), "common_dir": self.common, "object_format": self.object_format, "head_commit": head, "head_tree": self.git.text(["rev-parse", "HEAD^{tree}"], allow=(0, 128)) if head else "", "branch": self.git.text(["symbolic-ref", "--quiet", "--short", "HEAD"], allow=(0, 1)) or "detached"}

    def _load_cache(self) -> None:
        try:
            data = read_json(self.recovery.root, f"folders/{self.folder_key}/statcache")
            paths, objects = data.get("paths", {}), data.get("objects", {})
            if not isinstance(paths, dict) or not isinstance(objects, dict):
                raise RecoveryError("statcache_invalid")
            self.cache, self.content.objects = paths, objects
        except (OSError, ValueError, RecoveryError, store.CustodyStoreError) as exc:
            self.cache, self.content.objects = {}, {}
            self.advisories.append("statcache_ignored")
            print(f"[recovery] stat cache unreadable; rereading files: {exc}", file=sys.stderr)

    def _foreign_paths(self, own: dict[str, str | None], own_stashes: set[str]) -> list[str]:
        foreign: list[str] = []
        for entry in self.entries:
            if entry["reference"] in own_stashes:
                continue
            path = str(self.folder / entry["path"])
            if entry["reference"] or path not in own or own[path] != entry["sha256"]:
                foreign.append(entry["path"])
        return foreign

    def _manifest(self, binding: Json) -> Json:
        own = self.recovery.own_paths(self.event)
        own_stashes = self.recovery.own_stashes(self.event)
        foreign = self._foreign_paths(own, own_stashes)
        foreign.extend(reference["name"] for reference in self.refs if reference["kind"] == "stash" and reference["tip"] not in own_stashes)
        return {
            "schema": VERSION,
            "binding": binding,
            "folder_key": self.folder_key,
            "tool": self.event.tool,
            "session_id": self.event.session_id,
            "turn_id": self.event.turn_id,
            "trigger": self.trigger,
            "command": command_text(self.event.command),
            "entries": self.entries,
            "refs": self.refs,
            "unsaved": self.unsaved,
            "unsaved_counts": self.counts,
            "foreign": sorted(set(foreign)),
            "advisories": self.advisories,
        }

    def _unchanged(self, previous: Json, fingerprint: str) -> tuple[str, Json] | None:
        last = previous.get("last_turn", {}).get(self.recovery.session_key(self.event), {})
        if self.trigger not in {"turn_end", "session_end"} or last.get("fingerprint") != fingerprint:
            return None
        try:
            _parts, prior, state = self.recovery.find(last["save_id"])
            return (last["save_id"], prior) if state == "present" else None
        except RecoveryError:
            return None

    def _metadata(self, binding: Json, previous: Json, fingerprint: str, packet_id: str, now: float) -> None:
        turns = previous.get("last_turn", {})
        if self.trigger in {"turn_end", "session_end"}:
            turns[self.recovery.session_key(self.event)] = {"fingerprint": fingerprint, "save_id": packet_id}
        write_json(self.recovery.root, ("folders", self.folder_key, "folder.json"), {**binding, "first_seen": previous.get("first_seen", now), "last_seen": now, "last_turn": turns})
        write_json(self.recovery.root, ("folders", self.folder_key, "statcache"), {"paths": self.cache, "objects": self.content.objects})
        self.recovery.touch(self.event, self.folder)

    def run(self) -> tuple[str, Json]:
        binding = self._bind()
        with folder_lock(self.recovery.root, self.folder_key, self.git.deadline):
            self.layers()
            manifest = self._manifest(binding)
            previous = read_json(self.recovery.root, f"folders/{self.folder_key}/folder.json")
            fingerprint = content_key([self.entries, self.refs, self.counts])
            unchanged = self._unchanged(previous, fingerprint)
            if unchanged is not None:
                return unchanged
            publication = self.recovery.publish(manifest, self.content.payloads)
            now = time.time()
            self.recovery.log("save", publication.packet_id, folder_key=self.folder_key, trigger=self.trigger, session_key=self.recovery.session_key(self.event), copied_payloads=publication.copied_payloads, at=now)
            self._metadata(binding, previous, fingerprint, publication.packet_id, now)
            return publication.packet_id, manifest

    def index_layer(self) -> None:
        indexed = [entry_record("index", path, mode, oid, base_oid=self.head.get(path, (0, None))[1], base_mode=self.head.get(path, (None, ""))[0], stage=stage) for (path, stage), (mode, oid) in self.index.items() if stage or self.head.get(path) != (mode, oid)]
        index_paths = {path for path, _stage in self.index}
        indexed.extend(entry_record("index", path, None, None, base_oid=oid, base_mode=_mode) for path, (_mode, oid) in self.head.items() if path not in index_paths)
        self.object_entries(indexed)

    def tracked_layer(self) -> None:
        try:
            for path in changed_paths(self.git, ["diff-files", "--no-ext-diff", "-z", "--raw"]):
                self.disk(path)
        except RecoveryError as exc:
            self.skip("tracked worktree", "deadline" if exc.code == "deadline" else "unreadable")

    def victims(self) -> None:
        if "victims" in self.discard.kinds:
            self.clean()
        for name in self.discard.stashes:
            self.stashes(name)
        for name in self.discard.branches:
            self.branch(name)
        if self.discard.paths:
            self.untracked(self.discard.paths)

    def layers(self) -> None:
        self.index_layer()
        self.tracked_layer()
        low_disk = shutil.disk_usage(self.recovery.root).free < self.recovery.limits.disk_floor
        if low_disk:
            self.advisories.append("disk_floor_tracked_only")
        if low_disk or self.trigger == "session_end" and self.event.tool == "codex":
            return
        try:
            self.victims()
            self.untracked((), remaining=True)
        except RecoveryError as exc:
            self.skip("remaining visible paths", "deadline" if exc.code == "deadline" else "unreadable")

    def leaves(self, directory: str) -> list[str]:
        return [os.fsdecode(name) for name in self.git.read(["ls-files", "-o", "--exclude-standard", "-z", "--", directory]).split(b"\0") if name]

    def modification_time(self, path: str) -> int:
        return (stat_path(self.folder, path) or (0, 0, 0, 0))[3]

    def untracked_names(self, paths: Sequence[str]) -> None:
        names = [os.fsdecode(name) for name in self.git.read(["ls-files", "-o", "--exclude-standard", "--directory", "-z", "--", *paths]).split(b"\0") if name]
        for name in (name for name in names if not name.endswith("/")):
            self.disk(name)
        for directory in (name for name in names if name.endswith("/")):
            leaves = self.leaves(directory)
            leaves.sort(key=self.modification_time, reverse=True)
            for path in leaves:
                self.disk(path)

    def untracked(self, paths: Sequence[str], *, remaining: bool = False) -> None:
        deadline = self.git.deadline
        if remaining:
            self.git.deadline = min(deadline, time.monotonic() + self.recovery.limits.untracked_seconds)
        try:
            self.untracked_names(paths)
        finally:
            self.git.deadline = deadline

    def clean_victim(self, name: str) -> None:
        if name.endswith("/"):
            leaves = self.leaves(name)
            for path in leaves:
                self.disk(path)
            if not leaves:
                self.skip(name, "ignored_victim")
        elif ignored_paths(self.git, [name]).get(name) is False:
            self.disk(name)
        else:
            self.skip(name, "ignored_victim")

    def clean(self) -> None:
        arguments = [argument for argument in self.discard.arguments if argument not in {"--force", "--interactive"}]
        for line in self.git.read(["clean", "-n", *arguments]).splitlines():
            if line.startswith(b"Would remove "):
                self.clean_victim(unquote_path(line[len(b"Would remove ") :]))


def save(recovery: Recovery, event: HookEvent, discard: Discard, trigger: str, deadline: float) -> tuple[str, Json]:
    try:
        return Capture(recovery, event, discard, trigger, deadline).run()
    except RecoveryError as exc:
        if exc.code != "git_read_failed" or trigger == "pre_discard":
            raise
        return non_git(recovery, event, discard.folder, trigger)


def non_git(recovery: Recovery, event: HookEvent, folder: Path, trigger: str) -> tuple[str, Json]:
    payloads: dict[str, store.Payload] = {}
    entries: list[Json] = []
    for absolute in recovery.own_paths(event):
        try:
            relative = str(Path(absolute).relative_to(folder))
        except ValueError:
            continue
        chunks: list[bytes] = []
        entry = store.read_entry(folder, relative, object_format="sha1", digest=True, sink=chunks.append)
        if entry.readable and entry.sha256 is not None:
            payload = store.Payload.from_bytes(b"".join(chunks), "sha1")
            payloads[payload.sha256] = payload
            entries.append({**entry_record("worktree", relative, entry.mode, entry.oid), "sha256": entry.sha256, "size": entry.size})
    key = content_key([str(folder), ""])
    manifest: Json = {
        "schema": VERSION,
        "binding": {"folder": str(folder), "common_dir": "", "object_format": "sha1", "head_commit": "", "head_tree": "", "branch": ""},
        "folder_key": key,
        "tool": event.tool,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "trigger": trigger,
        "command": "",
        "entries": entries,
        "refs": [],
        "unsaved": [],
        "unsaved_counts": {},
        "foreign": [],
        "advisories": ["non_git_journal_only"],
    }
    packet = recovery.publish(manifest, payloads)
    recovery.log("save", packet.packet_id, folder_key=key, trigger=trigger, session_key=recovery.session_key(event))
    return packet.packet_id, manifest
