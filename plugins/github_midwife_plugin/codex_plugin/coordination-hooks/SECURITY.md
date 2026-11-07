# Security notes — stock-Codex coordination-hooks

This page describes the complete stock-Codex package. The plugin currently
registers four handler entries backed by four entry-point scripts: the fixed
`step_zero_reminder.js` and measured `session_start_context.py` scripts, the
async `context_status_reporter.py` Stop reporter, and the SYNCHRONOUS
`inbox_consumer.py` Stop hook (CDX-06, 2026-08-24). The
sibling hydration package renders the launcher-owned watcher and exact
durable-inbox instructions; those deployment-specific surfaces are
intentionally outside the byte-identical plugin bundle.

The context reporter's background binding is defined by the current official
Codex hook contract. Live stock-process execution remains a separate
acceptance leg. It is context telemetry only: it neither reads peer messages
nor returns continuation instructions. Its one subprocess is an argv-only
call to the launcher-provided `AGENT_WAKE_CLI`; it never constructs a shell
command.

`inbox_consumer.py` is the deliberate exception to "never returns
continuation instructions": it CAN return `{"decision": "block", "reason":
"<fixed nudge>"}`, and measured live against the actual codex-0.149.0
binary this fleet runs (isolated proof, a dev-checkout workbench evidence
report, not part of this shipped bundle), that decision DOES force Codex to
continue the turn. This is exactly why it must be
synchronous rather than async (an async hook's decision is discarded — Codex
does not wait for it). It uses `solet-bridge wake --max-wait 0` with an
eight-second registration and two-second child bounds, so it observes only
already-present delivery. This synchronous shape can force continuation and
never reads or relays peer-message
CONTENT itself (same discipline as the Claude sibling's `wake_waiter.py`);
its own two subprocesses are argv-only calls to the launcher-provided
`AGENT_WAKE_CLI` (`wake`, then `call report_inbox_consumption`), never a
shell command.

## Trust boundary

Every handler is a non-managed Codex command hook. Installing or enabling the
plugin does not trust it. Codex skips new or modified definitions until the
operator reviews command, matcher, and timeout in `/hooks` and accepts the
definition's current hash. Hashes observed on one host are evidence only, never
seed values or portable policy.

The plugin is same-user context plumbing and reminders. It is not a
privilege boundary: a process that can rewrite the plugin, launcher environment,
or Codex user configuration already has the user's authority.

## Inputs and outputs

Inputs, exhaustively:

- hook JSON on stdin;
- the live-context configuration and child reads listed below;
- `AGENT_INSTANCE_ID`, `AGENT_SESSION_ID`, and `AGENT_WAKE_CLI`, which arm the
  context reporter, and separately arm the inbox consumer, only when all
  three are present in each hook's own environment read (no shared state
  between the two);
- the reporter's host-provided `session_id`, `transcript_path`, and `model`,
  cross-checked against `session_meta` and `turn_context` in that transcript;
- the inbox consumer's host-provided `hook_event_name` (must equal `"Stop"`);
  it reads no other field of the hook payload — deliberately not even
  `stop_hook_active`, since the spool-existence check is what terminates the
  block/continue chain, not a self-imposed reentrancy counter — and never
  inspects prompt or message content;
- Codex's `${PLUGIN_ROOT}` expansion in the manifest command paths.

The knowledge-base reminder reads only its event name. Live session context
reads event/source and the two service responses described below. Neither
relays prompt or peer-message content.

Outputs, exhaustively:

- reminders: one JSON object containing exactly
  `hookSpecificOutput.hookEventName` and fixed
  `hookSpecificOutput.additionalContext`; live context uses the same envelope
  for bounded register data, role/count observations and failure lines;
- context reporter success/no-reading/unarmed: inert `{}` JSON, exit `0`;
- armed reporter contract or delivery failure: fixed-prefix stderr, exit `1`.
- inbox consumer, nothing pending or unarmed: inert `{}` JSON, exit `0`;
- inbox consumer, pending found: `{"decision": "block", "reason": "<fixed
  nudge>"}` JSON, exit `0` — Codex's continuation contract is driven by the
  JSON body, not the exit code (unlike the Claude sibling's Stop-hook
  contract, where exit `2` is the wake signal); best-effort diagnostic
  stderr on a failed subprocess call, never a nonzero exit either way (see
  Failure modes below — this hook must never trap the session in a failing
  Stop hook).

No handler writes a file or opens a direct network/database connection. The
live-context child psolet performs its own normal credential lookup. The
context reporter and inbox consumer retain their existing argv-only child
contracts. `step_zero_reminder.js` reads stdin through Node's built-in fs.
The Python entry points import only standard-library modules and no sibling
policy module, so CPython writes no bytecode beside them.

## Live session context — reviewer due diligence (2026-10-08)

`session_start_context.py` replaces the static message and role reminders on
SessionStart `startup`, `resume`, and `clear`. It performs two read operations:

- `[PROJECT_SOLET_CLI, "resume-digest", "--limit", "1"]` reads the bounded,
  **register-wide** digest. The default executable is
  `$HOME/Workspace/project-solet/.venv/bin/psolet`; installations elsewhere
  set `PROJECT_SOLET_CLI` to one executable path. The child resolves its normal
  `PROJECT_SOLET_DATABASE_DSN` or Keychain credentials; the hook neither reads
  credentials itself nor prints child stderr. Register previews are data,
  not instructions or a substitute for full authoritative reads.
- `[SOLET_BRIDGE, "call", "plugin::agent_messaging_plugin::peer_inbox", JSON]`
  pages the instance and role sections with `observer: true`. `SOLET_BRIDGE`
  defaults to `solet-bridge` on PATH. The original `SOLET_NAME`,
  `AGENT_SESSION_ID` and `AGENT_INSTANCE_ID` are preserved and required.
  Current bound role names come from `role_read_coverage`, never the local
  label. Forward instance cursors preserve timestamp ties; role cursors are
  independent. The hook never acknowledges a page or advances a covered mark.

The role count means **pending above role covered mark (display/consumption
unknown)**. Observer mode includes display-receipted rows; it cannot prove
unread state. The instance count is stored-message history, explicitly labelled
**unread: unknown (no platform unread semantics)**. The hook fetches and
JSON-parses the complete peer_inbox response, including message objects and
their body fields. For inbox data it injects only derived counts, current
bound-role labels, and observation/error text; it never injects message bodies.
Paging produces an observation over an interval, not an atomic cross-page
snapshot; a binding change, repeated cursor, partial role failure or identity
mismatch makes the count unavailable.

The digest child has a three-second timeout, and all inbox pages share a
separate three-second deadline. The manifest timeout is eight seconds. A failed
read adds one explicit `unavailable (ErrorClass)` line and exits zero; a missing
identity is `MissingIdentity`, never zero messages. No cache or static reminder
fallback exists. JSON serialization escapes all observed strings. The digest
context is capped at 5800 UTF-8 bytes and inbox context at 1000, leaving room
inside 8192 for the unchanged knowledge-base reminder. Over-budget output is
reported as `ByteBudgetExceeded`, never silently clipped into apparent success.

This stdlib Python 3.13 hook uses `subprocess.run` with argument arrays and no
shell. It creates no files, performs no register writes, claims no role and
changes no Git policy. Hook installation/trust is separate from candidate delivery;
these are same-user coordination controls, not a privilege boundary.

The manifest consistency smoke and the reminder hooks smoke cover this candidate's manifest
and fixed reminder; the live SessionStart subprocess smoke is added by slice S3 of this Change.
This candidate makes no claim of live host deployment.
The independent review unit records the due-diligence verdict required by
`rul_af7c73e3` and `rul_f0e51f93`.

## Manifest execution contract

Stock Codex 0.141.0 required `hooks/hooks.json` to contain only the top-level
`hooks` key and required command handlers in command-string form. The manifest
therefore uses the measured `${PLUGIN_ROOT}` paths and does not add newer
optional top-level fields. Codex owns expansion and command invocation; the
scripts do not construct a shell command or interpolate hook input into one.

`context_status_reporter.py` is explicitly `"async": true`; the current
official hook contract defines that background mode, and the manifest smoke
requires it stay that way (removing it is a named failing mutation) —
transcript parsing and the platform call must never hold the turn boundary
for a hook that only reports telemetry. `inbox_consumer.py` is the one
deliberate exception in this manifest: it carries NO `async` key (the
manifest smoke requires exactly that, for every `Stop` entry other than the
reporter), because measured live (isolated proof, a dev-checkout workbench
evidence report, not part of this shipped bundle) an async hook's
`decision` output is discarded by Codex — only a synchronous hook can gate
or continue a turn. This is not "a synchronous command hook on Stop would
hold the turn boundary" being ignored; it is bounded by an immediate spool
observation (`solet-bridge wake --max-wait 0`) with an eight-second registration.
Multiple matching
hooks may run concurrently; reporter rows therefore carry both
`reporter_surface` and a content-generation integer rather than assuming
this source copy is unique — `inbox_consumer.py`'s own `report_inbox_consumption`
rows carry `reporter_surface` for the same reason.

## Failure modes

- `step_zero_reminder.js` is unconditionally armed — no missing variable
  disarms it.
- Missing identity or read failure: live context emits a named unavailable line, exit `0`.
- Malformed reminder stdin: the knowledge-base reminder uses its SessionStart default.
- All three reporter identity variables missing: deliberate non-fleet no-op.
- Partially armed reporter: fail loud; no guessed identity or CLI path.
- No positive `token_count` after the newest `context_compacted` row: no-op;
  never reuse the pre-compaction reading.
- Transcript/session/model disagreement, malformed measured fields, or a
  failed platform response: fail loud and do not emit a report.
- All three inbox-consumer identity variables missing: deliberate non-fleet
  no-op, same precondition shape as the reporter but a DIFFERENT failure
  posture (see below).
- Partially armed inbox consumer, or any inbox-consumer subprocess failure
  (`wake` spawn error, `report_inbox_consumption` call failure): fixed-prefix
  stderr, but exit `0` — deliberately NOT fail-loud like the reporter. This
  hook's whole value is its ability to gate a turn via its JSON decision
  body; an untested interaction between a nonzero exit and Codex honoring
  that body is a risk this hook does not take, so every failure degrades to
  "report nothing pending" rather than a nonzero exit.
- `hook_event_name` not `"Stop"`: silent no-op (a misregistration should not
  crash a hook bound to the wrong event).

`context_status_reporter.py` never exits `2` or requests continuation — the
only blocking outcome for a Codex hook in this manifest is `inbox_consumer.py`'s
`decision:block` JSON body, which is a DIFFERENT mechanism from an exit
code (Codex's Stop-hook continuation contract is JSON-body-driven, not
exit-code-driven — measured live, see the isolated proof cited above).
`inbox_consumer.py` itself always exits `0`; it never uses exit `2`.

## Trust revocation

Stock Codex's supported `/hooks` browser is the execution-revocation surface
for non-managed definitions. A disabled definition retains its reviewed hash
but gains disabled state; this is intentional because re-enabling is a separate
reviewed UI action. Historical evidence (stock 0.141.0, while a `Stop`
definition still shipped): disabling only the Stop definition changed it from
active to inactive while unrelated definitions remained active. Counts are
version-specific evidence, not a manifest contract; inspect the live `/hooks`
inventory after each reinstall.

Uninstall alone is not revocation. An identical reinstall reactivated all
definitions whose residual state was enabled. Repeating the cycle after
disabling a definition preserved that disabled state and left unrelated
definitions active. The operator can therefore disable one definition before
removal and know an identical reinstall will not silently re-enable it. Direct
edits to `hooks.state` are not part of this package's supported procedure.

## Transport asymmetries (MCP vs. non-MCP/watch)

The reminders do not depend on transport. Capabilities belonging
to the coordination layer are disclosed here so a review of this deployment
does
not discover any of them as a gap instead of reading it on this page. An
entry only earns a place here once the two transports are actually shown to
diverge; a capability that behaves the same on both is not listed — this
section is not a complete inventory of every capability that was checked,
only of the ones confirmed to differ, and equality beyond these three is not
otherwise claimed (see Explicit exclusions below) — plus one further entry,
below, that was checked, found equivalent, and is kept rather than silently
dropped because it was previously listed here as an open provisional entry.

Backend agent-thread dispatch (the coordination service's
`agent_thread_open` / `agent_send` / `agent_messages` / `agent_status` /
`agent_close` operations) was retired in the D3 dormant-head retirement —
the dormant `GuardedAgentInterface` backend had zero implementing plugins,
so the entire dispatch surface (both the MCP tools and their HTTP/service
implementation) was removed, not just its non-MCP transport reach. This
entry is kept for record: while live, it was an MCP-only mechanism with no
non-MCP-transport equivalent. Fleet coordination in this deployment
operates through full peer sessions exchanging messages, which this
retirement does not touch.

Peer enumeration is confirmed as an MCP-only capability. Discovering which
peer sessions are currently registered works only over the MCP transport. A
session on a non-MCP transport reaches the platform solely through
`solet-bridge call <process_key>`, and no registered process returns the peer
registry: the CLI exposes no `peers` subcommand, semantic discovery over the
knowledge base surfaces no peer-registry verb, and `peer_list` exists only
as an MCP tool. Sending is unaffected — `peer_send_by_name` resolves and
delivers identically on both transports — so the asymmetry is in
discovery, not delivery: a non-MCP session can address a known peer or
role by name but has no peer list available to it and cannot enumerate who
is currently live. One adjacent verb, `list_bridges`, returns a
well-formed, successful, and incorrect result (`0 bridge(s) tracked`
against a live multi-session fleet) rather than an error; treat it as
unreliable for this purpose, not as a substitute for peer enumeration. A
fix is scoped but not built: three thin CLI subcommands (`solet
inbox`, `solet peers`, `solet whoami`) over routes that already
exist would close this gap; none of the three exist today.

Idle-session auto-drive is a managed-session capability (`spawn_session`'s
`drive_on_delivery`), not a background-hook capability, and this plugin
still carries no automatic delivery-triggered WAKE on either transport — an
idle Codex pane with no turn in flight gets no notice from `inbox_consumer.py`,
because Stop only fires when a turn ends (confirmed live in a dev-checkout
workbench evidence report, and by source, `codex-rs/core/src/session/turn.rs:383`
per the 2026-07-26 investigation).
That gap is CDX-05/CDX-06's part B (interim managed-drive restart), not
this hook's job.

What CHANGED (CDX-06, 2026-08-24): a turn that IS ending no longer walks
past mail that is already queued. `inbox_consumer.py` checks the session's
already-armed sidecar watcher (`solet-bridge wake --max-wait 0`) on every
`Stop`, and if it finds something, forces the turn to continue with a nudge
rather than letting the session go idle unaware.
Because a real consumer now exists, the wake-hook spool is RE-ARMED for
Codex — the hydration-rendered launcher no longer passes `--no-spool` to
`<name> watch`. It was disabled by codex-0147-dead-spool-retirement
(2026-08-13) specifically because nothing consumed it then; that premise no
longer holds. An operator-launched Codex session still gets no automatic
DRAIN of message content from this hook (it nudges, never relays content —
see Inputs and outputs above) and the `SessionStart` live-context
observation fires only at startup/resume/`clear`, not per turn — the
operator (or the model, on an armed session, in its continued turn) still
reads `peer_inbox` explicitly. A managed `spawn_session` worker still also
gets `drive_on_delivery`'s driver-channel notice, independent of this
plugin's hooks entirely, for the idle-wake case this hook does not cover.
Delivery durability is unchanged throughout: the peer message is persisted
before any of these best-effort notifications, on both transports.

Delivery durability across a coordination-service cutover is checked,
confirmed equivalent on both transports. A message sent to a session
shortly before the coordination service restarts or swaps is durably
retrievable by that session afterward. Measured against a live blue-green
cutover (2026-08-02, ~220s): a live MCP session and a `watch`-armed
non-MCP witness both kept their pre-fire agent instance id across the
swap with no re-claim, and both had two marked messages sent to them
before the fire (one silent, one IMPORTANT) — all four confirmed durably
present, at their original message ids, via the peer inbox after cutover.
Both transports behaved identically, so this is not an asymmetry; it is
kept here, reclassified rather than silently dropped, because it was
previously listed as an open provisional entry and a reader who saw it
there should see it closed, not vanished. One distinct question remains
genuinely unmeasured, stated as a bound rather than folded into the
confirmed result: whether a live push notification in flight at the exact
reconnect instant is itself delivered — separate from the underlying
message record, which is durable regardless of notification delivery.
This measurement's own markers happened to be sent and delivered before
the restart began, an artifact of this run's timing, not a proof either
way; a future run could deliberately arm a marker mid-cutover to test that
instant directly. No claim of "always survives" is made — only what was
measured.

Other capability comparisons across transports are ongoing; of the
entries above, three (backend agent-thread dispatch, peer enumeration,
idle-session wake) are the only confirmed or provisional divergences so
far, and the cutover-durability entry is a checked, closed equivalent kept
for transparency rather than an open item. An
absence from this section reflects the measurement record at time of
writing, not a guarantee that no further asymmetry will ever surface —
this section is maintained as that record changes, and a future divergence
gets its own entry rather than a silent gap.

## Explicit exclusions and retirement gates

This package ships no git-mutation control. The Bash Git-Controller gate it once
carried was removed by operator ruling `rul_e71e43be` (commit `91b8ace14`); what
it refused, and that nothing enforces it now, is inventoried in `README.md` under
*Retired behaviours*, separately from any replacement claim.

This package does not prove or claim:

- production-fleet-wide inbox consumption or lifecycle survival, despite the
  delivered hydration launcher, marketplace, and paging contract. What IS
  proven live, against real codex-0.149.0 in an isolated marketplace/CODEX_HOME
  (never a live checkout or a working lane — recorded in a dev-checkout
  workbench evidence report, not part of this shipped bundle): the Stop-hook
  synchronous `decision:block` continuation mechanism itself, both that async
  discards it and that sync honors it. Not yet proven live: `inbox_consumer.py`
  driving a REAL peer-inbox delivery end to end against the running fleet
  (that live-fleet acceptance follows the interim managed-drive restart,
  coordinated separately);
- a fresh externally addressed idle stock-Codex model sample;
- Phase-5 MCP/non-MCP equality;
- patched-binary retirement or a stock-binary launcher swap.

Those (other than the CDX-06 Stop-hook mechanism proof above) are WS-4b.4 and
WS-4b.6. A routing receipt, inbox visibility, direct script output, or
self-addressed send is not evidence for them.

## Verification map

| claim | evidence |
|---|---|
| knowledge-base reminder is fixed and cannot relay prompt/message/label content | `tests/reminder_hooks_smoke.py` |
| conservative manifest handlers match the shipped entry points and Bash routing | `tests/manifest_consistency_smoke.py` |
| repo marketplace resolves to this plugin and manifest metadata is consistent | `tests/marketplace_consistency_smoke.py` |
| reporter uses the latest positive post-compaction native measurement, refuses identity mismatch, and never invents cache state | development-checkout behavioral smoke for the reporter (not shipped) |
| `context_status_reporter.py` is the single declared async subprocess exception; every other `Stop` entry carries no `async` key | `tests/manifest_consistency_smoke.py`'s inventory and source contract |
| `inbox_consumer.py` gates the fleet precondition, observes pending/empty/unknown via zero-wait `wake`, reports only known pending/empty results, never drains the inbox, decodes decision:block only when pending, and always exits 0 | `plugins/github_midwife_plugin/codex_plugin/coordination-hooks/tests/inbox_consumer_smoke.py` |
| a synchronous Stop hook's `decision:block` actually forces Codex to continue a turn, and an async hook's decision is discarded, on real codex-0.149.0 | a dev-checkout workbench evidence report (live, isolated, not part of this shipped suite) |

Run all seven with `python3 tests/run_all.py`. These are offline source and
behavioral checks. The live untrusted/trusted stock-process sentinel is a
separate acceptance leg because only Codex itself can prove hook trust state.
