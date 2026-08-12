# Upstream Feedback Runbook — Reporting Defects and Requesting Features From Your Seed's Maintainers

Tags: knowledge:tag:homunculus_lifecycle, knowledge:tag:upstream_feedback

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: How a deployed homunculus's driving agent reports platform defects, asks questions, and requests features upstream to the maintainers of the seed it was born from — the pull-request feedback channel against the seed's own GitHub repository, the dated numbered feedback-document convention that makes every item individually answerable, the evidence and content rules for what may and may not appear in an outbound report, how feature requests differ from defect reports, and what response to expect through release notes and pull-request replies.

**When you need this**: you hit a platform defect, a confusing behavior, or a missing capability and want the maintainers to know; the operator asks "can we get this fixed upstream?"; you are carrying a local fix or divergence and need to know whether upstream wants it; you sent feedback in a previous round and need to check what happened to it; deciding what is safe to include in an outbound report.

---

## Why this channel exists

The seed you were born from is a living product. Its maintainers re-mint it as the platform improves, and — this is the part worth internalizing — **field reports from deployed homunculi are a primary input to what gets fixed and built next**. Several of the platform's real shipped fixes originated as adopter feedback, including defects that were invisible from the origin because they only occur on a born clone. A deployed homunculus that hits a wall and works around it silently helps one deployment; the same observation, reported, helps every deployment.

Feedback is the driving agent's job, done on the operator's behalf and with the operator's knowledge. The operator should never need to write a bug report themselves; they should only need to say yes to sending one.

## The channel — a pull request against the seed repository

Your clone came from a GitHub repository, and that repository is the channel. `git remote get-url origin` names it.

1. **Write a feedback document** under `workbench/` in your clone (create the directory if it does not exist — it is deliberately not shipped): `workbench/<YYYY-MM-DD>_<your-homunculus-name>_seed_feedback_part<N>.md`.
2. **Branch, commit, push** the document on a branch named `seed-feedback/<YYYY-MM-DD>-<your-homunculus-name>`, pushed to your fork of the seed repository (or directly, if your operator has write access).
3. **Open a pull request** against the seed repository. The pull request is the delivery event; its thread is where per-item replies land.

Outbound sends are external publication: get the operator's explicit yes before pushing, and let them see what is being sent. This is one conversation, not a standing burden — an operator can grant standing approval for feedback reports that pass the content gate below.

## The document convention — make every item individually answerable

These conventions come from a live adopter relationship that has run for dozens of rounds; they are what made that relationship work.

- **Number the rounds.** Keep one monotonically increasing part number per deployment (`Part 1`, `Part 2`, …), never reused. Number items within a round as `§N.M` (part number, item number). Upstream answers by citing these numbers; an unnumbered item cannot be answered precisely and tends to get lost.
- **Classify every item** as one of: **defect** (something behaves wrongly, with evidence), **question** (something is unclear and you need an answer to proceed), **feature request** (a capability you want, with the workflow that needs it), or **closure confirmation** (a previous item you verified fixed — these are genuinely valuable, send them).
- **Evidence discipline.** For defects: the exact command or action, the observed output, the expected behavior, and the release you measured against (your seed repository's HEAD commit and, when present, the version named in `RELEASE_NOTES.md`). Say what you measured and how; mark any inference as inference. A report that says "X is broken" without the command that shows it costs a round-trip; a report with the command is often fixable the day it arrives.
- **Batch rounds, don't drip items.** Collect items into one document per round rather than opening a pull request per item. Small rounds are fine; ten single-item pull requests are not.
- **Carrying a local fix or divergence? Say so explicitly, and ask for an explicit yes or no.** Describe the defect first and your local change second — the maintainers' tree has usually moved, and a patch against your older base can be behind their current fix without either side noticing. If you keep a deliberate local divergence, state that you are carrying it and ask directly whether upstream wants it or declines it. "We will stop raising it if this is a deliberate no" is a complete and welcome sentence; it lets both sides close the loop.

## Feature requests — outcome first, workflow evidence second

State the outcome you need, not the implementation you imagine. The strongest feature request names the workflow that hit the gap ("every time we do X we must hand-do Y"), what it costs you, and what you are doing as a workaround. An implementation sketch is welcome as an appendix, never as the headline — the maintainers own the method, and a stated goal gives them room to solve it better than the sketch would.

## The content gate — run it before every push, no exceptions

The feedback surface is outside your deployment. Before pushing, verify the document contains:

- **No personal identifiers** — no names of people, no email addresses, no GitHub handles beyond your own deployment's.
- **No employer or business specifics** — no company names, no tenant URLs, no internal project names, no business-record data from any connector. Describe findings generically: "a business-connector read of ~40k rows", never the rows or whose they are.
- **No credentials or secret-looking values** — nothing from Keychain, config, or environment. If evidence output contains one, redact it and say so.
- **No transcript excerpts containing any of the above.** Re-read pasted command output specifically — that is where identifiers hide.

If an item cannot be reported without violating this gate, report the generic shape of it and offer detail through whatever private channel the operator and the maintainers separately share, if any.

## What to expect back

- **Release notes are the primary response surface.** Each re-mint ships a `RELEASE_NOTES.md`; items you raised appear there when they land. After updating from a re-mint (see the seed update runbook), check it against your open parts.
- **Pull-request replies** carry per-item dispositions, especially for items that need an answer rather than a fix: accepted, declined-with-reason, already-fixed-since-your-release, or a question back.
- **Silence is not a disposition.** If a round has had no response by the next re-mint you adopt, re-raise the open items by number in your next round — briefly, as a list, not re-argued. The convention of asking "is this a deliberate no?" exists precisely so that deprioritized and overlooked stay distinguishable.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md` — adopting a re-mint, which is where answered feedback comes back to you.
- `plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md` — the deployed-state report card; gaps it cannot close locally are feedback candidates.
- `knowledge_bases/ananta_platform/24_operator_communication/01_operator_decision_briefs.md` — the decision-brief shape, which a good feedback item resembles: decidable from the document alone.
