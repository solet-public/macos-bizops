# Deployment Report Card — Measuring a Homunculus's Functionality Against Its Own Active Roster

Tags: knowledge:tag:homunculus_lifecycle, knowledge:tag:deployment_report_card

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: The canonical definition of a fully-functioning homunculus and the report card a driving agent produces against it for the operator — eight functional sections (exception-based setup, launch environment, memory, session ledger, peer communications, knowledge base, session management, and one row per active plugin), each derived from the deployment's OWN active plugin roster rather than a fixed tier list, the evidence command that proves each item actually works, the scoring and presentation rules for telling an operator how much of their deployment is functioning and what remains, and the rule that consent-gated core capabilities are measured and re-offered rather than silently treated as optional.

**When you need this**: hydration just finished and you are closing out with the operator; the operator asks "is everything set up?", "what am I missing?", or "are all the cool features actually working?"; a seed update landed and you want to confirm nothing regressed; you are about to tell an operator their deployment is "done" and need the checkable definition of done; deciding whether a declined capability should be re-offered.

---

## Why a report card

A homunculus degrades gracefully — almost everything works partially. The launch works without the launcher, sessions run without the ledger, one session runs without the fleet hooks. That grace is a trap: a deployment can sit for weeks at half its value with nothing visibly broken, and the operator never learns what they are missing. Field experience with real deployments shows exactly this failure: **newborn deployments treating core capabilities — session-ledger ingestion, tmux worker hosting — as optional extras**, because nothing ever told them those were load-bearing.

The report card is the correction. It defines **fully functioning** as a checkable set of capabilities grounded in what this specific deployment actually runs, measures the live deployment against it with real commands, and gives the operator a plain-words score: what works, what remains, and what each remaining item would give them. The goal is a deployment that reaches 100% because the operator can always see the distance to it — not because anything was configured behind their back.

Two rules keep it honest:

- **Measure everything; configure nothing unasked.** The card is a measurement, and producing it is always in scope. Acting on a gap follows the normal offer-and-explicit-yes ladder from the hydration runbook.
- **Consent-gated is not optional.** Several core capabilities read the operator's own files and rightly require an explicit yes. A "no" is respected — no partial wiring — but the row stays visibly unconfigured on every future card, with what it costs stated plainly. Track a decline explicitly (a dated note of what was asked and the "no" given), not by inference from an empty config: an explicit decline and "never asked" are different states, and only a tracked decline is exempt from being re-pitched on every single card. It never migrates to "not applicable", and the card never presents a core row as a take-it-or-leave-it extra.

## The card is roster-derived, not a fixed checklist

This article ships identically to every deployment, but no two deployments run the same plugin set — one runs local Postgres and LM Studio, another runs RDS and a cloud embedding endpoint; one spawns fleet workers under tmux, another runs solo. A card built from a hardcoded component list either nags an operator about something they were never going to run, or misses something their own roster actually requires. **Every section below is a derivation method, not a fixed roster** — read this deployment's own active plugin list, then apply the method to find out what this deployment specifically needs to check.

The concrete source for "what is active": the deployment's live plugin manifest (the `apply_manifest`-maintained list a running homunculus was actually started with). Cross-reference each entry against the platform's own plugin-purpose reference to learn what external dependency, if any, that plugin implies. A plugin not in the active manifest contributes no row to the card, even if the underlying technology (an app, a service) happens to be in everyday human use on the machine for unrelated reasons — the plugin's load state is the derivation key, not habit or memory.

The operator's own framing for why this matters: *"we want to make sure that all the cool features are accounted for"* — a roster-derived card is how a growing feature set stays visible instead of some of it quietly falling off the operator's radar as new plugins ship.

## 1. Setup — exception-based

List **only** components required by the active roster that are **not** configured correctly. A dependency that checks out cleanly does not appear as a row at all — this section is a list of problems, not a list of prerequisites. If nothing is broken, the section is empty and says so in one line.

Derivation method: for each actively-loaded plugin, determine whether it implies an external dependency the platform cannot substitute for, then run that dependency's own liveness check.

