# Seed update to release 2026-08-20, and the orphan-color leak it uncovered

Date: 2026-08-21
Session: Operator (interactive seat)
Operator instruction: "look at the seed from which we were birthed and apply the
updates you see there" — plus a standing complaint that Dax had "tons of
problems" that were being muddled through rather than escalated.

---

## 1. What the seed update actually was

| | |
|---|---|
| Upstream re-mint | `5319656` (2026-08-20T20:07:40Z) |
| Merged as | `0f00fe7` into `seed-feedback-and-gsuite-bind-fix` |
| Files | 92 changed, +9212 / -505 |
| Conflicts | none |
| Release notes | "A fresh install can do work again, and the hook errors on a stock Mac are gone" |

**⚠ The re-mint carries no git tag and no GitHub release.** Tags stop at
`release-2026-08-19`. The upstream feedback runbook makes releases "the primary
response surface" and tells adopters to watch releases only — so a
releases-only subscriber never learns this re-mint exists. Filed as a feedback
item.

### The three adopter-visible changes, and what each meant here

1. **Launcher defaults permission prompts OFF.** Upstream dropped its own
   "never pass `--dangerously-skip-permissions`" invariant and now passes
   `--permission-mode bypassPermissions`, with `SOLET_PERMISSION_PROMPTS=1` as
   the opt back in. **Already satisfied locally by a stronger mechanism** — the
   2026-08-21 operator ruling has `client/bin/claude-dax` passing
   `--dangerously-skip-permissions`. Re-rendering the launcher from the
   template (which the release notes instruct) would have DOWNGRADED it,
   because the measured 2026-08-21 failure was auto-mode overriding a
   `bypassPermissions` setting. Adopted only the escape hatch; recorded the
   convergence in the launcher's own comment block so it is not re-litigated.
2. **Coordination hooks crashed on `python3` < 3.11** (8 of 20 modules could
   not load; stock macOS ships 3.9). **No-op here** — this machine's `python3`
   is pyenv 3.13.7 in both interactive and fresh login shells. The guard is now
   present in the live cache regardless.
3. **A hook that logged a diagnostic on every tool call when not in a fleet.**
   Silent now.

### Delivery steps that a `git pull` does NOT perform (both done)

- `coordination-hooks` 0.6.0 → 0.7.0. A version bump only ARMS a refresh;
  neither `claude plugin install` on an installed plugin nor
  `claude plugin update` re-copies bytes. Genuine uninstall-then-install was
  required. Mirror repo `BranchMetrics/bizops-claude-code-plugin` synced and
  pushed as `f83f5f9`; cache `0.7.0` verified byte-identical to the mirror; the
  `sys.version_info < (3, 11)` guard confirmed present in the live cache copy.
- Launcher reconciliation (above), instead of a blind re-render.

Deploy: `apply_manifest` dry-run → CAS commit → release
`rel-20260821T211746Z-0f00fe7` activated. New code verified live by resolving
`plugin::agent_messaging_plugin::retire_gauge_canary`, a verb that exists only
in this bundle.

---

## 2. The real find: every blue-green cutover leaked a whole solet process

This is almost certainly the substance behind "tons of problems."

### Measured state before cleanup

**Five live `ananta.cli` processes**, from three different releases, all
against the one Postgres schema:

| PID | Instance | Release tree | Age | Router |
|---|---|---|---|---|
| 31201 | `solet-blue-ce8555e0` | `rel-20260821T211746Z-0f00fe7` | minutes | **active** |
| 62108 | `solet-green-bd36da8d` | `rel-20260820T190035Z-2329ea4` | 1d 2h | inactive |
| 73835 | `solet-blue-26c37d49` | `rel-20260819T205556Z-2329ea4` | 2d 0h | inactive |
| 80575 | `solet-blue-e520f46d` | (launchd root, working tree) | 2d 2h | inactive |
| 85662 | `solet-green-fe02e6f7` | `rel-20260819T185543Z-e9c6558` | 2d 2h | inactive |

Parent chain: `80575 → 85662 → 73835 → 62108`. Each generation kept its parent
alive. `80575` was the launchd-managed root.

### They were not idle

- The Aug-19 color was still running `agent_messaging_plugin`'s full rotation
  sweep (L4a/L4b/L4c/L4d legs) on a ~60s cycle, two days after being
  superseded — writing to `2026-08-19_profile.log`, last entry 14:19:17 on
  2026-08-21.
