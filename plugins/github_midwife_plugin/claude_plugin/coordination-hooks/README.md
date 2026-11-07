# coordination-hooks

Fleet coordination hooks for multi-session Claude Code workflows:

- **Knowledge-base-first reminder** (`SessionStart`) — suggests checking
  this project's documented conventions before non-trivial work. A reminder,
  not an enforced gate: the underlying search this suggests is asynchronous,
  so the hook cannot block on its result. Unconditionally armed — no
  environment variable disarms it, alongside the live-context hook below — so a
  session never silently loses awareness that the mechanism exists.
- **Live session context** (`session_start_context.py`, SessionStart) — bounded register-wide digest, current roles and observer inbox counts; explicit unavailable lines on failure. See the read and timeout contract below.
- **Idle-wake waiter** (`Stop`) — opt-in per session (armed by environment
  variables, see Configuration), but the fleet this plugin ships with runs
  on it as the PRIMARY idle-wake mechanism as of 2026-08-06, not a
  fallback — see `SECURITY.md`'s "Operational status" note. Nudge-only.
  When a session with
  an `AGENT_SESSION_ID` goes idle, invokes exactly
  `$AGENT_WAKE_CLI wake --max-wait <seconds>` (fixed argv, no
  shell) — the operator-configured coordination CLI's blocking wait verb,
  bounded so the quiet case releases the turn boundary (see Configuration) —
  and **discards its output entirely**. On the wake signal (exit 2) it
  emits its own fixed nudge: deliveries are pending in the peer inbox. The
  session then fetches the actual messages itself via the normal tool
  path. The hook conveys exactly one bit of dynamic information —
  "deliveries pending" — plus timing; it never relays message content.
- **Heartbeat report-alive** (`PostToolUse`, unconditional) — opt-in per
  session via its owner receipt and `AGENT_INSTANCE_ID`. After each tool call
  it takes a worktree attestation (three git read commands in the session's
  working directory, snapshot kept in worktree-resync.json), and at most once
  per ~180s it shells out to the fleet's `report_alive` CLI verb so a
  fleet-managed session's liveness stays
  current without a standalone background loop — the loop shape this hook
  replaced could survive a session `/clear` and keep reporting a dead
  session as live for hours, since it had no coupling to whether the process
  it vouched for was still doing anything. This hook has no persistent
  process of its own; it dies with the tool-call lifecycle it fires inside.
  Failed calls are kept in failures.json and reported on the next successful
  heartbeat.
  Conveys no dynamic information back to the session — it never prints a
  `hookSpecificOutput` block, only an occasional stderr diagnostic on
  failure. See `SECURITY.md` for the full contract.
- **Rotation-due watch** (`PostToolUse`, unconditional) — opt-in per session
  via `AGENT_INSTANCE_ID`. The host-independent sibling of the heartbeat:
  reads the session's own transcript usage, compares it against a declared
  context ceiling, and on a threshold crossing sends ONE notification (via
  `peer_send_by_name` to the session's steward, or a local marker file when
  no steward is resolvable) — at most once per session generation. It never
  acts on the signal itself (never clears or rotates the session); rotation
  timing stays a steward/operator decision. Also opportunistically caches a
  lightweight context-status reading on every un-throttled tick, independent
  of the threshold check. Like the heartbeat, it prints nothing to stdout;
  diagnostics are stderr-only. See `SECURITY.md` for the full contract.
- **Installed-owner receipt guard** (`coordination_owner.py`, imported by the
  heartbeat, rotation-due, and wake entrypoints) — validates an immutable,
  owner-qualified installation receipt before any marker, lock, watcher, or
  bridge side effect. Missing, foreign, stale, corrupt, or undesignated roots
  refuse explicitly and inertly; the absolute receipt path is supplied as
  `AGENT_COORDINATION_RECEIPT_PATH`, never guessed from CWD.
