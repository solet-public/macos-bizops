# Updating Your Solet — A Step-by-Step Operator Guide

Tags: knowledge:tag:seed_update, knowledge:tag:operator_guide, knowledge:tag:solet_lifecycle

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Plain-language, no-jargon walkthrough for the solet's OWNER to bring an already-running solet up to date with a newer published seed release, written to be followed directly at a terminal rather than requiring a coding agent to drive every step — when a pulled update needs a dependency install, when it needs generated files (AGENTS.md, CLAUDE.md, shell integration, Claude Code hooks) re-rendered and why that specific step needs a coding agent's help even though the rest does not, verifying the user-scope CLAUDE.md instruction section is actually installed rather than assumed, configuring a business-connector export/workspace root on an already-updated solet so record reads don't fail loud, the Claude Code plugin cache-refresh step people skip because everything looks like it worked without it, a verification checklist including confirming a newly-live plugin's knowledge is actually searchable plus behavioral read/override/refusal checks for connectors only verifiable on the owner's own machine (Marketo, Zuora), and a closing note on shaping newly-authored joseki cards now that connector reads never return record values inline. Companion to `05_seed_update_runbook.md`, which is written to the coding agent performing the same update and carries the full technical detail and measurement history this guide deliberately leaves out.

## When to use this guide

You've been told a new version of your solet is available and want to
bring it up to date without losing anything it has learned or remembers.
This guide assumes no prior knowledge of how the update was built — just
that you have a solet already running on your computer.

**When NOT to use this:** if your solet won't start at all, or you
want a completely fresh instance, this is the wrong guide — ask whoever
gave you this document for the alternative.

**If you have a coding agent (Claude Code or Codex) available and you'd
rather have it drive the whole process:** point it at
`05_seed_update_runbook.md` in your solet's own knowledge base instead
— search for "seed update runbook" — that version is written directly to
the agent and covers everything below in more technical depth. This guide
is for running the update yourself, at a terminal, when that isn't how
you'd rather do it.

## Before you start

- Make sure your solet is currently running: run `<name> health` in a
  terminal (replace `<name>` with your solet's name throughout this
  guide) and confirm you get a healthy response, not an error.
- You'll need a terminal window and about 10 minutes, most of which is
  waiting.

## Step 1 — get the update

In a terminal, go to the folder where your solet lives and run:

```bash
git pull --ff-only
```

**If this succeeds:** continue to Step 2.

**If it refuses** with a message about not being able to fast-forward:
stop here and get in touch with whoever gave you this guide. This means
something unusual has happened to your local copy and needs a look before
continuing — don't try to force it through.

You may also notice files like `AGENTS.md`, `CLAUDE.md`, or a `client/`
folder showing up as changed or untracked if you check `git status` — that
is normal and harmless; those are files generated for your own machine and
the update never touches them directly.

## Step 2 — install any new dependencies (only if you were told to)

Most updates need nothing here — skip straight to Step 3. Do this step only
if whoever gave you this guide specifically said the update adds a new
component or changes what your solet depends on:

```bash
cd <your solet's folder>
.venv/bin/python -m pip install --no-build-isolation -e plugins/<the named component>
```

It's safe to run even if you're not sure it's needed — running it again on
something already installed does nothing harmful.

## Step 3 — restart and wait

```bash
launchctl unload ~/Library/LaunchAgents/local.solet.<name>.plist
launchctl load ~/Library/LaunchAgents/local.solet.<name>.plist
```

Then wait about 30–60 seconds before doing anything else. Your solet
is reloading in the background during this time, and using it too soon can
cause errors that look scarier than they are. When in doubt, wait a bit
longer.

*If you'd rather avoid even that brief downtime:* some solets support a
zero-downtime update path instead of the restart above. If you have a
coding agent handy, you can ask it to check and use that path for you
instead of the commands shown here — otherwise the restart above is always
safe and is what most operators use.

## Step 4 — refresh your generated files (only if you were told to)

Some updates change more than your solet's own code — they also change
files that live on *your* side: your project's `AGENTS.md`/`CLAUDE.md`,
your shell setup, or your Claude Code hooks. A restart alone does not
regenerate those; they only update if someone re-runs the setup steps that
originally created them, and that work needs a coding agent's help (it
isn't a plain command you can type — the agent has to compare your current
files against what changed and merge carefully, not overwrite blindly).

Skip this step unless whoever gave you this guide said this update touches
those files. If they did: open a **fresh** Claude Code (or Codex) session
inside your solet's folder and ask it to "re-run step 2 of the seed
hydration runbook" (add "and step 4a too" if you use named multi-session
roles). Let the agent walk you through it — it will show you what's
changing before it touches anything.

