#!/usr/bin/env node
"use strict";

// Synchronous stock-Codex Stop hook. It carries one bit only: whether this
// session's watcher has received new coordination deliveries while the model
// was idle. Message content remains in the durable inbox.

const fs = require("fs");
const { spawn } = require("child_process");

const WAKE_SIGNAL = 2;
const NOTE_PREFIX = "[coordination-hooks wake]";
const NUDGE =
  "New coordination deliveries are pending for this session; their durable " +
  "contents remain unread in this turn.";

function emit(value) {
  process.stdout.write(JSON.stringify(value) + "\n");
}

function emitNoop() {
  emit({});
}

function diagnostic(message) {
  process.stderr.write(`${NOTE_PREFIX} ${message}\n`);
}

// BOUNDED WAIT (ported 2026-08-12 from claude_plugin's wake_waiter.py, where
// it landed 2026-08-09). The wait is capped instead of inheriting the CLI's
// own ~23.9h default: a Stop hook that blocks for the CLI's full default
// holds this session's turn boundary for the whole quiet stretch — starving
// any host-side machinery that needs to observe the session idle, and
// leaving a near-day-long child running for a session that may have nothing
// to say. The cap stays inside the ~1h prompt-cache TTL so a quiet session
// releases its boundary while still warm; a delivery still wakes the
// session immediately — the cap only bounds the QUIET case. Override
// per-session with AGENT_WAKE_MAX_WAIT_S; a malformed or non-positive
// override is announced on stderr and falls back to the default — never
// silently, and raw environment text never reaches the argv.
const DEFAULT_MAX_WAIT_S = 2400;
const MAX_WAIT_ENV = "AGENT_WAKE_MAX_WAIT_S";

function resolveMaxWaitS() {
  const raw = (process.env[MAX_WAIT_ENV] || "").trim();
  if (!raw) {
    return DEFAULT_MAX_WAIT_S;
  }
  if (!/^-?[0-9]+$/.test(raw)) {
    diagnostic(
      `${MAX_WAIT_ENV}=${JSON.stringify(raw)} is not an integer; using ${DEFAULT_MAX_WAIT_S}s`
    );
    return DEFAULT_MAX_WAIT_S;
  }
  const value = parseInt(raw, 10);
  if (value <= 0) {
    diagnostic(
      `${MAX_WAIT_ENV}=${value} is not positive; using ${DEFAULT_MAX_WAIT_S}s`
    );
    return DEFAULT_MAX_WAIT_S;
  }
  return value;
}

function readStopPayload() {
  let value;
  try {
    value = JSON.parse(fs.readFileSync(0, "utf8"));
  } catch (_error) {
    return null;
  }
  if (
    value === null ||
    Array.isArray(value) ||
    typeof value !== "object" ||
    value.hook_event_name !== "Stop" ||
    typeof value.stop_hook_active !== "boolean"
  ) {
    return null;
  }
  return value;
}

const payload = readStopPayload();
if (payload === null) {
  diagnostic("invalid Stop hook input; wake wait skipped");
  emitNoop();
  process.exit(0);
}

// Codex invokes Stop again after a continuation. Never wait on that second
// invocation or one delivery could create an unbounded continuation loop.
if (payload.stop_hook_active) {
  emitNoop();
  process.exit(0);
}

const sessionId = process.env.AGENT_SESSION_ID;
const cli = process.env.AGENT_WAKE_CLI;
const transport = process.env.FLEET_TRANSPORT;

// Armed on unset/empty/"watch" FLEET_TRANSPORT, disarmed on any OTHER
// declared value (e.g. "mcp") -- the same rule claude_plugin's wake_waiter.js
// pins (Architect-ruled 2026-07-31: empty is not a declaration, so it arms).
// This hook originally diverged on purpose, disarming on unset to avoid a
// double-wake against the then-live patched-Codex MCP wake path. That
// rationale retired with the patch-the-application pipeline itself
// (fleet-watch-transport-migration, codex-watch-migration lane, 2026-08-06):
// stock Codex has no other wake mechanism once the patch is gone, so an
// unset transport must arm this hook, not silently leave the session
// unwakeable.
if (!sessionId || !cli || (transport && transport !== "watch")) {
  emitNoop();
  process.exit(0);
}

let child;
try {
  child = spawn(cli, ["wake", "--max-wait", String(resolveMaxWaitS())], {
    shell: false,
    stdio: ["ignore", "ignore", "ignore"],
  });
} catch (_error) {
  diagnostic("configured wake CLI could not be started");
  emitNoop();
  process.exit(0);
}

let settled = false;
let cancelling = false;
let forceTimer;
const handledSignals = ["SIGTERM", "SIGINT", "SIGHUP"];

function removeSignalHandlers() {
  for (const signal of handledSignals) {
    process.removeListener(signal, cancel);
  }
  if (forceTimer) {
    clearTimeout(forceTimer);
  }
}

function cancel(signal) {
  if (settled || cancelling) {
    return;
  }
  cancelling = true;
  if (!child.pid) {
    process.exit(0);
  }
  try {
    child.kill(signal);
  } catch (_error) {
    process.exit(0);
  }
  forceTimer = setTimeout(() => {
    try {
      child.kill("SIGKILL");
    } catch (_error) {
      process.exit(0);
    }
  }, 1000);
}

for (const signal of handledSignals) {
  process.once(signal, cancel);
}

child.once("error", () => {
  if (settled) {
    return;
  }
  settled = true;
  removeSignalHandlers();
  if (cancelling) {
    process.exit(0);
  }
  diagnostic("configured wake CLI could not be started");
  emitNoop();
  process.exit(0);
});

child.once("close", (status) => {
  if (settled) {
    return;
  }
  settled = true;
  removeSignalHandlers();
  if (cancelling) {
    process.exit(0);
  }
  if (status === WAKE_SIGNAL) {
    emit({ decision: "block", reason: NUDGE });
    process.exit(0);
  }
  if (status !== 0) {
    diagnostic("wake CLI ended without a supported status");
  }
  emitNoop();
  process.exit(0);
});
