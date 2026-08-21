# Defect — fleet spawn is unreachable: obsolete preflight + unwired override

**Date:** 2026-08-21
**Severity:** blocks all multi-session work on this deployment
**Live release:** `rel-20260820T190035Z-2329ea4`
**Reproduced on:** both `headless` and `tmux` host drivers

## Symptom

Every `plugin::agent_messaging_plugin::spawn_session` call fails:

```
host_cannot_spawn — the managed Claude Code policy at
~/.claude/remote-settings.json lists 'hooks' in strictPluginOnlyCustomization,
which strips hooks from every non-plugin source — including the --settings blob
this driver injects the worker's hooks through. A worker spawned here would
answer turns while running NONE of its hooks: it would never register, never
heartbeat, and never capture its session mapping. Delivering the worker hooks as
PLUGIN hooks is the capability fix and is not yet shipped. To spawn anyway, set
degraded_hooks_acknowledged on the spawn.
```

## Defect 1 — the preflight's premise is obsolete

The capability fix the message says "is not yet shipped" **has shipped**, as a
plugin. Verified on this machine:

| Evidence | Value |
|---|---|
| `~/.claude/settings.json` → `enabledPlugins` | `{"coordination-hooks@bizops-claude-code-plugin": true}` — **user scope** |
| Plugin location | `~/.claude/plugins/cache/bizops-claude-code-plugin/coordination-hooks/0.6.0/` |
| Ships `.claude-plugin/plugin.json` + `hooks/hooks.json` | yes |
| Hook events registered | `SessionStart` ×4, `PostToolUse` ×3, `PreToolUse` ×1, `UserPromptSubmit` ×2, `Stop` ×1 |
| Worker-critical hooks present | `capture_session_mapping.py`, `heartbeat_report_alive.py`, `git_controller_gate.py` |
| `remote-settings.json` → `allowManagedHooksOnly` | `false` |
| `remote-settings.json` → `strictPluginOnlyCustomization` | `["hooks"]` |

`strictPluginOnlyCustomization: ["hooks"]` restricts hooks to **plugin-delivered
ones**. The worker hooks are now plugin-delivered, so they are precisely what
that policy permits — they would **not** be stripped.

Proof the plugin hooks actually run in an ordinary session on this machine
(observed repeatedly today): `git_controller_gate.py` blocked `git clone` and
`git -C`, and the 9-step pre-commit gate ran on every commit. Those are the
plugin hooks firing.

A spawned worker reads the same user-scope `~/.claude/settings.json`, so it
would inherit the same plugin hooks.

**Fix:** the preflight should test whether the worker's hooks are available as
plugin hooks, not whether `"hooks"` appears in `strictPluginOnlyCustomization`.
As written it fails closed on a condition that is now the *supported*
configuration.

## Defect 2 — the documented override is not reachable

The error instructs the caller to set `degraded_hooks_acknowledged`. Setting it
changes nothing — the identical refusal is returned.

Cause: **the parameter is not in the process's invocation schema.** `dax search
"spawn_session"` returns 19 accepted arguments:

```
agent_runtime, allow_askuserquestion, allowed_tools, brief_ref, budget_line,
effort, host, lane_id, model, permission_mode, provider, report_by_seconds,
role_class, role_name, spawned_by_instance_id, spawned_by_role, ttl_seconds,
visibility, work_class
```

`degraded_hooks_acknowledged` is absent, so the bridge drops it before it
reaches the implementation.

The implementation *does* support it — in the live release:

- `session_lifecycle_verbs.py:450` — field on the request dataclass
- `session_lifecycle_verbs.py:529` — plumbed to the driver
- `session_lifecycle_verbs.py:567` — recorded on the ledger row
- `plugin.py:8706` — `raw.get("degraded_hooks_acknowledged")`
- `schema.py:876` — ledger column definition
- `knowledge_base/processes/spawn_session.json` — documented at length in the
  process description

So the flag is implemented, persisted, and documented, but undeclared — which
makes the only advertised escape hatch unusable and the refusal unconditional.

## Consequence

No worker can be spawned by any means. On 2026-08-21 this forced an entire
day of BizOps work (Looker→Snowflake mapping, semantic-layer validation, the
`psb_export` live-resolution gate, the `parity_tools` migration, a knowledge-base
index repair) through a single serial session, with no parallel lanes and no
Git-Controller. Both defects are one-line-ish fixes; together they are a
complete capability outage.

## Reproduction

```bash
dax call plugin::agent_messaging_plugin::spawn_session '{
  "role_class": "project", "role_name": "Fleet-Probe", "host": "headless",
  "lane_id": "lane-probe", "budget_line": "x", "work_class": "read_only",
  "ttl_seconds": 600, "report_by_seconds": 300,
  "degraded_hooks_acknowledged": true,
  "brief_ref": "<any existing file>",
  "dispatch_text": "Reply ACK and stop."
}'
# -> host_cannot_spawn, identical with and without the flag; same on host="tmux"
```

## Related

A second, independent gap: the `dax watch` process holding the `Operator` role
registration was reaped as a background task belonging to a pre-`/clear`
session. The peer registry still lists `Operator` with no live process behind
it — presence without a holder. Role-addressed messages route nowhere until
`/rename Operator` re-arms the watcher.

## Recommended

File both upstream via the `feedback` skill. Evidence above is complete and
reproducible. Suggested framing: Defect 1 is a correctness bug in the preflight
predicate; Defect 2 is a schema/implementation sync bug that makes the
documented remedy inert.