**Make sure this explicitly includes the instruction section in your
user-scope `~/.claude/CLAUDE.md`** — this is the one place your coding
agent actually reads its operating instructions from day to day, so if an
update changes those instructions and this file doesn't get refreshed, the
update effectively never reached your agent even though everything else
went fine. This isn't automatic — there is no installer that does it for
you, so it only happens when you (or your agent) explicitly re-run this
step. Verify it's actually there rather than assuming — name your own
solet specifically, not just any solet's section, since this
file can hold more than one if you use more than one solet:

```bash
grep -c "BEGIN SOLET <name> v1" ~/.claude/CLAUDE.md
```

A count of 1 means your section is present. Zero, or the file doesn't
exist yet, means it was never installed or got lost — ask your agent to
render and install it now, the same way, before continuing. (A count
above 1 would mean something merged wrong — re-runs are supposed to
replace your section in place, not duplicate it — flag that to whoever
gave you this guide rather than the file.)

Do this before Step 5 below. Refreshing the plugin without also doing this
step would leave these other files out of date even though the plugin
itself is current.

## Step 4a — configure the export/workspace root (only if you were told to)

Some updates change what your solet requires before it will read from
business systems (Jira, Salesforce, and similar) — specifically, requiring
a folder on your computer where results are allowed to be saved, so records
never land directly in a conversation. Skip this step unless whoever gave
you this guide said this update adds or changes that requirement.

If it does: tell your coding agent where you keep the folders you work in
day to day (the parent folder, not any single project — something like
`~/Workspace`, not `~/Workspace/some-specific-project`) and ask it to
configure that as your workspace root for business-connector results. Your
agent will validate the folder and confirm what it did — if it refuses
your answer, that's expected behavior protecting your solet's own
files, not an error to work around; give it a different folder instead.

## Step 5 — refresh the Claude Code plugin (don't skip this)

This is the step people miss, because everything *looks* like it worked
without it. Pulling the update and restarting brings new files onto your
computer, but a small companion tool your solet uses inside Claude
Code — called a **plugin** — keeps running its own separate, older copy
until you explicitly tell it to refresh. Skipping this step means you keep
running old behavior with no warning that anything is out of date.

**5a. Find your plugin's marketplace name** (a name that was generated
automatically when your solet was first set up):

```bash
cat ~/.claude/plugins/known_marketplaces.json
```

Look for an entry whose `path` points at your solet's folder. The
name of that entry (not `claude-plugins-official`, which is unrelated) is
your marketplace name — you'll use it in the next command.

**5b. Refresh the plugin**, replacing `<marketplace-name>` with the name
you just found. Type `command claude`, not just `claude` — if your terminal
has a shortcut set up for `claude` that adds extra options, typing it plain
can make these specific commands fail to parse correctly, and `command`
sidesteps that safely either way:

```bash
command claude plugin uninstall coordination-hooks@<marketplace-name> --scope local
command claude plugin install coordination-hooks@<marketplace-name> --scope local
```

You should see a confirmation message after each command. If either one
reports an error instead, stop and get in touch with whoever gave you this
guide rather than continuing.

## Step 6 — confirm everything actually updated

Open a **brand new** Claude Code window (closing and reopening an existing
one is not enough — it needs to be a fresh start) inside your solet's
folder, and run:

```bash
<name> health
<name> call service_interface::knowledge_service::search '{"query": "seed update runbook", "top_k": 3}'
```

Both should respond normally. If either one errors, or the second command
comes back empty, get in touch with whoever gave you this guide before
using your solet for anything important.

