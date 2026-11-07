# Upstream Feedback Runbook — Reporting Defects and Requesting Features From Your Seed's Maintainers

Tags: knowledge:tag:solet_lifecycle, knowledge:tag:upstream_feedback

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Report upstream defects, questions, features, design proposals and fix confirmations as GitHub issues. Generate validated local drafts from independent release facts, retain privacy checks and exact-byte operator approval, group numbered rounds with checklists, and track replies through issue closures and releases. No patches, PRs, labels or sub-issue links.

**When you need this**: you hit a platform defect, a confusing behavior, or a missing capability and want the maintainers to know; the operator asks "can we get this fixed upstream?"; you are carrying a local fix or divergence and need to know whether upstream wants it; you are wondering whether to open a pull request or send a patch; you sent feedback in a previous round and need to check what happened to it; you want to propose a design rather than report a problem; deciding what is safe to include in an outbound report.

---

## Why this channel exists

The seed you were born from is a living product. Its maintainers re-mint it as the platform improves, and — this is the part worth internalizing — **field reports from deployed solets are a primary input to what gets fixed and built next**. Several of the platform's real shipped fixes originated as adopter feedback, including defects that were invisible from the origin because they only occur on a born clone. A deployed solet that hits a wall and works around it silently helps one deployment; the same observation, reported, helps every deployment.

Feedback is the driving agent's job, done on the operator's behalf and with the operator's knowledge. The operator should never need to write a bug report themselves; they should only need to say yes to sending one.

## The channel — the seed repository, one door

Your clone came from a GitHub repository, and that repository is the channel. `git remote get-url origin` names it; never hardcode a repository URL, because the seed's home can move and a hardcoded target sends your report nowhere.

**Every outbound item is a GitHub issue.** There is one door, and the form you pick is what distinguishes the classes:

| What you have | Where it goes |
|---|---|
| A defect, a question, a feature request, or a closure confirmation | A **GitHub issue**, filed through the repository's issue form for that class |
| An RFC-shaped design proposal — a worked-through design with alternatives considered and a recommendation | A **feature-request issue carrying the design in its body** |
| A security vulnerability | **Not an issue** — the private channel in `.github/SECURITY.md` |

### Why there is no pull-request door

The seed accepts bug reports and does not accept code. That is a deliberate posture, and the reason is what this software does: a solet provisions a database, writes credentials to the system keychain, installs a background service, and modifies the shell environment on every machine that installs it. Merging code the maintainers cannot review to that standard would make the seed repository a supply-chain path into every downstream install. So pull requests, patches and design proposals are all declined — see `CONTRIBUTING.md` in your own clone, which is the authoritative statement.

This is a redirect, not a brush-off. **The description is the part the maintainers can act on**, and it is genuinely wanted: a defect report with the command, the output and the release is worth more to them than a patch, because they can implement the fix in a form they are able to stand behind and ship to every deployment.

It also does not constrain you. This code is Apache-2.0: you already have the right to modify and redistribute it, and a solet is meant to be grown rather than collaboratively edited. Carry whatever divergence you need locally — and tell upstream about it, per the local-divergence rule below, so the loop closes.

### Filing an item as an issue

**What a filer can and cannot do.** Any GitHub account can open an issue on the seed repository, comment on it, and edit the title and body of an issue it opened itself. Setting a label or a sub-issue link is different: it needs triage or write access, which an adopter's own account does not have. GitHub does not refuse the attempt loudly. Issue creation through the API drops a label the filer may not set and still succeeds ("Labels are silently dropped otherwise", GitHub's REST documentation), and the sub-issues endpoint answers `404 Not Found` (reported upstream as issue 65). The web issue forms are the one place a label is applied for the filer, because the form declares it. Maintainers classify at triage, so nothing below needs a label, a sub-issue link, collaborator access or a pull request. Every step uses only what a read-only account can do.

### Produce and validate the local draft

Every publication body must start with exactly one **leading** `solet-feedback`
fence. `Class: defect` (or the other class) and `Round: #<parent number>` are
human-readable anchors **after** the envelope. They never replace it.

