#!/usr/bin/env node
"use strict";

// Stop hook — the idle-wake half of session coordination, nudge-only.
//
// When this session goes idle, this hook invokes the operator-configured
// coordination CLI's blocking wait verb: exactly `$AGENT_WAKE_CLI wake`,
// fixed argv, no shell. That command blocks (at zero model cost) until a
// coordination delivery arrives for this session, then exits with the
// Claude Code hook wake code (2).
//
// DELIBERATELY DISCARDS the CLI's output. The child's stdout/stderr are
// dropped unread; on the wake signal this hook emits its own compiled-in
// fixed nudge instead. The hook therefore conveys exactly ONE BIT of
// dynamic information — "deliveries are pending" — plus timing. Message
// CONTENT enters the session only when the model explicitly fetches it
// from the peer inbox (the durable store; the watcher's spool is a tee of
// notifications, so discarding it here loses nothing).
//
// Armed ONLY when ALL of the following hold (silent no-op otherwise):
//   - AGENT_SESSION_LABEL is set (this is a labeled coordination session);
//   - AGENT_WAKE_CLI is set (the operator's launcher names the CLI to run);
//   - FLEET_TRANSPORT is unset or "watch" (a declared non-watch transport,
//     e.g. "mcp", disarms this hook even if the CLI variable is exported —
//     on those transports the live bridge connection does the waking).
//
// Exit contract: child exit 2 (wake) -> fixed nudge on stderr + exit 2.
// Child exit 0 (idle expiry / not a fleet session / another waker armed)
// -> silent exit 0. Anything else (including spawn failure) -> exit 0 with
// a one-line fixed-format note carrying only the numeric status — a broken
// wake path must never trap the session in a failing Stop hook.

const label = process.env.AGENT_SESSION_LABEL;
const cli = process.env.AGENT_WAKE_CLI;
const transport = process.env.FLEET_TRANSPORT;

if (!label || !cli || (transport && transport !== "watch")) {
  process.exit(0);
}

const WAKE_SIGNAL = 2;
const NUDGE =
  "While this session was idle, its coordination watcher received one or " +
  "more new peer-message deliveries. Durable copies are preserved in this " +
  "session's peer-message inbox and have not yet been read here.";

const { spawnSync } = require("child_process");
const result = spawnSync(cli, ["wake"], {
  stdio: ["ignore", "ignore", "ignore"],
});

if (result.error) {
  process.stderr.write(
    `[coordination-hooks wake] could not run the configured wake CLI: ${result.error.code || "spawn error"}\n`,
  );
  process.exit(0);
}

if (result.status === WAKE_SIGNAL) {
  process.stderr.write(NUDGE + "\n");
  process.exit(WAKE_SIGNAL);
}

if (result.status !== 0) {
  process.stderr.write(
    `[coordination-hooks wake] wake CLI exited with status ${result.status}\n`,
  );
}
process.exit(0);