**If this update mentioned a newly-activated plugin** (something you were
told is now live on your solet that wasn't before), confirm its
knowledge is actually searchable, not just installed. Run two or three
searches for things that plugin should know about — for example, if it's a
marketing-data plugin, try queries like `"list campaigns"` or the plugin's
own name — and confirm the results include content from that plugin, not
just unrelated hits. A plugin can be correctly installed and still return
nothing until this is checked, and nothing else in this guide would catch
that.

**If you use Marketo or Zuora specifically**, the retrieval check above
isn't enough by itself — it only proves the plugin's own documentation is
searchable, not that reading real records through it behaves correctly.
These two are only verifiable on YOUR machine, not before this update ships
to you, so this is the one place these checks actually happen. Ask your
coding agent to run three reads through the connector and confirm:

1. **A normal, everyday read** (whatever you'd do day to day, without
   asking for anything unusual) — the results should land in a file on
   your computer, and the response you see should NOT contain the actual
   record values, just a description of where they were saved.
2. **A read where you explicitly ask for more than the default amount** —
   this should work, and should visibly fetch more than the normal read
   did.
3. **A read where you ask for an unreasonably large amount** (more than
   the connector allows even with the override) — this should be refused
   outright, with a message naming the limit, not silently trimmed down to
   something smaller.

If any of these three doesn't behave as described, stop and get in touch
with whoever gave you this guide rather than continuing to use that
connector.

## If something goes wrong

- **Step 1 refused to pull:** don't force it. Ask for help.
- **Right after Step 3's restart, `<name> health` briefly errors with
  something mentioning "no active color":** this is a known transient
  state — wait about 10 more seconds and try again before assuming
  something broke.
- **Step 5's commands errored:** don't skip ahead. Ask for help — running
  Step 6 afterward won't tell you anything useful if Step 5 didn't
  actually succeed.
- **Everything ran without errors, but something still seems off:** re-run
  Step 5 exactly as written. It's always safe to repeat.

## Questions

If anything here doesn't match what you're seeing on your screen, stop and
reach out rather than guessing — a quick question now is much easier to
answer than untangling a problem later.

## If you (or your agent) author your own joseki cards

Not an update step — a note for later, since it follows directly from what
this update changes. Business-connector reads no longer return record
values directly; a card that used to expect a full record back needs a
different shape now. Two patterns cover most cases, and the choice isn't
arbitrary:

- **IDs and distinguishing fields** fit a card whose job is picking ONE
  thing out of several candidates before a follow-up action — listing
  campaigns before triggering one, searching issues before transitioning
  one. The card mostly needs enough to tell candidates apart, not full
  records.
- **Reading the whole result file** fits a card whose job is processing an
  entire result set — an audit pass, a bulk export, anything that was
  always going to consume everything returned.

If a card's real use doesn't cleanly match either — sometimes picking one
thing, sometimes wanting the whole set — narrow the request itself (ask for
specific fields, or a bounded range) rather than forcing one shape as a
blanket rule.

## What changed in this release — you can now run a small fleet of sessions from this solet (2026-08-10 update)

If you already start extra agent sessions from this solet (or want to
start), this update is the one that makes that practical rather than
manual. Two things matter for you specifically:

**Don't skip Step 5 above this time.** A small companion tool (the
`coordination-hooks` plugin) got a real update in this release, including
a fix to a background helper that used to be able to wait forever without
telling anyone. Step 5's refresh commands are what actually pick that up —
if you skip it thinking "it probably updated with everything else," it
did not; that's exactly the trap Step 5 exists to catch.

**A new "operating manual" now ships with your solet** for anyone
running more than one session of it at once — how to hand off work between
sessions, pause one, bring back a stuck one, and check whether one is
running low on its own working memory. If you (or a coding agent working
on your behalf) manage multiple sessions, ask your agent to search your
solet's knowledge base for "maintenance verbs joseki cards" — that's
the manual. If you only ever talk to one session at a time, none of this
changes anything for you.

**One more thing, only if you use a connected Postgres or Snowflake
database:** both can now write to your database, but only if the login
you registered is itself allowed to — your solet does not add or
remove any permission on its own; your database's own access rules
decide. If you never want a particular connection to be able to write,
register it under a read-only login, same as you would for any other
tool. Snowflake's write path is brand-new this release and hasn't been
tested against a live account yet — if you turn it on, you're among the
first to actually use it live.

## What changed in this release — mostly behind-the-scenes (2026-08-08 update)

This update is almost entirely internal — you can follow Steps 1–6 above
exactly as written, with nothing extra. There's no new dependency to
install, and nothing new to configure.

The one thing worth knowing: if you (or a coding agent working on your
behalf) ever start OTHER agent sessions from this solet — not just the
one you're talking to — this release gives those sessions more ways to
coordinate with each other (starting, watching, and handing off work
between them). If that's not something you do, you can ignore this
entirely; it doesn't change how your solet behaves for ordinary use.

**One thing to know about, not something this update fixes:** a plugin
your solet ships and enables by default includes a few small reminder
hooks that depend on a program called `node` being present on your
machine. If it isn't, those specific reminders simply do not run. Most of
the time you'll notice — you'll see an on-screen error naming the missing
program, both at the start of a session and when you submit a prompt, and
everything keeps working normally either way. One of the reminders (an
idle-wake helper that's meant to run quietly in the background) is the
exception: if it fails to start, there's no visible sign of it at all.
Your git-safety protection is a separate hook, built differently, and
keeps working either way. This isn't fixed yet.

See the root `RELEASE_NOTES.md` file in your solet's folder for the
full list of what changed, in more detail than this guide covers.

## Reference

- `05_seed_update_runbook.md` — the same update procedure written to a
  coding agent, with the full technical detail (why each stale-copy check
  works, the measured plugin-cache behavior, and the verb-level detail
  behind Step 4's hydration re-render) that this guide leaves out on
  purpose.
- `01_hydration_runbook.md` — the first-time setup steps Step 4 above
  re-runs selectively.
- `RELEASE_NOTES.md` at the repo root — the full changelog for every
  release, including the ones summarized above.
