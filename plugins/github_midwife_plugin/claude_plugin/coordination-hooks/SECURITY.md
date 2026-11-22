# Security notes — coordination-hooks

This page describes the reviewed data-flow surface. The plugin is fourteen Claude Code hooks/utilities:
one context reminders entry (`step_zero_reminder.py`), live session context
(`session_start_context.py`), `wake_waiter.py`,
`rotation_due_watch.py` (the context gauge report), `capture.py`,
`session_context.py`, `drain.py`, `hydrate_render.py`,
`index_render.py`, `sync.py`, `headless_tool_allowlist_gate.py`, and
`capture_session_mapping.py`, `session_cron_sentinel.py`, and `recovery_claude.py`
(working-tree recovery saves, described in its own section below). The worker hooks are also registered in hooks.json.
The memory utilities share `_journal.py`. Four hooks share `_session_identity.py`
(env-less session identity, described in its own section below).
`recovery_claude.py` calls 23 engine modules that ship beside it as byte-identical
copies; they are not hooks, and the recovery section inventories them.

Six hooks/utilities can execute children: live session context, wake waiter,
rotation-due watch, sync, the session-cron sentinel and `recovery_claude.py`
(read-only git children, spawned only by the engine's `recovery_git.py`). The other eight hooks execute nothing.
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
The live inbox and principle-statement observations added here are bounded data, disclosed
below, and do not replace authoritative full reads. The guidance channel's removal is
complete as of this page — no hook in this plugin emits environment-supplied
prose, and there is no longer a full-trust content class to describe here.

## Inputs and outputs — the complete data-flow surface

The working-tree recovery hook's inputs, outputs, files and children are
disclosed in its own section, *Working-tree recovery*, where the shipped-hook
due-diligence checklist is answered item by item.

The rotation-due watch and wake entrypoints first import
`coordination_owner.py`. For a managed session it requires
`AGENT_COORDINATION_RECEIPT_PATH` to be the fixed profile-data receipt for the
exact `SOLET_NAME`, selector, selected root, manifest, interpreter, and hook
bytes. A missing, corrupt, foreign, stale, symlink-escaping, or unrecorded
designation refuses before every marker, lock, watcher, lifecycle write, or
bridge call. This receipt detects installed-state drift; it is not a grant of
authority.

The rotation-due watch's state lives under the runtime root the receipt names, in
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
  memory-passthrough capture/session-context hooks) or an allow (gate).
- **Environment variables**: the live context uses
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
  (required by the rotation-due watch hook — a functional precondition: the
  gauge row it reports is keyed on this id, never a protection),
  `AGENT_SESSION_ID` and `AGENT_COORDINATION_RECEIPT_PATH` (the rotation-due
  watch's owner receipt gate; `AGENT_CONTEXT_GAUGE_REPORTER_PATH` names which
  copy of the watch hook is the designated reporter; `AGENT_WAKE_CLI` is
  appended to `PATH` only, see the rotation-due watch section below;
  `CLAUDE_EFFORT` is passed through as the reported effort),
  `SOLET_NAME` (rung 1 of the memory-passthrough origin-
  resolution ladder — see the memory-passthrough section below; read-only,
  also required by the live inbox observer), `FLEET_HEADLESS_TOOL_ALLOWLIST` (arms
  the spawn-injected tool-allowlist gate — set only by a spawning host
  adapter, never meaningful in an ordinary interactive session; see the
  spawn-injected worker hooks section below), `ANANTA_SESSION_MAPPING_SPOOL_DIR`
  (the spool directory the spawn-injected session-mapping capture writes
  to — also adapter-supplied only). `step_zero_reminder.py` reads no
  environment variable at all — see the Configuration surface section.
- **Local state reads**: the rotation-due watch reads its owner receipt, the
  manifest and the hook files that receipt hashes, `stat()`s its own throttle
  marker's mtime to decide whether to skip a tick, and reads the calling
  session's **own** transcript file (never another session's) to compute
  current context usage and cache state.
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
  `session_start_context.py` emits the solet's own principle statement, measured inbox counts
  and roles or explicit read-failure lines. `session_context.py` emits its
  existing instruction template over resolved paths and pending count.
  Gate, rotation-due watch and capture have no context output.
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
  part; the child's own output is discarded unread); the rotation-due
  watch's diagnostic lines on a missing env var, an unreadable transcript, or
  a failed `solet-bridge` call, whose content classes are set out in its
  section below — always best-effort telemetry, never read by
  anything, never blocking;
  `hydrate_render.py`/`sync.py`'s own fixed-template failure lines
  (malformed record, unreachable platform on export) on their own error
  paths, same non-blocking telemetry contract.
- **Local file writes**: the rotation-due watch writes one file, a bare
  `str(time.time())` throttle marker per `agent_instance_id` under its
  receipt-derived identity directory (see its section below). The
  memory-passthrough files write more, by
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
is a fixed literal. Live session context carries the solet's own principle statement and
role/count observations under the contract below. No hook treats a
local label as proof of a held role.

