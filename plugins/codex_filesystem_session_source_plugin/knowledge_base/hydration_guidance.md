# codex_filesystem_session_source_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:session_ledger

Embedding Description: Operator-facing pitch and setup steps for ingesting Codex CLI session rollouts into a solet's own session ledger, surfaced during hydration if this plugin is present but not yet bound and registered.

## Pitch

With this plugin present but not wired up, the solet has zero visibility into its own conversation history — no `search_sessions`, no `search_event_content`, no cross-session recall, and the solet-first "ask the solet what happened" pattern this platform runs on has nothing to search. Every session with it starts from a blank slate.

Enabling this means the solet reads your local Codex CLI rollout files (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`, plus the archived-sessions sibling, on THIS machine). Nothing leaves the machine, but it is real filesystem access to your own conversation history, and that is exactly why it needs an explicit yes before wiring. Consent-gated is not the same as optional: this is a CORE capability of the platform — the deployment report card tracks it until configured. Ask before wiring it up: "Right now this solet can't recall anything about past conversations, even the one you're having right now once it ends. I can set up ingestion of your Codex session transcripts so it can search its own history — that means reading your local `~/.codex/sessions/` files. Want this set up?"

## Setup

On an explicit yes:

Codex stays optional. In a seed installed through setup, selecting the
`codex_local` session source performs steps 2 to 4 for you. The plugin declares
`~/.codex/sessions` as its default root, but boot registers it only once that
root is in `ledger_allowed_roots`, so an install that declines Codex, or a
machine without Codex, indexes nothing. A missing `~/.codex/sessions` is not an
error: the source registers and backfills zero sessions until Codex creates it.

Use the no-MCP command path first. Substitute the actual solet name for
`<name>` below; do not translate these steps into `mcp__<name>__process_call`
unless MCP tools are already connected and the operator's machine policy permits
MCP.

1. **Do not edit `service_bindings.json` for `session_ledger_service`.** The session ledger is constructed by platform startup, not bound through the installed solet's `service_bindings.json`; adding `session_ledger_service` there is invalid config because it is not a `ServiceName`. Verify the ledger with `<name> call service_interface::session_ledger_service::list_sources '{}'` and check whether `codex_local` already has a registered row.
2. **Set `ledger_allowed_roots`** in `<clone>/profile/config/plugins/session_ledger_service.json` to include the operator-approved Codex history path. The usual root is `~/.codex/sessions`; the walker also checks the archived-sessions sibling when it walks that root, so ask before enabling it on a machine where archived Codex history should remain out of scope. If you change this file, restart the solet before registering sources; the session-ledger service reads `ledger_allowed_roots` at construction time, and `reload_plugin_config` is not enough.
3. **Register the source row** via `<name> call service_interface::session_ledger_service::register_source '{"source_kind": "codex_local", "root_uri": "~/.codex/sessions"}'` (or the operator-approved replacement path). Registration is idempotent; an existing row should be treated as already wired, not an error.
4. **Run an initial backfill** with `<name> call service_interface::session_ledger_service::trigger_poll '{}'` so existing history is ingested immediately, then verify with `<name> call service_interface::session_ledger_service::list_sources '{}'`, `<name> call service_interface::session_ledger_service::list_sessions '{"vendor": "codex", "limit": 5}'`, or `<name> call service_interface::session_ledger_service::search_sessions '{"query": "<known recent topic>", "limit": 5}'` that rows actually landed.

If the operator also uses Claude Code, see `plugins/claude_code_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` for the equivalent source rows. The shared setup is `ledger_allowed_roots`, not a session-ledger service binding.

On decline: stop, no partial wiring — the consent boundary is real and a no is respected. But state what it means, and keep it visible: without ingestion this solet has no session memory at all, and the platform treats ledger functionality as core correctness, not an optional extra (operator ruling 2026-08-02, quoted in the hydration runbook's ingestion disclosure). The deployment report card (`plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md`) carries the decline as an unconfigured core row on every future card; re-offer at natural moments rather than silently accepting the gap as permanent.

## Backfill progress and large sessions

The staged polling implementation is complete when both runtime Part A and
filesystem Part B are present. Codex filesystem, Claude filesystem, Claude
tasks, and Codex ambient explicitly activate safe whole-session polling;
there is no operator activation toggle. The normal five-minute heartbeat
admits sessions newest first using a stable `(mtime_ns, relative path)` pair.
The default 60-second deadline is cooperative between whole sessions. One
session, stat enumeration, header reading, or Codex prefix/model decoding may
overrun; this is not a universal wall-clock bound. `CODEX_HISTORY`,
`CLAUDE_CODE_HISTORY`, `CODEX_GOALS`, and `CODEX_STATE` remain NON-YIELDING.
Their full polls can still delay the normal producer; unknown kinds are advised.

A source's `caught_up` state means its regular-size sweep reached actual EOF.
It does not mean all history is imported. EOF clears the sweep boundary and
next tick enumerates again, including old appended files and new non-head
paths. Successful stable physical EOF versions suppress unchanged content
reads. Partial records, unstable versions, and failed checkpoint writes stay
pending; existing confirmed cursor reset reopens completed versions. Task and
ambient snapshots replay completely with their existing event IDs; this does
not repair mutable task history. Replacement/truncation and unproved legacy
rollout offsets replay with an advisory. Deleted unseen content is unavailable.
Codex's population remains recursive live rollout files plus the **flat**
archived-sessions sibling; nested archive files are not added by this change.

Sessions whose aggregate input exceeds **50,000,000 bytes** are named and
counted as deferred; equality stays regular. After a normal heartbeat completes,
a separate single-slot worker attempts at most one whole oversized session.
Under the source lease it first rechecks fresh regular work. If any appears,
only regular state becomes `backfilling`, preserving its version/boundary, and
no large read starts. Contention writes nothing; a successful large pass leaves
the regular cursor unchanged. Later heartbeats retry, with eligibility rebuilt
from durable event cursors. Other source polls can complete while this worker
blocks, but recent same-source work waits for its lease. No new cron, queue
table, or process is introduced.

The certified seat design census counted 9,110 files/21,149,451,035 bytes
(9,108 live and 2 flat archives), SHA256
`0e1d4aaeb72954c5af005482d6b69eb0f33391135c59915a76c88b142f22fac4`.
A later separately attributed seat snapshot reported 9,112/about 21.18 GB,
39 above 50 MB and maximum about 513 MB. Neither is a current full-corpus
completion claim. After deployment the seat must record fresh file/byte
coverage, three scheduled regular polls, an oversized completion with two
other-source regular completions during it, lease/slot release, and nonempty
current Codex usage/model data when its lease is available. A trigger response,
a passing finite batch, or `caught_up` alone is not that live acceptance.
