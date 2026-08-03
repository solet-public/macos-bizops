# Hydration templates — render contract + rename register

**NOT a KB article.** This directory is excluded from knowledge-base ingestion
(`content.patterns.exclude` in `../manifest.yaml`) — these files are render
sources the driving agent instantiates at genesis time, per the hydration
runbook
(`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`).

## Placeholder convention (seam contract with the SF-C seed-README generator)

- `{{HOMUNCULUS_NAME}}` style double-brace tokens are the **machine-render
  tokens**. Substitute by **literal string replacement** — never Python
  `str.format` (it treats `{{` as an escaped brace) and never a shell
  heredoc pass (templates contain live `$VAR` shell references that must
  survive verbatim).
- `<name>`-style angle brackets are **human-instruction prose only** (the
  genesis README ladder convention). They are never render targets.

| Token | Value at render time |
|---|---|
| `{{HOMUNCULUS_NAME}}` | the newborn's validated name (`^[a-z][a-z0-9_-]{1,62}$`) |
| `{{CLONE_DIR}}` | absolute path of the newborn clone (no trailing slash) |
| `{{HYDRATION_DATE}}` | `YYYY-MM-DD` of the hydration run |
| `{{BACKUP_PATH}}` | the collision-free .zshrc backup path chosen in the runbook's offer step (only in `zshrc.template`) |
| `{{MARKETPLACE_NAME}}` | the Claude Code and Codex marketplace name, **DERIVED from `{{HOMUNCULUS_NAME}}`** by one documented transform — underscores become hyphens (`acme_corp` → `acme-corp`) — reported at hydration output and recorded in the hydration record, never silent (operator ruling 2026-08-01: *"the name of the seed is the name of the seed… built off a user input — use that"*). Every other kebab-case violation, and the 16 reserved Claude marketplace names, **REFUSE LOUDLY** naming the rule — a silent skip would register no marketplace and the plugin would simply be absent, with no error anywhere. ⚠ The `_`→`-` transform is **non-injective** (`a_b` and `a-b` both derive `a-b`), so hydration also refuses when the derived name is already registered to a different source. Verification legs derive the expected enabled/installed plugin selector from the rendered name, never pin a literal |
| `{{GIT_CONTROLLER_NAME}}` | the role name the operator chose as sole git-mutator in the runbook's git-safety conversation (Step 4a), rendered into **all generated coordination launcher templates** — this is the *only* token whose correct render is sometimes **no line at all**: a solo deployment DELETES the whole `GIT_CONTROLLER_NAME=…` line, and that absence IS the exemption (see the `GIT_CONTROLLER_NAME` row below). Never invent a default; a name nobody chose arms a gate against a role that does not exist and blocks every session |

## Per-template notes

⚠ **This is NOT the roster and does not claim to be complete.** The CANONICAL
list of hydration templates — membership, count, and which renders are
conditional — is the render table in `01_hydration_runbook.md`. This section
carries only the *per-template semantics* a renderer needs (why a file exists,
what its schema requires, what silently breaks). If a template is missing a row
here that is a documentation gap; if it is missing from the runbook table, that
is a hydration defect. Never derive a count from this section, and never
reconcile the runbook against it — the dependency runs one way.