Beyond that: no network I/O of any kind in any hook (no HTTP, no sockets,
no DNS) — the hooks/utilities that shell out to `solet-bridge` do not
perform network I/O themselves; the `solet-bridge` CLI is a separate,
independently reviewable local process, same reasoning as the wake
waiter's configured CLI below. No hook writes a file as an action of its
own, **except the seven disclosed exceptions and the local recovery store
described in its own section** — the rotation-due watch hook writes a
bare-timestamp throttle marker; `capture.py`/`hydrate_render.py`/`index_render.py`/`_journal.py`
write this agent's own memory/journal files, content sourced from an
already-exported snapshot or this agent's own already-resolved path,
never anything read from stdin, tool input, or another session (see
Outputs above and the memory-passthrough section below for the exact
per-file contract); `capture_session_mapping.py` writes a file-per-firing
session-mapping record to the adapter-declared spool directory (see the
spawn-injected worker hooks section below). One interpreter side effect is disclosed rather than
claimed away: a hook that imports the shared `_journal.py` library or the
recovery engine's modules lets CPython byte-compile them, so
`hooks/__pycache__/*.pyc` can appear beside the scripts. Nothing in the plugin reads those files back,
and an operator who wants them gone can set `PYTHONDONTWRITEBYTECODE=1` in
the session environment. The live-context hook reads the inbox through the solet's bridge
and the approved statement from one fixed path of the solet's own knowledge base (through
the bridge only when no file is found); no credential or raw error output is injected. The other five subprocess
owners retain their fixed argv contracts described in their sections. The
other eight hooks execute nothing; file-writing effects remain those disclosed
above, plus the local recovery store disclosed in its own section.

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

## The rotation-due watch hook — the context gauge report

**What it does.** `rotation_due_watch.py` fires on `PostToolUse`,
unconditionally (every tool call, no tool-name matcher), and reports this
session's own context gauge to the platform. It reads and sends; it acts on
nothing. It sends no message to any session, resolves no role or steward,
writes no notice marker, and does not call `clear_session`, `report_alive` or
any lifecycle-transition verb: when a session rotates is the session's and the
operator's decision, made from the gauge when they choose to read it. The
file's name is historical. The rotation notice it once delivered, the notice
hook that surfaced it and the heartbeat hook no longer ship.

Per firing, in order: (1) it verifies its owner receipt (see Arming below);
(2) it reads the stdin payload for the session's own `transcript_path` and
`session_id`; (3) one `stat()` of its throttle marker ends the firing when a
report completed within the last ~120 s for this `agent_instance_id` (a
throttled firing starts no subprocess and makes no bridge call); (4) otherwise
it reads the session's **own** transcript file (never another session's — the
path comes from this session's own stdin payload) and scans it from the end for
the most recent assistant usage block and the most recent forty assistant
calls (their timestamps and cache-read token counts), then computes current
context tokens (input, cache-creation and cache-read tokens of that usage
block), the model's context ceiling and a cache state with the constants and
classifier of `agent_messaging_plugin.rotation_thresholds`; (5) it runs one
`solet-bridge call` for `report_context_status` (`subprocess.run`, no shell, a
20 s timeout); (6) only when the platform completed that call it touches the
throttle marker, so a failed report is retried on the next tool call.

