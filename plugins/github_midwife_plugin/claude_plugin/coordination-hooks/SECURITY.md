# Security notes — coordination-hooks

This page describes the reviewed data-flow surface. The plugin is fifteen Claude Code hooks/utilities:
one context reminders entry (`step_zero_reminder.py`), live session context
(`session_start_context.py`), `wake_waiter.py`,
`heartbeat_report_alive.py`, `rotation_due_watch.py`, `rotation_due_notice.py`,
`capture.py`, `session_context.py`, `drain.py`, `hydrate_render.py`,
`index_render.py`, `sync.py`, `headless_tool_allowlist_gate.py`, and
`capture_session_mapping.py`, and `session_cron_sentinel.py`. The worker hooks are also registered in hooks.json.
The memory utilities share `_journal.py`. Four hooks share `_session_identity.py`
(env-less session identity, described in its own section below).

Six hooks/utilities can execute children: live session context, wake waiter,
heartbeat, rotation-due watch, sync and the session-cron sentinel. The other nine hooks execute nothing.
The first is a read-only observer described below; the existing four retain
their documented effect and output boundaries.

**Operational status (fleet-watch-transport-migration phase 3, 2026-08-06):**
this plugin's wake waiter is the fleet's primary idle-wake mechanism, not an
optional convenience — it replaced an inline-shell Stop hook that predated
this plugin and could not pass the corporate-endpoint security review this
page exists to support. It is not a fallback path kept for open-source
strangers who might want it; strangers get the same reviewed mechanism the
fleet itself now depends on for delivery to idle sessions.

An operator-guidance channel (`operator_guidance.js`) shipped here until
2026-08-01, when the operator ruled it out of the plugin fleet-wide:
deployment-specific guidance belongs in the project's own instruction files
(`CLAUDE.md`), never in an environment-supplied variable a hook emits verbatim.
The live register/inbox observations added here are bounded data, disclosed
below, and do not replace authoritative full reads. The guidance channel's removal is
complete as of this page — no hook in this plugin emits environment-supplied
prose, and there is no longer a full-trust content class to describe here.

## Inputs and outputs — the complete data-flow surface

The heartbeat, rotation-due, and wake entrypoints first import
`coordination_owner.py`. For a managed session it requires
`AGENT_COORDINATION_RECEIPT_PATH` to be the fixed profile-data receipt for the
exact `SOLET_NAME`, selector, selected root, manifest, interpreter, and hook
bytes. A missing, corrupt, foreign, stale, symlink-escaping, or unrecorded
designation refuses before every marker, lock, watcher, lifecycle write, or
bridge call. This receipt detects installed-state drift; it is not a grant of
authority.

The heartbeat's state lives under the runtime root the receipt names, in
directories named by the SHA-256 of `SOLET_NAME`, `AGENT_INSTANCE_ID` and
`AGENT_SESSION_ID`. The receipt binds different files by surface: for an
installed plugin, the plugin's own `coordination_owner.py`; for a development
checkout, the checkout's .claude/hooks copies. There, `coordination_owner.py`
is a shim that loads the plugin verifier, so the shim, not the verifier, is
what the receipt hashes.

Inputs, exhaustively:

- **stdin**: the JSON payload Claude Code passes to every hook (event name,
  tool name, tool input, and — for the rotation-due watch hook only — its
  own `transcript_path`/`session_id`). Parsed defensively; malformed input
  degrades to a no-op (rotation-due watch, the
  memory-passthrough capture/session-context hooks) or an allow (gate). The
  heartbeat never reads stdin.
- **Environment variables**: the live context uses `PROJECT_SOLET_CLI`,
  `SOLET_BRIDGE`, `SOLET_NAME`, `AGENT_SESSION_ID`, `AGENT_INSTANCE_ID`, and
  `HOME`, as described below. It does not read `AGENT_SESSION_LABEL`.
  `AGENT_SESSION_ID` also arms the wake waiter;
  `AGENT_WAKE_CLI` (wake waiter on/off + the command it runs),
  `AGENT_WAKE_MAX_WAIT_S` (bounds the wake waiter's quiet-case wait; used
  only when it parses as a positive integer — anything else is announced
  on stderr and falls back to the compiled-in default, so raw environment
  text never reaches the argv),
  `FLEET_TRANSPORT` (disarms the wake waiter on a declared non-watch
  transport; unset and empty both mean "not declared" and leave it armed),
  `CLAUDE_PROJECT_DIR` (repo root — used to locate `.git/` for the
  write-protection check; by rotation-due watch to resolve the sibling
  `agent_messaging_plugin` module for threshold constants; and by every
  memory-passthrough file as the required root for deriving this agent's
  own memory directory and origin tag — see below), `AGENT_INSTANCE_ID`
  (required by the heartbeat and rotation-due watch hooks — a functional
  precondition: `report_alive`/`session_status` are both keyed on this id,
  never a protection), `AGENT_SESSION_ID` and `AGENT_COORDINATION_RECEIPT_PATH`
  (the heartbeat's owner receipt gate; `AGENT_CONTEXT_GAUGE_REPORTER_PATH`
  selects the heartbeat's hook root; `AGENT_WAKE_CLI` is appended to `PATH`
  only, see the heartbeat paragraph below), `AGENT_HEARTBEAT_MARKER_DIR`
  (rotation-due watch's throttle-marker directory; the heartbeat never reads
  it; when `AGENT_INSTANCE_ID` is set but this is not, rotation-due watch
  falls back to a fixed OS-temp-dir marker root — see the "heartbeat and
  rotation-due watch" section below), `SOLET_NAME` (rung 1 of the memory-passthrough origin-
  resolution ladder — see the memory-passthrough section below; read-only,
  also required by the live inbox observer), `FLEET_HEADLESS_TOOL_ALLOWLIST` (arms
  the spawn-injected tool-allowlist gate — set only by a spawning host
  adapter, never meaningful in an ordinary interactive session; see the
  spawn-injected worker hooks section below), `ANANTA_SESSION_MAPPING_SPOOL_DIR`
  (the spool directory the spawn-injected session-mapping capture writes
  to — also adapter-supplied only). `step_zero_reminder.py` reads no
  environment variable at all — see the Configuration surface section.
- **One local file read**: `~/.claude/sessions/<parent_pid>.json`, to
  resolve the calling session's name (see the identity caveat below).
- **Local state reads**: the heartbeat reads its owner receipt, the manifest
  and the four hook files that receipt hashes, its failure record and its
  worktree snapshot, and `stat()`s its throttle stamp and failure record. Its
  git reads are listed in the heartbeat paragraph below. Rotation-due watch
  `stat()`s its own throttle marker's mtime to decide whether to skip a
  tick, and additionally reads its own latch marker's existence and the
  calling session's **own** transcript file (never another session's) to
  compute current context usage.
- **Memory-passthrough reads** (`capture.py`, `session_context.py`,
  `drain.py`, `hydrate_render.py`, `index_render.py`, `_journal.py`):
  this agent's own memory directory (existence check, and its contents
  when rendering/journaling), this agent's own append-only journal file,
  this agent's own `root_manifest.yaml` (rung 2 of the origin-resolution
  ladder — a single `solet_name:` line, read via a minimal regex
  scan, never a full YAML parse — see the memory-passthrough section),
  and — `hydrate_render.py`/`sync.py` only — an already-exported JSON
  snapshot file path the AGENT supplies as an argument (the export itself
  is the agent's own `process_call`, never this plugin's own network I/O).
  Never another session's or another agent's files.