| Active plugin (example) | Implied dependency | Evidence if it needs a row |
|---|---|---|
| The state-management plugin backing the platform's state interface (a local-Postgres plugin on a laptop deployment, an RDS-backed plugin on a cloud deployment) | A reachable database matching that plugin's target — local Postgres OR the RDS endpoint, never assume which without checking which plugin is actually loaded | `pg_isready` / an equivalent reachability probe against the configured target; a failing probe is the row, a passing one is not |
| The embeddings plugin (a local OpenAI-shape endpoint plugin, e.g. LM Studio-backed, on a laptop; a cloud embeddings plugin elsewhere) | The endpoint the plugin is configured to call actually serving the configured model | Hit the configured endpoint's own model-list/health surface; confirm the specific model name the plugin expects is present, not just that something answers |
| The messaging plugin's tmux worker-host adapter, when fleet workers are spawned programmatically | `tmux` present at a version the host driver requires | `tmux -V` |
| A plugin that drives a specific terminal application to manage coding-agent sessions, when that plugin is actually in the active manifest (not merely installed on disk) | The driven application present and scriptable | The plugin's own liveness probe for that application; if the plugin itself is not in the active manifest, this row is skipped entirely regardless of what terminal the operator happens to use day to day |

This table is illustrative of the method, not an exhaustive or permanent list — a newly shipped plugin with its own external dependency gets its own row the same way, derived at report-card time, not by editing this article.

**Plus, always:** is the platform up to date with the seed it was born from? For a downstream deployment, diff the deployment's own tree against the seed commit/tag it was minted from and report drift. For the origin deployment itself — the one the seed is published from — there is no upstream seed to compare against, so the check inverts: is the published seed current with the origin's own `master`? Evidence is a commit/tag comparison in whichever direction applies; flag drift either way rather than silently skipping the check because the usual direction doesn't apply here.

## 2. Launch environment

| Component | Evidence |
|---|---|
| Operator session-launching function configured **and actually in use** | The named launcher script exists on disk AND a real recent session was started through it — presence alone is not use |
| Platform running now | The startup-manager entry (e.g. `launchctl list` on macOS) shows a stable process id across two checks a minute apart |
| Configured to run at startup | The startup-manager unit is registered to run at load/boot, not merely running because someone started it by hand this session |
| Required hooks available **and proven to actually work** | Each hook the launch environment depends on fires and produces its documented effect on a live trigger — not just "the file exists and parses" |

The operator's own language named a specific hook-wiring file ("remote_settings.json") that does not exist anywhere in this tree under that name. Read that as pointing at the hook-wiring surfaces in general — the session settings file, the coordination-hooks plugin manifest, and any generated hook-registration blob those produce — rather than guessing a literal filename. **Flag the naming mismatch back to the operator in the report** instead of silently substituting a different file and presenting it as what they meant.

## 3. Memory

Derive the roster from the platform's own memory-passthrough design (the unified-memory-passthrough architecture doc): local per-agent memory is a disposable projection of one canonical store, kept live by a hydrate/drain loop, not an independent source of truth.

| Component | Evidence |
|---|---|
| Canonical memory store reachable | A memory-service status/stats call answers |
| Hydrate (canonical → local) round-trip works | Export the canonical store's tagged records to the platform-owned spool, run the local renderer, and confirm the regenerated per-fact files and index are byte-identical to what the canonical content specifies |
| Drain (local → canonical) round-trip works | A pending local edit reaches the canonical store via the tag-preserving upsert path, and the local pending-count returns to zero after |
| Capture hook actually fires | A local memory-file write triggers the capture journal to grow — checked live, not inferred from the hook's presence in settings |
| Echo-break holds | A hydrate immediately followed by its own capture does not re-queue the same content as a new pending write |
| Decay/consolidation exemption in force | The protective tag that shields passthrough records from the platform's own memory-decay/consolidation sweep is present on live records, not just documented as a policy |
| Local index within its stated size budget | The rendered index file's head stays under the ceiling its own renderer targets |

## 4. Session ledger

Derive the roster from the platform's own session-ledger architecture docs (the `summary_text`-origin and ingest-path articles): what counts as "the ledger is working" is ingestion, summarization, and searchability all actually happening, not just a source being registered.

