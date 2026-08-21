# Repos Refresh Report — `~/Workspace/bizops-knowledge-base/repos`

**Date:** 2026-08-21
**Brief:** `workbench/2026-08-21_git_controller_brief_repos_refresh.md`
**Executed by:** the Operator session, under the solo-fleet Git-Controller exemption
**Supports:** BIZOPS-5386 Looker→Snowflake model mapping

## Authorization basis (cited in band, as policy requires)

1. **Operator instruction, in band, 2026-08-21:** *"be sure to update our repos in the
   repos folder - we don't want to make assumptions based on out-of-date information."*
2. **Fleet verified solo:** `plugin::agent_messaging_plugin::peer_list` at
   2026-08-21T17:48:21Z returned exactly one live session — `session_label: "Operator"`,
   `agi-watch-e9dcd1b41c9d6f7cacf67b47`. No other session could collide.

## Why this did not run as a spawned Git-Controller

A real `Git-Controller` spawn was attempted first and **refused by the platform**, not
skipped for convenience:

- `spawn_session(work_class="mechanical")` → `unknown_work_class` (valid values are
  `analysis_deliverable`, `production_mutation`, `read_only`). Re-issued as
  `production_mutation`.
- `spawn_session(..., host="tmux")` → **`host_cannot_spawn`**: the managed Claude Code
  policy at `~/.claude/remote-settings.json` lists `hooks` in
  `strictPluginOnlyCustomization`, which strips hooks from the `--settings` blob the tmux
  driver injects the worker's hooks through. A worker spawned there would answer turns
  running **none** of its hooks — never registering, never heartbeating, never capturing
  its session mapping. Delivering worker hooks as plugin hooks is the capability fix and
  is not yet shipped.

`degraded_hooks_acknowledged` would have forced it through. I did not use it: that refusal
is a deliberate upstream guard (the tmux hookless-spawn refusal, issue #8), and an
unmonitorable worker with no `dax` on its PATH could not have reported back anyway. The
documented solo-fleet fallback was the cheaper and safer path.

Mechanism used: patched `~/.claude/sessions/65874.json`'s `name` field
`Operator` → `Git-Controller` (atomic `tempfile` + `os.replace`), ran the fetch/pull work,
and restored `name` → `Operator` via a shell `EXIT` trap in the same call, so the claim
could not outlive the work even on failure. Restore confirmed in output
(`RESTORED name -> Operator`).

## Result

| Repo | Branch | Old HEAD | New HEAD | Outcome |
|---|---|---|---|---|
| `dbt-bizops` | main | `76a9c735` | `45aaefb0` | **fast-forwarded, 20+ commits** |
| `bizops-snowflake-code` | main | `d62b667` | `ed7eb84` | **fast-forwarded, 16 commits** |
| `data-platform` | master | `4965020929` | `98b93dfe8e` | **fast-forwarded, 20+ commits** |
| `devops-center-main` | master | `1f6c35a6` | `88c11da9` | **fast-forwarded, 20+ commits** |
| `saas-eks-templates` | main | `9dc628c9` | `cc8ec147` | **fast-forwarded, 20+ commits** |
| `bizops-snowflake-prod-share-branch` | main | `1c5cb28` | `1c5cb28` | already current |
| `salesforce-sync` | master | `b1848c5` | `b1848c5` | **fetch only — dirty tree, per brief** |
| `salesforce-prod` | — | — | — | skipped: not a git checkout |

No commits, pushes, merges, rebases, stashes, or branch changes. Nothing outside the
declared surface was touched. `~/Workspace/dax` was not touched.

## Notes worth carrying forward

- **`salesforce-sync` was left alone deliberately.** Its 10 "dirty" files are all
  untracked junk — nine `.DS_Store` files and a `dev/` directory — so a pull would very
  likely have been safe. The brief said fetch-only for a dirty tree and I honoured that
  rather than exercising judgment on the operator's working tree. It is now **11 commits
  behind**, including `BIZOPS-5064: Fix checkpoint sync outage — Bulk API doesn't support
  aggregate queries`, which is directly relevant to the Salesforce→SnowPark sync
  remediation thread. **Recommend: clean the `.DS_Store` files and fast-forward it.**
- **`dbt-bizops` was materially stale** and this mattered for BIZOPS-5386: the pulled
  commits include `BIZOPS-5304: remove Google Ads and Google Analytics models` plus
  retirement of the Fivetran `Amendment__c`, `ContractLineItem` v1 and contract-SL-clicks
  snapshot chains. Analysis run against the old checkout would have mapped dashboards to
  models that no longer exist.
- `dbt/target/manifest.json` is present in `dbt-bizops` and compiled against the
  `DBT_DAVID` dev target, so relations in it read `ANALYTICS.DBT_DAVID.*`. Model **names**
  are still authoritative for lineage; the schema is not.
- The Looker LookML repos (`gtm-snowflake-looker`, `customer_success_looker`,
  `dqa-looker`, `marketing-looker`) are **not** in this repos folder. For BIZOPS-5386 they
  were fetched fresh from GitHub HEAD as tarballs via `gh api .../tarball` (no git
  mutation, no gate involvement). If Looker mapping becomes recurring work, adding
  `gtm-snowflake-looker` to this folder would be worthwhile.
