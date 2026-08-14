# Seed update plan — three pending re-mints, incl. the homunculus→solet rename

**Written:** 2026-08-14, before making any mutating change, per the seed
update runbook (`05_seed_update_runbook.md`) and advisor review.

## Trigger

Operator asked to check `https://github.com/dwestgate/2026-07-22_local_branch_bizops_ca9fe1fdc`
(this clone's own `origin`) for updates and apply them.

## State at the start of this update

- Current branch: `seed-feedback-and-gsuite-bind-fix`, HEAD `d9fe791`.
- Last seed merge landed at commit `477619e` (merged seed re-mint `040d7d4`).
- `origin/main` is now at `e592c67`, three "Seed bundle (factory-sealed)"
  commits ahead of `040d7d4`:
  - `a2bc5d6` — 2026-08-12 (creative-corpus removal from thinking KBs)
  - `67a5ddf` — 2026-08-13, tag `release-2026-08-13` — **BREAKING**:
    homunculus→solet rename; seed's home moves to
    `https://github.com/solet-public/macos-bizops` (confirmed reachable via
    `git ls-remote`, HEAD `f299512`); feedback channel moves to GitHub issues.
  - `e592c67` — 2026-08-14, tag `release-2026-08-14` (content hygiene, fleet
    worker reliability, AskUserQuestion default-deny overlay, retrieval gates)
- Diffstat `040d7d4..e592c67`: 823 files changed. No overlap with this
  clone's dirty paths (`.claude`, `workbench`, `client`, `CLAUDE.md` — empty
  diff), so the pre-existing uncommitted changes (`.claude/settings.json` →
  `{}`, BranchMetrics audit script edits, untracked `.mcp.json` and
  `2026-08-12_dax_report_card.md`) cannot conflict with the merge.
- Local commits since `040d7d4`: only 3 workbench-only commits (BranchMetrics
  audit, markdown→Google-Doc converter). No unmerged gsuite-bind-specific
  commits in this window — the gsuite OAuth-bind fix (`60e6af4`) predates
  `040d7d4` and is already upstream.
- This clone currently: `root_manifest.yaml` has `homunculus_name: dax`,
  env has `HOMUNCULUS_NAME=dax`, LaunchAgents `local.homunculus.dax.plist`
  and `local.homunculus.dax.router.plist` (router present —
  `macos_self_deployment_plugin` is in `profile/config/manifest.yaml`).
  `dax` resolves on PATH (`/Users/david.westgate/.local/bin/dax`).

## Planned sequence (per runbook + advisor review)

1. Merge `origin/main` (still reachable at the old URL) into this branch —
   real `git merge`, following the `477619e` precedent, not `pull --ff-only`
   (this branch carries 38 local commits by design). Resolve conflicts,
   dropping local interim fixes upstream has since fixed, same as before.
2. Re-point `origin` to `https://github.com/solet-public/macos-bizops`
   (Step 2a) — AFTER the merge lands, verified with a fetch, old URL kept as
   fallback if the new one becomes unreachable.
3. Run `deployment/scripts/migrate_to_solet.py` (dry run, read the plan,
   then `--apply`) BEFORE any restart — mandatory, boots the LaunchAgents
   out and back under the new `solet` names/labels, replaces the `dax`... er,
   the per-name console script, rewrites `root_manifest.yaml`. Do NOT use
   `apply_manifest`/blue-green for this cutover — the new color would build
   against plists/env still carrying old keys and refuse to boot; the
   migration script owns the restart.
4. Wait for startup quiescence (`wc -l` on the newest log, twice, matching).
5. Re-run Step 5 hydration re-render (CLAUDE.md/AGENTS.md/hooks/launcher) —
   needed regardless, and this is also where the `.claude/settings.json` →
   `{}` question resolves.
6. Step 6 stale-copy refresh: coordination-hooks 0.5.4→0.5.5
   uninstall+reinstall (or leave disabled — operator call), KB re-installs
   for deletion-only changes (`thinking_plans`, `plan_templates`,
   `github_midwife_plugin`), re-arm any watcher, relaunch any open MCP
   bridge client.
