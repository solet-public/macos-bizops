# HTTP Reference

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:bridge, knowledge:tag:http_surface

Article Tags: planning-stage:server-side-internals, planning-stage:verifier-authoring, evidence-category:http-route-contract, evidence-category:bridge-event-shapes, domain:agent-messaging, domain:bridge, domain:http-surface

Embedding Description: The localhost FastAPI route table exposed by agent_messaging_plugin under /api/v1/bridge, covering bridge lifecycle, the platform-call surface (process_*, download), peer messaging, the bridge-delivery EDGE_SINK processes that feed channel events back to MCP clients, and the Codex-specific solet peer-message notification used by the local patched Codex CLI.

## Purpose

Document the localhost HTTP API exposed by `agent_messaging_plugin`.
This surface is consumed by the in-tree Python MCP stdio bridge
(`python -m agent_messaging_plugin.mcp_bridge`), by verifier scripts
that exercise the bridge directly without an MCP client, and by any
future native HTTP integration.

Every MCP tool the agent sees (`mcp__<server-name>__*`) maps to one of the
routes below; the MCP bridge subprocess forwards the call, polls
the event queue, and bridges results back as MCP notifications. Most
events use `notifications/claude/channel`; Codex peer wake events use
`notifications/solet/peer_message`.

## Bind point

```
PortManager: bridge
Runtime port file: ~/.ananta/runtime/<solet>.bridge.port
API prefix: /api/v1/bridge
Bridge id prefix: agc-
```

The port is allocated dynamically at `start_interface`; **no port
literal appears anywhere** — not in profile config, not in MCP
client config. Callers must read the runtime port file
(`~/.ananta/runtime/<solet>.bridge.port`) rather than
hard-coding a value. The MCP bridge subprocess discovers the port
via `SOLET_NAME` env var plus the port file.

**Writer, by topology (D11 ruling, 2026-07-13,
`workbench/2026-07-13_d11_bridge_port_discovery_routerless_ruling.md`):**
router-owned when a router is deployed (the blue-green router's
installer writes it); bridge-plugin-owned in router-less topologies
(`agent_messaging_plugin.start_interface` calls
`write_routerless_bridge_port_file` after confirming its own bind,
gated on a manifest-declared — never runtime-probed — router-presence
check). One writer per topology; the read-side contract above is
identical either way.

## Full route table

```
# Bridge lifecycle
POST   /api/v1/bridge/open
POST   /api/v1/bridge/{bridge_id}/close
GET    /api/v1/bridge/{bridge_id}/events?after=N    (long-poll)

# Platform call surface (agent → solet)
POST   /api/v1/bridge/{bridge_id}/process/search
POST   /api/v1/bridge/{bridge_id}/process/schema
POST   /api/v1/bridge/{bridge_id}/process/call
GET    /api/v1/bridge/{bridge_id}/process/result/{action_id}?follow_job=true  (optional)
GET    /api/v1/bridge/{bridge_id}/download/{blob_id}

# Peer messaging (agent → other live agents)
POST   /api/v1/bridge/{bridge_id}/peer/register
GET    /api/v1/bridge/{bridge_id}/peer/list
POST   /api/v1/bridge/{bridge_id}/peer/send
POST   /api/v1/bridge/{bridge_id}/peer/send_by_name
GET    /api/v1/bridge/{bridge_id}/peer/inbox
GET    /api/v1/bridge/{bridge_id}/peer/holds_role         (direct read, no action queue)
GET    /api/v1/bridge/{bridge_id}/peer/inbox_for_session  (direct read, no action queue)

# Health
GET    /api/v1/bridge/health
```

All `bridge_id`-scoped routes enforce strict bridge ownership: a
request whose URL `bridge_id` does not match the resource's
recorded owner returns an analogous 403/404 code for peer and
platform-call paths.

## Loop lag on health

`GET /api/v1/bridge/health` also includes `violations`:
`{count, last_process_key, last_stack, recent}`. These are actual handler-entry
observations, independent of elapsed time. Entry on a registered asyncio loop
thread is a violation unless the exact handler is declared dispatch-only with
a reason. `LOOP_INVARIANT_VIOLATION` logs its process key, kind, thread id and
entry stack. The invocation continues; telemetry never refuses or throttles it.

