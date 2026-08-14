# Upstream Feedback Runbook — Reporting Defects and Requesting Features From Your Seed's Maintainers

Tags: knowledge:tag:solet_lifecycle, knowledge:tag:upstream_feedback

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: How a deployed solet's driving agent reports platform defects, asks questions, requests features, and confirms fixes upstream to the maintainers of the seed it was born from — filing each item as a GitHub issue through the seed repository's issue forms, grouping a round under a parent issue with sub-issues, reserving a pull request carrying a design document for RFC-shaped proposals, the numbered item vocabulary that keeps every item individually answerable, the evidence and content rules for what may and may not appear in an outbound report, and how answers come back as issue closures and GitHub releases.

**When you need this**: you hit a platform defect, a confusing behavior, or a missing capability and want the maintainers to know; the operator asks "can we get this fixed upstream?"; you are carrying a local fix or divergence and need to know whether upstream wants it; you sent feedback in a previous round and need to check what happened to it; you want to propose a design rather than report a problem; deciding what is safe to include in an outbound report.

---

## Why this channel exists

The seed you were born from is a living product. Its maintainers re-mint it as the platform improves, and — this is the part worth internalizing — **field reports from deployed solets are a primary input to what gets fixed and built next**. Several of the platform's real shipped fixes originated as adopter feedback, including defects that were invisible from the origin because they only occur on a born clone. A deployed solet that hits a wall and works around it silently helps one deployment; the same observation, reported, helps every deployment.

Feedback is the driving agent's job, done on the operator's behalf and with the operator's knowledge. The operator should never need to write a bug report themselves; they should only need to say yes to sending one.

## The channel — the seed repository, entered two ways

Your clone came from a GitHub repository, and that repository is the channel. `git remote get-url origin` names it; never hardcode a repository URL, because the seed's home can move and a hardcoded target sends your report nowhere.

Which door you use depends on the shape of what you are sending:

| What you have | Where it goes |
|---|---|
| A defect, a question, a feature request, or a closure confirmation | A **GitHub issue**, filed through the repository's issue form for that class |
| An RFC-shaped design proposal — a worked-through design you want reviewed as a document | A **pull request carrying a design document** |

The issue path is the common one. Reach for the pull-request path only when what you are offering genuinely is a document: a design with alternatives considered, tradeoffs weighed, and a recommendation — something that needs to be read in order, not answered in a field. If you are unsure which you have, file an issue; a maintainer can ask you to promote it.

### Filing an item as an issue

1. **Open the repository's issue chooser** and pick the form matching the item class. The forms exist so you cannot forget the evidence — they ask for the command, the observed output, the expected behavior, and the release you measured against, and they will not submit without them.
2. **One issue per item.** Resist the urge to fold three defects into one issue; each needs its own disposition, and a merged issue can only be closed once.
3. **A multi-item round gets a parent issue.** File a parent issue for the round, then attach each item's issue to it as a sub-issue. The parent carries the round's context and gives both sides one place to see what is still open; the children carry the individually answerable items.
4. **A single-item round needs no parent.** File the one issue.

Outbound sends are external publication: get the operator's explicit yes before filing, and let them see what is being sent. This is one conversation, not a standing burden — an operator can grant standing approval for feedback that passes the content gate below.

### Filing a design offer as a pull request

Write the document under `workbench/` in your clone (create the directory if it does not exist — it is deliberately not shipped): `workbench/<YYYY-MM-DD>_<your-solet-name>_seed_design_<slug>.md`. Push it on a branch named `seed-feedback/<YYYY-MM-DD>-<your-solet-name>` and open a pull request against the seed repository. The pull request is the delivery event; its thread is where review lands. Open a companion issue only if the design also reports a defect — otherwise the pull request is the item.

## The item convention — make every item individually answerable

These conventions come from a live adopter relationship that has run for dozens of rounds; they are what made that relationship work. Moving to issues changed where items live, not how they are identified.