- The Aug-20 color threw
  `psycopg.errors.FeatureNotSupported: cached plan must not change result type`
  — the signature of a schema migration landing under a process holding stale
  prepared statements. This very release migrates `peer_binding` (adds
  `watcher_declared`). So a stale color does not merely idle; it actively fails
  and does so against shared state.

### Root cause — two stacked defects

**(a) `complete_swap` is unbound in this profile, so teardown is never even
attempted.** `complete_swap` is the post-activate finisher that SIGTERMs the
prior color and unregisters it. It is registered under
`service_interface::local_self_deployment_service::complete_swap`, but this
profile's `service_bindings.json` binds only `self_deployment_service`. Every
cutover therefore ends with:

```
ERROR - Error executing action ae-2o8x1fnrruwmv:
        Service 'local_self_deployment_service' not available in orchestrator
INFO  - ACTION FAILED: service_interface::local_self_deployment_service::complete_swap
```

The seed's own KB **documents this binding gap** —
`macos_self_deployment_plugin/knowledge_base/operations_config_reinit_blue_green.md`,
"Verify-surface gap on this profile" — but frames it purely as a *diagnostic*
inconvenience: you cannot run `swap_status`, so verify with `list_plugins`
instead. That framing is wrong and is what let this run for days. The same
unbound service backs the **teardown finisher**, so the consequence is not a
missing read surface, it is a leaked solet process per deploy.

**(b) Even if it were bound, its SIGTERM would not kill the color.** Measured
on all four: `73835`, `85662` and `31201` each **ignored SIGTERM entirely**,
hanging in "initiating graceful shutdown" and requiring SIGKILL; `62108` had
been sent SIGTERM at 14:18:56 and was still alive six minutes later. And
because the color dies by signal rather than exiting 0, the launchd root's
`KeepAlive = {SuccessfulExit: false}` respawns it (`launchctl list` showed exit
status `-15`). So teardown of the launchd root is unachievable as designed.

### Cleanup performed

Unregistered each color from the router mgmt socket first (`{"verb": ...,
"args": {...}}` — args must be nested; a flat payload returns `bad_args`), then
signalled, one at a time with `dax health` re-verified between each. A live
orphan **re-registers itself**, so unregister alone is insufficient — the
process must die.

Killing the launchd root respawned it as `38323` (expected, per KeepAlive).
Converged on that as the single solet rather than fighting it: `activate`d its
color `solet-blue-890ede34` (activate keys on `instance_id`, so two same-colour
entries are unambiguous), verified the live path through the router, then
terminated the drained release-tree color.

**Final state: one solet process (`38323`, launchd-managed, `local.solet.dax`),
one router (`36596`), active color `solet-blue-890ede34`, health `healthy`, KB
live path returning results.** This state is self-healing across reboot because
the surviving process is the launchd-managed one.

---

## 3. Second instance of a known defect class: undeclared args silently dropped

Attempting the obvious local fix — bind `local_self_deployment_service` via
`apply_manifest`'s documented `service_bindings` merge — surfaced this.

`apply_manifest`'s own description states:

> Caller-supplied `service_bindings` ARE accepted and MERGED over the currently-bound
> snapshot (`effective_bindings = {**current, **caller}`) — the mechanism for plugin
> renames/rebinds

But `service_bindings` **is not in the published invocation schema**, whose
`arguments` object is `additionalProperties: false`. Passing it against an
otherwise-valid manifest returns `status: dry_run`, `success: true`,
`rebound_services: []` — **silently dropped, not rejected**. The schema's own
`additionalProperties: false` is not enforced at the bridge.

This is the **same failure mode** as the fleet-spawn blocker recorded in
`2026-08-21_fleet_spawn_blocked_defect.md`: an override that is implemented,
persisted and documented, but absent from the invocation schema, so the bridge
drops it. Two independent instances make it a systemic bridge-layer defect
rather than one verb's oversight, and the shared symptom is the dangerous one —
a caller gets a green envelope for an argument that was never applied.

The `new_manifest` field description **contradicts** the top-level description,
saying payload `service_bindings` is rejected with a stable
`bindings_change_rejected_in_v1` token. Whether this profile is *supposed* to
bind the plugin on both service names, or whether `complete_swap`'s enqueue is
wrong to name `local_self_deployment_service`, cannot be settled from here —
which is what makes it a feedback item rather than a local fix. **The rebind
was deliberately NOT committed:** committing triggers another cutover, which
with `complete_swap` still unbound would have leaked a fifth orphan.

