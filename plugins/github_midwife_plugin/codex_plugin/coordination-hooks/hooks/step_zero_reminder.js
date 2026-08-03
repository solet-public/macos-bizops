#!/usr/bin/env node
"use strict";

// UserPromptSubmit hook. ALWAYS ARMED: installed means armed, with no
// environment condition of any kind.
//
// The homunculus is a system-wide resource, so awareness of it is not
// fleet-only. This hook previously no-op'd unless AGENT_SESSION_LABEL was set;
// that gate was removed deliberately (operator ruling 2026-08-01), and the
// FAILURE DIRECTION INVERTED WITH IT: a silently disarmed awareness reminder
// means a session never learns the platform exists, which is the silent-absence
// class. Re-adding any env condition here is the red mutation for this hook's
// smoke leg.
//
// The literal below is true wherever the plugin is installed: it names no
// deployment-relative path and no fleet-specific command, so it reads
// correctly in an arbitrary directory with zero fleet context. The emitted
// context is one compiled-in literal: it never includes the prompt, a
// message, a lookup result, or an environment value.
//
// No async-non-blocking clause here, unlike the Claude sibling hook: this
// platform's Codex-side lookup path is the CLI `homunculus call` form (see
// AGENTS.md's Step Zero), which blocks/polls for its result before
// returning (agent_messaging_plugin/local_cli/cli.py's call_and_wait) — a
// genuinely synchronous mechanism, not drift from the Claude wording.

const output = {
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext:
      "For non-trivial work, checking a persistent knowledge base " +
      "available to this session (via a local CLI or a connected tool, " +
      "if any) and the current project's own instruction files before " +
      "other work is usually faster than re-deriving an answer partway " +
      "through.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
