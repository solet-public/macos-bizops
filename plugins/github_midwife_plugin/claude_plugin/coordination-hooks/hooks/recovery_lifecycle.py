"""Recovery lifecycle: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import time

import custody_store as store
from recovery_metadata import content_key
from recovery_packets import Recovery
from recovery_types import RecoveryError


def correct(recovery: Recovery, save_id: str, note: str) -> str:
    parts, manifest, state = recovery.find(save_id)
    if state != "present":
        raise RecoveryError("recovery_stale_state", f"packet is {state}; reread show before correction")
    successor = {**manifest, "predecessor": parts[-1], "note": note}
    payloads = {entry["sha256"]: store.Payload.from_path(recovery.root, "/".join((*parts, "payload", entry["sha256"])), manifest["binding"]["object_format"], sha256=entry["sha256"], oid=entry["oid"]) for entry in manifest["entries"] if entry["sha256"]}
    result = recovery.publish(successor, payloads)
    recovery.log("correct", result.packet_id, predecessor=parts[-1], note=note, folder_key=manifest["folder_key"], trigger=manifest["trigger"], session_key=content_key([manifest["tool"], manifest["session_id"]]))
    return result.packet_id


def _transition(recovery: Recovery, save_id: str, expected_state: str, action: str) -> str:
    parts, manifest, state = recovery.find(save_id)
    if expected_state != state:
        raise RecoveryError("recovery_stale_state", f"packet is {state}; reread show before {action}")
    original = ("packets", manifest["folder_key"], parts[-1])
    observation = store.observe_packet(recovery.root, original)
    if observation.state != state:
        raise RecoveryError("recovery_stale_state", "packet changed after read")
    last_retired = max((float(row["at"]) for row in recovery.history() if row.get("save_id") == parts[-1] and row.get("action") in {"forget", "prune"}), default=0.0)
    age = time.time() - last_retired
    if action == "unforget" and age >= recovery.limits.trash_seconds:
        raise RecoveryError("trash_grace_expired", "bytes remain readable until purge")
    if action == "purge" and age < recovery.limits.trash_seconds:
        raise RecoveryError("trash_grace_active")
    operation = {"forget": store.trash, "unforget": store.untrash, "purge": store.purge}[action]
    operation(recovery.root, original, expected=observation)
    recovery.log(action, parts[-1], folder_key=manifest["folder_key"])
    return parts[-1]


def forget(recovery: Recovery, save_id: str, expected_state: str) -> str:
    return _transition(recovery, save_id, expected_state, "forget")


def unforget(recovery: Recovery, save_id: str, expected_state: str) -> str:
    return _transition(recovery, save_id, expected_state, "unforget")


def purge(recovery: Recovery, save_id: str | None = None) -> list[str]:
    candidates = [recovery.find(save_id)] if save_id else recovery.packets(include_trash=True)
    purged: list[str] = []
    for parts, _manifest, state in candidates:
        if state == "trashed":
            try:
                purged.append(_transition(recovery, parts[-1], "trashed", "purge"))
            except RecoveryError as exc:
                if save_id or exc.code != "trash_grace_active":
                    raise
    if (recovery.root / "pool").exists():
        store.sweep_pool(recovery.root)
    return purged
