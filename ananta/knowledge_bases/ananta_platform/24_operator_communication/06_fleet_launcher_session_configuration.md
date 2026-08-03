# Fleet Launcher Session Configuration: Model, Effort, and Advisor Per Role

Tags: knowledge:tag:fleet_launcher, knowledge:tag:operator_communication, knowledge:tag:cli_configuration, knowledge:tag:advisor_tool

Article Layer: 2

Article Role: operations_reference

Article Tags: planning-stage:homunculus-lifecycle, evidence-category:operations-reference, domain:local-homunculus, domain:client-deployment, consumer_profile:both

Embedding Description: How to change the base model, effort level, and advisor model for one named role in an operator's multi-session fleet launcher — where that launcher actually lives, the CLI flags and environment variables involved, and the one verified-correct way to fully disable the advisor tool for a single session, with a link to Anthropic's own advisor documentation.

**When you need this**: an operator asks to change the model, effort level, or advisor configuration for a named session role (a coordinator, a reviewer, an implementation lane, any role launched by a wrapper function); a session needs to know where that configuration actually lives before searching for it in the wrong place; a session needs to turn the advisor tool off for one role without touching the operator's global default.

---

## Where this configuration lives

A multi-session fleet — several concurrently-running coding-agent sessions, each addressable by a role name (a coordinator, a git-controller, reviewers, implementation lanes) — is started by a **launcher that lives in the operator's own shell profile** (typically `~/.zshrc` or equivalent), not inside the platform repository and not inside any plugin's hydration templates. Searching the repo's own tree for this configuration will not find it; it is a machine-local file the operator maintains directly.

The shape is one shared, parameterized function plus one thin wrapper per role:

```sh
_claude_for_<homunculus>() {
  local role="$1"
  local model="$2"     # optional CLI model alias; empty = the operator's ~/.claude/settings.json default
  local effort="$3"    # optional effort level (low|medium|high|xhigh|max); empty = settings.json default
  # ... exports role-identity env vars, builds --model/--effort flags, execs `claude`
}

claude-<role>() { _claude_for_<homunculus> <Role-Label> <model> <effort> }
```

A **restart variant** of the same table (kill any running session under that role label, then relaunch) is common alongside the primary launcher functions. The restart wrappers must be edited in lockstep with the primary ones — the two tables silently disagree if only one is updated, and the disagreement only surfaces the next time someone restarts a role instead of freshly launching it.

A seed-hydration plugin may ship a *template* for producing this launcher for a brand-new clone (offered during first-run hydration, before an operator has a live launcher of their own). That template is the wizard that writes the file at birth; it is not the place to look for or edit an existing, already-hydrated operator's live configuration. If a launcher already exists, edit it directly.

## Model and effort

Both are ordinary per-session CLI flags on the `claude` invocation: `--model <alias>` and `--effort <level>` (`low`, `medium`, `high`, `xhigh`, `max`). Passing neither flag inherits the operator's `~/.claude/settings.json` defaults (`model` and `effortLevel` keys). A launcher function typically builds these as optional flags — an empty argument expands to zero flags rather than an empty-string flag value — so existing callers that don't pass a model/effort stay byte-identical after adding the parameters.

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

## Reference

- Anthropic — Advisor tool documentation: https://code.claude.com/docs/en/advisor (see "Enable the advisor," "Choose an advisor model," and "Turn the advisor off")
- Anthropic — CLI reference (the `--advisor`, `--model`, `--effort`, `--settings`, and `--disallowedTools` flags): https://code.claude.com/docs/en/cli-reference