| Template | Rendered to | Mode |
|---|---|---|
| `zshrc.template` | `~/.zshrc` (ONLY on accepted offer — see runbook) | 0644 |
| `homunculus.zsh.template` | `<clone>/client/<name>.zsh` | 0644 |
| `claude_launcher.template` | `<clone>/client/bin/claude-<name>` | 0755 |
| `codex_launcher.template` | `<clone>/client/bin/codex-<name>` | 0755 |
| `launch.template` | `<clone>/client/bin/launch-<name>` | 0755 |
| `CLAUDE.md.template` | `<clone>/CLAUDE.md` | 0644 |
| `AGENTS.md.template` | `<clone>/AGENTS.md` | 0644 |
| `claude_settings.json.template` | `~/.claude/settings.json` (USER scope, structural merge — operator ruling 2026-07-22: fleet sessions start in other repos, and project-scope hooks never fire there) | 0644 |
| `rename_skill_SKILL.md.template` | `~/.claude/skills/rename/SKILL.md` (USER scope, same ruling — the role-reclaim hook invokes it from any repo) | 0644 |
| `fleet_functions.zsh.template` | `<clone>/client/<name>-fleet.zsh` (ONLY on accepted Step 4a offer — see runbook) | 0644 |
| `marketplace_json.template` | `<clone>/.claude-plugin/marketplace.json` — the plugin catalogue the user-scope `extraKnownMarketplaces` entry points at. **Rendered, not copied:** the seed `copy:` allowlist is `ananta/`, `plugins/`, `disabled_plugins/`, `root_manifest.yaml`, so a `.claude-plugin/` directory committed at the platform repo root would **not ship** — hydration generates it instead, which also keeps this separate from the platform checkout's own root `.claude-plugin/` question (D-5a.4). The `owner` is a **name only, no `email`** (operator ruling 2026-08-01) — the only identity this lane ships, and it is a name with no person attached. ⚠ **The plugin entry's `source` is a plain STRING, relative to the marketplace root (the directory holding `.claude-plugin/`, i.e. `<clone>`) — NOT an object.** Measured 2026-08-02: Claude Code 2.1.220 rejects `{"source":"directory","path":…}` in a *plugin* entry with `Stubbing unparseable marketplace plugin entry … source: Invalid input`, then `Failed to cache plugin … source type your Claude Code version does not support` — **silently, with no operator-visible error outside `--debug`**, so the plugin is simply never installed while everything downstream looks normal. Corroborated independently: in the live `claude-plugins-official` catalogue every local-directory plugin source is a bare string (`./plugins/<name>`, `./external_plugins/<name>`) and object form appears **only** for remote `git-subdir`/`url` sources. 🚨 **Do NOT propagate this fix to `claude_settings.json.template`** — for the *marketplace registration* (`extraKnownMarketplaces` / `known_marketplaces.json`) the object form `{"source":"directory","path":"<absolute>"}` is the CORRECT and measured-working shape. Two different schema positions; the same-looking change breaks the working one | 0644 |
| `codex_marketplace_json.template` | `<clone>/.agents/plugins/marketplace.json` — stock Codex's repo marketplace catalogue. It resolves the clone-relative `codex_plugin/coordination-hooks` bytes, remains `AVAILABLE` rather than silently installing itself, and requires the explicit `codex plugin marketplace add <clone>` then `codex plugin add coordination-hooks@<marketplace-name>` hydration steps. Hook execution still requires separate review in Codex's `/hooks` surface | 0644 |
| `user_claude_md_section.template` | `~/.claude/CLAUDE.md` (USER scope, **CREATE-or-merge** of one marker-delimited section) — the production instruction surface (operator ruling 2026-08-01: nobody launches production sessions from the deployment directory, so `<clone>/CLAUDE.md` reaches nobody there). Body is the operator's **ruled minimal bootstrap**: the first sentence plus the ONE action that brings up knowledge-base page zero — deliberately nothing else, because page zero is what orients the session. ⚠ The installer takes this body **from this template**, never from a literal of its own: the pinned query (`"session start orientation"`) is tuned *with* page zero and re-validated by that article's retrieval test, so a baked-in copy would drift out of retrieval silently. ⚠ The marker is **deployment-keyed AND versioned** (`<!-- BEGIN HOMUNCULUS <name> v1 -->`): the key is what lets a second homunculus install alongside the first without clobbering it, and the version is what lets a later installer recognise an out-of-date section and re-merge rather than duplicate. This file is one the user owns wholesale — merge the section, never rewrite the file | 0644 |

This directory stays **FLAT** — the KB manifest's single exclude pattern
(`hydration_templates/*`) relies on it (`Path.match` has no recursive `**`).

