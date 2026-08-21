# Deployment Report Card — dax — 2026-08-12 (post-update run, re-mint 040d7d4 / HEAD 477619e)

Most of what makes dax useful day to day is working — search, credential storage, messaging role-binding, and the update itself all check out live. The clearest problem is that session history stopped feeding search on July 28 and nothing has caught it up since, and a chunk of plugin knowledge bases — including the memory, database, and embeddings plugins' own — are running with an empty index. Section-by-section detail and one recommended fix follow.

## 1. Setup — exception-based (4 of 5 clean, 80%)

| Dependency checked | ✅/⬜/➖ | Evidence |
|---|---|---|
| Postgres reachable (`postgres_state_management_plugin`, host `localhost:5432`, role `dax`) | ✅ | `pg_isready` → "accepting connections" |
| Embeddings endpoint serving the configured model (`openai_embeddings_plugin` → LM Studio `localhost:1234/v1`) | ✅ | `/v1/models` lists `text-embedding-nomic-embed-text-v1.5-embedding`, the exact model the plugin config names |
| tmux present for the messaging plugin's worker host | ✅ | `tmux -V` → `tmux 3.7b` |
| Up to date with the seed it was born from | ✅ | `git log` HEAD `477619e` is the merge of re-mint `040d7d4`; working tree clean (one unrelated untracked file); this session's own env shows `HOMUNCULUS_RELEASE_ID=rel-20260812T190032Z-477619e`, confirming the running release matches |
| Blue-green router: exactly one active color, no orphaned instances | ⬜ | Router process is alive and correct (`HOMUNCULUS_COLOR=green`, matches today's release) — but I found **four** separate `ananta.cli` processes running against this same profile directory; only one (PID 96799) is under LaunchAgent control. The router's own status verb (`swap_status`/`deploy_status`) isn't in the current process registry, so I could not confirm via API whether the other three are harmless drained colors or a leak — flagging rather than guessing |

## 2. Launch environment (3 of 4, 75%)

| Component | ✅/⬜/➖ | Evidence |
|---|---|---|
| Launcher configured and actually in use | ✅ | `client/bin/claude-dax` present (0755); `coordination-hooks` is installed under two real, dated entries (`~/.claude/plugins/installed_plugins.json`, installed 2026-08-11) — genuine recent use, not just a file on disk |
| Platform running now | ✅ | `local.homunculus.dax` PID 96799 stable since Mon Aug 10, 13:54 — 1 day 22+ hours uptime |
| Configured to run at startup | ✅ | LaunchAgent plist: `RunAtLoad = 1`, `KeepAlive` set |
| Required hooks available and proven to work | ⬜ | They fire correctly for programmatically-spawned workers — proven live: this very session's process reads the checkout's current hook files directly. But the installed plugin cache backing **interactive** `claude-dax` sessions is pinned to `coordination-hooks` **0.5.1** (installed 2026-08-11), while the checkout is already at **0.5.4** (today's re-mint). Interactive sessions are running last week's hooks — missing the step_zero_reminder stdin-echo fix that shipped in this update |

## 3. Memory (1 of 7, 14%)

| Component | ✅/⬜/➖ | Evidence |
|---|---|---|
| Canonical store reachable | ✅ | `memory_stats` answered live: 796 memories (793 active / 3 archived) |
| Hydrate round-trip | ⬜ | Not exercised this pass — a real write cycle, deferred on a read-only run |
| Drain round-trip | ⬜ | Not exercised this pass |
| Capture hook fires live | ⬜ | Not exercised this pass |
| Echo-break holds | ⬜ | Not exercised this pass |
| Decay/consolidation exemption in force | ⬜ | Not checked this pass |
| Local index within its size budget | ⬜ | Not checked this pass |

## 4. Session ledger (1 of 5, 20%)

| Component | ✅/⬜/➖ | Evidence |
|---|---|---|
| Past sessions ingested (every coding agent in use — Claude Code only; no Codex here) | ✅ | 410 sessions / 86,039 events on file across 3 real sources (312 local sessions, 76 cloud sessions, 22 agent-messaging sessions) |
| Ingestion currently active, not stalled | ⬜ | **Stalled.** Newest ingested content is dated 2026-07-28 — 15 days ago — despite substantial real work since (today's own seed update, git commits). `list_active_sessions` is empty; no scheduled poll job found under either plausible tag |
| Summarization actually landing (checked via origin discriminator) | ⬜ | Could not confirm — `list_sessions` doesn't surface a summary/origin field through the call I have, and I didn't find the discriminator verb this pass |
| Whether a **local** model performs summarization | ⬜ | Not established — `default_inference_plugin` (the local fallback) shows as not currently running; which binding actually backs summarization is unconfirmed |
| Embedding drain functional (content findable via search) | ⬜ | **Broken.** `event_embedding_coverage` → `embedded_chunk_count: 0`, `caught_up: false`. Every search tried — including the trivial query "the" — returned zero results |

## 5. Peer communications (1 of 4, 25%)

| Component | ✅/⬜/➖ | Evidence |
|---|---|---|
| Role bindings resolve to a live claim, not just listing presence | ✅ | `peer_holds_role` correctly returned `holds=true` for the session actually holding "Git-Controller" right now, and `holds=false` for my own session against a role I never claimed — the live-ownership check works as designed |
| Send → wake round trip | ⬜ | Not exercised this pass, to avoid interrupting the live Git-Controller lane mid-task. Noted in passing: the one live role I checked showed `delivery_route_attached: false` — a message to it right now would queue durably rather than wake it live |
| Durable inbox readable after an outage | ⬜ | Not exercised this pass |
| Stale-binding detection (a claim predating a restart/reconnect) | ⬜ | Only the "never claimed" case was exercised, not a genuinely stale pre-restart binding |

**Worth flagging directly:** the task brief for this run asserted a tmux-hosted Git-Controller was spawned and driven earlier this session via `spawn_session`/`drive_session`. I could not independently re-confirm that — the only tmux session running right now is this report-card session's own, and the live "Git-Controller" role is currently held by a long-running `dax watch --role Git-Controller` background process (up since Monday), a different transport than a fresh tmux spawn. That earlier tmux session, if it ran, has already exited and left nothing to inspect. I'm reporting what I could verify rather than repeating the assertion.

## 6. Knowledge base (1 of 6, 17%)

| Component | ✅/⬜/➖ | Evidence |
|---|---|---|
| Available and searchable | ✅ | Answered real queries correctly and repeatedly throughout this session |
| Embeddings complete, no plugin left vacant | ⬜ | **13 of this deployment's 34 active plugins have a populated knowledge base; 21 don't.** 12 installed KBs show 0 chunks (including `actr_memory_plugin`, `postgres_state_management_plugin`, `openai_embeddings_plugin`, `pgvector_service_plugin` — the memory, database, and embeddings plugins themselves), most untouched since the original 2026-07-22 install. 9 more active plugins have no knowledge-base install row at all |
| Retrieval self-tests green | ⬜ | Ran live just now: **8 of 16 passed (50%)** — 7 target-ranking drifts and 5 forbidden-query overreaches, plus stale process-key references in the report |
| Up to date after content changes / reindex freshness | ⬜ | Not tested this pass with a live edit |
| This runbook's own cited reference articles present | ⬜ | Two are missing from this checkout: `ananta/knowledge_bases/ananta_platform/23_plugin_inventory/01_full_plugin_roster.md` and `workbench/2026-07-16_unified_memory_passthrough_design_v2.md`. I substituted individual plugins' own reference articles to derive the roster above |

## 7. Session management — demonstrated matrix (2 of 5, 40%)

| Cell | ✅/⬜/➖ | Evidence |
|---|---|---|
| Spawn — this model, tmux (this session) | ✅ | Directly observed live in `ps aux`: this session was spawned via `agent_messaging_plugin`'s tmux adapter |
| Drive — this model, tmux (this session) | ✅ | Executing this dispatched brief right now |
| Terminate — this session | ⬜ | Still running; not attempted |
| Spawn + drive — Opus, tmux (an earlier Git-Controller lane, per the dispatching brief) | ⬜ | Asserted by the brief, not independently re-confirmed — see the note under Peer communications |
| Clean terminate — any model/tier | ⬜ | Not demonstrated anywhere this pass |

## 8. Plugins — one row per active plugin (11 of 30 scored functioning, 37%; 4 not scored — access state only)

| Plugin | ✅/⬜/➖ | Setup / usage / access |
|---|---|---|
| Credential/address book | ⬜ | Holds 6 real entries, used successfully this session; its own KB is vacant (0 chunks, stale since 2026-07-22) |
| File/blob storage | ✅ | KB populated (4 chunks, reindexed today); infra, no credential gate |
| Session history: Claude Code (push-based) | ⬜ | Declared but not registered as an active source; no KB install row |
| Session history: push-shipper setup | ⬜ | Zero active processes; supports the unregistered pushed-source above |
| Session history: Claude Code (cloud) | ⬜ | Registered, 76 real sessions on file, but its KB is vacant and ingestion has stalled since 2026-07-28 |
| Peer messaging / fleet coordination | ✅ | Running, well-populated KB (72 chunks, today), exercised repeatedly and directly this session |
| Salesforce connector | ✅ | Credentials on file, KB populated (29 chunks, today); usage not directly exercised this pass |
| Planning / thinking support | ⬜ | Running, but no KB install row under its own name; the related "thinking" KBs are both 0-chunk and stale since 2026-07-22 |
| Session history: Claude Code (local) | ⬜ | Registered, 312 real sessions on file, but ingestion stalled since 2026-07-28 and content isn't searchable |
| Marketo connector | ✅ | Running, credentials on file, KB populated (55 chunks, today); real prior use evidenced (a dedupe-sheet job noted on its own credential record) |
| Platform health checks | ⬜ | KB vacant (0 chunks, stale since 2026-07-29); not running |
| Session memory | ⬜ | Canonical store reachable and holds 796 real memories — functionally alive — but its own KB is vacant, never reindexed since the original 2026-07-22 install |
| Vector search backend | ⬜ | Running and backing real vector search, but its own KB is vacant, stale since 2026-07-22 |
| Session bridge (transcript capture) | ⬜ | Running, but zero registered processes and no KB install row — not independently verified this pass |
| Embeddings (LM Studio) | ⬜ | Endpoint verified live and serving the exact configured model — functionally fine — but its own KB is vacant, stale since 2026-07-22 |
| Developer/debug surface | ➖ | Dev-only surface — not part of this operator's normal use |
| Session history: claude.ai exports | ⬜ | KB install row present but vacant; not registered as an active source |
| Database (state store) | ⬜ | Verified live and reachable (`pg_isready`) — functionally fine — but its own KB is vacant, stale since 2026-07-22 |
| Snowflake connector | ➖ | No credentials on file — not yet configured; configure on first use |
| Session history: Claude Code history file | ⬜ | Declared but not registered as an active source; no KB install row |
| Google Workspace connector | ✅ | Running, credentials on file, KB populated (29 chunks, today); real evidenced use (Marketo dedupe-sheet access, provisioned 2026-07-27) |
| Jira connector | ✅ | Credentials on file, KB populated (17 chunks, today); usage not directly exercised this pass |
| Deployment/hydration tooling | ✅ | This deployment's own hydration and report-card tooling — clearly in active use; best-populated KB of all (262 chunks, today) |
| Credential vault (macOS Keychain) | ✅ | Real usage evidenced indirectly (Salesforce's keychain-backed CLI token); KB present (7 chunks, though stale since 2026-07-22) |
| External Postgres connector | ➖ | No registered connection on file — not yet configured |
| Code security review | ✅ | KB well-populated (68 chunks, today); always-available, no credential gate |
| Session history: Claude Code tasks | ⬜ | Declared but not registered as an active source; no KB install row |
| Self-update / blue-green deploy | ⬜ | The live router process is confirmed running and matches today's release — the update itself worked — but its KB is thin (8 chunks) and see the Setup section's untracked-process finding |
| Zuora connector | ➖ | No credentials on file — not yet configured |
| Knowledge base | ✅ | Proven repeatedly, live, throughout this entire session |
| Terminal-driven coding session control | ⬜ | KB install row present but vacant, stale since 2026-07-22; not running, not in confirmed use |
| Local inference (LM Studio text model) | ⬜ | Not running; this is the piece the Session ledger section couldn't pin down (whether summarization is actually local) |
| Session history: fleet messages | ⬜ | Registered, holds 22 real sessions, but no KB install row and ingestion stalled since 2026-07-28 like the others |
| Scheduling | ✅ | Running, KB populated (7 chunks, today); answered a live call successfully this pass |

## Recommended next step

Session-ledger search has been silently dead since July 28 — 86,039 events are sitting in the ledger with zero of them embedded, so nothing from the last two weeks (including today's own update work) is findable by search. This is the single highest-leverage fix on the card: re-arming the stalled poll/embed schedule would make all of it searchable again at once, with no new ingestion needed. I can trigger that now if you'd like.
