# Security notes — coordination-hooks

This page pre-answers the questions a security review of this plugin is
likely to ask. The plugin is five Claude Code hooks: three context reminders
(`step_zero_reminder.js`, `check_messages_reminder.js`,
`role_binding_reminder.js`), one opt-in idle-wake waiter (`wake_waiter.js`),
and one opt-in git-mutation guard (`git_controller_gate.py`).

The wake waiter is the plugin's one privileged behavior — it executes a
local command — so it gets its own section below and is the right place to
focus review attention. It emits only fixed string literals: it discards the
command's output and conveys a single bit ("deliveries pending"). The other
four hooks execute nothing at all.

An operator-guidance channel (`operator_guidance.js`) shipped here until
2026-08-01, when the operator ruled it out of the plugin fleet-wide: hooks
carry only hard-coded, generic text; deployment-specific guidance belongs in
the project's own instruction files (`CLAUDE.md`), never in an
environment-supplied variable a hook emits verbatim. Its removal is
complete as of this page — no hook in this plugin emits environment-supplied
prose, and there is no longer a full-trust content class to describe here.

## Inputs and outputs — the complete data-flow surface

Inputs, exhaustively:

- **stdin**: the JSON payload Claude Code passes to every hook (event name,
  tool name, tool input). Parsed defensively; malformed input degrades to a
  no-op (reminders) or an allow (gate).
- **Environment variables**: `AGENT_SESSION_ID` (arms `check_messages_reminder.js`
  and the wake waiter — a functional precondition, since the inbox and the
  wake spool are both keyed on identity, never a protection), `AGENT_SESSION_LABEL`
  (arms `role_binding_reminder.js` only, and — in that hook only — the one
  value the plugin interpolates into injected text; see the injection
  section below), `GIT_CONTROLLER_NAME` (gate on/off + controller name),
  `AGENT_WAKE_CLI` (wake waiter on/off + the command it runs),
  `FLEET_TRANSPORT` (disarms the wake waiter on a declared non-watch
  transport; unset and empty both mean "not declared" and leave it armed),
  `CLAUDE_PROJECT_DIR` (repo root, used only to locate `.git/` for the
  write-protection check). `step_zero_reminder.js` reads no environment
  variable at all — see the Configuration surface section.
- **One local file read**: `~/.claude/sessions/<parent_pid>.json`, to
  resolve the calling session's name (see the identity caveat below).

Outputs, exhaustively:

- **stdout**: a JSON object whose `additionalContext` is a **fixed string
  literal** compiled into the script (reminders); nothing (gate, wake
  waiter). One exception, stated precisely: `role_binding_reminder.js`
  interpolates a single value into its otherwise-fixed literal — the
  `AGENT_SESSION_LABEL` environment variable, JSON-escaped via
  `JSON.stringify`, so quotes, braces, newlines and control characters in
  the label cannot restructure the output. That value comes from the
  operator-controlled process environment, and it is the only value any
  hook in this plugin interpolates into injected text.
