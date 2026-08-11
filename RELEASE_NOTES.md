# Release notes

Newest release first. Earlier releases follow below the divider.

---

## 2026-08-10 (second update) — response to adopter feedback Parts 36–40

Five feedback parts arrived after this morning's release; every defect they
reported is fixed in this update, and the capability they requested is
accepted with its design complete. All four fix packages below were verified
with named failing mutations (the fix reverted reproduces the exact reported
failure), and the born-clone items against a born-clone-shaped fixture rather
than a development checkout.

**The vault-read envelope bug is fixed — bearer tokens survive restarts now
(§36.2).** The bridge's bearer-token HMAC signing key was silently re-minted
on every restart because the read path checked for a `status` field the vault
backend never returns, so an existing key was never recognized. Any deployment
with Streamable-HTTP MCP bearer auth enabled was invalidating every
outstanding client token on every boot — including, we measured after your
report, the origin deployment itself. A single vault-read seam now keys on the
envelope the vault actually returns, a malformed envelope fails loud instead
of reading as a miss, and the shipped smoke's fake vault returns the real
envelope shape so this class cannot false-clean again. The audit you asked for
ran costume-aware across the tree: one more same-class hit fixed, ten
lookalike sites swept and documented benign.

**Born clones can spawn workers on every host driver (§36.3, both drivers).**
`verify_config` required `.mcp.json` to exist even for watch-transport workers
that receive an inline empty MCP config and never read the file — so the very
first spawn on a fresh clone refused. Both the headless and tmux drivers (the
latter carried the identical check, found in the same audit) now require the
file only when the resolved transport is `mcp`, and the refusal message states
exactly what satisfies it.

**Your tmux third-party-provider fix is canonicalized (§39.1 / §40.1).**
Taken exactly as you field-verified it — the inert dev-channels flag is
omitted rather than waited on; with the flag absent the confirmation
expect-loop is never entered, and the first-party path is byte-for-byte
unchanged. Two deliberate bounds, stated honestly: the predicate keys on the
effective spawn environment (rather than a provider argument our tree does not
carry yet), and its marker set is exactly the one with live evidence behind
it — your verified Bedrock spawn. Other third-party provider families get
their markers when the accepted §36.1 registry lands, sourced from vendor
documentation rather than inference; the code comment names that extension
path. The headless driver keeps the flag with a documented rationale (it has
no confirm loop, so the flag is inert there with no failure mode).

**Both LaunchAgent plist renderers now emit a PATH (§39.2 / §40.2).** A
launchd daemon inherits the bare system PATH, so Homebrew-installed binaries
(tmux first among them) were invisible even when correctly installed, and the
recovery required exactly the hand-edit-a-live-plist procedure your report
described as ugly — it was. Both the self-deployment and genesis-time
renderers now write a deterministic PATH with the Homebrew locations ahead of
the system defaults.

**A born clone can run its own commit gate (§37 / §38).** The gate
orchestrator hard-referenced two paths the seed never shipped
(`deployment/scripts/check_gate_toolchain.sh` and the root `pyproject.toml`)
with no existence guard — a raw traceback before a single gate ran. Both are
now guarded with clean fail-loud messages AND shipped in the seed. The root
cause you ran down in Part 38 is fixed as scoped there: the gate toolchain
(`ruff`/`pyright`/`radon`) lives in a deliberately-mandatory `ananta[gate]`
extras group the birth-time provisioner never installed; it now installs that
group specifically, without sweeping in any plugin's deliberately
absence-tolerant extras.

**Per-spawn provider selection is accepted upstream (§36.1) — design
complete, implementation scheduled.** Your layering survives review intact:
the shim resolves, the verb stays secret-free, strip-then-set, credentials
vault-resolved and never persisted. The accepted design generalizes it to a
declarative registry covering the six vendor-documented provider families
with an operator extension path, validates model identity per provider
family, and persists the provider name (never credentials) so a restarted
worker keeps its provider. Two findings from review you may want locally in
the meantime: `-e VAR=""` on tmux sets rather than unsets (the canonical
version uses a real env unset), and a restarted worker under your variant
silently reverts to the daemon's provider.

