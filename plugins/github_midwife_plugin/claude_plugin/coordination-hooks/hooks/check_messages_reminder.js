#!/usr/bin/env node
"use strict";

// SessionStart + UserPromptSubmit hook. Reads the hook_event_name Claude Code
// passes on stdin so the same script serves both events without guessing which
// one fired.
//
// ARM CONDITION: AGENT_SESSION_ID present — IDENTITY, not label. This is a
// FUNCTIONAL precondition, never a protection: the inbox is keyed on identity,
// so without one there is no addressable inbox and the reminder would advertise
// an action that cannot resolve. Re-keyed from AGENT_SESSION_LABEL on
// 2026-08-01; the label was never what made the inbox reachable.

const sessionId = process.env.AGENT_SESSION_ID;
if (!sessionId) {
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
