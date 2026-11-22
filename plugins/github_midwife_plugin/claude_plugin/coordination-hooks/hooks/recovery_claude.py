#!/usr/bin/env python3
"""Claude Code adapter for working-tree recovery saves.

One thin adapter over the shared engine that ships beside it (`working_tree_recovery.py`
and the 22 modules its MODULES inventory names, byte-identical copies of
`coordination_hooks_common/`). It does exactly four things: read the hook payload Claude
Code writes on stdin, hand it to the engine as a `HookEvent`, render the engine's
optional text in Claude Code's output shape, and exit 0. Classification, capture and every
store access live in the engine; none lives here.

Registered in hooks.json for PreToolUse (matcher Bash), PostToolUse (the file tools and
Bash), Stop (async), SessionEnd and SessionStart, each through optional_hook_runner.py, so a
missing or stale script is skipped with exit 0 instead of failing a tool call. The runner
consumes the event argument, so the event is this script's own first argument when it is run
directly, and the payload's `hook_event_name` otherwise.

Inputs: the event name, and these payload fields (read by the engine): `hook_event_name`,
`session_id`, `cwd`, `tool_name`, `tool_input.command`, `tool_input.file_path`,
`tool_input.notebook_path`, `tool_input.edits[].file_path` and `turn_id` (falling back to `tool_use_id`). The transcript
is never read.

Output: nothing, or one JSON object whose only keys are `systemMessage` and
`hookSpecificOutput` (with `hookEventName` and `additionalContext`). PreToolUse prints it
when a save was made for changes the session did not make, or could not be made; SessionStart
prints `additionalContext` alone when recovery is inactive or over its cap; PostToolUse, Stop
and SessionEnd print nothing. This hook is not a control: it never refuses, blocks or steers
a tool call.

Failure contract: exit 0 on every path. One top-level handler turns any fault into a single
`[recovery] <reason>` line on stderr. Below Python 3.11 it exits 0 before importing any
engine module, and SessionStart says why. Stdlib only.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path

_FLOOR = (3, 11)
_TOOL = "claude"


def _note(text: str) -> None:
    """One diagnostic line on stderr; a closed stderr must not change the exit code."""
    with contextlib.suppress(Exception):
        sys.stderr.write("[recovery] " + " ".join(text.split())[:300] + "\n")
        sys.stderr.flush()


def _emit(output: dict[str, object]) -> None:
    if output:
        sys.stdout.write(json.dumps(output) + "\n")
        sys.stdout.flush()


def _session_start(context: str) -> dict[str, object]:
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}


def _below_floor(event: str) -> dict[str, object]:
    if event != "SessionStart":
        return {}
    found = f"{sys.version_info[0]}.{sys.version_info[1]}"
    return _session_start(f"Recovery saves are inactive: this hook needs Python 3.11 or later and python3 is {found}. Commands continue; nothing is saved until python3 resolves to 3.11 or later.")


def _payload() -> dict[str, object] | None:
    raw = sys.stdin.buffer.read()
    if not raw.strip():
        return None
    value = json.loads(raw)
    return value if isinstance(value, dict) else None


def _render(event: str, notice: str, context: str) -> dict[str, object]:
    if event == "PreToolUse":
        output: dict[str, object] = {}
        if notice:
            output["systemMessage"] = notice
        if context:
            output["hookSpecificOutput"] = {"hookEventName": "PreToolUse", "additionalContext": context}
        return output
    if event == "SessionStart" and context:
        return _session_start(context)
    return {}


def _event(argv: list[str], payload: dict[str, object]) -> str:
    if len(argv) > 1:
        return argv[1]
    name = payload.get("hook_event_name")
    return name if isinstance(name, str) else ""


def main(argv: list[str]) -> int:
    try:
        payload = _payload()
        if payload is None:
            return 0
        event = _event(argv, payload)
        if sys.version_info < _FLOOR:  # noqa: UP036 -- this file ships to machines whose python3 may be older
            _emit(_below_floor(event))
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from working_tree_recovery import HookEvent, handle  # noqa: PLC0415 -- must follow the interpreter floor

        result = handle(HookEvent(event=event, tool=_TOOL, payload=payload))
        if result.advisories:
            _note("advisories: " + ", ".join(result.advisories))
        _emit(_render(event, result.notice, result.context))
    except BaseException as exc:  # noqa: BLE001 -- every fault, including a deadline, ends in exit 0
        _note(f"{type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