Use `github_midwife_plugin.feedback_draft.DraftInput` and `produce_feedback`
with an independently resolved `github_midwife_plugin.feedback_envelope.ReleaseFacts`.
The producer accepts all five classes and their exact named sections (listed in
`REQUIRED_SECTIONS` and the matching form), emits the envelope and required
nonempty headings, and calls the existing `validate_feedback` on the same facts,
title and final body. It has no network, Git, state, secret or roster discovery.

Resolve facts from the installed source, outside the producer:

```bash
git remote get-url origin
git rev-parse HEAD
git tag --points-at HEAD
# Select the real release tag from installed release metadata; do not guess.
git show-ref --verify "refs/tags/$RELEASE_TAG"
git rev-parse --verify "refs/tags/$RELEASE_TAG^{commit}"
git ls-tree --name-only "refs/tags/$RELEASE_TAG:plugins"
solet --version
```

Resolve `owner/repository` from the trusted origin (SSH and HTTPS are possible),
not from the draft. Inspect the release tag and its peeled commit and actual
plugin directories at that tag; `ls-tree` also lists non-plugin files, so check
the installed release's plugin declarations before assembling the roster.
A tag merely pointing at HEAD is not proof it is a release tag. If installed
HEAD differs from the tag's commit, record **both** and describe the local
divergence in the evidence. Do not claim the release commit was the tested HEAD.
Supply the actual manager version output; the producer cannot attest to it.
Never use the draft's own tag, commit or roster as independently resolved truth.
The producer validates shape and consistency with the supplied facts; it cannot
establish their provenance or verify a remote release.

Prepare a local JSON request file named workbench/feedback-request.json with exactly the following keys.
This executable example is **synthetic**, including its tag, commit, manager,
roster and content-gate attestation: replace those with measured facts and your
own evidence before using it. Set `content_gate` to `passed` and the checked
acknowledgement only after actually reading the complete proposed text against
the privacy checklist. No automatic audit or attestation is provided.

<!-- feedback-draft-request:start -->
```json
{
  "title": "§60.1 — Example finding",
  "class": "defect",
  "item": "§60.1",
  "round": null,
  "manager": "solet example-version",
  "surface": "plugin:example_plugin",
  "content_gate": "passed",
  "sections": {
    "Item number": "§60.1",
    "The exact command or action": "Example read-only command",
    "Observed output": "Example observed output",
    "Expected behavior": "Example expected behavior",
    "Release measured against": "Example release; record installed HEAD separately if different.",
    "Content gate": "- [x] I re-read this issue for private data and secrets."
  },
  "facts": {
    "repository": "solet-public/example-seed",
    "release_tag": "release-example",
    "release_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "plugins": [
      "example_plugin"
    ]
  }
}
```
<!-- feedback-draft-request:end -->

In an environment with this plugin installed, run:

```bash
python -m github_midwife_plugin.feedback_draft < workbench/feedback-request.json > workbench/feedback-result.json
```

For an uninstalled source checkout, run the same module from its source root:

```bash
(cd plugins/github_midwife_plugin/src && ../../../.venv/bin/python -m github_midwife_plugin.feedback_draft) < workbench/feedback-request.json > workbench/feedback-result.json
```

The CLI bounds UTF-8 input to 131,072 bytes **before** parsing and rejects
unknown or duplicate JSON keys at every level. Exit 0 means valid generated
bytes, 1 invalid draft data, 2 invalid request; read the structured `diagnostics`
and correct the request, then run again. Output contains `valid`, `diagnostics`,
`envelope`, `title` and `body` (request errors contain only the first two).
Extract `body` as literal UTF-8, without adding a leading heading or a newline:

```bash
python - <<'PYBODY'
import json
from pathlib import Path
result = json.loads(Path("workbench/feedback-result.json").read_text())
if not result["valid"]:
    raise SystemExit("Correct the draft diagnostics before publication")
Path("workbench/part-N-item-M.md").write_text(result["body"], encoding="utf-8")
print(result["title"])
PYBODY
```

