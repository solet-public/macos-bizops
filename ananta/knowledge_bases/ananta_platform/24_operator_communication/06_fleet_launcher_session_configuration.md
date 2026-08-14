# Fleet Launcher Session Configuration: Model, Effort, Advisor, and Transport Per Role

Tags: knowledge:tag:fleet_launcher, knowledge:tag:operator_communication, knowledge:tag:cli_configuration, knowledge:tag:advisor_tool, knowledge:tag:fleet_transport

Article Layer: 2

Article Role: operations_reference

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-reference, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: How to change the base model, effort level, advisor model, and declared transport (MCP vs watch) for one named role in an operator's multi-session fleet launcher — where that launcher actually lives, the CLI flags and environment variables involved, the one verified-correct way to fully disable the advisor tool for a single session, where the fleet-wide default transport is declared and how a per-role launcher export overrides it, why solet launchers deny the structured-choice AskUserQuestion prompt by default and the per-launch override that lifts it, why spawn_session's tmux host driver carries that same deny (with its own allow_askuserquestion override) while headless needs none — the tool is inert there by construction, and how a long-running session clears its own context by delegating a helper session to type the clear command and the resume prompt into its own terminal surface.

**When you need this**: an operator asks to change the model, effort level, advisor configuration, or transport for a named session role (a coordinator, a reviewer, an implementation lane, any role launched by a wrapper function); a session needs to know where that configuration actually lives before searching for it in the wrong place; a session needs to turn the advisor tool off for one role without touching the operator's global default; a session needs to know why one role talks over MCP while another talks over the watch transport, or wants to change which one a role uses; a session wants a spawn_session-spawned worker to be able to use AskUserQuestion (or wants to confirm why one can't); a session's context has grown long and it needs to clear itself and continue working without waiting for the operator.

---

## Where this configuration lives

A multi-session fleet — several concurrently-running coding-agent sessions, each addressable by a role name (a coordinator, a git-controller, reviewers, implementation lanes) — is started by a **launcher that lives in the operator's own shell profile** (typically `~/.zshrc` or equivalent), not inside the platform repository and not inside any plugin's hydration templates. Searching the repo's own tree for this configuration will not find it; it is a machine-local file the operator maintains directly.

The shape is one shared, parameterized function plus one thin wrapper per role:

```sh
_claude_for_<solet>() {
  local role="$1"
  local model="$2"     # optional CLI model alias; empty = the operator's ~/.claude/settings.json default
  local effort="$3"    # optional effort level (low|medium|high|xhigh|max); empty = settings.json default
  # ... exports role-identity env vars, builds --model/--effort flags, execs `claude`
}

claude-<role>() { _claude_for_<solet> <Role-Label> <model> <effort> }
```

A **restart variant** of the same table (kill any running session under that role label, then relaunch) is common alongside the primary launcher functions. The restart wrappers must be edited in lockstep with the primary ones — the two tables silently disagree if only one is updated, and the disagreement only surfaces the next time someone restarts a role instead of freshly launching it.

A seed-hydration plugin may ship a *template* for producing this launcher for a brand-new clone (offered during first-run hydration, before an operator has a live launcher of their own). That template is the wizard that writes the file at birth; it is not the place to look for or edit an existing, already-hydrated operator's live configuration. If a launcher already exists, edit it directly.

## Model and effort

Both are ordinary per-session CLI flags on the `claude` invocation: `--model <alias>` and `--effort <level>` (`low`, `medium`, `high`, `xhigh`, `max`). Passing neither flag inherits the operator's `~/.claude/settings.json` defaults (`model` and `effortLevel` keys). A launcher function typically builds these as optional flags — an empty argument expands to zero flags rather than an empty-string flag value — so existing callers that don't pass a model/effort stay byte-identical after adding the parameters.

## Transport per role

Each session talks to the platform over one of two transports: **MCP** (a live bridge connection; requires `claude mcp add` and, on some tiers, Anthropic-direct auth — unusable on a machine whose policy blocks MCP) or **watch** (a per-session `solet watch` watcher plus a zero-token `Stop`-hook wake; no MCP registration needed). The fleet's charter (2026-08-06, operator, verbatim in the fleet-watch-transport-migration lane brief): corporate deployments disallow MCP, so **watch must be the fleet's primary transport**, MCP retained only as a backup and for chat-class sessions.