- **Rotation-due notice** (`UserPromptSubmit`, unconditional) — the delivery
  half of the hook above, for the session it cannot deliver to. When the
  watch hook finds no steward to notify (an operator-present session, which
  has none), it writes the notice to a local marker instead of dropping it;
  this hook surfaces that marker as prompt context on the session's own next
  turn, once per session generation, then stamps the marker so it never
  repeats. It is the only hook here that prints a `hookSpecificOutput`
  block, so what it will inject is bounded: content comes from a marker file
  this hook checks is owned by the current user and not group/world-writable,
  and is otherwise carried verbatim. On the ordinary path — no marker — it
  reads stdin, globs at most two directories, and emits nothing. See
  `SECURITY.md` for the full contract.
- **Memory-passthrough capture** (`PostToolUse`, `Write|Edit|MultiEdit`) —
  when the touched file is under this agent's own memory directory,
  appends one journal record (path, hash, timestamp) locally. No content
  is emitted or transmitted; this is a local append-only log entry, never
  a network call.
- **Memory-passthrough session context** (`SessionStart` + `UserPromptSubmit`)
  — when the agent's memory directory exists, prints a fixed instruction
  template telling the agent exactly what to run to hydrate its local
  memory projection from canonical storage, or to drain locally-captured
  edits back to it. A hook subprocess has no platform bridge of its own,
  so this is how the loop reaches the agent at all — the agent then runs
  the commands itself via Bash.
- **Memory-passthrough CLI utilities** (`drain.py`, `hydrate_render.py`,
  `index_render.py`, `sync.py`) — NOT Claude Code hooks; none fire
  automatically. Agent-invoked directly via Bash, on the agent's own
  judgment, following the session-context hook's own printed
  instructions. `drain.py` reads the pending local journal and prints the
  exact upsert arguments for each entry; `hydrate_render.py` regenerates
  the local memory projection from an already-exported snapshot;
  `index_render.py` renders the budget-fitted `MEMORY.md` index (shared
  library for `hydrate_render.py`, plus its own standalone entry point);
  `sync.py` wraps hydrate/drain into one Bash call per direction and is
  the one CLI utility in this group that shells out, via the same
  PATH-resolved `solet` convention as the rotation-due watch hook (the
  heartbeat calls `solet-bridge` the same way). See `SECURITY.md` for the full per-file contract.
- **Spawn-injected worker hooks** (`headless_tool_allowlist_gate.py`,
  `capture_session_mapping.py`) — NOT wired into this plugin's own
  `hooks/hooks.json` at all; a spawned headless/tmux worker's own host
  adapter (`agent_messaging_plugin`) references these files by path in a
  generated Claude Code `--settings` blob at spawn time, and they ship
  here purely as the fallback copy a born clone (no `.claude/hooks/` of
  its own) still carries. `headless_tool_allowlist_gate.py`
  (`PreToolUse`) enforces a spawned worker's spawn-time tool allowlist —
  unarmed by default, and FAIL-CLOSED (blocks on any parse error) when a
  caller does supply one, since this is the actual safety boundary for an
  unattended worker with no human to catch a hook bug.
  `capture_session_mapping.py` (`SessionStart`) writes one file-per-firing
  spool record mapping this worker's `AGENT_INSTANCE_ID` to its Claude
  Code `session_id`, for the platform's own session-ledger reconciliation
  — non-fatal by design, like the memory-passthrough capture hook. See
  `SECURITY.md` for the full contract of each.

## SessionStart cadence

The knowledge-base reminder and live session context fire on startup, resume
and clear, never UserPromptSubmit. The memory-passthrough session-context hook
keeps its separate existing SessionStart and UserPromptSubmit bindings.

## Configuration

