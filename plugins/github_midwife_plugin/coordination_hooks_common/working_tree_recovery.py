"""Public handle(HookEvent) -> HookResult and the recovery lifecycle CLI.

Adapters call handle in the main thread of a standalone Python hook process.
One implementation, one Git spawn boundary (recovery_git._spawn). No adapter
or hook wiring lives here. Python 3.11 grammar and stdlib only; runtime floor
proof is reported separately by recovery_runtime_smoke.

U3/U4 must materialize the complete canonical module inventory below:
  command_lex.py
  command_resolve.py
  command_types.py
  command_walk.py
  custody_store.py
  discard_forms.py
  recovery_capture.py
  recovery_cli.py
  recovery_content.py
  recovery_deadline.py
  recovery_events.py
  recovery_git.py
  recovery_journal.py
  recovery_lifecycle.py
  recovery_metadata.py
  recovery_packets.py
  recovery_rebuild.py
  recovery_refs.py
  recovery_restore.py
  recovery_retention.py
  recovery_rules.py
  recovery_types.py
  working_tree_recovery.py

Limits are optional config.json fields: set/correct a field there; removing it
clears it to its documented Limits default. hard_seconds bounds the whole
pre-discard hook; publication_seconds reserves time within that budget for
atomic publication after capture stops. An existing process alarm or off-thread
embedding produces an advisory rather than taking over another caller's timer.
Actual coding-tool hook processes have a fresh main thread, covered by the
public handle/CLI and held-root-lock smoke pass paths.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import custody_store as store
from command_walk import Discard, classify
from recovery_capture import save
from recovery_cli import main
from recovery_deadline import hook_deadline
from recovery_events import normalize_event, notice
from recovery_git import Git
from recovery_journal import record_edits
from recovery_metadata import append_event, prepare_root
from recovery_packets import Recovery
from recovery_retention import prune, storage_bytes
from recovery_types import _EVENTS, HookEvent, HookResult, Json, RecoveryError, command_text


def _budget(event: HookEvent) -> float:
    if event.event == "Stop":
        return 60.0
    if event.event == "SessionEnd" and event.tool == "codex":
        return 2.5
    return 8.0


def _commands(event: HookEvent, cwd: Path, deadline: float) -> tuple[Discard, ...] | None:
    if event.event != "PreToolUse":
        return ()
    if event.tool_name != "Bash" or not event.command:
        return None
    commands = classify(command_text(event.command), cwd, query=lambda folder, args: Git(folder, deadline).read(args, allow=(0, 1, 128)))
    return commands or None


def _group(commands: tuple[Discard, ...]) -> tuple[Discard, ...]:
    grouped: dict[Path, Discard] = {}
    for command in commands:
        previous = grouped.get(command.folder)
        if previous is None:
            grouped[command.folder] = command
        else:
            grouped[command.folder] = Discard(
                command.folder,
                tuple(sorted({*previous.kinds, *command.kinds})),
                previous.arguments + command.arguments,
                previous.paths + command.paths,
                tuple(dict.fromkeys((*previous.stashes, *command.stashes))),
                tuple(dict.fromkeys((*previous.branches, *command.branches))),
                previous.advisories + command.advisories,
                previous.stash_push or command.stash_push,
            )
    return tuple(grouped.values())


def _failure(event: HookEvent, exc: BaseException) -> HookResult:
    print(f"[recovery] {type(exc).__name__}: {exc}", file=sys.stderr)
    if isinstance(exc, RecoveryError) and exc.code == "git_missing":
        return HookResult()
    if event.event == "PreToolUse":
        text = f"Could not save before {command_text(event.command)!r}: {exc}; the command will run."
        return HookResult(notice=text, context=text)
    if event.event == "SessionStart":
        return HookResult(context=f"Recovery is inactive: {exc}. Commands continue; no save can be made until the local store is writable.")
    return HookResult()


def _session_start(recovery: Recovery, event: HookEvent, advisories: tuple[str, ...]) -> HookResult:
    size = storage_bytes(recovery)
    seen = any(row.get("kind") == "over_cap_notice" for row in recovery.journal(event))
    if size > recovery.limits.store_bytes and not seen:
        append_event(recovery.root, ("sessions", recovery.session_key(event), "edits.jsonl"), {"kind": "over_cap_notice", "at": time.time()})
        return HookResult(context=f"Recovery holds {size} bytes including undelivered work. Saving continues. Use {Path(__file__).resolve()} list --undelivered, forget, or prune to inspect it.", advisories=advisories)
    return HookResult(advisories=advisories)


def _notice_manifest(recovery: Recovery, event: HookEvent, manifest: Json) -> Json:
    if "disk_floor_tracked_only" not in manifest["advisories"]:
        return manifest
    seen = any(row.get("kind") == "disk_floor_notice" for row in recovery.journal(event))
    if seen:
        return {**manifest, "advisories": [reason for reason in manifest["advisories"] if reason != "disk_floor_tracked_only"]}
    append_event(recovery.root, ("sessions", recovery.session_key(event), "edits.jsonl"), {"kind": "disk_floor_notice", "at": time.time()})
    return manifest


def _capture_one(recovery: Recovery, event: HookEvent, command: Discard, trigger: str, deadline: float) -> HookResult:
    try:
        save_id, manifest = save(recovery, event, command, trigger, deadline)
        if event.event == "PreToolUse":
            message, context = notice(save_id, _notice_manifest(recovery, event, manifest), root=recovery.root)
            return HookResult(message, context, (save_id,))
        return HookResult(saves=(save_id,))
    except (OSError, RecoveryError, store.CustodyStoreError) as exc:
        return _failure(event, exc)


def _capture(recovery: Recovery, event: HookEvent, commands: tuple[Discard, ...], deadline: float, advisories: tuple[str, ...]) -> HookResult:
    trigger = {"PreToolUse": "pre_discard", "Stop": "turn_end", "SessionEnd": "session_end"}[event.event]
    results = [_capture_one(recovery, event, command, trigger, deadline) for command in _group(commands)]
    if event.event == "Stop":
        prune(recovery, apply=True, hourly=True, deadline=deadline)
    notices = "\n".join(result.notice for result in results if result.notice)
    contexts = "\n".join(result.context for result in results if result.context)
    saves = tuple(save_id for result in results for save_id in result.saves)
    return HookResult(notices, contexts, saves, advisories)


def _run(event: HookEvent, root: Path | None, cwd: Path, deadline: float) -> HookResult:
    commands = _commands(event, cwd, deadline)
    if commands is None:
        return HookResult()
    chosen, advisories = prepare_root(root, cwd, deadline)
    recovery = Recovery(chosen)
    advisories += recovery.advisories
    if event.event == "PreToolUse":
        deadline = min(deadline, time.monotonic() + recovery.limits.hard_seconds)
    if event.event == "PostToolUse":
        record_edits(recovery, event, deadline)
        return HookResult(advisories=advisories)
    if event.event == "SessionStart":
        return _session_start(recovery, event, advisories)
    if event.event in {"Stop", "SessionEnd"}:
        folders = {cwd, *(Path(row["folder"]) for row in recovery.journal(event) if row.get("folder"))}
        commands = tuple(Discard(folder, ("tree",)) for folder in sorted(folders))
    reserve = recovery.limits.publication_seconds if deadline - time.monotonic() > recovery.limits.publication_seconds else 0.0
    capture_deadline = deadline - reserve
    with hook_deadline(deadline):
        return _capture(recovery, event, commands, capture_deadline, advisories)


def _handle(event: HookEvent, root: Path | None) -> HookResult:
    cwd = Path(event.cwd).resolve()
    deadline = time.monotonic() + _budget(event)
    with hook_deadline(deadline):
        return _run(event, root, cwd, deadline)


def handle(event: object, *, root: Path | None = None) -> HookResult:
    """Accept HookEvent; every malformed input or fault stays informational."""
    if not isinstance(event, HookEvent):
        return HookResult()
    normalized = event
    try:
        normalized = normalize_event(event)
        if normalized.event not in _EVENTS or not normalized.session_id or not normalized.cwd:
            return HookResult()
        if sys.version_info < (3, 11):  # noqa: UP036 - installed hook has its own Python 3.11 floor
            return _failure(normalized, RecoveryError("python_below_3_11"))
        return _handle(normalized, root)
    except BaseException as exc:
        return _failure(normalized, exc)


if __name__ == "__main__":
    raise SystemExit(main())


MODULES = (
    "command_lex.py",
    "command_resolve.py",
    "command_types.py",
    "command_walk.py",
    "custody_store.py",
    "discard_forms.py",
    "recovery_capture.py",
    "recovery_cli.py",
    "recovery_content.py",
    "recovery_deadline.py",
    "recovery_events.py",
    "recovery_git.py",
    "recovery_journal.py",
    "recovery_lifecycle.py",
    "recovery_metadata.py",
    "recovery_packets.py",
    "recovery_rebuild.py",
    "recovery_refs.py",
    "recovery_restore.py",
    "recovery_retention.py",
    "recovery_rules.py",
    "recovery_types.py",
    "working_tree_recovery.py",
)

__all__ = ("HookEvent", "HookResult", "MODULES", "handle", "main")