**What it sends, and to whom.** The payload goes to the local platform's
`report_context_status` verb, which stores one row for the session;
`session_context_status`, `fleet_status` and usage accounting read that row on
demand. It carries: this session's `agent_instance_id`, its `agent_session_id`
(when set) and the Claude Code session id from stdin as `runtime_session_id`;
the fixed literals `provider: "anthropic"` and `runtime: "claude_code"`;
`effort` from `CLAUDE_EFFORT` (`unknown` when unset); the model name,
`current_tokens` and `ceiling`; `measured_at` (the hook's own clock) and
`reading_at` (the transcript line's own timestamp, omitted when the line has
none); `cache_read_tokens`, `cache_cold` and `cache_overage_signature`
(omitted when they cannot be measured); and which copy of the hook reported
(`reporter_surface`, a path class and never an absolute path, plus
`reporter_generation`). It carries no tool-call content, no prompt or
transcript text and nothing about another session.

**Arming.** The owner receipt gate, via `coordination_owner.py`. When none of
the six managed variables `AGENT_INSTANCE_ID`, `AGENT_SESSION_ID`,
`AGENT_WAKE_CLI`, `AGENT_COORDINATION_RECEIPT_PATH`, `SOLET_NAME` and
`AGENT_CONTEXT_GAUGE_REPORTER_PATH` is set, it is a silent no-op (for an
env-less session, `_session_identity.py` first supplies them: see its section
below). When any is set but `AGENT_INSTANCE_ID` or `AGENT_SESSION_ID` is
missing, or the receipt
fails verification, each firing prints one `[coordination-owner]` stderr line
and exits `0`, before any marker, bridge call or subprocess. Only a receipt
that passes lets it proceed, and its state then lives under the
receipt-derived runtime root, keyed by `SOLET_NAME`, `AGENT_INSTANCE_ID` and
`AGENT_SESSION_ID`.

**Designated reporter.** One `PostToolUse` event can reach more than one
registered copy of this hook (a checkout's own and the installed plugin-cache
copy). A spawned worker's host adapter exports `AGENT_CONTEXT_GAUGE_REPORTER_PATH`
naming the one copy that reports, and every other copy exits `0` before it
claims the throttle marker or makes any call. When that variable is absent,
the copy that passed ownership verification reports.

**Command provenance.** The hook runs `solet-bridge` by name through `PATH`;
its argv is `["solet-bridge", "call",
"plugin::agent_messaging_plugin::report_context_status", <payload>]`, no
shell. When `AGENT_WAKE_CLI` names a file whose directory holds a
`solet-bridge` file, that directory is appended to `PATH` for the call
(appended, never prepended, so no existing resolution changes; a dangling
`AGENT_WAKE_CLI` adds nothing). It takes no operator-configurable executable
name, unlike the wake waiter's `$AGENT_WAKE_CLI`. Only the JSON payload
varies, and it is built from this session's own environment and its own
transcript file.

**Dynamic information conveyed.** The hook never prints a
`hookSpecificOutput` block and writes nothing to stdout, so no output reaches
the session's context through it, on success or failure. Its only effects are
the platform report above, the throttle marker below and stderr diagnostics.

**Writes.** One file: the throttle marker
`context_watch/<agent_instance_id>.rotation_due_check.stamp` under the
identity directory, holding `str(time.time())`. The hook keeps no latch, no
failure record and no notification marker.

**Failure posture.** Non-fatal by design, the same contract as every other
hook in this plugin. A missing `CLAUDE_PROJECT_DIR`, an
`agent_messaging_plugin` module that cannot be imported, an unreadable
transcript, a malformed stdin payload, an unresolvable `solet-bridge` binary
and a failed or unparseable `solet-bridge call` each warn to stderr and exit
`0`. The hook cannot cost a tool call and does not use exit code `2`. A failed
report leaves the throttle marker untouched, so the next tool call retries it,
with no backoff.

**Diagnostics.** Stderr lines are prefixed `[rotation-due-watch]`. Their
content classes are fixed text; exception text from stdin parsing, the module
import, transcript timestamps or the throttle-marker write; for a failed call, the process key with the
OS or timeout exception text (a timed-out call's line includes its full argv
and payload, which carry counts and identifiers only) or the exit status with
at most 200 characters of the child's stderr; and, when the platform does not
complete the call, at most 300 characters of its reply. Best-effort
telemetry, read by nothing.

**Code the hook does not reach.** The source file also contains a
seat-identity helper (`_resolve_seat_instance_id` and what it calls: a `ps`
read of the process tree and a `peer_list` bridge call). `main()` calls none
of it, so it is not behavior of this hook; it is named here so that a reader
auditing the file's `subprocess.run` call sites does not mistake it for a
disclosed one.

## The memory-passthrough files — two hooks, four CLI utilities, one shared library

Six files, three of which are already covered above by category (`capture.py`
writes and never subprocesses; `session_context.py` neither writes nor
subprocesses; `sync.py` is one of the disclosed subprocess-capable
files listed at the top of this document). This section states each file's full, individual contract.

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
  shell out, via the same PATH-resolved `solet-bridge` convention as the
  rotation-due watch hook: one `export_memories` call on
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
rotation-due-watch/capture/session-context parity legs above.

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

A compaction runs SessionStart again with source `compact`; the same two reads run under the same caps and the output has the same shape, so nothing new is read, written or retained.

`session_start_context.py` replaces the static message and role reminders on
SessionStart `startup`, `resume`, `clear`, and `compact`. It performs two read operations:

- `[SOLET_BRIDGE, "call", "plugin::agent_messaging_plugin::peer_inbox", JSON]`
  pages the instance and role sections with `observer: true`. `SOLET_BRIDGE`
  defaults to `solet-bridge` on PATH. The original `SOLET_NAME`,
  `AGENT_SESSION_ID` and `AGENT_INSTANCE_ID` are preserved and required.
  Current bound role names come from `role_read_coverage`, never the local
  label. Forward instance cursors preserve timestamp ties; role cursors are
  independent. The hook never acknowledges a page or advances a covered mark.
- The article principles_statement.md (knowledge base ananta_platform, folder 24_operator_communication) is read
  from the solet's own knowledge-base file,
  `ananta/knowledge_bases/ananta_platform/24_operator_communication/principles_statement.md`, under the first of
  these roots that holds it as a regular file: `CLAUDE_PROJECT_DIR`, the working directory, the tree the bridge runs
  from (`<root>` of `<root>/.venv/bin/solet-bridge`, `<release>` of `<release>/venv/bin/solet-bridge`; a bare
  `SOLET_BRIDGE` name is looked up in `PATH`) and that tree's `code` directory. The path is fixed and the file is
  opened for reading only: the hook reads no other file, writes none, and the text is clipped and labelled as
  before. Only when no root holds the file does the hook run
  `[SOLET_BRIDGE, "call", "service_interface::knowledge_service::read_file", JSON]` for the same article. Either
  way the hook emits the text under the `## Approved statement` heading verbatim after one label line naming the
  article, capped at 8000 UTF-8 bytes; a longer article is clipped at a line
  boundary with a one-line advisory. A file without that heading gives one typed `StatementMissing` line and no
  bridge read. The hook neither reads credentials nor prints child stderr.

A session with no launcher identity also gets one line naming the fleet identity
that the read-only `_session_identity.py` helper found and the CLI claim path.

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

The principles are read before the inbox. All inbox pages share one 3.5-second
deadline, and the bridge read of the principles, made only when no statement
file is found, keeps a 3.5-second timeout of its own, so the worst case is 3.5
seconds with the file and seven seconds without, under the thirty-second
manifest timeout. A failed
read adds one explicit `unavailable (ErrorClass)` line and exits zero; a missing
identity is `MissingIdentity`, never zero messages. No cache or static reminder
fallback exists. JSON serialization escapes all observed strings. Inbox context
is capped at 1000 UTF-8 bytes and the principle text at 8000, leaving room
for the unchanged knowledge-base reminder. Over-budget inbox output is
reported as `ByteBudgetExceeded`, never silently clipped into apparent success;
the principle text is clipped only with its one-line advisory.

This stdlib Python 3.13 hook uses `subprocess.run` with argument arrays and no
shell. It creates no files, performs no register writes, claims no role. Hook installation/trust is separate from candidate delivery;
these are same-user coordination controls, not a privilege boundary.

The manifest consistency smoke and the reminder hooks smoke cover this candidate's manifest
and fixed reminder; the live SessionStart subprocess smoke is added by slice S3 of this Change.
This candidate makes no claim of live host deployment.
The independent review unit records the due-diligence verdict required by
`rul_af7c73e3` and `rul_f0e51f93`.

## Working-tree recovery — reviewer due diligence (0.10.0)

`recovery_claude.py` saves a copy of uncommitted work before a shell command
discards it and at the end of every turn, so a lost edit can be put back. It is
not a control: it never refuses, blocks or steers a tool call, prints no
`decision` or `permissionDecision` field, and exits `0` on every path. The save
happens, then the command runs. This section answers the shipped-hook
due-diligence checklist (`rul_f0e51f93`) against these exact bytes; items are
numbered as in that checklist.

**1. Inventory.** One hook and five `hooks.json` entries, each exec form through
`optional_hook_runner.py` (`python3 optional_hook_runner.py recovery_claude.py <Event>`):

- `PreToolUse`, matcher `Bash`, timeout 15;
- `PostToolUse`, matcher `Write|Edit|MultiEdit|NotebookEdit|Bash`, timeout 10;
- `Stop`, no matcher, `async` true, timeout 120;
- `SessionEnd`, no matcher, timeout 10;
- `SessionStart`, matcher `startup|resume|clear`, timeout 10.

Beside the hook ship the shared engine's 23 modules, byte-identical copies of
`coordination_hooks_common/` made by the source repository's materialization tool
(`materialize_hook_common.py`) and listed
by the literal `MODULES` tuple in `working_tree_recovery.py`: `command_lex.py`,
`command_resolve.py`, `command_types.py`, `command_walk.py`, `custody_store.py`,
`discard_forms.py`, `recovery_capture.py`, `recovery_cli.py`,
`recovery_content.py`, `recovery_deadline.py`, `recovery_events.py`,
`recovery_git.py`, `recovery_journal.py`, `recovery_lifecycle.py`,
`recovery_metadata.py`, `recovery_packets.py`, `recovery_rebuild.py`,
`recovery_refs.py`, `recovery_restore.py`, `recovery_retention.py`,
`recovery_rules.py`, `recovery_types.py` and `working_tree_recovery.py`. None is
registered as a hook. `working_tree_recovery.py` is also the user's command line
(`list`, `show`, `restore`, `correct`, `forget`, `unforget`, `prune`, `purge`,
`save`), run by hand like the memory utilities.

**2. Inputs.** From the hook payload: `hook_event_name`, `session_id`, `cwd`,
`tool_name`, `tool_input.command`, `tool_input.file_path`,
`tool_input.notebook_path`, `tool_input.edits[].file_path` and `turn_id` (falling
back to `tool_use_id`), and nothing more. The transcript (`transcript_path`) is
never read. From the environment: `SOLET_RECOVERY_ROOT` (an optional store
location), `HOME`, `TMPDIR`, `XDG_DATA_HOME` and `XDG_RUNTIME_DIR` (non-macOS) and
`PATH` for `git`. It reads the working files that git reports as changed, to copy
them. At `Stop` and `SessionEnd` the hook saves the working folder and every other
folder recorded in this session's journal.

**3. Outputs.** The only stdout of the hook is one JSON object whose only keys are
`systemMessage` and `hookSpecificOutput` (`hookEventName`, `additionalContext`).
`PreToolUse` prints it when a save included changes this session did not make,
when paths could not be saved, when low disk reduced the save, or when no save
could be made ("could not save before ...; the command will run").
`SessionStart` prints `additionalContext` alone when recovery is inactive or the
store is over its cap. `PostToolUse`, `Stop` and `SessionEnd` print nothing. The
command line (`working_tree_recovery.py`, run by hand and registered nowhere)
prints its result as one JSON object on stdout. No
`decision`, `permissionDecision`, `continue`, `stopReason`, `suppressOutput` or
`updatedInput` key can be emitted: the adapter smoke
(`plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/recovery_claude_adapter_smoke.py`)
walks every output of every fixture, the fault fixtures included, at every depth against
a closed key set, and checks that the adapter source names none of those keys.
The text carries up to ten file names from the user's repository (never
contents), a restore command line, and, in `additionalContext`, one sentence
addressed to the model ("If the user did not mean to discard these changes, stop
and tell them before continuing."). File names are repository-controlled text and
so enter the model's context; they are capped at ten and never interpreted.

**4. Children executed.** `recovery_claude.py` spawns nothing itself. The engine
has one spawn boundary, `recovery_git.py` (`_spawn`): `subprocess.Popen` with an
argument list and no shell, running
`git -c core.fsmonitor=false -c core.quotePath=false <subcommand>` in the folder
concerned. Stdin is `/dev/null` or a pipe the hook owns; no child inherits the
hook's stdin, stdout or stderr; each child gets its own session, and a watchdog
kills its process group when the shared deadline expires. Its environment is the
hook's with every `GIT_*` variable removed, then `GIT_OPTIONAL_LOCKS=0`,
`GIT_TERMINAL_PROMPT=0`, `LC_ALL=C` and `GIT_NO_LAZY_FETCH=1`. The subcommands of
a save, all read-only against the user's repository: `rev-parse`, `symbolic-ref`,
`config --get core.ignoreCase`, `config --get-regexp ^alias\.` (aliases are read as
text and never executed), `ls-files` (`--stage`; `-o --exclude-standard`),
`ls-tree -r -z`, `diff-files --no-ext-diff -z --raw`, `clean -n` (a dry run),
`stash list`, `for-each-ref`, `log -1 --format=%B`, `rev-list --parents -n 1`,
`merge-base`, and three that take requests on a pipe the hook owns:
`cat-file --batch-check`, `cat-file --batch` and `check-ignore --stdin -z -v -n`.
Only on an explicit user `restore`, never from a hook: `hash-object -w --no-filters
--stdin-paths`, `read-tree`, `update-index -z --index-info`, `write-tree`,
`commit-tree`, `stash store` and `branch`, in the two restore modules. No module
names `pack-objects`, `index-pack`, `checkout`, `reset`, `add`, `commit`,
`update-ref`, `gc`, `fetch`, `push` or `pull`; `manifest_consistency_smoke.py`
checks both statements against the source.

**5. No writes to the user's repository during a save.** Reads use
`GIT_OPTIONAL_LOCKS=0`, so no index refresh is attempted, and no subcommand that
writes a ref, the index, configuration, a hook or an object is named outside the
restore modules. A save never makes a commit or a branch.
The adapter smoke takes a digest of every file under `.git`
before and after a real `PreToolUse` save and requires them equal. The ignore-rule
check for what a stash or deleted branch carries runs against a throwaway
directory outside the repository and the store (item 7) and only reads the
repository.

**6. Repository-configured programs.** Every read passes `-c core.fsmonitor=false`,
uses raw diff output with `--no-ext-diff` and no `--textconv`, and reads objects
without filters, so a save never runs a program named in the repository's
configuration. Configuration is read only as text (aliases); the hook never
executes it. On restore, `hash-object` runs with `--no-filters`, so no clean filter
runs, and `restore --refs` updates refs through git, so the repository's own
`reference-transaction` hook, if any, runs and may refuse that entry.

**7. Files written.** The store, and nothing else outside the repository except
the throwaway rules directory below and Python's own byte-code (the `__pycache__`
note above). Default root: `~/Library/Application Support/Solet/Recovery/v1` on
macOS, `$XDG_DATA_HOME/Solet/Recovery/v1` (default `~/.local/share/`) elsewhere;
`SOLET_RECOVERY_ROOT` overrides it, and an override inside a git repository or under
`Documents`, `Desktop`, `Downloads`, `.claude`, `.codex`, iCloud or CloudStorage is
ignored with an advisory. An empty or blank `SOLET_RECOVERY_ROOT` is
treated as unset. Directories are mode `0700` and owned by the user; stored content, manifests
and the `.metadata_never_index` marker (which keeps Spotlight out) are `0400`; the
append-only journal and history files and the per-folder lock files are `0600`. Content is stored
once, in a pool, and hard-linked into each save (a verified copy where the volume
refuses links). Beside the saves it keeps a per-session journal (`edits.jsonl`: for
each edit a file tool reported, the absolute path, a content hash, the folder and a
timestamp, never content), per-folder lock files, an append-only history of saves,
restores and prunes, and an optional `config.json`. The one file written elsewhere:
when a stash or deleted branch carries ignore rules that must be applied, those
rule files are written to a `0700` directory made with `mkdtemp`
(`solet-recovery-rules-<pid>-*`) under the OS temporary directory, the macOS
per-user temporary directory, `~/Library/Caches/Solet/recovery-rules` (macOS) or
`$XDG_RUNTIME_DIR` (elsewhere). Before any
byte is written the directory is checked not to resolve inside the store, through a
symlink or an alias too; it is removed in a `finally`, and a killed run's
leftovers (dead pid, older than ten minutes) are swept by the next run. If no
candidate is outside the store, no ref-rule classification runs and nothing that
needed it is copied.