The knowledge-base and live-context hooks always run. The latter explicitly
reports missing read configuration. The idle-wake waiter keys on
`AGENT_SESSION_ID`; the heartbeat keys on its owner receipt with
`AGENT_INSTANCE_ID` and `AGENT_SESSION_ID`, and the rotation-due watch hook
keys on `AGENT_INSTANCE_ID` (its throttling additionally reads
`AGENT_HEARTBEAT_MARKER_DIR` when set — see `SECURITY.md` for the fail-open
behavior when it is absent but the instance id is present); the
memory-passthrough capture and session-context hooks key on whether this
agent's own memory directory exists at all — no environment variable
arms or disarms either (a bare session in a project with no memory
directory sees zero output/effect from both). The two spawn-injected
worker hooks key on their own adapter-supplied variables:
`headless_tool_allowlist_gate.py` enforces only when
`FLEET_HEADLESS_TOOL_ALLOWLIST` is set (even to an empty string);
`capture_session_mapping.py` writes only when both
`ANANTA_SESSION_MAPPING_SPOOL_DIR` and `AGENT_INSTANCE_ID` are set —
neither is meaningful outside a spawned worker's own generated settings,
so an ordinary interactive session never arms either.

The idle-wake waiter is separately opt-in via `AGENT_WAKE_CLI`: unset, it is
fully OFF. Set it to the coordination CLI the hook should invoke; the hook
runs exactly that executable with a fixed argument vector — the literal
subcommand `wake`, the literal flag `--max-wait`, and the bounded wait in
seconds (the compiled-in default, or `AGENT_WAKE_MAX_WAIT_S` when it parses
as a positive integer; anything else is announced on stderr and falls back,
so raw environment text never reaches the argv) — no shell involved. The
bound is what lets the harness stamp the session idle at all; a delivery
still wakes the session immediately (see `wake_waiter.py`'s module
docstring for the measured incident behind it). A declared non-watch transport (`FLEET_TRANSPORT` set to any
**non-empty** value other than `watch`) disarms it even when the CLI variable
is set, so deployments whose live bridge connection already does the waking
never double-arm. An unset **or empty** `FLEET_TRANSPORT` is not a
declaration and leaves the waiter armed — a hydration bug that exports an
empty value must not silently kill wakes on a watch deployment.

## Design notes

- Every hook uses exec-form invocation (`command` + a separate `args` array)
  — no shell string interpolation, no injection surface.
- Every hook in this plugin is stdlib-only Python (`python3`) — no
  interpreter discovery, no vendored binary, no runtime install step,
  except one disclosed, narrow exception: `rotation_due_watch.py` imports
  `agent_messaging_plugin.rotation_thresholds`, a zero-third-party-
  dependency SAME-PLATFORM module (not a PyPI package) that ships alongside
  this plugin in every capability bundle, resolved via `CLAUDE_PROJECT_DIR`
  and imported inside a `try`/`except` that degrades gracefully (skips the
  threshold computation, never crashes) if it is ever absent. The
  memory-passthrough CLI utilities import each other directly
  (`hydrate_render.py` imports `index_render`; `sync.py` imports `drain`
  and `hydrate_render`) to avoid an extra process layer — local modules
  within this same plugin, not a new external dependency, same reasoning
  as any hook importing its own underscore-prefixed sibling. `python3`
  itself is a guaranteed platform prerequisite, unlike this plugin's prior
  Node implementation of four of these hooks, which depended on a runtime
  nothing guaranteed and could silently fail to launch with no signal
  Claude Code surfaces to the user (measured 2026-08-08; retired the same
  day).
- Hooks have no direct network/database client. Five files invoke local
  children: session_start_context.py (read-only register/inbox), wake_waiter.py,
  heartbeat_report_alive.py, rotation_due_watch.py and sync.py. Existing
  marker/memory/spawn effects remain as documented in SECURITY.md.
- Context sources are the fixed knowledge-base reminder, bounded live register
  and inbox observations, and the existing qualified memory/rotation notices.
  No environment-authored instruction channel or message-body relay is added.

## Retired behaviours (inventory, not a replacement claim)

This section records what this plugin used to refuse and no longer does. It is
a statement of removal, kept apart from everything the plugin provides today:
none of the three behaviours below is armed, evaluated or checked by any hook
shipped here. The git-text gate (`git_controller_gate.py` and its policy
modules) was removed in `0.9.0` by operator ruling `rul_e71e43be` (commit
`91b8ace14`). Deleting the hook did not supply a mechanism for any of the three
bans; whatever is meant to take over git control is separate work and is not
delivered by this plugin.

