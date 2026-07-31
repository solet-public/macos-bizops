# coordination-hooks

Fleet coordination hooks for multi-session Claude Code workflows:

- **Knowledge-base-first reminder** (`UserPromptSubmit`) — suggests checking
  this project's documented conventions before non-trivial work. A reminder,
  not an enforced gate: the underlying search this suggests is asynchronous,
  so the hook cannot block on its result.
- **Check-your-messages reminder** (`SessionStart` + `UserPromptSubmit`) —
  a plain nudge to check for unread coordination messages. Intentionally has
  no backend dependency: it always fires the same reminder and lets the
  model's own tool call determine whether anything is actually there.
- **Role-binding reminder** (`SessionStart`) — notes that a session's local
  label and any external, durable role binding are separate things that can
  disagree, so a binding made before a `/clear`, a restart, or a transport
  reconnect may still point at a previous session. Performs no lookup: it
  cannot tell whether a binding is actually stale, and deliberately does not
  try. It echoes the session's own `AGENT_SESSION_LABEL` and leaves both
  verification and re-assertion to the session's own tools.
- **Idle-wake waiter** (`Stop`) — opt-in, nudge-only. When a labeled
  session goes idle, invokes exactly `$AGENT_WAKE_CLI wake` (fixed argv, no
  shell) — the operator-configured coordination CLI's blocking wait verb —
  and **discards its output entirely**. On the wake signal (exit 2) it
  emits its own fixed nudge: deliveries are pending in the peer inbox. The
  session then fetches the actual messages itself via the normal tool
  path. The hook conveys exactly one bit of dynamic information —
  "deliveries pending" — plus timing; it never relays message content.
- **Git-mutation guard** (`PreToolUse`) — opt-in, nameable gate that blocks
  git mutations (commit, push, checkout, merge, rebase, etc.) and subagent
  spawning (the `Task` tool) from any session other than a designated
  controller session. Scope is peer *mistake* prevention, not adversarial
  security — a session that wants to bypass the hook can edit it.

## Configuration

Every hook is guarded on the `AGENT_SESSION_LABEL` environment variable and
is a silent no-op when it is unset, so unrelated Claude Code sessions on the
same machine are never affected.

The git-mutation guard is separately opt-in via `GIT_CONTROLLER_NAME`: unset,
the guard is fully OFF (every session may run git and spawn subagents).
Set it to the session `name` (per Claude Code's `/rename`) that should be
allowed to perform git mutations and spawn subagents; every other session is
blocked.

The idle-wake waiter is separately opt-in via `AGENT_WAKE_CLI`: unset, it is
fully OFF. Set it to the coordination CLI the hook should invoke; the hook
runs exactly that executable with the single fixed argument `wake`, no shell
involved. A declared non-watch transport (`FLEET_TRANSPORT` set to anything
other than `watch`) disarms it even when the CLI variable is set, so
deployments whose live bridge connection already does the waking never
double-arm.

## Design notes

- Every hook uses exec-form invocation (`command` + a separate `args` array)
  — no shell string interpolation, no injection surface.
- The three reminder hooks are plain Node scripts with no dependency beyond
  Node itself, which Claude Code already requires to run — no interpreter
  discovery, no vendored binary, no runtime install step.
- The git-mutation guard is Python, stdlib-only, and never executes
  target-controlled code — it only inspects the tool-call payload Claude
  Code already passes on stdin.
- No hook calls out to any external service or database. The reminders and
  the gate execute nothing at all; the wake waiter executes exactly one
  operator-configured local command (`$AGENT_WAKE_CLI wake`, fixed argv, no
  shell), discards its output, and emits only its own compiled-in text.
  Every string this plugin can ever inject into a session is a fixed
  literal visible in its source, with one disclosed exception: the
  role-binding reminder interpolates the session's own
  `AGENT_SESSION_LABEL` into its text (JSON-escaped, read from the process
  environment — never from stdin or message content). They are guardrails
  and plumbing, not a security boundary — see the hooks' own docstrings for
  the precise scope of each one.
- Reminder text is phrased as factual statements about project state
  ("unread messages may be pending"), never as imperative commands
  ("check your messages now"). The Claude Code hooks documentation
  ("Add context for Claude") states that text framed as out-of-band
  system commands can trigger Claude's prompt-injection defenses, which
  causes the text to be surfaced to the user instead of treated as
  context — factual framing avoids that failure mode.
- Reminder text names no project-specific mechanism (no fixed knowledge-base
  tool, no fixed messaging system) and is written as a condition ("if this
  project uses X") rather than an assumption that X exists — a project that
  has no such mechanism gets a harmless no-op statement instead of a
  reference to something Claude has no way to act on.
- The gate resolves the calling session's name from
  `~/.claude/sessions/<pid>.json`, a user-writable file — deliberate at
  mistake-prevention scope, and stated plainly in `SECURITY.md`.

## Security notes

`SECURITY.md` (same directory) enumerates the complete data-flow surface —
inputs, outputs, no-network/no-secrets/no-subprocess guarantees, the
threat-model boundary and its accepted bypasses, failure modes with the
allow-on-error rationale, and the supply-chain posture. It is written to be
read by a security reviewer before the code.
