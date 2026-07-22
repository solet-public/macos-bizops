# claude_code_filesystem_session_source_plugin — Hydration Guidance

Article Layer: 2

Article Role: hydration_guidance

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:session_ledger

Embedding Description: Operator-facing pitch and setup steps for ingesting Claude Code session transcripts into a homunculus's own session ledger, surfaced during hydration if this plugin is present but not yet bound and registered.

## Pitch

With this plugin present but not wired up, the homunculus has zero visibility into its own conversation history — no `search_sessions`, no `search_event_content`, no cross-session recall, and the homunculus-first "ask the homunculus what happened" pattern this platform runs on has nothing to search. Every session with it starts from a blank slate.

Enabling this means the homunculus reads your local Claude Code session transcript files (`~/.claude/projects/<encoded_cwd>/<session_id>.jsonl`, one per session, on THIS machine). Nothing leaves the machine, but it is real filesystem access to your own conversation history, and that is exactly why it's opt-in rather than on by default. Ask before wiring it up: "Right now this homunculus can't recall anything about past conversations, even the one you're having right now once it ends. I can set up ingestion of your Claude Code session transcripts so it can search its own history — that means reading your local `~/.claude/projects/` files. Want this set up?"

## Setup

On an explicit yes:

1. **Bind `session_ledger_service`** in `<clone>/profile/config/service_bindings.json` to `postgres_state_management_plugin` (the same plugin already backing `state_service` — the session-ledger schema rides the same Postgres connection; no new binding target).
2. **Set `ledger_allowed_roots`** in `<clone>/profile/config/plugins/session_ledger_service.json` to include the operator's own `root_uri` (default `~/.claude/projects`, or wherever their Claude Code sessions actually live — ask if unsure). The default is `[]`, which denies every filesystem registration; this is the deliberate secure default, and the operator is opting a specific path in.
3. **Register the source** via `service_interface::session_ledger_service::register_source` for this plugin.
4. **Run an initial backfill** via `trigger_poll` so existing session history is ingested immediately, then verify with `service_interface::session_ledger_service::list_sessions` that rows actually landed.

If the operator also uses Codex, see `plugins/codex_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` for the equivalent steps — the `session_ledger_service` binding in step 1 only needs doing once regardless of how many source plugins get added.

On decline: stop, no partial wiring. An empty session ledger is a fully supported, privacy-preserving steady state, not a broken one.
