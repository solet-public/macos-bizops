# Part 52 — feedback round from dax

**DRAFT — not yet filed.** Awaiting operator sign-off (Step 6). Compiled from
a two-session investigation: this Operator session (`~/Workspace/dax`) and a
peer Claude Code session running in a separate, unrelated repository in the
operator's own organization (referred to below only as "the client-repo
session" — its actual repo/org name is redacted per the content gate, item 2a
below explains why it matters structurally). Genuine cross-session findings
about dax's own behavior, not about either repo's own content.

**Release this round is measured against:** HEAD `36dba33` (dax repo), which
descends from re-mint `5319656` (2026-08-20T20:07:40Z) / RELEASE_NOTES.md
"2026-08-20 — A fresh install can do work again, and the hook errors on a
stock Mac are gone" — same release Part 50 and Part 51 were measured against.
Nothing new has landed upstream since Part 51 (filed 2026-08-24).

**Round thesis:** a long orientation/diagnostic session surfaced a cluster of
related gaps around what happens when a session is *not* launched through the
`claude-<name>` launcher (a desktop-app session, or any session running in a
client repo rather than the solet's own clone), plus several independently
reproducible defects in the peer-messaging surface, a root-caused path-decode
bug, and an incomplete internal rename. Fourteen items; grouped by theme
below, each still filed as its own issue per the one-issue-per-item rule.

**Items carried forward (not re-argued here):** Part 43 (#7) with §43.2 (#9)
open; Part 46 (#14) with §46.2 (#16) open; Part 47 (#18) with all eight
children (#19–#26) open (§47.3/#21 carries a correcting comment, not
re-argued); Part 48 (#27) with all five children (#28–#32) open; Part 50
(#34) with all five children (#35–#39) open; §49.1 (#33, no parent) open;
Part 51 (#40, single item) open.

**Content gate — round-level note:** every path below that would otherwise
show the operator's own username or the client repo's real name has been
replaced with a generic placeholder (`<operator>`, `client-repo`) or a
synthetic example with fabricated values that reproduces the same defect
shape. `solet-public/macos-bizops` is the seed's own public channel repo name
— already used unredacted throughout this issue thread's prior history (e.g.
#8, #28, #40) — not treated as a business specific for that reason. No names
of people, no employer/org names, no credentials, and no tenant data appear
anywhere below. Per-item content-gate lines follow each item as well.

---

## §52.1 — Feature request: one canonical "how to use dax from a session" KB article [label: feature-request]

### The outcome you need
A fresh session should be able to establish, in one KB search, what a solet
is, what `dax call`/`dax search`/`dax watch`/`dax schema` each do, what the
`peer_send_by_name` delivery states mean, and that an empty registry view is
not evidence of anything by itself.

### The workflow that hit the gap
Getting oriented this round took roughly 20 separate tool calls across two
sessions — `knowledge_service::search`, `lifecycle_management_service::
list_plugins`, `session_ledger_service::list_sessions`/`get_session_timeline`,
`agent_messaging_plugin::peer_list` — and finally reading launcher template
source directly, because no KB article stated the one fact that resolved
everything: `AGENT_SESSION_ID` is minted client-side by the launcher
(`ases-$(date +%s)-$$-${RANDOM}`), nothing more.

Two wrong conclusions were drawn along the way and had to be walked back,
corrected only by empirical testing, not by anything documented:
- Assumed the session ledger was scoped to the querying session's own
  project, because a recency-sorted listing happened to return rows that
  were all from one project. It ingests any local Claude Code session
  regardless of which project it came from.
- Assumed `peer_list` returning empty meant "no live peer to reach." It can
  be empty while a role binding is fully valid — presence and the address
  book are different things. The only reliable test is attempting delivery
  and reading the literal `delivery` field back.

### What it costs you today
Every new session (or every session returning after a `/clear`) re-derives
this from scratch via trial and error, or from a prior session's memory file
if one happens to exist — which doesn't transfer to a session in a different
project (see §52.11).

### Your current workaround
Read launcher/template source directly, and accumulate findings in a local,
per-project memory file that other sessions can't see.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.2 — Defect + feature request: a session not launched via `claude-<name>` gets no fleet-identity env vars and no supported way to register anyway [label: defect]

### The exact command or action
Start Claude Code any way other than the `claude-<name>` launcher — the
desktop app, or a terminal session whose `cwd` is a client repo rather than
the solet's own clone — then check for fleet identity:
```
echo "$AGENT_SESSION_ID $AGENT_SESSION_LABEL $FLEET_TRANSPORT"
```

### The observed output
All three empty. The `rename` skill's own text anticipates this exact
failure ("if the watcher exits immediately complaining about a missing
session id, the session was not started via the launcher — say so rather
than inventing an id") but offers no supported path forward once that's true
— this is not a rare edge case, it is the default state for a desktop-app
session and for every session in every client repo.

### The expected behavior
Either a documented, sanctioned "register a non-fleet session" flow, or the
`rename` skill mints an equivalent identity itself when none is present and
says so explicitly (which is what this investigation ended up doing by hand,
after reading `fleet_functions.zsh.template` source to confirm
`AGENT_SESSION_ID` has no server-side minting or cryptographic tie to
anything — it worked immediately once tried, full round trip verified with
live `queued_watcher` delivery).

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.3 — Documentation gap: two independent, undocumented cross-session messaging channels exist [label: feature-request]

### The outcome you need
One place stating that Claude Code's own native cross-session messaging
(session-to-session, independent of any solet) and dax's own
`agent_messaging_plugin` peer registry are two separate systems, and which
one a given operation actually uses.

### The workflow that hit the gap
A message sent over Claude Code's own native session-messaging path arrived
successfully at a time when `dax call plugin::agent_messaging_plugin::
peer_list` was completely empty on both sending and receiving sessions.
Nothing in the KB or either session's `CLAUDE.md` distinguishes the two
paths, so a session has to discover empirically which channel a given
peer-addressing mechanism belongs to before it can reason about failures on
either one.

### What it costs you today
Diagnosing "why can't I reach this session" requires checking two unrelated
subsystems with no signal pointing at either.

### Your current workaround
Empirical testing of both channels independently.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.4 — Documentation gap: an enterprise policy that disables Claude Code hooks silently defeats the `dax wake` Stop-hook notification path [label: feature-request]

### The outcome you need
The `rename` skill's description of the normal experience ("the `dax wake`
Stop hook... turns the next delivery into a session turn — no MCP") should
name its own precondition: this requires `settings.json` hooks to be enabled
at all. On a machine where a managed/enterprise policy disables hooks
wholesale, this silently degrades to "the watch process's own log/spool is
the only signal," with nothing telling the operator or the session to expect
that, or how to monitor for it instead.

### The workflow that hit the gap
On a hooks-disabled machine, proving live delivery actually worked required
manually tailing the watcher's spool file and output log — a workaround this
investigation had to invent on the spot, with no doc pointing at it.

### What it costs you today
A session on a hooks-disabled machine has no way to know it's missing the
documented notification experience, or what the fallback monitoring loop
should look like, until it works this out by hand.

### Your current workaround
Manual tail-based monitoring of the watcher's spool/log files.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above. Not itself dax's bug (the
  policy is enterprise-side) — filed as a documented-interaction gap.

---

## §52.5 — Feature request: a clone-relative skill (e.g. `feedback`) has no way to resolve the solet's own clone root from a client-repo `cwd` [label: feature-request]

### The outcome you need
A skill that resolves its filing target from `git remote get-url origin` and
drafts into that repo's `workbench/` should still be usable when invoked from
a session whose `cwd` is a client repo, not the solet's own clone.

### The workflow that hit the gap
Running the `feedback` skill from a session in a client repo produced (real
values replaced with fabricated equivalents that reproduce the same
mismatch):
```
$ git remote get-url origin          # run from the client repo
git@github.com:some-org/client-repo.git
$ git -C ~/Workspace/dax remote get-url origin
git@github.com:solet-public/macos-bizops.git
```
The skill correctly detects this mismatch and stops per its own Step 2 rule
— not silently wrong. But the underlying assumption is categorically false
for this deployment shape: no session running in any repo other than the
solet's own clone will ever have a `cwd` inside it. Any clone-relative skill
hydrated into a client repo hits this same dead end every time it's invoked
from there.

### What it costs you today
Any clone-relative skill (feedback filing, and potentially others that
resolve `origin`/`workbench/` from `cwd`) hydrated into a client repo is
unusable from that repo, with the failure only discoverable by trying it.

### Your current workaround
Hand off to a session actually running in the solet's own clone.

### Content gate
- [x] No personal identifiers or business specifics — the git remote example
  above uses fabricated org/repo names, not the real ones.

---

## §52.6 — Defect + feature request: `peer_send_by_name`'s delivery-state ambiguity, and no self-check for a role claimed but not actually live [label: defect]

### The exact command or action
`peer_send_by_name` targeting a role whose session has a role-binding record
but no currently-running watcher process (e.g. after that session's watcher
died or was never armed, including across a `/clear`).

### The observed output
The call returns `success: true` with `delivery: "queued_for_replay"` — the
same top-level success shape as a real, live delivery (`delivery:
"queued_watcher"`). A caller has to know to compare the `delivery` field by
hand; nothing flags the difference proactively. On the receiving side, the
stale binding produced zero self-diagnostic signal — the session believed
itself correctly launched (all launcher env vars present and correct) and
had no way to discover its own watcher wasn't running until an external
peer's send bounced.

### The expected behavior
Either the response surfaces the distinction plainly (e.g. a
`recipient_last_seen` field or an explicit warning string, not just an
opaque enum the caller must know to check), or a self-check verb / periodic
self-heartbeat inside `dax watch` detects "I hold a role claim but my own
watcher isn't actually live" and alerts or self-heals — instead of requiring
an external peer's failed send to surface the gap.

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.7 — Defect: `dax schema`'s returned invocation shape does not match what `dax call` accepts on the CLI [label: defect]

### The exact command or action
```
dax schema plugin::agent_messaging_plugin::peer_send_by_name
dax call plugin::agent_messaging_plugin::peer_send_by_name '{"process": {...}, "reason": "...", "arguments": {"name": "...", "content": "..."}}'
```
(arguments shaped exactly as `dax schema`'s own `invocation_schema` shows,
wrapped in `{process, reason, arguments}`).

### The observed output
```
Error: Got unexpected extra arguments (in ...)
```
Passing the bare arguments object directly as the second positional
argument (`dax call <verb> '{"name": "...", "content": "..."}'`) succeeds.

### The expected behavior
`dax schema`'s output should describe the shape `dax call` on the CLI
actually accepts, or the CLI's help text / the schema command's own output
should note that the shown envelope is for a different calling convention
(the internal process-call/MCP shape) than the bare-arguments CLI form.

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.8 — Defect: a system-relayed message's own embedded reply address does not resolve [label: defect]

### The exact command or action
Following the literal reply instructions embedded in a system-relayed peer
message ("reply via peer_send with peer_id=system,
peer_agent_instance_id=system:scheduler") by calling:
```
dax call plugin::agent_messaging_plugin::send_peer_message '{"peer_id": "system", "peer_agent_instance_id": "system:scheduler", "content": "..."}'
```

### The observed output
```
peer_send_failed: peer_unreachable: no binding for 'system'/'system:scheduler'
```

### The expected behavior
Either the embedded reply-address is a real, reachable binding, or the
relay's own instructions should say to reply via `peer_send_by_name` to the
original sender's role instead (which is the path that actually works, and
was only found by trial).

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.9 — Defect/documentation: `peer_send` vs `send_peer_message` naming is inconsistent across surfaces and docs [label: defect]

### The exact command or action
```
dax schema plugin::agent_messaging_plugin::peer_send
```

### The observed output
```
Error: Process not found: plugin::agent_messaging_plugin::peer_send
```
The KB's own HTTP-reference article documents `mcp__<server>__peer_send` as
the tool name, and system-relayed messages embed "reply via peer_send" as a
literal instruction. The real CLI verb (found via `dax search`, not
discoverable from the relayed text or that doc) is `send_peer_message`.

### The expected behavior
One name used consistently across the MCP tool surface, the no-MCP CLI verb,
the KB docs, and the text embedded in relayed messages — or, if the names
are legitimately different per transport, that difference stated explicitly
wherever the name is used.

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.10 — Feature request: reduce cross-repo `CLAUDE.md` duplication and staleness risk [label: feature-request]

### The outcome you need
Only two `CLAUDE.md` files should need to be authoritative for any given
session: the operator's own home-directory file, and the current
repository's own file. Generic, cross-repo "how a solet works" documentation
copy-authored into every consuming repo's `CLAUDE.md` is a staleness risk by
construction and should live in the solet's own KB instead, queryable on
demand.

### The workflow that hit the gap
A client repo's own `CLAUDE.md` carried a "solet integration" section
describing solet-specific concepts (fleet roles, session-label env vars, a
retired session-broker mechanism, git-gating mechanics) that a session had
to absorb before it could even ask the solet a simple question — and at
least one claim in it was actively wrong against what this investigation
empirically found: it described the solet as "not scoped to this repo," when
in practice this exact repo's own session transcripts *are* ingested into
the session ledger within minutes (confirmed empirically: a session's own
first message appeared there roughly 4 minutes after being sent).

Root cause of how the stale claim got there in the first place: see §52.13 —
the copied prose was almost certainly authored from what the seed's own KB
and source were surfacing at the time, which still used the now-retired term
throughout.

### What it costs you today
Duplicated, unowned documentation drifts out of sync with what the solet
actually does, and a session has to read and reconcile it before it can
trust anything the solet's own KB says.

### Your current workaround
None — the discrepancy was only caught by empirical testing.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above. The client repo is referred
  to generically, never by its real name.

---

## §52.11 — Documentation gap: separate per-session Claude-Code auto-memory vs solet memory is undocumented [label: feature-request]

### The outcome you need
Something should state plainly that each Claude Code session/project has its
own, separate, harness-native "auto-memory" folder — a Claude Code app
feature, not a solet feature — and that it is invisible to the solet's own
memory/KB and to every other session, including another session of the same
person's, in a different project.

### The workflow that hit the gap
Confirmed empirically (filesystem check, not self-report) that two sessions
in two different projects on the same machine each have their own,
completely separate memory folder with its own index file, neither visible
to the other or to the solet's own memory system. A solet's own project
`CLAUDE.md` describes the solet's own "memories, knowledge bases, and
plugins" without ever mentioning that this second, harness-native layer
exists at all — a reader relying only on that file could reasonably assume
"memories" covers everything.

Practical consequence: operational knowledge *about* the solet itself
(verb-naming quirks, a root-caused defect, latency characteristics) that one
session wrote down went into that session's own project-scoped memory file —
useful to a future session in that same project, invisible to a session in
any other project, and invisible to the solet's own KB where every querying
session would otherwise benefit from it.

### What it costs you today
Knowledge about how the solet itself behaves gets siloed per consuming
project's own memory folder instead of accumulating in one place every
session can query.

### Your current workaround
None currently — this round's own findings are an example of the workaround
(compiling cross-session findings by hand into a shared file) rather than a
structural fix.

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.12 — Defect (root-caused, fix included): project-path decoding is lossy for usernames or repo names containing `.` or `-` [label: defect]

### The exact command or action
`_decode_project_dir()` in
`plugins/claude_code_filesystem_session_source_plugin/src/claude_code_filesystem_session_source_plugin/plugin.py:473`:
```python
return "/" + name[1:].replace("-", "/")
```
The function's own docstring states it reverses `cwd.replace('/', '-')`
only. That is incomplete: Claude Code's actual directory-naming convention
replaces **both** `/` and `.` with `-`.

### The observed output
Fabricated example reproducing the exact defect shape (values chosen to
demonstrate the mechanism, not the operator's real ones): a `cwd` of
`/Users/jane.doe/Workspace/multi-word-repo` is stored on disk as
`-Users-jane-doe-Workspace-multi-word-repo` (the dot in the username is
indistinguishable from a path separator once encoded). Decoding it back
produces `/Users/jane/doe/Workspace/multi/word/repo` — silently wrong, not
an error.

### The expected behavior
Any operator whose username contains a dot, or any repo whose name
legitimately contains a hyphen, should get a correct `project_path` back.
This isn't cosmetic — a corrupted `project_path` misattributes session-ledger
data to the wrong (nonexistent) project for any consumer, human or agent,
that trusts the field.

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Diagnosis (marked as inference, verified against source)
The fix looks low-risk: the function's own docstring already notes "the
first JSONL line still carries the authoritative `cwd` field for any
consumer that needs to disambiguate" — confirmed true, present verbatim from
the first non-`queue-operation` record on. Reading `cwd` directly from the
transcript instead of reverse-decoding the lossy directory name would fix
this without needing to solve the general "is a hyphen a slash, a dot, or a
real hyphen" ambiguity at all.

A broader scope check (grep across the live deployment's rendered config/
state and this clone for unsubstituted template placeholders or foreign
build-environment paths) found nothing beyond legitimate template source and
test fixtures — this appears to be an isolated defect, not a symptom of a
wider path-handling problem.

### Content gate
- [x] No personal identifiers or business specifics — the reproduction
  example uses fabricated username/repo values, not the operator's real
  ones.

---

## §52.13 — Defect: the "homunculus" → "solet" terminology migration is incomplete in live plugin source [label: defect]

### The exact command or action
```
grep -ri homunculus plugins/agent_messaging_plugin/src plugins/macos_self_deployment_plugin/src plugins/github_midwife_plugin/src
```

### The observed output
The retired term is still present in live plugin source, not just
historical workbench notes, including:
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/env_contract.py`
  (a literal string `"seed-born homunculus"` and a reference to *"the
  `homunculus` console script"* by that literal name)
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/plugin.py`,
  `mcp_streamable/tools.py`, `mcp_streamable/notifications.py`,
  `mcp_bridge/__main__.py`, `mcp_bridge/forwarder.py`, `local_cli/__init__.py`
- `plugins/macos_self_deployment_plugin/src/macos_self_deployment_plugin/constants.py`
- `plugins/github_midwife_plugin/src/github_midwife_plugin/credential_seed.py`
- A KB article filename that still says "homunculus" —
  `plugins/agent_messaging_plugin/knowledge_base/06_codex_homunculus_wake_runbook.md`
  — which a session actually received verbatim from a normal
  `knowledge_service::search` call, under the title "Retired Codex
  Homunculus Wake Runbook (Historical)."

A deliberate rename effort clearly happened
(`deployment/scripts/migrate_to_solet.py`,
`workbench/2026-08-14_seed_update_solet_rename_plan.md` both exist in this
clone) but did not reach these files.

### The expected behavior
Either the rename is completed in live plugin source, or — if
`env_contract.py`'s literal `homunculus` console-script name is intentionally
retained for a compatibility reason — that is stated explicitly so it isn't
mistaken for leftover debt. The KB article's filename should be renamed or
clearly flagged so it doesn't keep surfacing the retired term as if current.
This is very likely how the term ends up copied into consuming repos'
`CLAUDE.md` files in the first place (§52.10) — a session authoring that
documentation is working from exactly what the seed's own KB and source
surface.

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## §52.14 — Defect/documentation: session-ledger ingestion lags a currently-open session, undocumented [label: defect]

### The exact command or action
Query `session_ledger_service::search_event_content` for content known to
exist in a specific, currently-open (not yet closed/idle) session's own
transcript, immediately after that content was produced.

### The observed output
Zero relevant hits across two differently-phrased queries, while the target
session's own transcript file on disk was independently confirmed to be
actively updating (its modification time matched the query time almost
exactly). The content existed on disk; the ledger had not yet indexed it.

### The expected behavior
The ledger is documented (in this deployment's own onboarding guidance) as
the answer to "any question, ever" about session history and cross-session
context. That's true for closed or idle sessions, but for a currently-open
session there is an unstated indexing lag, and nothing signals "this
session's recent activity may not be searchable yet" — a caller has no way
to distinguish "no such content exists" from "content exists but hasn't been
indexed yet."

### Release measured against
HEAD `36dba33` / RELEASE_NOTES.md "2026-08-20."

### Content gate
- [x] No personal identifiers, employer/business specifics, credentials, or
  transcript excerpts carrying any of the above.

---

## Filing plan (as executed)

Operator explicitly declined the parent-wrapper convention for this round
("I don't care about the parent wrapper" → confirmed as "skip it") — filed as
14 fully standalone issues, no parent, no sub-issue linking. Filed via
`gh issue create --title/--body-file` (not `--web`), each body reproducing
every field the repo's issue form requires plus this round's filing-note
disclosure, per the operator's standing scripted-filing instruction from
§47.8 (#26, still open).

## Filed

All 14 items filed 2026-09-15 as standalone issues (no parent):

| Item | Issue |
|---|---|
| §52.1 | [#42](https://github.com/solet-public/macos-bizops/issues/42) |
| §52.2 | [#43](https://github.com/solet-public/macos-bizops/issues/43) |
| §52.3 | [#44](https://github.com/solet-public/macos-bizops/issues/44) |
| §52.4 | [#45](https://github.com/solet-public/macos-bizops/issues/45) |
| §52.5 | [#46](https://github.com/solet-public/macos-bizops/issues/46) |
| §52.6 | [#47](https://github.com/solet-public/macos-bizops/issues/47) |
| §52.7 | [#48](https://github.com/solet-public/macos-bizops/issues/48) |
| §52.8 | [#49](https://github.com/solet-public/macos-bizops/issues/49) |
| §52.9 | [#50](https://github.com/solet-public/macos-bizops/issues/50) |
| §52.10 | [#51](https://github.com/solet-public/macos-bizops/issues/51) |
| §52.11 | [#52](https://github.com/solet-public/macos-bizops/issues/52) |
| §52.12 | [#53](https://github.com/solet-public/macos-bizops/issues/53) |
| §52.13 | [#54](https://github.com/solet-public/macos-bizops/issues/54) |
| §52.14 | [#55](https://github.com/solet-public/macos-bizops/issues/55) |