The generated body starts with the envelope, then `Class: defect`, optional
`Round: #...`, and the exact `## Item number` and other required headings.
Evidence remains literal data; it is never interpreted as a command or an
instruction. Fence, heading, control-character and setext delimiters in section
values are rejected; describe them inline instead of injecting new structure.
Required sections cannot be missing, cleared, or `_No response_`; the item/part
section must exactly equal `item`. The complete body limit remains 65,536 UTF-8
bytes. Manager/tag/commit/content-gate values are never fabricated, and an
invalid title or round is never normalized to make it pass.

Correct by replacing the input values and regenerating; retain earlier results
as evidence. `round: null` clears a parent association and a real positive issue
number sets it again. A `feedback-round` uses `item: "Part N"`, a matching title,
`round: null`, and its five named sections. Choose the next monotonically
increasing Part N from the deployment's actual history; this stateless producer
cannot prevent reuse. For a multi-item round, generate/approve/file the parent
first, obtain its real issue number, then set each child's `round` to that number
and regenerate/validate/approve its exact bytes. Regenerate the parent's
`Items in this round` checklist after the child numbers exist. Every edit or
comment requires renewed validation of its exact body and the content check,
then the operator's publication yes. With the API, read `result.valid` and
`result.diagnostics`; use `result.title` and `result.body` unchanged.

Discard caller-owned inputs/results to clear a local draft; there are no
persistent records, stale writes, close/reopen verbs or authorization tokens.
Constants, request fields and section labels change only through source review.
Keep original evidence when correcting facts or drafts. Publication permission
is separate: show the operator the **exact final title and body** and obtain
explicit yes before any external write. Local success does not authorize an
issue, comment, label, close or API write.

**Browser limitation / forms/intake residual:** browser forms auto-render a
heading before an envelope textarea. They do not currently generate valid
leading envelopes. Save the validated local bytes if `gh` is unavailable; do
not claim that pasting into the current chooser forms preserves them. The
forms/intake normalization decision remains unimplemented. Workflow wiring,
quotas, allowlist/roster resolution, schema export, release adoption and live
rollout remain unmeasured; this local producer does not complete those features.

