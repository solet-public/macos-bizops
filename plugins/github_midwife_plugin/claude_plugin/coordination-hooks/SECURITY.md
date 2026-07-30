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

## Inputs and outputs — the complete data-flow surface

Inputs, exhaustively:

- **stdin**: the JSON payload Claude Code passes to every hook (event name,
  tool name, tool input). Parsed defensively; malformed input degrades to a
  no-op (reminders) or an allow (gate).
- **Environment variables**: `AGENT_SESSION_LABEL` (reminders + wake on/off,
  and — in `role_binding_reminder.js` only — the one value the plugin
  interpolates into injected text; see the injection section below),
  `GIT_CONTROLLER_NAME` (gate on/off + controller name), `AGENT_WAKE_CLI`
  (wake waiter on/off + the command it runs), `FLEET_TRANSPORT` (disarms the
  wake waiter on non-watch transports), `CLAUDE_PROJECT_DIR` (repo root,
  used only to locate `.git/` for the write-protection check).
- **One local file read**: `~/.claude/sessions/<parent_pid>.json`, to
  resolve the calling session's name (see the identity caveat below).

Outputs, exhaustively:

- **stdout**: a JSON object whose `additionalContext` is a **fixed string
  literal** compiled into the script (reminders); nothing (gate, wake
  waiter). One exception, stated precisely: `role_binding_reminder.js`
  interpolates a single value into its otherwise-fixed literal — the
  `AGENT_SESSION_LABEL` environment variable, JSON-escaped via
  `JSON.stringify`. That value comes from the operator-controlled process
  environment; no hook interpolates anything read from stdin, from a file,
  or from any message content.
- **stderr**: a static block-explanation message when the gate blocks; the
  wake waiter's **fixed** nudge or fixed-format failure note (the numeric
  exit status is the only variable part; the child's own output is
  discarded unread).
- **Exit codes**: `0` (allow / no-op) or `2` (gate block; wake signal).

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

**Arming.** Three conditions, all required: `AGENT_SESSION_LABEL` set,
`AGENT_WAKE_CLI` set, and `FLEET_TRANSPORT` unset-or-`watch`. Any ordinary
Claude Code session — no label, no CLI variable — gets a silent no-op.

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
This is deliberate: it keeps the plugin's entire injectable surface
static, and it keeps untrusted message text out of the turn-opening
position.

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

## Threat model and its boundary

Hooks run as the same OS user as the Claude Code session that invokes them.
There is **no privilege boundary** here to defend, and the plugin claims
none: the git-mutation guard is **peer mistake-prevention** — it stops
trusted, cooperating sessions from *accidentally* mutating git state or
spawning subagents that would. A session that wants to bypass it can edit
the hook file; that is inside the accepted threat model, not a gap in it.
Deliberately out of scope (enumerated in the gate's docstring): shell
obfuscation (ANSI-C quoting, locale quotes, backslash-newline), runtime
script-source escapes (pipe-to-shell, here-docs), and substitution-output
content.

**Identity caveat, stated plainly**: the calling session's name is read
from `~/.claude/sessions/<pid>.json`, a user-writable file. Any local
process of the same user could alter it. That is acceptable at this scope
for the same reason the edit-the-hook bypass is: within a single user
account, cooperating sessions are trusted; the gate exists to prevent
mistakes, not malice.

## Failure modes

- **Reminders**: any failure (unset guard variable, malformed stdin) exits
  `0` with no output — a silent no-op. They cannot block anything: they do
  not use exit code `2`.
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
| Every hook is default-off behind its environment guard | `tests/reminder_hooks_smoke.py` and `tests/wake_waiter_smoke.py` |

Two limits, stated so the coverage is not read as wider than it is:

- The no-network, no-file-write, one-subprocess and supply-chain checks are
  **source-level**: they prove the code never names the primitive, which is what
  makes those claims auditable by reading. They are not a syscall trace.
- The git-mutation guard's own behaviour (which commands it blocks) is covered
  by a separate 143-case suite that currently lives in the repository this
  plugin is developed in, not in this directory. The tests here cover its
  registration, its imports, and its documentation — not its blocking rules.

## Supply chain

The reminders are plain Node scripts; the gate is stdlib-only Python. No
third-party packages, no install step, no vendored or compiled artifacts,
no interpreter downloads — the runtimes used are the ones Claude Code
already requires. Hook registration (`hooks.json`) uses exec-form
invocation (`command` + `args` array): no shell string interpolation
anywhere in the invocation path.

## Configuration surface

Everything is default-off. With `AGENT_SESSION_LABEL` unset the reminders
are silent no-ops; with `GIT_CONTROLLER_NAME` unset the gate allows
everything. Unrelated Claude Code sessions on the same machine are
unaffected unless an operator deliberately opts a session in.
