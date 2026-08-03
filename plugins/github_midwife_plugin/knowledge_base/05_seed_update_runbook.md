# Seed Update Runbook — Updating a Live Homunculus From a Re-Minted Seed

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for applying a newer seed release to an ALREADY-LIVE seed-born homunculus without losing its state — why a fast-forward git pull from the same seed repo is the default update path and teardown-plus-re-birth is only the fallback, the exact sequence (health probe, pull --ff-only, restart preferring apply_manifest's zero-downtime blue-green swap over a bare LaunchAgent restart when a router is present, startup quiescence wait, automatic knowledge-base re-ingest), when the virtual environment needs attention (editable installs make pulled code live at restart; only NEW plugins or changed dependencies need a pip step), configuring the business-connector export/workspace root on an already-hydrated install when a release adds or extends connector containment (the one-time gap an existing clone never closes on its own), re-running the changed hydration steps afterward — including adding a release-added plugin to the clone's profile manifest and running its hydration guidance so it actually activates — the three stale copies a restart alone never refreshes (the installed Claude Code plugin's version-keyed CACHE copy, an already-open MCP bridge subprocess, an armed watcher) with the verifiable diff-based refresh check for the plugin cache, and the verification checklist including the watcher role-claim ground truth.

## When to use this runbook

Use this when a homunculus is ALREADY alive and healthy on this machine and a
newer seed release has been published. Do not use it for first-time setup
(that is the hydration runbook) or for a broken instance that will not boot
(that is teardown plus re-birth).

## The one decision: pull-update or re-birth

**Pull-update is the default.** Seed releases are append-only: a re-mint adds
a commit to the SAME seed repository the clone was born from, fast-forward
only, never rewriting history. The clone's `origin` remote already points
there, so `git pull` brings the new code while everything the homunculus has
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

## Step 3 — virtual environment: usually nothing

The clone's `.venv` was built with EDITABLE installs (`pip install -e` per
package), so pulled code changes are live the moment the homunculus restarts —
no reinstall for ordinary updates, including new CLI subcommands.

The exception is structural: the release added a NEW plugin, or a plugin's
dependencies changed. The release notes (the re-mint commit message) say so
when it matters. Then, for each new plugin:

```bash
<clone>/.venv/bin/python -m pip install --no-build-isolation -e <clone>/plugins/<new_plugin>
```

If in doubt, this install form is idempotent — re-running it on an
already-installed plugin is harmless.

## Step 4 — restart and WAIT

**Prefer `apply_manifest` over a bare restart whenever this profile has a router.** Check
whether `macos_self_deployment_plugin` is in the clone's own plugin roster
(`<name> call service_interface::lifecycle_management_service::list_plugins`). If it is,
`apply_manifest` swaps in the pulled code through the per-homunculus blue-green router —
verifying before cutover, leaving `previous` as an intact rollback target, zero downtime for
this no-MCP CLI path. Search the knowledge base for "picking up new code without a bare
restart apply_manifest" via Step Zero for the exact call shape (`new_manifest` + `reason`). A
bare LaunchAgent restart is a full stop that drops in-memory state (blob storage by default);
it is not wrong, just the higher-cost path when a router is available.

If that plugin is absent, this homunculus has no router and no blue-green path
(single-color by design) — restart the LaunchAgent directly:

```bash
launchctl unload ~/Library/LaunchAgents/local.homunculus.<name>.plist
launchctl load ~/Library/LaunchAgents/local.homunculus.<name>.plist
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
the markers replace the old homunculus-owned pieces in place and leave
everything else alone.

A release that ADDS a plugin needs one more route. Step 3's editable install
puts the new code in the venv, but the pull never touches the clone's
genesis-written `profile/config/manifest.yaml`, so the plugin stays
installed-but-inert until it is listed there. Add it to that manifest's
`plugins:` list, restart again (Step 4), then run the hydration runbook's
Step 4c for it — the `hydration_guidance.md` glob picks up the new plugin's
activation work and first-use credential contract.

## Step 6 — the three stale copies a restart alone does not refresh

A restart makes the platform run the new code. It does not make every
already-running client execute it. Three separate copies sit downstream of
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

Until this wave runs — plugin cache refresh, client relaunch, watcher
re-arm — no observation from an old session or an old plugin cache measures
the new code. A green result taken before the wave completes is scope-class
false: it is evidence about the copy that no longer matters.

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
On earlier releases, bounce `local.homunculus.<name>.plist` once more (the
second boot sees no active binding and self-activates); leave the router
LaunchAgent alone either way.
- A labeled session's rename skill arms `<name> watch`; its first line is
  `"watch": "armed"`. Ground truth for the role claim:
  `<name> call plugin::agent_messaging_plugin::peer_holds_role` with the role
  name AND the `agent_instance_id` from the armed line — never a raw
  peer-list entry.
- `git -C <clone> log --oneline -1` shows the release commit the operator
  expected.

## What changed in this release — business-connector reads now spill by default, with limits

Business-system reads no longer return record-level data directly into agent context.
Postgres, Snowflake, Salesforce, Marketo, and Zuora reads now always write results to
the caller-supplied path configured in Step 4a — inspection is a deliberate act, not an
automatic side effect of the call. Every read across all eight business connectors (the five
above plus G Suite, Jira, and Schwab where applicable) defaults to 500 records per fetch, with
an informed-override path for callers who genuinely need more; within that limit, a connector
pages internally across the vendor's own per-call ceiling and delivers one complete result —
paging is never exposed as a caller-visible token or continuation parameter.

**G Suite and Jira are deliberately NOT part of the spill-by-default change** — the operator
scoped the spill floor to connectors handling mass record exposure, and ruled that neither
G Suite reads nor Jira's company-internal-account data carry that risk the same way. Both
still get the 500-record default and override; they simply keep returning results directly
rather than spilling to a file. This is a design choice stated in this release's own migration
record, not an inconsistency to work around.

If this update reaches an already-hydrated install, **Step 4a above is the action this
change requires** — an unconfigured workspace root means every affected connector read fails
loud on first use post-update. See
`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`, "What this
homunculus ingests and embeds," for what happens to results once they do reach a session.

## Reference

- `plugins/github_midwife_plugin/knowledge_base/06_seed_update_operator_guide.md`
  — the same procedure written directly to the homunculus's owner, for
  running the update at a terminal without a coding agent driving every
  step. Points back here for Step 5's hydration re-render, which does need
  an agent.
- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` —
  the hydration steps this runbook re-runs selectively after an update.
- `plugins/seed_factory_plugin/knowledge_base/02_seed_publish_runbook.md` —
  why re-mints are append-only fast-forward commits (the property Step 2
  relies on).
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py`
  — the editable-install provisioner (the property Step 3 relies on).
- `plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json`
  — the `version` field Step 6's open question is about.
- `code.claude.com/docs/en/plugin-marketplaces` — vendor documentation for the
  installed-plugin cache-copy behavior Step 6 cites.
- `workbench/2026-08-02_b2a_plugin_source_schema_stub_finding_reviewer_d.md`
  — the original declarative-install, hooks-execute-from-source measurement
  Step 6's three-way invocation table reconciles.
