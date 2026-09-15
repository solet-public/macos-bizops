# Part 53 — feedback round from dax

**DRAFT — not yet filed.** Awaiting operator sign-off (Step 6). Both items
are relayed from a peer solet ("Kenbot," confirmed safe to name verbatim by
the operator), reporting its own troubleshooting session dated **Friday,
2026-09-11** — not this session's own direct measurement. Neither item was
independently reproduced here; each body says so explicitly and quotes
Kenbot's own report as the primary evidence.

**Release measured against — best available, not independently confirmed.**
Kenbot's operator does not have the exact commit/tag Kenbot was updating to
on 2026-09-11, so this is anchored by date instead. Checked this seed's
public channel repo (`solet-public/macos-bizops`, the same one this dax
deployment tracks) for its own tag history around that date: the most recent
tagged release before 2026-09-11 was `release-2026-09-08` (commit `9f3cfa3`,
"The Homebrew install path: brew install solet-public/tap/solet, then solet
create") — thematically consistent with §53.1's mention of a new
`solet_cli` Homebrew-manager package, but **not confirmed** to be the exact
commit Kenbot pulled, and Kenbot's own report says the commit it pulled
carried **no RELEASE_NOTES.md entry at all** — which `release-2026-09-08`
does have, so Kenbot's channel may differ from this one, or it may have
pulled a later, untagged commit past that tag. Both items state this
uncertainty plainly rather than asserting a SHA that isn't confirmed.

**Content gate — round-level note:** "Kenbot" is a solet name, confirmed
safe to use verbatim by the operator. No operator/employer names, no
credentials, no tenant data. All file paths and identifiers below are seed-
internal source paths (`ananta/`, `macos_self_deployment_plugin`,
`solet_setup_contracts`, `solet_cli`), not business-specific.

---

## §53.1 — Blue-green release-copy step doesn't know about new top-level packages, silently dropping them from the release tree [label: defect]

### Item number
§53.1

### Provenance
Relayed from a peer solet ("Kenbot," a separate deployment, installed
~2026-08-28) reporting on its own attempt to update itself from a newer
pulled seed commit, dated 2026-09-11. Not reproduced by this session.
Quoted from Kenbot's own report to its operator, lightly reformatted into
this item's structure; substance unchanged.

### The exact command or action
Kenbot attempted a routine self-update — applying a newly pulled seed
commit via the platform's blue-green `apply_manifest` cutover, per its own
manifest-driven update flow. No unusual invocation; a normal update cycle.

### The observed output
The cutover surfaced three separate defects in the pulled commit, in
increasing order of severity — the first two collateral, already fixed by
Kenbot itself; the third is what's being reported here:

1. **Collateral, already fixed:** the pulled commit deleted two KB process
   JSON stubs (`io_interface_service::deliver_artifact`/`post_message`).
   Kenbot restored them locally (its own commit `d22fbf0`).
2. **Collateral, already fixed:** the pulled commit renamed
   `agent_messaging_plugin`'s console-script entry point away from `solet`
   to free that name for a new `solet_cli` Homebrew-manager package. Kenbot
   is a symlink straight to `.venv/bin/solet`, so installing `solet_cli`
   silently hijacked the name and broke Kenbot outright (`command not
   found`) until caught and the symlink was re-pointed at
   `.venv/bin/solet-bridge`.
3. **Not fixed, reported here:** the release builder's copy step doesn't
   know about the two new top-level packages the pulled commit introduced.
   Each blue-green release materializes a fresh copy of the source tree
   into `~/.ananta/releases/<name>/rel-.../code/`, but that copy step only
   carries `ananta/` and `plugins/` — it silently drops `solet_setup_
   contracts/` and `solet_cli/`, even though `root_manifest.yaml` now lists
   both as required root directories and `ananta/setup.py` now depends on
   the former. The new release's own venv ends up missing
   `solet_setup_contracts.selected_source_record`, and `apply_manifest`'s
   L2 probe correctly refuses to cut over on that basis.

The pulled commit also carried **no `RELEASE_NOTES.md` entry at all**,
which reads as an unfinished snapshot tagged prematurely rather than
something meant to ship.

### The expected behavior
The release-copy step should derive its copy list from `root_manifest.yaml`
(or whatever declares required root directories), so that adding a new
top-level package to the manifest is sufficient on its own — not a second,
separately-remembered change to the copy logic. Short of that, the copy
step silently dropping a manifest-required directory should itself be a
loud, named failure rather than something that only surfaces two steps
later as an opaque missing-module error inside the new venv.

### What worked as designed (not itself a defect)
The L2 probe's refusal to cut over is correct behavior — it caught a
genuinely broken release tree before it went live. Kenbot's own summary:
"kenbot is healthy and fully working on the old code (every failed attempt
rolled back cleanly, exactly as the platform's blue-green design is
supposed to do). Nothing is broken." Filed as a defect in the release-copy
logic specifically, not in the blue-green safety mechanism, which worked.

