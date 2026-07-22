# Seed Hydration Runbook — Operator Environment Setup After Genesis

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for setting up the user's own environment around a homunculus birth, covering wizard step one per-homunculus role and database provisioning before a verb-mode or same-machine birth (create the homunculus's own non-superuser role, its database, the pgvector extension, the PUBLIC-connect revoke, the one-time default-scram pg_hba block, and the negative-auth and isolation probes — no cross-homunculus credential copy, and shared-vs-separate Postgres instance as the driving agent's topology decision), then after genesis the shell launcher for a properly named Claude Code session, optional multi-role fleet setup, additive shell integration with recoverable backups, install-all-now configure-on-first-use plugin hydration guidance, generated project CLAUDE.md and settings hooks, no-MCP primary command-line operation through the per-homunculus `<name>` command, optional MCP bridge registration only where policy permits, and the homunculus-alive verification checklist.

## When to use this runbook

Run this ladder after genesis completes and the newborn homunculus boots, when the user's own environment still lacks the launch tooling. Genesis provisions the homunculus side only, so a fresh clone gives the user no command to start a named Claude Code session, no project CLAUDE.md, and no Claude Code hooks. This runbook is written TO the driving coding agent, the same audience as the genesis section of the seed README. Everything here follows the genesis ladder shape: probe first, offer, act only under the user's normal tool-approval flow, verify, and stop and ask rather than guess.

Hydration is deliberately NOT genesis code. The files this ladder writes belong to the user's side of the genesis boundary, exactly like the `claude mcp add` registration step: the agent performs them with its own tools, in conversation, under the user's approval.

## Three fixed rules

These rules are operator requirements, not tunable defaults. Every step below is shaped by them.

1. Shell startup files may be inspected to understand structure, but never echo, transmit, summarize, or persist secret-looking values. Treat tokens, keys, passwords, tenant URLs, and private hostnames as sensitive: if they appear, say only that secret-looking or private values were present and avoided. Integrate additively when possible; whole-file replacement is the fallback, not the default.
2. Any user-owned shell change is an OFFER, but the analysis is not optional. Before asking, inspect the current startup file, the shipped `zshrc.template`, the rendered `<clone>/client/<name>.zsh`, and any existing local launcher/fleet pattern you find. Then recommend a concrete plan in plain language. Do not ask the user to choose between raw implementation details before doing that review.
3. The user always ends up with a command-line way to start Claude Code with a properly named session, whether or not they accepted shell integration.

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
- `<clone>/.claude/settings.json` and `<clone>/CLAUDE.md` may exist from a prior hydration; same re-run rule.
- Read the shell templates that drive the integration: `zshrc.template`, `homunculus.zsh.template`, and any fleet/sample launcher file referenced by the clone or already present in the operator's startup file. Then inspect `~/.zshrc` or the active shell's startup file only far enough to classify its structure and choose an additive integration point. Do not print or copy secret-looking values into the transcript or generated files.
- Confirm genesis actually finished: the newborn's LaunchAgent plist exists and the manifest marker `<clone>/profile/data/github_midwife/attempt.json` is present. If not, stop; hydration follows genesis, it does not replace it.

## Step 2 — install the clone-side artifacts

Render and write the six in-clone artifacts; none of them touch a dotfile. Writing inside the clone needs no offer beyond the user's normal tool-approval flow.

| Template | Destination | Mode |
|---|---|---|
| `homunculus.zsh.template` | `<clone>/client/<name>.zsh` | 0644 |
| `claude_launcher.template` | `<clone>/client/bin/claude-<name>` | 0755 |
| `launch.template` | `<clone>/client/bin/launch-<name>` | 0755 |
| `CLAUDE.md.template` | `<clone>/CLAUDE.md` | 0644 |
| `claude_settings.json.template` | `<clone>/.claude/settings.json` | 0644 |
| `rename_skill_SKILL.md.template` | `<clone>/.claude/skills/rename/SKILL.md` | 0644 |

The launcher invariant is non-negotiable: `claude-<name>` never passes `--dangerously-skip-permissions`. The user's tool-approval flow is the safety boundary of the whole client-deployment pattern, and a generated launcher that bypasses it deletes that boundary. Do not add the flag on request without pointing the user at this paragraph first.

The settings hooks no-op silently for bare sessions. A user who runs plain `claude` instead of the launcher has no session-label environment variable set, and the SessionStart hook must produce zero output and zero errors in that case. The shipped template already guards this; keep the guard if you edit.

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
I checked your shell startup file and the Dax shell template. I recommend adding
a small managed block to ~/.zshrc that loads Dax's launcher from this clone. I
will save a backup first, will not print or change any existing secrets or
unrelated settings, and existing aliases/functions will stay in place. After
that, new terminals can run claude-dax directly. Should I make that additive
change?
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

