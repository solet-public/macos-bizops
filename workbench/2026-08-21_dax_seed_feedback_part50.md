# Part 50 — feedback round from dax

**FILED 2026-08-21** as `solet-public/macos-bizops`
[#34](https://github.com/solet-public/macos-bizops/issues/34) (parent) with
five sub-issues §50.1 [#35](https://github.com/solet-public/macos-bizops/issues/35),
§50.2 [#36](https://github.com/solet-public/macos-bizops/issues/36),
§50.3 [#37](https://github.com/solet-public/macos-bizops/issues/37),
§50.4 [#38](https://github.com/solet-public/macos-bizops/issues/38),
§50.5 [#39](https://github.com/solet-public/macos-bizops/issues/39). Plus a
correcting comment on the still-open
[#21](https://github.com/solet-public/macos-bizops/issues/21#issuecomment-5376345513)
in lieu of a duplicate sixth item. Operator yes given inline; filed via
`gh issue create --title/--body-file`, not `--web`, per §47.8. Evidence
sourced from
`workbench/2026-08-21_seed_update_0820_and_orphan_color_leak.md` §§2-6, with
three claims (untagged re-mint, `iterm2` packaging metadata, the two
contradicting `apply_manifest` schema-description quotes) re-verified live
immediately before filing rather than trusted from that prior record.

**Release this round is measured against:** re-mint `5319656`
(2026-08-20T20:07:40Z) / RELEASE_NOTES.md "2026-08-20 — A fresh install can do
work again, and the hook errors on a stock Mac are gone". Merged locally as
`0f00fe7` into `seed-feedback-and-gsuite-bind-fix`.

**Filing note (carried forward, per §47.8/#26, still open):** filed via
`gh issue create --title/--body-file`, not the web chooser, per the operator's
standing instruction.

---

## Redirected, not filed as a new item: the leaked-process-per-cutover defect

This was the strongest item in the source evidence, but it duplicates an
already-open issue: **§47.3 / [#21](https://github.com/solet-public/macos-bizops/issues/21)**
— "the blue-green swap's own completion leg dispatches `complete_swap` to a
service the orchestrator does not have bound." That issue ends by asking
upstream to say whether the missing binding is a profile-configuration gap or
a registration defect. This session's evidence answers that question and adds
severity data #21 doesn't have. Per the runbook ("reference the still-open
issue rather than filing duplicates"), this round adds it as a **comment on
#21**, not a new sub-issue.

### Comment to post on #21

> **The backstop claim in my original report was wrong.** I wrote "the
> pending-finisher backstop reaped the outgoing color 10 seconds later — so
> the practical cost today is... log noise." There is no working backstop.
> Nothing reaps the outgoing color. Colors accumulate across releases, and
> some of them are actively harmful while they sit there. Correcting that in
> the same place I stated it, with the measurement below.
>
> New measured evidence, from a 2026-08-21 `apply_manifest` cutover on
> release `rel-20260821T211746Z-0f00fe7` (re-mint `5319656` /
> RELEASE_NOTES.md 2026-08-20 entry).
>
> Answering the open question at the end of the original report: on this
> profile, `local_self_deployment_service` is **not** bound in
> `service_bindings.json` (only `self_deployment_service` is). This is not
> boot-ordering or a one-off registration defect — the binding is simply
> absent from this profile's manifest, consistently, across separate
> releases and cutovers.
>
> Because teardown dispatches to the same unbound service, the cost isn't
> confined to the ERROR-level log noise this report already describes: every
> cutover on this profile leaves the previous color's entire process running,
> never terminated. Measured five live `ananta.cli` processes from three
> different release trees, all against one Postgres schema (parent chain:
> each generation kept its predecessor alive rather than replacing it). Two
> of the four orphans were not idle:
> - one was still running its full `agent_messaging_plugin` rotation sweep on
>   a ~60s cycle, two days after being superseded
> - one threw `psycopg.errors.FeatureNotSupported: cached plan must not
>   change result type` — consistent with a schema migration (this release
>   adds a column to `peer_binding`) landing under a process holding stale
>   prepared statements from before the migration
>
> So `macos_self_deployment_plugin`'s own "verify-surface gap" KB note, which
> frames this purely as an inability to run `swap_status`, understates the
> deploy-path consequence too: it isn't just a missing read surface, it's an
> accumulating leak of live, occasionally-harmful processes against shared
> state.
>
> Separately (filed as Part 50 §50.1) — even a correctly-bound `complete_swap`
> would not fully close this on its own: the orphaned colors measured here
> also did not terminate cleanly on SIGTERM.

---

## What this round is about

Five items from applying the 2026-08-20 re-mint to a live, already-hydrated
clone. Two (§50.1, §50.2) compound already-open issues without duplicating
them: §50.1 shows that even a fixed `complete_swap` (#21) wouldn't stop the
leak, because the orphaned process doesn't die on SIGTERM either; §50.2 is a
second, independent instance of the undeclared-argument bridge defect first
reported at §48.1 (#28), now confirmed as a systemic gap rather than one
verb's oversight. §50.3 is a documentation self-contradiction surfaced while
investigating §50.2. §50.4 and §50.5 are unrelated: an untagged re-mint that a
releases-only watch would miss, and an optional dependency with no discovery
path before it's hit.

## Items in this round

| Item | Class | Summary |
|---|---|---|
| §50.1 | defect | A solet color does not terminate on SIGTERM — hangs in graceful shutdown, needs SIGKILL; also defeats launchd's respawn-on-signal-death path |
| §50.2 | defect | Undeclared invocation arguments are silently dropped rather than schema-rejected — second independent instance (first: §48.1/#28) |
| §50.3 | defect | `apply_manifest`'s top-level description contradicts its own `new_manifest` field description on whether `service_bindings` is merged or rejected |
| §50.4 | defect | Re-mint `5319656` shipped with no git tag and no GitHub release, so a releases-only watch never learns it exists |
| §50.5 | defect | `iterm2`, a dependency `seat_rotation_helper` needs, is undeclared anywhere an adopter would find it before hitting the gap |

---

## §50.1 — A solet color does not terminate on SIGTERM

### The exact command or action
```
# Measured across two sources: orphan-color cleanup sending SIGTERM directly,
# and the platform's own failed teardown path (§21/#complete_swap) sending it
# on our behalf during a cutover:
kill -TERM <pid>
```

### Observed output
SIGTERM measured on four processes — three confirmed orphans, plus the
then-active color from a later cutover — none of which terminated cleanly:
- PID `73835` (`rel-20260819T205556Z-2329ea4`, orphan) — hung in "initiating
  graceful shutdown", required SIGKILL
- PID `85662` (`rel-20260819T185543Z-e9c6558`, orphan) — same, required
  SIGKILL
- PID `62108` (`rel-20260820T190035Z-2329ea4`, orphan) — SIGTERM issued by
  the platform's own (failing) teardown path at 14:18:56, still alive six
  minutes later
- PID `31201` (`rel-20260821T211746Z-0f00fe7`, the active color at the time,
  terminated deliberately during convergence onto the launchd-respawned
  process) — same, required SIGKILL

### Expected behavior
SIGTERM should complete a graceful shutdown within a bounded time and the
process should exit `0`.

### Release measured against
re-mint `5319656` (2026-08-20T20:07:40Z) / RELEASE_NOTES.md 2026-08-20 entry.

### Diagnosis or inference
Because the process exits by signal rather than a clean `exit(0)`, the
launchd job's `KeepAlive = {SuccessfulExit: false}` respawns it — observed
exit status `-15` via `launchctl list`. This compounds #21/§47.3: even a
correctly-bound `complete_swap` teardown of a launchd-managed root would be
self-defeating today, because killing it just respawns it.

### Local fix or divergence you are carrying
None — converged on the launchd-respawned process as the single running
color and reconciled router state around it, rather than fixing the shutdown
path itself.

### Content gate
- [x] Re-read; no personal identifiers, no employer or business specifics, no
  credentials or secret-looking values.

---

## §50.2 — Undeclared invocation arguments are silently dropped rather than schema-rejected (second independent instance)

### The exact command or action
```
dax call service_interface::lifecycle_management_service::apply_manifest \
  '{"new_manifest": {...}, "service_bindings": {"local_self_deployment_service": "macos_self_deployment_plugin"}, "reason": "..."}'
```
`apply_manifest`'s own top-level description states:
> Caller-supplied `service_bindings` ARE accepted and MERGED over the
> currently-bound snapshot (`effective_bindings = {**current, **caller}`) —
> the mechanism for plugin renames/rebinds

### Observed output
`status: "dry_run"`, `success: true`, `rebound_services: []` — the call
succeeds as if `service_bindings` were a valid argument, but produces no
rebind. The published invocation schema's `arguments` object declares
`additionalProperties: false`, and `service_bindings` is not a property in
that schema, so the value is silently stripped before the verb executes —
never rejected, despite the schema's own `additionalProperties: false`.

### Expected behavior
Either honor a documented, server-implemented argument, or reject an
undeclared one loudly (a schema validation error) — never both drop it and
return `success: true`.

### Release measured against
re-mint `5319656` (2026-08-20T20:07:40Z) / RELEASE_NOTES.md 2026-08-20 entry.

### Diagnosis or inference
Same failure mode already reported at §48.1 (#28, still open) for
`spawn_session`'s `degraded_hooks_acknowledged`: an argument implemented and
documented server-side, absent from the schema the bridge actually validates
against, dropped before the verb sees it. Filing this as a separate item
because it's a second, independent verb hitting the identical shape — that
upgrades it from "one verb's oversight" to "a systemic gap between a verb's
declared schema and what its implementation reads," worth fixing once for the
class.

### Local fix or divergence you are carrying
None committed — the rebind was deliberately not forced through any other
path, since committing triggers another cutover that (per §50.1 / #21) would
leak another orphan.

### Content gate
- [x] Re-read; no personal identifiers, no employer or business specifics, no
  credentials or secret-looking values.

---

## §50.3 — `apply_manifest`'s own description contradicts its `new_manifest` field description on `service_bindings`

### The exact command or action
```
dax schema service_interface::lifecycle_management_service::apply_manifest
```

### Observed output
The verb's top-level `description` field states, verbatim: *"Caller-supplied
`service_bindings` ARE accepted and MERGED over the currently-bound snapshot
(`effective_bindings = {**current, **caller}`) — the mechanism for plugin
renames/rebinds."*

The `new_manifest` argument's own `description` field states, verbatim:
*"`service_bindings` and `plugin_config_overrides` in the payload are
REJECTED with a stable `bindings_change_rejected_in_v1` token before any disk
write."* Both fields are read from the same live schema response, same call,
same timestamp — this isn't stale docs vs. current behavior, it's two fields
of one schema disagreeing with each other.

### Expected behavior
One verb's documentation should not assert both behaviors for the same
input.

### Release measured against
re-mint `5319656` (2026-08-20T20:07:40Z) / RELEASE_NOTES.md 2026-08-20 entry.

### Diagnosis or inference
Whether this profile is *supposed* to be able to bind
`local_self_deployment_service` via this path, or whether §50.1/#21's
`complete_swap` dispatch is wrong to name it, cannot be settled by reading
the docs alone — that ambiguity is itself the finding.

### Local fix or divergence you are carrying
None.

### Content gate
- [x] Re-read; no personal identifiers, no employer or business specifics, no
  credentials or secret-looking values.

---

## §50.4 — Re-mint `5319656` shipped with no git tag and no GitHub release

### The exact command or action
Checked for a tag/release corresponding to re-mint `5319656`
(2026-08-20T20:07:40Z, RELEASE_NOTES.md entry "2026-08-20 — A fresh install
can do work again, and the hook errors on a stock Mac are gone").

### Observed output
The seed's own tags stop at `release-2026-08-19`. No tag and no GitHub
release exists for the `5319656` re-mint, despite a RELEASE_NOTES.md entry
describing adopter-visible changes.

### Expected behavior
The update runbook states releases are "the primary response surface" and
directs adopters to subscribe to releases rather than poll. Every re-mint
that carries a RELEASE_NOTES.md entry should ship a matching tag and
release, or a releases-only subscriber never learns it shipped.

### Release measured against
re-mint `5319656` itself is the subject of this item.

### Diagnosis or inference
Discovered this re-mint only because the operator explicitly pointed the
session at the seed and asked it to apply the update — not because a release
notification surfaced it.

### Local fix or divergence you are carrying
None.

### Content gate
- [x] Re-read; no personal identifiers, no employer or business specifics, no
  credentials or secret-looking values.

---

## §50.5 — `iterm2`, a dependency `seat_rotation_helper` needs, is undeclared anywhere an adopter would find it before hitting the gap

### The exact command or action
None — this is an absence.
`agent_messaging_plugin/src/agent_messaging_plugin/seat_rotation_helper.py`
imports `iterm2` as an optional dependency; the import failure is caught and
disclosed (raised by name) only at the one step that actually drives an
iTerm2 pane.

### Observed output
On a profile that excludes `iterm2_coding_agent_management_plugin` — the
plugin that would otherwise pull `iterm2` in as a transitive dependency —
the distribution was simply absent from the venv, with no signal anywhere
upstream of that one call site. Practical consequence measured this session:
a seat operated for an extended period believing self-context-clearing was
structurally impossible, because the only place that says otherwise is an
exception raised deep in a helper it had no reason to import speculatively.

### Expected behavior
Either declare `iterm2` as a direct dependency of `agent_messaging_plugin`
(the helper ships there, not in the excluded plugin), or have hydration emit
a one-time note when a profile excludes
`iterm2_coding_agent_management_plugin` while the fleet plugin shipping
`seat_rotation_helper` is still present.

### Release measured against
re-mint `5319656` (2026-08-20T20:07:40Z) / RELEASE_NOTES.md 2026-08-20 entry
— dependency gap predates this release but was diagnosed during it.

### Diagnosis or inference
None beyond the above — this was confirmed by installing the missing
distribution and running the shipped smoke test, not inferred.

### Local fix or divergence you are carrying
Installed `iterm2==2.20` and `websockets==17.0.1` directly into dax's venv.
`plugins/agent_messaging_plugin/tests/seat_rotation_helper_smoke.py`: 68
passed, 0 failed.

### Content gate
- [x] Re-read; no personal identifiers, no employer or business specifics, no
  credentials or secret-looking values.

---

## Items carried forward from earlier rounds

Checked live issue state immediately before drafting this round (not just
release notes):

- Part 43 (#7) — §43.2 (#9) still open
- Part 44 (#10) — all children closed
- Part 46 (#14) — §46.2 (#16) still open
- Part 47 (#18) — all eight children (#19–#26) still open, including §47.3
  (#21), which this round adds new evidence to by comment rather than
  duplicating
- Part 48 (#27) — all five children (#28–#32) still open, including §48.1
  (#28), which §50.2 above cites as the first instance of the same defect
  class
- Part 49 (#33) — still open

None of the above have closed since Part 49 was filed today.

## Content gate (round-level)

- [x] Re-read the parent-issue text and every child issue above against: no
  personal identifiers, no employer or business specifics, no credentials or
  secret-looking values. Filesystem paths are generalized to `~/...` /
  `<clone>` where they appeared in source evidence.

## Filing plan (executed, in this order)

1. Posted the comment above on #21 — landed as
   [issuecomment-5376345513](https://github.com/solet-public/macos-bizops/issues/21#issuecomment-5376345513).
2. `gh issue create` — parent #34, "Part 50 — feedback round from dax", label
   `feedback-round`.
3. `gh issue create` ×5 for §50.1–§50.5 (#35–#39), each `--parent 34`, label
   `defect`. Confirmed via `gh api repos/solet-public/macos-bizops/issues/34/sub_issues`
   that all five attached correctly.