**Queued, not in this mint (§36.4):** the three Marketo asks (vendor error
code surfacing, in-plugin retry on idempotent long-window reads, documented
per-verb `row_limit` ceilings) are filed with the existing Marketo package to
land together.

---

## 2026-08-10 (first update) — multi-session self-management

**Update your homunculus.** This release adds the multi-session
self-management capability described below — your homunculus can now
spawn, monitor, hand off work between, and safely retire other agent
sessions of itself, with an operating manual (the maintenance-verbs joseki
cards) that ships in the same update. If you've been running a single
always-on session, updating is what makes the fleet capability available
to it; nothing about your current setup breaks if you don't use it yet.
The seed's own update runbook (`plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md`,
or its plain-language companion `06_seed_update_operator_guide.md`) walks
an already-running homunculus through picking this up — point a coding
agent at it and ask it to "update me."

This is the first release-notes package shipped with this seed. Previous
updates carried no equivalent document, which meant a deliberate scope
decision and a plain omission looked identical to anyone reading from
outside — this document exists to close that gap, going forward as a
standing practice, not just for this release.

## Why you should update

Four real defects are described below. Two are already fixed in this
release; two remain open, each disclosed with its exact scope. Two of the
four were found through actual use of this platform outside its original
environment — not discovered internally — which is itself worth knowing:
real use surfaces real defects that review alone did not catch. Separately,
one change below alters how you should interpret your own gate output going
forward, even though nothing in your own setup changes — worth reading
regardless of whether you act on anything else here.

## What changed

**A cloned seed now receives a working `.gitignore`.** The birth step that
writes `.gitignore` sat behind a check for whether a `.git` directory
already existed, intended to avoid clobbering an existing setup — but a
seed obtained by cloning its repository *always* already has `.git`, so
that check was always true and the file was never written, for every
adopter who got the seed the normal way. The write now happens regardless
of which shape of tree it's running against, and an existing `.gitignore`
is still never overwritten. **If you update an existing clone, the file
will arrive untracked** — genesis must never touch your git history, so
it writes the file to disk without staging or committing it; seeing a new
untracked `.gitignore` after updating is correct, not a symptom of
anything. Commit it yourself when convenient. If you already have your
own `.gitignore`, this change does not touch it. Without this fix,
`__pycache__` directories, your `.venv`, and other runtime state stay
unfiltered from git's view — one `git add -A` away from being committed
alongside the credential and vault material a proper `.gitignore` is
supposed to keep out.

**The hydration template now states Step Zero as a first action, before
its first use.** The template previously referenced "Step Zero" three
times without ever defining it, and stated the knowledge-base-search
expectation in a way that read as advisory — easy to read past under
time pressure, including for tasks that look like plain code or config
work, or where the answer feels already known. A short block now opens
the template, naming Step Zero before anything else references it and
stating plainly that acting from prior assumptions, source reading, web
search, or MCP tools before searching the homunculus is a defect, not a
shortcut. If you've been carrying a local edit to this template to
strengthen the same instruction, you can drop it — diff first if you want
to confirm nothing else in your local version is worth keeping.

**Gate output now tells you the difference between "passed" and "declined
to run."** Previously, a smoke test that detected a required tool or
service was missing — an absent type-checker, an unavailable optional
dependency — printed its own line saying so, exited 0, and was counted as
a pass at every level: the per-entry line, the aggregate total, and the
process exit code. The text disclosing the skip was captured and then
discarded unless the smoke also failed outright. **If you have ever read
an `N/M passed` figure from this platform's gate output, some unknown
fraction of that N may have been declined smokes, not passing ones, and
nothing in the output told you which.** The runner now reports passed,
skipped, and failed as three distinct outcomes, with a dedicated exit
code for a disclosed skip and its own count in the summary line, plus an
optional strict mode to treat any skip as a hard failure for callers that
want zero tolerance. Re-read your gate output after updating; a skip is
now visibly a skip. Nothing needs to be redone for a prior run — the
number wasn't wrong, it just meant less than it looked like it meant, and
now the meaning is legible.