**8. Content sensitivity.** Saves are copies of the user's own working files. They
can contain secrets that the user keeps tracked, or untracked but not ignored; that
includes a tracked file force-added despite an ignore pattern, because git treats
tracked files as visible. The invariant: no ignored file content is ever
persisted in the store.
Every path is classified with git's own matcher (`check-ignore`) before any byte is
stored; for a stash or deleted branch a path is left out when the folder's current
rules or the ignore files that ref carries match it; a classification that is
incomplete copies nothing. Names of ignored paths can appear in a save's unsaved
list and in a notice; their contents cannot. One residual, a known limit: a rule
that lived only in `.git/info/exclude` or `core.excludesFile` and was removed before
the save cannot be applied to a stash's untracked files. Another, also a known
limit (R19): the rule text a ref carries, which can include a `.gitignore` that is
itself ignored, sits briefly in the rules directory outside the store and is
removed when the classification ends; it is never written to the store. The store is user-only,
never indexed by Spotlight, and backed up by Time Machine like any user data; the
hook does not change backup settings. Nothing leaves the machine.

**9. Network.** None. No recovery module imports a network module, and none calls
the bridge, the register or a solet verb; `manifest_consistency_smoke.py` checks
the imports of the adapter and of every engine module.

**10. Resource bounds.** Per save: 512 MiB of new content, 50,000 files and 64 MiB
per file. A 5 GiB store: past it a prune pass runs and the person is told once per
session at `SessionStart`; saves not yet delivered are never pruned. At most once an
hour the `Stop` hook also runs a prune pass of its own, which deletes only inside
the store (below). A 10 GiB disk
floor: below it only the index and tracked files are saved, and the person is told
once per session. Time: one shared deadline covers the whole hook, every git child
included: 8 seconds for `PreToolUse`, `PostToolUse`, `SessionStart` and
`SessionEnd` (inside their declared 15, 10, 10 and 10), and 60 seconds for `Stop`
(inside its declared 120, asynchronous); one second of the budget is reserved to
publish what was captured, two to list untracked files, and a held store lock is
waited for at most one second. A save counts as delivered when its content is found
in `HEAD` or a local branch; it is then kept for 30 days, trashed for one day and
purged, and the newest turn-end save of each session is always kept. These figures
are fields of `<store>/config.json`, set or corrected there and restored to their
default by removing the field: `cap_bytes`, `cap_files`, `cap_file_size`,
`store_bytes`, `disk_floor`, `grace_seconds`, `trash_seconds`, `hard_seconds` (the
8 second deadline, applied to `PreToolUse` only), `publication_seconds` (the one
second publish reserve) and `untracked_seconds` (the two second untracked-listing
budget). The 60 second `Stop` budget, the 8 second deadline of `PostToolUse`,
`SessionStart` and `SessionEnd` and the one second lock wait are fixed in the code.

