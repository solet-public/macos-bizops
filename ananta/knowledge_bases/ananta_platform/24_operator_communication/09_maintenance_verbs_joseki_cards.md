# Maintenance-Verbs Joseki Cards: Rotation, Seat Self-Rotation, Restart, Memory Sync, KB Refresh, Context Gauge

Tags: knowledge:tag:maintenance_verbs, knowledge:tag:joseki, knowledge:tag:session_lifecycle, knowledge:tag:memory_passthrough, knowledge:tag:context_gauge

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:agent-messaging, consumer_profile:both

Embedding Description: Ordered-call joseki cards for seven recurring fleet maintenance operations — rotate a worker, rotate the operator seat itself through a delegated helper that drives its terminal, restart a dead worker, sync the local memory-passthrough projection, curate the ambient MEMORY.md head at a rotation boundary, refresh a plugin's KB/process registry, check a session's context-window occupancy — each with its verify step and known traps. Follow the card, don't re-derive the sequence.

**When you need this**: a session is about to rotate, restart, or check on another managed session; the operator seat itself needs its context cleared and resumed mid-programme without an operator keystroke; a session needs to hydrate or drain its local memory-passthrough projection; a seat is at a rotation boundary or just hit a hydrate budget failure and needs to curate the ambient MEMORY.md head back under budget; a session is editing a plugin's KB process JSON or config and needs to refresh the live registry; a session (worker or the operator seat) needs to know how close it is to its context-window ceiling. Design records: `workbench/2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md` (maintenance-verbs programme, Lane A, M0/M1) and the 2026-08-10 maintenance-verbs M2 memory-curation charter draft (M2, ambient-index curation & placement decay).

---

## Card: Worker rotation

Clears a managed worker's context and drives its next work turn — the "rotate in place" operation, distinct from a restart (which kills and respawns the process).

1. `plugin::agent_messaging_plugin::list_sessions` — resolve the ledger `agent_instance_id` by `lane_id`/`host_ref`. **Never** the id from a role-thread message or `peer_list` — that is the watch-transport worker's OWN watch-registration id (`agi-watch-*`), a different value by construction. `clear_session`/`drive_session`/`terminate_session`/`session_status` all key on the spawn-time LEDGER id and reject the watch id loudly (`session_not_found`).
2. `plugin::agent_messaging_plugin::peer_send_by_name` with `name=<the worker's role>` and a pickup pointer to the worker's own handoff note plus "drain your role inbox." Durable — delivers even if the driven turn below races it.
3. `plugin::agent_messaging_plugin::clear_session` with `agent_instance_id=<ledger id>`, `park=False`, `directed_by=<your role>`.
4. `plugin::agent_messaging_plugin::drive_session` with the same `agent_instance_id`, `text=<the same pickup pointer>`, `directed_by=<your role>`.
5. **Verify — do not skip.** Re-check `session_status` after a short wait, or watch for a positive transcript/mtime signal. A `queued_notification`/`queued_wake` delivery receipt from step 2 or 4 is a confirmation that the message was ACCEPTED for delivery, never a confirmation that a turn actually ran.

**Traps:**
- *Ledger id vs. watch id* (step 1) — a watch-transport worker has TWO different `agent_instance_id`s; `peer_send_by_name` wants the role (routes via the watch id), the fleet lifecycle verbs want the ledger id. Neither accepts the other's.
- *Armed ≠ fired* (step 5) — a rotation recorded as complete on the strength of a delivery receipt and one `busy`/`idle` reading, with no positive observation of an actual turn, is exactly the failure mode that left a seat un-rotated for 6.4 hours in a recorded 2026-08-09 incident. A delivery confirmation is not a turn.

## Card: Seat self-rotation (delegated helper)

Clears the operator seat's own context and resumes it mid-programme — the seat-side counterpart of worker rotation. A managed (tmux-hosted) worker never needs this card: the worker-rotation card's `clear_session` + `drive_session` verbs are its mechanism. This card is for the session with no manager above it — it delegates the drive to a helper session it dispatches itself, using only primitives every deployment has (spawning plus terminal injection), never a vendor remote-control capability. Proven live 2026-08-13: a helper cleared the driving seat and resumed it mid-programme, and the seat's durable identity and role binding survived the clear intact, with no re-claim needed.

