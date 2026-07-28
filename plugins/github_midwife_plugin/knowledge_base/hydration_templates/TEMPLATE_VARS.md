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

## File map (template → rendered destination)

| Template | Rendered to | Mode |
|---|---|---|
| `zshrc.template` | `~/.zshrc` (ONLY on accepted offer — see runbook) | 0644 |
| `homunculus.zsh.template` | `<clone>/client/<name>.zsh` | 0644 |
| `claude_launcher.template` | `<clone>/client/bin/claude-<name>` | 0755 |
| `launch.template` | `<clone>/client/bin/launch-<name>` | 0755 |
| `CLAUDE.md.template` | `<clone>/CLAUDE.md` | 0644 |
| `AGENTS.md.template` | `<clone>/AGENTS.md` | 0644 |
| `claude_settings.json.template` | `~/.claude/settings.json` (USER scope, structural merge — operator ruling 2026-07-22: fleet sessions start in other repos, and project-scope hooks never fire there) | 0644 |
| `rename_skill_SKILL.md.template` | `~/.claude/skills/rename/SKILL.md` (USER scope, same ruling — the role-reclaim hook invokes it from any repo) | 0644 |
| `fleet_functions.zsh.template` | `<clone>/client/<name>-fleet.zsh` (ONLY on accepted Step 4a offer — see runbook) | 0644 |

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
path versus the runner-specific mechanics an AGENTS.md-reading agent retrieves
from the available messaging documentation) — if no runner-specific contract
ships, report the gap; do not freeze one transport into the generic bootstrap
and do not let cosmetic wording drift accumulate between the files.

## Homunculus agent environment contract

Generated launchers export neutral per-session variables consumed by the
SessionStart hook and by optional peer-bridge registration when policy permits
it. These names are part of the seed contract and should not be renamed
casually:

| Var | Occurrences |
|---|---|
| `HOMUNCULUS_AGENT_SESSION_LABEL` | `claude_launcher.template` (export); `claude_settings.json.template` (guard on ALL THREE hooks + SessionStart read — load-bearing at user scope: unlabeled sessions must get zero output); `fleet_functions.zsh.template` (export) |
| `HOMUNCULUS_AGENT_SESSION_ID` | `claude_launcher.template` (export); `fleet_functions.zsh.template` (export); consumed (not referenced textually) by the Stop hook's `<name> wake`, which derives the per-session watch spool path from it — the same derivation `<name> watch` uses, so the pair meets with no flags |
| `FLEET_TRANSPORT` | `fleet_functions.zsh.template` (export, from the operator knob `<NAME>_FLEET_TRANSPORT`, default `watch`); `claude_settings.json.template` (Stop-hook guard, `:-watch` default); `rename_skill_SKILL.md.template` (transport-selection read). Values `watch` \| `mcp`; unset resolves to the machine's standing default (`watch` on hydrated seeds, `mcp` on the platform development checkout). Declares the fleet-coordination transport; the rename skill never probes and never silently crosses transports (design: platform repo `workbench/2026-07-28_fleet_transport_parity_design.md`). Deliberately UNPREFIXED (operator seed-naming ruling 2026-07-28: no `HOMUNCULUS_*` env names in seed-facing artifacts) — safe because no shipped Python reads it; its exporter and readers all travel together in this render set |

| `HOMUNCULUS_AGENT_IDENTITY` | none here — lives in the root README's `claude mcp add` reference form, cited by the hydration runbook's mcp-add step |

The `HOMUNCULUS_AGENT_*` names above predate the 2026-07-28 seed-naming
ruling (no `HOMUNCULUS_*` env names in seed-facing artifacts) and are read by
shipped platform code (`mcp_bridge/__main__.py`, `local_cli` watch/wake) with
no fallback, so they can only change together with that code — see the design
record's addendum (platform repo
`workbench/2026-07-28_fleet_transport_parity_design.md` §7) for the ruled
follow-on migration. Do not rename them piecemeal in these templates; a
template-only rename recreates the 2026-07-25 half-landed-rename incident.

## Invariant

`claude_launcher.template` NEVER carries `--dangerously-skip-permissions`.
The user's tool-approval flow IS the client-deployment safety boundary.
Adding a permissions bypass to a generated launcher is a design violation,
not a tuning choice.