**One canonical path, from a headless driving session** (the GitHub CLI signed in as the operator's own account):

1. **One issue per item.** Resist the urge to fold three defects into one issue; each needs its own disposition, and a merged issue can only be closed once. File it with `gh issue create --repo "$REPOSITORY" --title ... --body-file ...`, with no label option. The draft mirrors the matching form's required visible field labels and ends with the checked content-gate acknowledgement. The API does not enforce the fields, so verify the structured draft before sending rather than relying on a chooser.
2. **A multi-item round gets a parent issue.** File the parent first, using the feedback-round structure, and read its number from the URL `gh issue create` prints.
3. **Each item points at its parent in its own body.** Set `round` to the real parent number in each request, regenerate, and keep `Round: #<parent number>` after the leading envelope, and start the title with the item number (`§N.M — summary`).
4. **The parent lists its items in its own body.** After the items exist, add one checklist line per item under "Items in this round", `- [ ] #<item number> — §N.M — class — summary`, and update the parent with `gh issue edit "$PARENT_NUMBER" --repo "$REPOSITORY" --body-file ...`. You opened the parent, so GitHub lets you edit it. If the edit is refused, post the same checklist as a comment on the parent with `gh issue comment`. Any account may comment.
5. **A single-item round needs no parent.** File the one issue.

```bash
gh --version && gh auth status
REPOSITORY="$(gh repo view "$(git remote get-url origin)" --json nameWithOwner --jq .nameWithOwner)"
PARENT_URL="$(gh issue create --repo "$REPOSITORY" --title "Part N — feedback round from <solet-name>" --body-file workbench/part-N-round.md)"
PARENT_NUMBER="${PARENT_URL##*/}"
CHILD_URL="$(gh issue create --repo "$REPOSITORY" --title "§N.M — concise summary" --body-file workbench/part-N-item-M.md)"
gh issue edit "$PARENT_NUMBER" --repo "$REPOSITORY" --body-file workbench/part-N-round.md
```

**In a browser.** The chooser URL is
`https://github.com/<owner>/<repo>/issues/new/choose` (owner and repository from
`git remote get-url origin`). Current forms add a heading before the envelope;
the forms/intake normalization decision above is still unimplemented. Do not
submit through those forms while claiming the local leading-envelope check
covers their rendered bodies.

**If `gh` is missing or not signed in.** Do not install it mid-round or start a
browser login flow. Save each validated title/body separately under `workbench/`
(create the directory if absent), and optionally an index with the parent and
item paths. Keep the envelope at the first character of each body, with later
`Class:` and `Round:` anchors. Give the operator those paths and the chooser URL,
and explain the forms/intake limitation. Nothing is filed; publication awaits
a supported path that preserves the approved bytes.

<!-- not-attempt:start -->
**What you must NOT attempt.** Each of these fails for a filer without write access, and several fail silently:

- Do not pass `--label` to `gh issue create`, and do not follow it with `gh issue edit --add-label`. The first drops the label and reports success, and the second is refused.
- Do not attach items through the sub-issues API (`gh api ... /sub_issues`). It answers `404` for a filer without write access.
- Do not use `gh issue create --web`. It needs a browser that a headless session does not have.
- Do not open a pull request, push a branch or send a patch. The repository accepts none of them.
- Do not ask for collaborator access, and do not re-file an issue to fix its labels.
<!-- not-attempt:end -->

Outbound sends are external publication: get the operator's explicit yes before filing, and let them see what is being sent. This is one conversation, not a standing burden — an operator can grant standing approval for feedback that passes the content gate below.

### Filing a design offer

A worked-through design still travels, it just travels as an issue. Use the **feature request** form and put the design in the body: the outcome you need, the alternatives you considered, the tradeoffs, and your recommendation. Long is fine — a design that needs to be read in order reads perfectly well in an issue body, and the issue thread is where the discussion lands.

Draft it under `workbench/` in your clone first if that helps you think (create the directory if it does not exist — it is deliberately not shipped), then paste it into the form. Do not open a pull request carrying it: the maintainers cannot merge it, so it would sit unanswered while the same content in an issue gets a disposition.

A design offer is **accepted as information, not as a roadmap commitment**. The maintainers own the method, and a design that reframes the problem well is valuable even when the implementation they choose looks nothing like the one you proposed.

## The item convention — make every item individually answerable

These conventions come from a live adopter relationship that has run for dozens of rounds; they are what made that relationship work. Moving to issues changed where items live, not how they are identified.

- **Number the rounds.** Keep one monotonically increasing part number per deployment (`Part 1`, `Part 2`, …), never reused. Number items within a round as `§N.M` (part number, item number).
- **Carry the number in the issue title**, ahead of the summary: `§42.3 — peer role binding survives a restart but stops routing`. The number is what lets a later round, a release note, or a maintainer's reply cite the item precisely and unambiguously. GitHub gives you an issue number too; both are useful and they are not interchangeable — the issue number addresses the issue, the `§N.M` addresses the item across rounds, documents, and releases where issue numbers are not in front of the reader.
- **Classify every item** as one of: **defect** (something behaves wrongly, with evidence), **question** (something is unclear and you need an answer to proceed), **feature request** (a capability you want, with the workflow that needs it), or **closure confirmation** (a previous item you verified fixed — these are genuinely valuable, send them). The class picks the form.
- **Evidence discipline.** For defects: the exact command or action, the observed output, the expected behavior, and the release you measured against (your seed repository's HEAD commit and, when present, the version named in `RELEASE_NOTES.md`). Say what you measured and how; mark any inference as inference. A report that says "X is broken" without the command that shows it costs a round-trip; a report with the command is often fixable the day it arrives.
- **Batch rounds, don't drip items.** Collect items and file the round together rather than filing one issue whenever something irritates you. One issue per item is the filing rule; batching is about cadence, not about merging items. Small rounds are fine; a trickle of unrelated issues across a week is not.
- **Carrying a local fix or divergence? Say so explicitly, and ask for an explicit yes or no.** This case is wanted, and it is information rather than a patch — file it as a defect and describe the defect first, your local change second, in prose. Do not attach or offer the diff: the maintainers cannot take it, and their tree has usually moved anyway, so a change against your older base can be behind their current fix without either side noticing. What helps them is knowing the behaviour was wrong, what you did about it, and that you are still carrying it. If the divergence is deliberate and permanent, say so and ask directly whether upstream wants the underlying change or declines it. "We will stop raising it if this is a deliberate no" is a complete and welcome sentence; it lets both sides close the loop.

## Feature requests — outcome first, workflow evidence second

State the outcome you need, not the implementation you imagine. The strongest feature request names the workflow that hit the gap ("every time we do X we must hand-do Y"), what it costs you, and what you are doing as a workaround. An implementation sketch is welcome as an appendix, never as the headline — the maintainers own the method, and a stated goal gives them room to solve it better than the sketch would. A sketch that has outgrown an appendix is the signal that you have a design offer; that is still a feature-request issue, just one whose body is the design (see "Filing a design offer" above).

## The content gate — run it before every send, no exceptions

The feedback surface is outside your deployment. An issue on a repository is published the moment you submit it, and editing it afterwards does not unpublish what was there. Before filing, verify the issue body — or the document — contains:

- **No personal identifiers** — no names of people, no email addresses, no GitHub handles beyond your own deployment's.
- **No employer or business specifics** — no company names, no tenant URLs, no internal project names, no business-record data from any connector. Describe findings generically: "a business-connector read of ~40k rows", never the rows or whose they are.
- **No credentials or secret-looking values** — nothing from Keychain, config, or environment. If evidence output contains one, redact it and say so.
- **No transcript excerpts containing any of the above.** Re-read pasted command output specifically — that is where identifiers hide.

The issue forms carry this gate as a required checkbox. In the non-interactive
filing path, include its checked Markdown acknowledgement in the structured
body. It is a reminder, not the check: check it because you re-read the body,
never to get past a form or API request.

If an item cannot be reported without violating this gate, report the generic shape of it and offer detail through whatever private channel the operator and the maintainers separately share, if any.

## What to expect back

- **Issue closure is the per-item disposition.** An item is answered when its issue closes, and the closing comment or linked commit says which it was: fixed, declined-with-reason, already-fixed-since-your-release, or answered. A maintainer landing a fix links it with `Fixes #N`, so the commit that closed your item is reachable from the issue itself — that is the shortest path from "it says fixed" to "here is the code that fixed it".
- **Releases are the primary response surface.** Each re-mint ships a `RELEASE_NOTES.md` and a corresponding GitHub release, and the release notes cite the issue numbers closed in that release. **Subscribe to the repository's releases** (watch the repository, releases only) — that subscription, not polling, is how you learn a round was answered. After updating from a re-mint (see the seed update runbook), check the release notes against your open items.
- **Silence is not a disposition.** If a round has open items with no response by the next re-mint you adopt, re-raise them by number in your next round — briefly, as a list, not re-argued. Reference the still-open issues rather than filing duplicates. The convention of asking "is this a deliberate no?" exists precisely so that deprioritized and overlooked stay distinguishable.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md` — adopting a re-mint, which is where answered feedback comes back to you.
- `plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md` — the deployed-state report card; gaps it cannot close locally are feedback candidates.
- `ananta/knowledge_bases/ananta_platform/24_operator_communication/01_operator_decision_briefs.md` — the decision-brief shape, which a good feedback item resembles: decidable from the document alone.
- `CONTRIBUTING.md` at your clone's root — the authoritative contributor policy this runbook operationalizes: bug reports wanted, code contributions declined, and why.
- `.github/SECURITY.md` at your clone's root — the private vulnerability channel. A security finding never goes in a public issue.