- **Number the rounds.** Keep one monotonically increasing part number per deployment (`Part 1`, `Part 2`, …), never reused. Number items within a round as `§N.M` (part number, item number).
- **Carry the number in the issue title**, ahead of the summary: `§42.3 — peer role binding survives a restart but stops routing`. The number is what lets a later round, a release note, or a maintainer's reply cite the item precisely and unambiguously. GitHub gives you an issue number too; both are useful and they are not interchangeable — the issue number addresses the issue, the `§N.M` addresses the item across rounds, documents, and releases where issue numbers are not in front of the reader.
- **Classify every item** as one of: **defect** (something behaves wrongly, with evidence), **question** (something is unclear and you need an answer to proceed), **feature request** (a capability you want, with the workflow that needs it), or **closure confirmation** (a previous item you verified fixed — these are genuinely valuable, send them). The class picks the form.
- **Evidence discipline.** For defects: the exact command or action, the observed output, the expected behavior, and the release you measured against (your seed repository's HEAD commit and, when present, the version named in `RELEASE_NOTES.md`). Say what you measured and how; mark any inference as inference. A report that says "X is broken" without the command that shows it costs a round-trip; a report with the command is often fixable the day it arrives.
- **Batch rounds, don't drip items.** Collect items and file the round together rather than filing one issue whenever something irritates you. One issue per item is the filing rule; batching is about cadence, not about merging items. Small rounds are fine; a trickle of unrelated issues across a week is not.
- **Carrying a local fix or divergence? Say so explicitly, and ask for an explicit yes or no.** Describe the defect first and your local change second — the maintainers' tree has usually moved, and a patch against your older base can be behind their current fix without either side noticing. If you keep a deliberate local divergence, state that you are carrying it and ask directly whether upstream wants it or declines it. "We will stop raising it if this is a deliberate no" is a complete and welcome sentence; it lets both sides close the loop.

## Feature requests — outcome first, workflow evidence second

State the outcome you need, not the implementation you imagine. The strongest feature request names the workflow that hit the gap ("every time we do X we must hand-do Y"), what it costs you, and what you are doing as a workaround. An implementation sketch is welcome as an appendix, never as the headline — the maintainers own the method, and a stated goal gives them room to solve it better than the sketch would. A sketch that has outgrown an appendix is the signal that you have a design offer, not a feature request; send it as a pull request instead.

## The content gate — run it before every send, no exceptions

The feedback surface is outside your deployment. An issue on a repository is published the moment you submit it, and editing it afterwards does not unpublish what was there. Before filing, verify the issue body — or the document — contains:

- **No personal identifiers** — no names of people, no email addresses, no GitHub handles beyond your own deployment's.
- **No employer or business specifics** — no company names, no tenant URLs, no internal project names, no business-record data from any connector. Describe findings generically: "a business-connector read of ~40k rows", never the rows or whose they are.
- **No credentials or secret-looking values** — nothing from Keychain, config, or environment. If evidence output contains one, redact it and say so.
- **No transcript excerpts containing any of the above.** Re-read pasted command output specifically — that is where identifiers hide.

The issue forms carry this gate as a required checkbox. The checkbox is a reminder, not the check: tick it because you re-read the body, never to get past the form.

If an item cannot be reported without violating this gate, report the generic shape of it and offer detail through whatever private channel the operator and the maintainers separately share, if any.

## What to expect back

- **Issue closure is the per-item disposition.** An item is answered when its issue closes, and the closing comment or linked commit says which it was: fixed, declined-with-reason, already-fixed-since-your-release, or answered. A maintainer landing a fix links it with `Fixes #N`, so the commit that closed your item is reachable from the issue itself — that is the shortest path from "it says fixed" to "here is the code that fixed it".
- **Releases are the primary response surface.** Each re-mint ships a `RELEASE_NOTES.md` and a corresponding GitHub release, and the release notes cite the issue numbers closed in that release. **Subscribe to the repository's releases** (watch the repository, releases only) — that subscription, not polling, is how you learn a round was answered. After updating from a re-mint (see the seed update runbook), check the release notes against your open items.
- **Silence is not a disposition.** If a round has open items with no response by the next re-mint you adopt, re-raise them by number in your next round — briefly, as a list, not re-argued. Reference the still-open issues rather than filing duplicates. The convention of asking "is this a deliberate no?" exists precisely so that deprioritized and overlooked stay distinguishable.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md` — adopting a re-mint, which is where answered feedback comes back to you.
- `plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md` — the deployed-state report card; gaps it cannot close locally are feedback candidates.
- `ananta/knowledge_bases/ananta_platform/24_operator_communication/01_operator_decision_briefs.md` — the decision-brief shape, which a good feedback item resembles: decidable from the document alone.