Outputs, exhaustively:

- **stdout**: `step_zero_reminder.py` emits a fixed knowledge-base reminder;
  `session_start_context.py` emits bounded live register data, measured counts
  and roles or explicit read-failure lines. `session_context.py` emits its
  existing instruction template over resolved paths and pending count.
  `rotation_due_notice.py` emits the qualified notice described below.
  Gate, heartbeat, rotation-due watch and capture have no context output.
  The four agent-invoked CLI
  utilities (`drain.py`, `hydrate_render.py`, `index_render.py`,
  `sync.py`) print their own result JSON to stdout when run — this is
  NEVER injected into a session's context by this plugin (no hook wiring
  exists for it), it is the direct return value of a command the agent
  itself chose to run.
- **stderr**: a block-explanation message when the gate blocks — a fixed
  template over the calling session's own already-supplied identity and a
  slice of its own command, path, or subagent-type argument, detailed in
  the trust-classes note below and in the Threat model section, never
  third-party or message content; the wake waiter's **fixed** nudge or
  fixed-format failure note (the numeric exit status is the only variable
  part; the child's own output is discarded unread); the heartbeat and
  rotation-due watch's fixed-template diagnostic lines on a missing
  env var, an unreadable transcript, or a failed `solet` call; the
  heartbeat's diagnostic lines, whose content classes are set out in its
  failure paragraph below — always best-effort telemetry, never read by
  anything, never blocking;
  `hydrate_render.py`/`sync.py`'s own fixed-template failure lines
  (malformed record, unreachable platform on export) on their own error
  paths, same non-blocking telemetry contract.
- **Local file writes**: the heartbeat writes three files under its
  receipt-derived identity directory (see the heartbeat paragraph below): a
  bare `str(time.time())` throttle stamp, a failure record, and a worktree
  snapshot (worktree-resync.json). Rotation-due watch writes a bare
  `str(time.time())` throttle/latch marker per `agent_instance_id` (and,
  rotation-due watch only, a self-notification marker on the
  no-steward-resolved path, carrying the hook's own generated notification
  text — still same-user, same-machine content the hook itself generated,
  never third-party input). The memory-passthrough files write more, by
  design: `capture.py` appends one journal record (path, hash, timestamp —
  never file CONTENT) to this agent's own journal; `hydrate_render.py`
  writes this agent's own per-fact `.md` files and `MEMORY.md`, content
  taken VERBATIM from an already-exported snapshot the agent itself
  supplied, never invented or read from stdin/tool-input; `index_render.py`
  writes `MEMORY.md` the same way when run standalone; `_journal.py`
  (imported by the above) writes the journal file, watermark, and
  hydrated-hash oracle used to detect the capture hook's own echo. Every
  write target across all six writer files is this agent's own
  already-resolved memory/state path under its own project — never
  another session's or another agent's path, never a path built from raw
  tool-call content. The spawn-injected `capture_session_mapping.py`
  writes a seventh, distinct kind of content record — a file-per-firing
  JSON mapping this worker's own `agent_instance_id` to the Claude Code
  `session_id` this firing carries on stdin — to the adapter-declared
  `ANANTA_SESSION_MAPPING_SPOOL_DIR`, never a path this hook derives
  itself; see the spawn-injected worker hooks section below for its full
  contract.
- **Exit codes**: `0` (allow / no-op — every hook in this plugin except the
  wake waiter and the spawn-injected tool-allowlist gate, always) or `2`
  (gate block; allowlist-gate block; wake signal). The four
  agent-invoked CLI utilities are not Claude Code hooks and have no
  Claude-Code-facing exit-code contract at all — their exit code is
  ordinary CLI convention (`0` success, non-zero failure) read only by the
  agent that invoked them.

**Context and authority are separate.** Only the knowledge-base reminder
is a fixed literal. Live session context carries measured register data and
role/count observations under the contract below. No hook treats a
local label as proof of a held role.

Beyond that: no network I/O of any kind in any hook (no HTTP, no sockets,
no DNS) — the hooks/utilities that shell out to `solet` (or, for the heartbeat, `solet-bridge`) do not
perform network I/O themselves; the `solet` CLI is a separate,
independently reviewable local process, same reasoning as the wake
waiter's configured CLI below. No hook writes a file as an action of its
own, **except the eight disclosed exceptions** — the heartbeat and
rotation-due watch hooks each write a bare-timestamp throttle/latch
marker (the heartbeat also writes its failure record and worktree
snapshot); `capture.py`/`hydrate_render.py`/`index_render.py`/`_journal.py`
write this agent's own memory/journal files, content sourced from an
already-exported snapshot or this agent's own already-resolved path,
never anything read from stdin, tool input, or another session (see
Outputs above and the memory-passthrough section below for the exact
per-file contract); `capture_session_mapping.py` writes a file-per-firing
session-mapping record to the adapter-declared spool directory (see the
spawn-injected worker hooks section below). One interpreter side effect is disclosed rather than
claimed away: a hook that imports the shared `_journal.py` library lets
CPython byte-compile it, so `hooks/__pycache__/*.pyc` can appear beside the
scripts. Nothing in the plugin reads those files back,
and an operator who wants them gone can set `PYTHONDONTWRITEBYTECODE=1` in
the session environment. The live-context hook delegates the register's credential lookup to psolet;
no credential or raw error output is injected. The other five subprocess
owners retain their fixed argv contracts described in their sections. The
other nine hooks execute nothing; file-writing effects remain those disclosed
above.

## The wake waiter — the one privileged hook, in full

**What it does.** On `Stop` (session going idle), when armed, it runs
exactly `$AGENT_WAKE_CLI wake --max-wait <seconds>` — the executable named
by the operator's environment, with a fixed argument vector: the literal
subcommand `wake`, the literal flag `--max-wait`, and the bounded wait in
seconds — the compiled-in default, or `AGENT_WAKE_MAX_WAIT_S` when it
parses as a positive integer (anything else is announced and falls back,
so no raw environment text ever enters the argv) — spawned directly with
**no shell** (no interpolation, no PATH tricks beyond ordinary executable
resolution). That command is expected to block until a coordination
delivery arrives for this session, then exit with the hook wake signal
(exit code 2); if the bounded wait expires quietly instead, it exits 0 and
the stop proceeds — the bound is what lets the harness stamp the session
idle at all (see the hook's module docstring), and a delivery still wakes
the session immediately. The waiter **discards the child's stdout and stderr
unread**; on the wake signal it emits its own compiled-in fixed nudge —
"deliveries are pending in the peer inbox" — and exits 2, which turns the
nudge into the session's next turn. The session then fetches the actual
messages itself through the normal tool path, where they arrive as
ordinary tool results. Discarding the child's output loses nothing: the
watcher's spool is a tee of notifications, and the durable copies live in
the coordination service's message store.

**Arming.** Three conditions, all required: `AGENT_SESSION_ID` set,
`AGENT_WAKE_CLI` set, and `FLEET_TRANSPORT` unset, empty, or `watch` — an
empty value is not a declaration, so it arms exactly as unset does (a
hydration bug exporting `""` must never silently kill wakes). Re-keyed from
`AGENT_SESSION_LABEL` on 2026-08-01: the label was only ever a proxy for
having a spool to wait on; the session id is what the spool path is
actually built from. Any ordinary Claude Code session — no session id, no
CLI variable — gets a silent no-op.

**Command provenance.** The command comes from the invoking user's own
environment, set by the same launcher configuration that labels the
session. It executes as that user, with that user's privileges — identical
in power to the operator typing the command themselves. The hook grants no
elevation and reaches no other trust domain. An attacker who can set this
environment variable can already run arbitrary commands as the user by
definition; the variable is a configuration point, not a privilege
boundary.

**Dynamic information — exactly one bit.** The waiter never relays message
content, and no output of the configured CLI can reach the session through
it. The only dynamic information this hook can convey is *that* deliveries
are pending (and, in its fixed-format failure notes, a numeric exit
status). Every string it can emit is a compiled-in literal visible in its
source — the same property the knowledge-base reminder has. Message *content* enters
the session only when the model explicitly fetches it from the peer inbox
via its own tool calls, in tool-result position, subject to the
coordination service's own controls — never as hook-injected context.
This is deliberate: it keeps the wake waiter's own nudge — like the
reminders it shares a turn-opening position with — a fixed literal, and it
keeps untrusted message text out of the turn-opening position entirely.
(The gate's block message is a different surface, held to its own
separately-stated guarantee described in the trust-classes note above — it
never opens a turn at all; it is stderr fed back after a blocked tool call.)

## The heartbeat and rotation-due watch hooks — the other two privileged hooks

**What they do.** Both fire on `PostToolUse`, unconditionally (every tool
call, no tool-name matcher), and both are throttled via a local marker
file's mtime. For the rotation-due watch, most firings take one `stat()` and
nothing more; a throttled tick costs no CLI or network round trip. The
heartbeat throttles only its `report_alive` call (its other work is below).

`heartbeat_report_alive.py`: on every firing outside its failure backoff it
first verifies its owner receipt, then takes a worktree attestation: three
`git` reads in the session's current directory (`rev-parse --show-toplevel`,
`rev-parse HEAD` and `status --porcelain=v1 --untracked-files=all -z`), each
run with `--no-optional-locks`, through `subprocess.run` with no shell, a 10 s
timeout and the inherited environment. The attestation runs before the
throttle check, so it runs on every such firing, and it then rewrites the
worktree snapshot. When the throttle allows (at most once per ~180 s per
`agent_instance_id`), it runs
`["solet-bridge", "call", "plugin::agent_messaging_plugin::report_alive", <payload>]`
(`subprocess.run`, no shell, a 20 s timeout), the fixed process key
`report_alive`. The payload is built from this session's own
`AGENT_INSTANCE_ID`, two fixed literals (`status: "heartbeat"` and
`status_note: "t2-posttooluse-heartbeat"`), a count of failures since the
last success and, after a recorded failure, its first-failure time and last
reason. It carries no tool-call content. git runs with the inherited
environment and honours that repository's and the user's git configuration,
as `git status` run there by hand would (for example a configured
core.fsmonitor helper). It has no persistent process
of its own: it only runs synchronously inside an actual tool-call
lifecycle, so an idle or dead session simply stops firing it — nothing is
left running to keep stamping liveness on a session's behalf after it goes
away, unlike a standalone background-loop design this hook was built to
replace (a prior loop shape survived a session `/clear` and kept reporting
a dead session as live for hours; disclosed in the hook's own module
docstring as the incident that shaped this design).

`rotation_due_watch.py`: when un-throttled (at most once per ~120s per
`agent_instance_id`), reads the calling session's **own** transcript file
(never another session's — the path comes from this session's own stdin
payload) to compute current context-token usage, then (a) unconditionally
caches that reading via one `report_context_status` call, and (b), only
once per session generation (latch-gated) and only if a declared threshold
is crossed, delivers ONE notification — `peer_send_by_name` to the
session's resolved steward role if one exists, else a local marker file —
built from a fixed template interpolating only this session's own
`agent_instance_id`, `session_label`, `model`, and measured token counts,
never tool-call content or another session's data. It never acts on the
signal: it does not clear, rotate, or otherwise mutate the session it
measures — rotation timing stays a steward/operator decision, stated
explicitly in the notification text itself.

**Arming.** The heartbeat is armed by its owner receipt gate, not by
`AGENT_INSTANCE_ID` alone. When none of the six managed variables
`AGENT_INSTANCE_ID`, `AGENT_SESSION_ID`, `AGENT_WAKE_CLI`,
`AGENT_COORDINATION_RECEIPT_PATH`, `SOLET_NAME` and
`AGENT_CONTEXT_GAUGE_REPORTER_PATH` is set, it is a silent no-op. When any is
set but `AGENT_INSTANCE_ID` or `AGENT_SESSION_ID` is missing, or the receipt
fails verification, each firing prints one `[coordination-owner]` stderr line
and exits `0`, before any marker, git or bridge call. Only a receipt that
passes lets it proceed, and its state then lives under the receipt-derived
runtime root, keyed by `SOLET_NAME`, `AGENT_INSTANCE_ID` and
`AGENT_SESSION_ID`. The heartbeat never reads `AGENT_HEARTBEAT_MARKER_DIR`:
the hook defines that constant and never uses it. Rotation-due watch
requires `AGENT_INSTANCE_ID` set; absent, it is a silent no-op (`session_status`
is keyed on this id, so there is nothing to watch without it — a functional
precondition, never a protection). `AGENT_HEARTBEAT_MARKER_DIR` affects its
*throttling*, not its arming: when `AGENT_INSTANCE_ID` is set but the marker
dir is not, it falls back to a fixed OS-temp-dir marker root so its throttle
and once-per-generation latch still work. That fallback exists because an
unlatched rotation-due notification would `peer_send` a steward on every
single completed tool call above threshold.

**Command provenance.** The rotation-due watch resolves `solet` via `PATH`,
the same convention this plugin's other subprocess-capable hook and this
checkout's other hooks already rely on for `python3`. Its command is literally
`solet`. The heartbeat resolves `solet-bridge` via `PATH` in the same way, with
one widening: when `AGENT_WAKE_CLI` names a file whose directory holds a
`solet-bridge` file, that directory is appended to `PATH` for the call
(appended, never prepended, so no existing resolution changes). Neither takes
an operator-configurable executable name (unlike the wake waiter's
`$AGENT_WAKE_CLI`). Only the JSON payload varies, built entirely from this
session's own environment and (for rotation-due watch) its own transcript
file, never from tool-call content or another session's input.

**Dynamic information conveyed.** Neither hook ever prints a
`hookSpecificOutput` block — no output reaches the session's context
through either of them, ever, success or failure. The heartbeat writes nothing
to stdout. Their only possible output is stderr diagnostics, never read by
anything (content classes in the heartbeat failure paragraph below), and, for rotation-due watch, at most one `peer_send_by_name` call
per session generation carrying this session's own already-known identity
and measured token counts to its own resolved steward — not to the
calling session's own context.

**Failure posture.** Both are non-fatal by design, the same contract as
every other hook in this plugin. The rotation-due watch's missing env var,
unreadable transcript, malformed stdin payload, unresolvable `solet`
binary, or failed `solet-bridge call` all warn to stderr and exit `0`.
Neither hook can ever cost a tool call, and neither uses exit code `2`.

**Heartbeat failure record.** A failed `report_alive` call (a non-zero exit,
a timeout, a launch error, or a rejected or unreadable response) and a
worktree attestation fault are each recorded in `<agent_instance_id>.failures.json`
under the identity's heartbeat directory, holding a count, the first failure
time and the last reason. A reason is the exception type plus at most 200
characters of detail, the child's stderr capped at 200 characters, or the
rejection or fault text, and is cut to 216 characters when written. The next
successful payload carries the record as `heartbeat_failures_since_last`,
`heartbeat_failure_first_at` and `heartbeat_failure_last_reason`, and that
success deletes the record unless a worktree attestation fault is still
pending. After a recorded failure, every firing within 180 s of the record's
mtime returns before any git or report call. Diagnostics go to stderr as
`[heartbeat-report-alive]` lines. Exception text is printed uncapped there, so
a timed-out call's line includes its full argv and payload, and an attestation
fault's OS error text is uncapped; only the child's stderr is capped at 200
characters.
One accepted gap, stated in the heartbeat's own docstring rather than
engineered around: a single tool call longer than a session's report-by
window still trips an overdue alarm mid-call, since the hook only fires
*after* a tool call completes. Rare; disclosed, not chased.

## The rotation-due notice hook — the one hook whose output enters a prompt

`rotation_due_notice.py` (`UserPromptSubmit`, unconditional) is the delivery
half of the rotation-due signal for a session the fleet's own sweep cannot
reach. It reviews no state and measures nothing: when `rotation_due_watch.py`
finds no steward to notify (an operator-present session has none) it writes
the notice to a local marker file, and this hook surfaces that marker as
context on the session's own next turn, once, then stamps it so it never
repeats.

**Why it gets its own section.** It is the only hook in this plugin that puts
FILE CONTENT into a prompt. The knowledge-base reminder injects a compiled-in literal; live context injects the bounded data disclosed above; and
`session_context.py` injects a fixed template; this one injects text it read
from disk. That is a different class of claim and is bounded explicitly rather
than by category:

- **Provenance is checked before injection.** A marker is refused unless it is
  owned by the current user AND is not group- or world-writable. The relevant
  exposure is concrete: the writer's fallback marker root lives under the
  system temp directory, which on a shared host is world-writable, so without
  this check any local user could place text into another user's next prompt.
  On a platform with no `geteuid` the ownership half cannot run and is SKIPPED
  rather than faked — the permission half still applies.
- **Scope of what is injected.** The marker's `content` field verbatim, inside
  a fixed frame this hook compiles in. No other field of the marker reaches
  the prompt, and nothing else on disk is read.
- **Bounded volume.** At most one `hookSpecificOutput` object per prompt
  however many markers match, and each marker is surfaced at most once —
  the stamp is written INTO the marker, so there is no second file that could
  disagree with it about whether the notice was delivered.

**Execution and writes.** No subprocess of any kind — it is not one of the
five subprocess-capable files. It writes exactly one file: the marker it just
surfaced, rewritten with a `surfaced_at` timestamp added.

**Failure posture.** Non-fatal by design like every hook here: an unreadable,
unparseable, foreign-owned or world-writable marker is skipped with a stderr
diagnostic and exit `0`. A marker that cannot be stamped is still DELIVERED
and will repeat on the next prompt — deliberately, since a repeated warning is
a smaller failure than a swallowed one.

**On the ordinary path it does nothing.** No marker means: read stdin, glob at
most two directories, emit nothing. This hook fires on every prompt, so its
empty path is the cost that is actually paid.

## The memory-passthrough files — two hooks, four CLI utilities, one shared library

Six files, three of which are already covered above by category (`capture.py`
writes and never subprocesses; `session_context.py` neither writes nor
subprocesses; `sync.py` is one of the five disclosed subprocess-capable
files). This section states each file's full, individual contract.

**`capture.py`** (`PostToolUse`, `Write`/`Edit`/`MultiEdit` only — the
ONE hook in this plugin with a tool-name matcher, since it only needs to
notice file-writing tools). Reads `tool_input.file_path` from stdin; if
it matches a path under this agent's own memory directory, appends one
JSON line — path, sha256, mtime, timestamp, never file CONTENT — to a
local journal file. No subprocess. No network. No other file touched.
Malformed or unrelated input is a silent no-op, always exit `0`.

**`session_context.py`** (`SessionStart` + `UserPromptSubmit`). Reads
whether this agent's memory directory exists (a bare/no-memory session
gets nothing); if so, prints a `hookSpecificOutput.additionalContext`
block — a FIXED instruction template (hydrate/drain command recipes,
interpolating only this session's own already-resolved paths and a
pending-count integer, never external content) telling the calling agent
exactly what to run next. No subprocess. No file writes. This hook is how
the loop reaches the agent at all — a hook subprocess has no platform
bridge of its own, so hydrate/drain are agent-mediated by design, never
server-side.

