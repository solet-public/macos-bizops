# Inter-Agent Messaging

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:bridge, knowledge:tag:peer_messaging, knowledge:tag:delivery_contract

Article Tags: planning-stage:agent-to-agent-coordination, planning-stage:tool-discovery, evidence-category:peer-messaging-contracts, evidence-category:identity-model, domain:agent-messaging, domain:inter-agent-routing

Embedding Description: How live agent sessions talk through the solet — the three identity concepts, the default no-MCP `<name> watch` registered-presence receive pattern (register, claim role, drain inbox, stream events), the runner-specific Stop-hook contract (Claude can re-wake; stock Codex currently ships no Stop-hook delivery mechanism at all — async command hooks are unsupported on measured stock builds, so the binding was removed rather than left registered-but-dead — see codex-0147-async-hook-regression, 2026-08-13), the unconditional delivery contract (wake is a transport property, not a sender-declared marker — every peer_send is delivery-attempted against the resolved recipient's live binding), the delivery-outcome vocabulary (queued_for_replay / queued_notification / queued_wake / queued_watcher) and the optional "IMPORTANT" prose-emphasis convention that is stripped as cosmetic input hygiene and never branched on, what unconditional delivery means for a watcher-held recipient, drive-on-delivery for a MANAGED session (spawn_session-tracked fleet workers get an extra best-effort nudge through their driver channel alongside the ordinary delivery, including exact stable-session reconciliation for watch registrations), the report_by / sweep_overdue_sessions staleness contract that replaced the retired per-message escalation apparatus, peer_inbox catch-up semantics including how to read your own inbox on demand with NO MCP by calling the registered peer_inbox process with your own stable session id, the two independent inbox cursors and why role-section exhaustion is a null cursor rather than an ok status, whether the role you hold still has a live delivery route attached to its current holder, multi-instance addressing, and Claude Code native-channel wake routing.

## Purpose

How live MCP-connected agents (Claude Code sessions, Codex sessions,
future agents) talk to one another through the solet. Covers the three
identity concepts, the unconditional delivery contract (wake is a transport
property, resolved from the recipient's live binding, never a sender-declared
marker), delivery-outcome vocabulary, peer_inbox semantics, and how multiple
concurrent sessions of the same agent kind are addressed
unambiguously.

This article covers two surfaces:

- **`current_identity`** — native MCP identity/routing
  introspection for the current transport session.
- **`peer_*`** — peer messaging between live, MCP-connected agents.

Both live on the same `<server-name>` MCP server with the `mcp__<server-name>__*` tool
prefix. The platform-call surface (`process_*`, `download`) is
documented in `02_platform_call_surface.md`.

## Choose a cross-session channel

Use the solet-scoped peer bridge when a message must reach a durable role or
continue across runner sessions. Use a runner's native session messaging only
for temporary coordination inside an already-shared runner task or team.
These are separate channels with different address spaces and reconnect
behavior:

| Channel and use | Address | Delivery and reconnect |
| --- | --- | --- |
| **Solet peer bridge — durable role or cross-session handoff.** Preferred for fleet/task assignment and any work that must survive a recipient going offline. | Use `peer_send_by_name(name=..., content=...)` (CLI process key `plugin::agent_messaging_plugin::peer_send_by_name`). The role name resolves through this solet-scoped role-binding table at send time. For example: `solet-bridge call plugin::agent_messaging_plugin::peer_send_by_name '{"name":"Coordinator","content":"Please review the fix."}'`. | The message is persisted before delivery is attempted. A role holder without a live route gets `queued_for_replay`; it is available when a holder attaches again. A queued delivery does not prove that a turn started or that the message was read. The role can have a different holder after reconnect, so resolve the current holder when exact ownership matters. |
| **Solet peer bridge — one selected live instance.** Use when the task is specifically for one session rather than whoever holds a role. | Address with `peer_id` and `peer_agent_instance_id` (CLI process key `plugin::agent_messaging_plugin::send_peer_message`). `peer_list` exposes the instance id and session label, not the stable session id. For a reply, take `peer_agent_session_id` from the reply hint/message meta when present and pass both ids. | The exact `(peer_id, peer_agent_instance_id)` is tried first. Only when it is `peer_unreachable` does routing try the stable session id, and only if that binding's `agent_id` matches `peer_id`. Ordinary MCP instance ids identify one bridge; watcher ids remain stable across watcher reconnects, as described below. A queued result does not prove reading or model activity. |
| **Runner-native session message — temporary coordination.** Use only when both sessions are already in the same active runner-provided task, team, or conversation and no durable role handoff is needed. | Address with the recipient handle supplied by that native runner context (such as its member name or id). That handle belongs to that runner context; it is not a solet role name and is not portable to another solet or runner. | Follow the native channel's own delivery and acknowledgment contract. This route does not create a solet peer-inbox message or role replay. If the runner task or conversation ends or is recreated, resolve recipients in the new context; use the peer bridge for messages that must survive that boundary. |

The durable-role row is solet-scoped: if another solet also has a
`Coordinator` role, call that solet's own peer bridge. Do not route a durable
role through a machine-global session label or a runner-native recipient
handle. Those names can collide and do not select a solet role binding.

Here, “runner-native session message” means the runner's own temporary
session-to-session feature. It is different from the **Claude Code native wake
adapter** below: that adapter is a delivery path for an already-persisted
solet peer-bridge message, not a second message-addressing channel.

## Managed-dispatch uptake measurement

`plugin::agent_messaging_plugin::managed_dispatch_uptake_rate` is a stateless,
read-only query over existing dispatch contracts and append-only event receipts.
Its uptake proxy is an accepted, assignment-bound, worker-authored ACK for the
same dispatch and initial attempt. Submission, bridge delivery, heartbeat,
inbox selection and current active state do not prove uptake. Receipt times
are observation times, **not first model token timing**.

```bash
solet-bridge call plugin::agent_messaging_plugin::managed_dispatch_uptake_rate '{"since":"2026-10-18T00:00:00Z","as_of":"2026-10-18T04:00:00Z","window_seconds":300,"sample_size":50,"runtime":"codex","transport":"tmux"}'
```

`since` is an inclusive aware ISO timestamp for contract creation; `as_of` is
an inclusive aware receipt cutoff and defaults to the invocation time.
`window_seconds` defaults to 300 (range 1–86400); `sample_size` defaults to 50
(range 1–500). Optional `runtime` and `transport` filters use the accepted
`first-turn:1` payload's `agent_runtime` and `host`, respectively. Here
transport means the recipient host transport (for example `tmux` or `headless`),
not the bridge configuration's `mcp`/`cli` transport. Vendor is explicitly
mapped from that original runtime product (`codex` → `openai`,
`claude_code` → `anthropic`); an unproved runtime/vendor stays `unknown`.
Current replacement-session labels are never used as historical evidence.

The query selects the earliest consecutive eligible contracts by
`(created_at,id)` within `[since,as_of]` before inspecting ACK outcomes. Known
runtime/transport mismatches are returned in `exclusions`; unknown eligibility
stays visible in the selected denominator. Failed starts, failed submissions
and missing records cannot disappear through success filtering. Reads use
at most 100 rows per page and 100 pages per inventory or dispatch event scan;
an interrupted, malformed or budget-limited read sets `reads_complete=false`
and cannot support a qualified SLO. A budget-limited inventory remains partial; it does not authorize changing
the intended denominator or claiming that the unread rows are absent.

Per-dispatch output includes contract and source row/event IDs, initial
submission receipt, label provenance, `submitted_at`, `delivered_at`,
`first_uptake_event_at`, `uptake_event_id`, latency, conversion, maturity and
reasons/advisories. `delivered_at` is null for a failed submission. The earliest
valid ACK after initial submission wins once; rejected, wrong-attempt,
wrong-dispatch and non-worker receipts cannot convert. Later retries remain
in the evidence but cannot repair or double-count the initial outcome.
An ACK after `as_of` cannot enter that observation. A contract is mature only
at initial submission time plus the full window; missing initial timing is
unknown, including a failed start with no submission receipt.

`conversion_rate` is timely successes divided by **all selected contracts**;
the response also gives pending, failed and unknown counts and breakdowns by
transport/runtime/vendor. Pending counts include timely ACKs whose observation
window is still open. Only an explicitly filtered tmux Codex cohort with
`sample_size=50`, `window_seconds=300`, 50 mature measurable members and
complete reads can produce `PASS` (at least 48 timely ACKs) or `FAIL` (47 or
fewer). Incomplete, pending or unknown cohorts return advisory `PARTIAL` or
`UNMEASURABLE`; they never block dispatch or unrelated work. Invalid caller
syntax remains an ordinary input error.

Retain the echoed input cutoff, selected contract IDs and raw receipt IDs for
replay. They identify read evidence, **not an atomic database snapshot**:
backdated or newly available receipts may change a later rerun, even with the
same cutoff. No source history is corrected or mutated; create, update, clear,
retire, close and reopen do not apply to this derived query.

Synthetic tests verify implementation only. The operating seat separately
qualifies the next 50 consecutive tmux Codex dispatches after landing and
deployment, freezes cutoffs/IDs, observes each for 300 seconds and requires at
least 48 timely initial-attempt ACKs. Insufficient live evidence remains
`LIVE-SLO-PARTIAL`; it does not close the Feature. This bridge query does
not implement the historical request for a Project Solet `psolet` CLI verb.

## Identity model — three distinct concepts

Inter-agent addressing keeps three concepts separate:

```
agent_id              "claude_code", "codex", ...     stable kind
agent_instance_id     "agi-<uuid>"                    live binding key
agent_session_id      "ases-<uuid>"                   stable logical session key
session_label         "codex on baroque-suite"        mutable human metadata
```

- **`agent_id`** is the stable kind. Many sessions can share one.
- **`agent_instance_id`** addresses one live peer binding. For an ordinary
  MCP bridge it is an instance id for that bridge; a newly created bridge can
  have a different id. Watch registrations keep their identity across
  reconnects: a managed watcher uses its launcher-provided ledger
  `AGENT_INSTANCE_ID`, while a bare manual watcher derives a deterministic
  `agi-watch-<digest>` id from its stable `AGENT_SESSION_ID`. The paired wake
  path uses the same managed-id-first rule. Use this key for exact-instance
  sends; never use `parent_pid` or `session_label` as a routing key.
- **`agent_session_id`** identifies a logical session across its bridge
  reconnects. A reply hint/message meta may provide it as the fallback key for
  `peer_send`. The exact instance id is still tried first, and a session
  fallback is accepted only for the requested `agent_id`. `peer_list` does
  not expose this routing key.
- **`session_label`** is mutable, non-unique human metadata. Useful
  when listing peers so a person can pick the right instance. Never
  used as a routing key.

A fourth value, `parent_pid`, exists as live-routing metadata only —
it identifies which OS process owns the bridge for native wake
routing (today, the Claude Code MCP host). It is never persisted as
identity and never used to address peer sends.

## No-MCP terminal receive pattern — `<name> watch` (the default)

The DEFAULT receive path for terminal coding-agent sessions (Claude Code and
similar) is the local CLI's registered-presence watcher — zero MCP:

```bash
<name> watch                 # role from $AGENT_SESSION_LABEL
<name> watch --role <Role>   # explicit role
```

Run as a persistent background process per session, it performs the whole
receive contract in one command:

1. **Register** — opens a bridge and POSTs `peer/register` with the stable
   logical session id from the launcher environment and a watcher id:
   `agent_session_id` = `$AGENT_SESSION_ID` (the per-logical-
   session carrier; watch fails loud when it is absent rather than register
   a degraded binding), `session_label` = the role, and
   `agent_instance_id` = the managed ledger `AGENT_INSTANCE_ID` when set, or
   otherwise the deterministic `agi-watch-<sha256(session_id) prefix>` id for
   a bare manual watch. Reconnect reuses that same watcher id, replacing its
   prior binding instead of minting a sibling; wake uses the same precedence.
2. **Claim** — dispatches `plugin::agent_messaging_plugin::peer_claim_role`
   over that same registered bridge (REL-07: the server sources the binding's
   session id from the caller's live peer binding, never from claim args) and
   fails loud on a terminal rejection.
3. **Drain** — reads `peer_inbox(include_important=true)` once, emitting
   messages that arrived while unwatched.
4. **Stream** — long-polls `/events` from cursor `-1`, one JSON line per
   delivery, nothing while idle, and re-asserts `peer/register` on a ~60s
   heartbeat: the binding can be dropped server-side while the bridge stays
   healthy (empty 200s, no error signal), and the idempotent re-register
   bounds that silent-delivery outage to one interval. On bridge rotation
   (blue-green swap 404) or idle-reap it silently reconnects and re-runs 1–4.

Every emitted line is a JSON object tagged `"watch": "armed" | "inbox" |
"event"`; the armed line carries the claim result and the
`agent_instance_id`. Ground truth for "is my role claimed" is
`peer_holds_role` with the role name AND that `agent_instance_id` — an entry
in raw peer-list output is connection presence, not a claim. That verb also
returns `delivery_route_attached`: whether the role's CURRENT holder still has
an open bridge bound. A role binding is durable and outlives the session that
claimed it, so `holds` and `delivery_route_attached` answer different
questions and `holds=true` alongside `delivery_route_attached=false` is the
normal reading of a claim whose receiver is gone. It reports that a route is
bound, not that a wake will fire. The optional MCP
bridge below is an alternative holder of the same registered presence for
operators who explicitly chose MCP; it is required for nothing.

## Claude desktop app and hand-started Claude Code sessions

The Claude desktop app is a supported client, as is a bare `claude` started
in a terminal without the `claude-<name>` launcher. Such a session has no
launcher environment, but Claude Code itself exports `CLAUDE_CODE_SESSION_ID`
to every child it starts (hooks, the Bash tool, MCP servers), and a resume
keeps that id. The session's MCP bridge, or its `<name>` CLI when no MCP server
is attached, creates one small identity record on first use:

```text
<runtime dir>/claude-session-identity/<CLAUDE_CODE_SESSION_ID>.json
<runtime dir> = $XDG_RUNTIME_DIR/ananta, else ~/.ananta/runtime
```

The record holds `agent_instance_id` (`agi-` + 32 hex characters derived from the
Claude session id), `agent_session_id` (`ases-claude-<id>`) and
`session_label` (`claude-<first 8 characters>`). The MCP bridge, every
`<name>` CLI verb (`send-by-name` attribution, `inbox`, `watch`) and the
coordination hooks read the same record. The hooks only read it, and before it
exists they derive the same three values from the session id, so the session
is the same peer with the MCP server attached or removed. Resuming the session reuses the record; a
new session gets a new one. Editing `session_label` in the record corrects the
label; deleting the record retires it and the next start mints the same ids
again.

Environment wins. When a launcher exported any of `AGENT_INSTANCE_ID`,
`AGENT_SESSION_ID` or `AGENT_SESSION_LABEL`, the record is neither read nor
written. The hooks also need the clone's coordination receipt at
`<clone>/profile/data/coordination-hooks/claude/installation.v1.json`, found
from `CLAUDE_PROJECT_DIR`, so open the desktop session in the solet clone.

The SessionStart context names the identity and the claim path in one line.
A recorded label is display metadata and never claims a role. To claim one
from the CLI alone, keep this running in the background:

```bash
<name> watch --role <Role>
```

Plain `<name> watch` registers presence under the recorded label without a
claim. The heartbeat hook stays silent for these sessions, as for any session
without a fleet ledger row (`report_alive` answers `session_not_found`).
Liveness is the peer registration held by the MCP bridge or by `watch`.

## Attended session without a Claude session id — `<name> attend`

Use `<name> attend` for a terminal the operator attends directly that has no
launcher env (no `AGENT_SESSION_ID`, `AGENT_SESSION_LABEL` or
`AGENT_INSTANCE_ID`) and is not a Claude Code session (no
`CLAUDE_CODE_SESSION_ID`, so `<name> watch` refuses), and that may have no
MCP configuration. It joins that session to fleet identity using only a
credential the bridge already authenticates: an operator-registered OAuth
client.

Enablement is an operator step. The operator mints a `client_credentials`
client with `service_interface::vault_service::oauth_client_register`
(operator principal only; interface in
`ananta/src/ananta/interfaces/vault_service_interface.py`), keeps the
returned secret in the keychain, and ends it with `oauth_client_revoke`.

```bash
<name> attend --client-id <client id> --client-secret-file <file holding its secret>
```

The command takes a bearer from the bridge's `/oauth/token`, opens the
bearer-verified Streamable HTTP session, and reads the identity back with
`current_identity`. That identity is sealed by the server from the verified
credential: `agent_instance_id` is `agi-oauth-<client_id>`, `agent_id` is the
constant `claude_phone`, and `session_label` is the client's name. No flag,
environment variable, PID or pane supplies identity, and anyone holding the
client's secret IS that identity, so run one `attend` per client.

It prints one JSON line tagged `"attend": "armed"` (the sealed identity),
one `"attend": "event"` line per delivery, and on every stream end it DELETEs
the session and re-arms with a fresh token. While re-arming, an unavailable
bridge (HTTP 5xx or 429, a transport error, no port file — the normal shape of
a deploy) is not a refusal: it prints the retry advisory, a line tagged
`"attend": "retry"` with the reason, and tries again. On interrupt or SIGTERM
it DELETEs its session and exits 0.

Not yet provided: no role claim, no catch-up drain of messages that arrived
while unattended, and no wake pairing. Its refusals:

- `attend refused: the bridge did not issue a token for client <id> (...)` —
  the first arm got no token (unknown, unapproved or revoked client, or a
  client without the `client_credentials` grant).
- `attend refused: the bridge did not authenticate this identity
  (identity_trust=...)` — the bridge runs without bearer verification
  (`streamable_no_auth`), so the session has no authenticated identity.
- `attend: the bridge no longer accepts client <id> (<status>)` — on re-arm
  the token endpoint answered 400, 401 or 403 with an OAuth error code; the
  client was revoked or its grant withdrawn.

## Streamable HTTP peer receive pattern

The Streamable HTTP MCP transport exposes `peer_register`,
`peer_list`, `peer_send`, and `peer_inbox` as **native MCP tools** in
`mcp_streamable/tools.py` and `mcp_streamable/dispatch.py`. They are
not process-registry entries, so `process_search("peer_register")` is
not expected to find them. Use `tools/list` on the streamable MCP
session to discover them.

Use `current_identity` / `whoami` for identity introspection. It
returns transport, solet name, `agent_id`, `agent_instance_id`,
`agent_session_id`, `session_label`, bridge id, MCP session id,
`roles_held`, and an identity-trust field without secrets. The
`roles_held` value is a reverse lookup over `agent_role_binding`
rows whose `agent_instance_id` equals the current instance; it is not
derived from `session_label`. `peer_register` is for registration or
relabeling, not for asking "who am I?"

At `initialize` time the streamable session allocates a synthetic
bridge and registers a peer binding from the bearer-token claim. The
common receive pattern is:

1. Call `peer_register(agent_id=..., session_label=...)` to set the
   public peer kind/label for this streamable session. The returned
   `agent_instance_id` is the durable instance routing key for this
   session.
2. If the session should be reachable by a stable role name, call
   `plugin::agent_messaging_plugin::peer_claim_role` with the
   `agent_id` and `agent_instance_id` returned by `peer_register`.
   Role claiming is a process-registry operation because the durable
   role table lives in platform state.
3. Poll `peer_inbox(...)` for catch-up and
   receive — page the durable catch-up view to `instance_exhausted=true`
   for instance mail (A4, 2026-08-04: `include_important` is retired from
   this tool's schema). `peer_inbox` reads the same durable thread store
   used by
   stdio bridge peers, keyed by identity and role binding. This is a
   poll-based receive path; no native wake is implied for generic
   streamable clients.

For the local stdio bridge, an explicitly configured
`AGENT_SESSION_LABEL` is also claimed as the standing role after
registration and every reconnect. `AGENT_ROLE` overrides that
default when the display label and routable role differ. Inferred cwd labels
are display-only and do not claim roles. Registration/relabeling and role
claiming remain distinct server operations; the bridge performs both so a
session cannot appear correctly labeled while remaining unroutable.

Security note for `streamable_no_auth=True`: the permissive verifier
uses the synthetic `agent_id` / `agent_instance_id` sentinel
`tunnel_passthrough`. That mode is only appropriate behind an outer
trust boundary such as the OpenAI tunnel runtime key and local network
controls. It is not a per-user identity assertion, and it does not add
a wake channel; callers still receive by polling `peer_inbox`.

Invariant — bearer *enforcement* and the OAuth *login* surface are
orthogonal. `streamable_no_auth` selects the bearer verifier only
(permissive vs. real `BearerVerifier`). Which OAuth login surface mounts
is decided *independently* by whether a stable issuer is pinned: static
surface when `oauth_enabled` + `oauth_issuer_url` are set (cloud ALB),
otherwise the origin-following **dynamic** surface (local tunnel,
ephemeral loopback origin). An OAuth login surface is therefore *always*
mounted — `/authorize` + `/oauth/token` + discovery — regardless of
`streamable_no_auth`. Do NOT re-gate the dynamic surface on
`streamable_no_auth`: doing so is what stranded the ChatGPT connector at
a 404 on the enforcement cutover (bearer required, but no endpoint to
obtain a bearer). Under enforcement the dynamic surface mints tokens the
real verifier accepts — same HMAC key, `client_id` + `exp` present; the
`aud` check is disabled for the origin-following resource (redundant for
a single-key HMAC server). See `_mount_oauth_routers` in `plugin.py`.

## Delivery contract — wake is a transport property

Every `peer_send` / `peer_send_by_name` / role send is unconditionally
delivery-attempted against the resolved recipient's live binding. There is no
sender-declared switch between a silent mode and a notifying mode — the
platform decides how the message surfaces, based entirely on what the
recipient's binding actually is at send time (A4, 2026-08-04; retired the
prior opt-in "IMPORTANT" marker gate). A leading `IMPORTANT` (followed by
`:` or whitespace) is still recognized and stripped from the delivered prose
as a purely cosmetic, optional emphasis convention for the reader — it is
never branched on, so writing it or omitting it produces identical delivery
behavior:

```
peer_send (any prose)
  -> IMPORTANT-prefix, if present, stripped as cosmetic input hygiene only
  -> delivery is unconditionally attempted against the resolved recipient's
     live binding
  -> delivery = "queued_notification" for a normal bridge channel event
             or "queued_wake" when peer_registry used a registered native
             wake adapter, currently Claude Code
             or "queued_watcher" when the resolved recipient is held by a
             no-MCP `<name> watch` subprocess (either transport)
             or "queued_for_replay" (role sends only) when the role's
             current holder has no live, polling binding — the envelope is
             durable and replays on that holder's next attach
```

A stale-but-present instance binding (bridge row exists, but
`last_seen_at` is past `binding_liveness_window_s` — e.g. a SIGKILLed
process with no client-initiated `/close`) fails the send loud with
`PeerUnreachableError` rather than reporting a `queued_*` outcome that will
never be drained; the message is still persisted and readable from the
recipient's inbox. `binding_is_live` applies this check uniformly across
every recipient kind, not just watcher-held ones (A4 Amendment 5).

A `queued_*` delivery means the platform placed the event on the recipient's
live bridge queue; emission to the client happens at the forwarder's next drain,
and whether it becomes a turn is client-side and is NOT confirmed by this field
(REL-06 honest-return relabel — the platform can only prove "queued", so it says
"queued"). Transport delivery itself does not prove that a `queued_*`
delivery entered a turn. The platform's staleness signal is the recipient's
own `report_by` promise:
`sweep_overdue_sessions` marks a managed session overdue when it misses that
deadline, and `_notify_steward_of_overdue` surfaces the miss to its steward.
Managed Codex recipients additionally have the bounded doorbell/pickup evidence
described below. Other delivery adapters do not acquire that evidence merely
from a successful transport return. Never resend a stored message to compensate
for an unproved wake.

### Current overdue-notice routing (iss_a07c576f-0820-4d53-a3bb-01780bc23e18)

`report_by` remains a produced-work promise: a heartbeat never re-arms it.
The overdue-notice consumer uses the server's `last_heartbeat_at` only for
routing. A stamp no older than the shipped heartbeat hook's 180-second cadence
emits a visible, low-priority `session_overdue_quiet_notice` and never uses the
direct managed-driver wake. A stale or absent heartbeat retains the existing
`session_overdue_notice` alarm and wake. In both cases the row remains
`overdue`; this routing is tracked by iss_a07c576f-0820-4d53-a3bb-01780bc23e18
and does not change `report_by` state.

The role-addressed durability guarantee is unaffected by any of this: an
unreachable role holder gets `queued_for_replay`, and the envelope replays
through the surviving `list_undelivered_for` / `mark_delivered_for_instance`
path the next time that role's holder attaches — this is the one piece of
the old insurance apparatus that was deliberately kept, byte-for-byte,
because it backs a real guarantee (role backlog is never silently dropped),
not a per-message heuristic.

### What unconditional delivery means for a watcher-held recipient

An MCP-free fleet session receives through a `<name> watch` subprocess. The
watcher itself is a PULL consumer: the queued event streams into the watch
task's output and is also available through the durable inbox. The host then
determines what a Stop hook may do. Claude's re-wake hook can convert the spool
signal into a turn. Codex currently ships no Stop-hook delivery mechanism at
all: stock Codex does not execute async command hooks on any measured build
(0.141.0, 0.147.0), so the plugin's prior `async: true` binding never fired
and was removed rather than left registered-but-dead
(codex-0147-async-hook-regression, 2026-08-13) — do not assume a synchronous
fallback exists either; that design was retired for blocking the turn
boundary. `queued_watcher` therefore names transport delivery, never a
guaranteed live interrupt. Expectations to plan around:

- The message IS delivered (durable + streamed into the watch output within
  one long-poll cycle). It is not lost, and no resend is needed.
- An idle Claude recipient with the re-wake hook may start a turn on delivery.
  An unmanaged Codex TUI has no automatic delivery notice at all right now; it
  must poll `peer_inbox` itself, and gets a fresh unread-coordination reminder
  only at its next `SessionStart` (startup/resume/`clear`), not per turn. A
  managed Codex worker is unaffected — it receives an independent
  driver-channel notice from `drive_on_delivery`, which can start its next
  turn regardless of this plugin's hooks. If action is required now and the
  recipient is not managed, reach the operator or drive the session through an
  approved host channel.
- Consumption is the watcher's events-ack, not model activity: when the
  watcher's long-poll cursor moves past the delivered event (it printed the
  line), the platform stamps the message consumed. There is no per-message
  escalation apparatus watching for a missed consumption anymore (A4 retired
  it); staleness is caught at the session level instead — `report_by` /
  `sweep_overdue_sessions` flags a session that has gone quiet against its
  own promised deadline, independent of any one message.
- The watch client's arm-time catch-up drain (`peer_inbox`) surfaces
  anything that arrived while unwatched the same way live delivery does.

When you receive a peer_message notification, reply with substance, or
stay silent if a reply adds no value. **Do not reply with
acknowledgements**, "got it", or "thanks" — those create loops. The
persisted thread captures the conversation regardless of whether
anyone replies.

### What unconditional delivery means for a MANAGED session (drive-on-delivery)

A MANAGED session — one spawned through `spawn_session` and tracked in the
`managed_session` ledger (fleet workers: `headless`/`tmux`-hosted) — gets one
more best-effort layer on top of everything above, added 2026-08-04. After
the ordinary delivery attempt (`queued_wake` / `queued_notification` /
`queued_watcher` / `queued_for_replay`, unchanged), `dispatch_peer_send` /
`dispatch_role_send` also resolve the recipient's `managed_session` row and,
when it is `live`/`idle`/`overdue` with a live driver channel, inject a
short fixed notice ("delivery waiting … — drain peer_inbox") straight into
the session's driver channel — `session_lifecycle_verbs.drive_on_delivery`.
This is a waker, never a second copy of the message: the durable thread
entry stays the single source of truth, and the drive never touches
`report_by` (an inbound nudge extending a worker's report-or-die deadline
would let a silent worker blind the overdue sweep). The same helper reroutes
the platform sweep's own dependency-wake and overdue-steward-notice
deliveries (`session_sweep.py`), so an armed `session_dependency` edge
firing on a managed waiter, or an overdue worker's managed steward, get the
same nudge.

**Scope, stated honestly:** this covers managed sessions only. A
non-managed session — an ordinary operator-launched one (the seat,
Git-Controller, any session never spawned through `spawn_session`) — has no
`managed_session` row, so `drive_on_delivery` no-ops silently for it every
time. Everything in the "watcher-held recipient" section above is
completely unaffected: an operator-hosted recipient keeps exactly the same
no-waker property it always had. Do not assume a `queued_wake` to a
non-managed session gets any extra push — it does not, by design.

## peer_inbox semantics

```
mcp__<server-name>__peer_inbox(after="<recent ISO time>")                  # default: catch-up mode
```

- Returns one bounded page of the durable catch-up view (A4, 2026-08-04: the old silent/IMPORTANT split
  at send time is retired, so there is only one view left, and it is the
  only one this tool now offers). Use a recent `after` timestamp during
  incident polling; otherwise old history can flood the context window.
- Each entry exposes `sender_agent_id`, `sender_agent_instance_id`,
  and `sender_session_label`. Use the `sender_agent_instance_id` as
  the reply's `peer_agent_instance_id` to target the original sender
  (not just any session of that agent kind).

### Reading the inbox without MCP

The same read is a registered platform process, so a session with no MCP
tools — and no live `watch` stream — can still pull its own mail:

```
<name> call plugin::agent_messaging_plugin::peer_inbox \
  '{"agent_session_id": "'"$AGENT_SESSION_ID"'"}'
```

The MCP tool infers the reader from the calling bridge's registration. A
`<name> call` opens a fresh, unregistered bridge, so the process takes the
reader's own stable `agent_session_id` instead and resolves the rest of the
identity from that session's live binding. The launcher exports the value as
`AGENT_SESSION_ID`; `peer_register`, `current_identity`, and the watcher's
armed line all echo it. An id with no live binding is an error, never an
empty page — "the reader is unknown" and "the reader has no mail" are
different answers and the verb keeps them different.

### The role section is paged separately

The response carries two sections with two independent cursors, and mixing
them is the mistake this section exists to prevent:

- `entries` + `next_after_created_at` + `next_since_created_at` +
  `next_since_cursor` + `instance_exhausted` — messages addressed to this
  instance. The backward timestamp cursor and forward opaque cursor run in
  opposite directions and cannot be combined. With neither, the page is the newest messages,
  newest-first. `after` walks backward: messages older than the timestamp,
  newest-first; echo `next_after_created_at`. `since` starts a forward read:
  messages strictly newer than the timestamp, oldest-first; echo the opaque
  `next_since_cursor` as `since_cursor` on later pages. That token preserves
  the original horizon plus the last `(created_at, id)` key, so a page boundary
  inside equal timestamps is lossless. `next_since_created_at` remains a
  timestamp observation for compatibility, not a continuation. To ask "what
  is new since T", pass `since`, never `after`. Stop only when
  `instance_exhausted` is true.
- `role_entries` + `next_role_cursor` — messages addressed to any role this
  session holds, merged across roles, paged by `role_after` (an opaque token).

`since` is a strict `created_at > since` horizon on BOTH sections: the instance
section reads forward from it, and the role section returns only rows newer than it
(still newest-first, still paged by `role_after`); `role_since_created_at` echoes the
horizon the role section applied, and null means it did not (none was sent, the server
predates the fix, or `role_section_status` is `"error"`). `after` and `role_after` are
both backward cursors (rows older than the position, newest-first); `after` pages only
the instance section and `role_after` only the role section. `role_section_status` is a
fault-domain flag, not a progress flag: `"ok"` means that section was computed
without error and says nothing about whether it is drained. **The role section
is exhausted only when `next_role_cursor` is null**; `"error"` means it failed
(detail in `role_section_error`) while the instance section was still served.

`role_read_coverage` says which roles the role section read and what it hid, so an
empty or short `role_entries` can be diagnosed from the page (iss_ab82bd8a; the
iss_14bedd40 case is a notice that fires while `role_entries` is empty and
`role_floor_applied` is true). It holds one entry per role read:
`{role, covered_mark_present, floor_hidden, receipt_hidden}`. A held role absent from
the list was not read. `floor_hidden` counts rows the covered-mark floor removed (rows at
or below the mark a `peer_mark_role_covered` attested) and `receipt_hidden` counts rows a
display receipt removed; a row a receipt hid is counted only as `receipt_hidden`.
Both are counts, never hidden content, and both are taken inside the window the call
already scanned (at most `limit + 1` rows per role plus the bounded receipt refill), so
a nonzero value is exact and a count is a lower bound on the role's whole hidden
history: no extra history scan is made to produce it. `covered_mark_present` is false
under `include_covered` and on a history read, which read no mark, and for a role with
no mark yet. The list is empty when the session holds no role or the role section
failed (`role_section_status` is `"error"`). The process, the `/peer/inbox` and
`/peer/inbox_for_session` routes and the MCP tool all return it.

`solet-bridge inbox --since T` sends `T` on its role-section pages and applies it to the
direct section as the client's own stop, then reports `since_applied`:
`{"direct": <stop mark>, "role": <the server's role_since_created_at echo>}`, null when
`--since` was not given. A role page whose `role_since_created_at` is null (an older
server that ignored `since`) or names another instant fails the command, non-zero with
`complete: false` and `role_section_error` beginning `role_since_not_applied`, instead of
printing an unfiltered set as bounded. The payload's
`role_read_coverage` is the first role page's coverage.

Every page also carries `read_coverage.direct` and `read_coverage.role`.
Each immutable statement describes **returned rows only**: `first` and `last`
are `{created_at, id}` keys in emitted order, `count` is the exact returned
count, and an empty successful section has zero count and null endpoints.
It retains `requested_since`, `effective_since` (the exclusive horizon restored
from a continuation), `after`, `since_cursor`, `role_after`, and the decoded
direct `continuation_after`, including on empty pages. `direction`, `cursor_mode`,
`limit`, `next_cursor`, and `exhausted` describe that section's existing paging.
No query, cursor, receipt or covered-mark behavior changes.

A direct `since` page labels continuation safety `horizon_only` and advises
using `next_since_cursor`. Reusing `next_since_created_at` can skip rows sharing
its timestamp (twelve tied rows at limit five can lose seven). A direct
`since_cursor` page labels its continuation `tie_safe`; backward timestamp
paging labels it `timestamp_only` with a tie warning. The independent opaque
role cursor is `tie_safe`. These labels describe cursor mechanics, never global
delivery or uninterrupted coverage of an interval.

Compare the prior response's returned cursor/horizon with the next request's
echoed boundary. If A ends at T1 and B manually starts at a later T2, the
interval (T1,T2] is unverified: the server does not claim it queried or counted
any message there. Correct opaque continuation keeps the original horizon.
Endpoint arithmetic alone cannot establish continuity. `outside_query` and
`human_read` remain `unknown`, and `unread_count` is null in both sections:
instance messages are addressed history, while role observer reads bypass
display receipts and remain subject to existing covered-mark rules.
`observer`, `include_covered`, `include_important`, `role_floor_applied`,
`role_history_cursor`, and the nested `role_read_coverage` retain that scope.
A role fault has `status: error`, unknown count/exhaustion and no effective
horizon, distinct from successful empty coverage.

The merged `solet-bridge inbox` output preserves `read_coverage.direct_pages`
and `role_pages` in fetch order, including intermediate and empty pages. Only
the section actually consumed by each independent drain contributes evidence;
the other section fetched incidentally is excluded. `complete` retains its
meaning of drain exhaustion within requested scope, not global, human-read or
unread coverage. An older server missing section metadata produces an explicit
`status: unknown` advisory per consumed page, without invented endpoints or
counts and without blocking the read. A corrected read creates a new response;
these projections have no stored update, clear or unread lifecycle.

A successful page is not a drain; continue until `next_role_cursor` is null.
`role_limit` reports the effective limit of a successful role page;
`role_page_truncated` and `role_truncation_reason` disclose whether a
continuation was bounded by `row_limit` or `byte_ceiling`. When the reason is
`byte_ceiling`, `role_byte_ceiling` contains the applied ceiling value.

Page rather than widen. An entry carries the whole message — a 50-entry
instance section plus a 50-entry role section measured 422,513 characters on
2026-08-01, roughly 4 KB per entry — so `limit` bounds the count, not the
bytes. The process defaults to a small page for that reason; the cursors are
how a backlog gets drained.

Reading a role message here does not by itself mark it consumed for replay
purposes. The surviving role-replay durability guarantee
(`list_undelivered_for` / `mark_delivered_for_instance` /
`mark_role_consumed_on_ack`) is keyed on the watcher's or bridge's own
delivery acknowledgement, not on a `peer_inbox` read, so a role message
pulled this way may still be re-served through that path until its holder
acks it directly.

## Sending to a specific instance

When N instances of the same `agent_id` are registered:

```
mcp__<server-name>__peer_send(peer_id="codex")
  -> if exactly one codex instance: delivers to it
  -> if multiple: fails peer_ambiguous, lists candidates

mcp__<server-name>__peer_send(peer_id="codex", peer_agent_instance_id="agi-abc...")
  -> delivers to that specific instance
  -> fails peer_unreachable if no match
```

Discover candidate instances via `mcp__<server-name>__peer_list` (returns
`agent_instance_id` and `session_label` for each). For replies,
take the `sender_agent_instance_id` from the inbox entry or the
`peer_message` notification meta.

## Native-Channel Routing And Codex Peer Wake

There are two implemented wake paths. They both begin with the same
unconditional `peer_send` delivery attempt and both persist the message
before live delivery, but they differ in where the wake conversion happens.

### Claude Code registered adapter

When the recipient `agent_id` has a registered native wake adapter
(`claude_code` is self-registered during `start_interface`), every send to
it routes through that adapter — there is no marker gate to clear first.
Claude Code wakes into a real turn instead of waiting for the next user
input, and `peer_send` reports `delivery="queued_wake"` (the event is queued
on the recipient's live bridge — the platform cannot observe whether the
wake became a turn; REL-06). When the resolved recipient binding is a
`<name> watch` subprocess, the same queued event reports
`delivery="queued_watcher"` instead — the platform-side transport is pull,
and runner-specific Stop/managed-driver behavior follows the delivery contract
section above.

The pairing key is `parent_pid`: the bridge and the host MCP
session share the same OS parent process. The native wake adapter
finds the sibling bridge by parent_pid match and appends a
`post_message` event there. Receivers see an envelope:

```
[peer:codex "codex on task-X" instance=agi-abc...] <message>

(reply via mcp__<server-name>__peer_send with
 peer_id=codex, peer_agent_instance_id=agi-abc...)
```

### Stock Codex Delivery And Managed Auto-Drive

The locally patched Codex notification sink is retired. Stock Codex receives
through the watch/durable-inbox path. The coordination plugin currently ships
no `Stop` binding at all (codex-0147-async-hook-regression, 2026-08-13): stock
Codex does not execute async command hooks on any measured build, so a bound
one never fires, and a synchronous one would block the turn boundary — the
defect the async design was meant to fix. An unmanaged Codex session has no
automatic delivery notice; it drains `peer_inbox` on its own initiative and
gets the fixed unread-coordination reminder at `SessionStart` only.

Autonomous Codex sessions are launched through `spawn_session`. On dispatch,
`drive_on_delivery` first tries the recipient's current `agent_instance_id`.
A watch process intentionally registers under an `agi-watch-*` id, so a direct
lookup may miss the spawn-time managed row; the delivery path then resolves by
the exact, registration-backfilled `agent_session_id`. Exactly one match is
required. Zero matches preserve the unmanaged/operator no-op, and multiple
matches log and refuse to guess. Eligible `live`, `idle`, or `overdue` rows get
the fixed driver notice; parked, spawning, terminal, or unavailable-driver rows
do not.

## See also

- `02_platform_call_surface.md` — agent calling the solet
  (`process_*`, `download`).
- `05_http_reference.md` — the HTTP routes behind each MCP tool.
- The local patched-Codex runbook — mechanism and update procedure.
- `processes/` — per-process schemas for `post_message`,
  `deliver_result`, `deliver_error`, `start_interface`,
  `stop_interface`.
## Managed Claude and Codex eventual pickup

For managed Claude (`claude_code`) and Codex recipients, a stored peer message retains a durable
`delivery_pickup` record. The `delivery_pickup_event` table keeps append-only
causal history. Message bodies remain exclusively in the existing direct or
role inbox. Both schemas are declared through `get_schema_definitions` and
installed through the state-management service; no separate migration runs.

`driver_scheduled` proves executor admission only. Both direct and role sender
receipts expose `send_status=stored_wake_pending`: durable mail is stored, the
driver is merely scheduled, and wake confirmation and turn start remain
unconfirmed. Do not resend the stored message. `stored_no_wake` still describes
the existing watcher/replay cases where no driver was scheduled.

Pane submission is confirmed only by an empty composer (`driver_sent`) or a
new active turn (`driver_turn_observed`); neither proves inbox uptake. If the
owned doorbell remains after the two bounded Enter attempts,
`submitted_no_turn` becomes `driver_error` with submission explicitly
unconfirmed. A WARNING names the current recipient instance, session and role
and message reference; it includes `pending_retry_limit` when that failure
exhausts the existing cap. Mail stays pending, the failed attempt is counted,
and the existing sweep can retry without another paste. Unknown panes and
unproven or deferred composer shapes receive zero keys; an idle single-line
human draft is preserved exactly (see "Codex draft preservation" below). Only
exact inbox return establishes uptake.

A Claude recipient hosted in a tmux pane gets its driver notice only through an
empty composer. Before it pastes anything, the driver channel samples the pane
for up to 10 seconds (`DEFAULT_SEND_BOUNDARY_TIMEOUT_SECONDS`) until it observes
the empty composer row. If a human draft or an earlier queued notice still
occupies the composer for the whole bound, nothing is pasted and no key is sent,
one WARNING names the pane and the bound, and the channel raises
`DriverChannelSendError`. The delivery layer reports that as `driver_error`
(sender receipt `send_status=stored_no_wake`): the message is already stored,
do not resend it, and the recipient picks it up on its next inbox read. The
explicit verbs `clear_session`, `compact_session` and `drive_session` report the
same cause as `driver_delivery_failed`. Once a send does happen, the existing
verification and its no-re-send rule are unchanged.

The finished drive records
submission separately from a live busy-status observation (`driver_turn_observed`).
Neither proves an inbox read or task completion. An exact message id returned
by a non-observer recipient `peer_inbox` read records `inbox_returned`; this
proves server-side return, not display, execution, or completion. Both `entries`
and `role_entries` contribute exact returned ids, and a partial/error role
section never substitutes for a direct-section read. Continue both independent
cursors to finish the merged drain.

Claude retains the single original first doorbell: one native send of
`delivery waiting from <sender> — drain peer_inbox`, including the existing
parked-composer interrupt when applicable. Its pickup record is claimed with
a versioned CAS before that input, so duplicates, a restart, an explicit retry,
or a late executor result cannot repeat it. Every subsequent Claude retry,
replay and re-drive returns `driver_pending` with the reason
`durable mail retained; waiting for existing hook/next-turn inbox pickup`.
No such retry sends text, interrupts, clears, captures, or classifies a composer.
Unreachable role mail also retains pickup evidence. A waiting tick consumes
no key-attempt budget and identical ticks do not append duplicate history.

The existing Claude watch sidecar tees delivery to its spool. The shipped
Stop hook runs `wake_waiter.py` with `asyncRewake=true`, emits a fixed nudge,
and the model reads `peer_inbox` on its next turn. This is the normal safe
consumer path; an absent consumer or a session that never reaches a turn
boundary has no guaranteed autonomous uptake. Spool/bridge notification is
not terminal pickup evidence. The merged CLI's observer direct census does
not settle direct mail; supported settlement is the recipient's nonobserver
role-page read or explicit nonobserver direct API/process read.

The Claude no-key boundary protects input integrity. Completion requires an
exact returned id from the current recipient's nonobserver inbox read, supplied
by the existing tracking service. Current role resolution follows a successor
without rewriting the original route; the old holder, an observer, an empty
page, or an unrelated id cannot settle successor mail. Unknown telemetry never
rejects durable mail or a recipient read. Legacy automatic notices from the
overdue, dependency and rotation riders retain their durable notification
contract; their shared `drive_on_delivery` boundary avoids additional Claude
input while addressed pickup remains pending. The fixed first-doorbell
regressions remain in the unchanged sweep and diagnostic smokes.

The Codex doorbell uses fixed wording and hash references, never a peer body or
sender-defined instructions. Doorbells on one actual tmux pane are serialized
across driver-channel instances, and the pane identity is read again once that
lock is held; a changed pane gets `pending_retargeted` and no keys. A composer
showing the dim structural placeholder takes the landed empty route: it checks
a positively idle, empty active composer twice, sends Escape only there,
rechecks, inserts, waits for paste stability, then sends Enter separately. Busy
and unknown panes receive no keys. A stranded exact copy of this doorbell may
receive another Enter without another paste or clear. Styled redraws alone
never count as a turn.

### Codex draft preservation (Part 1: idle single-line drafts)

When an idle Codex composer holds a fully visible single-line human draft, the
doorbell preserves it byte for byte instead of leaving the mail pending:

1. `End` moves the cursor to the end of the draft. tmux pads captured rows
   with spaces, so the runtime cursor is the only proof of trailing spaces.
   The draft is the exact cells between the `› ` prompt and the cursor.
2. The draft is saved into a uniquely named private tmux buffer
   (`solet-composer-<uuid>`) and read back byte for byte. It is verified again
   immediately before `C-u` clears it, and the composer must then read empty.
3. The notice is bracketed-pasted. Each of the two inherited Enter attempts
   fires only while the composer shows exactly the notice with the cursor at
   its end. No whitespace is normalized; soft-wrapped rows join at their
   single break space.
4. After `turn_observed` or `submitted`, the draft is bracketed-pasted back
   into a freshly verified empty composer WITHOUT Enter. Its exact bytes are
   verified, and only then is the buffer retired.

The dim placeholder is an empty composer. The same words typed by a human are
a draft. An unstyled placeholder counts as empty only when `End` cannot move
the cursor past the prompt.

Failure handling never submits or overwrites human text:

- A failure before the clear leaves the draft untouched (`pending_end_failed`,
  `pending_save_failed`).
- A failed clear that leaves the draft intact reports `pending_clear_failed`
  and retires the buffer.
- A paste or Enter failure withdraws only the exactly owned notice with `C-u`
  and restores the draft (`pending_paste_failed`, `pending_enter_failed`,
  `submitted_no_turn`).
- A human edit or any other text that is not the owned notice stops the
  transaction: no Enter, no clear, no paste over it.

Whenever the draft cannot be verified back, the detail carries
`composer=recovery_needed`. An ERROR line `COMPOSER_RECOVERY_NEEDED` names the
message id, the recipient instance, session and role, the pane, the phase, the
retained `recovery_buffer` and the exact `saved_draft`. A routine success never
logs the draft. A recovery-needed attempt counts against the bounded pickup
attempts.

Deferred to Part 2 (mail retained, zero destructive or submitting keys, and a
pending advisory in the detail):

- busy turns (`pending_busy`)
- multiline, wrapped and popup composers (`pending_composer_shape`)
- copy mode or a missing prompt (`pending_composer_unknown`)
- zero-width or control characters, or a cursor that is not provably at the
  end (`pending_composer_unproven`)

Ordinary channel `send`/`insert`/`submit`, `drive_session`, first-turn
dispatch, `/clear` and `/compact` are Part 3 and unchanged here. The landed
empty route keeps its normalized ownership check, so a whitespace-only human
edit typed after its paste remains a residual of that route. Other residuals
are the OS race between the final read and its key, and a space typed exactly
at a soft-wrap boundary followed by moving the cursor back to the end.

The evidence is `composer_preservation_smoke.py`. It drives the production
channel through a real private tmux server against the deterministic
`composer_tmux_fixture.py`, never a fleet session. Real Codex rendering of the
dim placeholder, the hardware cursor, `End`/`C-u` and bracketed paste is
UNMEASURED. A compatibility claim needs a capture and round trip in a
disposable Codex seat.

The startup-owned delivery executor and existing `BridgeLifecycleSweeper`
`on_tick` rider revisit pending pickup records at the configured
`bridge_sweep_interval_seconds` cadence (default 300 seconds). A busy pane
keeps its mail pending without consuming an attempt. Repeated notices coalesce
by the current recipient; each act re-resolves the role, managed row and host.
A role successor receives the next doorbell; the previous holder is untouched.
Restart resumes durable pending records after a bounded in-flight lease.
Shutdown cancels queued work and joins running drivers before bridge teardown.

Three submitted/uncertain driver attempts bound automatic retries. Exhaustion
records `pending_retry_limit`, never message loss or successful uptake. Queue
saturation and unavailable drivers retain mail and a truthful pending/failure
outcome. `stored_no_wake` still means stored: never resend the message.

Read the existing `plugin::agent_messaging_plugin::session_status` for the
recipient. Its `delivery_pickup` field reports recent current projections and
versioned history, with explicit scan bounds and `next_history_version`.
The bounded view is evidence of those exact ids, never a complete inbox census.
The store's `read(message_id)` and `history(message_id, after_version)` provide
exact current and historical continuation through `StateManagementInterface`.
Its `reopen(message_id, expected_version)` is the correction/retry operation:
a stale version or terminal `inbox_returned` is refused. Reopening clears the
attempt count and lease while retaining immutable history and the original
message; it never sends a second copy. Store operations use the existing
state-service access, and status uses the existing session-status access.
No pickup operation grants task authority or deletes mail, history, branches,
or session artifacts.

Pass condition: retained durable mail and a currently drive-capable, positively
idle Codex pane. The shipped components making it true are the managed tmux
driver, declared state schemas, startup-owned executor and existing sweep rider.
`codex_eventual_pickup_smoke.py` exercises startup/start-stop at a nondefault
configured cadence, a scheduled miss recovered at the next eligible tick,
busy-to-idle pickup, role rotation, duplicate coalescing, both inbox sections,
queue saturation, restart and transaction rollback. Its checks also run through
the registered `tmux_driver_channel_smoke.py` gate entry.

## Waiting for a process result through the local CLI

`solet-bridge call PROCESS_KEY '<arguments>'` waits for a born-async job by
default on terminals, pipes and redirected files. It prints one terminal job
row; `job.result` retains the original operation envelope and `job.error`
retains a failure. Synchronous responses keep their existing envelope.
`--wait` states that choice explicitly; `--no-wait` returns the queued handle
and preserves ordinary completion delivery.

Wait intent travels with the initial dispatch and is copied into the root
job's metadata before work starts. A valid `bridge_wait_attached` binding
suppresses both caller role delivery and the fallback continuation, so a
caller receiving the result does not also receive a completion wake. Registered
children consult the root marker at completion time to suppress only their
caller-role message; their internal continuations still run. Children carry no
waiter marker. Missing or corrupt intent
advises and uses ordinary completion routing.

`--job-timeout` limits waiting, without cancelling or replaying the operation.
On timeout the CLI makes one owner-bound release attempt and prints a JSON
handle with error code `job_wait_timeout`, then exits nonzero and gives the
redemption command on stderr. Release clears the flag, preserves its binding,
unrelated metadata and append-only attachment/release history, and restores
normal completion delivery. A repeated release is idempotent. A foreign
bridge/session or stale metadata write is refused. Terminal outcomes remain
readable and cannot reopen. SIGINT, SIGTERM, SIGHUP, SystemExit during waiting
or output, and output failure also attempt one release before the originating
bridge closes; an uncertain release is disclosed and never retried automatically.
The single root release also restores notification for children that finish
later, including children created after release.

The active manager serializes release with terminal commit and routing. The
durable result is authoritative even if notification delivery fails; retrieve
the exact job id before resubmitting an operation. SIGKILL or host loss cannot
run cleanup and may leave the waiter attached. At the cross-runtime timeout or
SIGKILL/host-loss boundary, at most one root completion wake may be missed as
an explicit advisory; the durable result remains redeemable through `get_job`.
This does not authorize effect replay.

## Ledger/tmux metadata reconciliation

`plugin::agent_messaging_plugin::reconcile_ledger_tmux` takes no arguments and
returns a queued job handle immediately. Its existing single choreography worker
collects the fresh, read-only report. Poll
`plugin::agent_messaging_plugin::check_choreography_job_status` with that exact
`job_id` until terminal; the status response's `result` holds the former complete
response envelope and its `data` holds the report. Collection failures retain
the original error under the terminal error payload. Job creation and progress
write only the existing job ledger; the observed session ledger is unchanged.
The same collector rides the existing
bridge lifecycle sweep (configurable `bridge_sweep_interval_seconds`, shipped
default 300 seconds). A separately guarded rider logs its timestamped report;
an observer fault emits an advisory and leaves the existing riders running.
It adds no timer, cache, schema, host adoption or session action.

The ledger reads use `StateManagementInterface`, separate `host=tmux` and
`host=operator` filters, and `(created_at, id)` cursors with pages of at most
100 rows. Each host has a 250-row budget, for at most 500 ledger rows per
observation, including terminal rows. `ledger_sources` records read boundaries,
status, row counts, completeness and a continuation cursor when unfinished.
One argv-only `tmux list-panes -a -F` census records only session id/name,
pane id/pid/dead/current-command. It has a five-second timeout, at most 2,000
retained pane rows and 262,144 retained output bytes. Output is spooled to a
private temporary file rather than captured into an unbounded memory buffer;
excess metadata is reported as incomplete. The observer reads no pane text,
command arguments, environment, credentials or coding-agent history.

Joins require an exact `host_ref` match to a native session id or name and an
unambiguous one-to-one association. Every observed pane stays in the evidence.
`matched_process_present` requires an observed `pane_dead=0`; it establishes
only native process presence. All-dead joined sessions report
`matched_native_pane_dead`. Mixed sessions retain both live and dead pane
metadata. Invalid or missing dead flags yield `probe_unknown`.
`terminal_ledger_pane_present`, `duplicate_host_ref` and `host_ref_missing`
retain the ledger row and exact native evidence rather than repairing identity.
Operator rows without a trustworthy reference remain unresolved. Native-only
sessions report `unbound_tmux` and `scope_unknown`; no solet ownership is inferred
from a label, role, PID or instance suffix.

`ledger_tmux_missing` requires complete valid native and ledger observations.
A successful complete empty census exercises that outcome; timeout, nonzero
exit, malformed metadata and read failures never mean absence. Incomplete
coverage reports `partial_coverage` and preserves source status and cursors.
The reads are timestamped sequential snapshots, **not an atomic snapshot**;
a pane may disappear between them. Neither presence, absence, heartbeat nor a
queued/read/initially acknowledged message proves death, busy/idle state,
productive work or ongoing recipient uptake. Uptake remains the separate
Change `chg_ac80c4fa-8c12-453f-808c-776a21546021`; this report cannot close that
dependency or the whole fleet-reconciliation group.

The report is derived, under existing bridge process authorization, and writes
no source state. There is no persistent report to update, clear, retire,
close or reopen, and no optimistic write premise. Historical readback is the
retained timestamped report/log; a fresh observation corrects the current
picture without changing earlier observations. The fixture-only smoke is
`plugins/agent_messaging_plugin/tests/ledger_tmux_reconciliation_smoke.py`.


### Causal managed-dispatch Unit observations

A winning failed first-turn submission appends an `uptake_uncertain` Unit
observation; a winning current-attempt host-death transition appends
`worker_lost`. Both use the existing `managed-dispatch-link/v2` native writer.
They run after the dispatch CAS and its causal `first_turn` or `host_dead`
event. The observation binds prior/transition versions, causal event ID,
dispatch, Unit, assignment digest, worker and attempt from that transition's
local snapshot, even if a concurrent retry has already replaced the projection.

The prelaunch CAS retains `annotation_source` inside `attempt_receipt`.
It records the authenticated initiating instance, session, role and lane.
This is historical source attribution: the source did not necessarily perform
the later liveness probe. A source-only receipt is a reservation, not a
completed launch. Duplicate reserved requests refuse through the existing
pending-launch rule. Retained-host owner recovery preserves the original
attempt/source and existing owner refusals; an incremented retry captures its
actual authenticated initiator, including a legitimate coordinator successor.
Launcher receipt merging cannot overwrite the source. Existing first-turn and
spawned/failed-start evidence retains it for history.

The configured register client reaches fresh and retained first turns and the
periodic plugin sweep explicitly. Stale workers, terminal rows, duplicate
delivery and CAS losers append no observations. Missing, malformed or
mismatched provenance, unavailable clients and native refusals (including a
retired historical actor) produce `register_annotation_failed` with source
identity and precise error detail. They preserve the dispatch transition and
original submission/liveness error, without choosing another actor or retrying
the native write. Legacy rows receive an audit rather than invented provenance.

These are append-only observations, read through existing Unit event history.
Correction and withdrawal append targeted events and retain originals.
Assignment correction remains cancel plus a new dispatch; snapshots are
immutable per attempt. There is a best-effort cross-database crash gap between
the dispatch CAS/event and native observation; exactly-once crash recovery is
not provided. Client doubles test argv and response boundaries, not the actual
native parser, actor resolver or PostgreSQL lifecycle.