**Multi-agent session management** — spawning, monitoring, and
coordinating other agent sessions from your own — gained substantial new
capability in this release: session lifecycle tracking through a durable
state machine, dispatching follow-up work into an already-running
session rather than only at spawn time, formal handoff of a durable named
role from one session to another, two ways to run a spawned session
unattended (fully non-interactive, or driving a real interactive
terminal), a registered-presence message transport as the new default for
newly spawned sessions, automatic rotation for a session approaching its
context limit, and per-session token usage accounting. None of this
affects a homunculus that never spawns other sessions.

**Session-registration behavior clarified — a registration and a live
watcher process are different things.** A durable role registration
(a name like "the session currently speaking for X") can outlive the
process that claimed it; a live watcher process is what actually holds
the delivery route. Specifically: arming a second watcher *while one is
genuinely running* is refused, naming the running process. Arming when
*no watcher process is alive* — even though a registration row still
exists — is permitted, and re-points the delivery binding to the new
arm. That second case is the intended reconnect path, not a missed
refusal: a successful arm is itself evidence nothing was actually holding
the lock. If you're addressing sessions by a registered role name and
something seems unresponsive, check whether a watcher process is actually
alive, not just whether the role is claimed — those are two different,
both-legitimate states.

**Session-ledger data-integrity fixes.** Deterministic handling of two
rows describing the same external event (previously outcome depended on
read order), a repair path for a source that produced duplicate records,
a way to safely disable a source without deleting its history, and a
declared natural key on the event table so a given external event can't
be recorded twice even under concurrent ingestion.

**Postgres connections can now write, if the credential you registered is
allowed to.** Previously every Postgres connection this platform opened was
hard read-only — no write verb existed at all, regardless of what your
database credential could do. That blanket restriction is reversed: one new
verb, `run_statement`, opens a connection without the read-only flag and
executes whatever statement you give it. What it can actually do there is
entirely your own database's decision — this plugin performs no
write-permission check of its own; your Postgres GRANTs are the only
control plane. If you want a connection that can never write no matter what,
register it under a read-only database role; that boundary now lives in
your database, not in this platform. Every existing read verb is completely
unaffected — reads still open strictly read-only connections, exactly as
before. **Snowflake gets the same reversal in this release**, one new verb
`run_statement`, same design: no plugin-side write-permission check, your
registered role's own grants are the entire control plane. One structural
difference from Postgres worth knowing if you rely on it: Snowflake has no
session-level read-only connection flag at all, for either read or write
verbs — Postgres's read verbs structurally refuse a write at the server
even for an over-privileged credential, Snowflake's do not (the boundary
there is entirely which grants your registered role has, for both read and
write verbs alike). **Two things about the Snowflake write verb are
disclosed as open, not fixed by this release** — see Known Issues below.
Salesforce, Zuora, and Jira were already write-capable before this release
and are unaffected either way.

**Internal hygiene: identifying information removed from source and test
files.** A number of source comments, docstrings, and test fixtures
across several plugins previously cited internal review references by an
identifier scoped to a specific deployment, or carried that deployment's
operator's personal name, email, or account handle as literal values.
All of it has been removed or replaced with synthetic placeholders where
a realistic-looking value was needed for a test to remain meaningful.
None of this shipped in any previously-sealed release. No behavior change
results, except one environment-variable rename and one default-value
removal, both confined to test-only or fallback code paths.

**The coordination-hooks plugin previously required `node` for four of its
five hooks; this is now fixed and independently re-verified by this
lane against this checkout's own actually-installed plugin copy, not just
the source tree.** As disclosed in an earlier revision of this document,
this platform names Python 3.13 as its only dependency but four reminder
hooks (a knowledge-base-first prompt and a check-your-messages prompt on
every submitted prompt, a check-your-messages prompt and a role-binding
prompt at the start of every session, and an opt-in idle-wake waiter at
the end of a turn) were invoked via `node` regardless — on a node-less
machine those four silently did not run, worst of all for the idle-wake
waiter, whose correct steady-state behavior (wait quietly, no output) is
indistinguishable from that failure. **All four have since been ported to
`python3`** and the old `.js` originals removed (source commit
`1dc804b00`, 2026-08-08). Re-verified independently by this worker two
ways: (1) direct read of the shipped `hooks.json` — every one of its five
hook entries now invokes `python3`, none `node`; (2) a byte-level check
against this checkout's own **installed Claude Code plugin cache** at
`~/.claude/plugins/cache/<marketplace>/coordination-hooks/0.3.0/hooks/hooks.json`
— the actual file the plugin loader executes, not the source tree — which
confirms all five commands there are `python3` too. The landing commit
itself flagged an owed live at-path verification before this seed's next
mint; this check is exactly that verification, for this machine, and it
passes. If you update from an older install, this fix reaching your own
running copy still depends on the plugin-cache refresh step in the seed
update runbook (the cache is version-keyed and does not update on its
own) — that mechanism is unchanged by this fix, only the defect it used
to carry.

