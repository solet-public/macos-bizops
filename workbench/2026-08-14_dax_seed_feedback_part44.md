# Seed feedback Part 44 (dax) — re-verification and delivery of the undelivered Part 42 — FILED 2026-08-14

**Filed as:** parent https://github.com/solet-public/macos-bizops/issues/10
(Part 44), sub-issues #11 (§44.1), #12 (§44.2), #13 (§44.3), attached via the
sub-issue API. Filed via `gh issue create` (CLI) at the operator's explicit
direction, with the filing-note disclosure in each body; content gate run
before submission.

Channel: `solet-public/macos-bizops` (resolved from `git remote get-url origin`).
Release measured against: clone HEAD `68adf8b` (seed bundle `e592c67` merged at
`d406d5c`) / RELEASE_NOTES.md "2026-08-14" release.

Background: this deployment's Part 42 (drafted 2026-08-12 under the previous
PR-carried-document convention) was never delivered — the previous repository
was archived before the pull request was opened, and the channel moved to
GitHub issues. Every item was re-verified against the currently adopted
release before re-filing here; one item drafted as a defect turned out to be
already fixed upstream and is filed as a closure confirmation instead.

Round shape: two closure confirmations + one feature request → parent round
issue (form 05) + three sub-issues (forms 04, 04, 03).

Filing note (goes in each body): filed via the `gh` CLI rather than the web
issue form — unattended filing at the operator's explicit direction; all of
the form's required fields are reproduced by hand below, and the content gate
was run against this exact text before submission.

---

## Parent — "Part 44 — feedback round from dax"  [label: feedback-round]

**Part number:** Part 44

**Release this round was measured against:** HEAD `68adf8b` (seed bundle
`e592c67`) / RELEASE_NOTES.md 2026-08-14 release.

**What this round is about:** Delivery of this deployment's Part 42, which was
drafted 2026-08-12 under the previous PR-carried-document convention but never
delivered (the previous repository archived before its pull request was
opened). All three items were re-verified against the currently adopted
release before filing: one is the closure confirmation it always was (§44.1),
one was drafted as a defect and is **already fixed** in the current release so
it is filed as a closure confirmation of an unreported defect (§44.2), and one
feature request is still valid with updated evidence (§44.3). Part 42's
number is considered consumed by the undelivered draft and is not reused.

**Items in this round:**

| Item | Class | Summary |
|---|---|---|
| §44.1 | closure confirmation | `step_zero_reminder` hookEventName mismatch fixed in 0.5.2 — verified; local divergence dropped (drafted as §42.1) |
| §44.2 | closure confirmation | Git-Controller gate over-match on git substrings in argument data — already fixed by the lexer/walker in the current release (drafted as defect §42.2, never filed) |
| §44.3 | feature request | A spawned worker's local session name is its lane id, not its `role_name`, so a spawned Git-Controller can't pass the git gate without a drive→rename round-trip (drafted as §42.3, still valid) |

