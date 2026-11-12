# Scheduling Service Reference
Article Layer: 1

The scheduling service provides two types of scheduled execution:
- **Recurring** schedules via cron expressions
- **One-time** delayed execution after a specified number of seconds

All scheduled wake-ups execute independently of the current inference chain. They fire asynchronously through the action queue.

---

## Execution Modes

Both scheduling operations require exactly one execution mode:

1. `action_definitions`: a non-empty list of syntactically valid canonical
   `{process_key, arguments}` objects. Registration validates entry shape and
   the scheduled-action result-processor policy; it does not promise that a
   named process exists. Execution resolves each `process_key` against processes
   registered at fire time. Use this mode for work that must happen, including
   scheduled peer notifications.
2. `memory_tag`: a terminal
   `service_interface::memory_service::get_memories_by_tag` read. The fetched
   content is written to the scheduled action's result row; no model turn or
   downstream action starts. Use this only when that terminal read is the whole
   intent.

Supplying both modes or neither mode fails loudly. `actions` remains an internal
legacy alias, while `action_definitions` is the discoverable public field.

---

## create_cron_schedule

Create a recurring schedule using a cron expression. The schedule fires repeatedly until cancelled.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `cron_expression` | string | Yes | Standard 5-field cron expression (UTC) |
| `action_definitions` | list | One mode required | Non-empty, syntactically valid `{process_key, arguments}` actions |
| `memory_tag` | string | One mode required | Terminal memory read; does not start a model turn |
| `label` | string | No | Human-readable label |
| `tags` | list | No | Tags for grouping (used by `clear_scheduled_actions_by_tag`) |

### Cron Expression Format

```
minute (0-59)
hour (0-23)
day of month (1-31)
month (1-12)
day of week (0-6, 0=Sunday)
```

**Common expressions:**

| Expression | Meaning |
|-----------|---------|
| `* * * * *` | Every minute |
| `*/5 * * * *` | Every 5 minutes |
| `0 * * * *` | Every hour (at minute 0) |
| `0 9 * * *` | Daily at 9:00 AM UTC |
| `0 9 * * 1-5` | Weekdays at 9:00 AM UTC |
| `0 0 * * 0` | Weekly on Sunday at midnight |
| `0 */6 * * *` | Every 6 hours |

### Example: Scheduled Peer Notification Every 15 Minutes

```json
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "*/15 * * * *",
    "label": "Coordinator status reminder",
    "tags": ["coordination:status-reminder"],
    "action_definitions": [
      {
        "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
        "arguments": {
          "name": "Coordinator",
          "content": "Scheduled reminder: review active scheduler work."
        }
      }
    ]
  }
}
```

The scheduler invokes `peer_send_by_name` directly. No Claude or Codex
inference is required when the schedule fires. The peer message is the action
itself, not a prompt asking a seat to perform some other scheduled action.

### Example: Scheduled Mechanizable Joseki

```json
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "0 6 * * 1",
    "label": "Weekly platform quality sweep",
    "action_definitions": [
      {
        "process_key": "service_interface::thinking_service::run_joseki",
        "arguments": {
          "joseki_key": "run_platform_quality_gates",
          "bindings": {},
          "label": "Scheduled platform quality sweep"
        }
      }
    ]
  }
}
```

Scheduler submission works for registered, mechanizable cards such as the
closed-world deterministic `run_platform_quality_gates` card. `run_joseki`
validates the card and bindings at execution time; this example does not imply
that inference-bearing or otherwise non-mechanizable cards are admitted.

---

## ensure_global_heartbeat

Ensure the platform has a single global heartbeat wake-up scheduled (tag convention: `heartbeat:global`).

This is an idempotent helper for liveness and responsiveness:

- If no heartbeat exists, it creates one.
- If duplicates exist, it normalizes back to a single schedule.
- If a heartbeat exists with the same cadence, it returns the existing schedule.

The helper currently uses the terminal `memory_tag` mode. It can maintain the
scheduled read/touch, but it does not create a model turn or cause a model to
act on the fetched content.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `cadence_minutes` | integer | No | Desired cadence in minutes (default: 5) |
| `tag` | string | No | Schedule tag to use (default: `heartbeat:global`) |
| `memory_tag` | string | No | Memory tag to wake up (default: same as tag) |

### Example: Ensure a 5-minute global heartbeat

```json
{
  "process_key": "service_interface::scheduling_service::ensure_global_heartbeat",
  "arguments": {
    "cadence_minutes": 5
  }
}
```

---

## get_schedules_by_tag

