# Seed Feedback — commit `be7a58f31` (coordination-hooks 0.5.1 cadence fix)

Written 2026-08-11 by the driving Claude Code session of the operating
homunculus `dax`, on the operator's request, while diagnosing a report that
`coordination-hooks` was silently non-functional after installing it from
the `bizops-claude-code-plugin` GitHub marketplace — i.e. this is *operating*
feedback, not birth feedback. **Intended for Ada** (the seed authors).
Follows the established `workbench/…_seed_feedback_*.md` convention (prior
reports: `_dax_setup_feedback.md` at the Workspace root for seed `9f55e5c3`,
`workbench/2026-07-22_seed_feedback_ca9fe1fdc.md`,
`workbench/2026-07-26_seed_feedback_a01575d31.md`, and
`workbench/2026-07-27_seed_feedback_caaf7e423.md`).

The bug below is **already fixed locally in this repo** (commit `a1b188a`,
this branch) — the fix and updated tests ship alongside this note. It needs
a matching fix upstream in the seed so the next re-mint doesn't reintroduce
it, and so every other homunculus running this plugin gets it too.

## Operator's own words (the request that produced this file)

> "Please fix locally and create a feedback note in the seed so that Ada can
> address it."

That request followed a live debugging session that started as "is the
Claude Code plugin working yet?" (an org marketplace-policy block, since
separately fixed by the operator's admin) and turned into "are the hooks
actually working?" — which is where this bug was found.

## Summary

`step_zero_reminder` (the KB-first / Step Zero reminder hook, both the
Claude Python variant and the Codex JS twin) silently fails on every
`SessionStart` in **every** install of `coordination-hooks` 0.5.1 — the local
`dax` marketplace, the `bizops-claude-code-plugin` GitHub marketplace, and
presumably any other install pulling this same commit. It is not a local
misconfiguration: verified byte-identical between this repo and the actual
installed cache pulled from the GitHub marketplace. The failure is
completely invisible without `--debug`; a normal session just never gets the
Step Zero reminder and nothing indicates why.

---

## Issue 1 — BUG (FIXED LOCALLY, commit `a1b188a` this repo): `step_zero_reminder` hardcodes a `hookEventName` literal that no longer matches its own wiring

**Severity:** High. Silent, and it defeats the platform's own "Step Zero is
unconditionally armed" contract (the 2026-08-01 operator ruling this same
hook's docstring cites) on every session, everywhere this seed is installed.

**What happened:** A fresh Claude Code session (`--debug`) showed:

```
[DEBUG] Hook SessionStart:startup (SessionStart) error:
Failed to run: Hook returned incorrect event name: expected 'SessionStart'
but got 'UserPromptSubmit'. Full stdout: {
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "For non-trivial work, checking a persistent
    knowledge base ..."
```

Claude Code rejects a hook whose declared `hookEventName` doesn't match the
event that actually invoked it. The rejection is silent to a normal user —
non-JSON or rejected hook output is discarded at debug level only, never
surfaced. The reminder simply never fires and there is no symptom to chase.

**Root cause:** This commit (`be7a58f`, merged as `bfc46ba`, "coordination-hooks
0.5.1 cadence fix — KB-first + check-messages reminders move to SessionStart")
rewired `step_zero_reminder`'s trigger in **both** `hooks.json` files
(`claude_plugin` and `codex_plugin`) from `UserPromptSubmit` to `SessionStart`,
but did not touch either script's own payload. Both scripts still emitted a
compiled-in `"hookEventName": "UserPromptSubmit"` literal — stale the moment
the wiring changed. The sibling hook moved in the *same* commit,
`check_messages_reminder` (both variants), did **not** break, because it was
already written to read `hook_event_name` off stdin and echo it back rather
than hardcode it. `step_zero_reminder` was the one reminder in this migration
that hadn't been given that pattern, and the commit that moved its wiring
didn't notice.

Confirmed via git history, not inference: `step_zero_reminder.py`'s entire
history is the single commit that introduced it (`1a2d791`) — nothing since
has touched the file itself, only `hooks.json`'s wiring around it (most
recently `be7a58f`). `git status` was clean on this whole directory before
the fix below — nothing local had drifted from what the seed shipped.

