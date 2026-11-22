#!/usr/bin/env python3
"""Codex adapter for working-tree recovery: read one hook payload, call the shared engine, print only what Codex allows.

The adapter holds no classification, capture or store logic. It hands the payload to the
shared recovery engine (the sibling modules materialized byte-identical from
coordination_hooks_common) and renders the engine's answer in Codex's output shape.

Output contract (design sections 5.2 and 7.1, security checklist items 3, 9 and 12):
- PreToolUse prints ``systemMessage`` and ``hookSpecificOutput.additionalContext`` when a
  save needs telling. SessionStart prints ``hookSpecificOutput.additionalContext`` only
  when recovery is inactive or over its cap. PostToolUse, Stop and SessionEnd print nothing.
- No decision of any kind is ever printed and nothing is ever blocked. Every fault ends in
  one stderr line and exit 0, so the tool call always proceeds.
- No network, no register and no bridge call. This file writes no file itself; the engine
  writes only its local store. Bytecode is never written beside the shipped files.
"""

from __future__ import annotations

import contextlib
import json
import sys
from pathlib import Path
from typing import Any, cast

sys.dont_write_bytecode = True

_FLOOR = (3, 11)
_SPEAKING = frozenset({"PreToolUse", "SessionStart"})
_FLOOR_ADVISORY = "Working-tree recovery is inactive: it needs Python 3.11 or newer and this hook runs an older interpreter. Commands continue without a save."


def _emit(value: dict[str, object]) -> None:
    print(json.dumps(value))
    sys.stdout.flush()


def _floor_advisory(text: str) -> None:
    """Below the floor nothing else may be imported; SessionStart alone says so, once, in the one output shape."""
    event = json.loads(text).get("hook_event_name") if text.lstrip().startswith("{") else None
    if event == "SessionStart":
        _emit({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": _FLOOR_ADVISORY}})


def _render(event: str, notice: str, context: str) -> dict[str, object] | None:
    """Only PreToolUse and SessionStart may speak, and only with the two allowed keys."""
    if event not in _SPEAKING or not (notice or context):
        return None
    output: dict[str, object] = {}
    if notice and event == "PreToolUse":
        output["systemMessage"] = notice
    if context:
        output["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": context}
    return output or None


def _run(text: str) -> None:
    loaded: Any = json.loads(text)
    if not isinstance(loaded, dict):
        return
    payload = cast("dict[str, Any]", loaded)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    # Imported only here, after the floor guard: the engine uses Python 3.11 grammar.
    from working_tree_recovery import HookEvent, handle

    with contextlib.redirect_stdout(sys.stderr):  # a stray engine print can never reach the stdout Codex parses
        result = handle(HookEvent(tool="codex", payload=payload))
    event = str(payload.get("hook_event_name", ""))
    rendered = _render(event, result.notice, result.context)
    if rendered is not None:
        _emit(rendered)
    if result.advisories:
        print("[recovery] advisory: " + ", ".join(result.advisories), file=sys.stderr)


def main() -> int:
    try:
        text = sys.stdin.read()
        if sys.version_info < _FLOOR:
            _floor_advisory(text)
        else:
            _run(text)
    except BaseException as exc:  # a recovery hook never troubles the tool call: one stderr line, exit 0
        with contextlib.suppress(OSError, ValueError):
            print(f"[recovery] {type(exc).__name__}: {exc}"[:500], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