**11. Failure modes.** Every path exits `0` and prints a few short `[recovery]`
lines on stderr (at most one for a fault and one for advisories). In effect: `git`
missing, no save and nothing said; not a
repository, no save for that folder; store unwritable or disk full, "could not save
before <command>: <reason>; the command will run" to the person and the model, then
the command runs; a git child that stalls is killed at its deadline, and what was
captured is published with the rest named unsaved; paths whose ignore status cannot
be determined are not saved and are named in the notice; a file that changes while
it is read is read once more and then named unreadable; a FIFO, socket or device
file is never opened; a held store lock is waited for at most one second, after
which the save is published without it; a malformed or unknown
payload, or a `hook_event_name` that does not match the event, is a no-op; Python
below 3.11, the adapter exits `0` before importing any engine module and
`SessionStart` says recovery is inactive; the script missing from a stale or partial
plugin cache, `optional_hook_runner.py` skips it with exit `0`. A hook killed by
Claude Code's own timeout is, by its documented default, reported and the action
goes ahead; that default is not measured here (see Unmeasured below). Restore never
overwrites a newer edit: an existing file is parked in a private sibling directory,
kept, and its path reported.

**12. Never blocking.** The threat-model statement that this plugin has no control
stays true and is extended: the recovery hooks are not a control either. They save,
then the command runs; they never refuse, and nothing in them can emit a decision
field.

