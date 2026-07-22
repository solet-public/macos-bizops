# Run-Turn And Durable Storage

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:run_turn, knowledge:tag:storage

Article Tags: planning-stage:server-side-internals, planning-stage:turn-execution, evidence-category:edge-process-contract, evidence-category:durable-schema, domain:agent-messaging, domain:storage

Embedding Description: How agent_messaging_plugin persists inter-agent threads and messages and how the run_turn EDGE process drives one turn against a backend agent, including thread lifecycle, the bridge-delivery trigger-data contract, and structured failure payloads.

## Purpose

This article is the server-side reference for inter-agent threads —
the durable record of one agent talking to another through the homunculus — and
the `run_turn` EDGE process that executes a single turn against a
backend agent (`codex`, `claude_code`). For the HTTP/MCP surface that
opens threads and dispatches sends, see `05_http_reference.md` and
`03_inter_agent_messaging.md`.

## Where this lives in the plugin

`agent_messaging_plugin` wears three hats (see `01_bridge_overview.md`).
This article is the "agent messaging core" hat: durable schema,
`run_turn` EDGE process, and the lazily-constructed
`AgentMessagingService` that the FastAPI route handlers call into.

## Persistence

Two tables in the `core` namespace:

- `core__agent_thread` — per-thread state
  (`originator_*`, `target_backend`, `target_plugin_name`, `status`,
  `working_directory`, `last_message_cursor`, `metadata`,
  `active_action_id`, `backend_session_id`, …).
- `core__agent_message` — append-only, cursor-addressable messages
  (`thread_id`, `cursor`, `role` ∈ `originator|agent|system`,
  `kind` ∈ `message|status|result|error|artifact`, `content`,
  `artifacts`, `metadata`, …).

Cursor allocation MUST be atomic. The repository uses
`state_service.transactional()` to wrap an `UPDATE … RETURNING
last_message_cursor` together with the message insert and any
status-update side effects. Read paths use the autocommit
`execute_sql` API.

## Lifecycle

Allowed thread statuses:

```
open        → newly created with no active turn
queued      → originator message persisted, run_turn submitted
running     → run_turn picked up; executing the backend
idle        → turn completed cleanly; ready for next send
interrupted → backend reported interrupted=True (watch-phrase trip,
              timeout, or future agent_interrupt); ready for next send
error       → backend execution failed; thread is sticky-failed
              (but may still be re-sent — see below)
closed      → terminal; sends rejected
```

Allowed transitions (informally):

- `open → queued` on first send.
- `queued → running` when the runner picks up the action.
- `running → idle` on backend success.
- `running → interrupted` when the backend reports
  `ExecutionResult(interrupted=True)`.
- `running → error` on backend or pipeline failure.
- `idle | interrupted → queued` on next send (interrupted is fully
  re-sendable; the partial response stays as a `system`/`status`
  message in the thread but the operator may follow up).
- `open | idle | interrupted | error → closed` via `agent_close`.

`queued` and `running` reject `agent_send`
(`agent_thread_busy`) and `agent_close` (`agent_thread_running`)
because the originator-driven `agent_interrupt` HTTP route is
deferred. Once `agent_interrupt` ships, it will add
`running → interrupted` explicitly — backend-side interruption
already populates the `interrupted` status today.

## `plugin::agent_messaging_plugin::run_turn`

| Field | Value |
|---|---|
| process_key | `plugin::agent_messaging_plugin::run_turn` |
| category | EDGE |
| context_handling | SESSION_AWARE |
| result_processor_kind | BRIDGE_DELIVERY |
| error_processor_kind | BRIDGE_DELIVERY |

Required arguments:

```json
{
  "thread_id": "agt-…",
  "message_id": "agm-…"
}
```

Required `flow.trigger_data` (set by the dispatcher; all keys are
mandatory under the bridge-delivery contract):

```json
{
  "source_namespace": "agent_messaging_plugin",
  "source": "bridge",
  "originator_type": "mcp_bridge",
  "originator_bridge_id": "agc-…",
  "bridge_id": "agc-…",
  "session_id": "session-…",
  "thread_id": "agt-…",
  "message_id": "agm-…",
  "bridge_plugin_namespace": "agent_messaging_plugin",
  "deliver_result_process_key": "plugin::agent_messaging_plugin::deliver_result",
  "deliver_error_process_key": "plugin::agent_messaging_plugin::deliver_error"
}
```