List schedules matching a tag. Use this for introspection (detecting missing heartbeats, duplicates, or stray per-job check-ins) without querying raw tables directly.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tag` | string | Yes | Tag to match |

### Example: Check whether a global heartbeat is present

```json
{
  "process_key": "service_interface::scheduling_service::get_schedules_by_tag",
  "arguments": {
    "tag": "heartbeat:global"
  }
}
```

---

## execute_in_seconds

Schedule one-time direct action execution or a terminal memory read after a
delay. Exactly one execution mode is required.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `seconds` | integer | Yes | Delay in seconds (must be > 0) |
| `action_definitions` | list | One mode required | Non-empty, syntactically valid `{process_key, arguments}` actions |
| `memory_tag` | string | One mode required | Terminal memory read; does not start a model turn |
| `content` | string | No | Content to store before a terminal memory-tag read; invalid in action-definition mode |
| `label` | string | No | Human-readable label |
| `tags` | list | No | Tags for grouping and cancellation |

Content is valid only with `memory_tag`. The plugin validates the complete
mode/content/action request and the scheduled-action policy before any memory or
schedule write. If valid `content` and `memory_tag` are both provided, it then
stashes the instructions as a memory automatically (tagged with `memory_tag`)
before creating the wake-up. If you omit `content`, the system assumes the
memory was already stashed (two-step pattern). Supplying `content` with
`action_definitions` fails with
`default_scheduling_plugin.parameter_error` rather than being ignored.

`execute_in_seconds` rejects inference-bearing scheduled actions before
persistence, using the same scheduled-action validator as recurring
registration and both restoration paths.

Note: the one-step stash uses only `tags=[memory_tag]`. If you need richer tags (for example `session:<id>` or `job:<id>`), use the two-step pattern.

### Returns

| Field | Type | Description |
|-------|------|-------------|
| `schedule_id` | string | Unique schedule identifier |
| `message` | string | Confirmation message |
| `run_at` | string | ISO 8601 timestamp when the wake-up will run |

### Example: One-Time Scheduled Peer Notification

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "label": "One-minute coordinator reminder",
    "action_definitions": [
      {
        "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
        "arguments": {
          "name": "Coordinator",
          "content": "Scheduled reminder: inspect the pending job."
        }
      }
    ]
  }
}
```

### Example: Terminal Memory Read

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "memory:recency-touch"
  }
}
```

The lookup result stays on the completed action row. This example does not
notify a peer, resume a seat, or start inference.

---

## clear_scheduled_action

Cancel a specific scheduled job.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `schedule_id` | string | Yes | The schedule ID returned by create/execute |

### Example

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_action",
  "arguments": {
    "schedule_id": "sch_abc123"
  }
}
```

---

## clear_scheduled_actions_by_tag

Cancel all scheduled jobs matching a tag. This is the preferred way to clean up recurring schedules.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tag` | string | Yes | Tag used when schedules were created |

### Example

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_actions_by_tag",
  "arguments": {
    "tag": "followup:sess-abc123:tts"
  }
}
```

---

## Common Patterns

### Pattern: Deterministic Progress Check

Schedule the check verb itself in `action_definitions`. If the verb must notify
a peer, either make that notification part of the deterministic verb or schedule
`peer_send_by_name` directly. Do not use `memory_tag` as instructions for a
model; no model receives that result at fire time.

### Pattern: Timeout with Fallback

Schedule the deterministic fallback action in case the primary task takes too
long:

```
Step 1: execute_in_seconds(seconds=300, action_definitions=[{"process_key": "<fallback EDGE/EDGE_SINK verb>", "arguments": {"job_id": "<id>"}}])
Step 2: ... execute primary task ...
Step 3: clear_scheduled_actions_by_tag(tag="followup:timeout:<id>")  // Cancel if task completed in time
```

---

## Schedule Lifecycle

| Status | Description |
|--------|-------------|
| `scheduled` | Awaiting execution |
| `running` | Currently executing |
| `completed` | Successfully completed (one-time only) |
| `cancelled` | Manually cancelled via clear operations |
| `error` | Execution failed |
| `paused` | Temporarily paused |

---

## Important Notes

- All cron expressions use **UTC** timezone
- The `state` parameter is **auto-injected** -- do not set it manually. It carries `session_id` and `flow_id` for proper context routing
- Scheduled wake-ups execute **asynchronously** and independently of the current inference chain
- Provide exactly one of `action_definitions` or `memory_tag`
- `action_definitions` execute directly; `memory_tag` is a terminal read and starts no model turn
- One-time schedules (`execute_in_seconds`) auto-complete after execution
- Recurring schedules (`create_cron_schedule`) continue until explicitly cancelled
- **Always tag your schedules** so they can be bulk-cancelled with `clear_scheduled_actions_by_tag`
- Schedules persist across service restarts