`handler_entries` exposes `{invocations, capacity, recent_entries, violations, handler_wall_time}`.
`handler_wall_time` is `{evicted_keys, by_process_key}`: for each plugin action
process key, `count`, `total_s`, `max_s` and `last_s` of timed handler calls in
seconds, including calls that raised. A synchronous handler's time is time it
held the executing thread; an awaited handler's time includes its suspensions.
At most `capacity` keys are kept; a new key beyond that evicts the
least-recently-recorded key and increments `evicted_keys`. The measurement is
advisory and never refuses, delays or changes a handler or its response.
Each entry includes sequence, process key, kind (`handler` or `route`), executing
thread id, `on_loop`, recording time (Unix seconds), dispatch-only reason or
`null`, and a stack for violations. Worker invocations are retained too, with
`on_loop: false`. By default, the last 128 entries and last 128 violations are separate
bounded rings; counters cover the process lifetime. No observations yields
count zero, null last fields and empty history. Snapshot reads do no database
or filesystem I/O. Stopping a loop removes its thread registration; histories
retire at process exit, while log history remains. A new process starts empty.

The recorder has no dispatch-only exceptions by default. The bridge declares
20 specific telemetry or offload-only endpoint bodies in
`ananta.core.runtime.loop_invariant.DISPATCH_ONLY_ROUTES`, each with its reason.
The `open_bridge` body is declared because its loop body only dispatches the
bridge open and sender attribution to the bridge workers and builds the response.
Declarations are reviewed source: add or
correct an exact key and reason, or remove it to clear the exception. There is
no prefix exemption, dynamic authorization change, or live reset verb. The
synthetic smoke proves both a loop violation and zero violations for the same
registered handler in a worker. This instrumentation covers the action
processor, service/plugin execution boundaries and this HTTP route layer;
the attached static async census is a worklist, not proof that every arbitrary
callback, external client or helper process has been instrumented.

`LOOP_LAG_SUMMARY` includes violation count, last process key and last stack.
Stacks retain at most 12 frames and 8192 characters by default. Retention and
stack limits use the startup settings documented in **Event Loop Lag Monitor
Configuration**. A zero count is evidence
only for observed invocations since process start, not a completed 24-hour run.

`GET /api/v1/bridge/health` includes `loop_lag`, a map from loop name to
`{p50_ms, p99_ms, max_ms, over_budget_count, window_s, samples, budget_ms,
interval_ms, sample_capacity, threads, thread_cpu_status, thread_cpu_window_s,
py_switch_interval_ms}`. Percentiles use nearest rank; measurements
are wakeup lateness in milliseconds over the rolling window. The count is
the number of over-budget samples in that same window. Before the first
sample, and after a window has emptied, the three lag values are `null`.
Each loop retains at most 10,000 samples; a window longer than that capacity
can cover fewer seconds than `window_s` at the configured sampling rate.

`threads` repeats the cached process-wide top eight live threads by CPU delta:
each row is `{name, id, cpu_s}` (user plus system seconds). On macOS, `id`
is the native thread ID matching Python `Thread.native_id`; names come from Python
or the OS, with `native` for unnamed native threads. `thread_cpu_window_s`
is the actual sampled span; its boundary can precede the requested window
by one census interval. `thread_cpu_status` is `pending`, `ok`, `truncated`
(4096-thread cap), or an `unavailable` diagnostic. Unavailable counters
produce an empty ranking and a warning, without affecting health readiness.
The daemon watchdog samples macOS `libproc` counters outside every event
loop, at a cadence of `max(1, window_s / 60)` seconds, retaining at most 62
samples. New threads contribute CPU since creation; exited threads leave
the next census. `py_switch_interval_ms` is the effective Python thread
switch interval. The platform article **Event Loop Lag Monitor Configuration**
documents its optional startup setting; unset or invalid input preserves the
current interpreter setting. These fields also appear in `LOOP_LAG_SUMMARY`.

