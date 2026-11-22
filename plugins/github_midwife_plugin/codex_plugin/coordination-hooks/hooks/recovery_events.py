"""Recovery events: canonical stdlib Python 3.11 recovery component.

Part of one shared engine; working_tree_recovery is its public facade.
"""

from __future__ import annotations

import re
import shlex
import sys
from dataclasses import replace
from pathlib import Path

from recovery_types import HookEvent, Json, command_text


def _input_command(value: object) -> str | list[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(token, str) for token in value):
        return value
    return ""


def _edited_paths(tool_input: Json) -> list[str]:
    paths = [tool_input[name] for name in ("file_path", "notebook_path") if isinstance(tool_input.get(name), str)]
    edits = tool_input.get("edits", [])
    if isinstance(edits, list):
        paths.extend(edit["file_path"] for edit in edits if isinstance(edit, dict) and isinstance(edit.get("file_path"), str))
    return paths


def _patch_paths(value: object) -> list[str]:
    if isinstance(value, dict):
        value = value.get("patch", value.get("input", ""))
    if not isinstance(value, str):
        return []
    return re.findall(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+)$", value, flags=re.MULTILINE)


def normalize_event(event: HookEvent) -> HookEvent:
    payload = event.payload
    if payload is None:
        return event
    event_name = payload.get("hook_event_name", "")
    if event.event and event.event != event_name:
        return HookEvent()
    required = (event_name, payload.get("session_id", ""), payload.get("cwd", ""))
    if not all(isinstance(value, str) for value in required):
        return HookEvent()
    raw = payload.get("tool_input", {})
    tool_input = raw if isinstance(raw, dict) else {}
    paths = _patch_paths(raw) if payload.get("tool_name") == "apply_patch" else _edited_paths(tool_input)
    return replace(event, event=event_name, session_id=payload.get("session_id", ""), turn_id=payload.get("turn_id", payload.get("tool_use_id", "")), cwd=payload.get("cwd", ""), command=_input_command(tool_input.get("command", "")), tool_name=payload.get("tool_name", ""), edited_paths=tuple(paths), payload=None)


def _omissions(manifest: Json) -> tuple[int, str]:
    incomplete = sum(value for reason, value in manifest["unsaved_counts"].items() if reason not in {"ignored_victim", "ignored_in_ref"})
    text = ""
    if incomplete:
        text += f"; {incomplete} paths were not saved: " + ", ".join(f"{entry['path']} ({entry['reason']})" for entry in manifest["unsaved"][:10])
    ignored = sum(manifest["unsaved_counts"].get(reason, 0) for reason in ("ignored_victim", "ignored_in_ref"))
    if ignored:
        names = [entry["path"] for entry in manifest["unsaved"] if entry["reason"] in {"ignored_victim", "ignored_in_ref"}]
        text += f"; {ignored} ignored paths left out by design (" + ", ".join(names[:10]) + ")"
    return incomplete, text


def _capture_advisories(manifest: Json) -> str:
    text = ""
    if any(str(reason).startswith("ref_rules_unclassified") for reason in manifest["advisories"]):
        text += "; stash or branch untracked files could not be saved because their ignore rules could not be checked in a safe place"
    if "disk_floor_tracked_only" in manifest["advisories"]:
        text += "; disk space is low, so only index and tracked files were saved"
    return text


def notice(save_id: str, manifest: Json, *, root: Path | None = None) -> tuple[str, str]:
    foreign = manifest["foreign"]
    incomplete, omitted = _omissions(manifest)
    if not foreign and not incomplete and "disk_floor_tracked_only" not in manifest["advisories"]:
        return "", ""
    text = f"Saved {len(manifest['entries'])} file versions before {command_text(manifest['command'])!r}, including {len(foreign)} changed outside this session"
    if foreign:
        text += " (" + ", ".join(foreign[:10]) + ")"
    text += omitted + _capture_advisories(manifest)
    arguments = [sys.executable, str(Path(__file__).with_name("working_tree_recovery.py").resolve())]
    if root is not None:
        arguments.extend(["--root", str(root)])
    restore = shlex.join([*arguments, "restore", save_id[:8]])
    text += f". To put them back, ask me to restore save {save_id[:8]}, or run: {restore}"
    return text, text + ". If the user did not mean to discard these changes, stop and tell them before continuing."