**`drain.py` / `hydrate_render.py` / `index_render.py` / `sync.py`** are
NOT Claude Code hooks — none fire automatically on any event; all four
are CLI utilities the agent invokes directly via Bash, on its own
judgment, following `session_context.py`'s own printed instructions.
(This is why none carries an `AGENT_SESSION_LABEL`/arming-variable story
at all — there is no automatic firing to arm or disarm; see the
Configuration surface section.)
- `drain.py`: reads the pending local journal, prints the exact
  `upsert_memory_by_tag` arguments for each entry (content re-read from
  the local file at drain time + tags) — the agent then issues those
  calls itself. `--advance` writes one watermark file (via `_journal.py`,
  not directly in its own source). A journal entry whose file no longer
  exists is skipped, not upserted. No subprocess, no network.
- `hydrate_render.py`: takes an already-exported JSON snapshot path (the
  export itself is the AGENT's own `process_call`, not this script's job)
  and writes per-fact `.md` files plus `MEMORY.md`, content taken
  VERBATIM from the snapshot — never content this script invents.
  Fail-loud on any malformed record (no slot tag, unresolvable path,
  missing frontmatter) — a corrupt projection is never half-written. No
  subprocess, no network.
- `index_render.py`: pure rendering/budget-fitting logic, imported by
  `hydrate_render.py` for the shared frontmatter-parsing and
  budget-fitting contract (so a record that indexes one way cannot
  hydrate another); also has its own standalone CLI entry point for a
  direct out-of-band re-render, which writes `MEMORY.md` the same way.
  No subprocess, no network.