The monitor starts before infrastructure/plugin initialization. Its stdlib
loop run hook covers `asyncio.run`, `asyncio.Runner`, explicit
`asyncio.new_event_loop` and implicit `get_event_loop` creation: the platform
action-poller loop, bridge HTTP and streamable listeners, optional Discord,
Google/Schwab OAuth and Vault bridge loops, and temporary bootstrap/backfill
loops. Names combine the running thread name and a unique sequence;
`platform/loop-N` denotes the main thread, and
`agent_messaging_plugin-server/loop-N` and
`agent_messaging_plugin-streamable-server/loop-N` name the listeners. Loops
created on one thread and run on another are named for the running thread.
Stopping a loop cancels its sampler and removes its window; restarting it
creates a fresh window. Separate client bridge, router, ingress and seat-tool
processes have their own lifecycles and are outside this process's map.

Monitoring is enabled by default. The process-wide startup settings, their
defaults and set/correct/clear lifecycle are documented in the platform article
**Event Loop Lag Monitor Configuration**. They apply to every loop in the
process, independently of which plugin provides the health endpoint.

One daemon watchdog inspects overdue heartbeats and emits a `LOOP_LAG`
warning with `phase=stalled`, including loop, lag, executing task, process
key and up to eight Python stack frames. The action poller publishes the
process key in the executing task's context and clears it after success or
failure; unrelated tasks are not attributed to an awaiting executor action.
An unavailable owner is `unknown`. Native code holding the GIL can prevent
the watchdog from running; wakeup lateness is still recorded after recovery,
with an explicit unavailable-stack warning when no live capture was possible.
The watchdog retains the captured stack and attribution for the scheduled
wake; `phase=recovered` attaches those same details with `capture=during_stall`
and final lateness. These two log phases describe one episode. Without a
during-stall capture, recovery uses `capture=unavailable_gil_or_scheduling_delay`
and `stack=unavailable_after_recovery`; missing capture cannot by itself
distinguish GIL retention from scheduler starvation. Earlier episodes' stacks
are never attached to a later wake.
`LOOP_LAG_SUMMARY` logs the current windows and handler-entry violation
counters at the summary cadence. These logs
provide historical evidence; the health map contains only running windows.

This telemetry has no database access. Ring/registry locks cover only bounded
in-memory copies; percentile sorting and logging happen outside those locks.
Over-budget lag never refuses, throttles, changes handler/executor placement,
or changes the endpoint's readiness/status decision. Existing `503 starting`
readiness responses retain their startup contract.

## Bridge lifecycle

```
POST /api/v1/bridge/open
  body: {}
  resp: { "bridge_id": "agc-…", "long_poll_timeout_seconds": 25 }

GET  /api/v1/bridge/{bridge_id}/events?after={cursor}
  long-poll; returns queued events in cursor order. Each event is
  one of: channel_message (post_message), bridge_delivery_result,
  bridge_delivery_error, peer_message. The cursor also
  ACKS: events at or below `after` are drained server-side, the caller's
  peer binding is touched (liveness), and — for a role-addressed delivery
  to a no-MCP watcher (`agi-watch-*`) — the ack advances the role-replay
  consumption mark (`mark_role_consumed_on_ack`), so the pull recipient
  having provably streamed it retires that role message from replay.

POST /api/v1/bridge/{bridge_id}/close
  body: {}
  resp: { "status": "closed" }
```

Each bridge owns a platform session id
(`session_manager.create_session` under the
`agent_messaging_plugin` namespace). The session id appears on
every thread row's `originator_session_id` and in every
bridge-delivery `trigger_data["session_id"]`.

Blocking bridge request bodies, including `/open` session creation and the
streamable OAuth authorize/token bodies, use `run_bridge_work`, backed by each
serving loop's named `solet-bridge-<loop>` pool: four workers and at most 32
additional queued calls by default. The streamable MCP dispatch, authorization
and close bodies use a separate `solet-streamable-<loop>` pool. Configure and
retire these pools as described under **Worker isolation** in
`08_mcp_bridge_troubleshooting.md`. Full admission waits asynchronously;
it neither blocks the serving loop nor refuses the request. The long-poll wait
and `/health` remain on the loop. Diagnostic writes serialize separately from
the registry lock, so slow diagnostic storage cannot hold up registry readers.

