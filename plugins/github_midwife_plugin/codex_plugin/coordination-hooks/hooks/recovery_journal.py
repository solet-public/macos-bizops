"""Recovery journal: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import time
from pathlib import Path

import custody_store as store
from command_walk import classify
from recovery_git import Git
from recovery_metadata import append_event, safe_path
from recovery_packets import Recovery
from recovery_types import HookEvent, command_text


def _record_stash(recovery: Recovery, event: HookEvent, deadline: float) -> None:
    cwd = Path(event.cwd).resolve()
    commands = classify(command_text(event.command), cwd, query=lambda folder, args: Git(folder, deadline).read(args, allow=(0, 1, 128)))
    for command in commands:
        if not command.stash_push:
            continue
        oid = Git(command.folder, deadline).text(["rev-parse", "--verify", "-q", "refs/stash"], allow=(0, 1, 128))
        if oid:
            append_event(recovery.root, ("sessions", recovery.session_key(event), "edits.jsonl"), {"kind": "stash", "oid": oid, "folder": str(command.folder), "at": time.time()})


def _record_path(recovery: Recovery, event: HookEvent, value: str, deadline: float) -> None:
    raw = Path(event.cwd) / value
    absolute = raw.parent.resolve() / raw.name
    repository = Git(absolute.parent, deadline).text(["rev-parse", "--show-toplevel"], allow=(0, 128))
    folder = Path(repository).resolve() if repository else absolute.parent
    relative = str(absolute.relative_to(folder))
    safe_path(relative)
    entry = store.read_entry(folder, relative, object_format="sha1", digest=True)
    if entry.readable:
        recovery.touch(event, folder)
        append_event(recovery.root, ("sessions", recovery.session_key(event), "edits.jsonl"), {"kind": "edit", "path": str(absolute), "sha256": entry.sha256, "folder": str(folder), "at": time.time()})


def record_edits(recovery: Recovery, event: HookEvent, deadline: float) -> None:
    recovery.touch(event, Path(event.cwd).resolve())
    if event.tool_name == "Bash":
        _record_stash(recovery, event, deadline)
    elif event.tool_name in {"Write", "Edit", "MultiEdit", "NotebookEdit", "apply_patch"}:
        for value in event.edited_paths:
            _record_path(recovery, event, value, deadline)
