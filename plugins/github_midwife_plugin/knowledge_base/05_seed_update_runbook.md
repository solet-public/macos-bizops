# Seed Update Runbook — Updating a Live Solet From a Re-Minted Seed

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for applying a newer seed release to an ALREADY-LIVE seed-born solet without losing its state — why a fast-forward git pull from the same seed repo is the default update path and teardown-plus-re-birth is only the fallback, the exact sequence (health probe, pull --ff-only, restart preferring apply_manifest's zero-downtime blue-green swap over a bare LaunchAgent restart when a router is present, startup quiescence wait, automatic knowledge-base re-ingest for changed files), when the virtual environment needs attention (editable installs make pulled code live at restart; only NEW plugins or changed dependencies need a pip step), configuring the business-connector export/workspace root on an already-hydrated install when a release adds or extends connector containment (the one-time gap an existing clone never closes on its own), re-running the changed hydration steps afterward — including adding a release-added plugin to the clone's profile manifest and running its hydration guidance so it actually activates — the four stale copies a restart alone never refreshes (the installed Claude Code plugin's version-keyed CACHE copy, an already-open MCP bridge subprocess, an armed watcher, and knowledge-base chunks indexed from files the release removed — a deletion-only KB change is invisible to the startup staleness check and needs an explicit knowledge-service re-install with a negative-search verification) with the verifiable diff-based refresh check for the plugin cache, the one-time `git remote set-url` re-point when the seed repository moves to a new home (taken after the pull, verified with a fetch, rolled back to the old URL if the new one is unreachable), the solet rename migration's post-apply sequence (backfilling a pre-June LaunchAgent plist's missing StandardOutPath/StandardErrorPath log redirection as a stopgap versus re-running the autostart install verb as the durable fix, renaming the HOMUNCULUS_* environment keys inside `~/.claude.json`'s MCP server entries and why that step refuses outright while any Claude Code process is still running, and running `--scan-stale`'s report-only sweep for surviving old-name references split into historical/leave-alone, live-process-state/relaunch-to-fix, and fixable/safe-to-edit-directly categories with the memory-fact filename-plus-frontmatter-plus-backlink triple that a blind rename would break), and the verification checklist including the watcher role-claim ground truth.

## When to use this runbook

Use this when a solet is ALREADY alive and healthy on this machine and a
newer seed release has been published. Do not use it for first-time setup
(that is the hydration runbook) or for a broken instance that will not boot
(that is teardown plus re-birth).

## The one decision: pull-update or re-birth

**Pull-update is the default.** Seed releases are append-only: a re-mint adds
a commit to the SAME seed repository the clone was born from, fast-forward
only, never rewriting history. The clone's `origin` remote already points
there, so `git pull` brings the new code while everything the solet has
become — its database, memories, knowledge, credentials, LaunchAgents —
stays untouched.

**Re-birth is the fallback, not the routine.** Choose teardown plus re-birth
only when the clone's history has diverged from the seed repo (the pull below
refuses), the release notes explicitly require a fresh birth, or the operator
wants a clean-slate instance. Re-birth resets accumulated state; say that
plainly to the operator before choosing it.

## Step 1 — probe before touching anything

- `<name> health` answers (the instance is alive now; if it is not, this is
  repair or re-birth territory, not an update).
- `git -C <clone> remote get-url origin` points at the seed repository.
- `git -C <clone> status --short` — hydration-generated files (`AGENTS.md`,
  `CLAUDE.md`, `client/`, workbench notes) showing as untracked or modified is NORMAL and
  harmless; the seed never ships those paths, so they cannot conflict. Only a
  conflict during the pull itself is a stop condition.

## Step 2 — pull, fast-forward only

```bash
git -C <clone> pull --ff-only
```

If this refuses ("not possible to fast-forward"), STOP. The clone's history
and the seed repo have diverged — that is not a normal update state. Present
the facts to the operator; the usual resolution is re-birth from the current
seed. Never force, never rebase, never merge — a factory re-mint always
fast-forwards.

## Step 2a — re-point `origin` to the seed's new home (ONE TIME, this update only)

The seed has moved. The repository this clone was born from published its
FINAL update as the release you just pulled; every future re-mint is published
at the new home only. Nothing about your clone's history changes — the new
repository was seeded with the old repository's history before this release
published there, so the two share a common ancestor and your next pull is an
ordinary fast-forward, not a re-birth and not a divergence.

Do this AFTER Step 2's pull, not before. Pulling from the old URL still works
for this release (it published to both), so the safe order is: take the update
from the remote you can already reach, then move the remote.

```bash
git -C <clone> remote get-url origin                        # note the old URL first
git -C <clone> remote set-url origin <new-repository-URL>   # from this release's RELEASE_NOTES.md
git -C <clone> remote get-url origin                        # confirm it took
git -C <clone> fetch origin                                 # proves the new home is reachable
```

The exact new URL is in this release's `RELEASE_NOTES.md`; take it from there
rather than from memory, and do not guess at it.

**If `git fetch origin` fails with an access or not-found error, put the old
URL back** (`git remote set-url origin <old-URL>`) and tell the operator before
going further. Access to the new home is granted per-deployment, and a clone
pointed at a repository it cannot read has no update path at all — that is a
worse state than the one you started in. A failed fetch here is an access
question for the operator, not something to work around by re-pointing
somewhere else.

Verify at the end of the update: `git -C <clone> remote -v` shows the new URL
for both fetch and push, and `git -C <clone> status -sb` shows the branch
tracking a branch on it.

## Step 3 — virtual environment: usually nothing

The clone's `.venv` was built with EDITABLE installs (`pip install -e` per
package), so pulled code changes are live the moment the solet restarts —
no reinstall for ordinary updates, including new CLI subcommands.

The exception is structural: the release added a NEW plugin, or a plugin's
dependencies changed. The release notes (the re-mint commit message) say so
when it matters. Then, for each new plugin:

```bash
<clone>/.venv/bin/python -m pip install --no-build-isolation -e <clone>/plugins/<new_plugin>
```

If in doubt, this install form is idempotent — re-running it on an
already-installed plugin is harmless.

## Step 3a — the solet rename migration (MANDATORY when the update crosses 2026-08-13)

Releases minted from 2026-08-13 onward name every live identifier `solet`:
the CLI console script, the `SOLET_*` environment family, the `solet_name:`
root-manifest key, launchd labels `local.solet.<name>`, and the `--solet`
flag on the router/ingress entry points. There are NO compatibility aliases.
An installation born before that boundary still carries the old names in its
LaunchAgent plists, launcher shell functions, venv console script, and root
manifest — state a code pull does not rewrite. Restarting across the boundary
without migrating fails loud by design: the platform refuses to boot when it
finds `HOMUNCULUS_NAME` in its environment or the old manifest key, and the
error names this step.

Run the migration BEFORE Step 4's restart. It is self-executing — dry run
first, then apply:

```bash
<clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py
<clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py --apply
```

The script boots out the affected LaunchAgents (with a bounded wait for the
old label to actually clear `launchctl print` before bootstrapping — never
an unbounded spin, fails loud if it doesn't clear in time), rewrites their
labels, filenames, environment keys, and arguments, flips the launcher shell
functions, reinstalls the messaging plugin so `<clone>/.venv/bin/solet`
replaces the old console script (the old shim is removed, not aliased), and
bootstraps the agents back. Idempotent: re-running reports already-migrated
pieces and changes nothing — re-running `--apply` against an already-migrated
surface produces byte-identical files, never a spurious rewrite. Updates that
do not cross the boundary find nothing to do — the dry run prints an empty
plan and you move on.

After the platform is back (Step 4), run the residual guard: enumerate the
plugin-config store and fail loud on any `homunculus_name` key it still
carries (`solet call` a config listing if the release provides one, or ask
the solet directly to enumerate its plugin-config keys). A hit means a
plugin carried deployment-local config this script does not know about —
stop and repair before continuing, do not rename it ad hoc.

**Post-apply, in the order you'll hit them:**

1. **Plist log redirection is a durable-path issue, not a one-shot patch.**
   `migrate_to_solet.py` backfills `StandardOutPath`/`StandardErrorPath` on a
   solet-owned plist that predates them (pre-June installs), pointed at the
   same `<log_dir>/<name>_autostart.log` `autostart_manager` itself derives —
   but a field patch is a stopgap. The DURABLE path is re-running the
   autostart install verb: `_classify_install_prior` re-renders a
   `present_but_stale` plist wholesale, which a field patch never will. Prefer
   that over trusting the migration script's patch to have caught everything.

2. **`~/.claude.json`'s MCP server env keys need Claude Code closed.** The
   migration script renames `HOMUNCULUS_*` → `SOLET_*` inside every
   `mcpServers.*.env` block, but refuses outright while any Claude Code
   process is running — detected by process, not by a lockfile, and fails
   safe (refuses on an ambiguous detection too, never a false "not running").
   **Quit every Claude Code window/session on this machine before this
   surface applies**, or its renames silently no-op this run; Claude Code
   holds the file in memory and rewrites it on exit regardless of what the
   script wrote moments earlier.

3. **Run `--scan-stale` as the last manual-sweep step.**
   ```bash
   <clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py --scan-stale
   ```
   Report-only, case-insensitive, over `~/.claude/`, this clone's own
   `.claude/`, and `CLAUDE*.md`. Three categories, because the fix differs by
   category — never auto-rewritten, in any of them:
   - **historical** (a transcript, log, or other append-only record —
     `projects/*/*.jsonl`, `file-history/`, `history.jsonl`, `jobs/`,
     `paste-cache/`, `backups/`, `*.pre-*`, and siblings): leave alone,
     always — rewriting one falsifies the record.
   - **live process state** (`~/.claude/sessions/<pid>.json`): not a
     transcript, but still never hand-edited — a hit here is fixed by
     relaunching that session, not by editing its state file.
   - **fixable** (everything else): a plain edit is safe, EXCEPT a memory
     fact — any `.md` under a `memory/` directory — where the filename, its
     frontmatter `name:` slug, and every `[[link]]` pointing at it must move
     together. A blind substitution breaks that triple and leaves dangling
     links; fix those by hand, one fact at a time, never with a bulk
     find-and-replace.

## Step 4 — restart and WAIT

**Prefer `apply_manifest` over a bare restart whenever this profile has a router.** Check
whether `macos_self_deployment_plugin` is in the clone's own plugin roster
(`<name> call service_interface::lifecycle_management_service::list_plugins`). If it is,
`apply_manifest` swaps in the pulled code through the per-solet blue-green router —
verifying before cutover, leaving `previous` as an intact rollback target, zero downtime for
this no-MCP CLI path. Search the knowledge base for "picking up new code without a bare
restart apply_manifest" via Step Zero for the exact call shape (`new_manifest` + `reason`). A
bare LaunchAgent restart is a full stop that drops in-memory state (blob storage by default);
it is not wrong, just the higher-cost path when a router is available.

If that plugin is absent, this solet has no router and no blue-green path
(single-color by design) — restart the LaunchAgent directly:

```bash
launchctl unload ~/Library/LaunchAgents/local.solet.<name>.plist
launchctl load ~/Library/LaunchAgents/local.solet.<name>.plist
```

The blue-green router (if this profile has one) is a separate KeepAlive
LaunchAgent and is deliberately left alone regardless of which restart path you took. Then
wait for startup to finish before ANY query: `wc -l <newest log> && sleep 5 && wc -l <newest
log>` until the two counts match. Startup is also when changed knowledge bases re-ingest
automatically (content-hash comparison) — no manual re-ingest step exists or is needed.

## Step 4a — configure the export/workspace root (required if this release adds business-connector containment)

**Do this before any business-connector use, on ANY already-hydrated install.** A release
that adds or extends business-connector containment (export-root validation, per the
architect ruling on business-connector data boundaries, filed in this checkout's
`workbench/` directory under that date; §3) flips every covered connector's default from
"works" to "refuses until a workspace root is configured" the moment this update lands —
an already-hydrated clone never goes through the hydration runbook's export-root prompt
again on its own, so this step is the only thing that closes that gap for an EXISTING
install. Skipping it does not mean a smaller safety margin; it means every covered
business-connector read fails loud on first use post-update, for an operator who was never
told why. The release notes say when a given update actually touches this (not every
release does); when they do, treat this step as a prerequisite, not a recommendation.

If a workspace root was already configured (an earlier hydration or a previous run of this
step), this step is a no-op check, not a re-ask — skip straight to verification below.
Otherwise, ask the operator in plain words: "Where do you keep the folders you work in day
to day — the parent directory, not any one project?" (a `~/Workspace`-style directory:
stable, singular, covers every future job folder). Then call the real validator rather than
hand-rolling the check — it rejects a root that is, contains, or is contained by `app_home`
(naming which direction failed), and otherwise persists the root into every business-connector
plugin actually installed in this clone, additively and idempotently:

```bash
<clone>/.venv/bin/python3 -c "
from pathlib import Path
from github_midwife_plugin.export_root_validation import configure_export_root
written = configure_export_root(Path('<clone>'), '<clone>/profile', '<operator's answer>')
print(written)
"
```

Verify: the printed dict names every connector plugin that got the new root, and is never
empty on a clone that ships at least one business-connector plugin — an empty result means
none of the connector plugin directories were found under `<clone>/plugins/`, which is worth
a second look, not a silent pass.

## Step 5 — re-run the hydration steps the release changed

Updates that only change platform code end here. Updates that change the
OPERATOR-SIDE artifacts — the generated `AGENTS.md` / `CLAUDE.md` blocks, the
user-scope `~/.claude/settings.json` hooks, the rename skill, the fleet functions — need
the matching hydration steps re-run once. The hydration runbook's steps are
idempotent by design: probes first, marker-based structural merges, never
clobber. Re-run its Step 2 (and Step 4a if the operator uses fleet roles);
the markers replace the old solet-owned pieces in place and leave
everything else alone.

A release that ADDS a plugin needs one more route. Step 3's editable install
puts the new code in the venv, but the pull never touches the clone's
genesis-written `profile/config/manifest.yaml`, so the plugin stays
installed-but-inert until it is listed there. Add it to that manifest's
`plugins:` list, restart again (Step 4), then run the hydration runbook's
Step 4c for it — the `hydration_guidance.md` glob picks up the new plugin's
activation work and first-use credential contract.

## Step 6 — the four stale copies a restart alone does not refresh

A restart makes the platform run the new code. It does not make every
already-running client execute it. Four separate copies sit downstream of
"pulled and restarted," and each needs its own refresh.

**1. The installed Claude Code plugin runs from a CACHE COPY, not the source
tree.** Per vendor docs (`code.claude.com/docs/en/plugin-marketplaces`):
*"when users install a plugin, Claude Code copies the plugin directory to a
cache location."* `${CLAUDE_PLUGIN_ROOT}` resolves to that cache copy:

```
~/.claude/plugins/installed_plugins.json     # installPath, version, installedAt, lastUpdated
~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/   # the copied bytes
~/.claude/plugins/marketplaces/<marketplace>/               # the catalogue clone
```

A fix can land, deploy, assemble, seal, publish, and be pulled onto the
target machine — and that machine can still run the OLD hook. Every
upstream artifact reports success; the failure is silent and invisible from
our side, because nothing local can observe which copy a remote machine
executes.

Verify by diffing the executing bytes against the pulled deployment source —
never trust that a refresh happened:

```bash
python3 -c 'import json,sys; d=json.load(open(sys.argv[1]));
print([e["installPath"] for k,v in d["plugins"].items() if k.startswith("coordination-hooks@") for e in v])' \
  ~/.claude/plugins/installed_plugins.json

diff -r -x '__pycache__' -x '*.pyc' \
  "<installPath-from-above>/hooks" \
  "<clone>/plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks"
```

Empty output = the cache copy's hooks match the shipped hooks. Any output =
the fix is dormant regardless of what the pulled seed says. Scope the diff
to `hooks/`, not the whole plugin directory: Claude Code injects its own
`.claude-plugin` / `.in_use` markers at the plugin root and omits some
source-only files (e.g. `LICENSE`), so a whole-directory diff fires even on
a correctly-refreshed install.

✅ **CONFIRMED, direct measurement (2026-08-02, isolated scratch clone, global
state restored byte-identical afterward):** the cache path is version-keyed
(`cache/<plugin>/<version>/`), and neither `claude plugin install` on an
already-installed plugin nor `claude plugin update` re-copies changed hook
content when `plugin.json`'s `version` is unchanged — `install` reports
"already installed," `update` reports "already at the latest version," and
in both cases the cache bytes are untouched. Only `claude plugin uninstall`
followed by a fresh `claude plugin install` forces a real re-copy regardless
of version. **`coordination-hooks/.claude-plugin/plugin.json`'s `version`
has never moved past `0.1.0`** (confirmed at source): every shipped hook
change to date has been dormant on any machine with a prior install, by
construction, until that machine's operator runs an explicit
uninstall-then-reinstall. **Named fix: bump `plugin.json`'s `version` on
every shipped hook change**, as a step in the *release* procedure — a
refresh the operator cannot trigger from their own side is not their step
to own. Recovery today, without a version bump: `claude plugin uninstall
coordination-hooks@<marketplace-name> --scope local && claude plugin
install coordination-hooks@<marketplace-name> --scope local` — the diff
check above is what tells you whether this is actually needed.

⚠ **Declarative `extraKnownMarketplaces` + `enabledPlugins` alone — the
mechanism `01_hydration_runbook.md` currently relies on, with no explicit
`plugin install` step — behaves THREE different ways depending on exact
invocation shape, all three directly measured, none of them the CLI-install
path Reviewer-B originally confirmed works.** Named precisely, because
collapsing these into one "declarative doesn't work" claim would itself be
false precision:

| Invocation (exact flags measured) | Outcome |
|---|---|
| Headless (`-p`), `--setting-sources local`/`project` reading `.claude/settings.local.json`/`settings.json` from the cwd (Reviewer-B's method) | Marketplace never registers — `installPluginsForHeadless: no marketplaces declared`. `enabledPlugins` entry left "orphaned." Nothing installs, nothing loads. |
| Real TTY, interactive, trust dialog accepted, `--setting-sources local` (this session's probe) | `extraKnownMarketplaces` self-registers the marketplace. `enabledPlugins` produces a **broken** `installed_plugins.json` entry claiming a cache `installPath` that is never actually created on disk — confirmed after both an abrupt kill and a graceful `/exit`, ruling out a race. Hooks at a nonexistent path cannot execute; failure is silent. |
| Headless (`-p`), `--settings <externally-supplied file>` instead of `--setting-sources` alone (Reviewer-D's original method, independently reproduced this session against the current, fixed template) | Marketplace registers via a distinct `installPluginsForHeadless` reconcile pass (confirmed in `--debug` log: *"Added marketplace source"*, *"Read hooks.json for plugin coordination-hooks... from"* the **source** path, not a cache path). Plugin loads and hooks execute for that session — but ephemerally: no cache directory is created, and no persistent `installed_plugins.json` entry survives. This is Reviewer-D's "hooks execute directly from source, no cache" finding, now independently confirmed rather than just cited — and it explains her result cleanly: her method never goes through the cache-copy code path at all, so there was nothing for the cache to go stale. |

**None of these three is a clean stand-in for what a real operator's first
session actually does** — every measurement here (including Reviewer-D's
and Reviewer-B's) deliberately scoped `--setting-sources` or substituted
`--settings` specifically to avoid touching this machine's real global
`~/.claude/settings.json`, which is necessary test hygiene but means true
default, all-scopes-merged, no-special-flags resolution — the actual
production path when the operator's rendered `~/.claude/settings.json` is
just sitting there and they launch `claude` normally — **remains untested,
labeled OPEN, not resolved.** Testing it directly is blocked on the same
constraint already on record: overriding `$HOME` to sandbox a user-scope
test breaks Claude Code login on this machine
(`reference_a_scoped_home_override_breaks_claude_code_login`).

Only the explicit `claude plugin marketplace add <clone> --scope local` +
`claude plugin install coordination-hooks@<marketplace-name> --scope local`
CLI sequence (Reviewer-B's originally measured path) reliably produces a
complete, persistent, working cache copy in every configuration tested.
**This is a defect in the runbook's current install mechanism, not just an
update-refresh gap — flagged for whoever owns `01_hydration_runbook.md`'s
install step next; not fixed here, out of this article's scope.** The
version-bump fix above is method-independent of all of this and does not
wait on it: it only matters once a working install exists to go stale in
the first place.

**2. The MCP bridge subprocess a client already has open.** A blue-green
swap reconnects the bridge to the new colour; it does not respawn the
client-side subprocess that spawned it. A client already running keeps its
OLD subprocess until the client itself relaunches.

**3. An armed watcher (`<name> watch`).** Not re-armed by a swap — relaunch
the session that ran it. Verify against the live session id (the watcher
spool path matches it), never against the fact that a watch command was run
at some point.

**4. Knowledge-base chunks indexed from files the release REMOVED.** The
startup auto-install pass re-indexes a knowledge base when a surviving
file's mtime moves past the install record's `indexed_at` — it walks the
files that exist NOW, so a release that only deletes content from a KB is
invisible to it: the pull removes the files, the restart finds nothing
modified, and the old chunks keep answering searches indefinitely. The
`update` verb has the same blind spot — it collects changed files from
disk, and a vanished file is not on disk to collect. The deterministic
re-ingestion step, for every knowledge base the release notes name as
having content removed, is a re-install:

```bash
solet call service_interface::knowledge_service::install '{"name": "<kb-name>"}'
```

Re-install is the documented idempotent path: it drops the KB's entire
chunk set by the KB's own tag (embeddings included) and re-indexes from
the files now on disk, so a removed article cannot survive it. Verify
with a negative search afterward — a query that used to retrieve the
removed content must no longer surface it. "The restart re-indexed" is
not evidence for a deletion-only change; nothing in the restart path can
see one.

Until this wave runs — plugin cache refresh, client relaunch, watcher
re-arm, KB re-install where the release removed content — no observation
from an old session or an old plugin cache measures the new code. A green
result taken before the wave completes is scope-class false: it is
evidence about the copy that no longer matters.

## Step 7 — verify

- `<name> health` answers and a KB search returns content from the new
  release (search for a phrase the release notes mention).

Known post-restart state on blue-green-router profiles: `<name> health`
returns HTTP 503 `no_active_color` through the router while the platform
itself is healthy (a direct `curl` of the platform's own ephemeral bridge
port answers `200`). That is the router activation race: the new instance
registered before the router's 30s heartbeat GC expired the outgoing one,
so the one-shot cold-start auto-activate declined, and the GC then cleared
the active binding. On releases carrying the steady-state re-assert fix
(2026-07-23 and later) this heals itself within ~10 seconds — just re-probe.
On earlier releases, bounce `local.solet.<name>.plist` once more (the
second boot sees no active binding and self-activates); leave the router
LaunchAgent alone either way.
- A labeled session's rename skill arms `<name> watch`; its first line is
  `"watch": "armed"`. Ground truth for the role claim:
  `<name> call plugin::agent_messaging_plugin::peer_holds_role` with the role
  name AND the `agent_instance_id` from the armed line — never a raw
  peer-list entry.
- `git -C <clone> log --oneline -1` shows the release commit the operator
  expected.
- When this update re-pointed the remote (Step 2a): `git -C <clone> remote -v`
  shows the new URL for fetch and push, and a plain `git -C <clone> fetch
  origin` succeeds. Do this before declaring the update finished — a re-point
  that silently failed looks exactly like a healthy clone until the next
  release never arrives.
- Any feedback items you have open upstream: check this release's
  `RELEASE_NOTES.md` against them. Under the current feedback model an item is
  answered when its issue closes, and the release notes cite the issue numbers
  closed in that release — so the notes plus your own open-item list are enough
  to tell answered from still-open without asking anyone.
- When the release removed KB content: a search for a distinctive phrase
  from the removed material returns nothing from the affected KB. Run this
  AFTER Step 6's re-install — before it, a hit is the expected stale-copy
  signal, not evidence the update failed.

## What changed in this release — the seed's new home and a new feedback channel (2026-08-13 final update at the old repository)

Two adopter-facing changes ride along with this release, neither of which
touches platform code.

**1. The seed moved.** This is the last release published at the repository
this clone was born from. **Step 2a above is the action this requires** — one
`git remote set-url`, taken after the pull, with the new URL read from this
release's `RELEASE_NOTES.md`. Skipping it costs you nothing today and every
future release after that: a clone still pointed at the old URL will keep
reporting "already up to date" forever, which is indistinguishable from "no new
releases" and is the failure mode this step exists to prevent. If access to the
new home is not working, that is an operator conversation, not a workaround.

**2. Feedback is filed as GitHub issues now, not as a pull request carrying a
numbered document.** The item vocabulary you already use is unchanged — rounds
are still `Part N`, items are still `§N.M`, the four item classes are the same,
and the evidence and content rules are the same. What changed is where an item
goes: each item is filed through the repository's issue form for its class,
a multi-item round gets a parent issue with the items attached as sub-issues,
and a design proposal — an RFC-shaped document — is a feature-request issue
carrying the design in its body. **The repository accepts no pull requests and
no patches** (see `CONTRIBUTING.md`); Apache-2.0 already lets you fork and
change your own clone, so nothing about that policy limits what you can build.
Answers come back as issue closures and GitHub releases; subscribe to the new
repository's releases rather than polling it.
The full procedure is in the upstream feedback runbook, which was rewritten in
this release; read it before your next round rather than working from memory of
the old convention.

This one is worth a verification, because the guidance it replaces is guidance
your solet may still be answering searches with. The feedback runbook was
MODIFIED rather than deleted, so its file mtime moves and the startup pass
re-indexes it normally — this is not the deletion-blind case Step 6's fourth
stale copy describes, and no `install` call is required. Confirm rather than
assume: after the restart, search your own knowledge base for the old
convention (a phrase like "seed feedback pull request convention numbered
parts"). You should get the CURRENT issues-only guidance back — every item,
design proposals included, filed through an issue form. If you get either of
the superseded conventions instead — pull-request-per-round, or the
2026-08-13 hybrid where designs alone travelled as a pull request — the
re-index did not take, and that is when Step 6's re-install applies:

```bash
solet call service_interface::knowledge_service::install '{"name": "github_midwife_plugin"}'
```

## What changed in this release — the solet rename identifier cutover (2026-08-13 update)

Every live identifier renamed from its homunculus-era form: the CLI is now
`solet` (`solet call …`, `solet health`), the environment family is
`SOLET_*` (`SOLET_NAME` foremost), the root manifest key is `solet_name:`,
launchd labels are `local.solet.<name>`, the lifecycle verbs are
`birth_solet` / `teardown_solet` / `provision_solet`, and result envelopes,
error codes, and process JSONs follow. No aliases ship. **Step 3a is
mandatory for this update** — run the migration before restarting, or the
platform will refuse to boot (by design, with an error pointing back here).
Two spellings are deliberately unchanged: the MCP wire route
`notifications/homunculus/peer_message` and the channel `source="homunculus"`
attribute are frozen protocol names (a rename there is a versioned protocol
change, explicitly deferred), and history — ledgers, memories, old
`homunculus_decommissioned:` tag rows, workbench notes — is never rewritten.
Knowledge-base articles were renamed in place; because a rename is a
delete-plus-add, the startup staleness check cannot see the deletions —
Step 6's knowledge-service re-install with a negative-search verification
applies to this update.

## What changed in this release — origin working-corpus removal from the shipped thinking KBs (2026-08-12 update)

The seed no longer ships the minting origin's own pre-product working
corpus inside `default_thinking_plugin`: composition designs, sketch
packets, dated working plans, WBS specifications, and one legacy
creative-domain plan template — material from the origin's earlier
creative work that was never part of the business-ops product surface.
Two shipped knowledge bases lose indexed articles and gain nothing:
`thinking_plans` (its `plans/` and `wbs/` articles) and `plan_templates`
(its single legacy template). The same release also stops shipping two
never-indexed artifact directories in the same plugin. The KB
registrations and the thinking system prompt still ship; an empty
article set is a store's normal newborn state (a sibling thinking KB
already ships registration-only).

This is exactly the deletion-only KB change Step 6's fourth stale copy
describes: pull plus restart leaves every previously-indexed article
still answering searches. After the restart, run the re-install pair,
then the negative check:

```bash
solet call service_interface::knowledge_service::install '{"name": "thinking_plans"}'
solet call service_interface::knowledge_service::install '{"name": "plan_templates"}'
```

Then search for a phrase only the removed corpus contained (any
composition-specific phrase your deployment used to retrieve). A hit
from either KB means the re-install has not yet run against the store
actually serving your searches.

## What changed in this release — multi-session self-management adoption, connector write reversal (2026-08-10 update)

This release makes the full fleet-lifecycle stack from this seed's multi-agent
session management (see the 2026-08-08 delta below) something an ALREADY-LIVE
solet can actually operate, not just receive as dormant capability.
Three things this update needs beyond the base Steps 1–7, if this
solet is going to spawn or manage other sessions of itself:

**1. Refresh the `coordination-hooks` Claude Code plugin and verify you
land on `0.5.0` — that is the version this update targets, not an
intermediate one.** The version story, in one line: `0.3.1` = the
bounded-wait fix bump; `0.4.0` = heartbeat + rotation-due vendored;
`0.4.1` = memory-passthrough + origin ladder vendored; `0.5.0` = the two
remaining spawn-injected worker hooks plus the fail-loud resolution
ladder that makes a *programmatically spawned* worker's hooks actually
resolve on a born clone. **"After updating, verify `0.5.0`" is the
single instruction.** In full: `0.2.0` (pre-release baseline) → `0.3.0`
(four reminder hooks ported from `node` to `python3`, closing an
undeclared-runtime defect — see this release's `RELEASE_NOTES.md`) →
`0.3.1` (bounded the `Stop`-bound idle-wake waiter's wait, default 2400s
via `AGENT_WAKE_MAX_WAIT_S`, fails loud on a bad override rather than
silently) → `0.4.0` (vendors the heartbeat and the rotation-due watcher
into the plugin) → `0.4.1` (vendors the memory-passthrough sync set and
the origin-resolution ladder into this plugin, `6f840d7d7`) →
**`0.5.0`, the version this update is actually about**: ships
`headless_tool_allowlist_gate.py` and `capture_session_mapping.py` — the
two hooks a **programmatically spawned** worker needs (`spawn_session`,
distinct from every hook discussed so far, which cover interactively-launched
fleet sessions only) — and, in the same landing (`248a1294a`, merged
`a83a4baa2`), a two-rung resolution ladder in both host adapters
(`headless_adapter.py`/`tmux_adapter.py`) that fixes a real defect: a
born clone ships no `.claude/hooks/` directory at all, so a spawned
worker's generated hook settings previously pointed at a nonexistent
path and every one of that worker's tool calls was silently blocked from
its first turn. The ladder now resolves the checkout copy first, falls
back to this plugin's own shipped copy on a clone with no checkout
`.claude/hooks/`, and **refuses the spawn outright, loudly**, if neither
resolves — see the hydration runbook's Step 4a-ii for the full mechanism
and a zero-risk verification recipe. Step 6 above ("the three stale
copies a restart alone does not refresh") is exactly what makes reaching
`0.5.0` real rather than theoretical: every one of `0.3.0` through
`0.5.0` is dormant on an existing install until its cache copy is
explicitly refreshed — an operator who pulls this update and only
restarts keeps running whatever pre-`0.5.0` copy their cache already
held, with no local signal anything is out of date. Run Step 6's diff
check against `0.5.0` specifically, not just "a newer version than
before," before trusting any of this update's self-management hooks —
interactive or spawned — are actually live.

**1a. Before first launch: export `SOLET_NAME` in the environment
Claude Code is launched from.** One export does double duty for this
release's self-management stack — set it once, in the same shell/terminal
you launch Claude Code from (the same convention this checkout's own
`CLAUDE.md` uses for the platform's own foreground launch: `SOLET_NAME=<name>
python -m ananta.cli --app-home <profile>`), and both of the following
resolve correctly without further setup:

- **Memory-passthrough origin resolution, rung 1.** The origin-resolution
  ladder that decides which solet a session's memory writes belong to
  checks `SOLET_NAME` first; without it, resolution falls through to
  rung 2 (`root_manifest.yaml`, only valid once genesis has rewritten it)
  or rung 3 (`CLAUDE_PROJECT_DIR`'s basename) — both work, but rung 1 is
  the reliable one and doesn't depend on either.
- **The heartbeat and rotation-due watch hooks' identity.** Both hooks
  (new in `0.4.0`/`0.4.1`) call into other platform verbs that also read
  `SOLET_NAME` for identity. (Separately: those two hooks additionally
  need `AGENT_INSTANCE_ID` exported to arm at all — without it they are
  silent no-ops, by design, not a bug; see `SECURITY.md`'s Configuration
  surface section, "Adopter setup note," for the exact arming variables
  and the full default-off/default-on table across all thirteen hooks.)

`SOLET_NAME` is a soft dependency for the origin ladder (rungs 2/3
still resolve it) but the only path to a reliable heartbeat/rotation-due
identity — export it before first launch rather than discovering the gap
later.

**2. This checkout's own project-scope `.claude/hooks/` stack is being
folded INTO the coordination-hooks plugin by `0.4.0`, not layered
alongside it.** Earlier in this release cycle, the heartbeat
(`heartbeat_report_alive.py`), the rotation-due watcher
(`rotation_due_watch.py`, now also feeding the context-gauge verb pair
below), and the memory-passthrough sync wrapper lived only as this
checkout's own operator-scope hooks, rendered by the hydration runbook
(`01_hydration_runbook.md`) rather than shipped in the plugin bundle. As
of `0.4.0` they vendor into the plugin itself — Step 1 above is what
delivers them to an existing solet, not a separate hydration re-run,
though Step 5 ("re-run the hydration steps the release changed") may
still matter if this release also changes anything on the operator-scope
side that `0.4.0` does NOT absorb; check the final hooks manifest below
before assuming Step 5 is a no-op here. **Verification that everything in
this hook stack actually ships in this seed's own bundle and manifest is
tracked separately, in this same overnight wave (`R4` seed-packaging
audit) — do not assume shipped from this document alone; confirm against
that lane's own report or by reading `capability_bundles.yaml` /
`seed_manifest.yaml` / the `assemble()` allowlist directly before relying
on it.**

**2a. After updating: verify your hooks actually fire.** Table below is
read directly from the **landed** `hooks.json`
(`plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json`,
master HEAD `a83a4baa2`) — every row verified against that file, not
taken from any summary. **`0.5.0` changed this file's own `description`
field only** (disclosing the two spawn-injected hooks below) — the
event/hook wiring table itself is byte-identical to `0.4.1`, verified by
diff (`git diff 6f840d7d7 248a1294a -- .../hooks/hooks.json`).

| Event | Hook(s) | Confirms |
|---|---|---|
| `UserPromptSubmit` | `step_zero_reminder.py`, `check_messages_reminder.py`, `session_context.py` (3 independent hook entries) | The KB-first reminder, the check-your-messages reminder, and per-prompt session context all fire on every prompt you submit. |
| `SessionStart` | matcher `startup\|resume\|clear`: `check_messages_reminder.py`, `role_binding_reminder.py`; unconditional (any matcher): `session_context.py` | The check-your-messages and role-binding reminders fire on a startup/resume/`/clear`; session context fires on every `SessionStart`, including other matcher values not listed above (none currently defined). |
| `Stop` | `wake_waiter.py` (`asyncRewake: true`, `timeout: 86400`) | The bounded idle-wake waiter, `0.3.1`+, is wired and its wait is capped rather than open-ended. |
| `PreToolUse` | matcher `^(Bash\|Edit\|Write\|MultiEdit\|NotebookEdit\|Task\|Agent)$`: `git_controller_gate.py` | The git-mutation safety gate fires before every tool call in that matcher set — present since before this release, unaffected by `0.4.0`/`0.4.1`. |
| `PostToolUse` | unconditional: `rotation_due_watch.py`, `heartbeat_report_alive.py` (2 independent hook entries, fire after every tool call); matcher `Write\|Edit\|MultiEdit`: `capture.py` | The rotation-due watcher and the heartbeat both fire on every tool call, new in `0.4.0`. `capture.py` (the memory-passthrough capture hook, new in `0.4.1`) fires only on file-mutating tool calls. |

**Four CLI utilities ship in the same `hooks/` directory but are
deliberately NOT hook-wired** — `drain.py`, `hydrate_render.py`,
`index_render.py`, `sync.py` are agent-invoked directly (see the
memory-passthrough hydrate/drain procedure in `CLAUDE.md`/`AGENTS.md`),
not triggered by any Claude Code event. Do not expect them in the table
above or treat their absence as a wiring gap.

**`headless_tool_allowlist_gate.py` and `capture_session_mapping.py`
(new in `0.5.0`) will never appear in the table above either — by
design, not omission.** Both ship in `hooks/` as this plugin's shipped
fallback copy, but neither is registered in `hooks.json` — they are
never fired by an interactively-launched fleet session at all. A
**programmatically spawned** worker (`spawn_session`) gets them, plus
six other hooks already in the table above, wired into its own
per-spawn generated `--settings` blob instead — a completely separate
delivery path from everything else in this table. See the hydration
runbook's Step 4a-ii for the full mechanism, the two-rung resolution
ladder that decides which copy a given spawn actually runs, and a
zero-risk verification recipe. Verifying THIS table (an installed
`hooks.json` diff) tells you nothing about whether a spawned worker's
hooks are wired correctly — that needs the spawn-time probe Step 4a-ii
describes, not a cache-copy diff.

A quick self-check any adopter can run against their own installed cache
copy, not just the source tree: `python3 -c 'import json; d=json.load(open("<installed cache
path>/hooks/hooks.json")); [print(e) for v in d["hooks"].values() for g in
v for e in g["hooks"]]'` against their own installed cache path (found via
Step 6's `installed_plugins.json` lookup above) shows every command their
own copy will actually run.

**3. The maintenance-verbs joseki cards ARE the operating manual for the
fleet verbs below — read them before improvising a sequence.** Search this
solet's own knowledge base for "maintenance verbs joseki cards"
(source: `ananta_platform`, `24_operator_communication/09_maintenance_verbs_joseki_cards.md`).
It carries the exact ordered-call sequence, verify step, and known traps
for worker rotation, worker restart, memory-passthrough sync, memory-head
curation, KB/process refresh, and context-gauge checks. Follow the card;
do not re-derive the sequence from the verb names alone — several of these
verbs have a non-obvious trap (see the quickstart below) that the card
exists specifically to prevent.

**Fleet-verbs quickstart**, all under `plugin::agent_messaging_plugin::`,
all documented in depth by the joseki cards above — this is an index, not
a substitute for reading them:

| Need | Verb(s) | One trap worth knowing before you call it |
|---|---|---|
| Spawn a new worker session | `spawn_session` | Already drives one automatic first turn — do not also hand-drive a first turn unless no lane charter is on file. |
| Rotate a worker in place (clear + redrive, same process) | `peer_send_by_name` (pickup pointer) → `clear_session` (`park=False`) → `drive_session` | Rotate uses the spawn-time LEDGER `agent_instance_id`, never the `agi-watch-*` id from a role-thread message — they are different values for the same worker. |
| Restart a dead/hung worker (kill + fresh process) | `session_status` → `terminate_session` → `spawn_session` | A hand relaunch that bypasses `spawn_session` (e.g. raw pane injection into a terminal) does NOT get the automatic first-turn drive for free and needs one explicitly. |
| Park a worker (clear and leave idle, deliberately) | `clear_session` with `park=True` | Its role binding and any armed watcher survive a park — a parked worker is not a terminated one. |
| Check a session's context-window occupancy | `session_context_status` | A `resolved: false` result is the expected shape for an operator-hosted seat (a disclosed gap, not a bug) — never estimate a number in its place. |
| Curate the ambient `MEMORY.md` head at a rotation boundary | `generate_curation_report` → (seat-ratified) `reinforce_by_slug` | Every demotion is a seat judgment call — there is no auto-trim, by design. |

Verify any of the above actually took effect the way the joseki card's own
"Verify — do not skip" step says to — a queued delivery receipt confirms a
message was accepted, never that a turn actually ran.

**4. Postgres AND Snowflake connections can now write, if the registered
credential's own grants allow it.** One new verb per connector —
`run_statement` on `external_postgres_plugin`, `run_statement` on
`snowflake_plugin` (landed `2d562767b`, ancestor-verified against master
by this worker) — opens a non-read-only connection; every existing read
verb on both connectors is unaffected and stays strictly read-only.
Neither plugin performs a write-permission check of its own — the
connected database's own RBAC/grants are the entire control plane
(operator ruling 2026-08-09 + Amendment 1). Full detail is in each connector's
own overview article — `01_external_postgres_overview.md` and
`01_snowflake_overview.md`, both under "Read/write posture" — in the
corresponding plugin's knowledge base, if this seed carries those connectors.
**Two things about Snowflake's write verb
are open, not yet answered by measurement:** `RETURNING`-equivalent
clause support is object-dependent and uncharacterized here (the
connector rolls back rather than silently discarding rows if one produces
output with no export path given); and this release ships with no live
write smoke against a real Snowflake account — every registered
connection here is pinned to a read-only role, so a write was never
exercised end-to-end, only against a fake client. Confirm against
`01_snowflake_overview.md` before telling an operator either caveat has
been resolved.

Full detail: this release's `RELEASE_NOTES.md` at the repo root.

## What changed in this release — multi-agent session management, fleet transport default, and ledger fixes (2026-08-08 update)

This release (source commit `71159c02e1a4ce373db6561a30a1b4b00d0b0b91` through
`837ad3e359ce2b42041594f12cde98a78468148c`) needs **no extra steps beyond
Steps 1–7 above.** No plugin was added or removed, no dependency changed,
and every new database table installs itself automatically the next time
the platform starts (the standard idempotent schema-install path every
table already goes through — nothing to run by hand, nothing to verify
beyond the normal Step 7 health check).

What it actually contains, in case it's relevant to you:

- **A fuller multi-agent session management surface** — spawning, listing,
  checking on, dispatching follow-up work into, and retiring other agent
  sessions from your own, plus a durable per-session lifecycle record and
  automatic sweeps for sessions that go quiet. This is new *capability*,
  not a change to how a single, non-spawning session behaves. If you don't
  spawn sessions from this solet, nothing here affects you.
- **`default_fleet_transport` config knob, defaulting to `watch`.** This
  only governs how a *newly spawned* session receives messages going
  forward — it does not change how an already-running session you started
  before this update behaves. No action needed unless you spawn sessions
  and want a different transport; see
  `ananta/knowledge_bases/ananta_platform/24_operator_communication/06_fleet_launcher_session_configuration.md`
  for the knob if you need it.
- **`root_manifest.yaml` gains a `sanctioned:` entry for `.claude-plugin`.**
  Purely informational — it tells your own root-strictness check that this
  directory is allowed to exist (written by local Claude Code plugin setup,
  never shipped as content), so a fresh clone's first cutover gate doesn't
  block on its absence. You get this automatically with the pull in Step 1;
  no separate step.
- **Session-ledger duplicate/concurrency fixes.** Internal correctness
  fixes for how the ledger resolves two rows describing the same external
  event. Nothing you need to do; if you have your own tooling reading that
  ledger directly, behavior around duplicate rows is now deterministic
  rather than read-order-dependent.

**One known issue, still open** — running the full gate suite on a
freshly-born clone will show two smoke entries fail: they require local
paths (`.claude`, `.agents`) that this platform's own root-strictness
contract says a born clone never has. This is a pre-existing gap in the
gate register, not something this update caused or something an update
step here can fix. Expect 247/249 on a healthy clone; those two entries
are the exception, not a broken install.

**A second known issue, still open** — the `coordination-hooks` plugin
ships in the bundle and is enabled by default via the hydration path.
Four of its five hooks (the reminder hooks) are invoked via `node`; this
platform documents Python 3.13 as its dependency and never requires,
installs, or mentions node. On a host without node on PATH, those four
hooks do not run. **You will see most of this happen:** the two
`UserPromptSubmit` reminder hooks and the two `SessionStart` reminder
hooks fail to launch with a visible, on-screen error naming the missing
command, and the session continues normally either way. **The exception
is the `Stop`-bound idle-wake waiter — it fails without any indication at
all**, since its normal job is to wait quietly in the background, so a
failed launch and correct operation look identical from outside.
(Measured: three repetitions per hook-event type against a validated
harness with a positive control, on Claude Code v2.1.226; covers
hook-launch failure specifically, not a hook that launches and then
errors internally, times out, or hangs.) The fifth hook — the PreToolUse
git-mutation safety gate — is implemented in `python3`, not `node`, and
is unaffected either way; do not assume a missing node also removes your
git guard. Not fixed by this release, and no fix is scheduled by it.

**Check for a newly-untracked `.gitignore` after updating.** A cloned
seed previously never received one (the birth-time write was skipped
whenever a `.git` directory was already present, which is always true for
a GitHub-cloned seed) — this is now fixed. On an existing clone the file
arrives untracked, since genesis must never touch your git history;
commit it yourself when convenient. If you already had your own, this
does not touch it.

Full detail: this release's `RELEASE_NOTES.md` at the repo root.

## What changed in this release — business-connector reads now export to file by default, with limits

Business-system reads no longer return record-level data directly into agent context.
Postgres, Snowflake, Salesforce, Marketo, and Zuora reads now always write results to
the caller-supplied path configured in Step 4a — inspection is a deliberate act, not an
automatic side effect of the call. Every read across all eight business connectors (the five
above plus G Suite, Jira, and Schwab where applicable) defaults to 500 records per fetch, with
an informed-override path for callers who genuinely need more; within that limit, a connector
pages internally across the vendor's own per-call ceiling and delivers one complete result —
paging is never exposed as a caller-visible token or continuation parameter.

**G Suite and Jira are deliberately NOT part of the export-by-default change** — the operator
scoped the data-export requirement to connectors handling mass record exposure, and ruled that neither
G Suite reads nor Jira's company-internal-account data carry that risk the same way. Both
still get the 500-record default and override; they simply keep returning results directly
rather than exporting to a file. This is a design choice stated in this release's own migration
record, not an inconsistency to work around.

If this update reaches an already-hydrated install, **Step 4a above is the action this
change requires** — an unconfigured workspace root means every affected connector read fails
loud on first use post-update. See
`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`, "What this
solet ingests and embeds," for what happens to results once they do reach a session.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/06_seed_update_operator_guide.md`
  — the same procedure written directly to the solet's owner, for
  running the update at a terminal without a coding agent driving every
  step. Points back here for Step 5's hydration re-render, which does need
  an agent.
- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` —
  the hydration steps this runbook re-runs selectively after an update.
- `plugins/github_midwife_plugin/knowledge_base/07_upstream_feedback_runbook.md`
  — how to report what this update got wrong, ask a question about it, or
  confirm a fix landed. Rewritten in this release to the issue-form model; it
  is also where the "subscribe to releases" instruction lives, which is how you
  learn a future update exists at all now that the seed has moved.
- `plugins/seed_factory_plugin/knowledge_base/02_seed_publish_runbook.md` —
  why re-mints are append-only fast-forward commits (the property Step 2
  relies on).
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py`
  — the editable-install provisioner (the property Step 3 relies on).
- `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json`
  — the `version` field Step 6's open question is about.
- `code.claude.com/docs/en/plugin-marketplaces` — vendor documentation for the
  installed-plugin cache-copy behavior Step 6 cites.
- The original declarative-install, hooks-execute-from-source measurement that
  Step 6's three-way invocation table reconciles is a dated note in the
  ORIGINATING checkout's `workbench/` directory, which is never shipped in a
  seed. Step 6's table states every outcome it established, so a deployment
  needs no copy of the note.
- `RELEASE_NOTES.md` at the repo root — the full changelog for every
  release, including the ones summarized above.