- `sync.py`: the disclosed subprocess exception in this group — wraps the
  hydrate/drain steps above into one Bash call per direction and DOES
  shell out, via the same PATH-resolved `solet` convention as the
  rotation-due watch hook (the heartbeat calls `solet-bridge` the same way): one `export_memories` call on
  hydrate, and — on drain — one `upsert_memory_by_tag` call PER pending
  journal entry (unbounded per invocation, bounded only by how many local
  edits are actually pending). Every argv is `["solet-bridge", "call",
  <fixed process_key>, <JSON payload>]`, no shell, `subprocess.run` only —
  same shape-fixity property as every other solet-calling hook in
  this plugin, just not a Claude-Code-fired one. A failed drain upsert
  never advances the watermark — the failed (and only the failed) entries
  retry on the next run, reported loudly, never silently swallowed.

**`_journal.py`**: the shared library every file above imports. Owns
this agent's memory-dir/state-dir path resolution, the tag-shape helpers,
and — the one piece with real cross-machine correctness stakes — the
origin-resolution ladder:

1. `SOLET_NAME` env var, if set.
2. `root_manifest.yaml`'s own `solet_name:` field, read via a
   minimal regex line-scan — **never a real YAML parse**: PyYAML is a
   venv-only dependency on this platform (measured 2026-08-10: available
   under `.venv/bin/python3`, absent under the bare `python3` these files
   actually run under), so a YAML parse here would silently violate this
   loop's own stdlib-only claim. Skipped when the field still carries the
   unwritten placeholder `"solet"` — the midwife rewrites this field
   ONLY at genesis, so a raw/pre-genesis checkout keeps the placeholder
   and must fall through, never treat it as a real name.