**The coordination-hooks plugin gained two more hooks, then a fourth
capability set, in this release (`0.4.0` then `0.4.1`).** `0.4.0` vendors
a heartbeat and a rotation-due watcher directly into the plugin, both
wired as `PostToolUse` hooks; `0.4.1` — the same commit — additionally
vendors the memory-passthrough capture/session-context pair and a
new origin-resolution ladder for deciding which homunculus a session's
memory writes belong to. All of this is part of this release's
self-management enablement work — see "Update your homunculus" above and
the seed's own update runbook for the exact hook list and the
`HOMUNCULUS_NAME` export this ladder's first rung depends on. **Not yet
tested end-to-end on a fresh adopter install** — see Known Issues below.

**Salesforce, Schwab market data, Jira, and Snowflake's read verbs moved
onto an async dispatch/completion shape (no verb behavior change).**
Fourteen verbs across these four plugins (Salesforce: all 9 real-I/O
verbs; Schwab: `get_options_chain`; Jira: 7 verbs plus `jql_search`,
`list_comments`, and `test_connection`; Snowflake: 7 read verbs) now
return a `{job_id, status: "queued"}` envelope in milliseconds instead of
blocking on the underlying network call, with the actual work completing
on a background worker thread and the result delivered through the same
completion channel every other async verb on this platform already uses.
This is a dispatch-shape change only — access control, output contracts
(TSV export limits, inline-vs-file delivery), and what each verb actually
does are all unchanged; if you call these verbs programmatically rather
than through an agent that already handles the async envelope, you will
need to poll for completion instead of getting an inline result.

**A packaging gap that kept the coordination-hooks plugin undiscoverable
in a freshly-cloned seed is fixed.** The plugin's own source shipped
correctly via this seed's normal birth mechanism, but the root-level
marketplace registration file Claude Code needs to discover and install
it (`.claude-plugin/marketplace.json`) was never included in the seed's
own copy manifest — a fresh clone had the plugin's code but no way for
Claude Code to find it. Fixed by adding it to the manifest's copy list;
no code path changed. The same commit also bumps the plugin from `0.3.0`
to `0.3.1` to pick up an earlier real behavioral change (the bounded
idle-wake wait) that had landed without its own version bump — an
already-installed `0.3.0` would otherwise have kept running the old
unbounded wait indefinitely, with no local signal anything was stale.

**Memory-head curation gained two new verbs.** `generate_curation_report`
surfaces activation-ranked demotion candidates from the ambient memory
index; `reinforce_by_slug` wires a cite into a reinforcement of the memory
it cited. Both follow the same async dispatch shape described above. Every
demotion decision from a curation report is still a human/seat judgment
call — there is no automatic trimming.

## Known issues — disclosed, not yet fixed

**Two gate-register entries cannot pass on a freshly-born clone.** The
shipped quality-gate register contains two entries that each read a local
path (a Claude Code session-surface path, and a plugin-marketplace path)
this platform's own root-strictness contract declares a born clone may
never have — both are origin-only by construction. **What to expect from
your own gate run:** measured end-to-end on a real, freshly-born clone of
the currently published seed, 247 of 249 register entries pass, and the
two failures are exactly these entries. Anything else failing is worth
investigating; these two specifically are not a broken install. A
publication-time check that would catch this class before a seed ships
has been designed and measured; it is not built yet, and is sequenced
behind other in-progress work. No date is promised.

