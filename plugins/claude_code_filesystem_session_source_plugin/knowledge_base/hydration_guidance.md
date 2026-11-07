# claude_code_filesystem_session_source_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:session_ledger

Embedding Description: Operator-facing pitch and setup steps for ingesting Claude Code session transcripts into a solet's own session ledger, surfaced during hydration if this plugin is present but not yet bound and registered.

## Pitch

With this plugin present but not wired up, the solet has zero visibility into its own conversation history — no `search_sessions`, no `search_event_content`, no cross-session recall, and the solet-first "ask the solet what happened" pattern this platform runs on has nothing to search. Every session with it starts from a blank slate.

Enabling this means the solet reads your local Claude Code session transcript files (`~/.claude/projects/<encoded_cwd>/<session_id>.jsonl`, one per session, on THIS machine). Nothing leaves the machine, but it is real filesystem access to your own conversation history, and that is exactly why it needs an explicit yes before wiring. Consent-gated is not the same as optional: this is a CORE capability of the platform — the deployment report card tracks it until configured. Ask before wiring it up: "Right now this solet can't recall anything about past conversations, even the one you're having right now once it ends. I can set up ingestion of your Claude Code session transcripts so it can search its own history — that means reading your local `~/.claude/projects/` files. Want this set up?"

## Polling work and pacing

The ledger's periodic poll normally fires every five minutes. It submits to
the existing single-slot `ledger-poll` worker; discovery does not create a
thread or an unbounded work queue. A slow pass may still be running when the
next heartbeat arrives, in which case that heartbeat reports already running.

Activated Claude filesystem discovery streams directory entries rather than
materializing each directory through `Path.rglob`. It visits only the project,
root-session, and `subagents` directories that can contain the accepted depth-2
root sessions or depth-4 `agent-*.jsonl` sessions. Directory symlinks are not
followed. The configured glob, accepted session identities, newest-first
metadata ordering, and completed-version cursors retain their meaning.

The worker yields for 1 ms after 128 entries or 2 ms of enumeration, whichever
comes first, and leaves a one-second gap after a sweep before scanning the same
root again. These named module defaults bound each enumeration slice, not the
total population or the duration of an individual filesystem call. A sweep
still returns every recognized match; it does not cache or truncate the
population. A configuration replacement clears the in-memory cadence, and
closing an abandoned discovery iterator releases its sweep lock. Unreadable
or disappearing directories produce an availability warning.

Startup environment overrides are `SOLET_CLAUDE_SWEEP_ENTRY_LIMIT` (128),
`SOLET_CLAUDE_SWEEP_SLICE_SECONDS` (0.002),
`SOLET_CLAUDE_SWEEP_YIELD_SECONDS` (0.001), and
`SOLET_CLAUDE_SWEEP_INTERVAL_SECONDS` (1.0). Values must be positive and finite;
the entry limit must be an integer. Correct an invalid value or clear the
override to restore its default, then restart the platform to apply it.

Direct `poll_source` calls use the same dedicated ledger-poll slot as periodic
polling. Busy direct callers wait for that slot and receive the original result
or exception; they do not run importing work on their caller's loop thread.
Calls already on the owner worker remain on that worker. Periodic heartbeats
retain their already-running no-op.

The session bridge likewise hands direct `drain_once` calls to its owned
single-worker executor (`session-bridge-drain_0`), with admission held through
completion so only one job can be submitted. Its periodic worker uses that same
path. Stop drains and
closes the executor off-loop; a later direct call or service start reopens it.
Spool enumeration, metadata/stat work, cursor changes, file draining and deletion
yield between entry/time slices while retaining chronological order and the
watermark anchor. Startup environment settings are
`SOLET_SESSION_SPOOL_ENTRY_LIMIT` (128),
`SOLET_SESSION_SPOOL_SLICE_SECONDS` (0.002),
`SOLET_SESSION_SPOOL_YIELD_SECONDS` (0.001), and
`SOLET_SESSION_BRIDGE_DRAIN_INTERVAL_SECONDS` (5.0). Values must be positive and
finite; the entry limit must be integral. These are worker execution guarantees;
the synchronous caller still waits for its result. They do not claim that every
caller becomes an asynchronous API.