**Items carried forward from earlier rounds:** Part 43 (#7, #8, #9) is open
and current — listed for continuity, not re-argued.

**Content gate:** run against this text — no personal identifiers, no employer
or business specifics, no credentials.

---

## §44.1 — CLOSURE: `step_zero_reminder` hookEventName mismatch fixed in 0.5.2; local divergence dropped  [label: closure-confirmation]

**Item number:** §44.1

**The item being confirmed:** The `step_zero_reminder` hookEventName-vs-wiring
mismatch reported in this deployment's Part 41, acknowledged in the
coordination-hooks 0.5.2 hotfix notes and in that hook's own docstring.
(This confirmation was drafted 2026-08-12 as §42.1 in a round that was never
delivered — the previous repository archived before its PR opened — and is
re-verified and filed now.)

**Verified outcome:** Fixed. Both hook variants read `hook_event_name` off
stdin and echo it, and the suggested manifest-consistency guard shipped
(`check_manifest_bound_events_echo`). We had carried a local interim fix for
the same bug; on the 2026-08-12 re-mint it conflicted with upstream's
canonical fix (same functional change, different shape — upstream lifts the
reminder text to a module-level constant), and we resolved in favor of
upstream and **dropped the local divergence**. No action needed — thank you.

**How you verified it:**

```
grep -n "hook_event_name" plugins/github_midwife_plugin/claude_plugin/\
coordination-hooks/hooks/step_zero_reminder.py
# -> payload.get("hook_event_name") — present at the currently adopted HEAD
# Originally verified 2026-08-12 by reading origin/main's
# step_zero_reminder.py/.js at seed HEAD 040d7d4; re-confirmed at 68adf8b.
```

**Release you verified on:** originally seed `040d7d4` (2026-08-12 re-mint);
re-confirmed at HEAD `68adf8b` / RELEASE_NOTES.md 2026-08-14 release.

**Content gate:** ticked after re-reading this text.

---

## §44.2 — CLOSURE: Git-Controller gate over-match on git substrings inside argument data — already fixed in the current release  [label: closure-confirmation]

**Item number:** §44.2

**The item being confirmed:** A defect this deployment drafted 2026-08-12 as
§42.2 but **never filed** (the round was never delivered): on the 2026-08-12
release (seed HEAD `040d7d4`), `git_controller_gate.py`'s Bash `PreToolUse`
path classified a git invocation from any git-subcommand-shaped substring in
the command string — including inside quoted *data* arguments to non-git
programs. Driving a work brief containing the phrase `git pull` to a spawned
worker (a `python3`/CLI invocation with the brief as a JSON `text` argument)
was blocked as "banned git invocation", which broke the platform's own
spawn→drive Git-Controller workflow. The workaround was writing briefs to a
file outside the repo. Filing as a closure so nobody re-reports it: the
current release's lexer/token-walker rewrite already fixes it.

**Verified outcome:** Fixed at the currently adopted release. The gate now
walks tokens and classifies `git` only in command position (including
`bash -c`/`eval`/`$(...)` wrapping, per `_git_controller_walker.py`); git
substrings inside quoted data arguments no longer trip it, while real
mutations still block and read-only git still passes.

**How you verified it:** Ran the gate directly with three PreToolUse payloads
on stdin, `GIT_CONTROLLER_NAME` set, from a non-controller (unbound) session
context:

```
1) command = a CLI dispatch whose quoted JSON text argument contains
   "... run git pull re-mint integrate ... then git merge the release ..."
   -> exit 0 (passes; this exact class was blocked on the 2026-08-12 release)
2) command = git pull origin main
   -> BLOCKED, exit 2, "subcommand 'pull' is not in the read-only allowlist"
3) command = git status
   -> exit 0 (read-only allowlist)
```

**Release you verified on:** HEAD `68adf8b` (seed bundle `e592c67`) /
RELEASE_NOTES.md 2026-08-14 release.

**Content gate:** ticked after re-reading this text.

---

## §44.3 — FEATURE REQUEST: give a spawned worker its `role_name` as its local session name (or a flag to), so a spawned Git-Controller can pass the git gate in one step  [label: feature-request]

**Item number:** §44.3

**The outcome you need:** Spinning up a worker to *do gated git* should be
usable in one step: `spawn_session(role_name="Git-Controller", ...)` should
yield a worker that can immediately pass the Git-Controller gate, without a
separate driven turn instructing it to claim the name via the rename skill.

**The workflow that hit the gap:** To run gated git from an automation
session, we spawned a Git-Controller (`spawn_session` with
`role_class="project"`, `role_name="Git-Controller"`, `host="tmux"`). The
worker came up with a derived local session name, not `Git-Controller`.
Because the git gate keys on the local session-name binding matching
`GIT_CONTROLLER_NAME`, the freshly spawned "Git-Controller" was itself
blocked from git until a second driven turn had it claim the name via the
rename skill. Re-verified on the current release (HEAD `68adf8b`): the
adapters now label workers explicitly — `label = lane_id or
agent_instance_id` — with the headless driver passing `--name <label>` and
the tmux driver exporting the label and deriving the pane session name from
it. So naming is now deliberate and lane-based, but `role_name` still plays
no part in it: the drive→rename round-trip is still required, and the gap is
unchanged in effect. (The current rename skill's incumbent-watcher detection
for fleet-spawned workers smooths the *claim* step; the request here is to
not need the extra driven turn at all.)

**What it costs you today:** Every "spawn a controller to do gated git" flow
is spawn → drive(claim role) → drive(work); the middle step is easy to forget
and fails silently until the worker's first git command is blocked.

**Your current workaround:** Drive an explicit first turn that invokes the
rename skill with the role name before dispatching any git work.

**Implementation sketch (optional):** Method is upstream's call; one shape:
when `role_name` is set, have the adapters use it for the worker's local
name/label (headless `--name`, tmux label/session-name) — or add a
`local_name` spawn field — so the gate's name check and the ledger's role
intent agree at birth. The outcome wanted is only: "spawn a Git-Controller
and it can immediately pass the git gate."

**Content gate:** ticked after re-reading this text.
