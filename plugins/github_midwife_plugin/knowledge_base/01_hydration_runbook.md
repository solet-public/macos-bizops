# Seed Hydration Runbook — Operator Environment Setup After Genesis

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for setting up the user's own environment around a homunculus birth, covering wizard step one per-homunculus role and database provisioning before a verb-mode or same-machine birth (create the homunculus's own non-superuser role, its database, the pgvector extension, the PUBLIC-connect revoke, the one-time default-scram pg_hba block, and the negative-auth and isolation probes — no cross-homunculus credential copy, and shared-vs-separate Postgres instance as the driving agent's topology decision), then after genesis the shell launcher for a properly named Claude Code session, optional multi-role fleet setup, additive shell integration with recoverable backups, install-all-now configure-on-first-use plugin hydration guidance, generated project CLAUDE.md and settings hooks, no-MCP default command-line operation through the per-homunculus `<name>` command including the `<name> watch` registered-presence watcher that claims each session's role and receives role-addressed messages, MCP as a strictly optional add-on offered only on explicit operator request, a connectivity glossary separating the bridge, the blue-green router, MCP registration, development channels, and peer-registry presence versus the durable role binding, and the homunculus-alive verification checklist.

## When to use this runbook

Run this ladder after genesis completes and the newborn homunculus boots, when the user's own environment still lacks the launch tooling. Genesis provisions the homunculus side only, so a fresh clone gives the user no command to start a named Claude Code session, no project CLAUDE.md, and no Claude Code hooks. This runbook is written TO the driving coding agent, the same audience as the genesis section of the seed README. Everything here follows the genesis ladder shape: probe first, offer, act only under the user's normal tool-approval flow, verify, and stop and ask rather than guess.

Hydration is deliberately NOT genesis code. The files this ladder writes belong to the user's side of the genesis boundary, just like optional client registration does: the agent performs them with its own tools, in conversation, under the user's approval.

## Three fixed rules

These rules are operator requirements, not tunable defaults. Every step below is shaped by them.

1. Shell startup files may be inspected to understand structure, but never echo, transmit, summarize, or persist secret-looking values. Treat tokens, keys, passwords, tenant URLs, and private hostnames as sensitive: if they appear, say only that secret-looking or private values were present and avoided. Integrate additively when possible; whole-file replacement is the fallback, not the default.
2. Any user-owned shell change is an OFFER, but the analysis is not optional. Before asking, inspect the current startup file, the shipped `zshrc.template`, the rendered `<clone>/client/<name>.zsh`, and any existing local launcher/fleet pattern you find. Then recommend a concrete plan in plain language. Do not ask the user to choose between raw implementation details before doing that review.
3. The user always ends up with a command-line way to start Claude Code with a properly named session, whether or not they accepted shell integration.

## Connectivity stance — no-MCP primary path

Treat the operator's machine policy as authoritative. If the operator says MCP
servers are restricted, the bridge is unavailable, or Claude Code cannot use
MCP, do not inspect `.mcp.json`, run `claude mcp`, search for `mcp__<name>__*`
tools, or try to prove the operator wrong. The primary interface is the
genesis-installed `<name>` command:

```bash
<name> health
<name> call service_interface::knowledge_service::search '{"query": "hydration runbook", "top_k": 8}'
```

That command uses the homunculus's localhost bridge internally, but it is not an
MCP server and needs no client-side MCP registration. Use it for knowledge-base
search, process calls, session-history setup, and — via `<name> watch` — peer
registration, role claiming, and message receive. The no-MCP path is the
DEFAULT and the complete path on every machine, not a managed-machine fallback.
Never pitch MCP proactively during hydration: if you notice the environment
permits MCP, you may mention once that an optional MCP bridge exists, and act
on it only if the operator explicitly asks for it.

## Glossary — distinct layers that all get called "the bridge"

Sessions repeatedly get lost by conflating these. When you explain or debug
connectivity, name the layer you mean:

- **Bridge** — the homunculus's localhost HTTP process-call surface; what the
  `<name>` command talks to. Needs no MCP.
- **Blue-green router** — a separate KeepAlive LaunchAgent (blue-green-capable
  profiles only) holding the stable public port while homunculus instances swap
  behind it. Not a message path; a port-stability device.
- **MCP bridge registration** — the optional `claude mcp add` client wiring
  that exposes `mcp__<name>__*` tools where policy permits MCP.
- **Development channel / remote-control** — fleet-launcher flags: the
  development channel is optional MCP-transport idle-wake (gated OFF by
  default in the fleet template; only for operators who explicitly want MCP);
  Claude Code remote-control lets one session drive another and involves no
  MCP at all.
