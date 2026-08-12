# Deployment Report Card — Measuring a Homunculus Against the Fully-Deployed State

Tags: knowledge:tag:homunculus_lifecycle, knowledge:tag:deployment_report_card

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: The canonical definition of a fully-deployed homunculus and the report card a driving agent produces against it for the operator — the tiered component roster (core capabilities including session-ledger ingestion, the coordination watcher and wake path, and the export root; fleet components including tmux worker hosting and the git-controller gate; per-connector first-use configuration; and explicit-opt-in extras), the evidence command that proves each component configured, the scoring and presentation rules for telling an operator how much of their deployment is set up and what remains, and the rule that consent-gated core capabilities are measured and re-offered rather than silently treated as optional.

**When you need this**: hydration just finished and you are closing out with the operator; the operator asks "is everything set up?" or "what am I missing?"; a seed update landed and you want to confirm nothing regressed; you are about to tell an operator their deployment is "done" and need the checkable definition of done; deciding whether a declined capability should be re-offered.

---

## Why a report card

A homunculus degrades gracefully — almost everything works partially. The launch works without the launcher, sessions run without the ledger, one session runs without the fleet hooks. That grace is a trap: a deployment can sit for weeks at half its value with nothing visibly broken, and the operator never learns what they are missing. Field experience with real deployments shows exactly this failure: **newborn deployments treating core capabilities — session-ledger ingestion, tmux worker hosting — as optional extras**, because nothing ever told them those were load-bearing.

The report card is the correction. It defines the **fully-deployed state** as a checkable roster, measures the live deployment against it with real commands, and gives the operator a plain-words score: what is configured, what remains, and what each remaining item would give them. The goal is a deployment that reaches 100% because the operator can always see the distance to it — not because anything was configured behind their back.

Two rules keep it honest:

- **Measure everything; configure nothing unasked.** The card is a measurement, and producing it is always in scope. Acting on a gap follows the normal offer-and-explicit-yes ladder from the hydration runbook.
- **Consent-gated is not optional.** Several core capabilities read the operator's own files and rightly require an explicit yes. A "no" is respected — no partial wiring — but the row stays visibly unconfigured on every future card, with what it costs stated plainly. It never migrates to "not applicable", and the card never presents a core row as a take-it-or-leave-it extra.

## The fully-deployed state — the canonical roster

Four tiers. **Core** applies to every deployment. **Fleet** applies as soon as more than one session exists or any worker is spawned programmatically — and it appears on every card either way, so a solo operator knows the tier exists before they need it. **Connectors** is per-operator. **Opt-in extras** are the only rows that are genuinely elective, and they never count against the score.

Every row names its evidence — the command whose output proves the state. "I set it up earlier" is not evidence; run the command. The hydration runbook's Step 6 carries the full form of most probes; this table is the roster, not a replacement for those procedures.

### Tier 1 — Core (every deployment; target: 100%)

| # | Component | Evidence that it is configured |
|---|---|---|
| 1 | Homunculus alive and stable | `launchctl list` shows the same pid across two checks a minute apart; newest `profile/data/logs/` file quiescent |
| 2 | Bridge + knowledge search answering | `<name> health`; a `knowledge_service::search` call returns results |
| 3 | Named-session launcher | `<clone>/client/bin/claude-<name>` (and `codex-<name>` where Codex is used) exists and starts a labeled session |
| 4 | `coordination-hooks` plugin installed | `~/.claude/plugins/installed_plugins.json` names it AND its `installPath` exists on disk (`ls` it) |
| 5 | Project instruction files carry the managed block | `BEGIN HOMUNCULUS HYDRATION` markers present in `<clone>/CLAUDE.md` and `<clone>/AGENTS.md` |
| 6 | User-scope instruction surface + rename skill | this deployment's marker section in `~/.claude/CLAUDE.md`; `~/.claude/skills/rename/SKILL.md` present |
| 7 | Role binding held, idle wake working | `peer_holds_role` resolves the session's role claim; a message to the idle session wakes it (the `<name> wake` Stop-hook path) |
| 8 | **Session-ledger ingestion — every coding agent the operator uses** | `ledger_allowed_roots` configured; `session_ledger_service::list_sources` shows registered rows for each agent (Claude Code and/or Codex); a `search_event_content` query returns content from a real prior session |
| 9 | Ledger embedding drain functional | event content lands in search results without a manual drain — after a session, new content is findable |
| 10 | Export/workspace root configured | the validated root persisted via `configure_export_root` (hydration Step 4b) — business-connector verbs do not refuse on a missing root |
| 11 | Git worktree + `.gitignore` | `git rev-parse --is-inside-work-tree` succeeds in the clone; `.gitignore` present |
| 12 | Blue-green router (capable profiles only) | `launchctl list` shows `local.homunculus.<name>.router`; free-tier profiles mark this row ➖ not-applicable |

