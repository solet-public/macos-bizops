# Git-Controller brief — EXECUTE the PII purge (Option A: force-push + Support flush; the repo is NOT deleted), 2026-08-17

> **🛑 LOCAL-ONLY — never commit/push this file or its two companion .txt
> files** (they name customer files). Delete all three after verified
> completion.

You are the executor holding the **Git-Controller** gate name — either the
Operator session itself under the gate's solo-fleet exemption (the mode the
operator selected 2026-08-17: verify peer_list self-only + no tmux sessions,
patch own `~/.claude/sessions/<pid>.json` name to `Git-Controller` for the
duration, restore after Phase 6), or a spawned Git-Controller session if the
fleet is no longer solo. This brief authorizes the operations below —
including the named force/mirror pushes and the GitHub repo delete/re-create
— per operator sign-off of the manifest
(`2026-08-17_bizops_pii_purge_manifest.md`, same directory) and operator
selection of its Option B. Execution begins only on the operator's explicit
final "go" — if you do not have it in hand (in-conversation or in a
dispatch), STOP and ask.

Inputs (same directory):
- `2026-08-17_bizops_purge_paths.txt` — 30 exact path spellings to purge
- `2026-08-17_bizops_purge_blobs.txt` — 28 blob SHAs; success = all
  unreachable everywhere afterward

## Phase 0 — preconditions (verify ALL before any mutation)

1. Backup exists: `~/Workspace/bizops-knowledge-base-2026-08-17`, and its
   `git remote -v` prints nothing.
2. `~/BizOpsData/` holds the 17 preserved working files (37 MB).
3. `which git-filter-repo` resolves.
4. `gh auth status` shows account `dwestgate` with `repo` scope
   (no `delete_repo` needed — the repo is NOT deleted; operator ruling
   2026-08-17).
5. No other session mutating the repo (peer_list; confirm with Operator if
   in doubt).
6. Working clone status: only `.claude/settings.local.json` modified.

## Phase 1 — build and verify the cleaned mirror (no external effects)

```bash
mkdir -p /tmp/purge-work && cd /tmp/purge-work
git clone --mirror git@github.com:BranchMetrics/bizops-knowledge-base.git bkb-mirror.git
cd bkb-mirror.git
git filter-repo --invert-paths --paths-from-file /Users/david.westgate/Workspace/dax/workbench/2026-08-17_bizops_purge_paths.txt
# verify inside the mirror — MUST output nothing:
git rev-list --all --objects | grep -F -f <(awk '{print $1}' /Users/david.westgate/Workspace/dax/workbench/2026-08-17_bizops_purge_blobs.txt)
```

If the grep outputs anything: STOP, report which SHAs survived. Do not
proceed to Phase 2.

Also sanity-check ref count: `git for-each-ref | wc -l` ≈ 21 branches
(+ tags if any) — record the exact list for the Phase 3 comparison.

## Phase 2 — the point of no return (GitHub side): force-push the cleaned refs

**The repo itself is NOT deleted or re-created (operator ruling 2026-08-17).**

If `main` has branch protection blocking force-pushes, temporarily lift it
(`gh api` or repo settings), push, and reinstate stronger protection in
Phase 4.

```bash
cd /tmp/purge-work/bkb-mirror.git
git push --force git@github.com:BranchMetrics/bizops-knowledge-base.git 'refs/heads/*:refs/heads/*' 'refs/tags/*:refs/tags/*'
```

(Explicit refspecs, NOT `--mirror`: a mirror-push against a live GitHub repo
errors on GitHub's read-only hidden refs like `refs/pull/*`. The mirror
holds exactly origin's 21 branches, so these refspecs force-update
everything real without touching hidden refs.)

Then verify remotely:
- Branch inventory on GitHub matches the Phase 1 list
  (`gh api repos/BranchMetrics/bizops-knowledge-base/branches --paginate`).
- Fresh-clone check comes in Phase 5 — that is the immediate pass/fail.
- NOTE: `gh api .../git/blobs/<sha>` may still return 200 for old blobs at
  this stage — EXPECTED. Server-side flush happens via the Phase 4b Support
  request; the 404 evidence is collected after Support confirms.

## Phase 2b — file the GitHub Support request (the server-side flush)

Submit via https://support.github.com (or the org's support channel) for
`BranchMetrics/bizops-knowledge-base`: sensitive data was removed by history
rewrite; request permanent removal of unreachable/cached objects and cached
views, citing the 28 blob SHAs from
`2026-08-17_bizops_purge_blobs.txt` and noting the 5 PRs whose refs may pin
old history (#1–#5). Record the ticket id in the Phase 6 report. When
Support confirms: re-run the blob-API checks — all 28 must 404 — and append
the evidence to the report.

## Phase 3 — rewrite the operator's working clone in place

```bash
cd ~/Workspace/bizops-knowledge-base
mv .claude/settings.local.json /tmp/purge-work/settings.local.json.keep
git filter-repo --invert-paths --paths-from-file /Users/david.westgate/Workspace/dax/workbench/2026-08-17_bizops_purge_paths.txt --force
mv /tmp/purge-work/settings.local.json.keep .claude/settings.local.json
git remote add origin git@github.com:BranchMetrics/bizops-knowledge-base.git
git fetch origin
```

Verify: current branch is still `dax-homunculus-integration`; the 28-SHA
grep against `--all` outputs nothing locally; for every fetched `origin/<b>`
there is ref equality with the mirror's rewritten counterpart
(spot-check `main` and `dax-homunculus-integration` by `git rev-parse`).
NOTE: filter-repo will NOT touch `~/BizOpsData/` or the dated backup —
those are the retention copies; leave them alone.

## Phase 4 — recurrence guards (first commits on the new history)

On a branch off rewritten `main` (or directly if operator prefers — confirm
in dispatch): apply the `.gitignore` overhaul EXACTLY as §8 of the manifest
(including the tracked-queries note; NO queue-dir ignores; NO test_accounts
allowlist). Commit, push, merge to `main` per normal controller procedure.
Then re-enable branch protection on `main`
(`gh api -X PUT repos/.../branches/main/protection ...` — settings per the
collaboration plan; at minimum: require PRs, block force pushes).

## Phase 5 — final verification (fresh clone)

```bash
git clone git@github.com:BranchMetrics/bizops-knowledge-base.git /tmp/purge-verify
cd /tmp/purge-verify
git rev-list --all --objects | grep -F -f <(awk '{print $1}' .../2026-08-17_bizops_purge_blobs.txt)   # MUST be empty
git log --all --format= --name-only | sort -u | grep -E '\.(tsv|csv)$|Renewal|payload_'               # review output
```

## Phase 6 — report

ONE consolidated report to role `Operator` (SendMessage; fallback: write
`2026-08-17_purge_execution_report.md` in this directory): per-phase
outcomes, the 28 × 404 evidence, ref-equality results, anything that
deviated. Note for the Operator's follow-ups (NOT yours): teammate re-clone
message (§9 of manifest), cake_tools rework, work_items redacted re-adds,
`~/BizOpsData` export-root repoint, deletion of the three LOCAL-ONLY
workbench files, re-opening the 3 open PRs if wanted.

Absolute boundaries: never touch `~/Workspace/bizops-knowledge-base-2026-08-17`
(the backup) or `~/BizOpsData/`; no operations on any other repo; if any
verification fails, stop and report rather than improvising.
