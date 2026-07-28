#!/usr/bin/env node
"use strict";

// SessionStart + UserPromptSubmit hook. Silent no-op unless AGENT_SESSION_LABEL
// is set. Reads the hook_event_name Claude Code passes on stdin so the same
// script serves both events without guessing which one fired.

const label = process.env.AGENT_SESSION_LABEL;
if (!label) {
  process.exit(0);
}

let raw = "";
try {
  raw = require("fs").readFileSync(0, "utf8");
} catch {
  raw = "";
}

let eventName = "UserPromptSubmit";
try {
  const payload = JSON.parse(raw);
  if (payload && typeof payload.hook_event_name === "string" && payload.hook_event_name) {
    eventName = payload.hook_event_name;
  }
} catch {
  // Malformed/absent stdin: fall through with the default event name.
}

const output = {
  hookSpecificOutput: {
    hookEventName: eventName,
    additionalContext:
      "Unread coordination messages from other sessions may be pending, " +
      "if this project uses a peer-messaging or shared-inbox mechanism.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