After a model-initiated handler returns, its authoritative in-memory activity
timestamp is captured before sending the response. The durable binding mirror
runs in the background on the bounded request executor, in order per identity,
using that captured timestamp. Mirror or executor failures are logged and keep
the response and in-memory stamp. Forwarder/infra classification is unchanged.
Calls queued for a loop worker are removable on cancellation; submitted calls
keep their slot until the native worker finishes. Authentication, route bodies
and response envelopes are unchanged.

Pairing, Google OAuth and Schwab OAuth each own an independent single-worker
executor with at most 32 queued jobs. Pairing bodies remain serialized through
verification, mint and any orphan compensation. Vendor nonces are consumed on
the request loop before queuing config load, exchange and token persistence.
Vendor callback mutations remain serialized while asynchronous admission waits.
Admission propagates request context; caller cancellation retains an admitted
mutation and its slot until the worker actually completes. These pools do not
share asyncio's default executor or the delivery-drive executor.

Idle bridges are reaped by the bridge-lifecycle sweeper (REL-09): a
plugin-owned daemon thread runs `BridgeSessionManager.sweep_idle` every
`bridge_sweep_interval_seconds` (default 300), expiring bridges idle past
`bridge_idle_timeout_seconds` (default 3600). Every expired bridge gets
the SAME full cleanup as an explicit close — inference-provider sidecar
clear plus tombstone, the sys:autonomic Trigger-2 succession hook, and
the peer-registry unregister — so an idle-swept session is
indistinguishable from a cleanly-closed one. At startup the plugin also
purges every persisted peer_binding row (pre-restart zombies; live
sessions re-register on reconnect).

## Platform call surface

These routes back the four direct `process_*` tools and
`download`. See `02_platform_call_surface.md` for the
agent-facing semantics.

```
POST /api/v1/bridge/{bridge_id}/process/search
  body: { "query": "...", "top_k": 8 }
  resp: { "processes": [ { "process_key", "description", "score" }, ... ] }

POST /api/v1/bridge/{bridge_id}/process/schema
  body: { "process_key": "service_interface::..." }
  resp: { "invocation_schema": { ... full JSON Schema ... } }

POST /api/v1/bridge/{bridge_id}/process/call
  body: { "process_key": "...", "arguments": { ... },
          "bridge_wait_attached": true } # optional, defaults false
  resp: { "action_id": "act-..." }   # 202 Accepted; result via events

GET  /api/v1/bridge/{bridge_id}/process/result/{action_id}?follow_job=true
  resp: { "status": <current status>, "result": <structured result>,
          "error": <job failure when present>, "next_read": <read while running> }
  # follow_job is optional and defaults to false. Finished jobs return their
  # result or error; running jobs return their status and next_read. The CLI
  # does not send follow_job and handles its own wait.

POST /api/v1/bridge/{bridge_id}/job/{job_id}/waiter/release
  body: {}
  resp: { "job_id": "job-...", "released": true | false,
          "bridge_wait_attached": false }

GET  /api/v1/bridge/{bridge_id}/download/{blob_id}
  resp: streamed blob bytes with Content-Type and Content-Disposition
        from the blob metadata
```

`process_call` is the zero-inference path: the action is submitted
to the orchestrator and the structured result is bridged back via
`plugin::agent_messaging_plugin::deliver_result` (or
`...::deliver_error`), emitted on the channel as
`bridge_delivery_result` / `bridge_delivery_error`. No inference
runs unless the bridge-delivery contract itself is violated.

The local CLI sends `bridge_wait_attached=true` before dispatch for default
and explicit `--wait` calls, including piped output. The root job persists
that intent and its originating flow/bridge/session before scheduling work.
Its terminal outcome is read through `job_service.get_job`; both role pushes
and fallback caller continuations are suppressed while the binding is attached.
Registered children consult that root binding at completion time: only their
caller-role message is suppressed, and internal continuations continue normally.
They have no independent marker; the single root release restores their later
notifications as well.
Absent/false intent preserves existing routing, including `--no-wait` calls.