3. `CLAUDE_PROJECT_DIR`-basename — the final fallback, this module's
   prior sole behavior, preserving continuity for any checkout where
   neither rung above resolves.

Fails fast (raises, caught by every caller's own non-fatal contract) only
when `CLAUDE_PROJECT_DIR` itself is unset AND `SOLET_NAME` is unset —
there is nothing left to derive a name from at all. This resolved name
becomes this agent's `origin` tag (`claude_code.<name>`), which scopes
every memory record this loop reads or writes so hydrate never pulls
another agent's records. The SAME ladder, same placeholder-skip guard,
is implemented independently in a separate, distinctly-named function on
the platform-verb side (never in this shared `_resolve_solet_name()`,
which an unrelated MCP-router-identity caller also depends on and which
this package deliberately leaves untouched) — a parity test proves the
two agree across a matrix of env/file/dirname combinations rather than
trusting them to stay in sync by inspection alone.

## The spawn-injected worker hooks — two files, a different delivery path

Unlike every hook above, these two files are never wired into THIS
plugin's own `hooks/hooks.json`. A spawned headless- or tmux-hosted
worker's own host adapter (`agent_messaging_plugin`'s `headless_adapter.py`/
`tmux_adapter.py`) references them by path directly, in a generated
Claude Code `--settings` blob it builds for that one spawn, via a
two-rung resolution ladder: the origin checkout's own
`.claude/hooks/<file>` when present, falling back to this plugin's own
shipped copy (exactly the file you are reading about) on a born clone
that carries no `.claude/hooks/` directory at all. The two copies must
stay behaviorally byte-identical — the checkout-external cross-copy
parity smoke (see Verification below) is the guard, same pattern as the
heartbeat/rotation-due-watch/capture/session-context parity legs above.

**`headless_tool_allowlist_gate.py`** (`PreToolUse`, would be wired only
into a spawned worker's own generated settings, never this plugin's
`hooks.json`). Enforces a spawned worker's spawn-time tool allowlist.
Reads `tool_name` from stdin and the comma/whitespace-separated
`FLEET_HEADLESS_TOOL_ALLOWLIST` environment variable; a call outside the
allowlist is BLOCKED (exit `2`) with a fixed-template stderr reason
naming the tool and the allowlist. **Ships unarmed by default** — the
spawning adapter only sets the environment variable when a caller
supplied an explicit `allowed_tools` list — and is the one hook in this
plugin that is **FAIL-CLOSED**: any parse or exception path also returns
`2`, never `0`, because this is the actual safety boundary for an
unattended worker with no human present to catch a hook bug. No subprocess, no network, no file write. Stdlib-only.

**`capture_session_mapping.py`** (`SessionStart`, would be wired only
into a spawned worker's own generated settings). Writes one file-per-firing
JSON record — this worker's own `AGENT_INSTANCE_ID` (already exported by
both host adapters), the Claude Code `session_id` this SessionStart
firing carries on stdin, a UTC timestamp, and the firing's `source`
(startup/clear/resume/compact) — to the adapter-declared
`ANANTA_SESSION_MAPPING_SPOOL_DIR`. The spool path is never derived by
this hook itself (it stays dumb and host-agnostic); a missing env var, an
unparseable stdin payload, or an unwritable spool dir warns to stderr and
exits `0` — non-fatal by design, the same posture as the memory-passthrough
capture hook, since a broken capture hook must never cost a worker its
session start. No subprocess, no network. Content-bearing (never a bare
marker): the written record is the file's whole purpose, disclosed as its
own content-bearing write-primitive class in the Inputs and outputs
section above.

**Arming.** Both require their own adapter-supplied environment variable
(`FLEET_HEADLESS_TOOL_ALLOWLIST`, `ANANTA_SESSION_MAPPING_SPOOL_DIR`
respectively) — neither is ever set outside a spawned worker's own
generated settings, so an ordinary interactive or operator session never
arms either, and a plain "this plugin is installed" is not itself an
arming condition for these two (unlike `step_zero_reminder.py`).

**Dynamic information conveyed.** Neither ever prints a
`hookSpecificOutput` block — no output reaches a session's context
through either of them. The allowlist gate's only possible output is its
fixed-template stderr block reason on a denial (a slice of the calling
session's own already-supplied tool name and allowlist, echoed back —
never third-party content). The session-mapping capture never prints
anything on success; its only possible output is an occasional
fixed-template stderr diagnostic on a non-fatal failure path.

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

## Deployment context (disclosed so nothing is discovered later)

The knowledge-base reminder stays generic. Live context explicitly calls the
local psolet and solet-scoped bridge interfaces under the configuration above.
Unavailable services are named in context. The wake waiter retains its own
operator-configured `AGENT_WAKE_CLI` fixed-argv contract and discards its child
output. The hook scripts have no direct network/database client.

One property of that CLI/service is worth disclosing here rather than
assuming out of scope: message attribution in the coordination service is
**advisory, never authenticated identity**. A message's stated sender is
populated from information the sending process supplies about itself;
nothing in the delivery path independently verifies that claim against
anything the sender could not also present as its own. Any local process
running as the same OS user as a legitimate sender can therefore construct
a message a receiver cannot distinguish from a genuine one — the same
single-user, cooperating-peers boundary this page draws everywhere else,
applied to who a message says it is from rather than to what a hook can
do. None of the five hooks in this plugin read, send, or attribute a peer
message themselves — the messaging path is entirely outside this plugin's
own surface — so the guarantee to carry forward is precisely this: treat a
message's stated sender as advisory, never as authenticated identity,
wherever that CLI/service's output reaches a session.

## Transport asymmetries (MCP vs. non-MCP/watch)

This plugin's own behavior does not depend on transport — every hook above
runs identically regardless of which transport the session's coordination
layer uses. Three capabilities belong to the *coordination layer itself*,
not to this plugin, and are disclosed here so a review of this deployment
does not discover any of them as a gap instead of reading it on this page.
A candidate only earns a row once the two transports are actually shown to
diverge; a capability that behaves the same on both is not listed here at
all — this section is not a complete inventory of every capability that was
checked, only of the ones confirmed to differ — plus one further candidate,
below, that was checked, found equivalent, and is kept rather than silently
dropped because it was previously listed here as an open provisional row.

**Backend agent-thread dispatch — retired (D3 dormant-head retirement).**
Opening and driving a backend agent thread (the coordination service's
`agent_thread_open` / `agent_send` / `agent_messages` / `agent_status` /
`agent_close` operations) was removed along with the dormant
`GuardedAgentInterface` backend, which had zero implementing plugins —
the entire dispatch surface (MCP tools and HTTP/service implementation)
is gone, not just its non-MCP reach. Kept for record: while live, it was
an MCP-only mechanism with no non-MCP-transport equivalent. Fleet
coordination in this deployment operates through full peer sessions
exchanging messages, which this retirement does not touch.

