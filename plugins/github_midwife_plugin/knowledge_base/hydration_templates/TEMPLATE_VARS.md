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
| `claude_settings.json.template` | `~/.claude/settings.json` (USER scope, structural merge — operator ruling 2026-07-22: fleet sessions start in other repos, and project-scope hooks never fire there) | 0644 |
| `rename_skill_SKILL.md.template` | `~/.claude/skills/rename/SKILL.md` (USER scope, same ruling — the role-reclaim hook invokes it from any repo) | 0644 |
| `fleet_functions.zsh.template` | `<clone>/client/<name>-fleet.zsh` (ONLY on accepted Step 4a offer — see runbook) | 0644 |

This directory stays **FLAT** — the KB manifest's single exclude pattern
(`hydration_templates/*`) relies on it (`Path.match` has no recursive `**`).

## Homunculus agent environment contract

Generated launchers export neutral per-session variables consumed by the
SessionStart hook and by optional peer-bridge registration when policy permits
it. These names are part of the seed contract and should not be renamed
casually:

| Var | Occurrences |
|---|---|
| `HOMUNCULUS_AGENT_SESSION_LABEL` | `claude_launcher.template` (export); `claude_settings.json.template` (guard on BOTH hooks + SessionStart read — load-bearing at user scope: unlabeled sessions must get zero output); `fleet_functions.zsh.template` (export) |
| `HOMUNCULUS_AGENT_SESSION_ID` | `claude_launcher.template` (export); `fleet_functions.zsh.template` (export) |
| `HOMUNCULUS_AGENT_IDENTITY` | none here — lives in the root README's `claude mcp add` reference form, cited by the hydration runbook's mcp-add step |

## Invariant

`claude_launcher.template` NEVER carries `--dangerously-skip-permissions`.
The user's tool-approval flow IS the client-deployment safety boundary.
Adding a permissions bypass to a generated launcher is a design violation,
not a tuning choice.
