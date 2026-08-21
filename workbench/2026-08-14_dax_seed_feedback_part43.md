# Seed feedback Part 43 (dax) — FILED 2026-08-14

**Filed as:** parent https://github.com/solet-public/macos-bizops/issues/7
(Part 43), sub-issues #8 (§43.1) and #9 (§43.2), attached via the sub-issue
API. Filed via `gh issue create` (CLI) at the operator's explicit direction,
with the filing-note disclosure in each body; content gate run before
submission. Responses arrive as issue closures and repository releases —
subscribe to releases rather than polling.

Channel: `solet-public/macos-bizops` (resolved from `git remote get-url origin`).
Release measured against: clone HEAD `68adf8b` (seed bundle `e592c67` merged at
`d406d5c`) / RELEASE_NOTES.md "2026-08-14 — Content hygiene, fleet-worker
reliability, and a default deny for blocking choice prompts".

Round shape: two defects found in one fleet-verification exercise → parent
round issue (form 05) + two defect sub-issues (form 01).

Filing note (goes in each body): filed via the `gh` CLI rather than the web
issue form — unattended filing at the operator's explicit direction; all of
the form's required fields are reproduced by hand below, and the content gate
was run against this exact text before submission.

---

## Parent — "Part 43 — feedback round from dax"  [label: feedback-round]

**Part number:** Part 43

**Release this round was measured against:** HEAD `68adf8b` (seed bundle
`e592c67`) / RELEASE_NOTES.md 2026-08-14 release.

**What this round is about:** After adopting the 2026-08-14 release (and the
solet rename), this deployment verified the managed-fleet path end to end —
`spawn_session` (headless host, a small/fast model at low effort) →
`drive_session` → `terminate_session` — on a machine governed by an
organization-managed Claude Code policy that restricts hooks to plugin sources
(`strictPluginOnlyCustomization: ["hooks"]`). The lifecycle verbs all function,
but the exercise surfaced two distinguishable defects: spawned workers
silently receive none of their spawn-injected hooks under such a policy
(§43.1), and `drive_session` reports a channel-unavailable error on a dispatch
it actually delivers (§43.2, observed while the row was stuck in the state
§43.1 causes).

**Items in this round:**

| Item | Class | Summary |
|---|---|---|
| §43.1 | defect | Spawn-injected worker hooks silently stripped under a plugin-only hooks policy; workers never register |
| §43.2 | defect | `drive_session` returns `unsupported_on_host` while actually delivering the dispatch and re-arming `report_by` |

**Items carried forward from earlier rounds:** None filed as issues yet. This
deployment's Part 42 (2026-08-12) was drafted under the previous
PR-carried-document convention and was never delivered before the channel
moved to GitHub issues; its items will be re-checked against the current
release and re-filed individually if still valid, rather than referenced here.

**Content gate:** run against this text — no personal identifiers, no employer
or business specifics, no credentials.

---

## §43.1 — Spawn-injected worker hooks silently stripped under a plugin-only hooks policy; workers never register  [label: defect]

**Item number:** §43.1

**The exact command or action:**

```
solet call plugin::agent_messaging_plugin::spawn_session '{
  "role_class": "ephemeral", "role_name": "Haiku-Verify",
  "lane_id": "verify-solet-fleet-20260814",
  "brief_ref": "<scratch>/haiku_verify_brief.md",
  "work_class": "analysis_deliverable",
  "budget_line": "operator-verification-20260814",
  "host": "headless", "model": "claude-haiku-4-5-20251001", "effort": "low",
  "report_by_seconds": 600, "ttl_seconds": 1200,
  "spawned_by_role": "Operator", "directed_by": "Operator"}'

# then, after the worker's automatic first turn completed:
solet call plugin::agent_messaging_plugin::session_status \
  '{"agent_instance_id": "agi-5399ceff29d7a341eec6a84102729e4b"}'
```

**Observed output:**

```
spawn_session -> success: agent_instance_id agi-5399ceff..., host headless,
host_ref <pid>, first_turn_delivered: true, lifecycle_state: "spawning"

Worker process argv (ps, trimmed; paths generalized): claude --input-format
stream-json --output-format stream-json ... --setting-sources project
--settings {"permissions": {"deny": ["Agent","Task"]}, "hooks": {"PreToolUse":
[headless_tool_allowlist_gate.py], "SessionStart":
[capture_session_mapping.py, check_messages_reminder.py,
role_binding_reminder.py], "UserPromptSubmit": [step_zero_reminder.py,
check_messages_reminder.py], "PostToolUse": [heartbeat_report_alive.py,
rotation_due_watch.py], "Stop": [wake_waiter.py]}} --model
claude-haiku-4-5-20251001 --effort low ...

The worker's first turn completed normally ("up and reachable, standing by").
But NONE of the injected hooks executed:
- worker transcript contains zero hook events;
- APP_HOME/data/session_claude_mapping_spool/ is EMPTY (no mapping record
  ever written — capture_session_mapping never fired);
- session_status remains lifecycle_state: "spawning", agent_session_id: null,
  agent_id: null, indefinitely after the first turn.

Isolation test (same machine, plain interactive CLI): a one-off session
launched with a marker hook passed via the --settings flag; the marker did
NOT fire, while hooks from an installed, policy-allowlisted PLUGIN fired in
the very same session. The managed policy in effect carries
strictPluginOnlyCustomization: ["hooks"].
```