| Retired behaviour | What the gate refused | Enforced today | Covering mechanism today |
|---|---|---|---|
| Git mutation ban | A mutating `git` command in a `Bash` tool call (commit, push, checkout, merge, rebase and the like), and an `Edit`/`Write`/`MultiEdit`/`NotebookEdit` of the repository's own `.git/`, from any session other than the one named in `GIT_CONTROLLER_NAME`. Armed only when that variable was exported; fail-open when unset. | No | None in this plugin. Git control among sessions sharing a directory is the repository's own responsibility (`rul_d8893e33`): name the one git-mutating role by convention and tell the operator that no hook enforces it (hydration runbook, Step 4a). |
| Agent and Task spawn ban | The `Agent` tool, and `Task` under its former name, from any session other than the named controller, so that a peer could not spawn a subagent that mutates git. | No | None. `headless_tool_allowlist_gate.py` is a different control and not a replacement: it constrains only spawned headless workers whose spawning call supplied an explicit `allowed_tools` list, ships unarmed otherwise, and never applies to an interactive session. |
| Git beside a shell evaluator word or an untokenisable construct | A `Bash` command naming `git` that the policy could not safely inspect, refused fail-closed. The origin repository's own copy of the gate (`64e9810fb`, never shipped) went further: for every role, the controller included, it refused `git` beside a shell evaluator word (`bash`, `sh`, `zsh`, `dash`, `ksh`, `fish`, `eval`, `source`, `exec`) or an unreadable shell construct (ANSI-C quoting, backslash-newline, here-string, process substitution, backtick), and `git` its tokeniser could not parse. | No | None. A command of this shape is no longer refused or flagged by any hook. |

`GIT_CONTROLLER_NAME` is still rendered into the hydration launchers when an
operator chooses a name, but since `0.9.0` no shipped hook reads it: exporting
it arms nothing and leaving it unset exempts nothing.

## Security notes

`SECURITY.md` (same directory) enumerates the complete data-flow surface —
inputs, outputs, no-network/no-secrets/no-subprocess guarantees, the
threat-model boundary and its accepted bypasses, failure modes with the
allow-on-error rationale, and the supply-chain posture. It is written to be
read by a security reviewer before the code.

## Live session context — reviewer due diligence (2026-10-08)

`session_start_context.py` replaces the static message and role reminders on
SessionStart `startup`, `resume`, and `clear`. It performs two read operations:

- `[PROJECT_SOLET_CLI, "resume-digest", "--limit", "1"]` reads the bounded,
  **register-wide** digest. The default executable is
  `$HOME/Workspace/project-solet/.venv/bin/psolet`; installations elsewhere
  set `PROJECT_SOLET_CLI` to one executable path. The child resolves its normal
  `PROJECT_SOLET_DATABASE_DSN` or Keychain credentials; the hook neither reads
  credentials itself nor prints child stderr. Register previews are data,
  not instructions or a substitute for full authoritative reads.
- `[SOLET_BRIDGE, "call", "plugin::agent_messaging_plugin::peer_inbox", JSON]`
  pages the instance and role sections with `observer: true`. `SOLET_BRIDGE`
  defaults to `solet-bridge` on PATH. The original `SOLET_NAME`,
  `AGENT_SESSION_ID` and `AGENT_INSTANCE_ID` are preserved and required.
  Current bound role names come from `role_read_coverage`, never the local
  label. Forward instance cursors preserve timestamp ties; role cursors are
  independent. The hook never acknowledges a page or advances a covered mark.

The role count means **pending above role covered mark (display/consumption
unknown)**. Observer mode includes display-receipted rows; it cannot prove
unread state. The instance count is stored-message history, explicitly labelled
**unread: unknown (no platform unread semantics)**. The hook fetches and
JSON-parses the complete peer_inbox response, including message objects and
their body fields. For inbox data it injects only derived counts, current
bound-role labels, and observation/error text; it never injects message bodies.
Paging produces an observation over an interval, not an atomic cross-page
snapshot; a binding change, repeated cursor, partial role failure or identity
mismatch makes the count unavailable.

