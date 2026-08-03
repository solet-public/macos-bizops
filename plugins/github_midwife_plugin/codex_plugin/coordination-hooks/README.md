# coordination-hooks for Codex

This is the stock-Codex sibling of the Claude coordination plugin. The current
increment contains:

- fixed `UserPromptSubmit` and `SessionStart` context reminders;
- the WS-4b.1 `PreToolUse` Bash Git-Controller gate;
- the WS-4b.3 synchronous `Stop` wake waiter.

The sibling hydration package now renders the stock-Codex launcher-owned
watcher, repo marketplace, and exact durable-inbox/paging instructions. Those
artifacts are outside this hook bundle so hydration can render deployment names
and paths without changing the plugin bytes. Their externally addressed live
acceptance, lifecycle matrix, and the patched-binary cutover remain WS-4b.4 and
WS-4b.6 retirement gates. Trust revocation is delivered as an explicit operator
action in Codex's supported `/hooks` surface; this package deliberately does
not rewrite Codex's private trust state behind that review boundary.

## Hook inventory

| event | script | behavior |
|---|---|---|
| `UserPromptSubmit` | `step_zero_reminder.js` | fixed project-orientation reminder |
| `UserPromptSubmit` | `check_messages_reminder.js` | fixed unread-coordination reminder |
| `SessionStart` (`startup`, `resume`, `clear`) | `check_messages_reminder.js` | the same fixed unread-coordination reminder |
| `SessionStart` (`startup`, `resume`, `clear`) | `role_binding_reminder.js` | fixed label-versus-role reminder |
| `Stop` | `wake_waiter.js` | synchronous, nudge-only idle-wake waiter |
| `PreToolUse` (`Bash` only) | `git_controller_gate.py` | opt-in Git-Controller mistake-prevention gate |

`step_zero_reminder.js` is unconditionally armed — installed means armed,
with no environment condition (§7 re-key, 2026-08-02; parity with the Claude
sibling's `2fb49dbf2`). `check_messages_reminder.js` keys on
`AGENT_SESSION_ID` — identity, not label, since the inbox it points at is
keyed on identity; the role-binding reminder keys on `AGENT_SESSION_LABEL`,
because the label is its content. Their `additionalContext` is compiled into
the scripts and is independent of the prompt, hook payload, role, label,
inbox state, and message content.

Every `additionalContext` string in this plugin is byte-fixed and generic.
Deployment-specific commands, process keys, and local tool guidance belong in
the project's native `AGENTS.md` instruction surface, which Codex loads without
introducing a second environment-authored prompt channel.

The `Stop` waiter arms only when `AGENT_SESSION_ID` and `AGENT_WAKE_CLI` are
non-empty and `FLEET_TRANSPORT` is exactly `watch`. It parses Codex's
`stop_hook_active` loop guard, runs exactly `AGENT_WAKE_CLI wake` without a
shell, and discards the child's streams unread. Exit `2` from that CLI produces
one fixed factual continuation nudge; exit `0` produces no continuation. The
manifest's 86,400-second synchronous bound is longer than the CLI's 86,100-
second default wait, so ordinary idle expiry is owned by the CLI rather than a
hook-timeout cancellation.

The Bash gate is separately opt-in through `GIT_CONTROLLER_NAME`. It authorizes
only from `AGENT_ROLE`; labels, session IDs, hook thread IDs, and runner identity
cannot grant controller authority. Native edit and delegation tools stay
unrouted until their live stock-Codex payloads are captured and reviewed.

## Repo-marketplace install and trust

The repository marketplace is `.agents/plugins/marketplace.json` at the clone
root. Its local source points directly to this directory, so the reviewed source
and marketplace source are one tree.

From this checkout, using the supported stock binary during validation:

```sh
/opt/homebrew/bin/codex plugin marketplace add /Users/alice/Workspace/homunculus --json
/opt/homebrew/bin/codex plugin add coordination-hooks@homunculus-development --json
```

Installation and enablement do not grant hook execution. Start a fresh stock
Codex process, open `/hooks`, inspect each command, matcher, and timeout, then
trust only the expected definitions. New or changed definitions remain inactive
until reviewed. Do not copy another host's `trusted_hash` values.

Use the same supported `/hooks` browser to revoke execution of one definition:
select its event, select the definition, and press Space or Enter so it is
unchecked. Stock Codex persists that definition as disabled without deleting
its reviewed hash, leaves unrelated definitions unchanged, and reports the
revoked definition as installed but inactive. Re-enable it only through the
same reviewed surface.

The seed lane must render its own repo marketplace identity and preserve the
same plugin bytes. This development marketplace does not substitute for that
seed integration or for the later installed-cache equality proof.

## Update/cachebuster procedure

Only the live Git-Controller may mutate files in the shared homunculus checkout. For a
reviewed local development update, that session runs the Codex plugin-creator
cachebuster helper against this plugin, reviews the resulting single version
suffix, and reinstalls from the existing marketplace:

```sh
python3 ~/.codex/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py \
  /Users/alice/Workspace/homunculus/plugins/github_midwife_plugin/codex_plugin/coordination-hooks
/opt/homebrew/bin/codex plugin add coordination-hooks@homunculus-development --json
```

The helper preserves the base version and replaces, rather than stacks, one
`+codex.<cachebuster>` suffix. Re-open `/hooks`: only definitions whose
normalized content changed should require fresh trust. A new Codex thread is the
pickup boundary after reinstall.

Do not hand-edit the marketplace during this update loop. Do not treat plugin
removal as trust revocation. Measured on stock Codex 0.141.0, uninstall keeps the
definition state: an enabled definition automatically becomes active after an
identical reinstall, while a definition disabled through `/hooks` remains
inactive after that same uninstall/reinstall cycle. The supported revocation
procedure is therefore: disable the intended definition in `/hooks`, verify its
event row reports the expected installed-versus-active count, and only then
remove the plugin if complete removal is also wanted. Do not automate this by
rewriting `config.toml`; stock Codex owns the parser and persistence semantics.

## Verification

The artifact carries its own offline suite:

```sh
python3 tests/run_all.py
```

The repository gate also registers each new `*_smoke.py`. Live untrusted and
trusted stock-process legs are recorded separately because direct script tests
cannot prove Codex trust enforcement.

Read [SECURITY.md](SECURITY.md) before trusting the hooks.