Waiter release requires the originating open bridge and session. It changes
only waiter metadata, retains attachment/release history, and is idempotent;
it never cancels or replays the job. A suppressed terminal job routes after a
successful first release. Foreign ownership returns
`bridge.waiter_owner_mismatch` (403); unknown job returns
`bridge.job_not_found` (404); a stale metadata compare-and-set returns
`bridge.waiter_release_conflict` (409). The active job manager coordinates
release with terminal routing. CLI timeout makes one release attempt and
returns `job_wait_timeout` plus the durable job id; an uncertain release is an
explicit advisory, with no automatic retry. Waiting calls also make one release
attempt on SIGINT, SIGTERM, SIGHUP, SystemExit during waiting or output, and
output failure, before closing the originating bridge. SIGKILL or host loss
cannot run cleanup and may leave the waiter attached. At the cross-runtime
timeout or SIGKILL/host-loss boundary, at most one root completion wake may be
missed as an explicit advisory; the durable result remains redeemable through
`get_job`. Reconcile that job before another submission.

## Peer messaging

```
POST /api/v1/bridge/{bridge_id}/peer/register
  body: { "agent_id": "codex", "agent_instance_id": "agi-...",
          "session_label": "codex on baroque-suite" }

GET  /api/v1/bridge/{bridge_id}/peer/list
  resp: { "agent_ids": ["claude_code", ...],
          "instances": { "<agent_id>": [ {agent_instance_id, session_label, ...}, ... ] } }

POST /api/v1/bridge/{bridge_id}/peer/send
  body: { "peer_id": "claude_code",
          "peer_agent_instance_id"?: "agi-...",   # required if N>1
          "content": [ {"type": "text", "text": "please review ..."} ] }
  resp: { "delivery": "queued_notification" | "queued_wake"
                    | "queued_watcher",
          "thread_id": "...", "message_id": "..." }
  # A4 (2026-08-04): delivery is unconditional — every send is
  # delivery-attempted against the resolved recipient's live binding, no
  # sender-declared marker gates it. A resolved-but-stale binding fails the
  # call loud with peer_unreachable instead of returning a delivery kind
  # that will never drain; the message is still durably persisted.
  # A queued_* delivery means the platform placed a turn-triggering event on the
  # recipient's live bridge queue; emission to the client happens at the
  # forwarder's next drain, and whether it becomes a turn is client-side and is
  # NOT confirmed by this field (REL-06). There is no per-message
  # consumption-insurance apparatus anymore (the REL-05 direct-wake outbox
  # retired with A4) — staleness is caught at the session level instead,
  # via the recipient's own report_by / sweep_overdue_sessions contract.
  # queued_watcher = the recipient is a no-MCP `<name> watch` binding: the same
  # queued event streams into the watch output (an idle session with the
  # `<name> wake` Stop hook installed starts a turn on it; a busy or hook-less
  # one sees it at its next look), and the watcher's /events cursor ack stamps
  # it consumed.

POST /api/v1/bridge/{bridge_id}/peer/send_by_name
  body: { "name": "Git-Controller",
          "content": "please commit ..." }
  resp: { "delivery": "queued_notification" | "queued_wake" | "queued_watcher"
                    | "queued_for_replay",
          "thread_id": "role:Git-Controller",
          "message_id": "...",
          "resolved_agent_id": "claude_code",
          "resolved_agent_instance_id": "agi-..." }
  # queued_for_replay: the role's current holder has no live, polling
  # binding — the envelope is durable and replays on that holder's next
  # attach via the surviving role-replay path.
  # The registered sender takes precedence. A local one-shot CLI bridge may
  # instead resolve its launcher-provided caller_agent_session_id to exactly
  # one existing peer binding. This does not register the caller bridge or
  # replace the receiving binding. Resolution runs again on every send.
  # Unregistered OAuth bridges, missing/unknown attribution and ambiguous
  # session bindings are refused with a remedy; no sender identity is guessed.
  # CLI: solet-bridge send-by-name Project-Solet-Main --content-file report.txt
  # Run inside the controller launcher, preserving AGENT_SESSION_ID. No
  # receiving bridge id is needed. The CLI prints the complete direct receipt
  # and exits nonzero on refusal; it never silently uses /process/call.
  # The Git-Controller landing_report.py wrapper explicitly tries the queued
  # peer_send_by_name process after a direct failure, and discloses that failure
  # in both the report and output. A timeout may follow persistence: reconcile
  # duplicate reports by their landing identity before manually resending.

GET  /api/v1/bridge/{bridge_id}/peer/inbox?since=<recent-ISO-time>
  # Two instance directions, never both: since=T starts with messages strictly
  # newer than T, oldest-first (continue with next_since_cursor as
  # since_cursor); after=T walks backward, messages older than T, newest-first
  # (echo next_after_created_at). No cursor returns the newest page,
  # newest-first.
  resp: { "entries": [ {sender_agent_id, sender_agent_instance_id,
                        sender_session_label, message, ...}, ... ],
          "role_entries": [ {sender_agent_id, sender_agent_instance_id,
                              sender_session_label, message, ...}, ... ],
          "next_after_created_at": null,   # set only on a non-since page
          "next_since_created_at": "...",  # compatibility timestamp
          "next_since_cursor": "...",      # opaque tie-safe continuation
          "instance_exhausted": false,
          "next_role_cursor": "..." }
  # Instance mail is exhausted only when instance_exhausted is true under the
  # requested direction; the opaque forward cursor is tie-safe by message id.
```

