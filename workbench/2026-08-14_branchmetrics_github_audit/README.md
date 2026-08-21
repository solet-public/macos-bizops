# BranchMetrics GitHub Repo Compliance Audit

Regenerates the report at https://docs.google.com/spreadsheets/d/1HnRbBxV8Cc3k0btEXi_JTba042pZSQtj6-bluzLGm7Y/edit
(or a fresh copy of it) against the requirements in
[BranchMetrics' GitHub repository setup spec](https://app.notion.com/p/brancheng/Github-repository-setup-8d62d9d4ffd14e47b27a649baec88d63).

Trigger phrase: "I want to create a report on our GitHub repositories" (or
close variants) means re-run this. See the matching project memory in
`~/.claude/projects/-Users-david-westgate-Workspace-dax/memory/`.

## Prerequisites (one-time, or when the token expires)

1. `gh` installed (`brew install gh`) and authenticated:
   - A classic PAT with `repo` + `read:org` scopes.
   - **SSO authorization is required separately from scopes** — BranchMetrics
     enforces SAML SSO, so even a correctly-scoped token is blocked until you
     visit https://github.com/settings/tokens, click "Configure SSO" next to
     the token, and authorize it for the BranchMetrics org. Without this step
     every API call 403s with a SAML-enforcement message, not a scope error.
   - Vaulted copy: `dax.default_address_book_plugin.github_api_token` (address-book
     entry `github_api`). This is a *write-only* vault entry by platform design —
     it cannot be read back out to feed `gh auth login` on a fresh session. If
     `gh auth status` shows logged out, generate a fresh token (or re-authorize
     the existing classic one, whose value doesn't change) and re-run
     `gh auth login --with-token` yourself; there's no way to script around this.
2. dax's `g_suite_plugin` connected (`dax call
   service_interface::lifecycle_management_service::list_plugins` should show
   it `is_running: true`; confirmed working as of 2026-08-14).

## Running it

```sh
./run_all.sh
```

Or step by step (useful for re-running just one stage after a fix):

```sh
python3 discover_repos.py > run_2026-08-14/repos.json
python3 audit_github.py run_2026-08-14/repos.json run_2026-08-14
python3 analyze.py run_2026-08-14
python3 generate_csvs.py run_2026-08-14
python3 create_sheet.py run_2026-08-14 2026-08-14
```

## What "repos we own" means here (2026-08-14 decision, may need re-confirming)

Three criteria, OR'd together, restricted to non-archived repos:
1. Non-archived repos where GitHub team **`business-solutions`** (BranchMetrics'
   internal name for "bizops") holds an explicit team grant.
2. Non-archived repos with `bizops` in the name.
3. A short manually-confirmed addendum list in `discover_repos.py`
   (`EXTRA_INCLUDES`/`EXTRA_EXCLUDES`) for repos that don't fit either
   pattern but the operator confirmed do/don't belong. **Re-confirm this list
   with the operator on each re-run** — there's no reliable API signal for
   "this repo is ours" beyond (1) and (2), so it will not self-update.

`business-solutions` team membership was confirmed 2026-08-14 to include the
people the operator named (Sushant Yadav, Jitender Yadav, Hasan Caliskan,
David Westgate) — if the team's membership or purpose changes, re-verify this
assumption before trusting a re-run's repo list.

## `business-solutions` keeps Admin, not Maintain (2026-08-14 operator ruling)

The doc's Repository Permissions model wants the owning team at **Maintain**
and an org-admin team (`saas-gh-admins`/`disco-gh-admins`/`infra-gh-admins`)
holding sole **Admin**. The operator (David Westgate, a `business-solutions`
owner) explicitly declined this for bizops' own repos: business-solutions is
not relinquishing Admin/ownership on its repos to an org-admin team. This is
a deliberate stance, not a compliance gap — `analyze.py`'s
`owning_team_ok` check accepts either `maintain` or `admin` for
`business-solutions` and only flags a real gap if the team holds neither
(i.e. is missing owning-team-level access entirely). Scoped to
business-solutions' own repos specifically — this isn't a read that the doc's
Maintain/Admin separation is wrong for every team, just that bizops isn't
adopting it for itself.

## Known platform gap (reported upstream, fix said to be imminent)

Every `g_suite_plugin` verb (as of 2026-08) dispatches as an async job — the
call returns `{job_id, status: "queued"}` immediately, and there's no
plugin-specific verb to retrieve the result from a `dax call` CLI session
(as opposed to a live listening flow, which gets the result pushed to it
automatically). `create_sheet.py` works around this via the generic
`service_interface::state_service::read_state` primitive against the
`core.job_payload` table, polling for a `result`/`error` row keyed by
`job_id`. If a future dax release exposes a real job-status verb for
g_suite, switch to that instead — check
`plugins/g_suite_plugin/knowledge_base/processes/` for one first.

## Known report limitations

- Branch protection and the collaborator list both require **admin**-level
  GitHub access to read; a 403 on either is recorded as "Unassessable", not
  coerced into a false "No". Check the `Confidence` column in the Summary
  tab and the `Notes` column in Detail before trusting a "No" as a real gap.
- The doc's "auto-delete branches, except the dashboard team" exception is
  not evaluated automatically — there's no clean API signal mapping a team
  to "the dashboard team". Check by hand if a dashboard-owned repo is ever
  in scope.
- The Bot Permissions section of the doc (branchlet, sa-sdk-branch, etc.) has
  no settled rule to check against in the source doc itself (every line ends
  in "?") — nothing to grade there yet.