**Fix (local, this repo, commit `a1b188a`):** Both scripts now read
`hook_event_name` off stdin and echo it back, matching their own sibling
hook's already-reviewed pattern, instead of a literal that can silently
desync from `hooks.json` again:

- `step_zero_reminder.py` — mirrors `check_messages_reminder.py`'s
  `_read_stdin_event_name(default)` helper (any non-empty string accepted,
  default `"SessionStart"`).
- `step_zero_reminder.js` — mirrors `check_messages_reminder.js`'s stricter
  two-value allowlist (`"UserPromptSubmit"` or `"SessionStart"` only, else
  default `"SessionStart"`).

Both `reminder_hooks_smoke.py` suites were updated to match: `step_zero` is
now grouped with the event-echoing reminders (`STDIN_AWARE` /
`EVENT_ECHOING`) instead of the fully-static ones, and its documented default
event is `SessionStart`, not the stale `UserPromptSubmit` the tests
previously (incorrectly) asserted as correct. Both suites are fully green:
107/107 (`claude_plugin`), 103/103 (`codex_plugin`).

**Recommend for the seed:** Apply the same diff upstream — this is a clean,
mechanical fix (see the diff in `a1b188a` on the `dax` clone this note ships
with) — and consider a manifest-consistency check that flags any hook
emitting a hardcoded `hookEventName` that doesn't appear in the set of events
`hooks.json` actually wires it to, so a future rewiring-without-payload-update
mistake fails the test suite instead of shipping silently. This exact class
of bug (wiring changed, payload literal not updated) is cheap to catch
statically and expensive to catch by hand.

---

## Also worth knowing (not a seed defect, but adjacent, in case it recurs elsewhere)

While root-causing the above, an *unrelated* local-machine issue briefly
looked like a seed/plugin bug and turned out not to be one: this operator's
`pyenv global` was set to `system`, which on this machine resolved `python3`
(in the non-login shell Claude Code spawns hooks in) to a stale
`/Library/Frameworks/Python.framework/Versions/3.7` install rather than the
intended pyenv-managed 3.13.7. That made every hook importing `_journal.py`
(which does `from datetime import UTC, datetime`, Python 3.11+ only) crash
with `ImportError`, fully masking the Issue 1 bug above until it was fixed
(`pyenv global 3.13.7`). Noting it here only because the seed's own dev
standard is Python 3.13 with no backwards-compatibility shims — that
standard is correct and shouldn't change; this was purely a local `pyenv`
misconfiguration on the operator's machine, not something for the seed to
accommodate.

## Local interim state (this machine, not seed feedback — for the record)

Pending the upstream fix, the two installed caches on this machine were
hand-patched directly with the same fixed `step_zero_reminder.py` so the
plugin actually works here today:

- `~/.claude/plugins/cache/bizops-claude-code-plugin/coordination-hooks/0.5.1/hooks/step_zero_reminder.py`
- `~/.claude/plugins/cache/dax/coordination-hooks/0.5.1/hooks/step_zero_reminder.py`

**These are landmines, not a real fix.** The next `claude plugin
install`/`update coordination-hooks@bizops-claude-code-plugin` (or `@dax`)
will overwrite the patched file with whatever that marketplace currently
ships and silently reintroduce this bug — no error, no warning. Symptom to
watch for: the Step Zero reminder quietly stops appearing at session start
again. Resolved permanently only once the fix above lands in
`bizops-claude-code-plugin` (and this repo's own seed lineage) and a normal
install/update picks it up.

## Priority for Ada

1. **Port the `step_zero_reminder` event-echo fix upstream** (Issue 1) — it
   is already written, tested, and diffed in this clone (`a1b188a`); every
   other homunculus on this seed has the identical silent failure right now.
2. **Consider the manifest-consistency check** suggested above so this class
   of bug fails loudly in CI next time, rather than needing a live
   `--debug` session to surface it.