See `03_inter_agent_messaging.md` for the delivery
contract, multi-instance addressing rules
(`peer_ambiguous` / `peer_unreachable`), and native-channel wake
routing for Claude Code and the locally patched Codex CLI.

For Codex bridges, `peer_message` and `post_message` events are emitted
on the MCP transport as:

```text
notifications/solet/peer_message
  params.content = readable peer envelope + message prose
  params.meta    = bridge metadata, including thread/message ids,
                   sender ids, recipient ids, cursor, and
                   trigger_turn=true
```

This method is intentionally narrow. Bridge-delivery result/error events
and non-peer Codex events still use `notifications/claude/channel`.

## Direct act-time reads (no action queue)

`peer_holds_role` and `peer_inbox` are platform processes, so a caller with no
registered bridge reaches them through `process/call`, which runs on the
serial action queue. A deploy (`apply_manifest` holds that queue for the whole
action) or a slow `peer_send_by_name` therefore froze the Git-Controller
landing guard even though both reads are cheap state reads (iss_d97f6633). The bridge's HTTP surface stays responsive while the
queue is held, so the same two reads are served directly:

```
GET /api/v1/bridge/{bridge_id}/peer/holds_role?name=N&agent_instance_id=I
  resp: { "holds": true, "name": "N", "agent_session_id": "ases-...",
          "delivery_route_attached": true,
          "instance_registered": true, "session_label": "..." }
  # The first four fields are exactly what the peer_holds_role process returns.
  # instance_registered and session_label come from the instance's own
  # peer_binding row (the same row that supplied agent_session_id).

GET /api/v1/bridge/{bridge_id}/peer/inbox_for_session
      ?agent_session_id=S&observer=true&limit=100&after=T&since=T&role_after=C&include_covered=true
  resp: the page the peer_inbox process returns for S (entries, role_entries,
        both cursors, role_section_status, ...)
  # include_covered (observer-only, default false) reads the role section with
  # no covered-mark floor; it is the only request field besides the cursors
  # that crosses to the server besides the cursors. `since` (ISO-8601, forward
  # horizon) bounds BOTH sections and is echoed as role_since_created_at (null =
  # the role section did not apply it); it excludes `after` (the opposite,
  # backward direction), so `solet-bridge inbox --since` sends it on its
  # role-section pages only and fails the command (role_since_not_applied) when
  # a page does not echo it. The page also carries role_read_coverage, one
  # {role, covered_mark_present, floor_hidden, receipt_hidden} entry per role the
  # role section read: counts of rows the covered-mark floor and a display
  # receipt hid inside the scanned window, never their content. A page that
  # comes back with
  # role_floor_applied=true for an include_covered request means the server or
  # the route dropped the field, and `solet-bridge inbox --include-covered`
  # treats that as a loud fault.
```

Each route and its process call ONE shared function
(`peer_direct_reads.read_holds_role` and `read_peer_inbox_for_session`), so the
answer cannot drift between the two doors; every refusal of the process is the
same refusal on the route (`missing_argument`, `identity_not_registered`,
`peer_session_ambiguous` as 409, `invalid_after`, `invalid_since`, `peer_inbox_rejected`), and a
refusal is never an empty page. A malformed `role_after` is not a refusal of the
read: the page carries `role_section_status: "error"` at both doors. Nothing is
cached and no table is added. The caller needs no `peer/register`: the instance
or session is a query argument, so an unregistered one-shot `solet-bridge`
bridge reads without registering under, and sweeping, its own registry row.
`solet-bridge holds-role --name N --instance-id I` and `solet-bridge inbox` use
these routes.

