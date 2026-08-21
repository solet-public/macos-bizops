# PII purge manifest — BranchMetrics/bizops-knowledge-base

> **🛑 LOCAL-ONLY — NEVER COMMIT OR PUSH THIS FILE.** It contains
> customer-identifying filenames. The dax repo's origin is a `solet-public`
> org; this file must never reach it (or any remote). Delete after the purge
> is executed and verified.

**Status: DRAFT — awaiting operator sign-off. Nothing below has been
executed against the real repo or GitHub.**

Prepared 2026-08-17 by the Operator session from the completed PII audit,
with rename-scan, blob-SHA capture, and per-ref verification run read-only
against the live clone. Operator approvals on file: "Yes please proceed"
(manifest preparation); purge execution itself still requires sign-off on
this document.

Invariants this plan honors:
1. Nothing is ever lost locally — the dated backup
   `~/Workspace/bizops-knowledge-base-2026-08-17` (APFS clone, verified
   byte-identical, 163,373 files, origin remote removed) retains everything.
2. After execution, the purged content exists ONLY on the operator's machine.
3. Fast-forward-only rules are suspended solely for the force-pushes named in
   §6, each individually listed, on operator sign-off of this manifest.

---

## 1. What gets purged (path spellings — filter-repo needs every one)

### Category P — confirmed PII, purge (no decision needed)

From `origin/main` and/or `origin/dax-homunculus-integration`:

```
users/david.westgate/from_claude/2026-07-09_mass-deleted-contacts-restore-audit.tsv
users/david.westgate/from_claude/2026-07-09_purged-tasks-worklist.tsv
users/david.westgate/from_claude/2026-07-06_zuora_account_backfill_touch_list.csv
users/david.westgate/from_claude/2026-07-09_unrestorable-contacts-duplicate-value.tsv
users/david.westgate/from_claude/2026-08-07_core_sync_endpoint_payload_multi_pair_account.json
users/david.westgate/from_claude/2026-08-07_core_sync_endpoint_payload_nextdoor.json
users/david.westgate/from_claude/2026-08-07_core_sync_endpoint_payload_migrated_account.json
users/david.westgate/work_items/2026-06-17_core_query_inventory.md
users/david.westgate/work_items/2026-06-18_cake_monetization_spec.md
```

Renewal PDFs — **three directory generations plus one standalone spelling**
(the rename scan found one generation more than the audit listed; all four
spellings must be given to filter-repo):

```
task_list/R2G SOP V1. .pdf
task_list/Renewal_Summary/R2G SOP V1. .pdf
task_list/Renewal_Summary/Renewal Kick-Off - Ban Ban (0013Z00001jyVumQAE).pdf
task_list/Renewal_Summary/Renewal Kick-Off - Cars24 (001f100001VSsCyAAL).pdf
task_list/Renewal_Summary/Renewal Kick-Off - Template.pdf
users/david.westgate/task_list/Renewal_Summary/R2G SOP V1. .pdf
users/david.westgate/task_list/Renewal_Summary/Renewal Kick-Off - Ban Ban (0013Z00001jyVumQAE).pdf
users/david.westgate/task_list/Renewal_Summary/Renewal Kick-Off - Cars24 (001f100001VSsCyAAL).pdf
users/david.westgate/task_list/Renewal_Summary/Renewal Kick-Off - Template.pdf
users/david.westgate/task_list/2026-04-17_contract_renewal_brief/R2G SOP V1. .pdf
users/david.westgate/task_list/2026-04-17_contract_renewal_brief/Renewal Kick-Off - Ban Ban (0013Z00001jyVumQAE).pdf
users/david.westgate/task_list/2026-04-17_contract_renewal_brief/Renewal Kick-Off - Cars24 (001f100001VSsCyAAL).pdf
users/david.westgate/task_list/2026-04-17_contract_renewal_brief/Renewal Kick-Off - Template.pdf
```

### Category P2 — bulk record-level data, no direct PII, purge recommended
(uniformity: no data files in `from_claude/`/`work_items/` history at all)

