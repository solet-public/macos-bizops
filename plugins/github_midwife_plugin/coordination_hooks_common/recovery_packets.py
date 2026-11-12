"""Recovery packets: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import os
import re
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import custody_store as store
from recovery_metadata import _json_bytes, append_event, content_key, load_limits, read_lines, safe_path
from recovery_types import _DIGEST, VERSION, HookEvent, Json, Limits, RecoveryError


class Recovery:
    """Recovery: one responsibility in the shared recovery implementation."""

    def __init__(self, root: Path, limits: Limits | None = None) -> None:
        self.root = root
        loaded, self.advisories = load_limits(root)
        self.limits = limits or loaded

    def log(self, action: str, save_id: str = "", **detail: Any) -> None:
        append_event(self.root, ("events.jsonl",), {"action": action, "save_id": save_id, "at": time.time(), **detail})

    @staticmethod
    def session_key(event: HookEvent) -> str:
        return content_key([event.tool, event.session_id])

    def journal(self, event: HookEvent) -> list[Json]:
        return read_lines(self.root, f"sessions/{self.session_key(event)}/edits.jsonl")

    def touch(self, event: HookEvent, folder: Path) -> None:
        if str(folder) not in {row.get("folder") for row in self.journal(event)}:
            append_event(self.root, ("sessions", self.session_key(event), "edits.jsonl"), {"kind": "touch", "folder": str(folder), "at": time.time()})

    def own_paths(self, event: HookEvent) -> dict[str, str | None]:
        return {row["path"]: row["sha256"] for row in self.journal(event) if row.get("kind") == "edit"}

    def own_stashes(self, event: HookEvent) -> set[str]:
        return {row["oid"] for row in self.journal(event) if row.get("kind") == "stash"}

    def publish(self, manifest: Json, payloads: dict[str, store.Payload]) -> store.Publication:
        """Enforce the recovery domain before invoking U1 atomic publication."""
        expected = {entry["sha256"]: entry["oid"] for entry in manifest["entries"] if entry["sha256"]}
        if expected.keys() != payloads.keys():
            raise RecoveryError("recovery_payload_set_mismatch")
        for digest, payload in payloads.items():
            if (payload.sha256, payload.oid) != (digest, expected[digest]):
                raise RecoveryError("recovery_payload_identity_mismatch")
        return store.publish_packet(self.root, ("packets", manifest["folder_key"]), store.canonical_bytes(manifest), payloads, dir_mode=0o700, file_mode=0o400, pool=("pool",))

    def packet_paths(self, include_trash: bool) -> list[tuple[str, ...]]:
        paths: list[tuple[str, ...]] = []
        with ExitStack() as stack:
            for directory in ("packets", "trash") if include_trash else ("packets",):
                try:
                    descriptor = store.open_store_directory(self.root, (directory,), stack)
                except store.CustodyStoreError as exc:
                    if exc.code != "custody_packet_missing":
                        raise
                    continue
                paths.extend(self.directory_packets(directory, descriptor))
        return paths

    def directory_packets(self, directory: str, descriptor: int) -> list[tuple[str, ...]]:
        paths: list[tuple[str, ...]] = []
        with ExitStack() as stack:
            for name in sorted(os.listdir(descriptor)):
                if not _DIGEST.fullmatch(name):
                    continue
                if directory == "trash":
                    paths.append((directory, name))
                else:
                    folder = store.open_store_directory(self.root, (directory, name), stack)
                    paths.extend((directory, name, packet) for packet in sorted(os.listdir(folder)) if _DIGEST.fullmatch(packet))
        return paths

    def packets(self, *, include_trash: bool = False) -> list[tuple[tuple[str, ...], Json, str]]:
        return [(parts, self.read(parts), "trashed" if parts[0] == "trash" else "present") for parts in self.packet_paths(include_trash)]

    def read(self, parts: tuple[str, ...]) -> Json:
        raw = store.read_verified(self.root, parts, packet_id=parts[-1], object_format="sha1")
        manifest = _json_bytes(raw)
        if manifest.get("schema") != VERSION or not isinstance(manifest.get("entries"), list):
            raise RecoveryError("recovery_packet_invalid", parts[-1])
        expected: dict[str, tuple[str, int]] = {}
        for entry in manifest["entries"]:
            safe_path(entry["path"])
            if entry["sha256"]:
                expected[entry["sha256"]] = entry["oid"], entry["size"]
        store.verify_payload_set(self.root, parts, expected, object_format=manifest["binding"]["object_format"])
        return manifest

    def find(self, save_id: str) -> tuple[tuple[str, ...], Json, str]:
        if not re.fullmatch(r"[0-9a-f]{8,64}", save_id):
            raise RecoveryError("save_id_invalid")
        matches = [packet for packet in self.packets(include_trash=True) if packet[0][-1].startswith(save_id)]
        if len(matches) != 1:
            raise RecoveryError("save_id_ambiguous" if matches else "save_missing", save_id)
        return matches[0]

    def history(self) -> list[Json]:
        return read_lines(self.root, "events.jsonl")

    def show(self, save_id: str) -> Json:
        parts, manifest, state = self.find(save_id)
        return {"save_id": parts[-1], "state": state, "manifest": manifest, "history": [row for row in self.history() if row.get("save_id") == parts[-1]]}

    def payload(self, parts: tuple[str, ...], manifest: Json, entry: Json) -> bytes:
        return store.read_payload(self.root, "/".join((*parts, "payload", entry["sha256"])), manifest["binding"]["object_format"], (entry["sha256"], entry["oid"]))
