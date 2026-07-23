# Seed Update Runbook — Updating a Live Homunculus From a Re-Minted Seed

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-runbook, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for applying a newer seed release to an ALREADY-LIVE seed-born homunculus without losing its state — why a fast-forward git pull from the same seed repo is the default update path and teardown-plus-re-birth is only the fallback, the exact sequence (health probe, pull --ff-only, restart, startup quiescence wait, automatic knowledge-base re-ingest), when the virtual environment needs attention (editable installs make pulled code live at restart; only NEW plugins or changed dependencies need a pip step), re-running the changed hydration steps afterward, and the verification checklist including the watcher role-claim ground truth.

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
- `git -C <clone> status --short` — hydration-generated files (`CLAUDE.md`,
  `client/`, workbench notes) showing as untracked or modified is NORMAL and
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

```bash
launchctl unload ~/Library/LaunchAgents/local.homunculus.<name>.plist
launchctl load ~/Library/LaunchAgents/local.homunculus.<name>.plist
```

The blue-green router (if this profile has one) is a separate KeepAlive
LaunchAgent and is deliberately left alone. Then wait for startup to finish
before ANY query: `wc -l <newest log> && sleep 5 && wc -l <newest log>` until
the two counts match. Startup is also when changed knowledge bases re-ingest
automatically (content-hash comparison) — no manual re-ingest step exists or
is needed.

## Step 5 — re-run the hydration steps the release changed

Updates that only change platform code end here. Updates that change the
OPERATOR-SIDE artifacts — the generated `CLAUDE.md` block, the user-scope
`~/.claude/settings.json` hooks, the rename skill, the fleet functions — need
the matching hydration steps re-run once. The hydration runbook's steps are
idempotent by design: probes first, marker-based structural merges, never
clobber. Re-run its Step 2 (and Step 4a if the operator uses fleet roles);
the markers replace the old homunculus-owned pieces in place and leave
everything else alone.

## Step 6 — verify

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

## Reference

- `plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md` —
  the hydration steps this runbook re-runs selectively after an update.
- `plugins/seed_factory_plugin/knowledge_base/02_seed_publish_runbook.md` —
  why re-mints are append-only fast-forward commits (the property Step 2
  relies on).
- `plugins/github_midwife_plugin/src/github_midwife_plugin/venv_provision.py`
  — the editable-install provisioner (the property Step 3 relies on).
