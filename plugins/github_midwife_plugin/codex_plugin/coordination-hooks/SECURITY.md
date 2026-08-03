# Security notes — stock-Codex coordination-hooks

This page describes the complete WS-4b.3 package. The plugin currently
registers six handler entries backed by five entry-point scripts: the fixed
`step_zero_reminder.js`, `check_messages_reminder.js`, and
`role_binding_reminder.js` scripts, the WS-4b.1 Git-Controller gate, and the
WS-4b.3 synchronous `wake_waiter.js`. The sibling hydration package renders the
launcher-owned watcher and exact durable-inbox instructions; those deployment-
specific surfaces are intentionally outside the byte-identical plugin bundle.

## Trust boundary

Every handler is a non-managed Codex command hook. Installing or enabling the
plugin does not trust it. Codex skips new or modified definitions until the
operator reviews command, matcher, and timeout in `/hooks` and accepts the
definition's current hash. Hashes observed on one host are evidence only, never
seed values or portable policy.

The plugin is same-user mistake-prevention and context plumbing. It is not a
privilege boundary: a process that can rewrite the plugin, launcher environment,
or Codex user configuration already has the user's authority.

## Inputs and outputs

Inputs, exhaustively:

- hook JSON on stdin;
- `AGENT_SESSION_ID`, which arms `check_messages_reminder.js` (a functional
  precondition, not a protection — the inbox it points at is keyed on
  identity) and, together with `AGENT_WAKE_CLI` and `FLEET_TRANSPORT`, arms
  the synchronous wake waiter;
