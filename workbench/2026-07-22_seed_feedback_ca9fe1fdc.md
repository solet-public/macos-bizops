# Seed Feedback — `2026-07-22_local_branch_bizops_ca9fe1fdc` (commit ca9fe1fdc)

Written 2026-07-22 by Git-Controller session during a full birth-and-hydration
run on a clean machine. Intended for the birthing homunculus to review and feed
back to the seed authors.

## Summary

This seed is clean of the "Ada" naming defect found in the prior seed
(`2026-07-21_local_branch_bizops_cc6eee9e`). All env vars use neutral
`HOMUNCULUS_*` naming throughout. Three issues were found during setup;
one is a blocking defect, two are seed defects with workarounds applied.

---

## Issue 1 — BLOCKING: `root_manifest.yaml` declares files/dirs the seed never creates

**Severity:** Blocking (prevents blue-green restart at every `apply_manifest` call
until manually fixed post-birth)

**What happened:** After genesis and hydration, calling
`service_interface::lifecycle_management_service::apply_manifest` to trigger a
restart failed with:

```
ROOT MANIFEST CHECK — BLOCKING
Unknown entries (not declared in manifest):
  - PROVENANCE.json
  - client

Missing universal entries (declared but not present in tree):
  - .dockerignore
  - .githooks
  - .gitignore
  - .mcp.json
  - AGENTS.md
  - binary_libraries
  - pyproject.toml
  - workbench
```

`PROVENANCE.json` is written by genesis. `client/` is written by hydration. Both
are legitimate, permanent parts of every born homunculus, but neither is declared
in `root_manifest.yaml`. The eight "missing universal" entries are declared as
required but are not shipped by the seed and not materialized by genesis — they
only exist on a more developed homunculus repo.

**Root cause:** `root_manifest.yaml` reflects the state of a developed homunculus
worktree (the platform's own development repo), not the state of a freshly-born
seed clone. The seed ships a `root_manifest.yaml` that immediately flags its own
birth artifacts as drift.

**Fix applied (workaround):** Moved the eight absent entries and the two
present-but-undeclared entries to the `sanctioned` list in
`~/Workspace/dax/root_manifest.yaml` with `operator_approved: "2026-07-22"`. This
unblocks `apply_manifest` but requires manual intervention on every fresh birth.

**Recommended seed fix:** Either:
1. Remove all entries from `universal` that the seed doesn't ship, OR
2. Have genesis materialize them (stub `.gitignore`, stub `pyproject.toml`, etc.), OR
3. Pre-populate `sanctioned` in the shipped `root_manifest.yaml` with entries
   that are expected to be absent on a fresh seed clone.

The most principled fix is option 3: the seed knows which universals are
"expected absent at birth" vs. "always required." Document that distinction
directly in the shipped manifest rather than leaving it to the operator to
discover at first `apply_manifest` call.

---

## Issue 2 — DEFECT: `hydration_guidance.md` Step 1 instructs adding `session_ledger_service` to `service_bindings.json`

**Severity:** Medium (causes a boot crash and a failed blue-green swap if followed)

**What happened:** The `claude_code_filesystem_session_source_plugin/knowledge_base/hydration_guidance.md`
Step 1 instructs:

> Bind `session_ledger_service` in `<clone>/profile/config/service_bindings.json`
> to `postgres_state_management_plugin`

Following this instruction caused the next `apply_manifest` restart to crash the
new color with:

```
Failed to initialize orchestrator: load_service_bindings failed:
Service binding error for 'session_ledger_service': Unknown service name
```

**Root cause:** `session_ledger_service` is NOT in the `ServiceName` enum in
`ananta/src/ananta/core/orchestration/service_bindings.py` — it is not a named
service interface that can appear in `service_bindings.json`. It is initialized
directly in `_init_session_ledger_service()` in `startup_sequence.py`, reading
its config from `profile/config/plugins/session_ledger_service.json`
(already present and correctly configured in this seed — `ledger_allowed_roots`
was pre-populated).

**Actual setup flow (what works):**
- Do NOT add `session_ledger_service` to `service_bindings.json`
- The service auto-initializes at startup
- The source auto-registers via `_auto_register_declared_pulling_sources`
- Just call `service_interface::session_ledger_service::trigger_poll` with the
  `source_id` returned by `register_source` (or found via `list_sources`)

**Recommended fix:** Remove Step 1 from `hydration_guidance.md` entirely, or
replace it with a note that `session_ledger_service` auto-initializes and needs
no binding entry.

---

## Issue 3 — DEFECT: Multiple failed blue-green swaps accumulate orphan processes

**Severity:** Low (recoverable, but requires manual intervention)

**What happened:** The first `apply_manifest` call (before the root_manifest fix)
failed. Subsequent `apply_manifest` calls each spawned a new "next color" process
that also failed, resulting in 4 orphan ananta processes running simultaneously
alongside the original. Each consumed ~70MB RSS.

**Recovery:** Stopped the LaunchAgent, killed all 5 accumulated processes, reloaded
the LaunchAgent for a clean restart.

**Note:** This may be expected behavior (each failed swap attempt properly cleans
up its child via SIGKILL after the 600s timeout), but the accumulation was
surprising. If the swap executor is supposed to prevent concurrent swap attempts,
that guard may not be working correctly when the previous attempt is in the
"spawned but not yet registered" state.

---

## What worked correctly

- **Ada naming:** completely absent — `HOMUNCULUS_*` env vars used throughout. ✓
- **Genesis:** all steps completed including blue-green router install. ✓
- **`session_ledger_service.json` config:** `ledger_allowed_roots` pre-populated
  with correct paths (`~/.claude/projects`, `~/.claude/history.jsonl`,
  `~/.claude/tasks`). ✓
- **Fleet template:** `fleet_functions.zsh.template` uses neutral `HOMUNCULUS_*`
  vars, one-example-role pattern is correct. ✓
- **Session ledger functionality (after fixes):** source auto-registered,
  `trigger_poll` worked, sessions landed in ledger. ✓
