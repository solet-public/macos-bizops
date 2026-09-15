# Part 51 — feedback round from dax

**FILED 2026-08-24** as `solet-public/macos-bizops`
[#40](https://github.com/solet-public/macos-bizops/issues/40). Single-item
round, no parent issue. Filed via `gh issue create --title/--body-file`
(operator standing instruction from §47.8/#26, not `--web`).

**Release this round is measured against:** HEAD `36dba33` (dax repo), which
descends from re-mint `5319656` (2026-08-20T20:07:40Z) / RELEASE_NOTES.md
"2026-08-20 — A fresh install can do work again, and the hook errors on a
stock Mac are gone" — same content Part 50 was measured against. Latest
tagged GitHub release is still `release-2026-08-19`; `5319656` itself carries
no tag/release (already reported as §50.4/#38). Nothing new has landed
upstream since.

**Round thesis:** one item, a direct follow-on to §43.1 (#8, closed/fixed).
While trying to spawn a `Git-Controller`-role worker for a routine git commit
on this operator's tmux host, `spawn_session` refused with `host_cannot_spawn`
exactly as §43.1's shipped fix intends — that refusal is correct and working
as designed, not itself a defect. But its own remedy text names "delivering
the worker hooks as plugin hooks" as "the capability fix" and states plainly
that it "is not yet built." Investigating why turned up that it's closer to
already-built than the remedy text suggests: the two hook files it needs
(`capture_session_mapping.py`, `headless_tool_allowlist_gate.py`) already
ship inside the `coordination-hooks` plugin bundle — confirmed byte-identical
across the installed plugin cache, this dax repo's own vendored seed copy,
and the published `bizops-claude-code-plugin` main branch (all `0.7.0`, so
this is not a version-drift question) — they are simply never referenced in
the plugin's own `hooks/hooks.json` (confirmed by grep: zero references).
§43.1's own resolution thread already established, empirically, that a
plugin already in `strictKnownMarketplaces` fires its hooks under this exact
policy (Case C in that thread). Filing this as the feature request to close
that stated gap, now with the added detail of exactly which two already-built
files need the wiring.

**Also encountered, not re-filed:** the `degraded_hooks_acknowledged` /
`local_name` schema gap this same investigation ran into is already tracked
at §48.1 (#28, still open) — reproduced it independently (called
`spawn_session` with and without `degraded_hooks_acknowledged: true`, byte-
identical `host_cannot_spawn` failure both times; confirmed via source read
of `agent_messaging_plugin/local_cli/cli.py` that the CLI itself does no
client-side filtering, so the drop is server-side, consistent with #28's own
diagnosis) but this doesn't add anything #28's existing diagnosis didn't
already establish, so no comment was added there — listed here only per the
"reference rather than re-argue" convention.

**Items carried forward (not re-argued here):** Part 43 (#7) with §43.2 (#9)
open; Part 46 (#14) with §46.2 (#16) open; Part 47 (#18) with all eight
children (#19–#26) open; Part 48 (#27) with all five children (#28–#32)
open; Part 50 (#34) with all five children (#35–#39) open; §49.1 (#33, no
parent) open.

**Content gate:** run against the drafted body below. `BranchMetrics/bizops-
claude-code-plugin` and `solet-public/macos-bizops` are the seed's own public
plugin-marketplace and channel repo names, already used unredacted throughout
this issue thread's own prior history (e.g. #8, #28) — not treated as
employer/business specifics for that reason. No personal identifiers, no
tenant data, no credentials or secret-looking values anywhere in the evidence
below (all evidence is source-file paths and public plugin metadata).

---

## §51.1 — Feature request: ship the "worker hooks as plugin hooks" fix §43.1 named as pending — the two files it needs already exist in the plugin bundle [label: feature-request]

### Item number
§51.1

### The outcome you need
A spawned worker on a host whose managed policy sets
`strictPluginOnlyCustomization: ["hooks"]` should get its registration,
heartbeat, session-mapping, and tool-allowlist hooks working — via the
already-installed `coordination-hooks` plugin, the same way every one of the
plugin's other hooks already does on this exact host — without needing
`degraded_hooks_acknowledged` and without losing `tmux` as a usable spawn
host. This is precisely what §43.1 (#8)'s closing comment named as the "real
capability fix," described there as "not yet built."

### The workflow that hit the gap
This operator's session (role `Operator`) needed to spawn a `Git-Controller`-
role worker via `spawn_session` to make a routine git commit gated by
`coordination-hooks`' own `git_controller_gate.py` — a working, correct
control, not itself the problem. The spawn refused with `host_cannot_spawn`,
exactly as §43.1's shipped preflight is supposed to do on this policy class.
That refusal is correct behavior, not a regression. But its own remedy names
an escape hatch that's separately unreachable (§48.1/#28, still open), and
its longer-term remedy — "delivering the worker hooks as plugin hooks" —
turned out, on inspection, to already be most of the way there:

- `coordination-hooks`' plugin bundle already ships `hooks/capture_session_
  mapping.py` and `hooks/headless_tool_allowlist_gate.py` as real files.
  Confirmed present, and byte-identical `plugin.json` (`version: 0.7.0`)
  across three places: this machine's installed plugin cache, this dax
  repo's own vendored seed copy under
  `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/`, and the
  published `bizops-claude-code-plugin` repository's main branch. So this
  isn't a case of the fix existing upstream and not having reached this
  deployment yet — the files are present everywhere already.
- Neither file is referenced anywhere in the plugin's own
  `hooks/hooks.json` (confirmed by grep — zero matches for either filename).
  Their own docstrings say so directly: each describes itself as a
  "spawn-injected worker hook" that a spawned worker's host adapter
  references "by path in a generated Claude Code `--settings` blob at spawn
  time," and states plainly "it is never wired into this plugin's own
  `hooks/hooks.json`."
- §43.1 (#8)'s own resolution thread already ran the relevant experiment:
  its second comment's Case C showed that a plugin already listed in
  `strictKnownMarketplaces` (which `bizops-claude-code-plugin` already is)
  fires its hooks under this exact policy, no special-casing needed — the
  maintainer's own words: "the shippable fix isn't 'spawn adapters declare
  an ad hoc plugin+marketplace inline' — it's 'ship worker hooks as part of
  a plugin published under a marketplace the operator already has in
  `strictKnownMarketplaces`' ... which is exactly how `coordination-hooks`
  already reaches this machine, unprompted, today."

Put together: the fix isn't blocked on new capability, a new distribution
channel, or an unproven approach — it's the last step of wiring, on files and
a delivery mechanism that already exist and are already proven to work
together.

### What it costs you today
Every git-mutation task (or any task needing a spawned `Git-Controller`, or
any spawned worker at all) on this class of host either needs a human to run
the git commands directly, or the requesting session has to fall back to a
scoped, self-authorized, manual workaround (temporarily relabeling its own
local session identity to the controller role for the duration of the
mutation, then reverting) instead of using the fleet mechanism built for
exactly this. `tmux` — the host this seed's own documentation tells adopters
to *prefer* for anything expected to outlive a release — is the one actually
affected; every spawn attempt on it under this policy hits this wall.

### Your current workaround
None durable. Per-task: either hand the git operation to the human operator,
or (with the operator's explicit authorization, citing a verified
single-live-session basis) have the requesting session perform the mutation
directly rather than delegating it to a spawned worker.

### Implementation sketch (optional appendix)
Register `capture_session_mapping.py` and `headless_tool_allowlist_gate.py`
in `coordination-hooks/hooks/hooks.json` (`SessionStart` and `PreToolUse`
respectively, matching what the adapters already inject today), gated to be
inert outside a spawned-worker context — e.g. exit 0 immediately when the
worker-only environment variables the host adapters already export
(`ANANTA_SESSION_MAPPING_SPOOL_DIR`, `AGENT_INSTANCE_ID`) are absent, so an
ordinary interactive session picks up two harmless no-ops. Once wired that
way, `strictPluginOnlyCustomization` should stop stripping them for the same
reason it doesn't strip any of this plugin's other hooks. §43.1/#8's own
Case C is the proof-of-concept; this item's contribution is narrowing it to
"these two specific, already-existing files, gated this specific way."

### Content gate
- [x] Re-read this issue as drafted above. No personal identifiers, no
  employer or business specifics, and no credentials or secret-looking
  values — including inside the workflow description. (`BranchMetrics/
  bizops-claude-code-plugin` and `solet-public/macos-bizops` are the seed's
  own public repository names, already used unredacted in this same issue
  thread's prior history.)

---

## Filing plan (proposed)

Single issue, form 03 (feature request), no parent. Would file via
`gh issue create --title/--body-file` (per the operator's standing
instruction from §47.8 — scripted, not `--web`) once the operator confirms.

## Filed

Operator confirmed 2026-08-24. Filed as
[solet-public/macos-bizops#40](https://github.com/solet-public/macos-bizops/issues/40)
via `gh issue create --title/--body-file`.