**Expected behavior:** Spawned workers get their coordination hooks
(registration, allowlist gate, heartbeat, rotation watch, idle-wake,
reminders) — or, where the environment makes that impossible, the spawn fails
or warns loudly. The 2026-08-14 release itself establishes that principle for
hook-file resolution (`WorkerHookResolutionError` → `HostCannotSpawnError`,
"a clear, attributable refusal instead of an unexplained hang"); a policy
stripping every injected hook deserves the same loudness rather than a worker
that spawns healthy-looking and silently never registers.

**Release measured against:** HEAD `68adf8b` (seed bundle `e592c67`) /
RELEASE_NOTES.md 2026-08-14 release.

**Diagnosis or inference (marked as inference):** Enterprise-managed Claude
Code policies can carry `strictPluginOnlyCustomization: ["hooks"]`, which (per
the isolation test) strips hooks from ALL non-plugin sources — settings files
AND the `--settings` CLI flag the host adapters (`headless_adapter.py` /
`tmux_adapter.py`) use to inject the worker hook set. Compounding it, workers
run `--setting-sources project`, so user-scope `enabledPlugins` never reaches
them — an installed, allowlisted plugin copy of these hooks doesn't load in
workers either. And two of the affected hooks (`capture_session_mapping`,
`headless_tool_allowlist_gate`) are deliberately not wired into the plugin's
own `hooks.json`, so no local configuration can restore registration. Net:
under this policy class there is no deployment-side workaround; the fleet
still functions mechanically (spawn/drive/terminate, driver-pipe delivery)
but registration, heartbeats, idle-wake, the allowlist gate, and worker
reminders are all silently dead.

**Local fix or divergence carried:** None — running degraded, not patched.
Request: an arming path for worker hooks that survives plugin-only hook
policies (e.g. resolving them through an installed plugin the worker actually
loads, or an adapter option), plus loud spawn-time detection of the
stripped-hooks condition. We would also accept "unsupported environment,
documented" as a disposition — but silent degradation is the defect either way.

**Content gate:** ticked after re-reading this text.

---

## §43.2 — `drive_session` returns `unsupported_on_host` while actually delivering the dispatch and re-arming `report_by`  [label: defect]

**Item number:** §43.2

**The exact command or action:**

```
solet call plugin::agent_messaging_plugin::drive_session '{
  "agent_instance_id": "agi-5399ceff29d7a341eec6a84102729e4b",
  "text": "Execute your brief now: read <scratch>/haiku_verify_brief.md and do
exactly what it says (write the two-line result file), then reply done. ..."}'
```

(Worker state at the time: headless host, process alive, row stuck in
`lifecycle_state: "spawning"` due to §43.1 — never registered.)

**Observed output:**

```
error_message: {'type': 'agent_messaging_error', 'code': 'unsupported_on_host',
'message': "host 'headless' for 'agi-5399ceff...' has no driver channel
(degenerate driver, or the tracking process's memory doesn't recognize this
host_ref, e.g. after a restart) — driver-channel verbs (clear/compact/drive)
are unavailable; use the manual equivalent for this session.", ...}
result: {"action_status": "completed", "data": {"lifecycle_state": "spawning",
"unparked": false}, "success": true}

Yet the dispatch WAS delivered and executed: the worker's transcript shows the
exact drive text arriving as a user turn at the same second the verb returned
(00:27:21Z), followed by the worker reading the brief, writing the requested
file, and replying "done". And session_status afterwards shows report_by
re-armed to dispatch-time + report_by_seconds (00:37:21Z) — the verb's own
re-arm side effect ran too.
```

**Expected behavior:** Either the verb fails cleanly with no side effects, or
it succeeds and says so. An error stating the driver channel does not exist
must not accompany a successful delivery through that channel plus a
`report_by` re-arm — a coordinator acting on this error (e.g. re-spawning per
the dispatch-worker repair guidance, since the "manual equivalent" it points
to doesn't exist for a headless pipe) would double-dispatch work the worker
is already doing.

**Release measured against:** HEAD `68adf8b` (seed bundle `e592c67`) /
RELEASE_NOTES.md 2026-08-14 release.

**Diagnosis or inference (marked as inference):** Looks like an ordering /
misattribution issue on the drive path when the row is still in `spawning`:
the channel dispatch and the `report_by` re-arm complete, then a later step
(plausibly a lifecycle transition that has no legal `spawning →` drive edge)
raises, and the failure surfaces under the generic no-driver-channel error
text. Only observed with the row stuck in `spawning` (§43.1's condition); not
re-tested against a registered worker.

**Local fix or divergence carried:** None.

**Content gate:** ticked after re-reading this text.
