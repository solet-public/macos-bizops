#!/usr/bin/env python3
"""SessionStart + UserPromptSubmit hook. Reads the hook_event_name Claude Code
passes on stdin so the same script serves both events without guessing which
one fired.

Armed on AGENT_SESSION_ID -- identity, not label, since the inbox this
reminder points at is keyed on identity. Silent no-op unless it is set, so
unrelated Claude Code sessions on the same machine never see this reminder.

Python since 2026-08-08 (promoted from a project-vendored port; see
`wake_waiter.py`'s docstring for the full runtime-dependency rationale
shared by all four reminder/wake hooks in this plugin).
"""

from __future__ import annotations

import json
import os
import sys


def _read_stdin_event_name(default: str) -> str:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001 -- malformed/absent stdin, fall through
        return default
    if isinstance(payload, dict):
        event_name = payload.get("hook_event_name")
        if isinstance(event_name, str) and event_name:
            return event_name
    return default


def main() -> int:
    session_id = os.environ.get("AGENT_SESSION_ID", "").strip()
    if not session_id:
        return 0
    event_name = _read_stdin_event_name("UserPromptSubmit")
    output = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": (
                "Unread coordination messages from other sessions may be "
                "pending, if this project uses a peer-messaging or "
                "shared-inbox mechanism."
            ),
        },
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