**13. Supply chain.** Standard library only. The adapter imports `json`, `sys`,
`contextlib`, `pathlib` and the engine facade; no engine module imports a network
module or a third-party package. A Python 3.11 floor guard runs before any engine
import. No new dependency. The engine copies are byte-identical to
`coordination_hooks_common/`: the check is the materialization tool's
`--check --dest <this hooks directory>` mode (`materialize_hook_common.py`, in the
source repository; its identity smoke,
`plugins/github_midwife_plugin/tests/hook_common_identity_smoke.py`, exercises the
tool against a temporary destination, not this directory), and the manifest smoke proves that every module
the shipped `MODULES` tuple names is present.

**14. Registration.** Exec form (`command` `python3` plus an `args` array), no shell
string, `${CLAUDE_PLUGIN_ROOT}` substituted per argument. The entries go through
`optional_hook_runner.py` because a bare interpreter call on a missing script exits
`2`, which blocks a `PreToolUse`, while the runner skips a missing script with exit
`0`. `manifest_consistency_smoke.py` pins the five entries (events, matchers,
timeouts, `async`, argument vector), pins the twelve existing entries unchanged and
proves that twelve named defects in the registration are each caught.

**15. Trust and revocation.** Claude Code runs the entries once this plugin is
installed and enabled; disabling or uninstalling it revokes them. Uninstalling
leaves the store in place. To remove saves, run `python3 working_tree_recovery.py
forget <save>` and `purge`, or delete the store folder; nothing else refers to it.