**Route policy.** The two routes are held to the policy of the process each one
stands in for:

- The bridge is resolved exactly as `process/call` resolves it
  (`bridge_manager.get`); an unknown or closed bridge is refused 404
  `bridge_not_found`.
- The per-session allowlist (M5 §14.5) is `bridge_sessions.bridge_allows_process`,
  the one function `process/call` also uses, applied to the route's own process
  key (`plugin::agent_messaging_plugin::peer_holds_role`, respectively
  `plugin::agent_messaging_plugin::peer_inbox`). A stdio bridge
  (`client_id == ""`, which is every `solet-bridge` one-shot) has no allowlist,
  as on `process/call`. An OAuth-bound bridge must carry the process key, or be
  operator-equivalent (unrestricted), or it is refused 403
  `bridge.process_not_allowed`; an empty allowlist refuses everything.
- Both routes are `MODEL_INITIATED` in `route_activity`, as `process/call` is, so
  model-activity stamping is unchanged; moving them to INFRA is a separate choice.
- The process export policy is not consulted: these are bridge routes like
  `peer/inbox` and `peer/list`, not process exports.

What stays refused: a real outage or restart, or a hold that freezes the HTTP
surface itself (a GIL-holding handler), still refuses a landing. That is correct
and unchanged. Only the refusal caused by a healthy HTTP surface with a held
action queue goes away.

## post_message (solet → agent)

This is the IO interface surface. Direct HTTP submission of
`post_message` is not supported — the model authors
`plugin::agent_messaging_plugin::post_message` as a normal EDGE_SINK
action, the action queue executes it, and the plugin enqueues a
`channel_message` event onto the bound bridge:

```
notifications/claude/channel
  params.content      = the message text (plain prose)
  params.meta.source           = "solet"
  params.meta.event_type       = "channel_message"
  params.meta.flow_id          = originating flow id
  params.meta.cursor           = monotonic event cursor
```

See `processes/post_message.json` for the schema and error tokens.

## Bridge delivery (EDGE_SINK pair)

`agent_messaging_plugin` exposes two EDGE_SINK processes that the
platform's bridge-delivery dispatcher submits on behalf of any
direct `process_call`:

- `plugin::agent_messaging_plugin::deliver_result` — appends a
  `bridge_delivery_result` event with the structured payload from
  the called process.
- `plugin::agent_messaging_plugin::deliver_error` — appends a
  `bridge_delivery_error` event with the platform's fixed failure
  payload.

Both are terminal (no result/error processors) so the dispatcher's
submission cannot recurse into inference.

These keys are NOT denied from direct MCP exposure — the export-deny list
(including the enumerated agent_messaging "private surface") was emptied by
operator ruling 2026-07-15 (see
`workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
In normal operation only the platform submits them as a side effect of
completing a `process_call`'d action; a caller invoking them directly would
just be appending a delivery event to its own bridge, which is harmless since
the delivery target is server-derived from the caller's own identity, never
caller-supplied.

## Error code reference

| HTTP | code | When |
|---|---|---|
| 400 | `agent_request_invalid` | malformed `peer_send`/`peer_inbox` body, oversize content, etc. |
| 400 | `peer_ambiguous` | `peer_send` to a kind with multiple registered instances and no `peer_agent_instance_id` hint |
| 403 | `bridge.process_not_allowed` | an OAuth-bound bridge's per-session allowlist lacks the process key behind `peer/holds_role` or `peer/inbox_for_session` |
| 404 | `bridge_not_found` | URL `bridge_id` is unknown or closed |
| 404 | `peer_unreachable` | targeted `peer_agent_instance_id` is not registered |
| 503 | `agent_messaging_disabled` | plugin config `enabled: false` |

`bridge_id` ownership is the canonical authorization model. There
is no user-principal reclaim across bridge reconnects; historical
thread rows (including retired backend-dispatch threads) remain
queryable via state SQL but are not addressable through the public
HTTP surface.