The digest child has a three-second timeout, and all inbox pages share a
separate three-second deadline. The manifest timeout is eight seconds. A failed
read adds one explicit `unavailable (ErrorClass)` line and exits zero; a missing
identity is `MissingIdentity`, never zero messages. No cache or static reminder
fallback exists. JSON serialization escapes all observed strings. The digest
context is capped at 5800 UTF-8 bytes and inbox context at 1000, leaving room
inside 8192 for the unchanged knowledge-base reminder. Over-budget output is
reported as `ByteBudgetExceeded`, never silently clipped into apparent success.

This stdlib Python 3.13 hook uses `subprocess.run` with argument arrays and no
shell. It creates no files, performs no register writes, claims no role. Hook installation/trust is separate from candidate delivery;
these are same-user coordination controls, not a privilege boundary.

The manifest consistency smoke and the reminder hooks smoke cover this candidate's manifest
and fixed reminder; the live SessionStart subprocess smoke is added by slice S3 of this Change.
This candidate makes no claim of live host deployment.
The independent review unit records the due-diligence verdict required by
`rul_af7c73e3` and `rul_f0e51f93`.

The fixed knowledge-base reminder remains checked by `tests/reminder_hooks_smoke.py`.

## Session-cron sentinel

`session_cron_sentinel.py` observes CronCreate and ScheduleWakeup call attempts.
Attempt observed, success unknown: neither a tool response nor a backend receipt
proves schedule creation. This is the plan amendment under `uev_c545d677` to the
original response-contract clause. Fixtures are synthetic-input-schema-derived;
native response capture and a typed delay field remain owning-seat follow-ups.

The hook reads only stdin and AGENT_INSTANCE_ID. Missing identity or unsupported
input warns and exits 0. It creates no log, marker, cache, credential or session
file, reads no history, runs no model and never promotes a prompt to a duty.
Its only child is literal argv `["solet-bridge", "call",
"plugin::fleet_maintenance_plugin::session_cron_observation", <json>]`, with
shell=False, SESSION_CRON_BRIDGE_TIMEOUT_SECONDS (default 5 seconds), and
bounded rendered diagnostics. SESSION_CRON_MAX_INPUT_CHARS (default 65536
Unicode characters) bounds stdin and accepted receipt size. Invalid settings
warn without blocking. These settings bound RPC/input, never test batteries. Child
stdout is used only to validate the append-only receipt; child stderr is never
relayed. Receipt failure warns without blocking the originating tool call.
The process may persist an attempt even when the tool response reports failure.

The strict backend receives observation_kind=capture, tool_name, instance/session
provenance, aware hook receipt time, and the verbatim prompt. CronCreate supplies
verbatim tool_input.cron as cron_expression. ScheduleWakeup supplies
`delaySeconds=<n>` as wake_time: requested delay input text, never a computed
instant or response field. No outcome or observed_job_id_unverified field is sent.
recurring, durable, noop, stop and reason have no backend slot and are omitted;
noop/stop still signal attempts. Backend bounds and truncation flags still apply.
Corrections and withdrawals remain explicit backend/seat operations.

Root settings and shipped hooks.json each register one CronCreate|ScheduleWakeup
PostToolUse entry for their respective installation surface. Hydrated settings
only enable coordination-hooks and carry no inline hooks. These surfaces are
alternative installation routes, not two copies to install for the same event.
The hook is label-independent; missing identity is visible rather than silent.
No installation, live capture, schedule mutation or activation is proved here.
`plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/session_cron_sentinel_smoke.py` covers the synthetic attempt inputs,
bridge failures, literal argv, and checkout root/twin byte parity. In an isolated
plugin artifact, the checkout-only root parity leg is unavailable and skipped.
Package plugin.json metadata is outside this Unit's fence; the sentinel inventory
is maintained in README.md, SECURITY.md and hooks.json instead.