```
users/david.westgate/from_claude/2026-07-09_lost-campaign-memberships-worklist.tsv   (20.2 MB)
users/david.westgate/from_claude/2026-07-09_lost-campaign-memberships-sample.tsv
users/david.westgate/from_claude/2026-07-09_purged-events-worklist.tsv
users/david.westgate/from_claude/2026-07-09_purged-ocr-worklist.tsv
users/david.westgate/from_claude/2026-08-03_marketo_lead_field_descriptors_872.tsv   (schema only)
users/david.westgate/work_items/2026-07-10_prod_share_branch_model_inventory.tsv     (schema only)
```

### Category D — RESOLVED by operator rulings (2026-08-17)

| Item | Ruling |
|---|---|
| `python_scripts/cake_tools/test_accounts.tsv` | **PURGE, no re-add.** The data is retrieved fresh from Salesforce on every report run — it never needed to live in the repo. Follow-up (§8): `cake_tools/cake_monetization_report.py` reads this file (`_load_test_accounts`); rework it to pull the list fresh (or write to a gitignored runtime path) in the same change. |
| `users/david.westgate/docs/example_zshrc.txt` | **PURGE, no re-add.** Not needed — that setup is handled by Dax now. |
| `users/david.westgate/docs/*.pdf` + architecture PDF | **KEEP.** Source documents already/soon converted into KB markdown; nothing sensitive per operator. |
| 27 × `python_scripts/*/{soql,sql}/queue/*.{soql,sql}` | **KEEP.** Executed SOQL/SQL commands are retained deliberately for future mining. Only RESULTS are banned from git — covered by the extension rules in §8. |

**Paths file is final as-is:** the scratchpad `purge_paths.txt` (30 paths)
already equals P + P2 + the two purge-ruled items. No regeneration needed.

## 2. Verification baseline (captured pre-purge, read-only)

**28 blob SHAs** — every historical version of every candidate. Post-purge,
every one must be unreachable from every ref. (The renewal-PDF blobs are
identical across their three directory generations, so they appear once.)

```
02aa5e181367d1b403488dea5460a04f29a9fea8  from_claude/..._payload_multi_pair_account.json (v1)
a66efed3dc21cddcd36c4d09fab2bd2adc866ffb  from_claude/..._payload_multi_pair_account.json (v2)
2fa6e324c433e31f0c2397336727c8c2173f784c  from_claude/..._payload_nextdoor.json (v1)
eb63123efc938384efd23a36b8176ee1180caa17  from_claude/..._payload_nextdoor.json (v2)
2d189fb2d88199de3b029cf5bd29cd72fc9331af  from_claude/..._payload_migrated_account.json (v1)
ac99e4e2a789a8f13aa569a6d9585cad5c72a0e7  from_claude/..._payload_migrated_account.json (v2)
f83505c5848d2b75e7bae9ec2cb06411b91e9a3b  from_claude/2026-07-09_mass-deleted-contacts-restore-audit.tsv
5a3204707dd95d6e3bd18f815971ad746bb1b6d9  from_claude/2026-07-09_purged-tasks-worklist.tsv
670d9c7ec9b98df3a5d19eaefe8f3da3b965c81b  from_claude/2026-07-06_zuora_account_backfill_touch_list.csv
29538ab72ed83d5a4f39e18fa303f42a1dfc97b6  from_claude/2026-07-09_unrestorable-contacts-duplicate-value.tsv
c86b197040578aa06bfefc92ae0408372479bca4  from_claude/2026-07-09_lost-campaign-memberships-worklist.tsv
24dbf47294f361da7daf1f9c19277446fce1d034  from_claude/2026-07-09_lost-campaign-memberships-sample.tsv
9591a7ebbf3ebcf83420b945c36079dbc1bd19fc  from_claude/2026-07-09_purged-events-worklist.tsv
6aa50a8be9d4d15a19911bf52b6c805d8bbee2e4  from_claude/2026-07-09_purged-ocr-worklist.tsv
6716d5566dcadce7bb1c9119a519c12991af4ac1  from_claude/2026-08-03_marketo_lead_field_descriptors_872.tsv
ceef7633fead40029c7c8c3773607015d77de1a3  work_items/2026-06-17_core_query_inventory.md
71133758d54812f344aa9e80f8f4423362af6f93  work_items/2026-06-18_cake_monetization_spec.md (v1)
7d96f3626ddc4d32b71c5e9af135ea0647a61985  work_items/2026-06-18_cake_monetization_spec.md (v2)
7e1b66f51921b5d302948cb63a33e58bf0fa1440  work_items/2026-06-18_cake_monetization_spec.md (v3)
bbfb34352ac1fe26b1ab27cfb82a0f13069ed375  work_items/2026-06-18_cake_monetization_spec.md (v4)
dde8bf123776cbd89b8c9fd712de058b9ca943f7  work_items/2026-06-18_cake_monetization_spec.md (v5)
c62ea7150642ea3b36a4a7fffe71ba6b6f65bc67  work_items/2026-07-10_prod_share_branch_model_inventory.tsv
e8b683f65a3e15d4814628f8820c1ad6997fa0aa  R2G SOP V1. .pdf
3ecac9c28ce4eab8ed42b20cd11bf7eeaa168cc5  Renewal Kick-Off - Ban Ban.pdf
386654a4c9873caffc4ec8ba84f13a7a55ec77f4  Renewal Kick-Off - Cars24.pdf
3462c06e6fd9e112d772877442a20690b1245381  Renewal Kick-Off - Template.pdf
102f1ad82aa932ef17799e839b82fb74b76dedfc  docs/example_zshrc.txt (ruled: purge, no re-add)
a167a7bf77b37cd95626dff53ba72b3c8f349b04  cake_tools/test_accounts.tsv (ruled: purge, no re-add)
```