## Action targets, completeness and destination recovery

Use `service_interface::scheduling_service::list_schedules` without a filter to
observe the complete owner registry. `get_schedules_by_tag` uses the same
projection. `count` describes returned rows after filtering. `complete=true`
means valid paging was exhausted at unchanged persistent and memory epochs; the
owner retries a concurrent change once. Failures retain known rows with
`complete=false`, precise advisories, `checked_epochs` and an observation token.
Neither list verb changes the registry. Restoration preserves registered jobs
when its source is incomplete and discloses deferred rows.

Each row retains the existing schedule metadata and adds the exact copied
`action_definitions`, per-action `targets` with zero-based ordinals and actual
process keys, the owner revision, error, and runtime job presence/revision.
Messaging observations distinguish `retired`, `vacant`, `not_found`,
`disconnected`, `live` and `unavailable`. They come from the messaging owner,
not foreign table reads. A registered delivery route does not prove eventual
delivery. Context session/flow IDs are separate from action destinations.
Unsupported or ambiguous action destinations remain visible.

For `send_peer_message`, `peer_agent_session_id` is a fallback hint, not an
independent destination. Transport resolves `peer_id` and its optional instance
first; a sole registered peer can win over a different session hint. With only
the hint, the target is `kind=unknown`, `supported=false`, with advisory
`session_hint_is_fallback`. The original `peer_id` and hint remain readable in
`action_definitions`. A destination-only rebind of that hint is refused without
changing the schedule or history (`session_hint_rebind_matches_dispatch` tests
the real resolver against an in-memory registry). A raw peer action with both
instance and fallback remains ambiguous for destination-only rebinding. Role
targets and explicit instance targets without a fallback are supported.

`rebind_scheduled_action(schedule_id, action_index, target_kind, target_value,
expected_revision, reason)` corrects exactly one supported destination. Its
pass condition is an existing nonterminal action at the revision returned by
owner list; shipped create/list make that attainable and
`rebind_current_revision_passes` exercises it. Original text (including
whitespace, Unicode and newlines), nested non-destination values, result
processors, ID, cadence, context, label, tags and status remain unchanged.
Required destinations cannot be independently cleared into invalid actions:
rebind to correct/reverse, or use the existing clear verbs to terminate.
Unknown holder liveness is advisory and never blocks a rebind.

Rebinding an errored schedule leaves it errored. Explicit
`recover_scheduled_action(schedule_id, expected_revision, reason)` is the
error-to-scheduled transition for recurring schedules. Its attainable pass
condition is a recurring error row at the current revision, produced by the
owner creation/fire-failure paths and exercised by
`recover_error_current_revision_passes` and `recovered_error_restores`. Recovery
clears the error and reconciles one cron job without immediate firing or
duplicate jobs. `activation_status=pending` is not effective recovery; startup
restoration and explicit idempotent retries reconcile it. A retry at a current
scheduled revision does not append another event. One-time completion/clear
is terminal; creating a new schedule is a new identity, never a reopen.

Creation, rebind correction/reversal, recovery, status error/completion and
clear share owner transactions: conditional current-row writes, immutable
history, and epoch advance commit or roll back together. Existing revision-0
rows acquire an explicitly observed baseline on first mutation, without an
invented creator. Server CallContext supplies actor provenance; unavailable
attribution is recorded as such, and internal events name the scheduler.
The trusted local mutation authority is the existing create/clear authority;
no additional durable-role gate is introduced.

`get_schedule_binding_history(schedule_id, limit=100, after=None)` reads retained
events even after clear. Follow its `[created_at,id]` cursor; limits are 1..100.
History has no edit/delete operation. Revision and registry epoch
are initialized and advanced by their owner; namespace uninstall retires those
metadata and history. There is no reset-in-place operation.

Dispatch locks the current schedule through synchronous submission of its
ordered action set. A callback saved before rebind reads the new target when
invoked afterward. Work already submitted before the returned
`effective_revision` boundary is unchanged and may execute later. Clear and
completion serialize with rebind; an orphan callback is a disclosed no-op.
A no-op dispatch lock does not consume semantic revision, history or epoch.
Queue submission is not exactly-once external delivery, and queue persistence
and scheduling history are not one atomic transaction. Partial submission is
disclosed with the observed revision.


## Seat heartbeat code schedule

