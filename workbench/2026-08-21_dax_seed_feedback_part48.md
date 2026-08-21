# Part 48 — feedback round from dax

**FILED 2026-08-21** as `solet-public/macos-bizops` parent
[#27](https://github.com/solet-public/macos-bizops/issues/27), with five
sub-issues attached via `gh issue create --parent 27` (native sub-issue
support, confirmed via `subIssuesSummary: {total: 5}` on the parent).

| Item | Issue | Class |
|---|---|---|
| §48.1 | [#28](https://github.com/solet-public/macos-bizops/issues/28) | defect |
| §48.2 | [#29](https://github.com/solet-public/macos-bizops/issues/29) | feature-request |
| §48.3 | [#30](https://github.com/solet-public/macos-bizops/issues/30) | defect |
| §48.4 | [#31](https://github.com/solet-public/macos-bizops/issues/31) | defect |
| §48.5 | [#32](https://github.com/solet-public/macos-bizops/issues/32) | feature-request |

Five-item round with a parent issue. Filed via `gh issue create --title/--body`
(with `--parent` for sub-issue attachment) rather than the web chooser, per
Step 7's own escape clause and the same reasoning as §47.8 (#26, still open) —
operator instruction: use the same mechanism as prior rounds, not the
browser-interactive one, since that mandate is expected to leave the skill's
own guidance regardless. Every form field reproduced by hand under its own
heading, matching each item's issue-form class; content gate run against the
exact submitted text (see below).

**Release this round was measured against:** HEAD `2329ea4` (seed re-mint
merge dd4109d..9e595f4) / RELEASE_NOTES.md 2026-08-19 release.

**Round thesis:** §48.1 and §48.4 are unrelated systems that share a shape —
in both, one side of a two-part mechanism (a declared-parameter filter vs. a
spawn adapter; a wake-hook spool vs. an inbox drain) doesn't learn what the
other side already resolved, so it keeps insisting on a stale state. §48.2 is
a deferred-capability request forced by hitting a real deadline. §48.3 is a
hydration-generated invariant that fought the operator's own configured
policy after an upstream (Claude Code) update. §48.5 is a capability built
this round and offered upstream rather than kept as a private local artifact.

**Items carried forward:** Part 43 (#7) with §43.2 (#9) open; Part 46 (#14)
with §46.2 (#16) open; Part 47 (#18) with all eight children (#19–#26) open.
Checked live issue state via `gh issue list --state all` immediately before
filing — confirmed none of the above have closed since Part 47 (2026-08-19).
Not re-argued here.

**Content gate:** run against the parent and all five child bodies as
actually submitted — no personal identifiers, no employer or business
specifics, no credentials or secret-looking values. The one absolute
filesystem path that carried the operator's own username (in an early draft
of §48.5, describing the local statusLine script's settings.json wiring) was
generalized to `~/.claude/...` before filing; third-party plugin names cited
as evidence for §48.5 were genericized to avoid anything handle-adjacent.

---

## Parent — "Part 48 — feedback round from dax" [label: feedback-round]

See body as filed: [#27](https://github.com/solet-public/macos-bizops/issues/27).

## §48.1 — spawn_session's documented degraded_hooks_acknowledged escape hatch is unreachable from the CLI [label: defect]

See body as filed: [#28](https://github.com/solet-public/macos-bizops/issues/28).

## §48.2 — Feature request: pull bulk_load (Bulk API v2 ingest) and run_apex into salesforce_plugin v1 [label: feature-request]

See body as filed: [#29](https://github.com/solet-public/macos-bizops/issues/29).

## §48.3 — Hydration-generated launcher hard-codes a permission-mode invariant that can contradict the operator's declared policy [label: defect]

See body as filed: [#30](https://github.com/solet-public/macos-bizops/issues/30).

## §48.4 — Spurious Stop-hook wake: delivery drained via peer_inbox still counted "unread" by the wake hook's spool accounting [label: defect]

See body as filed: [#31](https://github.com/solet-public/macos-bizops/issues/31).

## §48.5 — Feature request: hydration's client-side artifact set has no operator-facing context-occupancy / prompt-cache-TTL display [label: feature-request]

See body as filed: [#32](https://github.com/solet-public/macos-bizops/issues/32).

---

## Filing plan (executed)

Parent issue (form 05) + five sub-issues attached via `gh issue create
--parent`: three defects (form 01) and two feature requests (form 03). Filed
through the `feedback` skill, which ran the evidence discipline and the
content gate before publishing. No pull request and no patch — per
`CONTRIBUTING.md` the repository accepts neither.