**Origin refs carrying candidates at HEAD** (12 of 21; history reachability
is broader — commit `7dbe3ce` with the PDFs is an ancestor of all 21):

```
origin/main: 13                                    origin/dax-homunculus-integration: 17
origin/kb-snowpark-snapshot-pitfalls-and-lead-deletion-bug: 13
origin/kb/sfdc-143-entitled-branch-account-permitted-usage: 13
origin/kb/sfdc-143-psa-professional-services-automation: 13
origin/kb/sfdc-143-reseller-management-system: 4
origin/feature/kb-session-20260701-gong-retention-stages: 5
origin/cake-monetization-{invitations-chart,net-collected,revision-3}: 4 each
origin/consolidated-work-2026-06-17-through-2026-06-24: 4
origin/feature/kb-overhaul-renewal-tools-app-import: 4 (incl. renewal PDFs at HEAD)
```

## 3. Pre-flight checklist

- [x] Full local backup exists and is verified (163,373 files matched);
      its origin remote removed 2026-08-17 18:54Z (Operator session under the
      gate's solo-fleet exemption — basis recorded in band: peer_list
      self-only at 18:53:53Z, tmux server down, operator "proceed" on file).
- [x] `git-filter-repo` installed (2026-08-17, `/opt/homebrew/bin/`).
- [x] Operator ruled on all Category D items (2026-08-17); paths file final
      unchanged (30 paths). Durable copies:
      `2026-08-17_bizops_purge_paths.txt` / `_purge_blobs.txt` (this dir).
- [x] Working-tree copies preserved to `~/BizOpsData/` (17 files, 37 MB,
      paths mirrored) — the go-forward working location; dated backup holds
      everything else (incl. the renewal PDFs absent from the working tree).
- [x] PR inventory done: 5 KB-article PRs total, nothing to preserve;
      open-PR branches survive the mirror push.
- [x] Admin confirmed (`dwestgate`, admin: true, repo private).
- [ ] `gh auth refresh -h github.com -s delete_repo` run by operator
      (token currently lacks the `delete_repo` scope).
- [x] Execution mode decided (operator, 2026-08-17): NO spawned session, NO
      daemon bounce — the Operator session executes directly under the
      gate's solo-fleet exemption (verify peer_list self-only + no tmux
      immediately before; patch own session name to Git-Controller for the
      duration; restore after). Spawning a dedicated Git-Controller is an
      acceptable alternative if the fleet is no longer solo at execution
      time.
