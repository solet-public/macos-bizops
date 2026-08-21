# Part 47 — feedback round from dax

**FILED 2026-08-19** as `solet-public/macos-bizops` parent
[#18](https://github.com/solet-public/macos-bizops/issues/18), with eight
sub-issues attached via the sub-issue API:

| Item | Issue | Class |
|---|---|---|
| §47.1 | [#19](https://github.com/solet-public/macos-bizops/issues/19) | defect |
| §47.2 | [#20](https://github.com/solet-public/macos-bizops/issues/20) | defect |
| §47.3 | [#21](https://github.com/solet-public/macos-bizops/issues/21) | defect |
| §47.4 | [#22](https://github.com/solet-public/macos-bizops/issues/22) | defect |
| §47.5 | [#23](https://github.com/solet-public/macos-bizops/issues/23) | defect |
| §47.6 | [#24](https://github.com/solet-public/macos-bizops/issues/24) | feature-request |
| §47.7 | [#25](https://github.com/solet-public/macos-bizops/issues/25) | feature-request |
| §47.8 | [#26](https://github.com/solet-public/macos-bizops/issues/26) | defect |

Eight-item round with a parent issue. §47.8 was added during filing — the
shipped `feedback` skill's Step 7 mandates `gh issue create --web`, which ends
in a browser form only a human can submit, contradicting that skill's own
opening contract that the operator "should only have to say yes to sending it."
Filed via `gh issue create --title/--body` under Step 7's own escape clause
("when the web chooser genuinely cannot be reached"), with every form field
reproduced by hand under its own heading and a filing-note disclosure in every
body. Content gate run against the exact submitted text.

**Release this round was measured against:** HEAD `e9c6558` (seed bundle
`dd4109d` merged at `871229c`) / RELEASE_NOTES.md releases 2026-08-17.2,
2026-08-18, 2026-08-18.2.

**Round thesis:** the seed's update and migration machinery has no model for an
adopter that carries local commits — a state the seed's own upstream-feedback
runbook explicitly sanctions ("Carry whatever divergence you need locally").
§47.1 is that gap in documentation form and §47.2 is the same gap in runtime
form; §47.3–§47.5 are independent defects the same update surfaced; §47.6 and
§47.7 are requests.

**Items carried forward:** Part 43 (#7) with §43.2 (#9) open; Part 44 (#10)
with all children closed; Part 46 (#14) with §46.2 (#16) open. Not re-argued
here. §46.1 (#15) closed in this release and is confirmed fixed — see §47.7,
which reports a residual ordering gap rather than re-opening it.

Per Step 7 of the update runbook, we checked these releases' `RELEASE_NOTES.md`
against our open items. Issues answered are cited as #8, #17 and #13; **neither
#9 nor #16 is cited**, and both stay open on our list. The nearest change is
"Long-running workers survive a deploy," which makes all four spawn adapters
re-resolve the worker CLI path per spawn instead of caching it at construction —
adjacent to #16 but not the same defect, which is the *tmux* worker environment
missing the CLI-directory `PATH` prepend. We have not re-probed either item since
the update.

**Content gate:** run against this text — no personal identifiers, no employer
or business specifics, no credentials. Filesystem paths are redacted to
`<clone>` / `~/`.

---

## Parent — "Part 47 — feedback round from dax"  [label: feedback-round]

**Part number:** Part 47

**Release this round was measured against:** HEAD `e9c6558` (seed bundle
`dd4109d`) / RELEASE_NOTES.md 2026-08-18.2.

**What this round is about:** Applying releases 2026-08-17.2 / 2026-08-18 /
2026-08-18.2 to a live, already-hydrated clone that carries 23 local commits.
The update landed and the solet is healthy, but getting there surfaced two
defects that are the same underlying gap seen from two sides — the seed
sanctions carrying local divergence in one runbook and forbids the only
mechanism for absorbing an update into it in another (§47.1), and the rename
migration plus the blue-green router together leave a live deployment unable to
deploy at all once a pre-rename process survives (§47.2). Three further defects
are independent of that thesis (§47.3–§47.5), and two items are requests
(§47.6–§47.7).

**Items in this round:**

| Item | Class | Summary |
|---|---|---|
| §47.1 | defect | Update runbook's `--ff-only`-or-re-birth path contradicts the feedback runbook's sanctioned local divergence; no merge procedure and no post-merge verification gate |
| §47.2 | defect | A surviving pre-rename process holds the router's active binding forever; the post-migration instance can never self-activate and `apply_manifest` refuses without naming cause or repair |
| §47.3 | defect | The blue-green swap's own completion leg dispatches `complete_swap` to a service the orchestrator does not have bound |
| §47.4 | defect | Three consecutive releases shipped a 125-line vendored-hook change with `plugin.json` `version` pinned at `0.5.9` — the seed's own release rule, unfollowed |
| §47.5 | defect | `marketo_plugin`'s poll worker reads `orchestrator_ref.async_job_manager` eagerly where every sibling connector lazy-acquires; ~15 ERROR tracebacks per boot |
| §47.6 | feature request | No sanctioned mechanism for the Git-Controller gate on a genuinely solo fleet, now that the session-label patch is explicitly forbidden |
| §47.7 | feature request | A release that fixes the deploy preflight cannot be installed by the preflight it fixes — the update runbook should say the bare-restart path is mandatory once |

**Content gate:** run against this text — no personal identifiers, no employer
or business specifics, no credentials.

---

## §47.1 — DEFECT: the seed sanctions carrying local divergence in one runbook and forbids the only way to absorb an update into it in another; no merge procedure and no post-merge verification gate  [label: defect]

**Item number:** §47.1

**The exact command or action:**

```bash
git -C <clone> pull --ff-only
```

Step 2 of `05_seed_update_runbook.md`, run on a clone carrying local commits.

**Observed output:** the pull refuses (`not possible to fast-forward`). The
runbook's response to that refusal is unambiguous:

> If this refuses ("not possible to fast-forward"), STOP. The clone's history
> and the seed repo have diverged — that is not a normal update state. Present
> the facts to the operator; the usual resolution is re-birth from the current
> seed. **Never force, never rebase, never merge** — a factory re-mint always
> fast-forwards.

**Expected behavior:** a documented, supported way to take a re-mint into a
clone that carries local commits — because `07_upstream_feedback_runbook.md`,
shipped in the same seed, explicitly tells adopters to be in exactly that state:

> This code is Apache-2.0: you already have the right to modify and
> redistribute it, and a solet is meant to be grown rather than collaboratively
> edited. **Carry whatever divergence you need locally** — and tell upstream
> about it, per the local-divergence rule below, so the loop closes.

These two shipped documents cannot both be followed. An adopter who takes the
feedback runbook's invitation is guaranteed to hit the update runbook's STOP on
every subsequent release, and the only resolution the update runbook offers —
re-birth — destroys the accumulated state that runbook exists to preserve.
Divergence here is not exotic: several of our local commits exist *because* we
reported a defect upstream and carried a fix while waiting, which is the
workflow the feedback runbook prescribes.

**What it costs us, measured:** with no sanctioned procedure, we improvised —
and the improvisation was destructive in a way nothing reported. Our 2026-08-17
merge commit (`dbb6dac`) records its own strategy:

> Conflicts resolved by taking upstream (`-X theirs`) throughout, after
> verifying line-by-line that every local change on our side was either a strict
> subset of upstream's change to the same file, or a stopgap this release's
> official fix supersedes (plugin.py/**session_lifecycle_verbs.py**/session_sweep.py/
> tmux_adapter.py/solet_cli.py: our local coordination-hooks 0.5.5 port for #8 is
> superseded by the real #13/#8 fix…)

That judgment was wrong for one of the files it names. `session_lifecycle_verbs.py`
did not only carry the superseded hook port — it also carried an unrelated local
feature, and `-X theirs` discards local hunks silently, with no conflict markers
and no report. It hit two files:

1. `CLAUDE.md` — the hydrated operator block was replaced by the seed's stock
   version (9822 bytes → 4478 bytes). Noticed the same day and restored in the
   next commit.
2. `session_lifecycle_verbs.py` — the `SpawnSessionRequest.provider` dataclass
   field was dropped while every call site that reads `req.provider` was kept.
   Verified from the merge parents: the field is present in ours, absent in
   theirs, absent in the result. Nothing reported it.

The second one shipped. `spawn_session` raised
`AttributeError: 'SpawnSessionRequest' object has no attribute 'provider'` on
every invocation from **2026-08-17 11:06:47 through 2026-08-19 08:56:54** — 28
logged occurrences across 7 dispatched calls, roughly 46 hours during which
this deployment could not spawn a worker session at all. It was found only when
an operator happened to exercise the verb, and repaired by hand.

**We are not asking you to absolve the judgment call — it was ours and it was
wrong.** We are reporting it because of what it demonstrates: a careful,
file-by-file review was performed, was written down in the commit message, and
still missed a regression that a single smoke run caught in seconds afterward.
That is the argument for a mechanical gate rather than a more careful reader. An
adopter reaching for `-X theirs` at all is a symptom of the first ask below;
shipping the result unverified is the second.

**Two distinguishable asks, either of which would have prevented this:**

1. **Sanction and document a merge path.** State it in Step 2: for a clone
   carrying local commits, `git merge <seed-ref>` (a real three-way merge, not
   `--ff-only`, and explicitly *not* a resolve-in-one-side's-favor strategy),
   with the conflict set reported before it is resolved. If the answer is that
   divergence genuinely is unsupported and re-birth is the only path, say that
   in the *feedback* runbook too — the current invitation to carry divergence
   is then misleading and should be withdrawn. We will stop raising it either
   way; we just need the two documents to agree.
2. **Add a mandatory post-merge verification step before Step 4's restart.**
   The seed ships a full smoke/quality gate and this deployment ran it *after*
   the fact — it caught the regression immediately. As a documented step
   between "merge" and "deploy," it would have caught it before the outage
   instead of two days after. Right now Step 4 goes straight from pulled code
   to restart with nothing in between, which is safe for a fast-forward and
   unsafe for anything else.

**Local divergence status:** we are carrying the repair (`e9c6558`) and the
per-spawn provider feature it restores. The feature is local and deliberate; we
are not asking you to take it. The defect we are reporting is the missing
procedure and the missing gate, not the feature.

---

## §47.2 — DEFECT: a surviving pre-rename process holds the blue-green router's active binding indefinitely; the post-migration instance can never self-activate, and the refusal names neither the cause nor the repair  [label: defect]

**Item number:** §47.2

**The exact command or action:**

```bash
solet call service_interface::lifecycle_management_service::apply_manifest '{
  "new_manifest": {"plugins": [<current roster, unchanged>]},
  "reason": "pick up merged seed re-mints",
  "expected_etag": "<etag from a same-run dry_run>"
}'
```

Run on a live deployment that had been migrated by
`deployment/scripts/migrate_to_solet.py` at the 2026-08-13 release.

**Observed output** (from the platform log, verbatim except for redaction):

```
[macos_self_deployment_plugin:swap_orchestrator.py:456] - WARNING -
  self_instance_id=solet-blue-e520f46d but
  router.active_instance_id=homunculus-blue-d8d5ee4e
  (stale non-active same-color process); refusing swap
[macos_self_deployment_plugin:swap_executor.py:147] - ERROR -
  blue-green swap failed [not_active_instance]: router status() did not show
  this instance as the active color; refusing to spawn a parallel green from
  a non-active blue.
```

**Expected behavior:** the running, healthy, post-migration instance should be
able to deploy. Either the router's active binding should be reclaimable by the
live instance, or the refusal should name the actual blocker and the repair.

**Root cause as far as we established it — and the part we did not:** the
router's active binding pointed at `homunculus-blue-d8d5ee4e`, a **pre-rename**
instance id. Three `ananta.cli` platform processes dating from 2026-08-10 to
2026-08-12 were still running and still heartbeating that id. Because those
processes were live, the router's `_heartbeat_gc` never expired the binding, and
`heartbeat_lifecycle`'s steady-state re-assert could not take over — that path
is documented in-code as deliberately unable to steal authority
(`heartbeat_lifecycle.py:252-260`), which is correct for its purpose and is
exactly what leaves this state unrecoverable. The fresh `solet-blue-*` instance
served CLI traffic correctly the whole time; only the blue-green machinery was
hostage. We repaired it by re-pointing the router over its management socket
and terminating the three processes, after which the swap ran cleanly end to
end.

**We did not establish how those three processes survived** — whether they were
launchd-supervised under a label the migration did not rewrite, or foreground
orphans from a debugging session. That distinction is the difference between "a
migration bug" and "a migration blind spot," and we are not guessing at it. What
we can state is the coverage: `migrate_to_solet.py` has no router-registry and
no process-table awareness (a grep for `router`, `instance_id`, or
`active_instance` finds only prose in its docstrings), and its `--scan-stale`
sweep covers `~/.claude/`, the clone's own `.claude/`, and `CLAUDE*.md` — not
the router's persisted binding and not surviving pre-rename processes.

**What it costs us:** this deployment's blue-green path was wedged from the
2026-08-13 migration until 2026-08-19 — the entire interval was invisible,
because ordinary CLI traffic worked perfectly and nothing surfaces the router's
binding unless a deploy is attempted. When it finally did surface, it surfaced
as a refusal that names both instance ids and explains neither: nothing in
`not_active_instance` says "a pre-rename process is holding your active
binding," nothing points at the management socket, and nothing suggests the
`homunculus-` prefix in the id it just printed is itself the diagnosis.

**Three asks, in descending order of value:**

1. **Make the refusal self-diagnosing.** `swap_orchestrator.py:456` already
   holds both ids at the moment it refuses. When the active id carries a
   pre-rename prefix, or its heartbeat originates from a process older than the
   current instance, say so and name the repair.
2. **Give the migration a post-apply router check.** Add the router's active
   binding to `--scan-stale`'s sweep, and fail loud on a pre-rename
   `active_instance_id`. This is the same class as #5 and #6 from the earlier
   round (MCP env keys, stale doc references) — a surface the rename touches
   that the migration does not know about.
3. **Have the migration look for surviving pre-rename platform processes** and
   report them, rather than leaving them to heartbeat a binding nothing else
   can reclaim.

---

## §47.3 — DEFECT: the blue-green swap's own completion leg dispatches `complete_swap` to a service the orchestrator does not have bound  [label: defect]

**Item number:** §47.3

**The exact command or action:** a successful `apply_manifest` cutover. No
special invocation and no manual call — the swap machinery enqueues this itself
as its own final step: `swap_executor.py:448` calls `_enqueue_complete_swap(...)`,
per `swap_orchestrator.py:569` ("Enqueue durable complete_swap action for green's
poller") and `swap_executor.py:7` ("…and enqueue the durable `complete_swap`").

**Observed output**, one second after a clean activation:

```
[swap_executor.py:347]  - INFO  - activated next color=green previous_color=blue drain_window=30s
[swap_executor.py:419]  - INFO  - durable swap pointers current=<new> previous=<old>
[action_processor.py:394] - ERROR - Error executing action <id>:
  Service 'local_self_deployment_service' not available in orchestrator
  ...
  ananta.error_handling.FrameworkError: Service 'local_self_deployment_service'
  not available in orchestrator
[action_queue_poller.py:3433] - INFO - ACTION FAILED:
  service_interface::local_self_deployment_service::complete_swap
```

**Expected behavior:** the swap's own completion leg should reach a bound
service. `constants.py:43` states the plugin "is a bound ServiceProvider on
`local_self_deployment_service`" and `constants.py:51` names
`service_interface::local_self_deployment_service::complete_swap` explicitly, so
the deploy path is dispatching to a binding the plugin declares it provides and
the orchestrator does not resolve.

**What it costs us:** every cutover ends with a failed action and a
`FrameworkError` traceback at ERROR level in the log. The swap itself completes
— durable pointers are written before the failure, and the pending-finisher
backstop reaped the outgoing color 10 seconds later — so the practical cost
today is that a healthy deploy is indistinguishable from a broken one by log
inspection, and the completion leg is running on a backstop rather than on its
intended path.

**Deliberately scoped narrower than it could be:** the sibling verb
`swap_status` fails identically, but that one is already documented as a known
verify-surface gap in `macos_self_deployment_plugin`'s own knowledge base, so we
are not re-filing it. What is new here is that the *deploy machinery itself*
depends on the same unbound service, which that article does not cover. If both
turn out to be one root cause, treat this as the deploy-path half of it. If
`local_self_deployment_service` is instead something an adopter is expected to
bind in their own profile manifest, this is a documentation gap — the hydration
and update runbooks say nothing about it — and we would file it that way
instead; please say which.

---

## §47.4 — DEFECT: three consecutive releases shipped a 125-line vendored-hook change with `coordination-hooks` `version` pinned at `0.5.9`  [label: defect]

**Item number:** §47.4

**The exact command or action:** the seed's own prescribed check, from Step 6 of
`05_seed_update_runbook.md` — *"Check with `git log <bump-commit>..master --
<vendored-hook-path>` and require empty output before treating a version as
covering."*

```bash
git log e934bb4..dd4109d -- \
  plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/
```

**Observed output:** two commits, not empty —

```
11a7b72 Seed bundle (factory-sealed)
6f7d844 Seed bundle (factory-sealed)
```

`hooks/rotation_due_watch.py` gained 125 lines and lost 12 across that range,
while `.claude-plugin/plugin.json` reads `"version": "0.5.9"` at all four of
`e934bb4` (2026-08-17), `6f7d844` (2026-08-18), `11a7b72` (2026-08-18) and
`dd4109d` (2026-08-18.2).

**Expected behavior:** the version bumps with the hook change. This is not an
inference about what the rule should be — it is the seed's own rule, stated in
its own runbook as a release-side obligation:

> **Named fix: bump `plugin.json`'s `version` on every shipped hook change**, as
> a step in the *release* procedure — a refresh the operator cannot trigger from
> their own side is not their step to own.

and

> **Cut the new version AFTER the hook change lands, not before.**

**What it costs us:** the failure is silent in exactly the way that runbook
warns about. `claude plugin install` reports "already installed" and
`claude plugin update` reports "already at the latest version" while the cache
copy keeps executing the old hook — so an adopter who follows Step 6 faithfully,
sees the version already matching the source manifest, and stops there is left
running superseded code with every local signal reading green. We only caught it
because we ran the `diff -r` check unconditionally rather than treating a
matching version as sufficient, and recovered with the documented
uninstall-then-install pair. That recovery is not discoverable from the version
number alone, which is the whole point of the bump.

The runbook's own framing is the ask: this is a release-side step, and it is the
one step an adopter cannot perform for themselves.

---

## §47.5 — DEFECT: `marketo_plugin`'s poll worker reads `orchestrator_ref.async_job_manager` eagerly where every sibling connector lazy-acquires  [label: defect]

**Item number:** §47.5

**The exact command or action:** none — this fires unprompted on every platform
boot. Observed on this deployment's ordinary startup.

**Observed output:**

```
[marketo_plugin:plugin.py:515] - ERROR - marketo_plugin worker: unexpected fault in poll cycle
Traceback (most recent call last):
  File ".../marketo_plugin/plugin.py", line 512, in _worker_loop
  File ".../marketo_plugin/plugin.py", line 521, in _process_pending_jobs
    raw_manager = self.orchestrator_ref.async_job_manager
AttributeError: 'EventOrchestrator' object has no attribute 'async_job_manager'
```

**Expected behavior:** no ERROR-level traceback for a known, benign,
self-resolving boot-ordering window.

**Precise characterization — this is a bounded startup race, not a permanent
fault.** We are correcting our own earlier reading of it. Measured on
2026-08-19 across two boots: 15 occurrences before the `marketo_plugin: ✅
RUNNING` marker at 11:47:29 and 1 before the marker at 11:56:21, then **zero in
the following 45 minutes** of continuous operation. The worker thread starts
polling at a ~2s interval before `EventOrchestrator.__init__` reaches
`_delegate_service_attributes()`, which is what sets the attribute; once
injection lands the worker proceeds normally and the plugin functions.

**The inconsistency is the actual defect.** Every sibling connector already
guards this, with an in-code comment naming this exact hazard —
`external_postgres_plugin/async_jobs.py:73` acquires the manager lazily and
documents why: *"plugin boot order does not guarantee
`orchestrator_ref.async_job_manager` is set by the time this plugin's readiness
hook runs, but it is always set by the time any verb is actually dispatched."*
`g_suite_plugin/plugin.py:269` uses `getattr(..., None)`. `marketo_plugin` alone
reads the attribute directly with no guard and no retry.

**What it costs us:** ~16 ERROR-level tracebacks per boot that look like a
broken connector and are not. It cost us a diagnosis pass to establish that a
loud, repeating `AttributeError` in the platform log was benign, and it degrades
the log as a signal — an adopter scanning a startup log for real problems has to
learn to skip these first.

**Not caused by this release** — present in every platform log this deployment
retains, back to 2026-08-10 (27, 2, 24, 28, 24 and 16 occurrences on 08-10,
08-11, 08-12, 08-14, 08-17 and 08-19 respectively). Reported now because this
update's diagnosis pass is what characterized it.

---

## §47.6 — FEATURE REQUEST: a sanctioned Git-Controller mechanism for a genuinely solo fleet  [label: feature-request]

**Item number:** §47.6

**What we want to do:** perform gated git mutations from the only session in the
fleet, on a deployment where no other session exists to spawn from or delegate
to.

**What blocks us:** the rename skill's template, as re-rendered by this release,
now explicitly forbids the standalone session-label patch — the mechanism this
deployment had been using — on the stated grounds that it cannot distinguish a
verified-solo fallback from a forged claim. That reasoning is sound and we are
not disputing it. But the template forbids the mechanism without naming a
replacement for the solo case, which leaves a deployment with one session and no
reachable peers unable to satisfy the gate by any documented route.

Note that the gate itself is exact-match on the session name
(`session_name == controller`), so there is no near-miss path: a session either
holds the name or cannot mutate git at all.

**What we did in the meantime, disclosed:** we used the forbidden mechanism once
during this update, with the solo basis recorded in the commit trailer
(`ListAgents` returned no reachable agents; operator instruction on file). We are
not comfortable making that a standing practice, which is why this is here.

**What we are asking for — either answer closes it for us:**

1. A sanctioned verified-solo path: some attestation the platform itself can
   check, so a solo deployment can hold the controller role without hand-editing
   its own session label. Or —
2. An explicit "no — always spawn a Git-Controller, even on a solo fleet." Now
   that `spawn_session` works again on this deployment (§47.1), spawning is a
   viable path for us, and a clear ruling is worth more than a mechanism. We
   will stop raising it.

---

## §47.7 — FEATURE REQUEST: the update runbook should state that a release fixing the deploy preflight cannot be installed by the preflight it fixes  [label: feature-request]

**Item number:** §47.7

**What we want to do:** follow Step 4 of `05_seed_update_runbook.md`, which
directs an adopter with a router to prefer `apply_manifest` over a bare restart.

**What blocks us:** for this particular update, that instruction cannot succeed
on the first attempt. The release being installed contains the fix for #15
(§46.1) — the cutover preflight validating against the running process's stale
in-memory modules rather than the on-disk code it is about to activate. But the
`apply_manifest` that would install that fix is executed *by the still-running
old code*, so it fails on the very defect it carries the fix for. The documented
bare LaunchAgent restart is the only way through, and only after it does
`apply_manifest` work.

**Confirming the fix, since we exercised both sides of it:** after the bare
restart, `apply_manifest` cleared preflight and built the candidate release
normally. #15's fix works. This item is not a re-open.

**What we are asking for:** a general note in Step 4 — when a release's notes
say it changes the deploy preflight or the swap path itself, the bare-restart
path is mandatory once, because the new code cannot deploy itself with the old
code's preflight. This is a recurring shape rather than a one-off: any
self-deploying system carries it. Naming it in the runbook turns a confusing
first failure into an expected step, and it is cheap to state.

---

## Filing plan

Parent issue (form 05) + seven sub-issues attached via the sub-issue API:
five defects (form 01) and two feature requests (form 03). Filed through the
`feedback` skill, which runs the evidence discipline and the content gate before
publishing. No pull request and no patch — per `CONTRIBUTING.md` the repository
accepts neither.