1. **Checkpoint.** Drain pending memory write-through, bring the workbench records current, and confirm no worker is holding for a go-signal only this seat can send.
2. **Write the resume handoff file** in workbench: what is in flight, the read order for the fresh context, and the single first action.
3. **Dispatch the helper** (an inexpensive model tier — the task is mechanical) with a brief naming the target surface and the only two texts it may ever inject: the literal `/clear`, and a short pickup prompt pointing at the handoff file.
4. **Helper: resolve the seat's pane via the iTerm2 Python API** — enumerate sessions and match the `user.role` session variable (set by the launcher's OSC SetUserVar) under the 0/1/N gate: exactly one match, or refuse loud. Not AppleScript: `osascript` addressing a session by id for `write text` fails with error -1728 even when the same id came from AppleScript's own enumeration loop.
5. **Inject `/clear`, then the carriage return as a separate send.** A `\n` inside the text never submits — the composer needs a distinct `\r` (Enter) send.
6. **Verify the clear took** by reading the screen back, and expect the mid-turn race: if the seat was mid-turn when the injection landed, both injected texts sit in its pending-input queue until the turn ends — poll until the queue drains and the fresh splash appears.
7. **Inject the pickup prompt + `\r`, then verify the fresh session is PROCESSING it** — actively working, not merely idle. `/clear`, executing from the queue, can consume a queued pickup prompt along with itself, leaving the fresh session deaf on an empty prompt line; if deaf, re-inject the pickup + `\r` once and re-verify.
8. **Report per-step evidence.** The fresh seat captures the helper's transcript as the rotation record, then terminates the helper.

**Traps:**
- *AppleScript is the wrong API* (step 4) — the -1728 direct-addressing failure reproduces even after the id resolves cleanly by enumeration; do not iterate osascript syntax, switch to the Python API.
- *Mid-turn injection queues; `/clear` eats the queued follow-up* (steps 6–7) — the pair of races observed in the live proof: injected text does not execute during the target's turn, and the clear can silently consume the queued resume prompt. Verify PROCESSING, not just cleared; one re-inject is the designed recovery.
- *Deaf-until-driven* (step 7) — a cleared interactive session sits idle until someone types a turn; the pickup injection IS that turn. Without it the rotation stalls indefinitely with no error anywhere.
- *Screen reads render spacing as NUL bytes* — normalize before matching text in a verify step.
- *Long injections collapse to paste chips* (step 3) — keep the pickup prompt short and point at the handoff file rather than carrying content inline.

## Card: Worker restart

Kills a dead or hung worker process and spawns a fresh one in its place — distinct from rotation (which reuses the live process).

