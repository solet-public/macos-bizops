#!/usr/bin/env node
"use strict";

// SessionStart hook. Silent no-op unless AGENT_SESSION_LABEL is set, so
// unrelated Claude Code sessions on the same machine never see this reminder.
// No shell involved (exec-form invocation from hooks.json) and no runtime
// dependency beyond Node itself, which Claude Code already requires to run.
//
// States a property of the environment; it does not instruct. The label is the
// only interpolated value and it comes from the process environment, never from
// stdin or from any message content.

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

let eventName = "SessionStart";
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
      "This session was launched with the role label " +
      JSON.stringify(label) +
      ". Where sessions are bound to durable role names, that binding lives " +
      "outside the session, so the local label and the external binding are " +
      "separate things and can disagree — a binding made before a /clear, a " +
      "restart, or a transport reconnect may still point at a previous " +
      "session, and messages addressed to the role would then route there. " +
      "Sessions in this environment typically re-assert their role binding at " +
      "session start, if this project provides a mechanism for it. A listing " +
      "that merely shows a session as present is evidence of presence, not of " +
      "a held claim.",
  },
};

process.stdout.write(JSON.stringify(output) + "\n");