**Delivery durability across a coordination-service cutover — checked,
confirmed equivalent on both transports.** A message sent to a session
shortly before the coordination service restarts or swaps is durably
retrievable by that session afterward. Measured against a live blue-green
cutover (2026-08-02, ~220s): a live MCP session and a `watch`-armed
non-MCP witness both kept their pre-fire `agent_instance_id` across the
swap with no re-claim, and both had two marked messages sent to them
before the fire (one silent, one IMPORTANT) — all four confirmed durably
present, at their original message ids, via the peer inbox after cutover.
Both transports behaved identically, so this is not an asymmetry; it is
kept here, reclassified rather than silently dropped, because it was
previously listed as an open provisional row and a reader who saw it there
should see it closed, not vanished. One distinct question remains
genuinely unmeasured, stated as a bound rather than folded into the
confirmed result: whether a live push *notification* in flight at the
exact reconnect instant is itself delivered — separate from the
underlying message record, which is durable regardless of notification
delivery. This measurement's own markers happened to be sent and
delivered before the restart began, an artifact of this run's timing, not
a proof either way; a future run could deliberately arm a marker
mid-cutover to test that instant directly. No claim of "always survives"
is made — only what was measured.

**Peer enumeration — confirmed.** Discovering which peer sessions are
currently registered is a capability of the MCP transport only. A session on
a non-MCP transport reaches the platform solely through
`solet-bridge call <process_key>`, and no registered process returns the peer
registry: the CLI exposes no `peers` subcommand, semantic discovery over the
knowledge base surfaces no peer-registry verb, and `peer_list` exists only as
an MCP tool. Sending is unaffected — `peer_send_by_name` resolves and
delivers identically on both transports — so the asymmetry is in discovery,
not delivery: a non-MCP session can address a known peer or role by name but
has no peer list available to it and cannot enumerate who is currently live.
One adjacent verb, `list_bridges`,
returns a well-formed, successful, and incorrect result (`0 bridge(s)
tracked` against a live multi-session fleet) rather than an error; treat it
as unreliable for this purpose, not as a substitute for peer enumeration. A
fix is scoped but not built: three thin CLI subcommands
(`solet-bridge inbox`, `solet-bridge peers`, `solet-bridge whoami`) over routes
that already exist would close this gap; none of the three exist today.

**Idle-session wake — confirmed.** A session on the MCP transport that goes
idle with nothing pending does not wake on its own — there is no waker for
that transport, so a truly idle MCP session never turns again without
something external driving it back open. A non-MCP (`watch`) session's
Stop-hook waiter (`wake_waiter.py`, this plugin) reopens the session on
delivery instead. The asymmetry is in **waking**, not in delivery: a
message sent to an idle session is durably received on either transport;
what differs is whether receiving it also resumes the session's turn.
Measured 2026-08-02 (a live coordination walk): negative control (idle,
nothing pending) run first on both arms — clean on both; an IMPORTANT
delivery addressed directly to the MCP arm's own instance produced no
unsolicited turn within 60s, while the same delivery to the non-MCP arm
woke the session at 55.6s. No fix is scoped for this row; it is disclosed,
not remediated.

Other capability comparisons across transports are ongoing; of the entries
above, three (backend agent-thread dispatch, peer enumeration, idle-session
wake) are the only confirmed or provisional divergences so far, and the
cutover-durability entry is a checked, closed equivalent kept for
transparency rather than an open item.
An absence from this section reflects the measurement record at time of
writing, not a guarantee that no further asymmetry will ever surface — this
section is maintained as that record changes, and a future divergence gets
its own row rather than a silent gap.

## Threat model and its boundary

Hooks run as the same OS user as the Claude Code session that invokes them.
There is **no privilege boundary** here to defend, and the plugin claims
none. Its reminders, wake waiter, liveness and rotation hooks, memory-passthrough
utilities and headless tool-allowlist gate are steering and bookkeeping, not a
git-mutation control. The git-text gate removed in `0.9.0` (operator ruling
`rul_e71e43be`) is inventoried in `README.md` under *Retired behaviours*: its
git-mutation ban, its `Agent`/`Task` spawn ban, its Bash-level `.git/` block,
its inline `git -c` and global-option bans, its `--no-verify` ban with the
operator-confirmation prefix, and its refusal of `git` beside a shell evaluator
word or an untokenisable construct are all unenforced, and no control in this
plugin replaces any of them.

## Failure modes

- **Knowledge-base reminder**: always emits context, exit `0`.
- **Live session context**: read failures emit named unavailable context, exit `0`; no zero-count fallback.
- **Wake waiter**: disarmed → immediate exit `0`. Armed, it is a
  deliberately long-running hook (registered `asyncRewake` with a 24-hour
  timeout): it blocks at zero model cost while the session is idle. If the
  configured CLI cannot be spawned it exits `0` with a one-line stderr note
  — a broken wake path degrades to "messages wait for the next turn"
  (where the reminders prompt an explicit check), never to a stuck session.
- **Heartbeat**: silent exit `0` when none of the six managed variables is
  set. When any is set but `AGENT_INSTANCE_ID` or `AGENT_SESSION_ID` is
  missing, or the receipt fails verification, it prints one
  `[coordination-owner]` stderr line and exits `0`. A failed `report_alive`
  call or a worktree attestation fault is recorded for the next payload and
  warns to stderr; it exits `0`, never `2`, never blocking a tool call. A
  broken heartbeat degrades to "liveness reporting stops" (the fleet's own
  overdue-session detection is the backstop, not this hook).
- **Rotation-due watch**: disarmed (no `AGENT_INSTANCE_ID`) →
  immediate exit `0`, silent. Every other failure mode (missing marker
  dir, unreadable transcript, malformed stdin, unresolvable `solet`
binary, a non-zero or unparseable `solet-bridge call` result) warns to
  stderr and exits `0` — never `2`, never blocking a tool call. A broken
  rotation-due watch degrades to "no threshold notification fires" (a
  session simply rotates later than it ideally would, never a stuck or
  crashed tool call).
- **Memory-passthrough capture/session-context**: no memory directory →
  `capture.py` silently ignores the write; `session_context.py` emits
  nothing (the shared bare-session guard). Any other failure (unparseable
  stdin, an unwritable journal/log path) is caught and logged locally,
  never disrupts the tool call, always exit `0`. Neither uses exit code `2`.
- **Memory-passthrough CLI utilities**: not Claude Code hooks, so this
  section's "never disrupts a tool call" framing does not apply the same
  way — they run only when the agent itself invokes them. `hydrate_render.py`
  fails LOUD (non-zero exit, explicit stderr message) on a malformed
  record rather than half-writing a projection — the deliberate exception
  to this page's usual fail-open-and-continue posture, because a silently
  corrupt local memory projection is worse than a visible refusal.
  `drain.py` never advances its watermark on a partial failure, so failed
  entries retry next run rather than being silently dropped. `sync.py`
  preserves both of those contracts verbatim (it imports and calls the
  same functions, never reimplements them) and additionally: solet/platform
  unreachable on export → hydrate is skipped entirely, non-zero exit, the
  last local projection stays untouched, never a partial write.
- The reminders, gate, and wake waiter are stateless and idempotent — no
  temp files, no ordering dependencies, and no state carried between
  invocations (the only files those four ever produce are the
  interpreter's own `__pycache__` byte-code artifacts disclosed above,
  which the plugin never reads). The heartbeat and rotation-due watch
  hooks carry small, local state between invocations by design (that state
  IS the throttle, latch and failure-record mechanism) — never read by
  anything outside those two hooks. The heartbeat's state is its throttle
  stamp, its failure record (count, first failure time, last reason) and its
  worktree snapshot (repository root, HEAD, a dirty flag and a hash of the
  status output); its failure counts reach the register in its payload.
  The memory-passthrough
  files carry real, meaningful state between invocations by design (the
  journal, the watermark, the hydrated-hash oracle, and this agent's own
  memory directory itself) — that persistence is the whole point of the
  loop, never accidental, and every one of those state files lives under
  this agent's own per-project paths, never shared with another agent or
  session.