- [ ] No other live session (peer_list + tmux check immediately before).
- [ ] Teammates notified: hands off the repo until the all-clear.
- [ ] Dirty file `.claude/settings.local.json`: `mv` aside pre-rewrite,
      `mv` back after (stash is banned by controller policy).

## 4. Rewrite — origin's exact refs, via a mirror clone

Rationale: a `--mirror` clone contains exactly origin's 21 branches + tags,
so the rewritten push is exact — no risk of accidentally publishing the 57
local-only branches, no manual branch bookkeeping.

```bash
cd /tmp/purge-work
git clone --mirror git@github.com:BranchMetrics/bizops-knowledge-base.git bkb-mirror.git
cd bkb-mirror.git
git filter-repo --invert-paths --paths-from-file <regenerated purge_paths.txt>
# (mirror clones are "fresh"; --force not needed here)
```

filter-repo notes: it processes all refs; it deletes the origin remote on
completion by design (re-add in §6); commit IDs change for every commit that
touched any candidate, and descendants thereof.

## 5. Rewrite — the operator's working clone, in place

Same filter, run in `~/Workspace/bizops-knowledge-base` so all 78 local
branches are rewritten too. **Why local must also be rewritten:** any later
push from an un-rewritten local branch would re-upload the purged blobs.
The dated backup is the retention point ("only on my machine" lives there
and in `~/BizOpsData/`).

```bash
cd ~/Workspace/bizops-knowledge-base
mv .claude/settings.local.json /tmp/purge-work/settings.local.json.keep
git filter-repo --invert-paths --paths-from-file <same paths file> --force
# --force required: not a fresh clone. Acceptable: dated backup is the net.
mv /tmp/purge-work/settings.local.json.keep .claude/settings.local.json
```

After this, working-tree copies of purged files are gone from the clone
(present in backup + ~/BizOpsData). filter-repo strips the origin remote
here too.

## 6. Force-push (the only non-fast-forward operations, each authorized by
sign-off of this manifest)

```bash
cd /tmp/purge-work/bkb-mirror.git
git push --mirror git@github.com:BranchMetrics/bizops-knowledge-base.git
```

