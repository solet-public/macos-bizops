# Scheduling Service Reference
Article Layer: 1

The scheduling service provides two types of scheduled execution:
- **Recurring** schedules via cron expressions
- **One-time** delayed execution after a specified number of seconds

All scheduled wake-ups execute independently of the current inference chain. They fire asynchronously through the action queue.

---

## Memory-Driven Scheduling

Schedules are **timers + memory addresses** (`memory_tag`).

When a schedule fires, the platform runs `service_interface::memory_service::get_memories_by_tag(tag=memory_tag)`. The recalled memory content arrives at a normal `process_results` vertex. The model reads the instructions and decides what to do next (post a status update, check a job, reschedule, or terminate).

This keeps schemas small and preserves the ReAct loop: observe, decide, act.

---

## create_cron_schedule

Create a recurring schedule using a cron expression. The schedule fires repeatedly until cancelled.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `cron_expression` | string | Yes | Standard 5-field cron expression (UTC) |
| `memory_tag` | string | Yes | Memory tag to wake up on each run |
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

### Example: Heartbeat Wake-Up Every 5 Minutes

```json
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "*/5 * * * *",
    "label": "Global Heartbeat Tick",
    "tags": ["heartbeat:global"],
    "memory_tag": "heartbeat:global"
  }
}
```

At each tick, the platform recalls memories tagged `heartbeat:global` and the model decides what (if anything) to do.

---

## ensure_global_heartbeat

Ensure the platform has a single global heartbeat wake-up scheduled (tag convention: `heartbeat:global`).

This is an idempotent helper for liveness and responsiveness:

- If no heartbeat exists, it creates one.
- If duplicates exist, it normalizes back to a single schedule.
- If a heartbeat exists with the same cadence, it returns the existing schedule.

The heartbeat itself should be low-noise: it only provides periodic wake-ups. What to do on each wake-up is model-driven (typically by recalling and processing queued follow-up memories).

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

Schedule a one-time wake-up after a delay, keyed by `memory_tag`.

When the wake-up fires, the platform retrieves memories tagged with `memory_tag` via `service_interface::memory_service::get_memories_by_tag`, and the model decides what to do next at the normal `process_results` vertex.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `seconds` | integer | Yes | Delay in seconds (must be > 0) |
| `memory_tag` | string | Yes | Memory tag identifying the follow-up memory to retrieve at wake-up time |
| `content` | string | No | Follow-up instructions to stash as a tagged memory (one-step pattern) |

If `content` and `memory_tag` are both provided, the scheduling plugin stashes the instructions as a memory automatically (tagged with `memory_tag`) before creating the wake-up. If you omit `content`, the system assumes the memory was already stashed (two-step pattern).

Note: the one-step stash uses only `tags=[memory_tag]`. If you need richer tags (for example `session:<id>` or `job:<id>`), use the two-step pattern.

### Returns

| Field | Type | Description |
|-------|------|-------------|
| `schedule_id` | string | Unique schedule identifier |
| `message` | string | Confirmation message |
| `run_at` | string | ISO 8601 timestamp when the wake-up will run |

### Example: One-Step Check-In

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "followup:sess-abc123:tts",
    "content": "Follow-up: if the TTS job is still processing, post a brief status update; otherwise deliver the audio."
  }
}
```

### Example: Two-Step Check-In (More Control)

Step 1 — stash the memory with richer tags:

```json
{
  "process_key": "service_interface::memory_service::remember",
  "arguments": {
    "content": "Follow-up: if the TTS job is still processing, post a brief status update; otherwise deliver the audio.",
    "tags": ["followup:sess-abc123:tts", "session:sess-abc123", "job:job_abc123"]
  }
}
```

Step 2 — schedule the wake-up:

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "followup:sess-abc123:tts"
  }
}
```

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

### Pattern: Progress Check-Ins During Long Tasks

1. **Seed a wake-up** (or rely on an existing global heartbeat): create a cron schedule or one-time delay with `memory_tag`.
2. **Store follow-up instructions** as memories (for example: queue items tagged `followup_queue:global`).
3. **On each wake-up**, check job status / progress, decide whether to send a user-visible update, and reschedule with an appropriate cadence.

### Pattern: Timeout with Fallback

Schedule a fallback wake-up in case the primary task takes too long:

```
Step 1: execute_in_seconds(seconds=300, memory_tag="followup:timeout:<id>", content="If still not complete, inform user and propose next steps.")
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
- One-time schedules (`execute_in_seconds`) auto-complete after execution
- Recurring schedules (`create_cron_schedule`) continue until explicitly cancelled
- **Always tag your schedules** so they can be bulk-cancelled with `clear_scheduled_actions_by_tag`
- Schedules persist across service restarts