## Verification — running the claims on this page

The claims above are executable. From this directory, on any machine with
`python3` — the plugin's sole runtime dependency, a guaranteed platform
prerequisite:

```
python3 tests/run_all.py
```

No repository, no network, no configuration, and no environment variables are
needed; the suite creates nothing outside a temporary directory it removes.

**Where the suite is present.** `tests/` accompanies the review and development
copies of this plugin. Distribution builds prune every `tests/` directory, so a
plugin installed from a distribution will not contain it and the paths cited
below will be absent there — the shipped hooks are byte-identical either way,
with the test files simply not carried. To verify a distributed copy, take the
suite from the plugin's source repository and run it against that copy.
It adds no dependency the plugin does not already have — the harness is Python
standard library and drives every hook as a `python3` subprocess, the same
way Claude Code invokes them.

| Claim on this page | Where it is proved |
|---|---|
| Exactly the five disclosed hooks/utilities (live session context, wake waiter, heartbeat, rotation-due watch, `sync.py`) can execute a subprocess; each uses `subprocess.run` only, no `shell=True` | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| No network I/O in any hook | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| No hook writes a file as an action of its own, except the eight disclosed exceptions | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` and `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/wake_waiter_smoke.py` |
| The two MARKER_ONLY writers (heartbeat, rotation-due watch) each contain the bare timestamp marker write `write_text(str(time.time()))`; the heartbeat also writes its failure record and worktree snapshot (see the heartbeat paragraphs above) | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| Supply chain is stdlib/built-ins only, except `rotation_due_watch.py`'s disclosed same-platform import and the memory-passthrough CLI utilities' disclosed intra-plugin imports of each other | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| Agent-invoked CLI utilities (`drain.py`/`hydrate_render.py`/`index_render.py`/`sync.py`) are correctly NOT wired in `hooks.json` — they never fire automatically | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| `hooks.json` is exec-form with no shell in the invocation path | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The hook inventory on this page matches the tree | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The knowledge-base reminder is a fixed literal | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/reminder_hooks_smoke.py` |
| Knowledge-base context does not interpolate the label | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/reminder_hooks_smoke.py` |
| The knowledge-base reminder is always armed | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/reminder_hooks_smoke.py` |
| Reminders can never block a session (never exit 2) | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/reminder_hooks_smoke.py` |
| The wake waiter discards the child's output unread | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` and `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/wake_waiter_smoke.py` |
| The wake waiter conveys exactly one bit, via a compiled-in nudge | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/wake_waiter_smoke.py` |
| The wake waiter's argv is fixed, with no shell | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The prose surfaces (README.md, this page) describe the wake waiter's real argv — expected tokens derived from the source literal, the stale unbounded form refused | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| A broken wake path never traps the session | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/wake_waiter_smoke.py` |
| Legacy guarded hooks retain their opt-in behavior; knowledge-base and live context are always armed | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/reminder_hooks_smoke.py` and `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/wake_waiter_smoke.py` |
| The heartbeat and rotation-due watch hooks' `solet-bridge call` argv carries their fixed process key, never a shell, never `subprocess.call`/`Popen` | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The heartbeat and rotation-due watch hooks never print a `hookSpecificOutput` block | disclosed here (source-read); no dedicated behavioral smoke ships in this plugin's own `tests/` yet (the heartbeat's checkout-external smoke is named in the Known gaps note below) |
| The rotation-due notice hook refuses to inject a marker owned by another uid or one that is group/world-writable, and skips the ownership half rather than faking it where no uid exists | the checkout's .claude/hooks/tests/rotation_due_notice_smoke.py — checkout-external, see the Known gaps note below |
| The rotation-due notice hook surfaces a marker at most once (the stamp is written into the marker itself), emits exactly ONE `hookSpecificOutput` object however many markers match, and still delivers when the stamp cannot be written | the checkout's .claude/hooks/tests/rotation_due_notice_smoke.py — same external caveat |
| The rotation-due notice hook's copies in this plugin and in the checkout's `.claude/hooks/` are BYTE-IDENTICAL (it has no adaptation window) and behave identically on the empty and the surfaced path | `.claude/hooks/tests/coordination_hook_ports_smoke.py` parity leg — same external caveat |
| The origin-resolution ladder (env → root_manifest.yaml, placeholder-skipped → dirname) resolves identically across both independent implementations (the hooks' `_journal.py` and the memory-tag verb resolver), across a matrix of env/file/dirname combinations | `.claude/hooks/tests/memory_passthrough_origin_ladder_smoke.py` — checkout-external, see the Known gaps note below |
| The heartbeat, rotation-due watch, capture, and session-context hook copies in this plugin and in the checkout's `.claude/hooks/` behave identically (throttle/latch marker paths, arming, argv/output shape) | the checkout's own `.claude/hooks/tests/coordination_hook_ports_smoke.py` parity legs — external to this plugin's own `tests/`, so not run by `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/run_all.py`; see the Known gaps note below |
| The spawn-injected `headless_tool_allowlist_gate.py`/`capture_session_mapping.py` copies in this plugin and in the checkout's `.claude/hooks/` behave identically (exit code, block/no-op decision, spool record shape) | the checkout's own `.claude/hooks/tests/coordination_hook_ports_smoke.py` parity legs — same external-to-this-plugin caveat as the row above |
| Every hook filename a spawning host adapter (`headless_adapter.py`/`tmux_adapter.py`) injects into a spawned worker's generated `--settings` exists in this plugin's own shipped `hooks/` — the two-rung resolution ladder's fallback rung never points at a missing file | `plugins/agent_messaging_plugin/tests/worker_hook_shipping_smoke.py` — checkout-external, adapter-side |
| `drain.py --advance` binds to the offset its own prior listing recorded rather than the journal's current end, so a capture landing between a listing and its `--advance` is never swallowed; a bare `--advance` with no recorded listing fails loud (exit 2) rather than silently no-op-ing | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/memory_passthrough_regression_smoke.py` |

One limit, stated so the coverage is not read as wider than it is:

- The no-network, no-file-write, subprocess-shape and supply-chain checks
  are **source-level**: they prove the code never names the primitive,
  which is what makes those claims auditable by reading. They are not a
  syscall trace.

