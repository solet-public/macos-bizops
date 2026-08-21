# Git-Controller Brief — Refresh `~/Workspace/bizops-knowledge-base/repos`

**Date:** 2026-08-21
**Dispatched by:** Operator session (dax driving session)
**Lane:** `lane-repos-refresh-20260821`
**Budget line:** `bizops-5386-looker-snowflake-mapping`
**Work class:** mechanical (fetch/pull only — no commits, no pushes, no merges of local work)

## Why

Operator instruction, in band, 2026-08-21: *"be sure to update our repos in the
repos folder - we don't want to make assumptions based on out-of-date
information."*

This supports BIZOPS-5386 (mapping Lauren Michael's Looker dashboards to the
Snowflake models behind them). The Operator session is reading `dbt-bizops`,
`bizops-snowflake-prod-share-branch`, `bizops-snowflake-code` and
`data-platform` as evidence for that mapping, so stale checkouts would produce
a wrong answer.

## Authorization basis

- `plugin::agent_messaging_plugin::peer_list` run at 2026-08-21T17:48:21Z shows
  exactly one live session (`session_label: "Operator"`,
  `agi-watch-e9dcd1b41c9d6f7cacf67b47`). Fleet is solo apart from this spawn.
- The Operator session did **not** route around the git gate; this brief exists
  so a real `Git-Controller` does the mutation, per the standing policy.

## Declared file surface

`/Users/david.westgate/Workspace/bizops-knowledge-base/repos/` only. Do not
touch any other path. Do not `git add`, `commit`, `push`, or `stash` anything.

## Observed pre-state (read-only, captured by Operator 2026-08-21)

| Repo | HEAD | Last commit date | Dirty files |
|---|---|---|---|
| `bizops-snowflake-code` | `d62b667` | 2026-08-17 | 0 |
| `bizops-snowflake-prod-share-branch` | `1c5cb28` | 2026-08-05 | 0 |
| `data-platform` | `4965020929` | 2026-08-17 | 0 |
| `dbt-bizops` | `76a9c735` | 2026-08-17 | 0 |
| `devops-center-main` | `1f6c35a6` | 2026-08-17 | 0 |
| `saas-eks-templates` | `9dc628c9` | 2026-08-17 | 0 |
| `salesforce-sync` | `b1848c5` | 2026-08-10 | **10** |
| `salesforce-prod` | — | — | **not a git repo** |

## Task

For each git repo under the declared surface:

1. `git fetch --all --prune`
2. If the working tree is **clean**: fast-forward the current branch
   (`git pull --ff-only`). If a fast-forward is not possible (diverged local
   commits), **stop on that repo**, record it, and move on — do not merge or
   rebase.
3. If the working tree is **dirty** (`salesforce-sync`, 10 files): do **not**
   pull. `git fetch` only, then record what the local modifications are
   (`git status --porcelain` + `git diff --stat`) and how far behind the branch
   now is (`git log --oneline HEAD..@{u}`). Leave the dirty files exactly as
   they are — they may be the operator's in-progress work.
4. `salesforce-prod` is not a git checkout — skip it, note it, do not
   initialize anything.

Priority order (the Operator session is blocked on the first two):
`dbt-bizops`, `bizops-snowflake-prod-share-branch`, `bizops-snowflake-code`,
`data-platform`, then the rest.

## Report contract

Write `workbench/2026-08-21_repos_refresh_report.md` in
`/Users/david.westgate/Workspace/dax/` containing, per repo: old HEAD → new
HEAD, commits pulled (count + one-line subjects, capped at 20 per repo),
skipped-and-why, and anything that looked unsafe. Derive the file list from
`git status` over the declared surface. Then `peer_send_by_name` a one-paragraph
summary to `Operator`.

**Report by:** 900 seconds from spawn.

## Explicit non-goals

- No commits, pushes, tags, branch creation, or deletion.
- No resolution of the `salesforce-sync` dirty state — report it, don't fix it.
- No touching `~/Workspace/dax` itself (that repo is mid-work on branch
  `seed-feedback-and-gsuite-bind-fix`).
