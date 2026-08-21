# Git-Controller brief — DRAFT the PII purge manifest (do NOT execute the purge), 2026-08-17

You are the **Git-Controller** for this deployment. Dispatched by the Operator
session with explicit operator approval ("Yes please proceed", 2026-08-17).
Report back to role `Operator`.

## Mission

Prepare — but do NOT execute — the history-rewrite plan that removes PII from
the shared repo `BranchMetrics/bizops-knowledge-base`. Your deliverable is a
manifest document the operator will review and sign off before anything
irreversible runs.

**Authorized mutations this turn (exactly two, both zero-risk):**
1. In the BACKUP copy only — `/Users/david.westgate/Workspace/bizops-knowledge-base-2026-08-17`
   — run `git remote remove origin` so the backup can never accidentally push.
   Verify afterward with `git remote -v` (must be empty). Touch nothing else
   in the backup.
2. Writing the manifest file (path below).

**Everything in the REAL repo (`/Users/david.westgate/Workspace/bizops-knowledge-base`)
is READ-ONLY for you this turn:** log/show/ls-tree/rev-list/rev-parse/
cat-file/diff/branch only. NO filter-repo, NO rm, NO commit, NO push, NO
checkout, NO fetch. The purge itself happens in a later, separately-approved
turn.

## Background (from today's completed audit)

PII reached origin. The operator's requirements: files must be fully removed
from GitHub and from git history on all refs; the operator's LOCAL copies are
preserved (already done — the backup above, verified byte-identical); nothing
may be lost.

## Purge candidates (verify each; expand for renames)

Category P — purge from all history (confirmed PII):
- users/david.westgate/from_claude/2026-07-09_mass-deleted-contacts-restore-audit.tsv
- users/david.westgate/from_claude/2026-07-09_purged-tasks-worklist.tsv
- users/david.westgate/from_claude/2026-07-06_zuora_account_backfill_touch_list.csv
- users/david.westgate/from_claude/2026-07-09_unrestorable-contacts-duplicate-value.tsv
- users/david.westgate/from_claude/2026-08-07_core_sync_endpoint_payload_multi_pair_account.json
- users/david.westgate/from_claude/2026-08-07_core_sync_endpoint_payload_nextdoor.json
- users/david.westgate/from_claude/2026-08-07_core_sync_endpoint_payload_migrated_account.json
- users/david.westgate/work_items/2026-06-17_core_query_inventory.md  (PII embedded; redacted re-add later)
- users/david.westgate/work_items/2026-06-18_cake_monetization_spec.md (same)
- task_list/Renewal_Summary/*.pdf — ALSO moved later to
  users/david.westgate/task_list/2026-04-17_contract_renewal_brief/ — both
  spellings must be purged (4 PDFs: Ban Ban, Cars24, R2G SOP, Template)

Category P2 — recommend purge (bulk record-level, re-identifiable, no direct PII):
- users/david.westgate/from_claude/2026-07-09_lost-campaign-memberships-worklist.tsv (20.2 MB)
- users/david.westgate/from_claude/2026-07-09_lost-campaign-memberships-sample.tsv
- users/david.westgate/from_claude/2026-07-09_purged-events-worklist.tsv
- users/david.westgate/from_claude/2026-07-09_purged-ocr-worklist.tsv

Category D — operator-decision items (list in manifest with a recommendation,
do not decide unilaterally):
- python_scripts/cake_tools/test_accounts.tsv (real gmail mailboxes; live
  input fixture for cake_tools — purging requires a synthetic replacement)
- users/david.westgate/docs/example_zshrc.txt (real core-Postgres
  username/host/DSN; purge + sanitized re-add)
- python_scripts/*/soql/queue/*.soql and */sql/queue/*.sql (27 files —
  convention violation, queries only, no PII: removal-at-head suffices,
  purge unnecessary — say so)

## Your tasks

1. Neuter the backup remote (authorized mutation #1 above).
2. In the real repo, for EVERY candidate: find every path spelling it has
   ever had (renames/moves), e.g. `git log --all --name-only --format= |
   sort -u | grep <basename fragment>` — filter-repo only removes spellings
   it is given. Record which refs (origin/* and local) can reach each file.
3. Capture the blob SHAs of each bad file at each spelling
   (`git rev-parse <ref>:<path>`) — these go in the manifest as the
   post-purge verification checklist ("this SHA must be unreachable from
   every ref afterward").
4. Check whether `git-filter-repo` is installed (`which git-filter-repo`,
   `brew list git-filter-repo` — read-only checks); note install step if
   absent.
5. Draft the manifest to
   `/Users/david.westgate/Workspace/dax/workbench/2026-08-17_bizops_pii_purge_manifest.md`
   with these sections:
   a. Path list (verified spellings per file, refs reachable, blob SHAs).
   b. Exact `git filter-repo --invert-paths` invocation(s), run in the
      WORKING clone so all 78 local branches are rewritten together
      (note the `--force` requirement on a non-fresh clone and why it is
      acceptable here: the dated backup is the safety net).
   c. Pre-flight checklist: backup verified (done, 163,373 files matched),
      no other live session, dirty file `.claude/settings.local.json`
      handling (filter-repo requires a clean tree — plan: stash is BANNED,
      so temporarily move the file aside and restore after; spell this out).
   d. Force-push plan: `git push --force --all origin` + `--tags`, plus
      GitHub branch-protection note if pushes are rejected.
   e. GitHub Support step: request cached-blob/PR-cache purge; list the
      blob SHAs to cite.
   f. Teammate instructions: delete old clones entirely, fresh re-clone
      (a pull is NOT sufficient) — plain-language, they are not git experts.
   g. Post-purge verification: fresh clone, `git rev-list --all --objects |
      grep <sha>` for every recorded blob SHA (must be empty), re-run of the
      path scan.
   h. The .gitignore overhaul to commit immediately after the rewrite
      (draft the full file content): blanket *.tsv/*.csv/*.xlsx/*.jsonl/
      *.parquet with explicit `!` allowlist; users/*/from_claude/ data
      extensions ignored (.md/.py stay tracked); staging ignores generalized
      to users/*/; **/results/, **/output/, **/exports/; soql/sql queue
      dirs; move /repos/ from .git/info/exclude into .gitignore.
   i. Category D decision items with your recommendation each.
6. Send a short completion report to role `Operator` (SendMessage; fallback:
   append a REPORT section to the manifest file): manifest path, headline
   numbers (files, spellings, refs, SHAs recorded), anything surprising
   found during verification (e.g. additional data files a rename scan
   surfaces), and explicit confirmation that the real repo was not mutated.

Do not start any other work. The purge execution will be a separate brief
after operator sign-off.