Everything through Step 4 gives the operator ONE properly-named session. Some operators want to run SEVERAL Claude Code (and/or Codex) sessions concurrently against the same homunculus, each independently addressable — a coordinator session, a session that owns all git mutations, an architect/reviewer session, worker sessions. This is exactly the pattern this platform's own development runs on (see this repo's own `.zshrc` and `.claude/settings.json` for a live, battle-tested example — `claude-coordinator-dawn`, `claude-git-controller`, `claude-architect`, and so on). Offer this explicitly; do not build it unasked, and do not build it as part of the Step 2 baseline.

**Ask first, don't prescribe.** "Do you want to run just one session with {{HOMUNCULUS_NAME}}, or several at once, each with its own role?" If several: "How many, and what should each be called?" Offer this platform's own roster as a concrete starting point, scaled to what the operator actually needs — most operators need far fewer than this repo's ~14 roles:

- A **coordinator** role (drives the work, delegates, holds the plan).
- A **git-controller** role, if more than one session will edit files in the same clone — see the git-safety paragraph below for why this matters.
- An **architect** or **reviewer** role, if the operator wants a second opinion / design-ruling session distinct from the one doing the work.
- Plain worker roles (`worker-1`, `worker-2`, ...) for parallel task lanes.

Let the operator pick names and count; do not invent roles they did not ask for.

**What accepting builds.** Render `fleet_functions.zsh.template` to `<clone>/client/{{HOMUNCULUS_NAME}}-fleet.zsh` and add `source "<clone>/client/{{HOMUNCULUS_NAME}}-fleet.zsh"` to `<clone>/client/{{HOMUNCULUS_NAME}}.zsh` (the file Step 2 already installed). The template gives one shared `_claude_for_{{HOMUNCULUS_NAME}}(role, model, effort)` launcher plus ONE example role function to copy per role the operator chose (a restart variant is included in the same comment block). This is a genuinely different trust level than the Step 2 launcher — read the template's own header comment and carry that reasoning into the conversation with the operator, don't just render it silently:

- `--remote-control "$role"` makes the session addressable by Claude Code's remote-control feature from another session — what lets a coordinator drive or monitor workers.
- `--dangerously-load-development-channels server:{{HOMUNCULUS_NAME}}` wires the MCP development-channel bridge that gives `peer_send` its idle-wake path. The Step 2 launcher deliberately omits this (see "What the user should expect afterward" below); a fleet needs it so sessions can wake each other.
- `--dangerously-skip-permissions` is opt-in and separately gated in the template (an env var the operator sets, not a hardcoded flag). It removes the per-action tool-approval prompt — necessary for several sessions to run without an operator babysitting each confirmation, but it removes the safety boundary Step 2's invariant exists to protect. State this tradeoff to the operator in plain language and let them decide; do not default it on.

**Git safety when more than one session shares a clone — an OPTIONAL, nameable gate you must NOT default on.** If the operator is choosing more than one role, ask directly: will more than one of these sessions ever run git commands (commit, push, checkout, stash, branch) against the SAME clone? If yes, concurrent mutating git from multiple sessions is a real collision risk (a stash or checkout from one session can silently clobber another session's in-progress work). There is an OPTIONAL gate for exactly this, and the operator names it themselves: designate ONE role — **any name they choose** (`Git-Controller`, `gitops`, whatever) — as the sole git-mutator, and every other session is then blocked from mutating git. The reference implementation is `.claude/hooks/git_controller_gate.py` (with `_git_controller_lex.py` + `_git_controller_walker.py`), enabled by setting `HOMUNCULUS_GIT_CONTROLLER_NAME=<chosen-role>` on the gate's `PreToolUse` command in `.claude/settings.json` — see `deployment/scripts/setup_clone.sh` step 3 for the exact wiring. **Default is OFF, and state the tradeoff up front, plainly:** enabling the gate BLOCKS the Task tool (Anthropic subagents) for every non-controller session — **and that block is the gate's PRIMARY PURPOSE, not a side effect.** Those subagents spawn their own git worktrees and run git, which is exactly how in-progress work gets lost — the very collision the gate exists to prevent. So the choice is honest and either-way: an operator who runs several git-mutating sessions against one clone enables the gate (names a controller, gives up subagents); an operator who wants Anthropic's subagents leaves the gate OFF and works some other way (separate clones per session, or a single git-mutating session). Do NOT default it on; do NOT skip this conversation when more than one session will share a clone.

**Operational guidance: leave sessions running, use `/clear`.** Once a role's session is started, tell the operator to leave it running in its own iTerm2 (or other terminal) tab rather than closing it between conversations. Quitting a session loses its process and its role's live identity until it is manually relaunched; the `/clear` slash command instead resets the conversation while the process (and its stable `HOMUNCULUS_AGENT_SESSION_ID`) keeps running — the SessionStart hook's `matcher` already covers `clear` (Step 2's `claude_settings.json.template`), so the role re-claim fires automatically and the session is addressable again within moments. This is the operational rhythm this platform runs on: long-lived tabs, `/clear` between tasks, restart only when a session is genuinely stuck.

