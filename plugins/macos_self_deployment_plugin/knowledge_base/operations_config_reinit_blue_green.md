# Re-initializing a plugin after a config change on a blue-green homunculus

Article Layer: 2

Article Role: operations_reference

Article Tags: domain:self-deployment, domain:local-homunculus, evidence-category:operations-runbook, planning-stage:execution

Embedding Description: How to apply a per-plugin config change (e.g. a new sf_cli_path in profile/config/plugins/<name>.json) and rebuild the LIVE plugin on a blue-green-router homunculus such as dax. Why launchctl unload/load is NOT the sanctioned path (it bounces the single active color in place and risks the no_active_color router wedge); why reload_plugin_config and set_plugin_enabled do NOT rebuild a plugin that builds its client in prepare_for_readiness; the apply_manifest zero-downtime swap as the correct re-init primitive; and the verify-surface gap where local_self_deployment_service::swap_status is unbound in this profile.

## When this applies

You changed a per-plugin config value that is read at plugin readiness — the
canonical case is writing `sf_cli_path` into
`profile/config/plugins/salesforce_plugin.json`, or any key a plugin consumes
in its `prepare_for_readiness()` when it builds a cached client/executor.
Writing the file does NOT apply the change to the already-running plugin; the
config is read at boot (or on an explicit reload) and the plugin's client is
built once and cached for the process lifetime.

## The three in-session mechanisms — and why they do NOT rebuild the plugin here

1. **`lifecycle_management_service::reload_plugin_config`** re-reads
   `profile/config/plugins/<name>.json`, refreshes the ConfigManager cache, and
   calls `initialize(new_config)` — but ONLY `initialize`. A plugin that builds
   its client/executor in `prepare_for_readiness()` (the macOS connector
   pattern: `salesforce_plugin`, etc.) leaves `initialize` as the PluginBase
   no-op, so its cached executor is NOT rebuilt. reload refreshes config the
   plugin will not re-read until the next readiness.
2. **`lifecycle_management_service::set_plugin_enabled` (disable→enable)** is
   BROKEN for in-session rebuild. Disable drops the plugin from the roster
   cleanly; the subsequent enable fails with
   `"<plugin>: orchestrator_ref not injected"` because the in-session
   rediscovery path does not re-run the boot injection sequence
   (`set_orchestrator_ref` → `set_event_bus` → `prepare_for_readiness`) before
   readiness. The plugin is left `status: uninitialized`, not running. Do NOT
   use disable/enable to rebuild a plugin until this is fixed. (Config-file
   safety note: the Track D verbs read/write `<name>.json` from DISK and
   refresh the cache in lock-step, so a hand-written key like `sf_cli_path` is
   preserved across a disable/enable, not clobbered — but the rebuild still
   fails.)
3. **`lifecycle_management_service::apply_manifest`** v1 rejects
   `plugin_config_overrides` (`forbidden_v1_keys_present`) and is documented for
   plugin-set/code changes, not config re-inits — but a same-manifest commit is
   still the correct zero-downtime re-init primitive (below), because the fresh
   color reads the on-disk config at boot.

## The sanctioned re-init: apply_manifest (zero downtime)

On a blue-green profile — `macos_self_deployment_plugin` in the manifest and a
`local.homunculus.<name>.router` LaunchAgent running — the correct way to bring
a plugin up against new on-disk config with NO bridge downtime is the
apply_manifest blue-green swap (joseki `deploy_local_blue_green_release`):

1. `apply_manifest` with `dry_run: true` and the CURRENT plugin list → returns
   the current manifest etag (verified working: empty diff on an unchanged
   list).
2. `apply_manifest` committing with `expected_etag` = that etag. A fresh color
   is materialized (`python -m ananta.cli`), boots reading the updated
   `profile/config/plugins/<name>.json`, the router polls until it registers,
   activates it, and drains the old color.

The per-homunculus `profile/config` and `profile/data` are shared across
release colors, so the new color sees the config you just wrote.

## The blunt fallback (and its risk)

A raw `launchctl unload -w / load -w local.homunculus.<name>.plist` bounces the
SINGLE active color in place. It works and applies the config, but it is NOT the
blue-green swap: the router has no backend during the gap (tens of seconds), and
on a warm machine it can WEDGE at `no_active_color` (the cold-start
auto-activate is one-shot and races the router's 30 s heartbeat GC — see the
Part 15 seed-feedback analysis). Recovery from a wedge is to bounce the plist
ONCE more. Use the plain restart only when apply_manifest is genuinely
unavailable, and re-probe `<name> health` through the router afterward.

## Verify-surface gap on this profile

`local_self_deployment_service::swap_status` (and the sibling
`swap_rollback`/`complete_swap`) is NOT bound in this profile's
`service_bindings.json` — only `self_deployment_service` is — so the call
returns `"Service 'local_self_deployment_service' not available in
orchestrator"`. Both the `deploy_local_blue_green_release` and
`verify_local_deploy_health` josekis START with `swap_status`, so they cannot
run their first step as written here. Verify a re-init instead with
`lifecycle_management_service::list_plugins` (expect the target plugin
`status: ready`) plus the plugin's own `test_connection`.

## Decision summary

- Config change to a `prepare_for_readiness`-built plugin → you MUST re-init the
  instance; a config reload alone is insufficient.
- Blue-green profile → re-init via `apply_manifest` (dry-run → CAS commit), not
  `launchctl`, not `set_plugin_enabled`.
- Verify with `list_plugins` + `test_connection`, not `swap_status` (unbound
  here).