**`CLAUDE.md.template` and `AGENTS.md.template` are the same knowledge-bootstrap
contract for two tools.** Both render the identical `<!-- BEGIN HOMUNCULUS
HYDRATION -->` / `<!-- END HOMUNCULUS HYDRATION -->` managed block, both are
merged with the same insert-or-update rule (see the runbook's Step 2), and
both state the same access-mode contract: the no-MCP `<name> call` CLI is the
default for knowledge/process/session-ledger access, MCP is opt-in only on
explicit operator request, and source-artifact recovery (reading the KB's raw
markdown directly) is the last-resort fallback when the homunculus runtime
itself is unavailable. Keep the two files in lockstep on that contract; let
them diverge only where the driving tool genuinely differs (Claude Code's
`claude-<name>` launcher and `<name> watch`/`<name> wake` no-MCP messaging
path versus stock Codex's generated `codex-<name>` launcher, Stop waiter, and
durable CLI inbox contract) — do not freeze one transport into the shared
knowledge bootstrap and do not let cosmetic wording drift accumulate between
the files.

## Homunculus agent environment contract

Generated launchers export neutral per-session variables consumed by the
SessionStart hook and by optional peer-bridge registration when policy permits
it. These names are part of the seed contract and should not be renamed
casually:

| Var | Occurrences |
|---|---|
| `HOMUNCULUS_NAME` | **NOT part of the neutral AGENT_* family below** — that family is deliberately unprefixed per the 2026-07-28 ruling (`agent_messaging_plugin/env_contract.py`), which is about not re-prefixing *those five names* with `HOMUNCULUS_`; it says nothing about this separate, pre-existing variable naming which homunculus a shell is scoped to. `launch.template` (daemon launcher — export), `claude_launcher.template`, `codex_launcher.template`, `fleet_functions.zsh.template` (session launchers — export, added 2026-08-02 to close a launcher asymmetry: session-launched shells previously had no way to know their own homunculus's name). Consumed at module-import time by several plugins for scoped vault-key resolution (`agent_messaging_plugin`, `g_suite_plugin`, `signal_plugin`, `soundcloud_artist_studio_plugin`) and required by `quality_gates/run_smokes.py`'s fail-closed check — the Git-Controller gate path — so a session launched without this export cannot run gate scripts unless the caller sets it by hand. ⚠ **Arm all launchers or none**, same reasoning as `GIT_CONTROLLER_NAME` below: a launcher that omits it produces a session class where in-session gate scripts and vault-scoped plugin code fail loud unpredictably depending on which launcher started the shell |
| `AGENT_SESSION_LABEL` | `claude_launcher.template` (export); `codex_launcher.template` (export); `fleet_functions.zsh.template` (export); and the coordination-hooks Claude plugin, where it is **load-bearing at user scope — an unlabeled `claude` session anywhere on the machine must get zero output**. ⚠ Measured via `ls hooks/` + `hooks/hooks.json` (re-derive both, never trust a count in prose — entrypoints, registrations and event types are three different numbers): **four of the five** shipped entrypoints are label-gated — `step_zero_reminder.js`, `check_messages_reminder.js`, `role_binding_reminder.js`, `wake_waiter.js` (which adds a `FLEET_TRANSPORT` gate on top). The fifth, `git_controller_gate.py`, is **label-INDEPENDENT**: it arms on the presence of `GIT_CONTROLLER_NAME` and evaluates wherever armed. Zero-output for unlabeled sessions therefore holds for the gate **by its arming boundary, not by a label guard** — see that variable's row. `claude_settings.json.template` no longer guards anything: it ships no hooks at all (the plugin owns every one — read the current roster from `hooks/hooks.json`, do not restate a count here). The repo-scoped Codex reminders use the same non-empty label guard |
| `AGENT_SESSION_ID` | `claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template` (single-sourced export); consumed by the Stop hook's `<name> wake`, which derives the per-session watch spool path from it — the same derivation `<name> watch` uses, so the pair meets with no flags. `peer_inbox` takes this same value and resolves the caller's registered binding server-side |
| `AGENT_WAKE_CLI` | `claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template` (export). Names the coordination CLI (the `<name>` console script) for the coordination-hooks plugin's Stop-hook waiter, which invokes exactly `$AGENT_WAKE_CLI wake`. The user-scope settings Stop hook does not read it (it execs `<name> wake` directly); the export exists so each plugin's transport-guarded waiter can arm on fleet machines |
| `FLEET_TRANSPORT` | `codex_launcher.template` and `fleet_functions.zsh.template` (export, from the operator knob `<NAME>_FLEET_TRANSPORT`, default `watch`); `rename_skill_SKILL.md.template` (transport-selection read). The Stop-hook guard moved OFF `claude_settings.json.template` under WS-5b-core — that file ships no hooks at all now; the guard lives in the coordination-hooks Claude plugin (`wake_waiter.js`). Values `watch` \| `mcp`; unset resolves to the machine's standing default (`watch` on hydrated seeds, `mcp` on the platform development checkout). Declares the fleet-coordination transport; the rename skill never probes and never silently crosses transports (design: platform repo `workbench/2026-07-28_fleet_transport_parity_design.md`). Stock Codex requires the explicit exported value `watch`, which prevents its watcher and a patched MCP receive path from double-waking one session |
| `AGENT_IDENTITY` | `codex_launcher.template` exports `codex`; the optional Claude MCP reference form lives in the root README, cited by the hydration runbook's mcp-add step |
| `AGENT_ROLE` | `codex_launcher.template` exports the launcher role. The stock-Codex git gate authorizes only from this variable; a controller-looking label or session id cannot grant mutation authority |
| `GIT_CONTROLLER_NAME` | `claude_launcher.template`, `codex_launcher.template`, and `fleet_functions.zsh.template` (export) — all rendered from `{{GIT_CONTROLLER_NAME}}`, or the whole line **deleted** for a solo deployment. **RULED 2026-08-01 (operator), superseding the previous ship-unarmed position:** the **configuring session sets this during hydration**, and its **presence is what arms** the coordination-hooks git-mutation gate. Export surface is the **launcher templates** (Architect ruling): the gate is a **FLEET-CHECKOUT control** — it arms exactly the sessions that operate in the shared checkout, and production user repositories are **out of scope by design** (their git safety belongs to each runner's own permission model). ⚠ **`GIT_CONTROLLER_NAME` is the name the SHIPPED copies read** (`claude_plugin/…`, `codex_plugin/…`); the origin repo's own `.claude/hooks/` copy reads `HOMUNCULUS_GIT_CONTROLLER_NAME` and **never ships** — a deliberate per-copy binding, parameterized and never unified by deletion. Setting the origin name on a seed arms **nothing**, and the gate is **fail-OPEN when unset**, so the wrong name yields a gate the operator believes is armed. ★ **SOLO EXEMPTION — the mechanism is this variable's ABSENCE.** A solo deployment's hydration simply **omits** it: nothing is armed, which is the exemption, with zero runtime machinery. **Do not add a session-count probe to "implement" this** — configuration already implements it, and every runtime session-count source is known to lie (stale stamped ids, phantom arms, label-sweep evictions). A transiently-solo *fleet* is handled by policy language, not by a mechanical check. ⚠ **MEASURED at source in both shipped copies** (`claude_plugin/…` and `codex_plugin/…`: `git_controller_name()` resolves to `os.environ.get(…, "").strip() or None`): an **empty value reads exactly as unset**, so a line accidentally rendered blank is disarmed, not half-armed. *Absence* remains the documented form anyway — presence-with-an-empty-value is not what the exemption says, and no later reader should have to re-derive that equivalence to trust it. ⚠ **Arm all launchers or none.** The gate reads no session label, so a deployment that exports the variable from one launcher and not another gains a session class that escapes the gate purely by which launcher started it |

The whole family is deliberately UNPREFIXED (operator seed-naming ruling
2026-07-28: no `HOMUNCULUS_*` env names in seed-facing artifacts). The
`AGENT_*` five-name family (`AGENT_IDENTITY`, `AGENT_INSTANCE_ID`,
`AGENT_SESSION_LABEL`, `AGENT_SESSION_ID`, `AGENT_ROLE`) is read by shipped
platform code (`agent_messaging_plugin/env_contract.py` is the single source
of truth; `mcp_bridge/__main__.py` and `local_cli` watch/wake read through
it) with no fallback and NO legacy-alias reads: for one release those entry
points fail loudly when a pre-migration `HOMUNCULUS_AGENT_*` name is present
without its neutral replacement. Renaming any member piecemeal in these
templates recreates the 2026-07-25 half-landed-rename incident — code and
templates move together or not at all.

## Invariant

`claude_launcher.template` never carries `--dangerously-skip-permissions`, and
`codex_launcher.template` never carries a dangerous approval, sandbox, hook-
trust, or MCP bypass. The user's native approval flow and explicit `/hooks`
trust review are the client-deployment safety boundary. `CODEX_BIN` may select
an explicit stock executable; hydration never points it at a locally patched
receive build.