## Step 4b — activate shipped plugins; configure credentials on first use

Shipped profile plugins should be installed and manifest-active now. Do not pitch each plugin as an optional birth-time decision. The operator already chose the seed/profile; hydration's job is to make that selected profile usable.

Some plugins still need real operator-specific setup before first useful invocation: a Jira site URL, a Snowflake account, a Salesforce connected app, Google OAuth, session-history access, and similar tenant/credential facts. Those are first-use configuration flows. The correct behavior is install-all-now, configure-on-first-use.

**Discover.** Glob `<clone>/plugins/*/knowledge_base/hydration_guidance.md` directly on the filesystem — NOT a knowledge-base search. A plugin can be present but not yet useful without credentials; a raw filesystem glob over the actual clone finds every guidance file that exists in it right now, regardless of manifest/ingestion state.

**Record the first-use contract.** For each guidance file found, read its `## Setup` section and keep the setup trigger attached to that plugin. Do not interrupt birth to collect every possible credential. When the operator first asks to use that plugin, follow its setup steps then, with the operator present and the concrete target known.

**Safe activation now.** If a guidance file contains non-secret, local-only activation work required for the shipped plugin to register or start, do it now after probing state and showing the exact action. If it asks for external tenant identity, OAuth, API tokens, file-system consent beyond the current clone, or any credential, defer to first use.

**Authoring convention, for the next plugin that needs this:** a `hydration_guidance.md` is a normal KB article (proper `Article Layer`/`Article Role: hydration_guidance`/`Article Tags`/`Embedding Description` header, so it is also findable by normal KB search once the plugin is active). Keep `## Setup` concrete enough that a first-use agent can run it without rediscovering the plugin's credential model.

## Step 5 — optional MCP bridge, only where policy permits

If the operator's environment permits MCP servers, follow the seed README's registration ladder to connect the newborn's MCP bridge to Claude Code. If policy restricts MCP servers, skip this step. Skipping MCP is not a degraded or unsupported state; the `<name>` command is the primary interface on managed work machines. Reference form when permitted: `claude mcp add --scope user -e HOMUNCULUS_NAME=<name> -e HOMUNCULUS_AGENT_IDENTITY=claude_code <name> -- <clone>/.venv/bin/python3 -m agent_messaging_plugin.mcp_bridge`. Probe `claude mcp add --help` before trusting that form; CLI flags drift.

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
- The SessionStart hook fires in that session and the rename skill claims the session's role binding (the registry entry that routes messages addressed to that session name); the claim result reads `registered`, `updated`, or `displaced`.
- A bare `claude` session in an unrelated directory starts with no hook errors.
- If MCP was registered because policy permits it, `claude mcp list` shows `<name>` and a fresh Claude Code session can see `mcp__<name>__*` tools and call one.

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

Messages from the homunculus surface when the user next interacts with a session, not as live interruptions. The launcher deliberately omits the development-channel wiring that gives the operator fleet its idle-wake path, so this is designed behavior, not a defect; the generated CLAUDE.md states the same expectation to the user directly.

## Reference

- `bootstrap.py` (repo root) — the canonical reference implementation of the step 0 sequence, in its `role_and_db` step.
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py` — `verify_newborn_db_scram_gated`, the negative-auth probe the birth verb re-runs against step 0's work.
- `plugins/github_midwife_plugin/knowledge_base/hydration_templates/TEMPLATE_VARS.md` — render tokens, file map, and the pending env-var rename register.
- `knowledge_bases/ananta_platform/17_client_deployment_pattern/01_pattern_overview.md` — the platform pattern hydration instantiates: in-clone package, agent-installed, zero secrets.
- `knowledge_bases/ananta_platform/13_homunculus_setup/08_macos_homunculus_birth_runbook.md` — the birth runbook whose genesis this runbook follows.
- `plugin::github_midwife_plugin::birth_homunculus` — the genesis verb that precedes hydration.
- `service_interface::knowledge_service::search` — the Step Zero search the generated CLAUDE.md instructs the newborn's driving agent to run.
- `plugin::agent_messaging_plugin::peer_claim_role` — the role-claim process the generated rename skill invokes.