### Release measured against
Not independently confirmed — see the round-level provenance note above.
Anchored by date (2026-09-11) rather than a verified commit/tag.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above. "Kenbot" confirmed safe to
  use verbatim by the operator.

---

## §53.2 — Defect (root-caused, fix proposed): job_service's uncapped job-table read trips MAX_READ_ROWS once job history exceeds ~100 matching rows [label: defect]

### Item number
§53.2

### Provenance
Relayed from the same peer solet ("Kenbot"), same reporting session, same
date (2026-09-11). Not reproduced by this session. Quoted from Kenbot's own
report, lightly reformatted; substance unchanged.

### The exact command or action
Any caller of `job_service::get_latest_job` (or equivalent) filtering by
`plugin_name`/`action_name`, once that filter still matches more than 100
rows in the job table.

### The observed output
Root-caused by Kenbot to `ananta/src/ananta/services/job_service/
service.py:595`: `_read_jobs()` calls `query_state` on the job table with no
limit, and its own docstring says "(uncapped)" by design. That assumption
held while the table was small; now that it has grown to **109,393 rows**,
any `plugin_name`/`action_name` filter that still matches more than 100 rows
trips `MAX_READ_ROWS` and the call errors out. There is no retention/pruning
job for the job table anywhere in `job_service` or `async_job_manager.py` —
nothing shrinks it automatically, and nothing about this self-heals with
time.

### The expected behavior
`_query_latest_job`/`_read_jobs` should pass an explicit limit (1 is
sufficient — it only wants the single newest match) instead of relying on
`read_state`'s default bound. Kenbot's own assessment: "a small, low-risk
fix." Absent that fix, the job table only ever grows, so this doesn't
self-heal, and every `build_*.py` reconciliation script (or any consumer)
that polls jobs this way keeps failing the same way, permanently.

### What it costs you today
Any caller — this includes `dax call service_interface::job_service::
get_latest_job` as used in this very deployment's own no-listener job-poll
convention — is one popular-enough `plugin_name`/`action_name` pair away
from tripping this, with no self-recovery short of the code fix or a manual
prune of platform job history (which Kenbot's own report explicitly treats
as not a casual option).

### Implementation sketch (optional appendix, per Kenbot's own diagnosis)
Pass `limit=1` explicitly in `_query_latest_job`/`_read_jobs` rather than
relying on `read_state`'s default (uncapped) bound. Kenbot offered to make
this fix directly, noting it's the seed's own source and that
`apply_manifest`'s blue-green router means a fix "can go live without a bare
restart" — but the decision of whether Kenbot applies it locally is
separate from filing this upstream, since the same bug will recur for any
other deployment whose job table crosses the same threshold.

### Release measured against
Not independently confirmed — see the round-level provenance note above.
Anchored by date (2026-09-11) rather than a verified commit/tag. Note this
defect is a data-growth threshold effect in already-running code, not
something introduced by the pulled commit §53.1 is about — it would trip on
any release once the job table crosses the row-count threshold.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above. "Kenbot" confirmed safe to
  use verbatim by the operator.

---

## Filing plan (proposed)

Two items, same day, same reporting session → filing as **Part 53** with no
parent wrapper, matching the operator's stated preference from Part 52
(say if you want a parent this time instead). `gh issue create --title/
--body-file` per the standing scripted-filing convention.

## Filed

Both items filed 2026-09-15 as standalone issues (no parent), operator
confirmed as-drafted:

| Item | Issue |
|---|---|
| §53.1 | [#56](https://github.com/solet-public/macos-bizops/issues/56) |
| §53.2 | [#57](https://github.com/solet-public/macos-bizops/issues/57) |