**16. Interpreter pin.** The Manager's `pin_hook_interpreter` rewrites every bare
`python3` hook command in `hooks.json` to `<clone>/.venv/bin/python3`; the five
entries use the same bare `python3` form and are covered. The two routes are the
pinned interpreter and the bare `python3` on `PATH`; below 3.11 either exits `0`
and says so at `SessionStart`.

**Unmeasured, stated so it is not read as measured.** The fixtures behind
the adapter smoke are synthetic: they follow Claude Code's
documented hook payloads and were not captured from a live session. Two assumptions
stay unmeasured until the live acceptance run: that Claude Code honours
`hookSpecificOutput.additionalContext` on `PreToolUse` (if it does not, the person
still sees `systemMessage` and only the model-facing sentence is lost), and the
bound Claude Code puts on a `SessionEnd` hook (the declared timeout is 10).

## Deployment context (disclosed so nothing is discovered later)

The knowledge-base reminder stays generic. Live context explicitly calls the
solet-scoped bridge interfaces under the configuration above.
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
do. None of the hooks in this plugin read, send, or attribute a peer
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
none. Its reminders, wake waiter, context gauge report, memory-passthrough
utilities and headless tool-allowlist gate are steering and bookkeeping, not a
git-mutation control. The git-text gate removed in `0.9.0` (operator ruling
`rul_e71e43be`) is inventoried in `README.md` under *Retired behaviours*: its
git-mutation ban, its `Agent`/`Task` spawn ban, its Bash-level `.git/` block,
its inline `git -c` and global-option bans, its `--no-verify` ban with the
operator-confirmation prefix, and its refusal of `git` beside a shell evaluator
word or an untokenisable construct are all unenforced, and no control in this
plugin replaces any of them.

The working-tree recovery hooks (`recovery_claude.py`, in their own section) are
not a control either: they save uncommitted work and let the command run. They
never refuse, restore nothing by themselves, and put no decision field in their
output.

## Failure modes

- **Working-tree recovery**: every path exits `0`, never `2`, and prints a few
  short `[recovery]` stderr lines (at most one for a fault and one for
  advisories). A store that cannot be written, a missing `git`, a
  malformed payload and an interpreter below 3.11 each degrade to "no save", with
  the person told only where a save was expected; a script missing from a stale
  plugin cache is skipped by `optional_hook_runner.py`. The full account is item 11
  of the Working-tree recovery section.

- **Knowledge-base reminder**: always emits context, exit `0`.
- **Live session context**: read failures emit named unavailable context, exit `0`; no zero-count fallback.
- **Wake waiter**: disarmed → immediate exit `0`. Armed, it is a
  deliberately long-running hook (registered `asyncRewake` with a 24-hour
  timeout): it blocks at zero model cost while the session is idle. If the
  configured CLI cannot be spawned it exits `0` with a one-line stderr note
  — a broken wake path degrades to "messages wait for the next turn"
  (where the reminders prompt an explicit check), never to a stuck session.