Row 8 deserves its own sentence, because it is the row most often skipped: **a homunculus without its session ledger cannot remember anything across sessions** — no "what did we decide last week", no prior-session search, no history-grounded answers. The platform's own operating stance is that ingestion completeness and ledger functionality are core correctness, not a privacy knob (operator ruling, 2026-08-02, quoted in the hydration runbook's ingestion disclosure). The consent conversation is real and required — it reads the operator's own transcript files — but the outcome on a "no" is a deployment visibly missing a core capability, and the card says so.

### Tier 2 — Fleet (applicable from the second session or first spawned worker; target: 100% of applicable)

| # | Component | Evidence that it is configured |
|---|---|---|
| 13 | Fleet launcher functions | `<clone>/client/<name>-fleet.zsh` rendered and sourced; role launchers work |
| 14 | Git-controller designated and gate proven | `GIT_CONTROLLER_NAME` set in the launchers; ONE real blocked mutation demonstrated from a non-controller session (a throwaway repo probe — "no error appeared" proves nothing) |
| 15 | **tmux worker hosting** | `tmux -V` reports ≥ 3.3; programmatically spawned workers use the tmux host driver |
| 16 | Work repos carry the managed block | each repo the fleet works in has the hydration block in its `CLAUDE.md`/`AGENTS.md` |

Row 15 is the other habitually-skipped row. A tmux-hosted worker survives a blue-green deploy; a headless worker is killed mid-swap by construction (measured, 2026-08-04). A fleet whose workers are headless loses its workers on every platform update. tmux is not a cosmetic preference — for any deployment that spawns workers, it is the difference between workers that survive updates and workers that silently die during them.

### Tier 3 — Connectors (per-operator; state, not score)

Discover the roster by globbing `<clone>/plugins/*/knowledge_base/hydration_guidance.md` on the filesystem (not KB search — a credential-less plugin may not be ingested yet). Each found plugin gets a row with one of three states: **configured** (first-use setup done, verified by that guidance file's own checks), **awaiting first use** (installed, pitched or not yet needed — the normal state under install-all-now/configure-on-first-use), or **declined**. Connectors are scored as "n configured of m installed", shown separately — an operator who never uses Jira is not 8% undeployed.

### Tier 4 — Opt-in extras (never scored)

Shell integration (the managed `~/.zshrc` block), the MCP bridge, and development channels. Listed so the operator knows they exist; configured only on the explicit-request paths their own runbook steps define; absence is never a finding.

## Producing and presenting the card

Run the evidence commands, then present — in the operator's language, not the platform's:

1. **Headline**: "**X of Y core components are configured (Z%)**", plus the fleet line when applicable.
2. **The table**, compact: component in plain words, ✅ / ⬜ / ➖, one-line value statement for each ⬜ — what the operator gets when it is on ("session memory: I'll be able to answer 'what did we decide' questions"), never a scold about what they did wrong.
3. **One recommended next step** — the single highest-value ⬜ row, with the offer to do it now. One, not a list of chores.
4. On a card with no gaps: say so in one line and stop. A 100% card is a sentence, not a ceremony.

Plain-words discipline from the first-days runbook applies: "session memory", not "ledger ingestion"; "update-proof worker sessions", not "tmux host driver swap-durability". Surface internal names only for operators who want them.

## When to run it

- **At hydration close-out — mandatory.** The card is the last act of the hydration runbook: it converts Step 6's verification into something the operator sees and keeps. A hydration that ends without delivering a card has not finished.
- **After every seed update**, to catch regressions and surface newly-shipped components the operator does not have yet.
- **Whenever the operator asks** any variant of "is everything set up?", "what am I missing?", or a general status question.
- **Periodically while gaps remain** — at a natural pause roughly weekly during the first month, then at natural moments (an update, a relevant work item). Re-offer, never nag: a declined row is re-raised when something changes (a new reason, a new release, an operator question), not on a timer in the middle of unrelated work.

## Recording the result

Keep the produced card as a dated memory or workbench note in the deployment, so the next session knows the last measured state and does not re-run every probe to answer a status question — and so "it regressed" is detectable, not just "it is off". The card's date matters: a card older than the last seed update is stale, and a status answer from a stale card says so.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` — Step 6's verification procedures, which this card's core rows compress into a deliverable; the card is that runbook's close-out.
- `plugins/github_midwife_plugin/knowledge_base/04_first_days_runbook.md` — the plain-words presentation discipline and the offer-don't-push relationship rules the card's delivery follows.
- `plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md` — the update flow after which the card is re-run.
- `plugins/github_midwife_plugin/knowledge_base/07_upstream_feedback_runbook.md` — where a gap the deployment cannot close locally (a defect, a missing capability) gets reported.
- `plugins/claude_code_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` and `plugins/codex_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md` — row 8's setup procedures.