- **Peer registry vs. durable role binding** — the registry is ephemeral
  connection presence; the role binding is the durable state row that
  `peer_send_by_name` / `peer_holds_role` resolve against. "Am I registered?"
  is answered by `peer_holds_role` resolution (role name + agent_instance_id),
  never by an entry in raw peer-list output. Claiming a binding requires a
  live registered peer connection; the DEFAULT holder is the
  `<name> watch` registered-presence watcher (register + claim + drain missed
  messages + stream deliveries, zero MCP), which the generated rename skill
  arms per session. The one-shot `<name>` command opens and closes a
  connection per command, so it invokes capabilities and sends outbound but
  never holds a role — that is the watcher's job. The optional MCP bridge is
  merely an alternative holder where an operator chose it.

## Render contract

All artifacts render from the flat template directory shipped in this plugin's knowledge base. The directory is `plugins/github_midwife_plugin/knowledge_base/hydration_templates/`. Substitute the double-brace tokens (`{{HOMUNCULUS_NAME}}`, `{{CLONE_DIR}}`, `{{HYDRATION_DATE}}`, `{{BACKUP_PATH}}`) by literal string replacement only. Do not use Python `str.format`, which mangles double braces, and do not pass templates through a shell heredoc, which would expand the live `$VAR` references they contain. Angle-bracket forms like `<name>` in prose are human-facing instructions, never render targets. The full token table and file map live in `plugins/github_midwife_plugin/knowledge_base/hydration_templates/TEMPLATE_VARS.md`.

## Step 0 — before the birth: provision the role and database (wizard step 1)

Skip this section when `bootstrap.py` ran on this machine for this homunculus, because its role-and-db step already did everything here. It is REQUIRED before a verb-mode or existing-clone birth, and for any second homunculus on a machine that already runs one — the birth verb assumes it done and fail-louds if the auth line is missing. Canonical reference implementation: `bootstrap.py`'s `role_and_db` step (this section explains WHEN and WHY and gives the by-hand sequence; if the two ever disagree, `bootstrap.py` is the WHAT). Throughout, `<login-user>` is your macOS login user (the Homebrew-Postgres trust admin — run `whoami`).

Per-homunculus role isolation (operator override): each homunculus gets its OWN non-superuser PostgreSQL role, its OWN database, and its OWN schema — all named after it (`<name>` = `HOMUNCULUS_NAME`) — so no homunculus can access anything another created. There is NO shared `ananta` role, and NO credential ever crosses a homunculus namespace. The database must exist, be owned by the homunculus's own role, carry the `pgvector` extension (per-database), have PUBLIC connect revoked, and be password-gated over localhost. Only the scram gate is INVISIBLE if missing (a missing db fails the launch; a missing extension crash-loops at readiness; a missing revoke surfaces as an isolation-probe failure) — so this step ends with two explicit probes, the same ones the birth verb re-runs.

Instance topology is YOUR decision as the driving agent, never runtime code: a shared local Postgres instance is the DEFAULT. Choose a separate instance ONLY for a concrete reason — an incompatible existing Postgres major version, an extension conflict, a port or ownership conflict, or a customer policy that mandates separation. Honest boundary: on one machine under one OS user a cluster split adds no real isolation (the same user can read both data directories); full local standalone means a separate OS user or machine, which is out of scope here. When unsure, use the shared instance.

1. **Probe (never clobber):** `psql -U <login-user> -d postgres -tAc "SELECT datdba::regrole FROM pg_database WHERE datname='<name>'"`. Empty output → continue. A row → the database EXISTS: STOP and present the facts (name, printed owner) to the user — resume-of-an-interrupted-birth vs foreign-name-collision is the user's call, made in conversation.
2. **Create role + database (this homunculus's OWN, non-superuser):** `createuser -U <login-user> <name>` (plain — NO `-s`/`--createdb`/`--createrole`, so the role is non-superuser, which is load-bearing for the revoke below), then `createdb -U <login-user> -O <name> <name>`, then `psql -U <login-user> -d <name> -c 'CREATE EXTENSION IF NOT EXISTS "vector"'` (the `vector` extension is non-trusted, so the `<login-user>` superuser must create it — the newborn's own non-superuser role cannot).
3. **Revoke PUBLIC (per-homunculus isolation, R4):** `psql -U <login-user> -d postgres -c 'REVOKE CONNECT, TEMP ON DATABASE "<name>" FROM PUBLIC'`. The owner role keeps implicit ALL and the `<login-user>` superuser bypasses; every OTHER role — including every sibling homunculus's role — is then refused at the front door with `permission denied for database`.
4. **Gate (first newborn on this machine only):** find the auth config with `psql -U <login-user> -d postgres -tAc 'SHOW hba_file'`; if the default-scram block is not already present, insert these lines immediately ABOVE the blanket `trust` block (first-match-wins), substituting your `<login-user>` for the admin — the `all all` scram lines gate EVERY per-homunculus role at once, so this is one-time machine setup, never a per-birth edit:
   ```
   local   all     <login-user>                            trust
   host    all     <login-user>    127.0.0.1/32            trust
   host    all     <login-user>    ::1/128                 trust
   local   all     all                                     scram-sha-256
   host    all     all             127.0.0.1/32            scram-sha-256
   host    all     all             ::1/128                 scram-sha-256
   ```
   The three `<login-user>` trust lines re-assert passwordless admin access above the scram block. Insert-only: any existing legacy scram lines stay byte-identical. Reload: `psql -U <login-user> -d postgres -c 'SELECT pg_reload_conf()'`.