- `--mirror` push force-updates exactly the 21 branches + tags and prunes
  nothing unexpected (the mirror holds precisely origin's refs).
- If branch protection rejects the push on `main`: temporarily lift it
  (repo Settings → Branches, or `gh api`), push, then REINSTATE it as
  part of §8's hardening (protection becomes permanent policy).
- Then re-wire the working clone:
  `git remote add origin git@github.com:BranchMetrics/bizops-knowledge-base.git
   && git fetch origin` and confirm every `origin/*` ref equals its local
  rewritten counterpart (`git rev-parse <ref>` pairs).

## 7. GitHub-side cleanup (the part a force-push cannot do)

Why this section exists: a force-push only moves branch pointers. GitHub
keeps the old commits/blobs on its servers — still fetchable by SHA, still
rendered at old `github.com/.../blob/<sha>/...` URLs, still pinned by any
PR refs — until GitHub itself removes them. Two ways to get that removal:

**Option A — SELECTED by operator 2026-08-17 (Option B rejected the same
day: the repo is NOT to be deleted). GitHub Support request** (GitHub's
documented procedure for sensitive-data removal; keeps the repo,
PRs/issues/settings):
1. Request removal of cached views and unreachable objects for
   `BranchMetrics/bizops-knowledge-base`, citing the 28 blob SHAs in §2.
2. Evidence check before/after: `gh api
   repos/BranchMetrics/bizops-knowledge-base/git/blobs/<sha>` — 200 before
   Support acts, 404 after. Record both.
3. Ask Support about (or close/delete) any PRs from the §3 inventory whose
   refs pin the old history.

**Option B — REJECTED by operator 2026-08-17 — do not delete the repo.**
(Kept for the record: delete + re-create under the same name would flush
server-side objects immediately, but the operator ruled the repo is not to
be deleted. The `delete_repo` scope refresh is therefore NOT needed.)
Pre-checks that remain useful: operator account `dwestgate` has admin; PR
inventory = 5 KB-article PRs (2 closed, 3 open whose branches survive the
force-push).

**Interim exposure under Option A, stated honestly:** between the
force-push and GitHub Support completing the flush, purged blobs remain
fetchable from GitHub ONLY by someone who already holds an exact old
commit/blob SHA or URL, on a private org repo. All normal access paths
(browse, clone, fetch) are clean immediately after the push.
- Requires org admin permission to delete/create the repo.
- Destroys PR/issue history, webhooks, repo settings, and branch
  protections — all must be re-created (branch protection is a §8 item
  anyway). Acceptable per the team's GitHub-as-plumbing stance IF no PR
  history is worth keeping — check the §3 PR inventory first.
- Sequence: §3 PR inventory → delete repo → re-create (same name, private)
  → `git push --mirror` from the cleaned mirror → §8 hardening.

Either option ends with the §10 verification; B additionally verifies the
old blob URLs return 404 immediately.

## 8. Immediately after push — land the recurrence guards

First commit on rewritten `main` (via normal controller flow):
`.gitignore` overhaul —

```gitignore
# ── Bulk-data guard (2026-08-17 remediation) ─────────────────────────────
*.tsv
*.csv
*.xlsx
*.jsonl
*.parquet
# from_claude: prose and code are records; data files are not committable
users/*/from_claude/*.json
# staging dirs: generalized off the hardcoded username
users/*/marketo_dedup_staging/
users/*/psb_export_staging/
users/*/*_staging/
# results/output dirs everywhere
**/results/
**/output/
**/exports/
**/export/
# NOTE: executed .soql/.sql queries are deliberately TRACKED (operator
# ruling 2026-08-17: retain commands for future mining; results never)
# nested clones (was only in .git/info/exclude, which does not travel)
/repos/
```

(Merge with the existing ~125-line `.gitignore`; the `*.log` un-ignore for
`from_claude` stays. Remove the two now-redundant user-literal staging
lines.) Same commit or immediately after: rework
`cake_tools/cake_monetization_report.py` to fetch the test-account list
fresh from Salesforce (its input fixture is purged, not replaced — operator
ruling), and redacted re-adds of the two work_items docs when the operator
has scrubbed them. Then: pre-commit size/email-pattern hook in `.githooks/`,
GitHub branch protection + required PII status check (Phase-1 items from
the collaboration plan). Optional follow-up per the mining ruling: the
current `**/query_history/` ignore hides *executed* queries along with
results — consider narrowing it (track `*.soql`/`*.sql`, keep ignoring the
paired result files) so history mining has material to work with.

## 9. Teammate instructions (plain language, send after §6–§7)

> We rewrote the history of bizops-knowledge-base to remove sensitive data.
> Your existing clone still contains that data and can no longer push.
> Please: (1) if you have uncommitted or unpushed work, copy those FILES
> (not the .git folder) somewhere safe and tell David before doing anything
> else; (2) delete your entire bizops-knowledge-base folder — a `git pull`
> is NOT enough and will corrupt your history; (3) re-clone fresh:
> `git clone git@github.com:BranchMetrics/bizops-knowledge-base.git`;
> (4) re-run your local KB index rebuild; (5) delete any old zips/copies of
> the repo you may have made.

Optional thoroughness (operator judgment): teammates' Time Machine backups
and their solets' ingested session/KB stores may retain content outside git.

## 10. Post-purge verification (run on a FRESH clone)

```bash
git clone git@github.com:BranchMetrics/bizops-knowledge-base.git /tmp/purge-verify
cd /tmp/purge-verify
git rev-list --all --objects | grep -F -f <28-SHA file>     # MUST be empty
git log --all --format= --name-only | sort -u | grep -E '\.(tsv|csv)$|Renewal|payload'  # review
```

Plus the §7 blob-API checks (404s) once Support confirms, and a re-run of
the audit agent's scan as a final sweep. Only after all pass do teammates
get the §9 message and does the collaboration-plan Phase 2 begin.

---

## Execution sequencing note

The purge rewrites `origin/dax-homunculus-integration` along with everything
else, so the 132-commit branch landing into `main` happens AFTER the purge on
rewritten history — do not merge before purging, or the merge commit becomes
one more contaminated ancestor to rewrite.