- **stderr**: a block-explanation message when the gate blocks — a fixed
  template over the calling session's own already-supplied identity and a
  slice of its own command, path, or subagent-type argument, detailed in
  the trust-classes note below and in the Threat model section, never
  third-party or message content; the wake waiter's **fixed** nudge or
  fixed-format failure note (the numeric exit status is the only variable
  part; the child's own output is discarded unread).
- **Exit codes**: `0` (allow / no-op) or `2` (gate block; wake signal).

**Environment variables arm and disarm behavior; exactly one additionally
supplies content, and only to one surface.** Two different guarantees hold on
this page, read separately — conflating them is exactly the false reading a
hostile review would, correctly, call out. The three reminders'
`additionalContext` holds the **stronger** property: a compiled-in literal, with at most one
interpolated environment-sourced value (`AGENT_SESSION_LABEL`) across all
three. The gate's stderr block message holds a **different, weaker but
still sound** property: it is a fixed template over caller-supplied
values — the calling session's own already-resolved identity, and a slice
of that same session's own command, file path, or subagent-type argument,
echoed back to the session that supplied them. Every value the block
message can ever contain is the calling session's own input, reflected
back to it; no third-party session's message content, and no other
session's input, ever reaches it. Where this page says a value is "fixed" or
"static" without further qualification, it means the stronger,
reminders-only property; the gate's block message is held to its own,
separately-stated guarantee instead, described on its own terms here and in
the Threat model section below.

Beyond that: no network I/O of any kind in any hook (no HTTP, no sockets,
no DNS). No hook writes a file as an action of its own. One interpreter
side effect is disclosed rather than claimed away: `git_controller_gate.py`
imports two sibling modules (`_git_controller_lex.py`,
`_git_controller_walker.py`), and CPython byte-compiles imports, so running
the gate causes `hooks/__pycache__/*.pyc` to appear beside the scripts.
Nothing in the plugin reads those files back, and an operator who wants them
gone can set `PYTHONDONTWRITEBYTECODE=1` in the session environment.
No secrets are read, stored, or transmitted; the
hooks have no credential access at all. Exactly one subprocess execution
exists in the plugin — the wake waiter's fixed-argv invocation of the
operator-configured CLI, detailed below. The gate executes nothing: it
lexes and inspects tool-call text; it never executes or evaluates it.

## The wake waiter — the one privileged hook, in full

**What it does.** On `Stop` (session going idle), when armed, it runs
exactly `$AGENT_WAKE_CLI wake` — the executable named by the operator's
environment, with the single fixed argument `wake`, spawned directly with
**no shell** (no interpolation, no PATH tricks beyond ordinary executable
resolution). That command is expected to block until a coordination
delivery arrives for this session, then exit with the hook wake signal
(exit code 2). The waiter **discards the child's stdout and stderr
unread**; on the wake signal it emits its own compiled-in fixed nudge —
"deliveries are pending in the peer inbox" — and exits 2, which turns the
nudge into the session's next turn. The session then fetches the actual
messages itself through the normal tool path, where they arrive as
ordinary tool results. Discarding the child's output loses nothing: the
watcher's spool is a tee of notifications, and the durable copies live in
the coordination service's message store.

**Arming.** Three conditions, all required: `AGENT_SESSION_ID` set,
`AGENT_WAKE_CLI` set, and `FLEET_TRANSPORT` unset, empty, or `watch` — an
empty value is not a declaration, so it arms exactly as unset does (a
hydration bug exporting `""` must never silently kill wakes). Re-keyed from
`AGENT_SESSION_LABEL` on 2026-08-01: the label was only ever a proxy for
having a spool to wait on; the session id is what the spool path is
actually built from. Any ordinary Claude Code session — no session id, no
CLI variable — gets a silent no-op.

**Command provenance.** The command comes from the invoking user's own
environment, set by the same launcher configuration that labels the
session. It executes as that user, with that user's privileges — identical
in power to the operator typing the command themselves. The hook grants no
elevation and reaches no other trust domain. An attacker who can set this
environment variable can already run arbitrary commands as the user by
definition; the variable is a configuration point, not a privilege
boundary.

**Dynamic information — exactly one bit.** The waiter never relays message
content, and no output of the configured CLI can reach the session through
it. The only dynamic information this hook can convey is *that* deliveries
are pending (and, in its fixed-format failure notes, a numeric exit
status). Every string it can emit is a compiled-in literal visible in its
source — the same property the reminders have. Message *content* enters
the session only when the model explicitly fetches it from the peer inbox
via its own tool calls, in tool-result position, subject to the
coordination service's own controls — never as hook-injected context.
This is deliberate: it keeps the wake waiter's own nudge — like the
reminders it shares a turn-opening position with — a fixed literal, and it
keeps untrusted message text out of the turn-opening position entirely.
(The gate's block message is a different surface, held to its own
separately-stated guarantee described in the trust-classes note above — it
never opens a turn at all; it is stderr fed back after a blocked tool call.)

## Context injection (reminders)

The text the three reminder hooks inject is the fixed literals visible in
their scripts, plus exactly one interpolated value: the
`AGENT_SESSION_LABEL` environment variable, echoed back by
`role_binding_reminder.js` so the reminder can name the label the session
was launched with. It is JSON-escaped on the way out, it originates in the
operator-controlled process environment, and it is the only non-literal
character any hook here can place in injected context.

No reminder relays message content, search results, or any other dynamic
data. Whatever knowledge-base, messaging, or role-binding mechanism a
project uses, its *content* and its *state* reach the model only through the
model's own tool calls or the wake path described above, each subject to its
own controls; the reminders only note such mechanisms may exist. In
particular `role_binding_reminder.js` performs no lookup of any kind — it
does not query, confirm, or repair a role binding, and cannot report whether
one is stale. It states that local labels and external bindings are separate
things, and leaves verification to the session's own tools.

## Deployment context (disclosed so nothing is discovered later)

This plugin hardcodes no deployment specifics: it names no knowledge base,
messaging system, or CLI, and every hook is a silent no-op in any project
that has not opted in via the environment variables above. In the
deployment it was built for, the mechanisms involved are a **localhost
FastAPI bridge process backing MCP tools** (inter-session messaging), a
**local Postgres-backed knowledge-base service**, and a **per-deployment
coordination CLI** — all same-user local processes, none reachable from off
the machine. The reminders and the gate never invoke any of them. The wake
waiter, when armed, invokes exactly one of them: the coordination CLI named
by `AGENT_WAKE_CLI`, whose blocking `wake` verb is the delivery-wait
described above. That CLI (and the service behind it) is a separate,
independently reviewable component; this plugin's contribution is the
fixed-argv invocation and a fixed nudge on its wake signal, nothing more —
the CLI's output never reaches the session through this plugin.

One property of that CLI/service is worth disclosing here rather than
assuming out of scope: message attribution in the coordination service is
**advisory, never authenticated identity**. A message's stated sender is
populated from information the sending process supplies about itself;
nothing in the delivery path independently verifies that claim against
anything the sender could not also present as its own. Any local process
running as the same OS user as a legitimate sender can therefore construct
a message a receiver cannot distinguish from a genuine one — the same
single-user, cooperating-peers boundary this page draws everywhere else,
applied to who a message says it is from rather than to what a hook can
do. None of the five hooks in this plugin read, send, or attribute a peer
message themselves — the messaging path is entirely outside this plugin's
own surface — so the guarantee to carry forward is precisely this: treat a
message's stated sender as advisory, never as authenticated identity,
wherever that CLI/service's output reaches a session.

## Transport asymmetries (MCP vs. non-MCP/watch)

This plugin's own behavior does not depend on transport — every hook above
runs identically regardless of which transport the session's coordination
layer uses. Three capabilities belong to the *coordination layer itself*,
not to this plugin, and are disclosed here so a review of this deployment
does not discover any of them as a gap instead of reading it on this page.
A candidate only earns a row once the two transports are actually shown to
diverge; a capability that behaves the same on both is not listed here at
all — this section is not a complete inventory of every capability that was
checked, only of the ones confirmed to differ — plus one further candidate,
below, that was checked, found equivalent, and is kept rather than silently
dropped because it was previously listed here as an open provisional row.

**Backend agent-thread dispatch — confirmed, MCP-only mechanism.** Opening
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

**Delivery durability across a coordination-service cutover — checked,
confirmed equivalent on both transports.** A message sent to a session
shortly before the coordination service restarts or swaps is durably
retrievable by that session afterward. Measured against a live blue-green
cutover (2026-08-02, ~220s): a live MCP session and a `watch`-armed
non-MCP witness both kept their pre-fire `agent_instance_id` across the
swap with no re-claim, and both had two marked messages sent to them
before the fire (one silent, one IMPORTANT) — all four confirmed durably
present, at their original message ids, via the peer inbox after cutover.
Both transports behaved identically, so this is not an asymmetry; it is
kept here, reclassified rather than silently dropped, because it was
previously listed as an open provisional row and a reader who saw it there
should see it closed, not vanished. One distinct question remains
genuinely unmeasured, stated as a bound rather than folded into the
confirmed result: whether a live push *notification* in flight at the
exact reconnect instant is itself delivered — separate from the
underlying message record, which is durable regardless of notification
delivery. This measurement's own markers happened to be sent and
delivered before the restart began, an artifact of this run's timing, not
a proof either way; a future run could deliberately arm a marker
mid-cutover to test that instant directly. No claim of "always survives"
is made — only what was measured.

**Peer enumeration — confirmed.** Discovering which peer sessions are
currently registered is a capability of the MCP transport only. A session on
a non-MCP transport reaches the platform solely through
`homunculus call <process_key>`, and no registered process returns the peer
registry: the CLI exposes no `peers` subcommand, semantic discovery over the
knowledge base surfaces no peer-registry verb, and `peer_list` exists only as
an MCP tool. Sending is unaffected — `peer_send_by_name` resolves and
delivers identically on both transports — so the asymmetry is in discovery,
not delivery: a non-MCP session can address a known peer or role by name but
has no peer list available to it and cannot enumerate who is currently live.
One adjacent verb, `list_bridges`,
returns a well-formed, successful, and incorrect result (`0 bridge(s)
tracked` against a live multi-session fleet) rather than an error; treat it
as unreliable for this purpose, not as a substitute for peer enumeration. A
fix is scoped but not built: three thin CLI subcommands
(`homunculus inbox`, `homunculus peers`, `homunculus whoami`) over routes
that already exist would close this gap; none of the three exist today.

**Idle-session wake — confirmed.** A session on the MCP transport that goes
idle with nothing pending does not wake on its own — there is no waker for
that transport, so a truly idle MCP session never turns again without
something external driving it back open. A non-MCP (`watch`) session's
Stop-hook waiter (`wake_waiter.js`, this plugin) reopens the session on
delivery instead. The asymmetry is in **waking**, not in delivery: a
message sent to an idle session is durably received on either transport;
what differs is whether receiving it also resumes the session's turn.
Measured 2026-08-02 (a live coordination walk): negative control (idle,
nothing pending) run first on both arms — clean on both; an IMPORTANT
delivery addressed directly to the MCP arm's own instance produced no
unsolicited turn within 60s, while the same delivery to the non-MCP arm
woke the session at 55.6s. No fix is scoped for this row; it is disclosed,
not remediated.

Other capability comparisons across transports are ongoing; of the entries
above, three (backend agent-thread dispatch, peer enumeration, idle-session
wake) are the only confirmed or provisional divergences so far, and the
cutover-durability entry is a checked, closed equivalent kept for
transparency rather than an open item.
An absence from this section reflects the measurement record at time of
writing, not a guarantee that no further asymmetry will ever surface — this
section is maintained as that record changes, and a future divergence gets
its own row rather than a silent gap.

## Threat model and its boundary

Hooks run as the same OS user as the Claude Code session that invokes them.
There is **no privilege boundary** here to defend, and the plugin claims
none: the git-mutation guard is **peer mistake-prevention** — it stops
trusted, cooperating sessions from *accidentally* mutating git state or
spawning subagents that would.

**What the guard blocks, stated at its actual scope.** It blocks every git
mutation issued from an opted-in session, regardless of which directory or
repository the command targets, as mistake-prevention steering — not
"this repository's git mutations." Its two mechanisms differ in reach, and
that difference is disclosed here rather than left for a reviewer to find:
the write-protection check (`Edit`/`Write`/`MultiEdit`/`NotebookEdit`) is
scoped to this repository's own `.git/` via `CLAUDE_PROJECT_DIR`, but the
Bash-command check has no such scoping at all — it inspects the command
text itself and blocks a mutating `git` invocation regardless of which
repository, or which directory on the machine, the command's working
directory targets. A session opted into the guard for this repository is
therefore also blocked from git-mutating *any other* repository on the
machine via Bash, for as long as it stays opted in (`GIT_CONTROLLER_NAME`
set in its process environment). That is disclosed by name, not fixed: it
is the steering working as intended — route the mutation through the
controller session, or through a per-session exemption — not a bug to
narrow away. One boundary worth stating plainly: a `PreToolUse` hook only
ever sees a **model-typed** tool call; a real subprocess `git` invocation
spawned outside the model's own tool-use turn never reaches the guard at
all, on either mechanism.

A session that wants to bypass the guard can edit
the hook file; that is inside the accepted threat model, not a gap in it.
Deliberately out of scope (enumerated in the gate's docstring): shell
obfuscation (ANSI-C quoting, locale quotes, backslash-newline), runtime
script-source escapes (pipe-to-shell, here-docs), and substitution-output
content.

**Over-blocking, the opposite direction from the escapes above.** The Bash
check scans the command's text for a `git` token, not its execution
structure — a command that merely mentions `git` inside a quoted argument
(a heredoc body, a string literal, prose describing an earlier refusal) can
be blocked even though it performs no git operation. This is the
fail-closed side of the same trade the escape paragraph prices: the
tokenizer cannot reliably distinguish command position from quoted data,
and the gate prefers a false block — friction, with a workaround — over a
false allow, which would be an unmediated git mutation. Measured twice in
practice. Workaround: write such content to a file and reference the file,
rather than embedding it in an inline heredoc or quoted literal passed to
Bash.

**Identity caveat, stated plainly**: the calling session's name is read
from `~/.claude/sessions/<pid>.json`, a user-writable file. Any local
process of the same user could alter it. That is acceptable at this scope
for the same reason the edit-the-hook bypass is: within a single user
account, cooperating sessions are trusted; the gate exists to prevent
mistakes, not malice. That same identity value, together with a slice of
the caller's own command, file path, or subagent-type argument, is what
the gate's block message echoes back to the caller when it blocks (the
trust-classes note above) — always the caller's own already-supplied
input, never another session's.

## Failure modes

- **Reminders**: any failure (unset guard variable, malformed stdin) exits
  `0` with no output — a silent no-op, except `step_zero_reminder.js`,
  which has no guard variable at all and is unconditionally armed. None of
  the three can block anything: none use exit code `2`.
- **Wake waiter**: disarmed → immediate exit `0`. Armed, it is a
  deliberately long-running hook (registered `asyncRewake` with a 24-hour
  timeout): it blocks at zero model cost while the session is idle. If the
  configured CLI cannot be spawned it exits `0` with a one-line stderr note
  — a broken wake path degrades to "messages wait for the next turn"
  (where the reminders prompt an explicit check), never to a stuck session.
- **Gate**: per Claude Code's hook contract, only exit code `2` blocks; any
  other exit is non-blocking. The gate therefore owns its error handling
  explicitly and **allows on unexpected error**. Rationale: at
  mistake-prevention scope, a hook bug should not break trusted peers'
  workflows; fail-closed is the wrong trade when the "attacker" is a typo.
  Blocking decisions are made only on affirmatively parsed evidence.
- The hooks are stateless and idempotent — no temp files, no ordering
  dependencies, and no state carried between invocations (the only files
  ever produced are the interpreter's own `__pycache__` byte-code artifacts
  disclosed above, which the plugin never reads).

## Verification — running the claims on this page

The claims above are executable. From this directory, on any machine with the
`node` and `python3` the plugin already requires:

```
python3 tests/run_all.py
```

No repository, no network, no configuration, and no environment variables are
needed; the suite creates nothing outside a temporary directory it removes.

**Where the suite is present.** `tests/` accompanies the review and development
copies of this plugin. Distribution builds prune every `tests/` directory, so a
plugin installed from a distribution will not contain it and the paths cited
below will be absent there — the shipped hooks are byte-identical either way,
with the test files simply not carried. To verify a distributed copy, take the
suite from the plugin's source repository and run it against that copy.
It adds no dependency the plugin does not already have — the harness is Python
standard library and drives the Node hooks as processes, exactly as Claude Code
invokes them.

| Claim on this page | Where it is proved |
|---|---|
| Exactly one subprocess exists in the plugin | `tests/manifest_consistency_smoke.py` |
| No network I/O in any hook | `tests/manifest_consistency_smoke.py` |
| No hook writes a file as an action of its own | `tests/manifest_consistency_smoke.py` and `tests/wake_waiter_smoke.py` |
| Supply chain is stdlib/built-ins only | `tests/manifest_consistency_smoke.py` |
| `hooks.json` is exec-form with no shell in the invocation path | `tests/manifest_consistency_smoke.py` |
| The hook inventory on this page matches the tree | `tests/manifest_consistency_smoke.py` |
| Injected context is a fixed literal, with `AGENT_SESSION_LABEL` the one exception | `tests/reminder_hooks_smoke.py` |
| That one interpolated value is JSON-escaped and cannot restructure the output | `tests/reminder_hooks_smoke.py` |
| Reminders are default-off and degrade to a silent no-op | `tests/reminder_hooks_smoke.py` |
| Reminders can never block a session (never exit 2) | `tests/reminder_hooks_smoke.py` |
| The wake waiter discards the child's output unread | `tests/manifest_consistency_smoke.py` and `tests/wake_waiter_smoke.py` |
| The wake waiter conveys exactly one bit, via a compiled-in nudge | `tests/wake_waiter_smoke.py` |
| The wake waiter's argv is fixed, with no shell | `tests/manifest_consistency_smoke.py` |
| A broken wake path never traps the session | `tests/wake_waiter_smoke.py` |
| Four of five hooks are default-off behind an environment guard; `step_zero_reminder.js` is unconditionally armed by design | `tests/reminder_hooks_smoke.py` and `tests/wake_waiter_smoke.py` |
| The git-mutation guard blocks every mutating git invocation (direct, shell-wrapped, chained, path-qualified) for a non-controller session, allows it for the controller, and is fail-open when its env var is unset | `tests/git_controller_gate_smoke.py` |

One limit, stated so the coverage is not read as wider than it is:

- The no-network, no-file-write, one-subprocess and supply-chain checks are
  **source-level**: they prove the code never names the primitive, which is what
  makes those claims auditable by reading. They are not a syscall trace.
  `git_controller_gate_smoke.py` is the exception — its Layer B cases drive the
  hook as a real subprocess with synthetic stdin, exercising its actual
  allow/block decisions rather than reading its source.

## Supply chain

The reminders are plain Node scripts; the gate is stdlib-only Python. No
third-party packages, no install step, no vendored or compiled artifacts,
no interpreter downloads — the runtimes used are the ones Claude Code
already requires. Hook registration (`hooks.json`) uses exec-form
invocation (`command` + `args` array): no shell string interpolation
anywhere in the invocation path.

## Configuration surface

Four of the five hooks are default-off; the fifth, `step_zero_reminder.js`,
is unconditionally armed — installed means armed, no environment condition
at all, since a silently disarmed awareness reminder is the failure this
specific hook exists to prevent. Of the other four: with `AGENT_SESSION_ID`
unset, `check_messages_reminder.js` and the wake waiter are silent no-ops;
with `AGENT_SESSION_LABEL` unset, `role_binding_reminder.js` is a silent
no-op; with `GIT_CONTROLLER_NAME` unset, the gate allows everything.
Unrelated Claude Code sessions on the same machine are unaffected by any of
the four opt-in hooks unless an operator deliberately opts a session in.