- **Rotation-due watch**: none of the six managed variables set →
  immediate exit `0`, silent. When any is set but `AGENT_INSTANCE_ID` or
  `AGENT_SESSION_ID` is missing, or the receipt fails verification, it prints
  one `[coordination-owner]` stderr line and exits `0`. Every other failure
  mode (missing `CLAUDE_PROJECT_DIR`, unreadable transcript, malformed stdin,
  unresolvable `solet-bridge` binary, a non-zero or unparseable
  `solet-bridge call` result) warns to stderr and exits `0` — never `2`, never
  blocking a tool call. A broken rotation-due watch degrades to "the stored
  context gauge row goes stale" (a session's context reading stops updating,
  never a stuck or crashed tool call).
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
  which the plugin never reads). The rotation-due watch hook carries one
  small, local piece of state between invocations by design (its throttle
  marker, `write_text(str(time.time()))`) — never read by anything outside
  that hook. The memory-passthrough
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
| Exactly the six disclosed hooks/utilities (live session context, wake waiter, rotation-due watch, `sync.py`, the session-cron sentinel, and `recovery_claude.py` through the engine's one spawn boundary) can execute a subprocess; each uses `subprocess.run` only, no `shell=True`, except that boundary, which uses `subprocess.Popen` with an argument list and no shell | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| No network I/O in any hook | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| No hook writes a file as an action of its own, except the seven disclosed exceptions and the local recovery store, which only the engine modules write | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` and `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/wake_waiter_smoke.py` |
| The one MARKER_ONLY writer (rotation-due watch) contains the bare timestamp marker write `write_text(str(time.time()))` (see its section above) | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| Supply chain is stdlib/built-ins only, except `rotation_due_watch.py`'s disclosed same-platform import and the memory-passthrough CLI utilities' disclosed intra-plugin imports of each other | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| Agent-invoked CLI utilities (`drain.py`/`hydrate_render.py`/`index_render.py`/`sync.py`) are correctly NOT wired in `hooks.json` — they never fire automatically | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| `hooks.json` is exec-form with no shell in the invocation path | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The hook inventory on this page matches the tree | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| `recovery_claude.py` prints only `systemMessage` and `hookSpecificOutput` (`hookEventName`, `additionalContext`) for every fixture, the fault fixtures included, and never a decision-shaped key; it exits `0` on every fault; below Python 3.11 it exits `0` before any engine import | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/recovery_claude_adapter_smoke.py` |
| A real `PreToolUse` save leaves the repository's `.git` byte-identical, and the saved bytes restore after the discard | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/recovery_claude_adapter_smoke.py` |
| The five recovery entries (events, matchers, timeouts, `async`, argument vector) are pinned, the nine existing `hooks.json` groups are unchanged, and twelve named registration defects are each caught | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The engine ships as the 23 listed modules, with one spawn boundary, no shell, no network import, no history-, ref-, pack- or remote-writing git subcommand, and object- or index-writing ones only in the restore modules | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
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
| The rotation-due watch hook's `solet-bridge call` argv carries its fixed process key, never a shell, never `subprocess.call`/`Popen` | `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/manifest_consistency_smoke.py` |
| The rotation-due watch hook never prints a `hookSpecificOutput` block | disclosed here (source-read); no dedicated behavioral smoke ships in this plugin's own `tests/` yet (see the Known gaps note below) |
| The origin-resolution ladder (env → root_manifest.yaml, placeholder-skipped → dirname) resolves identically across both independent implementations (the hooks' `_journal.py` and the memory-tag verb resolver), across a matrix of env/file/dirname combinations | `.claude/hooks/tests/memory_passthrough_origin_ladder_smoke.py` — checkout-external, see the Known gaps note below |
| The rotation-due watch, capture, and session-context hook copies in this plugin and in the checkout's `.claude/hooks/` behave identically (throttle marker path, arming, argv/output shape) | the checkout's own `.claude/hooks/tests/coordination_hook_ports_smoke.py` parity legs — external to this plugin's own `tests/`, so not run by `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/tests/run_all.py`; see the Known gaps note below |
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
rotation-due watch's behavioral smoke is checkout-external
(.claude/hooks/tests/rotation_due_watch_smoke.py), not in this plugin's `tests/`.
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
shell string interpolation anywhere in the invocation path. The 23
working-tree-recovery engine modules are standard library only and need Python
3.11 or later; `recovery_claude.py` exits `0` before importing them on an older
interpreter (Working-tree recovery, items 13 and 16).

## Configuration surface

Ten of the fourteen hooks are default-off or agent-invoked utilities;
`session_cron_sentinel.py` observes matching tool attempts;
`step_zero_reminder.py` and `session_start_context.py` always run at the
registered start sources; `recovery_claude.py` always runs, in every session, with
no environment gate (it has to fire in a user's own session), and reads only the
optional `SOLET_RECOVERY_ROOT` and the store's `config.json`. Missing register or identity configuration produces
explicit unavailable context for the latter. Existing rotation-due watch,
wake, memory and spawn-injected guards retain their documented behavior.

**Adopter setup note.** The rotation-due watch hook's
arming variable, `AGENT_INSTANCE_ID` (it also needs `AGENT_SESSION_ID` and a valid owner receipt), and `SOLET_NAME` (rung 1 of
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
identity. The receipt still gates the rotation-due watch's effects
through `coordination_owner.py`, unchanged.

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