| Component | Evidence |
|---|---|
| Past sessions ingested — every coding agent the operator uses | The ledger's source-listing call shows a registered row for each agent in use; a content-search query about a real prior session returns that content |
| Summarization is actually landing | Sessions are acquiring their summary text through one of the ledger's own legitimate origin paths (an operator-set title lifted at import, an extracted end-of-session recap, an inference-generated fallback, or the explicit trivial-session marker) — check the summary-origin discriminator on real rows, don't assume a path fired just because a summary field is non-empty |
| Whether a **local** model performs the summarization, established rather than assumed | A deployment can serve embeddings from a local endpoint while text summarization still routes through a separate, non-local inference binding — these are two different steps in the pipeline. Check each plugin's own live configuration (the embeddings plugin's target, and whatever plugin backs the inference service the ledger's summarization fallback calls) rather than inferring "summarization is local" from "a local model server happens to be running" |
| Embedding drain functional | After a real session, its content becomes findable via search without a manual drain trigger |

## 5. Peer communications

| Component | Evidence |
|---|---|
| Role bindings resolve to a live claim | A direct ownership check for the role (not just presence in a peer listing) confirms the current session actually holds it — a listing shows presence, never proof of a held claim |
| Send → wake round trip works | A message sent to a live role or instance produces an actual wake, not merely a durable/queued outcome that nothing is currently draining |
| Durable inbox is readable after an outage | A known session's inbox returns queued mail that arrived while it was unreachable, not just messages sent while it was live |
| Stale-binding detection catches a binding that predates a reconnect | A role claim made before a restart, `/clear`, or transport reconnect is either confirmed still live or flagged as possibly stale — never trusted purely because it still appears in a listing. (A real incident of exactly this failure mode is the motivating case for this row: a binding outliving the session it pointed at, discovered only because something checked.) |

## 6. Knowledge base

| Component | Evidence |
|---|---|
| Available and searchable | A knowledge-search call answers with results for a real query |
| Up to date after content changes | The KB's content-hash-based freshness check shows no stale entries for recently edited articles |
| Embeddings complete, no plugin left vacant | Every shipped plugin's KB has a real embedding binding — a plugin's knowledge surface booting vacant is a finding, not a quiet default |
| Retrieval self-tests green | Each changed article's own retrieval-test companion passes: its target queries rank within the stated bound and its forbidden queries (owned by sibling articles) stay outside it — this is the proof shape, not an eyeball read of the prose |
| Reindex freshness after content changes | A just-edited article is retrievable through the live search path without requiring a full restart, or the card states plainly that a restart is still required |

## 7. Session management

| Component | Evidence |
|---|---|
| Spawn a session per model tier actually used | For each model tier the deployment uses, at least one real spawn is evidenced — not merely asserted as supported |
| Spawn a session per effort level actually used | Same, per effort tier |
| Drive a spawned session | A dispatched brief is actually executed by the spawned session, not just launched and left idle |
| Clean terminate | A spawned session is torn down cleanly — process gone, any claimed role released — and this is evidenced separately from a successful spawn; a spawn demonstration is not a terminate demonstration |

Report this section as a **demonstrated matrix**, not a capability claim: list only the (model, effort, lifecycle-stage) combinations that were actually live-exercised with evidence, and say plainly which cells are unproven. "The platform supports N models and M effort levels" is a design claim; "spawn+drive was demonstrated on model X at effort Y tonight; a clean terminate and a second model/effort tier are not yet demonstrated" is the honest card.

## 8. Plugins — one row per active plugin

Enumerate the deployment's own active plugin manifest and give every plugin in it one row, not just business connectors.

| Column | What it measures |
|---|---|
| Setup completeness | Credentials the plugin needs are populated where it expects them (address book / vault entries); any required URLs or endpoint config are present |
| Validated usage | The plugin has actually been successfully exercised through a real call — configured-but-never-called is a distinct, weaker state than configured-and-proven, and the card says which one applies |
| Access state | Whether this operator has (or needs) access to what the plugin connects to |

