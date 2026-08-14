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