The fleet-wide default is declared in exactly ONE place: `default_fleet_transport` in `agent_messaging_plugin`'s `plugin.yaml` (shipped `"watch"`, the charter's own value — same declared-config posture as `headless_permission_mode`, changeable with a config edit and a routine blue-green swap, no launcher edit required to move the fleet-wide default).

That config value is the *source of truth for the default*; it is **not** what an individual session reads at launch. Each session reads its own `FLEET_TRANSPORT` environment variable directly (the rename skill, the spawned-worker hook guards, and — on Claude sessions — the watch-arm's `wake_waiter.py` Stop hook all read it independently — declared, never probed, never silently crossed; see the rename skill for the full rule). Codex sessions currently have no Stop-hook reader of this variable at all: stock Codex does not execute async command hooks on any measured build, so the coordination plugin ships no `Stop` binding (codex-0147-async-hook-regression, 2026-08-13) — a Codex session's watch-arm delivery notice comes only from `drive_on_delivery` for managed workers, or a manual `peer_inbox` drain otherwise. The goal state is that **every role's launcher wrapper exports `FLEET_TRANSPORT` explicitly**, per role, rather than relying on any consumer's own unset-fallback literal:

```sh
claude-<role>() { _claude_for_<solet> <Role-Label> <model> <effort> watch }
# a role that still needs MCP (chat-class, or not yet migrated):
claude-<role>() { _claude_for_<solet> <Role-Label> <model> <effort> mcp }
```

Declared beats fallback: once every role's launcher line names its transport explicitly, the various `${FLEET_TRANSPORT:-...}` fallback literals scattered across consumers become near-dead code — they only matter for a session launched outside any wrapper. Don't rely on them as the mechanism; treat an explicit per-role export as the actual configuration surface, the same way model and effort are.

**Mixed fleet during a migration — explicit exports are what make a fallback flip safe.** A consumer's own unset-fallback literal (e.g. the Stop hook that decides whether to run `solet wake`) should only be flipped to match a new fleet-wide default AFTER every role whose launcher doesn't yet pass an explicit transport has been given one. Flipping a shared fallback first, while some standing roles still rely on it silently, risks a role that has no watcher armed exec'ing a wake command against a spool nothing feeds — a wedged turn-end, not a clean failure. Sequence it: pin explicit per-role exports first, flip shared fallbacks last, never the reverse.

## The advisor tool

The advisor is a second, typically-stronger model Claude Code consults mid-task (before committing to an approach, on a recurring error, before declaring work done). It is configured three ways, each overriding the previous for the current session only:

1. **`advisorModel` in `settings.json`** — a persistent default (user-scope, project-scope, or passed via `--settings`).
2. **`/advisor <model>`** — an interactive slash command; changes and persists the setting mid-session.
3. **`--advisor <model>`** — a session-only CLI flag. It is intentionally **not listed in `claude --help`** (a hidden flag) but is documented on Anthropic's site (linked below). It takes precedence over the `advisorModel` setting for that one session, and exits with an error if the session's main model does not support the advisor or the requested advisor model is excluded by an organization allowlist.

All three of these **enable or repoint** the advisor — none of them can turn it off for one session while a persistent `advisorModel` default is set elsewhere (e.g. in a global user-scope `settings.json` that every session inherits from). `--advisor off` is rejected outright (`off` is not a valid model). This matters for a fleet launcher specifically: the global default is usually set once, fleet-wide, and most roles should inherit it — but a role on a model tier the operator wants advisor-free needs a real disable, not a same-session override that the persistent default will keep re-asserting on the next launch.

### Turn the advisor off for one session — the verified-correct way

```sh
CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1 claude ...
```

This environment variable is the **officially documented** disable path (Anthropic's advisor docs, "Turn the advisor off" section — see Reference below), not a workaround. With it set, the advisor tool is absent from the session entirely: the `/advisor` command becomes unavailable, and any configured `advisorModel` is ignored. The `--advisor` flag is still *accepted* (so a launcher that unconditionally passes it doesn't error) but has no effect.

Verified empirically before relying on it: a baseline session (inheriting a global `advisorModel` setting) reports the advisor tool present when asked directly; the identical launch with `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` set reports it absent. In a fleet launcher, gate this on a fourth positional argument to the shared launch function (e.g. `"off"` exports the variable, anything else unsets it) so per-role advisor state is explicit in the same table as model and effort, and forward that same argument through the restart-variant wrappers.

### Two things that look like they should disable it, and do not

Both were tried and empirically confirmed ineffective — worth knowing before spending time re-deriving this:

- **`--settings '{"advisorModel": null}'`** (or any other value, including a different valid model alias) has **no effect** when a persistent `advisorModel` already exists in a settings source that loads after `--settings` in the merge order (e.g. the operator's own user-scope `settings.json`). The CLI-supplied value is silently superseded, not merged as an override, for this key.
- **`--disallowedTools advisor`** produces `Permission deny rule "advisor" matches no known tool` and has no effect. The advisor is a server-side tool attached to the request, not a permission-gated tool name the standard allow/deny-list mechanism recognizes.

### Fable-tier sessions and the advisor

A Fable-5 main model only accepts a Fable advisor, and Claude Code does not currently offer Fable 5 as an advisor option at all (an Anthropic-side rollout gates when it returns). A Fable-tier role therefore runs advisor-less today regardless of configuration — but that is incidental, rollout-dependent behavior, not a stated setting. If a Fable-tier role is meant to run without an advisor, still pass the explicit disable (`CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` / the launcher's `"off"` argument) rather than relying on the current rollout state, so the role's behavior doesn't silently change the day Anthropic turns Fable-as-advisor on.

## Structured-choice prompts (`AskUserQuestion`) — denied by default

Claude Code's `AskUserQuestion` tool renders a blocking multiple-choice picker
and holds the session until a human answers — its auto-continue timeout
defaults to `"never"`. In an attended chat that is a feature; in a fleet it is
a stall: a worker, an unattended seat, or any peer-driven session that raises
the picker stops processing driver-channel turns and peer wakes until someone
happens to look at its terminal. The operator ruled (2026-08-14) that solet
sessions deny this tool **by default**, with an explicit override for attended
sessions that want it.

**Mechanism.** `AskUserQuestion` is a permission-gated tool name, so a
standard permissions deny works (unlike the advisor — see above — which is
not). The session launcher passes
`--settings <clone>/client/claude-session-overlay.json`, whose only content is
`{"permissions": {"deny": ["AskUserQuestion"]}}`. Settings sources merge, so
the deny unions into the session without editing any file the user owns.

**Overrides, in order of reach:**

- One attended session: `SOLET_ALLOW_ASKUSERQUESTION=1 claude-<name> <label>`
  — the launcher skips the overlay flag entirely.
- As the user's standing default: remove the overlay flag lines from the
  rendered launcher and record the choice.
- Softer middle ground: keep the tool but set `askUserQuestionTimeout` (user
  settings or `/config`) so an unanswered dialog auto-continues instead of
  hanging forever.

**Doctrine regardless of mechanism:** a session working unattended never
relies on the picker; it asks in plain text, or proceeds and discloses per its
brief. The deny exists because the tool's failure mode in a fleet is silent —
the stalled session looks idle, not stuck.

### Driver-level enforcement — `spawn_session`'s tmux and headless hosts

The seed launcher's `--settings` overlay above covers operator-launched
`claude-<name>` sessions only. Workers spawned through
`plugin::agent_messaging_plugin::spawn_session` go through a different path
entirely (`session_hosts.py`'s host drivers, not the seed launcher), so the
overlay never reaches them — this needed its own enforcement site, decided
2026-08-14.

**Only the `tmux` host driver carries the deny.** It launches a real
interactive `claude` CLI (a detached tmux pane, driven by keystrokes) — the
one Claude-runtime host where the picker can actually render, so it is the
only place the stall can happen. `tmux_adapter.py`'s `--settings` JSON
carries `permissions.deny: ["Agent", "Task", "AskUserQuestion"]` by default,
the same inline-JSON mechanism (no overlay file) that already denies
Agent/Task. The `headless` host driver needs **no equivalent config at
all** — measured live (a scratch spawn with the driver's exact
`--input-format stream-json --output-format stream-json` argv, instructed to
call the tool): the session's own `init` event never enumerates
`AskUserQuestion` in its tool list, and the model's own `ToolSearch` call
confirms it is not even among the deferred tools. The tool is inert by
construction in that mode, not merely denied — adding a deny rule for it
there would be dead configuration, so `headless_adapter.py` is deliberately
untouched. (If a future reader sees the tmux/headless asymmetry and reaches
to "fix" it by adding the deny to headless too: don't — re-run the same
measurement first, since this is the reason, not an oversight.) The codex
runtime has no equivalent tool at all, so its host drivers need nothing
either.

**Per-spawn override:** `spawn_session`'s `allow_askuserquestion` parameter
(boolean, default `false`) lifts the deny for one spawn — named after the
seed launcher's own `SOLET_ALLOW_ASKUSERQUESTION=1` for cross-surface
consistency. There is no `plugin.yaml` policy knob for this, unlike
`headless_permission_mode`/`default_fleet_transport`: the 2026-08-14 ruling
fixes the global default outright, so the per-call override is the whole
mechanism.

## Clearing a session's own context — the delegated drive

A long-running session that needs to clear its own context cannot type `/clear` into
itself — but it does not need to. The sanctioned mechanism is **delegation**: the
session dispatches a helper session whose whole job is to drive the clearing session's
own terminal surface. This uses only primitives every deployment already has (session
spawning plus terminal injection), so it works identically in a managed corporate
environment; it deliberately does not depend on any vendor remote-control capability,
which such environments commonly disable.

The procedure, from the clearing session's side:

1. **Checkpoint first.** Drain pending memory writes to the canonical store, bring the
   working notes current, and make sure no other session is holding for a go-signal
   that only this session can send. A clear at an unclean checkpoint loses exactly the
   state that was not written down.
2. **Write a resume handoff note** in the working-notes directory: what is in flight,
   what the fresh context should read first, and the single next action.
3. **Dispatch a helper session** (an inexpensive model tier suffices — the task is
   mechanical) briefed to: locate the clearing session's surface (a terminal
   multiplexer pane by session name, or a terminal window by its tty), inject the
   literal `/clear`, verify it took by reading the surface back, then inject a short
   resume prompt that points at the handoff note.
4. **The second injection is the resume.** A cleared interactive session sits idle
   until someone types a turn; the helper's resume prompt is that turn, so the cleared
   session continues unattended without waiting for the operator.

Two injection rules the helper must follow: send the text and the Enter key as
separate actions and verify between them (a same-burst Enter can be swallowed), and
keep the injected prompt short, pointing at the handoff note rather than carrying the
content itself (long injected text can collapse into an unsent paste buffer).

Two further traps, both observed in the first live proof of this procedure: if the
clearing session is mid-turn when the injection lands, both injected texts sit in its
pending-input queue until the turn ends, rather than executing; and when `/clear`
finally executes from that queue it can consume the queued resume prompt along with
itself, leaving the fresh session idle on an empty prompt line. The helper must
therefore verify that the fresh session is actively processing the resume prompt —
not merely that the clear took — and re-inject the resume prompt once if it is not.
The session's durable identity and any role binding survive the clear; the live proof
measured the same instance identity and an intact role binding on the far side, with
no re-registration required.

## Reference

- Anthropic — Advisor tool documentation: https://code.claude.com/docs/en/advisor (see "Enable the advisor," "Choose an advisor model," and "Turn the advisor off")
- Anthropic — CLI reference (the `--advisor`, `--model`, `--effort`, `--settings`, and `--disallowedTools` flags): https://code.claude.com/docs/en/cli-reference