1. `plugin::agent_messaging_plugin::session_status` on the dying worker — capture `lane_id`, `brief_ref`, `work_class`, `budget_line`, `role_class`, `host`, `model`, `effort` before the row goes terminal.
2. `plugin::agent_messaging_plugin::terminate_session` with `agent_instance_id`, `directed_by`, `grace_seconds=30`.
3. `plugin::agent_messaging_plugin::spawn_session` with the captured fields, including `role_name=<the role this worker should hold>`. This call ALREADY drives one automatic first turn immediately after a successful host dispatch (the lane's captured charter if one is on file, otherwise a fixed fallback bootstrap turn) — this is not optional and nothing further needs to trigger it.
4. Check whether a lane charter is on file for `lane_id` (only relevant if you need to know whether the automatic first turn from step 3 already instructs the fresh session to claim `role_name`). If no charter is on file: `plugin::agent_messaging_plugin::drive_session` on the new `agent_instance_id` with `text` explicitly instructing it to claim the target role ("claim role '<role_name>' via the rename skill / arm a watch process for it — this is a restart continuing lane '<lane_id>', not a fresh unbriefed spawn").
5. **Verify — do not skip.** `plugin::agent_messaging_plugin::peer_holds_role` with `name=role_name`, `agent_instance_id=<new id>` must return `holds: true` before the restart is reported complete.

**Traps:**
- *The claim circle* — a relaunched process can come up with the right transport but still resolve `peer_send_by_name` to the DEAD prior instance (`delivery: queued_for_replay`, empty `delivered_to_bridge_id`); on watch transport there is no bridge-connect auto-registration, so an idle fresh session never turns without a wake, which needs the binding. `spawn_session`'s own automatic first-turn dispatch (step 3) is what breaks this circle for a spawn-path relaunch — a hand relaunch that bypasses `spawn_session` entirely (e.g. raw pane injection) does not get this for free and needs an explicitly driven first turn.
- *No role-name placeholder in the authority template* — the `--append-system-prompt` delegation contract a fresh spawn receives does not itself name the target role; that instruction has to come from a captured lane charter's prose, or (default, when no charter exists) an explicit step-4 drive. Do not assume the automatic first turn alone re-claims the role.

## Card: Memory-passthrough sync (hydrate or drain)

Syncs the local `.md`-per-fact memory projection with the canonical `memory_service` store. As of the maintenance-verbs M1 slice, `.claude/hooks/memory_passthrough/sync.py {hydrate|drain}` composes the steps below into one Bash invocation each — prefer it over the manual sequence unless debugging a step in isolation.

**Hydrate** (regenerate the local projection from canonical truth):
1. `service_interface::memory_service::export_memories` with `tags=["agent_memory", "agent_memory:origin:<this checkout's origin tag>"]` and `file_path` inside the operator-configured `export_allowed_roots` (an out-of-root path is refused loud, not silently redirected).
2. `python3 .claude/hooks/memory_passthrough/hydrate_render.py <that file_path>`.
3. On solet-down (the export call fails): **stop, do nothing else.** The last projection stays untouched — never a partial write.

**Drain** (flush local edits back to canonical):
1. `python3 .claude/hooks/memory_passthrough/drain.py` — prints `{pending, upserts, skipped_deleted}`.
2. For each entry in `upserts`: `service_interface::memory_service::upsert_memory_by_tag` with that entry's `arguments` verbatim.
3. Only after every upsert in step 2 succeeds: `python3 .claude/hooks/memory_passthrough/drain.py --advance`.

**Traps:**
- *Deleting a local memory file is a cache clear, not a forget.* A deleted file is silently skipped at drain (never upserted empty) and hydrate will simply regenerate it next time. To forget a fact canonically, call `service_interface::memory_service::delete_memories_by_tag` on the fact's own slot tag directly.
- *A partial drain must not advance the watermark.* If any upsert in the loop fails, do not call `drain.py --advance` — every pending entry (including ones that already succeeded this pass) is retried next run, since the upsert is idempotent on the slot tag.

## Card: Memory head curation

Curates the ambient index — `MEMORY.md`'s curated head, injected once per
session context and carried on every subsequent stateless request — so it
stays inside `index_render.py`'s `DEFAULT_BYTE_BUDGET`/`DEFAULT_LINE_BUDGET`
with headroom, instead of accumulating hooks forever until a hydrate hard-
fails. **Decay here governs PLACEMENT (ambient → retrievable demotion),
never EXISTENCE** — the `agent_memory` store-level ACT-R consolidation
exemption (design v2 §4.2: similarity recall never reinforces, so store-
level decay would archive weekly-used facts wrongly) is untouched by this
card; a demoted hook's fact file and canonical record remain, and Step Zero
retrieval covers them from then on. Run this card at every seat-rotation
boundary, and immediately on any hydrate budget failure (do not wait for
the next scheduled boundary once the ceiling has already been hit once).

1. Measure the current head: `index_render.split_head(existing)` on the
   live `MEMORY.md`, then byte- and line-count it. The budgets themselves
   (`DEFAULT_BYTE_BUDGET`/`DEFAULT_LINE_BUDGET` in `index_render.py`) are
   the fence — read them from the module, never re-type them, and never
   raise them to make a curation pass unnecessary (the budget is the
   point: an always-present, ever-growing index competes for model
   attention on every decision, regardless of whether it technically
   fits).
2. Apply the demotion rules below to head lines whose backing fact(s) are
   candidates — **seat-ratified, never automatic.** A demotion is a
   judgment call, not a script's decision:
   - a trap superseded by a LANDED structural fix (the bug class the hook
     warns about can no longer occur in code) → retire the hook;
   - a lesson fully absorbed into a skill, gate, joseki card, or KB
     article (this card's own existence is an example of the shape) →
     move the content to the KB via `author-kb-content`, then retire the
     hook — the lesson survives, just not as an ambient line;
   - an Active-Work line for a lane that is closed or parked → collapse
     it into the standing backlog-pointer line rather than carrying its
     own slot indefinitely.
   A fact tagged `agent_memory:pinned` is EXCLUDED from candidacy
   entirely, regardless of how low its measured activation reads (see the
   Traps below) — never demote a pinned fact even if it would otherwise
   qualify.
3. Target head ≤ ~15,000 bytes (roughly one seat-session's worth of
   headroom below the hard budget, so hydrate does not hard-fail again
   before the next scheduled curation).
4. Log every retired hook in the curation note: which fact, why it
   qualified, and where the lesson now lives (KB article path, superseding
   commit/joseki, or the backlog-pointer line it collapsed into). A
   demotion is an edit with provenance, not a deletion — the note is what
   lets a later reader (or the fact's own future self) reconstruct why an
   ambient line disappeared.
5. Re-render (`hydrate_render.py`) and re-measure. If still over budget
   after step 3's target, repeat step 2 rather than raising the budget.

**Traps:**
- *No auto-trim, ever.* Every demotion in step 2 is a seat decision, not a
  mechanism's. A quiet process that deletes judgment — picking demotion
  candidates by a formula alone, with no human ratification, and calling
  the result a control — is exactly the failure mode this card exists to
  avoid.
- *Placement, not existence.* Retiring a hook from the ambient head is
  never a `delete_memories_by_tag` call — the canonical record and its
  local `.md` fact file are untouched. Confusing "no longer in the
  every-turn index" with "forgotten" will make someone re-derive a lesson
  the platform never actually lost.
- *`agent_memory:pinned` is orthogonal to activation, by design.* A pinned
  fact's measured ACT-R strength may be genuinely low (rare-but-
  catastrophic traps are rare precisely because the failure they guard
  against is rare) — that is expected and must never be "corrected" by
  reinforcing the fact or routing it through the `memorize`/spaced-
  repetition queue. Doing so would inflate exactly the signal any
  activation-ranked curation tooling depends on to find genuine demotion
  candidates among the OTHER, unpinned facts.

## Card: KB / process refresh

Refreshing a plugin's KB process-definition JSON or config after editing it in the checkout.

1. `service_interface::knowledge_service::refresh_plugin_processes` with `plugin_name=<plugin>` — **plural, never singular.** The singular `refresh_plugin_process` trips a result-contract violation for every process except itself while still reporting synchronous success; the caller never learns the result was rejected.
2. Check the response's `updated_count`. A `0`-update success means every key already matched (or the process key was not found) — it re-embeds nothing.
3. To verify an edit actually went live: put a distinctive phrase in `description` (not only `embedding_description`) and probe via `process_search`. Some fields (e.g. `error_processor_customizations`) are structurally invisible to every available retrieval probe — a negative there is UNDETERMINED, not ABSENT.
4. Refreshing N plugins in one window costs N full index rebuilds (`refresh_plugin_processes` clears and rebuilds the ENTIRE vector index every call, even though the verb takes one `plugin_name`). Run a multi-plugin refresh back-to-back and take no retrieval measurement mid-sequence.
5. For a config VALUE change (not a process JSON edit): use `service_interface::lifecycle_management_service::reload_plugin_config`, then **re-run the call that was originally failing as the verdict** — never trust the response's `dirty_keys` field. The verb updates the config store; a plugin instance that cached its config at construction does not automatically re-read it. A restart is not a guaranteed fix either — check the plugin's config-binding order against the platform's `STARTUP_SEQUENCE` before assuming a restart will help.

**Traps:**
- *Singular refresh lies.* `refresh_plugin_process` (singular) returns `{"success": true}` synchronously while its result is rejected in the async result-processing path — the caller gets an unambiguous success and never learns otherwise.
- *`0` updates ≠ nothing to do; it can also mean nothing changed.* Assert `updated_count > 0` with empty `errors`, not just a bare success flag.
- *Store-updated ≠ behavior-changed.* `reload_plugin_config`'s green means the config row was written, not that any running instance is reading it differently.

## Card: Context gauge check

Checks a session's current context-window occupancy against its model's ceiling. Depends on `session_context_status` (maintenance-verbs M1) — a cached-read verb fed by an extension to the existing `rotation_due_watch.py` PostToolUse hook, landed alongside this card. Coverage today: any worker spawned via `spawn_session` (the hook rides its spawn-time settings). The operator-hosted seat is a known, disclosed gap pending a separate seat-wiring decision (see `workbench/2026-08-09_context_gauge_seat_wiring_design_note_mverbs-impl.md`) — a `resolved: false` result for a `host=operator` session is the expected shape, not a bug to route around.

1. `plugin::agent_messaging_plugin::session_context_status` with `agent_instance_id`.
2. If `resolved: false`: **stop, report the `resolution_error` verbatim.** Do not estimate a number in its place — this is the standing rule against silently promoting an unknown into a fact, applied to context measurement specifically.
3. If `fraction >= 0.6` (the operator's checkpoint-and-stop tripwire — stricter than this verb's own `rotation_due` flag, which fires at 0.5): finish the current bounded unit of work, checkpoint continuously, and rotate at the nearest coherent boundary. Do not ride to the ceiling, and do not ride to 80% "because it's cached" — a cached-context read still bills roughly 10% of its size as input tokens on EVERY subsequent turn (`per_prompt_carriage_estimate_tokens` in the result), so an 800k-token context costs roughly 80k token-equivalents per prompt before any new work happens, and a quiet gap past the prompt-cache TTL makes that worse, not better.

**Traps:**
- *`budget_report` is the wrong instrument for this.* It structurally excludes `host=operator` rows and lifetime-sums a worker's usage per `budget_line` — neither "current" nor seat-inclusive. Do not adapt it for a live context reading.
- *`rotation_due=true` and the operator's own 0.6 checkpoint tripwire are two different thresholds.* Compare `fraction` directly against your own policy rather than treating this verb's `rotation_due` flag as the last word.