**The `marketo_plugin` ships in this seed but is not runtime-dispatchable
on any machine that doesn't add it to its own live platform manifest.**
This is a per-adopter manifest/credentials fact, not something this seed
can decide for you at ship time — the plugin's code, tests, and knowledge
base are all present and correct; whether a given homunculus can actually
call it depends on that homunculus's own manifest. If you try a Marketo
verb and get a not-found error, check your manifest before assuming
something is broken.

**Two Jira verbs were not migrated onto this release's async dispatch
shape: `add_attachment` and `download_attachment`.** An earlier
blocking-I/O inventory pass classified both as "clean" (non-blocking) by
source-scanning for a recognized I/O pattern; that scan's own bounded
trace missed a blob-write code path in both verbs, a disclosed
false-negative in the scan itself, not a defect in this release's
migration work. Both verbs are unaffected otherwise and continue to work
exactly as before — this only means they were not part of this release's
async-shape work and remain on the prior synchronous path.

**The Snowflake write verb (`run_statement`, this release) has two open
questions, not yet answered by measurement.** First, Snowflake's own
support for a `RETURNING`-equivalent clause depends on the target object
and has not been characterized here — the connector will roll back rather
than silently commit-and-discard if such a clause produces rows with no
export path given, but which statements actually produce rows this way
is not documented. Second, **this release ships with no live write smoke
against a real Snowflake account** — every connection this platform
currently uses is pinned to a read-only role by the operator's own
registration, so a live write was never exercised end-to-end, only
tested against a fake client. If you register a write-capable role and
use `run_statement`, you are the first live exercise of that path.

**Neither the `0.4.0`/`0.4.1` coordination-hooks vendoring nor the
memory-passthrough origin-resolution ladder has a live adopter-install
test as of this release.** Both were verified against this checkout's own
source and (for `0.3.0`) an installed plugin-cache copy — see "What
changed" above — but not against a fresh clone going through the full
install → hydrate → first-launch path end to end. Mitigations already in
place: every hook in this stack degrades to a fail-silent no-op rather
than crashing a session when its arming environment variable is unset
(see this release's own `SECURITY.md` Configuration surface section), the
existing parity smokes cover the vendored code directly, and this
release's update runbook includes an explicit post-update hook-verification
step (see "After updating: verify your hooks actually fire" in the
runbook). None of that substitutes for an actual fresh-install run; treat
this stack as freshly landed rather than battle-tested until one happens.

## How to safely update

1. **Check you're starting from a healthy, unmodified clone.** Confirm
   your homunculus responds normally, and note that files like
   `AGENTS.md`, `CLAUDE.md`, and any `client/` directory showing as
   untracked or modified in `git status` is normal — those are generated
   for your own machine and an update never touches them directly.
2. **Pull, fast-forward only, never merge or rebase.** A refusal to
   fast-forward means your history has diverged from the seed's — stop
   and ask, rather than forcing it; this is not a routine occurrence.
3. **Most updates need no dependency install.** Only if you're told a
   specific update adds a new component do you need an editable-install
   step for it; running that step when it isn't needed is harmless.
4. **Restart, preferring a zero-downtime swap over a bare restart** when
   your profile has a local blue-green router available; otherwise a
   plain restart is always safe.
5. **Wait for startup to fully settle before your first query afterward.**
   Anything that looks alarming immediately after a restart — a brief
   "no active color" response through a router, for instance — is a known
   transient state that clears itself within seconds; re-probe rather than
   assuming something broke.
6. **Check for a newly-untracked `.gitignore` at the root** and commit it
   when convenient (see "What changed" above) — expected, not a symptom.
7. **Refresh anything that lives outside the platform's own restart path.**
   A restart makes the platform run new code; it does not, by itself,
   refresh an already-open connection from an external client, nor an
   installed Claude Code plugin's cached copy of shipped hooks. If an
   update specifically changes those surfaces, it will say so, and the
   fix is a fresh client session plus an explicit plugin refresh, not
   another restart.
8. **Verify.** A normal health check, plus a knowledge search that
   returns content matching this release's notes, confirms you're
   actually running what you think you're running. Re-reading your gate
   output after this update, now that skips are visible, is also worth
   doing once.

The full agent-executable version of this procedure — the one a
clean-context Claude Code session can run end to end — lives in this
seed's own knowledge base (search for "seed update runbook") and is kept
current independently of this document.