7. Verify per Step 7 (health, KB search hits new content, remote -v shows
   new URL, negative search for removed KB content).

## Open questions put to the operator before executing (asked 2026-08-14)

(a) The uncommitted `.claude/settings.json` → `{}` change in the working
tree currently means there is no local Step Zero hook mechanism at all
(coordination-hooks@dax is separately disabled per
`seed-update-status.md` memory) — restore the local hooks block, re-enable
the plugin, or leave both off?
(b) Go ahead with the rename migration on the live, running daemon (LaunchAgent
bounce, console script replacement, root manifest rewrite)?

See conversation for the answer given; update this note if the plan changes.

## Status — as executed, 2026-08-14

Operator answered both open questions: restore the local hooks block (done —
`.claude/settings.json` reverted to HEAD before anything else ran), and
proceed with the full sequence, unattended, plus port the coordination-hooks
changes into `BranchMetrics/bizops-claude-code-plugin` (added as task #8).

1. **Merge** — done, commit `d406d5c`. Conflicts in `root_manifest.yaml` and
   four `agent_messaging_plugin` files, all independent-feature collisions
   (this branch's per-spawn provider overlay vs. upstream's rename / new
   `solet_bin` wake-CLI fix / new `allow_askuserquestion` field) — kept both
   sides in each case, not "ours wins."
2. **Re-point origin** — done. `git@github.com:solet-public/macos-bizops.git`,
   verified reachable and fetchable. Confirmed `e592c67` (old repo) and
   `f299512` (new repo) share an identical tree hash before moving, so no
   content was at risk from re-pointing early.
3. **migrate_to_solet.py --apply** — done. Both LaunchAgents renamed and
   reloaded (`local.solet.dax[.router].plist`), venv console script replaced
   (`solet`, `homunculus` shim removed), `root_manifest.yaml` already carried
   `solet_name: dax` from the merge (no-op guard, as expected).
   **Follow-up fix required and applied:** `~/.local/bin/dax` was a symlink
   to `.venv/bin/homunculus`, which the migration removed — this broke the
   `dax` CLI outright until re-pointed to `.venv/bin/solet` by hand. Not
   mentioned in the runbook (out of scope — that symlink is this operator's
   own PATH entry, not seed-managed); worth remembering for any future
   migration on this machine.
4. **Quiescence wait** — done, log line count stable.
5. **Hydration Step 5 re-render** — done, commit `0eb33ad`: all four
   `client/bin`+`client/dax.zsh` launchers, `CLAUDE.md`/`AGENTS.md` managed
   blocks, the new `claude-session-overlay.json` (AskUserQuestion deny — this
   is also the mechanism the operator asked for directly, "disable the
   radio-button picklist," so it's doing double duty), plus (outside this
   repo) `~/.claude/CLAUDE.md`'s marker section, the `rename` skill
   (gained incumbent-watcher detection for fleet-spawned workers — a real
   behavior change, not just wording), and the `feedback` skill (newly
   installed; `gh` 2.97.0 already present, contradicting the stale
   `seed-update-status.md` memory note that `gh` was missing — that memory
   needs correcting).
6. **Step 6 stale-copy refresh:**
   - KB re-installs: `thinking_plans` and `plan_templates` re-installed
     (chunk_count now 0, matching the 2026-08-12 creative-corpus removal),
     negative search for removed content returns nothing from either KB.
     `github_midwife_plugin` did NOT need a forced reinstall — its restart
     auto-reindex already landed the new feedback-runbook content (verified:
     a search for the old PR-per-round convention surfaces the new
     GitHub-issues runbook, not stale guidance).
   - **coordination-hooks 0.5.4→0.5.5: BLOCKED, not completed.** See
     "Enterprise-policy finding" below — this is the headline item for the
     operator, not a footnote.
   - Residual `homunculus_name` config-store guard: no plugin config carries
     that key (`grep -rl homunculus_name profile/config/` — no hits). Two
     harmless content-only mentions fixed by hand (not seed-managed, gitignored
     runtime config): `profile/config/prompts/system.json`'s system prompt
     text, and one address-book entry description.
   - Noted, NOT a defect: `plugins/macos_self_deployment_plugin/knowledge_base/
     operations_config_reinit_blue_green.md` still literally says "homunculus"
     in its shipped source — confirmed this is upstream's own unrenamed
     content (not a stale local cache), so nothing to reinstall for it.
7. **Verify** — `dax health` healthy throughout (after the symlink fix),
   `git remote -v` shows the new URL for fetch+push, KB searches above
   confirm new content is live.

## Enterprise-policy finding (headline, needs the operator's own follow-up)

Mid-Step-6, `claude plugin uninstall coordination-hooks@dax --scope user`
(meant to force a 0.5.4→0.5.5 cache refresh) succeeded, but the follow-up
`claude plugin install coordination-hooks@dax --scope user` — and even
`claude plugin marketplace add <clone>` — failed:

```
Failed to install plugin "coordination-hooks@dax": Plugin "coordination-hooks"
is from marketplace "dax", which is blocked by your organization's policy

Failed to add marketplace: Marketplace source 'dir:/Users/david.westgate/Workspace/dax'
is blocked by enterprise policy. Allowed sources: github:BranchMetrics/ai_coding,
github:BranchMetrics/bizops-claude-code-plugin, github:anthropics/claude-plugins-official,
github:obra/superpowers-marketplace, github:obra/superpowers,
github:thedotmack/claude-mem@v13.11.0, github:JuliusBrussee/caveman@v1.9.1,
github:mksglu/context-mode@v1.0.169, github:upstash/context7@ctx7@0.5.4
```

This is a BranchMetrics-side Claude Code enterprise policy (server-enforced
allowlist), unrelated to the seed content, and it blocks ANY local-directory
marketplace outright — confirmed it is source-type-specific, not `dax`-
specific, by testing the same uninstall/reinstall cycle against the
GitHub-sourced `bizops-claude-code-plugin` marketplace, which worked fine.

**Regression caused and repaired:** the failed reinstall left
`~/.claude/settings.json`'s `enabledPlugins` missing the
`coordination-hooks@dax` key (only the user-scope declaration; the two
project-local `.claude/settings.local.json` files, for this repo and for
`bizops-knowledge-base`, still declared it and were never touched). Restored
by hand-editing the key back to `true` — the existing `installed_plugins.json`
entries and the 0.5.4 cache bytes on disk were never removed by the failed
attempt, so this is a like-for-like restoration, not a fresh declarative-only
registration. Verified after restoring: `claude plugin list` shows
`coordination-hooks@dax` enabled, and the 0.5.4 cache path
(`~/.claude/plugins/cache/dax/coordination-hooks/0.5.4/hooks/hooks.json`)
exists on disk.

**Net effect: coordination-hooks@dax hooks are running, at 0.5.4** (not
0.5.5). The 0.5.4→0.5.5 delta is a reminder-wording fix plus test legs, per
this release's own notes — low-stakes to be stuck on, but the mechanism to
get unstuck (`claude plugin` install/uninstall against this marketplace) is
now closed on this machine going forward, for every future release, not just
this one. Two ways to reopen it, the operator's call:
(a) ask whoever administers the Claude Code enterprise policy to allowlist
`dir:/Users/david.westgate/Workspace/dax`, or
(b) stop relying on the local-directory marketplace for this hook plugin —
publish coordination-hooks through `BranchMetrics/bizops-claude-code-plugin`
instead (already allowlisted, and task #8 below is doing exactly this for
independent reasons).
Did NOT hand-copy plugin bytes into the cache directory to force a fake
0.5.5 install — that would fabricate `installed_plugins.json`'s
`gitCommitSha` provenance field and circumvent a security control that isn't
this session's call to override.

## Remaining

Task #8: port the coordination-hooks 0.5.5 changes into
`BranchMetrics/bizops-claude-code-plugin` — now more than a nice-to-have,
since it's the operator's actual allowlisted path back to a refreshable
install.