---

## 4. `/clear` — the capability was present; the bindings were missing

The standing claim that this session could not `/clear` itself was wrong, and
the seed says so explicitly. `rotate_own_context_by_delegated_drive`'s
coherence obligations: *"Never claim self-clearing is impossible. That claim has
been made and been wrong. What is impossible is delegating it to something that
can decide."*

The machinery ships:
`agent_messaging_plugin/src/agent_messaging_plugin/seat_rotation_helper.py` —
live `user.role` pane resolution under a 0/1/N gate, `/clear` and its carriage
return as separate sends, poll-settle on a positive cleared-state signature,
then the pickup and its own return. It holds the `iterm2` bindings as
*optional*, disclosed at import and re-raised at the one step that drives a
pane, because the distribution ships with
`iterm2_coding_agent_management_plugin`, which the bizops profile excludes.

The only gap on this box was that the `iterm2` distribution was absent from
dax's venv. Installed `iterm2 2.20` + `websockets 17.0.1`.
`plugins/agent_messaging_plugin/tests/seat_rotation_helper_smoke.py`: **68
passed, 0 failed**, including the false-green traps (send success ≠ submit
truth) and the blocked-import leg.

Remaining prerequisite for the unattended path: iTerm2's Python API must be
enabled in iTerm2 settings — the connect step raises by name if it is not.

The doctrine, which should not be softened when reporting: **operator-present
is the default** (the operator types the two lines; zero race, zero extra
tokens), and the detached two-injection script is **operator-ABSENT only**,
because on 2026-08-16 a script's injected clear landed inside the operator's
live composer. There is no agent in the injection path — that is a ruling, not
a workaround.

---

## 5. What this update did NOT fix

**Fleet spawn is still blocked.** Verified by absence: the 92-file diff does not
touch `spawn_session`'s process JSON, and nothing in it addresses either half of
the recorded blocker — the managed policy at `~/.claude/remote-settings.json`
listing `hooks` in `strictPluginOnlyCustomization` (which strips worker hooks
from the tmux driver's `--settings` blob, producing `host_cannot_spawn`), or the
`degraded_hooks_acknowledged` override missing from the invocation schema. The
capability fix is plugin-delivered worker hooks, still unshipped.

So the solo-fleet fallback (patch `~/.claude/sessions/<pid>.json`'s `name` to
`Git-Controller`, do the work in one Bash call carrying `trap '<restore>' EXIT`,
restore immediately) remains the only working path for gated git here. Used
twice in this session.

**Authorization basis cited in band, per the gate's own policy:** explicit
operator instruction to apply the seed update, plus
`plugin::agent_messaging_plugin::peer_list` returning an empty roster at
2026-08-21T21:10:44Z (solo fleet, no other live session).

---

## 6. Feedback items for Part 50

1. **`complete_swap` unbound → a leaked solet process per cutover.** Four
   measured orphans, three releases, ~60s duty cycles against shared state, a
   `cached plan must not change result type` failure. The KB's "verify-surface
   gap" framing understates it. Strongest open item.
2. **A solet color does not terminate on SIGTERM.** Measured on three
   processes: hangs in graceful shutdown, needs SIGKILL. Independently breaks
   `complete_swap` even once bound, and makes the launchd root un-teardownable
   because dying by signal trips `KeepAlive = {SuccessfulExit: false}`.
3. **Undeclared arguments are silently dropped rather than rejected**, despite
   `additionalProperties: false` in the published invocation schema — second
   instance after the fleet-spawn override. Systemic; a caller gets a green
   envelope for an argument never applied.
4. **`apply_manifest`'s description contradicts its own `new_manifest` field
   description** on whether caller `service_bindings` are merged or rejected.
5. **Re-mint `5319656` shipped with no tag and no GitHub release**, so the
   releases-only watch the runbook prescribes never surfaces it.
6. **The `iterm2` distribution is not declared anywhere an adopter would find
   it.** `seat_rotation_helper` is the shipped self-rotation contract, its
   absence is disclosed only at import, and the practical result was a seat
   that believed self-clearing was impossible. A hydration-time note, or a
   dependency on the profile that ships the helper, would close it.