**Known gaps, disclosed rather than silently left implicit (2026-08-10
vendoring, Packages A and B).** Unlike `wake_waiter.py`, the rotation-due
watch, capture, and session-context hooks do not yet have
their own dedicated behavioral smoke inside this plugin's `tests/`
directory (mirroring `wake_waiter_smoke.py`'s role) — their behavioral
coverage tonight is the checkout-external cross-copy parity smoke only,
which proves the copies agree with each other, not that any one copy's
behavior is independently correct against a live `solet` fixture. The
heartbeat's behavioral smoke is checkout-external
(.claude/hooks/tests/heartbeat_report_alive_smoke.py), not in this plugin's `tests/`.
`manifest_consistency_smoke.py`'s source-level checks (fixed process key,
no shell, `subprocess.run` only, marker/write shape) still apply and are
real coverage, just not a live-process behavioral proof. Additionally:
`hydrate_render.py`/`index_render.py`/`sync.py` still have no dedicated
smoke of their own in THIS plugin — their only coverage is
`manifest_consistency_smoke.py`'s source-level structural checks (no
network, correct import graph, `sync.py`'s subprocess shape) plus the
checkout-original's own pre-existing unit smokes for the code they share
via `_journal.py`. `drain.py` is the one exception (MEM-08, 2026-08-19):
`plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/memory_passthrough_regression_smoke.py` drives it and
`capture.py` as live subprocesses and re-proves the MEM-06 interleaved-
capture regression directly against this plugin's own copies, closing
the specific gap that let MEM-07's mirror landing (`8eb55dad9`) ship
with no smoke of its own. It does not cover `hydrate_render.py` — that
utility's checkout and vendored copies carry a disclosed, legitimate
behavioral divergence (head-budget policing exists only in the checkout)
that a shared smoke would need to declare, not merely re-run. The
origin-resolution ladder's parity smoke tests the
ladder logic itself across both implementations, but neither
implementation has yet been live-tested against a real
`root_manifest.yaml` on an actual post-genesis adopter clone — only
against synthetic fixtures constructed for the test. These gaps are
scoped, not fixed, in this vendoring pass; see the Package B commit
message for the same disclosure in the landing record. Same disclosure
class applies to `headless_tool_allowlist_gate.py` and
`capture_session_mapping.py` (Package C, 2026-08-10): no dedicated
behavioral smoke inside THIS plugin's `tests/` yet; coverage is the
checkout-external cross-copy parity smoke plus `manifest_consistency_smoke.py`'s
source-level checks (no subprocess, no network, correct write-shape
classification). The adapter-side resolution ladder itself (which rung
resolves for which file) is covered by
`plugins/agent_messaging_plugin/tests/worker_hook_shipping_smoke.py`, not
by anything in this plugin's own `tests/`.

## Supply chain

Every hook in this plugin is stdlib-only Python (`python3`), with two
disclosed, narrow exception classes: `rotation_due_watch.py` imports
`agent_messaging_plugin.rotation_thresholds`, a zero-third-party-dependency
SAME-PLATFORM module (not a PyPI package) that ships alongside this plugin
in every capability bundle, resolved via `CLAUDE_PROJECT_DIR` and imported
inside a `try`/`except` that degrades gracefully if it is ever absent;
and the memory-passthrough CLI utilities import each other directly
(`hydrate_render.py` imports `index_render`; `sync.py` imports `drain`
and `hydrate_render`) — local modules within this same plugin, never a
new external dependency, and specifically NEVER `yaml`/PyYAML, which is a
venv-only dependency on this platform (measured 2026-08-10) that every
one of these files, running outside the venv, must not depend on — the
origin-resolution ladder's `root_manifest.yaml` read uses a minimal
regex line-scan for exactly this reason. Otherwise: no third-party
packages, no install step, no vendored or compiled artifacts, no
interpreter downloads. `python3` is a guaranteed
platform prerequisite — unlike this plugin's prior Node implementation of
four of these hooks, which depended on a runtime nothing guaranteed and
could silently fail to launch with no signal Claude Code surfaces to the
user (measured 2026-08-08; retired the same day). Hook registration
(`hooks.json`) uses exec-form invocation (`command` + `args` array): no
shell string interpolation anywhere in the invocation path.

## Configuration surface

Twelve of the fifteen hooks are default-off or agent-invoked utilities;
`session_cron_sentinel.py` observes matching tool attempts;
`step_zero_reminder.py` and `session_start_context.py` always run at the
registered start sources. Missing register or identity configuration produces
explicit unavailable context for the latter. Existing heartbeat, rotation,
wake, memory and spawn-injected guards retain their documented behavior.

**Adopter setup note.** The heartbeat and rotation-due watch hooks'
arming variable, `AGENT_INSTANCE_ID` (the heartbeat also needs `AGENT_SESSION_ID` and a valid owner receipt), and `SOLET_NAME` (rung 1 of
the memory-passthrough origin-resolution ladder — see that section above;
also read by other platform verbs these hooks call into) are both
expected to be exported in the environment Claude Code is launched from
— see the fleet's own enablement runbook for the exact launch-time
export convention. `SOLET_NAME` is a soft dependency, not a hard
one: an adopter who never sets it still gets a correctly-resolved name
via the ladder's rung 2 (`root_manifest.yaml`, once genesis has rewritten
it) or rung 3 (`CLAUDE_PROJECT_DIR`-basename) — exporting it just makes
rung 1 reliable without depending on either.

## Env-less session identity — `_session_identity.py`

A Claude Code session started outside the fleet launchers (the Claude desktop
app, or a bare `claude`) exports no `AGENT_INSTANCE_ID`, `AGENT_SESSION_ID` or
`AGENT_SESSION_LABEL`. `_session_identity.py` is the sibling library that
`capture_session_mapping.py`, `session_start_context.py`,
`rotation_due_watch.py` and `session_cron_sentinel.py` import to give such a
session one durable identity. It runs no child and opens no socket.

**When it acts.** Only when none of those three launcher names is set. Any one
present means it returns immediately: no read, no write, no environment change.
It also needs `CLAUDE_CODE_SESSION_ID` (or the hook payload's `session_id`)
matching `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`, and a readable coordination receipt
at `<root>/profile/data/coordination-hooks/claude/installation.v1.json`, where
`<root>` is `CLAUDE_PROJECT_DIR` or the checkout holding `.claude/hooks/`.

**What it reads.** That receipt's `solet_name`, and the session's identity
record, `$XDG_RUNTIME_DIR/ananta/claude-session-identity/<id>.json`
(`~/.ananta/runtime/...` without `XDG_RUNTIME_DIR`). The session's MCP bridge
or bridge CLI writes that record (`agent_messaging_plugin.env_contract`); it
holds an instance id derived from the session id (`agi-` + 32 hex characters
of its SHA-256), `ases-claude-<id>`, `claude-<first 8 characters>`, the solet
name and a creation stamp, never a secret, prompt or tool input. While no
record exists yet the library derives those same three values itself.

**What it writes.** Nothing.

**What it changes.** It `setdefault`s this hook process's own environment
(never a parent's): `SOLET_NAME`, `AGENT_IDENTITY`, the three identity names,
`AGENT_COORDINATION_RECEIPT_PATH`, `ANANTA_SESSION_MAPPING_SPOOL_DIR`, and,
when `<root>/.venv/bin/solet-bridge` is a file, `AGENT_WAKE_CLI` and
`SOLET_BRIDGE`. The hooks then follow their existing contracts with that
identity. The receipt still gates the heartbeat and rotation-due effects
through `coordination_owner.py`, unchanged. `heartbeat_report_alive.py` does
not import this library: an env-less session has no fleet ledger row.

The venv-side twin is `agent_messaging_plugin.env_contract`, which the MCP
bridge and the bridge CLI use. The checkout-external
`plugins/agent_messaging_plugin/tests/desktop_session_identity_smoke.py` pins
both to the same record and asserts that a launcher session is untouched.

## Session-cron sentinel

`session_cron_sentinel.py` observes CronCreate and ScheduleWakeup call attempts.
Attempt observed, success unknown: neither a tool response nor a backend receipt
proves schedule creation. This is the plan amendment under `uev_c545d677` to the
original response-contract clause. Fixtures are synthetic-input-schema-derived;
native response capture and a typed delay field remain owning-seat follow-ups.

The hook reads only stdin and AGENT_INSTANCE_ID. The one exception is an
env-less session (no launcher identity): it reads its clone receipt and its
identity record through the read-only `_session_identity.py`. Missing identity or unsupported input warns and exits 0. It creates no log, marker, cache, credential or session
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
