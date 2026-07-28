#!/usr/bin/env node
"use strict";

// UserPromptSubmit hook. Silent no-op unless AGENT_SESSION_LABEL is set, so
// unrelated Claude Code sessions on the same machine never see this reminder.
// No shell involved (exec-form invocation from hooks.json) and no runtime
// dependency beyond Node itself, which Claude Code already requires to run.

const label = process.env.AGENT_SESSION_LABEL;
if (!label) {
  process.exit(0);
}

const output = {
  hookSpecificOutput: {
    hookEventName: "UserPromptSubmit",
    additionalContext:
      "For non-trivial work, a persistent knowledge base available to " +
      "this session (via a local CLI or a connected MCP tool, if any) " +
      "and the current project's own docs (e.g. CLAUDE.md/AGENTS.md, " +
      "if present) often already have an answer — checking either is " +
      "usually faster than re-deriving one. Any such lookup may be " +
      "asynchronous; its result is not required before proceeding.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
