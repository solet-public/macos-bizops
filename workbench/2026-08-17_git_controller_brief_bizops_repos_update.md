# Git-Controller brief — update bizops-knowledge-base + repos folder, 2026-08-17

You are the **Git-Controller** for this deployment (spawned with
`local_name="Git-Controller"`, so the git-controller-gate permits
repository-impacting git from you on your first turn — no rename needed).

Dispatched by: Operator session. Report back to role `Operator`.
Model/effort/lifetime: mechanical lane; finish this brief and stop — you will
be retired after your report is acknowledged.

## Task

Bring the following local clones up to date from their GitHub origins.
**Never** force-push, rebase, reset --hard, stash, clean, or switch/checkout
branches. Fast-forward-only integration everywhere. Any repo that cannot be
fast-forwarded is a REPORT item, not a problem to fix.

Network note: GitHub had a partial outage earlier today (~14:58 UTC). If a
fetch fails with a timeout/5xx/connection-reset, retry ONCE after ~30s; if it
fails again, mark that repo "fetch failed (outage-shaped)" in your report and
move on to the next repo. Do not retry aggressively.

## Repos and per-repo procedure

Primary repo:

1. `/Users/david.westgate/Workspace/bizops-knowledge-base`
   - Currently on feature branch `dax-homunculus-integration` with one
     locally modified tracked file (`.claude/settings.local.json`). That
     modification must survive untouched.
   - `git fetch origin --prune`
   - If the current branch tracks an origin branch: `git pull --ff-only`.
     If it refuses (divergence, or would touch the dirty file), leave
     everything as-is and note it in the report.
   - Also fast-forward the local `main` ref WITHOUT checking it out:
     `git fetch origin main:main`. If that refuses (local main diverged),
     do not force it — report.
   - Report: commits fetched, whether current branch and local main moved,
     and how far the current branch is ahead/behind origin/main
     (`git rev-list --left-right --count main...HEAD` style counts).

Repos under `/Users/david.westgate/Workspace/bizops-knowledge-base/repos/`
(all on their default branch, working trees clean or only untracked files):

2. `bizops-snowflake-code` (main)
3. `bizops-snowflake-prod-share-branch` (main)
4. `data-platform` (master)
5. `dbt-bizops` (main)
6. `devops-center-main` (master)
7. `saas-eks-templates` (main)
8. `salesforce-sync` (master — has untracked `.DS_Store`, `dev/`; leave them)

For each: `cd` into it (do not use `git -C`), `git fetch origin --prune`,
then `git pull --ff-only`. Untracked files are never a stop condition.
Record: old HEAD → new HEAD (or "already up to date"), and any failure
verbatim.

`repos/salesforce-prod/` is NOT a git repo — skip it, note it as skipped.

## Reporting

When all eight are done (or stopped), send ONE consolidated report to role
`Operator`. Preferred channel: the cross-session SendMessage tool addressed
to `Operator`. (The `dax` CLI is likely NOT on your PATH — spawned tmux
workers get a bare PATH; do not install anything to work around that.) If no
messaging channel works, write the report to
`/Users/david.westgate/Workspace/dax/workbench/2026-08-17_git_controller_repos_update_report.md`
and end your turn.

Report format: one line per repo — repo, action taken, old→new HEAD or
"up to date" or the verbatim error. Then stop. Do not start any other work.
