#!/usr/bin/env python3
"""UserPromptSubmit hook. ALWAYS ARMED: installed means armed, with no
environment condition of any kind.

The homunculus is a system-wide resource, so awareness of it is not
fleet-only. This hook previously no-op'd unless AGENT_SESSION_LABEL was set;
that gate was removed deliberately (operator ruling 2026-08-01), and the
FAILURE DIRECTION INVERTED WITH IT: a silently disarmed awareness reminder
means a session never learns the platform exists, which is the silent-absence
class. Re-adding any env condition here is the red mutation for this hook's
smoke leg.

The literal below is true wherever the plugin is installed: it names no
deployment-relative path and no fleet-specific command, so it reads correctly
in an arbitrary directory with zero fleet context. Deployment-specific
how-and-where lives in the user-scope instructions section, not here.

No shell involved (exec-form invocation from hooks.json). Runtime
dependency: python3, a guaranteed platform prerequisite -- unlike this
hook's prior Node implementation, which depended on a runtime nothing
guaranteed (see `wake_waiter.py`'s docstring; promoted 2026-08-08).
"""

from __future__ import annotations

import json
import sys

output = {
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": (
      "For non-trivial work, checking a persistent knowledge base "
      "available to this session (via a local CLI or a connected MCP "
      "tool, if any) or the current project's own docs (e.g. "
      "CLAUDE.md/AGENTS.md, if present) before other work is usually "
      "faster than re-deriving an answer partway through. Such a lookup "
      "may run asynchronously -- its result can arrive after other work "
      "has already started, so there is no need to block on it once it "
      "is under way."
    ),
  },
}


def main() -> int:
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