## Setup

On an explicit yes:

Use the no-MCP command path first. Substitute the actual solet name for
`<name>` below; do not translate these steps into `mcp__<name>__process_call`
unless MCP tools are already connected and the operator's machine policy permits
MCP.

1. **Do not edit `service_bindings.json` for `session_ledger_service`.** The session ledger is constructed by platform startup, not bound through `profile/config/service_bindings.json`; adding `session_ledger_service` there is invalid config because it is not a `ServiceName`. Verify the ledger with `<name> call service_interface::session_ledger_service::list_sources '{}'` and check whether `claude_code_local`, `claude_code_history`, or `claude_code_tasks` already have registered rows.
2. **Set `ledger_allowed_roots`** in `<clone>/profile/config/plugins/session_ledger_service.json` to include only the operator-approved local paths. For Claude Code these are usually `~/.claude/projects`, `~/.claude/history.jsonl`, and `~/.claude/tasks`; ask before adding any path that differs. Some seed profiles pre-populate these roots, but this file remains the consent boundary for filesystem ingestion. If you change this file, restart the solet before registering sources; the session-ledger service reads `ledger_allowed_roots` at construction time, and `reload_plugin_config` is not enough.
3. **Register any missing source rows** via `<name> call service_interface::session_ledger_service::register_source '{"source_kind": "claude_code_local", "root_uri": "~/.claude/projects"}'` for project transcripts. If desired, also register `source_kind="claude_code_history", root_uri="~/.claude/history.jsonl"` and `source_kind="claude_code_tasks", root_uri="~/.claude/tasks"` with the same command shape. Registration is idempotent; an existing row should be treated as already wired, not an error.
4. **Run an initial backfill** with `<name> call service_interface::session_ledger_service::trigger_poll '{}'` so existing history is ingested immediately, then verify with `<name> call service_interface::session_ledger_service::list_sources '{}'`, `<name> call service_interface::session_ledger_service::list_sessions '{"vendor": "claude_code", "limit": 5}'`, or `<name> call service_interface::session_ledger_service::search_sessions '{"query": "<known recent topic>", "limit": 5}'` that rows actually landed.

If the operator also uses Codex, see `plugins/codex_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` for the equivalent source rows. The shared setup is `ledger_allowed_roots`, not a session-ledger service binding.

On decline: stop, no partial wiring — the consent boundary is real and a no is respected. But state what it means, and keep it visible: without ingestion this solet has no session memory at all, and the platform treats ledger functionality as core correctness, not an optional extra (operator ruling 2026-08-02, quoted in the hydration runbook's ingestion disclosure). The deployment report card (`plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md`) carries the decline as an unconfigured core row on every future card; re-offer at natural moments rather than silently accepting the gap as permanent.

## Unknown transcript line types

The six recognized Claude Code metadata types (`api-request-blob`, `api-request`,
`api-request-shape`, `atis-latch`, `cost-state`, and `file-history-delta`) are
silently skipped and yield no ledger events. An unknown string top-level `type`
still yields no ledger event and records an advisory. Known messages before and
after it still import with their usage and model identity. Malformed JSON,
missing/non-string `type`, and malformed known events retain integrity errors;
unknown content-block kinds yield advisories and are skipped.
The existing drain completion log groups physical unknown-line counts by source,
session and type, without logging the payload. Known metadata types remain silent.
The public poll result still contains the same four counts.

A successful filesystem read checkpoints all consumed complete lines, including
trailing or all-unknown lines. Partial trailing bytes remain for the next eligible
read; failed reads do not publish a successful checkpoint. A later explicit replay
can legitimately report those physical unknown lines again.

This patch does not automatically repair old abandoned suffixes on unchanged
files: discovery may already have advanced its mtime cursor. An active affected
session becomes eligible on its next natural append. Historical repair requires
separate authorization; this change performs no blanket rescan or history rewrite.