The platform-health `ensure_main_seat_heartbeat_schedule` installer keeps
tag `heartbeat:main-seat` and cron `7,27,47 * * * *` (UTC), but binds one
terminal `plugin::platform_health_plugin::check_main_seat_heartbeat_cron`
action with its runtime `role` argument. Its code-only worker reads register state and
fleet liveness; complete unchanged ticks send no role mail. First baseline
sends only for a pending delivery, an explicitly labelled STOP event, or an
overdue/stalled lane. Model wake telemetry is measured separately by the seat.
Partial/failed reads are stored as unmeasured with advisories; they never prove
quiet. The full unit/candidate/verdict/event/landing reads cover incomplete
resume sections. Heartbeat timestamps and observation times are excluded from
the semantic digest. Generic event changes are reported by exact event id;
explicit stop labels are `STOP:`, `STOP ` and `BLOCKED:` at the start of the
effective, nonwithdrawn event detail. Correction/withdrawal is a reversal.

Supply the seat role as installer `role` and the prior deployment tag as
`prior_tag`; neither origin value is embedded in shipped code. New installs
without a role return an advisory and retain existing schedules. The current
SOLET_NAME supplies the register/repository scope.

The code reader uses these documented environment settings, read at plugin
import: `MAIN_SEAT_HEARTBEAT_PAGE_SIZE` (default 100, maximum 100, matching
the state-service page contract), `MAIN_SEAT_HEARTBEAT_MAX_PAGES` (default
100 per domain), `MAIN_SEAT_HEARTBEAT_MAX_TOKENS` (default 10000 semantic
entries), and `MAIN_SEAT_HEARTBEAT_READ_TIMEOUT_SECONDS` (default 60 for one
register CLI read). Values must be positive integers. Invalid values emit a
warning and use the documented default. Exceeding a read/state bound is
unmeasured with an advisory, never evidence of quiet. `PROJECT_SOLET_CLI`
selects the installed read-only CLI; its default is the existing local
Project Solet virtual-environment entry point under the runtime user's home.

The installer reads current definitions/history, creates and confirms the
successor, then clears predecessors. It reuses a matching installed successor.
The scheduler only revises messaging destinations, so expected revision on
replacement is advisory; it cannot provide a stale-write refusal for an action
process-key replacement. Create-confirm-clear changes the schedule id. An
uncertain create is reconciled through the tag before retrying; the previous
schedule is retained unless successor confirmation succeeds. The boot action
is submitted after startup service binding. Live replacement belongs to the
seat after landing.

Marker lifecycle uses StateManagementInterface KV in platform_health_plugin:
current key `main_seat_heartbeat:current`, immutable history keys
`main_seat_heartbeat:history:<record_id>`. The registered check accepts
`operation=read` (optional historical `record_id`), `correct` (tokens), or
`clear`; correction and clear require `expected_record_id` and `reason` and
serialize with ordinary ticks. Clear is a tombstone and the next tick establishes
a baseline. Intent custody survives correction/clear. Readback includes
previous_record_id, complete/advisories, last_tick/last_successful_tick, observed
and notified digests, and send_intent. An intent is persisted before role send.
Recovery searches exact intent tokens in role envelopes; unresolved outcomes
are retained and never replayed. A persisted envelope proves durable custody,
not actual model wake or consumption. No raw SQL, markdown cursor or new schema.

Reversal is a seat-authorized recreation of this exact predecessor definition.
Confirm its creation before clearing the checker schedule; this deliberately
restores unconditional role wakes. Historical bindings are never edited.

```json
{
  "cron_expression": "7,27,47 * * * *",
  "label": "<role> seat heartbeat",
  "tags": [
    "<prior-tag>"
  ],
  "actions": [
    {
      "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
      "arguments": {
        "name": "<role>",
        "content": "<role> seat heartbeat (platform scheduler, schedule tag <prior-tag>; safety net for iss_ebc1ce9c / iss_6524070b). First run `date -u` and use that time, never an estimate. Then: drain peer_inbox; read the register for work in flight (psolet resume --solet <solet>; psolet db unit who-has-work) and the units named in your last GOAL line; act on a stop, a delivery, or a lane idle for more than 15 minutes; run delivered-to-review (dry-run first) when a delivery arrived. End with a GOAL: line."
      }
    }
  ]
}
```

After landing and seat-native replacement, observe three scheduled ticks across
an idle hour. Retain schedule/tag/history reads and actual seat inbox/ledger
wake telemetry. Zero checker sends alone does not prove zero model wakes;
disclose unavailable telemetry.
