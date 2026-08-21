# Part 46 — feedback round from dax

Two-item round; needs a parent issue.

**Release this round was measured against:** HEAD `f4147c9` (merged seed
re-mints `f299512..e934bb4`) / RELEASE_NOTES.md 2026-08-17 release.

**What this round is about:** Two defects surfaced while applying this
release's own update to a live, already-hydrated clone: `apply_manifest`'s
zero-downtime path failed on a manifest it should have accepted, and a
tmux-spawned worker's environment did not carry the CLI-directory PATH
prepend that this same release's own machinery (`resolve_solet_bin` /
`expose_worker_cli`) appears designed to guarantee.

---

## §46.1 — DEFECT: apply_manifest's cutover preflight validates against the running process's stale in-memory modules, not the on-disk code it is about to activate  [label: defect]

**Item number:** §46.1

**The exact command or action:**

```
solet call service_interface::lifecycle_management_service::apply_manifest '{
  "new_manifest": {"plugins": [<the exact current roster, unchanged>]},
  "reason": "pick up a just-merged seed re-mint, unchanged plugin roster",
  "expected_etag": "<etag from a prior dry_run>"
}'
```

Immediately after a `git merge` had landed this release's changes on disk
(no dependency changes, no plugin roster changes — a dry-run of the same
call the moment before had already reported an empty diff).

**Observed output:**

```
{
  "status": "restart_failed_after_manifest_commit",
  "message": "Manifest written locally (and to S3 for cloud) but the
    deployment plugin could not schedule the restart. The running color
    still has the old config; future boots will pick up the new manifest.
    ...",
  "rejection_reasons": [
    "restart_failed_after_manifest_commit: deployment plugin returned
     status='failed': cutover preflight blocked on root_manifest drift:
     ROOT MANIFEST CHECK — BLOCKING

     Schema validation:
       - manifest schema violation at <root>: Additional properties are
         not allowed ('solet_name' was unexpected)

     Manifest: <repo>/root_manifest.yaml @ schema_version 1
     Homunculus: <unknown>"
  ]
}
```

`root_manifest.yaml` had carried `solet_name:` as a required top-level key
since well before this release (confirmed via `git log` on
`ananta/src/ananta/core/root_manifest/schema.json` — unchanged since a much
older commit), and the deployment had been running under that same manifest
shape without incident.

**Expected behavior:** `apply_manifest`'s cutover preflight should validate
the candidate manifest against the code it is about to cut over TO (or at
minimum, against the code actually on disk right now), not against whatever
schema/classifier logic happened to be imported into the currently-running,
not-yet-restarted process's memory. A zero-downtime code-pickup path that
can refuse a demonstrably valid manifest defeats its own purpose exactly
when it is needed most — right after a real update.

**Diagnosis (marked as inference, but backed by a direct reproduction):**
Running `jsonschema.Draft7Validator` directly against the on-disk
`ananta/src/ananta/core/root_manifest/schema.json` and the on-disk
`root_manifest.yaml`, using this checkout's own `.venv`, produced **zero**
validation errors. `_preflight_root_manifest` in
`macos_self_deployment_plugin/src/macos_self_deployment_plugin/
swap_orchestrator.py` calls `classify_root_entries`/`format_report` imported
from `ananta.core.root_manifest` at the TOP of that module — i.e. whatever
was imported into the currently-running process at ITS last start, not a
fresh subprocess import per call. The `"Homunculus: <unknown>"` label in the
error text (a leftover pre-rename string in `report.py`'s formatter,
`classification.solet_name or "<unknown>"`) is consistent with the
classifier's error branch never having populated `solet_name` — i.e. a
schema/classifier snapshot old enough to not expect it, still resident in
the running process from before its last full restart.

**Local fix or divergence carried:** Fell back to a bare LaunchAgent
restart (`launchctl unload`/`load`), which forces fresh module imports; a
follow-up `apply_manifest` dry-run then passed cleanly with an empty diff.
No code change made on our side.

**Content gate:** ticked after re-reading this text.

---

## §46.2 — DEFECT: a tmux-spawned worker's PATH did not carry the CLI-directory prepend `resolve_solet_bin`/`expose_worker_cli` appear designed to guarantee  [label: defect]

**Item number:** §46.2

**The exact command or action:**

```
solet call plugin::agent_messaging_plugin::spawn_session '{
  "role_class": "project", "role_name": "<role>",
  "lane_id": "<lane>", "brief_ref": "<workbench-path>",
  "work_class": "production_mutation", "budget_line": "<budget>",
  "host": "tmux", "visibility": "visible"}'
```

then, inside the spawned worker's own first turn, a plain Bash tool call:

```
echo PATH=$PATH && which solet && which dax && ls <repo>/.venv/bin/
```

**Observed output:**

```
PATH=/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin
which solet -> not found
which dax -> not found
```

No `.venv/bin` (or any release-tree `venv/bin`) directory appeared anywhere
in `PATH`. This matches the exact global tmux server environment
(`tmux show-environment -g` reports the identical five-entry PATH) rather
than anything CLI-directory-aware.

**Expected behavior:** Per `solet_cli.py`'s own docstrings —
`resolve_solet_bin`'s "Materialized blue-green releases intentionally run
with a minimal PATH... [rung 2 resolves] the release's Python executable
and `solet` console script [as] siblings" and `expose_worker_cli`'s "AND
prepends its directory to PATH (so the hooks and skills that invoke a bare
`solet`... resolve it too)" — and `tmux_adapter.py`'s own call site
(`cli_env: dict[str, str] = {"PATH": ...}; expose_worker_cli(cli_env,
solet_bin)`, then passed through as an explicit `-e PATH=<value>` to the
`tmux new-session` invocation) — a spawned tmux worker's PATH should have
had the CLI directory (this checkout's `.venv/bin`, containing an
executable `solet` at the time of the spawn) prepended. It did not.

**Diagnosis (marked as inference — the worker session ended before a
deeper live probe was possible; two candidate failure points, not
distinguished here):**
1. `resolve_solet_bin`'s own resolution may have failed silently on this
   host — its docstring explicitly documents this as non-fatal-by-design
   for the Claude adapters ("the Claude adapters fall back to the bare
   command name, preserving their pre-fix behavior exactly rather than
   refusing a spawn that used to work"), so a resolution failure here would
   produce exactly the observed symptom with no error anywhere. `.venv/bin/
   solet` did exist and was executable on this host at spawn time, so if
   this is the cause, the failure is in HOW it's discovered
   (`shutil.which("solet")` against the calling — not the worker's —
   PATH, then a sibling-of-`sys.executable` fallback), not in the target
   file's absence.
2. Alternatively, resolution may have succeeded and computed the correct
   PATH value, but the `-e PATH=<value>` flag passed to `tmux new-session`
   did not take effect in the resulting pane (e.g. overridden by a later
   step, or by the pane's own shell startup).

**What it costs us:** A spawned tmux worker cannot use this platform's own
documented no-MCP CLI access mode (`solet call ...`) for anything —
knowledge-base search, checking its own peer list, any process call — not
just the git-rename workflow this originally surfaced in (which #13 in a
prior round separately fixed, by removing the NEED to rename). Any worker
whose actual assigned task requires calling the platform directly, on a
tmux host, is silently unable to.

**Local fix or divergence carried:** None — the workaround used was
routing the actual git-mutating work through the DRIVING (already-CLI-
capable) session instead of the spawned worker, for the task this surfaced
during. No code change made on our side.

**Content gate:** ticked after re-reading this text.