Present access state as a **state, never a deficiency**: *"not all users will use all plugins... not all users will have [some connector's] access"* — a connector the operator has no account for is not part of the denominator any more than an opt-in extra is. Score only the plugins this operator is actually meant to use; list the rest with their access state shown plainly and no score attached. Where a plugin needs the same explicit-yes consent as a core capability (row 8's session-ledger consent is the canonical example), a tracked decline shows here too — how declines are tracked and re-offered is the same rule as the honesty section above, applied per plugin instead of per core row.

## Producing and presenting the card

Run the evidence commands, then present — in the operator's language, not the platform's. **The delivered card is a score card of tables, not prose with checkmarks stitched in.** This is a hard format rule, not a style preference:

1. **Headline**: "**X of Y functioning (Z%)**" per scored section, plus a one-line summary across sections. Prose, one or two sentences — the only place a summary sentence belongs.
2. **One compact table per functional section** (Setup, Launch environment, Memory, Session ledger, Peer communications, Knowledge base, Session management, Plugins — whichever have rows this run) — three columns: component in plain words, ✅ / ⬜ / ➖, and a one-line evidence-or-value column (what was checked for a ✅/⬜, or the plain-words payoff for a ⬜ — "session memory: I'll be able to answer 'what did we decide' questions" — never a scold about what they did wrong). A section lead-in, if one is needed, is at most one sentence directly above its table — never a paragraph, and never a row's own status folded into running prose instead of its table cell.
3. **One recommended next step** — the single highest-value ⬜ row, with the offer to do it now. One, not a list of chores. Prose, not a table.
4. On a card with no gaps: say so in one line and stop. A 100% card is a sentence, not a ceremony.

**What this rules out, explicitly:** a narrative walkthrough that mentions each component in a sentence with a ✅ or ⬜ dropped in mid-paragraph. That shape reads as a report, not a score card, and it is exactly what this presentation rule replaces — every component's state lives in a table cell, full stop. Prose is reserved for the headline, section lead-ins of at most a sentence, and the one recommended next step; nowhere else in the delivered card does a checkmark appear outside a table.

Plain-words discipline from the first-days runbook applies: "session memory", not "ledger ingestion"; "update-proof worker sessions", not "tmux host driver swap-durability". Surface internal names only for operators who want them.

## When to run it

- **At hydration close-out — mandatory.** The card is the last act of the hydration runbook: it converts Step 6's verification into something the operator sees and keeps. A hydration that ends without delivering a card has not finished.
- **After every seed update**, to catch regressions and surface newly-shipped components the operator does not have yet.
- **Whenever the operator asks** any variant of "is everything set up?", "what am I missing?", or a general status question.
- **Periodically while gaps remain** — at a natural pause roughly weekly during the first month, then at natural moments (an update, a relevant work item). Re-offer, never nag: a declined row is re-raised when something changes (a new reason, a new release, an operator question), not on a timer in the middle of unrelated work.

## Recording the result

Keep the produced card as a dated memory or workbench note in the deployment, so the next session knows the last measured state and does not re-run every probe to answer a status question — and so "it regressed" is detectable, not just "it is off". The card's date matters: a card older than the last seed update is stale, and a status answer from a stale card says so.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` — Step 6's verification procedures, which this card's checks compress into a deliverable; the card is that runbook's close-out.
- `plugins/github_midwife_plugin/knowledge_base/04_first_days_runbook.md` — the plain-words presentation discipline and the offer-don't-push relationship rules the card's delivery follows.
- `plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md` — the update flow after which the card is re-run.
- `plugins/github_midwife_plugin/knowledge_base/07_upstream_feedback_runbook.md` — where a gap the deployment cannot close locally (a defect, a missing capability) gets reported.
- `plugins/claude_code_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` and `plugins/codex_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` — the session-ledger section's per-agent setup procedures.
- `ananta/knowledge_bases/ananta_platform/23_plugin_inventory/01_full_plugin_roster.md` — the platform-wide plugin-purpose reference the Setup and Plugins sections' derivation method reads against.
- `workbench/2026-07-16_unified_memory_passthrough_design_v2.md` — the Memory section's canonical/local architecture.
- `ananta/knowledge_bases/ananta_platform/19_session_ledger/03_summary_text_origins.md` — the Session ledger section's summary-origin discriminators.
