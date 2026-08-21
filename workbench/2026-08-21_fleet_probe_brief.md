# Fleet Probe Brief — validate spawned-worker hook delivery

**Date:** 2026-08-21
**Purpose:** Confirm that a spawned worker inherits the coordination-hooks
PLUGIN hooks (registration, heartbeat, session mapping) despite the
`spawn_session` preflight refusing on `strictPluginOnlyCustomization: ["hooks"]`.

The preflight assumes worker hooks arrive via the driver's `--settings` blob and
are therefore stripped. They no longer do: `coordination-hooks` is enabled at
user scope in `~/.claude/settings.json` and registers SessionStart, PreToolUse
and PostToolUse hooks from `~/.claude/plugins/cache/`. Plugin hooks are exactly
what that policy permits.

**Task:** reply with the single word ACK, then stop. Nothing else.

**Acceptance:** the worker appears in `peer_list` with a live registration and a
heartbeat — which is only possible if its hooks ran.