After consolidation there is exactly one `deliver_result` and one
`deliver_error` — both `plugin::agent_messaging_plugin::*`. The old
`plugin::agent_channel_plugin::*` and
`plugin::claude_code_channel_plugin::*` delivery keys no longer
exist.

## Behavior

1. Mark thread `running`; capture `active_action_id`.
2. Assemble prompt (bounded recent transcript — ≤ 20 messages,
   ≤ 24,000 chars — plus current request).
3. Resolve backend via the `BackendRouter`; call
   `execute_agent(ExecutionParams(prompt, working_directory,
   timeout_seconds))`.
4. Persist an agent / result message; transition status to `idle`;
   clear active action.
5. Return a structured payload (the same shape on success, error,
   and interruption — discriminated by `status`):

```json
{
  "thread_id": "agt-…",
  "request_message_id": "agm-…",
  "response_message_id": "agm-…",
  "status": "idle",
  "backend": "codex",
  "backend_session_id": "…",
  "text": "…",
  "interrupted": false,
  "interrupted_on": null,
  "artifacts": [],
  "metrics": {},
  "error": null
}
```

The bridge-delivery dispatcher submits
`plugin::agent_messaging_plugin::deliver_result` against the
originating bridge with this payload — for **all** outcomes
(success, interruption, error). The platform's
`bridge_delivery_error` builder
(`ananta/src/ananta/core/actions/result_processing_glue.py`) only
emits a fixed key set (`error_message`, `process_key`, `action_id`,
`failed_arguments`, `canonical_schema`); routing turn failures
through the success channel keeps the consumer's parse path uniform
and preserves the structured fields (`thread_id`,
`request_message_id`, stable `error.code`, persisted thread state).

`deliver_error` is reserved for **bridge/platform contract
failures** — missing `trigger_data` keys, the originator session id
not matching the action session id, etc. Those route through the
existing `inference_service::process_error` escape valve so the
model can react to the platform-level fault.

## Failure mode

On failure: persist a `system` / `error` message, transition status
to `error`, then return a structured payload with `status="error"`
and `error: {code, message}`. Stable `code` values include
`agent_thread_not_found`, `agent_thread_run_guard`,
`agent_thread_transition_failed`, `agent_message_not_found`,
`agent_execution_failed`, and the catch-all
`agent_messaging_error`.

The runner does NOT call `resume()` in the first slice — registered
`GuardedAgentInterface` backends report `supports_resumption=False`.
The stored `backend_session_id` is informational until a backend
advertises resumption support.

## Exposure

`plugin::agent_messaging_plugin::run_turn` is NOT denied from direct
`process_call` — the export-deny list (including the enumerated
agent_messaging "private surface": `open_thread`/`send_peer_message`/
`deliver_result`/`deliver_error`/`run_turn`) was emptied by operator ruling
2026-07-15: on this single-user substrate the deny layer was friction, not
security (see
`workbench/2026-07-15_result_error_processing_architecture_deep_dive.md`).
The normal entry path is still HTTP `agent_thread_open`/`agent_send`, after
which the platform submits `run_turn` itself — a caller invoking `run_turn`
directly is unusual but no longer blocked.

## Not authored by the model

`run_turn` is internal infrastructure. The thinking model never
proposes this process key — it is dispatched by
`AgentMessagingService._dispatch_turn` after an
`agent_thread_open` or `agent_send` request lands.

## Service-binding skip-rule

`agent_messaging_plugin` is intentionally **not** registered in
`config/service_bindings.json`. Bound ServiceProviders are skipped
from the `plugin::*::*` registry namespace
(`process_registry/builder.py::_should_skip_plugin`), which would
hide `plugin::agent_messaging_plugin::run_turn` from
`submit_action_definition` and break every turn dispatch.

The plugin implements `AgentMessagingServiceInterface` (an ABC, used
as a type, not a service-binding key) and delegates each public
method to a lazily-constructed `AgentMessagingService`.

## Deferred

- `agent_interrupt` — needs `GuardedAgentInterface` to surface the
  backend session id mid-execution (start callback or
  caller-supplied id on `ExecutionParams`). Until that lands,
  `close_thread` on a queued/running thread returns 409
  `agent_thread_running`.
- backend `resume()` support — needs `supports_resumption=True` on
  at least one backend.
- artifact local-file exposure — first slice keeps artifacts as blob
  references only (`blob_id`, `filename`, `mime_type`, `size_bytes`).