- `AGENT_SESSION_LABEL`, which arms `role_binding_reminder.js` only — the
  reminder that names the label as its content. `step_zero_reminder.js` is
  unconditionally armed and reads no environment variable at all (§7 re-key,
  2026-08-02; parity with the Claude sibling's `2fb49dbf2`);
- `GIT_CONTROLLER_NAME` and `AGENT_ROLE`, used only by the Bash gate;
- Codex's `${PLUGIN_ROOT}` expansion in the manifest command paths.

The reminder scripts never read a prompt or message field. The shared
`check_messages_reminder.js` reads only `hook_event_name`, accepts only
`UserPromptSubmit` or `SessionStart`, and selects the same fixed reminder for
both. The role reminder does not interpolate the label it tests.

Outputs, exhaustively:

- reminders: one JSON object containing exactly
  `hookSpecificOutput.hookEventName` and fixed
  `hookSpecificOutput.additionalContext`;
- gate allow/no-op: no output, exit `0`;
- gate block: a fixed-format stderr explanation and exit `2`;
- waiter no-op/idle expiry: empty Stop JSON and exit `0`;
- waiter wake: fixed `{"decision":"block","reason":"..."}` JSON and exit `0`;
- waiter fault: empty Stop JSON, one bounded fixed-format diagnostic, and exit
  `0` so broken wake plumbing cannot trap the turn.

No handler writes a file, accesses the network, reads credentials, or relays
peer-message content. Two of the four Node hooks (`check_messages_reminder.js`,
`wake_waiter.js`) use built-in `fs` to read stdin; the other two
(`step_zero_reminder.js`, `role_binding_reminder.js`) never touch stdin —
`step_zero_reminder.js` reads no environment variable at all, and
`role_binding_reminder.js` reads only `AGENT_SESSION_LABEL`. Only
`wake_waiter.js` additionally
uses `child_process.spawn`, exactly once, to run the configured executable with
literal argv `["wake"]`, `shell: false`, and all three child streams set to
`ignore`. The Python gate is standard-library-only
and imports byte-identical materialized policy modules from its plugin
directory. CPython may create `__pycache__` files unless the launcher sets
`PYTHONDONTWRITEBYTECODE=1`; the plugin never reads them.

## No dynamic hook-text channel

All reminder context is compiled into the hook scripts as fixed generic text.
The plugin does not accept environment-authored instructions or deployment-
specific command text. Exact commands, process keys, and local routing guidance
belong in the project's native `AGENTS.md` instructions, where Codex already has
one reviewable project-instruction surface.

## Manifest execution contract

Stock Codex 0.141.0 required `hooks/hooks.json` to contain only the top-level
`hooks` key and required command handlers in command-string form. The manifest
therefore uses the measured `${PLUGIN_ROOT}` paths and does not add newer
optional top-level fields. Codex owns expansion and command invocation; the
scripts do not construct a shell command or interpolate hook input into one.

The `PreToolUse` matcher is exactly `^Bash$`, matching the only live payload
shape captured before WS-4b.1. This is intentionally narrower than current
Codex documentation may support. Widening to edit or delegation aliases requires
a separate live capture, negative control, policy review, and manifest update.

The adversarial identity control is deliberately stronger than a label-only
check: even when `AGENT_IDENTITY`, `AGENT_INSTANCE_ID`, `AGENT_SESSION_LABEL`,
`AGENT_SESSION_ID`, and the hook payload's `session_id` are all set to
`Git-Controller`, a missing or non-controller `AGENT_ROLE` still blocks a
detected mutation. `AGENT_ROLE` is the only authority input.

The `Stop` handler is deliberately synchronous because stock Codex parses but
does not support async command hooks. Its explicit timeout is 86,400 seconds;
the current `homunculus wake` default is 86,100 seconds, leaving a five-minute
cleanup margin. `FLEET_TRANSPORT` must be exactly `watch`; unset, empty, `mcp`,
and unknown values all disarm it to prevent two receive paths from waking the
same session. When Codex invokes the continued turn's second Stop with
`stop_hook_active: true`, the handler returns immediately without spawning.

## Failure modes

- `step_zero_reminder.js` is unconditionally armed — no missing variable
  disarms it.
- Missing `AGENT_SESSION_ID`: `check_messages_reminder.js` is a silent no-op.
- Missing `AGENT_SESSION_LABEL`: `role_binding_reminder.js` is a silent
  no-op.
- Malformed reminder stdin: fixed reminders still exit `0`; the shared reminder
  uses its harmless prompt-event default.
- Missing waiter input or any incomplete watch configuration: empty Stop JSON,
  no child process, exit `0`.
- Malformed Stop stdin: one fixed diagnostic, empty Stop JSON, exit `0`.
- Wake CLI exit `0` (idle expiry or competing waiter): empty Stop JSON, exit
  `0`.
- Wake CLI spawn failure, signal death, or unexpected status: one bounded
  diagnostic, empty Stop JSON, exit `0`.
- Hook cancellation: forward the signal to the child, escalate to `SIGKILL`
  after one second if required, and exit `0` after child teardown.
- Gate disabled (`GIT_CONTROLLER_NAME` missing): allow.
- Gate enabled with missing `AGENT_ROLE`: read-only Bash remains allowed, but a
  detected git mutation is blocked and reports role `<unknown>`.
- Unexpected gate parse/runtime error: allow, consistent with its explicitly
  documented same-user mistake-prevention scope.

Context hooks never exit `2`. Blocking outcomes are the fixed Stop
continuation JSON and an affirmative Bash policy match in the gate.

## Trust revocation

Stock Codex's supported `/hooks` browser is the execution-revocation surface
for non-managed definitions. A disabled definition retains its reviewed hash
but gains disabled state; this is intentional because re-enabling is a separate
reviewed UI action. In the stock 0.141.0 acceptance, disabling only the Stop
definition changed the event summary from `Installed 1 / Active 1` to
`Installed 1 / Active 0`; PreToolUse remained `1 / 1`, SessionStart `2 / 2`,
and UserPromptSubmit `2 / 2`.

Uninstall alone is not revocation. An identical reinstall reactivated all six
definitions when their residual state was enabled. Repeating the cycle after
disabling Stop preserved `Stop 1 / 0` and left the other five definitions
active. The operator can therefore disable one definition before removal and
know an identical reinstall will not silently re-enable it. Direct edits to
`hooks.state` are not part of this package's supported procedure.

## Transport asymmetries (MCP vs. non-MCP/watch)

This plugin's own behavior does not depend on transport — every handler above
runs identically regardless of which transport the session's coordination
layer uses. Three capabilities belong to the coordination layer itself, not
to this plugin, and are disclosed here so a review of this deployment does
not discover any of them as a gap instead of reading it on this page. An
entry only earns a place here once the two transports are actually shown to
diverge; a capability that behaves the same on both is not listed — this
section is not a complete inventory of every capability that was checked,
only of the ones confirmed to differ, and equality beyond these three is not
otherwise claimed (see Explicit exclusions below) — plus one further entry,
below, that was checked, found equivalent, and is kept rather than silently
dropped because it was previously listed here as an open provisional entry.

Backend agent-thread dispatch is confirmed as an MCP-only mechanism. Opening
and driving a backend agent thread (the coordination service's
`agent_thread_open` / `agent_send` / `agent_messages` / `agent_status` /
`agent_close` operations) is a capability of the MCP transport only — it is
bridge-bound by design and has no equivalent on a declared non-MCP
transport. A session running on a non-MCP transport cannot open or drive
one at all. Fleet coordination in this deployment operates through full
peer sessions exchanging messages, not through backend-driven agent
threads, so this capability's absence off the MCP transport does not
remove anything the operating model depends on. Whether this scope is
permanent is an open engineering question, not a decided one — no operator
ruling on this point exists on record.

Peer enumeration is confirmed as an MCP-only capability. Discovering which
peer sessions are currently registered works only over the MCP transport. A
session on a non-MCP transport reaches the platform solely through
`homunculus call <process_key>`, and no registered process returns the peer
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
fix is scoped but not built: three thin CLI subcommands (`homunculus
inbox`, `homunculus peers`, `homunculus whoami`) over routes that already
exist would close this gap; none of the three exist today.

Idle-session wake is confirmed as an MCP-only gap. A session on the MCP
transport that goes idle with nothing pending does not wake on its own —
there is no waker for that transport, so a truly idle MCP session never
turns again without something external driving it back open. A non-MCP
(`watch`) session's Stop-hook waiter (`wake_waiter.js`, this plugin)
reopens the session on delivery instead. The asymmetry is in waking, not
delivery: a message sent to an idle session is durably received on either
transport; what differs is whether receiving it also resumes the
session's turn. Measured 2026-08-02 (a live coordination walk): negative
control (idle, nothing pending) run first on both arms — clean on both;
an IMPORTANT delivery addressed directly to the MCP arm's own instance
produced no unsolicited turn within 60s, while the same delivery to the
non-MCP arm woke the session at 55.6s. No fix is scoped for this entry; it
is disclosed, not remediated.

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

This package does not prove or claim:

- live watcher/inbox consumption or lifecycle survival, despite the delivered
  hydration launcher, marketplace, and paging contract;
- a fresh externally addressed idle stock-Codex model sample;
- Phase-5 MCP/non-MCP equality;
- patched-binary retirement or a stock-binary launcher swap.

Those are WS-4b.4 and WS-4b.6. A routing receipt, inbox visibility, direct
script output, or self-addressed send is not evidence for them.

## Verification map

| claim | evidence |
|---|---|
| reminder text is fixed and cannot relay prompt/message/label content | `tests/reminder_hooks_smoke.py` |
| conservative manifest handlers match the shipped entry points and Bash routing | `tests/manifest_consistency_smoke.py` |
| repo marketplace resolves to this plugin and manifest metadata is consistent | `tests/marketplace_consistency_smoke.py` |
| Bash allow/block and AGENT_ROLE-only authority | `tests/git_controller_gate_smoke.py` |
| waiter arming, loop guard, one-bit output, failure paths, and cancellation cleanup | `tests/wake_waiter_smoke.py` |

Run all five with `python3 tests/run_all.py`. These are offline source and
behavioral checks. The live untrusted/trusted stock-process sentinel is a
separate acceptance leg because only Codex itself can prove hook trust state.