5. **Verify — two probes.** (a) NEGATIVE scram: `PGPASSWORD=wrong psql "host=127.0.0.1 dbname=<name> user=<name>" -c 'SELECT 1'` MUST fail with `password authentication failed`. If it CONNECTS, the scram gate isn't taking effect (position? reload?) — fix and re-run; never proceed while a wrong password is accepted. (b) ISOLATION (verb-mode, after the credential is seeded): the newborn's own role, with its REAL password, connecting to a SIBLING database (the parent's) must fail with `permission denied for database`, NOT `password authentication failed` — the birth verb's seed subprocess runs this probe itself. On the FIRST homunculus on a machine there is no sibling, so (b) is recorded as not-applicable.

**No credential copy.** Under per-homunculus isolation the newborn seeds its OWN role's password — a fresh value, generated in its own venv and `ALTER ROLE`-d onto its own role. There is NO pipe of another homunculus's password into this one; the old same-machine "credential pipe" sub-step is RETIRED, because reading another homunculus's Keychain entry is the exact cross-namespace access the isolation ruling exists to prevent.

**Resuming after Step 0 — which path called you here decides the next move.** On the README/`bootstrap.py` path (a cloned seed): re-run `bootstrap.py` — its `role_and_db` probe reads the reconciled state (role + your new database both present, revoke applied, auth line verified) as healthy and resumes at the next step. On the verb-mode path (an already-running homunculus births this one): proceed to the birth verb (`plugin::github_midwife_plugin::birth_homunculus`), which re-runs the negative-auth probe, seeds the newborn's OWN role in the newborn's OWN venv, and runs the isolation probe.

Full derivation and rationale: the sequence relocated out of `github_midwife_plugin/venv_provision.py` (renamed from `acquisition.py` when acquisition mode was retired) when database creation became a wizard step, and shifted to per-homunculus role isolation. The verb keeps `venv_provision.verify_newborn_db_scram_gated` (the negative scram probe) plus the post-seed isolation probe. `bootstrap.py`'s `role_and_db` step is the canonical WHAT.

## Step 1 — probe

Check each expected-good state read-only before writing anything.

- `<clone>/client/` absent means hydration has not run; present means a re-run, so compare intended content before overwriting and prefer updating over clobbering.
- `<clone>/CLAUDE.md` may exist from a prior hydration; same re-run rule. Probe `~/.claude/settings.json` for the `HOMUNCULUS_STEP_ZERO_HOOK=` / `HOMUNCULUS_ROLE_RECLAIM_HOOK=` markers (this homunculus's or another's) and `~/.claude/skills/rename/SKILL.md` for prior installs; a pre-2026-07-22 hydration may have left hook copies in `<clone>/.claude/settings.json` — migrate those to user scope on a re-run rather than leaving both.
- Read the shell templates that drive the integration: `zshrc.template`, `homunculus.zsh.template`, and any fleet/sample launcher file referenced by the clone or already present in the operator's startup file. Then inspect `~/.zshrc` or the active shell's startup file only far enough to classify its structure and choose an additive integration point. Do not print or copy secret-looking values into the transcript or generated files.
- Confirm genesis actually finished: the newborn's LaunchAgent plist exists and the manifest marker `<clone>/profile/data/github_midwife/attempt.json` is present. If not, stop; hydration follows genesis, it does not replace it.

## Step 2 — install the clone-side and user-scope artifacts

Render six artifacts. Four live inside the clone; two install at USER scope
(`~/.claude/`) by operator ruling 2026-07-22. The reason for user scope is
load-bearing, so understand it before deviating: Claude Code loads project
settings and skills from the directory a session starts in, and sessions that
coordinate through the homunculus routinely start in OTHER repos (a work repo,
a knowledge-base repo). Hooks installed only in the clone never fire for those
sessions — verified in the field: a fleet session in another repo got no
role-reclaim prompt and its role binding sat vacant with nobody told. User
scope fires from any directory; both hooks guard on
`HOMUNCULUS_AGENT_SESSION_LABEL` (set by the launchers), so sessions not
started through a homunculus launcher get zero output and zero errors.

Writing inside the clone needs no offer beyond the user's normal tool-approval
flow. The two user-scope writes touch the user's own Claude configuration:
back up an existing `~/.claude/settings.json` before first modification, and
merge structurally — never whole-file-replace it. If a destination path is
absent, write the rendered template directly. If `CLAUDE.md` or
`~/.claude/settings.json` already exists, do the guided merge below instead of
skipping the file or replacing the whole thing.

| Template | Destination | Mode |
|---|---|---|
| `homunculus.zsh.template` | `<clone>/client/<name>.zsh` | 0644 |
| `claude_launcher.template` | `<clone>/client/bin/claude-<name>` | 0755 |
| `launch.template` | `<clone>/client/bin/launch-<name>` | 0755 |
| `CLAUDE.md.template` | `<clone>/CLAUDE.md` | 0644 |
| `claude_settings.json.template` | `~/.claude/settings.json` (structural merge) | 0644 |
| `rename_skill_SKILL.md.template` | `~/.claude/skills/rename/SKILL.md` | 0644 |

Known limitation, state it rather than papering over it: on a machine running
MORE THAN ONE homunculus, each homunculus's hooks coexist in
`~/.claude/settings.json` (the per-name marker strings keep them separately
identifiable and mergeable), and every labeled session sees every homunculus's
Step Zero hook; the user-scope `rename` skill is a singleton, so the LAST
hydrated homunculus owns it. Single-homunculus machines — the normal case —
are unaffected. If the operator runs several homunculi and this bites, surface
it as a decision rather than inventing a scheme ad hoc.

**Existing `CLAUDE.md` guidance.** The rendered `CLAUDE.md.template` contains a homunculus-owned block bounded by:

```markdown
<!-- BEGIN HOMUNCULUS HYDRATION -->
...
<!-- END HOMUNCULUS HYDRATION -->
```

If `<clone>/CLAUDE.md` already exists, preserve its project-specific instructions and insert or update only that managed block. On a re-run, replace the existing block exactly. On a first merge, insert the block after the first top-level heading when one exists; otherwise insert it at the top. Leave the rest of the file in place. Do not decide that an existing `CLAUDE.md` is "probably fine" and leave it untouched: the homunculus operating block is how future Claude Code sessions learn the no-MCP Step Zero path, the implementation/debugging requirement to search the homunculus's own knowledge base first, the router-vs-bridge distinction, the session-ledger recall path, and the hook expectations.

**Existing `~/.claude/settings.json` guidance.** If the user-scope settings file already exists, back it up first (same collision-probed backup naming as Step 3's zshrc backup), then parse the existing file and the rendered `claude_settings.json.template` as JSON and merge structurally. Preserve unrelated top-level settings, unrelated hook events, and unrelated hook commands — this file governs ALL of the user's Claude Code sessions, not just homunculus work. For the two homunculus hooks, use the marker strings in the rendered commands as the agent-readable identity:

- `HOMUNCULUS_STEP_ZERO_HOOK=<name>`
- `HOMUNCULUS_ROLE_RECLAIM_HOOK=<name>`

For each relevant hook event, remove any existing command hook containing the same marker, then append the rendered hook object for that event. Keep existing non-homunculus hook objects (and other homunculi's marker-bearing hooks) in their current order. Create `~/.claude/` if needed, write valid pretty JSON, and validate it with a JSON parser before moving on. This is guidance for the driving agent, not hidden genesis automation: the point is to make the edit obvious and repeatable while preserving the user's configuration.

The launcher invariant is non-negotiable: `claude-<name>` never passes `--dangerously-skip-permissions`. The user's tool-approval flow is the safety boundary of the whole client-deployment pattern, and a generated launcher that bypasses it deletes that boundary. Do not add the flag on request without pointing the user at this paragraph first.

The settings hooks no-op silently for bare sessions, and at user scope this guard is load-bearing for the user's ENTIRE Claude Code use: a plain `claude` session anywhere on the machine has no session-label environment variable set, and BOTH hooks must produce zero output and zero errors in that case. The shipped template guards both; keep the guards if you edit.

## Step 3 — review, recommend, then offer shell integration

Do not start this step by asking a jargon question such as "source `client/<name>.zsh` from `.zshrc`, or use the absolute path?" That question is not grounded enough for an operator. First do the review from Step 1, then present a recommendation that explains the actual outcome.

The normal recommendation is additive shell integration: put a small managed source block in the operator's startup file so new terminals can run `claude-<name>` and related helper commands directly. Prefer this whenever the existing file can accept one more source block without changing unrelated content. Whole-file replacement is only for a missing, empty, or trivially generated file, or when the user explicitly chooses that route after seeing why additive integration is not coherent.

Before asking for approval, prepare these facts for yourself:

- which startup file you inspected, and whether it was missing, simple, already managed, or complex;
- whether any existing Claude/homunculus launcher pattern was present, described only structurally and without quoting private values;
- the exact source line or managed block you intend to add or update;
- the backup path you will create before writing;
- the operator-visible result: new terminals can run `claude-<name>` directly, while the absolute path `<clone>/client/bin/claude-<name>` remains available if they decline.

The user-facing offer should be specific and plain. Use this shape, adapted to the facts you found:

```
I checked your shell startup file and the <name> shell template. I recommend
adding a small managed block to ~/.zshrc that loads <name>'s launcher from this
clone. I will save a backup first, will not print or change any existing
secrets or unrelated settings, and existing aliases/functions will stay in
place. After that, new terminals can run claude-<name> directly. Should I make
that additive change?
```

If the startup file is missing, say that and recommend creating the minimal file. If the file is complex or already has a conflicting managed block, explain only the structure and the consequence, then ask the narrow decision you cannot safely make. Act only on an explicit yes.

On accept:

1. Choose the backup path `~/.zshrc.pre-<name>-hydration-<YYYYMMDD>` and collision-probe it with `test -f`. If it already exists, append `-HHMMSS`, then `-2`, `-3`, and so on until the path is free. An existing backup is never overwritten under any circumstances.
2. Copy the existing startup file to the backup path before changing it. If the file is absent, say so and create it.
3. Inspect the current structure and append or update a small managed block that sources `<clone>/client/<name>.zsh`. Do not duplicate the block on rerun.
4. If additive integration is not coherent, explain why and offer the whole-file replacement rendered from `zshrc.template` with `{{BACKUP_PATH}}` set to the backup path.

On decline: stop. No partial edits, no retry next session. Tell the user the launcher still works by absolute path: `<clone>/client/bin/claude-<name>`, and the newborn's `<name>` CLI should already be on PATH via genesis's `~/.local/bin/<name>` symlink.

If `~/.zshrc` did not exist at probe time, write a minimal file directly and say so; there is nothing to back up.

## Step 4 — named-session start

Requirement 3 is satisfied by the launcher that step 2 installed on the user's machine. With the accepted zshrc (new terminal window, or `source ~/.zshrc`), the command `claude-<name>` is on PATH; declined, the absolute path form works identically. Optional arguments: a session label (default `Operator`), then a model alias, then an effort level. The launcher exports the session-label and stable session-id environment variables and execs `claude --name <label>`, which is what makes the session properly named in both Claude Code and the homunculus's peer registry.

## Step 4a — offer: multi-role fleet coordination (optional)

Fleet coordination needs NO MCP anywhere: each session's SessionStart hook
invokes the rename skill, which arms that session's `<name> watch`
registered-presence watcher — register, claim the role as the durable
binding, drain missed messages, stream deliveries. Sessions are then
addressable by role name (`peer_send_by_name`) and receive role-addressed
messages, on any machine, including MCP-policy-blocked ones. Do not attempt
`claude mcp add` against policy and do not build ad-hoc registration
mechanisms — the watcher is the mechanism.

**Target work repos get the guidance too.** Launcher-started sessions carry
the homunculus guidance from the user-scope hooks in every prompt, from any
directory — but a BARE `claude` session in a work repo gets nothing (the
label guard is deliberate). So when the operator names the repos the fleet
will work in, OFFER the same managed-block merge Step 2 does for the clone's
`CLAUDE.md` into EACH target repo's `CLAUDE.md` (same
`BEGIN/END HOMUNCULUS HYDRATION` markers, same insert-or-update rule,
preserve everything else in the file). That block is what tells any future
session in that repo — launcher-started or not — that the homunculus exists,
how to reach it (`<name> call`, `<name> watch`), and that MCP is not needed.

Hook scope matters for fleets and is already settled (operator ruling
2026-07-22): Step 2 installs the Step Zero and role-reclaim hooks at USER
scope (`~/.claude/settings.json`), guarded on the session-label environment
variable, precisely so fleet sessions working in OTHER repos still get them —
Claude Code loads project settings from the directory a session starts in, so
clone-local hooks would never fire there. If you find the hooks only in the
clone's `.claude/settings.json`, that is a pre-ruling install: migrate them to
user scope per Step 2 rather than debugging why fleet sessions get no
role-reclaim prompt.

Everything through Step 4 gives the operator ONE properly-named session. Some operators want to run SEVERAL Claude Code (and/or Codex) sessions concurrently against the same homunculus, each independently addressable — a coordinator session, a session that owns all git mutations, an architect/reviewer session, worker sessions. This is exactly the pattern this platform's own development runs on (see this repo's own `.zshrc` and `.claude/settings.json` for a live, battle-tested example — `claude-coordinator-dawn`, `claude-git-controller`, `claude-architect`, and so on). Offer this explicitly; do not build it unasked, and do not build it as part of the Step 2 baseline.

**Ask first, don't prescribe.** "Do you want to run just one session with {{HOMUNCULUS_NAME}}, or several at once, each with its own role?" If several: "How many, and what should each be called?" Offer this platform's own roster as a concrete starting point, scaled to what the operator actually needs — most operators need far fewer than this repo's ~14 roles:

- A **coordinator** role (drives the work, delegates, holds the plan).
- A **git-controller** role, if more than one session will edit files in the same clone — see the git-safety paragraph below for why this matters.
- An **architect** or **reviewer** role, if the operator wants a second opinion / design-ruling session distinct from the one doing the work.
- Plain worker roles (`worker-1`, `worker-2`, ...) for parallel task lanes.

Let the operator pick names and count; do not invent roles they did not ask for.

**What accepting builds.** Render `fleet_functions.zsh.template` to `<clone>/client/{{HOMUNCULUS_NAME}}-fleet.zsh` and add `source "<clone>/client/{{HOMUNCULUS_NAME}}-fleet.zsh"` to `<clone>/client/{{HOMUNCULUS_NAME}}.zsh` (the file Step 2 already installed). The template gives one shared `_claude_for_{{HOMUNCULUS_NAME}}(role, model, effort)` launcher plus ONE example role function to copy per role the operator chose (a restart variant is included in the same comment block). This is a genuinely different trust level than the Step 2 launcher — read the template's own header comment and carry that reasoning into the conversation with the operator, don't just render it silently:

- `--remote-control "$role"` makes the session addressable by Claude Code's remote-control feature from another session — what lets a coordinator drive or monitor workers. A Claude Code feature; no MCP involved.
- `--dangerously-skip-permissions` is opt-in and separately gated in the template (an env var the operator sets, not a hardcoded flag). It removes the per-action tool-approval prompt — necessary for several sessions to run without an operator babysitting each confirmation, but it removes the safety boundary Step 2's invariant exists to protect. State this tradeoff to the operator in plain language and let them decide; do not default it on.
- Peer messaging and role binding come from each session's watcher (armed by the SessionStart hook → rename skill), not from any launcher flag. The optional MCP development-channel flag (`{{HOMUNCULUS_NAME}}_FLEET_MCP_CHANNELS=1`) exists in the template gated OFF; it is only for an operator who explicitly asks for MCP-native wake and whose policy permits MCP — never suggest it unprompted.

**Git safety when more than one session shares a clone — an OPTIONAL, nameable gate you must NOT default on.** If the operator is choosing more than one role, ask directly: will more than one of these sessions ever run git commands (commit, push, checkout, stash, branch) against the SAME clone? If yes, concurrent mutating git from multiple sessions is a real collision risk (a stash or checkout from one session can silently clobber another session's in-progress work). There is an OPTIONAL gate for exactly this, and the operator names it themselves: designate ONE role — **any name they choose** (`Git-Controller`, `gitops`, whatever) — as the sole git-mutator, and every other session is then blocked from mutating git. The reference implementation lives in the PLATFORM ORIGIN repository (`.claude/hooks/git_controller_gate.py` with `_git_controller_lex.py` + `_git_controller_walker.py`, enabled by setting `HOMUNCULUS_GIT_CONTROLLER_NAME=<chosen-role>` on the gate's `PreToolUse` command; the origin's `deployment/scripts/setup_clone.sh` step 3 shows the exact wiring) — **the seed does NOT ship these files.** On a seed-born deployment, be honest about that: if the operator wants the gate, obtaining or re-authoring those hook scripts is explicit fleet-setup work, not something already present in the clone. Do not cite paths to the operator as if they exist locally. **Default is OFF, and state the tradeoff up front, plainly:** enabling the gate BLOCKS the Task tool (Anthropic subagents) for every non-controller session — **and that block is the gate's PRIMARY PURPOSE, not a side effect.** Those subagents spawn their own git worktrees and run git, which is exactly how in-progress work gets lost — the very collision the gate exists to prevent. So the choice is honest and either-way: an operator who runs several git-mutating sessions against one clone enables the gate (names a controller, gives up subagents); an operator who wants Anthropic's subagents leaves the gate OFF and works some other way (separate clones per session, or a single git-mutating session). Do NOT default it on; do NOT skip this conversation when more than one session will share a clone.

**Operational guidance: leave sessions running, use `/clear`.** Once a role's session is started, tell the operator to leave it running in its own iTerm2 (or other terminal) tab rather than closing it between conversations. Quitting a session loses its process and its role's live identity until it is manually relaunched; the `/clear` slash command instead resets the conversation while the process (and its stable `HOMUNCULUS_AGENT_SESSION_ID`) keeps running — the SessionStart hook's `matcher` already covers `clear` (Step 2's `claude_settings.json.template`), so the role re-claim fires automatically and the session is addressable again within moments. This is the operational rhythm this platform runs on: long-lived tabs, `/clear` between tasks, restart only when a session is genuinely stuck.

## Step 4b — activate shipped plugins; configure credentials on first use

Shipped profile plugins should be installed and manifest-active now. Do not pitch each plugin as an optional birth-time decision. The operator already chose the seed/profile; hydration's job is to make that selected profile usable.

Some plugins still need real operator-specific setup before first useful invocation: a Jira site URL, a Snowflake account, a Salesforce connected app, Google OAuth, session-history access, and similar tenant/credential facts. Those are first-use configuration flows. The correct behavior is install-all-now, configure-on-first-use.

**Discover.** Glob `<clone>/plugins/*/knowledge_base/hydration_guidance.md` directly on the filesystem — NOT a knowledge-base search. A plugin can be present but not yet useful without credentials; a raw filesystem glob over the actual clone finds every guidance file that exists in it right now, regardless of manifest/ingestion state.

**Record the first-use contract.** For each guidance file found, read its `## Setup` section and keep the setup trigger attached to that plugin. Do not interrupt birth to collect every possible credential. When the operator first asks to use that plugin, follow its setup steps then, with the operator present and the concrete target known.

**Safe activation now.** If a guidance file contains non-secret, local-only activation work required for the shipped plugin to register or start, do it now after probing state and showing the exact action. If it asks for external tenant identity, OAuth, API tokens, file-system consent beyond the current clone, or any credential, defer to first use.

**Authoring convention, for the next plugin that needs this:** a `hydration_guidance.md` is a normal KB article (proper `Article Layer`/`Article Role: hydration_guidance`/`Article Tags`/`Embedding Description` header, so it is also findable by normal KB search once the plugin is active). Keep `## Setup` concrete enough that a first-use agent can run it without rediscovering the plugin's credential model.

## Step 5 — optional MCP bridge, ONLY on explicit operator request

Skip this step by default — it is not part of baseline hydration on any
machine. Nothing requires MCP: process calls, knowledge-base search,
session-history ingestion, role claiming, and message receive all run through
the `<name>` command (`<name> call`, `<name> watch`). If you notice the
environment permits MCP, you may mention once that an optional MCP bridge
exists for tool-native access, then move on; act only if the operator
explicitly asks for it. When asked: follow the seed README's registration
ladder. Reference form: `claude mcp add --scope user -e HOMUNCULUS_NAME=<name>
-e HOMUNCULUS_AGENT_IDENTITY=claude_code <name> --
<clone>/.venv/bin/python3 -m agent_messaging_plugin.mcp_bridge`. Probe
`claude mcp add --help` before trusting that form; CLI flags drift.

## Step 6 — verify

Verify each of the following before declaring hydration complete.

First, that the homunculus itself is alive (not just reachable):

- `launchctl list | grep <label>` shows the SAME process id across two checks a minute apart — a changing id means a crash-loop, not a running homunculus.
- The newest log under `<clone>/profile/data/logs/` is quiescent: `wc -l <logfile> && sleep 5 && wc -l <logfile>` prints equal counts.
- Optional: the log names the bridge port it bound (allocated dynamically at start; also in `~/.ananta/runtime/<name>.bridge.port`); `curl -s http://127.0.0.1:<port>/api/v1/bridge/health` answers.

Then, that the no-MCP command and session tooling work:

- `<name> health` answers.
- `<name> call service_interface::knowledge_service::search '{"query": "hydration runbook", "top_k": 3}'` returns a successful action/result path.
- A fresh session started via `claude-<name>` carries the intended label and can use the `<name>` command without a venv activation.
- `CLAUDE.md` contains the `BEGIN HOMUNCULUS HYDRATION` / `END HOMUNCULUS HYDRATION` block, including the no-MCP Step Zero command, the implementation/debugging KB-first rule, and the router-vs-bridge distinction.
- `~/.claude/settings.json` parses as JSON and contains exactly one `HOMUNCULUS_STEP_ZERO_HOOK=<name>` command and exactly one `HOMUNCULUS_ROLE_RECLAIM_HOOK=<name>` command; `~/.claude/skills/rename/SKILL.md` exists.
- The UserPromptSubmit hook in that session points the agent at `<name> call`, not MCP.
- A bare `claude` session in an unrelated directory starts with no hook errors and no injected homunculus context (the label guard covers both hooks).
- A LABELED session started in a different repo (not the clone) DOES receive the Step Zero and role-reclaim context — this is the fleet case the user-scope ruling exists for.
- In a labeled session, the rename skill arms `<name> watch` and its first output line is `"watch": "armed"` with a `claimed`/`updated`/`displaced` result — role-addressed receive is live with zero MCP.
- Role-claim ground truth: `<name> call plugin::agent_messaging_plugin::peer_holds_role` with the role name and the watcher's `agent_instance_id` resolves the claim to this session. An entry in raw peer-list output is connection presence, not a claim — never verify registration from a peer list.
- Only if the operator explicitly requested MCP in Step 5: `claude mcp list` shows `<name>` and a fresh session can call one `mcp__<name>__*` tool. Absence of MCP is not a finding.

## Your blue-green router (auto-installed at birth)

If your homunculus runs a **blue-green-capable profile** (its plugin allowlist includes `macos_self_deployment_plugin` — the `bizops`/standard tier), genesis **automatically installs a per-homunculus blue-green router** as the last birth step, right after the main autostart LaunchAgent. No operator action is needed: the router picks a free port in 8800-8999, writes its `<name>.router.port` + `<name>.bridge.port` discovery files, and loads under the label `local.homunculus.<name>.router`. That router is what lets the homunculus adopt new code with **zero downtime** via `apply_manifest` (blue-green swap). It is a separate KeepAlive LaunchAgent by design — it runs independently of Ananta and survives swaps untouched.

**Free-tier homunculi** (the `macos_free_minimal` profile, no self-deployment plugin) are **single-color by design** and have no router; their update path is a plain restart, not a blue-green swap. Genesis records the router step as `skipped` for them — that is expected, not a failure.

**One-line repair** if a capable homunculus ever loses its router (crash that KeepAlive didn't restore, manual removal): re-run the idempotent installer from the clone —

```
.venv/bin/python3 plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/blue_green_router/install_router.py <name>
```

It is a no-op success when the router is already healthy. `uninstall_router.py <name>` is the symmetric teardown.

## Your git worktree (auto-created at birth)

A seed never ships `.git` — the "no contaminated history travels" invariant is exactly why `.git/` is `never_copy` in the seed manifest — so a freshly-hydrated seed clone arrives as a plain source tree, **not** a git worktree. Genesis fixes this as its final step: it `git init`s the born tree with a **fresh, empty history of its own** (never the minting homunculus's `.git`), writes a sensible `.gitignore`, sets a **local** git identity (repo config only — your global git config is never touched), and makes one initial commit. No operator action is needed.

This is what lets `platform_dev_surface_plugin` come ready (its readiness probe runs `git rev-parse --is-inside-work-tree`, which a plain source tree fails) and makes any git-based workflow in the clone work from first boot. The step is **idempotent**: a tree that is already a worktree is left untouched, and an existing `.gitignore` is preserved, never clobbered — so if you cloned the seed from a GitHub repo (which arrives WITH a `.git`), genesis correctly leaves your existing history alone. The initial local identity (`<name>` / `<name>@localhost`) is a placeholder you are free to change with `git config`.

## What the user should expect afterward

With a session's watcher armed (the rename skill does this at session start), peer and role-addressed messages stream into the watch task's output and surface when the session next looks at it — not as live interruptions. Without a watcher, messages queue durably and drain when one next arms. This is designed behavior, not a defect; the generated CLAUDE.md states the same expectation to the user directly.

Senders see it named honestly: an IMPORTANT send to a watcher-held session returns `delivery="queued_watcher"` — delivered into the watch output, acted on at the recipient's next look, never a live turn. The watcher's own event-stream ack marks the message consumed platform-side, so armed watchers do not generate deaf-wake escalations; if a `deaf_wake_escalation` names a watcher-held role, that watcher is dead or its output is never being read — re-arm it (`/rename <Role>` or a fresh `<name> watch`) and resend. Where the driving harness supports a monitor-style waker (wake-on-output over a background task), run `<name> watch` under it — that is the cheapest near-interrupt upgrade, with zero platform change.

## Reference

- `bootstrap.py` (repo root) — the canonical reference implementation of the step 0 sequence, in its `role_and_db` step.
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py` — `verify_newborn_db_scram_gated`, the negative-auth probe the birth verb re-runs against step 0's work.
- `plugins/github_midwife_plugin/knowledge_base/hydration_templates/TEMPLATE_VARS.md` — render tokens, file map, and the pending env-var rename register.
- `knowledge_bases/ananta_platform/17_client_deployment_pattern/01_pattern_overview.md` — the platform pattern hydration instantiates: in-clone package, agent-installed, zero secrets.
- `knowledge_bases/ananta_platform/13_homunculus_setup/08_macos_homunculus_birth_runbook.md` — the birth runbook whose genesis this runbook follows.
- `plugin::github_midwife_plugin::birth_homunculus` — the genesis verb that precedes hydration.
- `service_interface::knowledge_service::search` — the Step Zero search the generated CLAUDE.md instructs the newborn's driving agent to run through `<name> call`.
- `<name> watch` (`plugins/agent_messaging_plugin/src/agent_messaging_plugin/local_cli/cli.py`) — the registered-presence watcher the generated rename skill arms: register, claim, drain, stream, reconnect.
- `plugin::agent_messaging_plugin::peer_claim_role` — the role-claim process the watcher dispatches over its registered bridge.
