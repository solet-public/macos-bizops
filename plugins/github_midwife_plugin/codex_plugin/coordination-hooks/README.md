# coordination-hooks for Codex

This is the stock-Codex sibling of the Claude coordination plugin. The current
increment contains:

- fixed `SessionStart` context reminders;
- the WS-4b.1 `PreToolUse` Bash Git-Controller gate.

**No `Stop` binding ships currently (codex-0147-async-hook-regression,
2026-08-13).** Commit 906753eb7 asserted stock Codex parses async command
hooks, "checked against 0.141.0 acceptance," and shipped an `async: true`
`Stop` handler (`wake_waiter.js`) on that basis. That assertion is contradicted
by a preserved 2026-07-31 probe
(`workbench/2026-07-31_codex_phase4_probe/evidence/async_unsupported.md`),
which already recorded 0.141.0 emitting `skipping async hook … async hooks
are not supported yet` for the identical handler shape; 0.147.0 emits the same
diagnostic now. Async command-hook support was never present in either
measured version, so 906753eb7's binding never fired. The binding and its
handler script were removed — `wake_waiter.js` and its dedicated smoke were
deleted outright, not kept dormant — rather than left registered-but-dead,
which is what produces the startup warning. Re-adding a `Stop` entry requires
first confirming async command-hook support on the target build; do not
restore the synchronous predecessor either (it blocked the turn boundary —
see SECURITY.md's Manifest execution contract section).

Codex operator delivery is therefore durable but silent: the message lands in
the durable store and the watch process's own log (redirected there by the
launcher, not the live terminal), but nothing surfaces it automatically. The
operator must explicitly drain `peer_inbox` on their own next user/model turn;
the `SessionStart` unread-coordination reminder above only fires at
startup/resume/`clear`, not per-turn. A `spawn_session`-managed worker is
unaffected: `drive_on_delivery`'s driver-channel notice is independent of this
plugin's hooks entirely and provides its turn initiation regardless. With no
`Stop` handler left to drain it, the wake-hook spool is retired end to end for
Codex: the hydration-rendered launcher arms `<name> watch --no-spool`
(stopping the watch process's own client-side tee), and the platform-side tee
that wrote the same spool path on every dispatch to a `wake_capable=False`
recipient (`_tee_spool_if_wake_incapable`,
`agent_messaging_plugin/peer_dispatch.py`) is retired in the same landing.

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
| `SessionStart` (`startup`, `resume`, `clear`) | `step_zero_reminder.js` | fixed project-orientation reminder |
| `SessionStart` (`startup`, `resume`, `clear`) | `check_messages_reminder.js` | fixed unread-coordination reminder |
| `SessionStart` (`startup`, `resume`, `clear`) | `role_binding_reminder.js` | fixed label-versus-role reminder |
| `PreToolUse` (`Bash` only) | `git_controller_gate.py` | opt-in Git-Controller mistake-prevention gate |

**Cadence ruling (2026-08-11):** `step_zero_reminder.js` and
`check_messages_reminder.js` moved from `UserPromptSubmit` to
`SessionStart` (`startup`, `resume`, `clear`) — both used to re-fire on
every prompt turn, accumulating one copy per turn in the transcript. This
changes CADENCE ONLY: the always-armed ruling for `step_zero_reminder.js`
below is unchanged and still enforced by `tests/manifest_consistency_smoke.py`'s
`EXPECTED` inventory (re-adding either reminder to a `UserPromptSubmit`
binding reds that check) and `tests/reminder_hooks_smoke.py`'s
`check_step_zero_fires_everywhere`.

**Tag-echo fix (2026-08-11, §41):** the cadence move initially shipped with
`step_zero_reminder.js` still hardcoding `"hookEventName":
"UserPromptSubmit"` — a host that validates the declared event name against
the firing event silently discards the output, so the reminder never landed
after the rebinding. `step_zero_reminder.js` now echoes stdin's
`hook_event_name` (two-value allowlist, like `check_messages_reminder.js`),
and `tests/reminder_hooks_smoke.py`'s `check_manifest_bound_events_echo`
derives each reminder's expected events from `hooks.json` itself, so a
hardcoded tag can never silently desync from the wiring again.

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

There is no `Stop` handler in the current manifest (see the top of this file
for the codex-0147-async-hook-regression removal). A synchronous `Stop` hook
is not an acceptable substitute: it would block the turn boundary and queue
composer input behind the wait, the exact defect 906753eb7 fixed. Autonomous
Codex workers still get turn initiation through `spawn_session`'s
`drive_on_delivery`, which uses the host driver channel and does not depend on
this plugin's `hooks.json` at all. An operator-launched Codex TUI currently has
no per-turn delivery notice; it drains coordination deliveries via the
`SessionStart` reminder above and a manual `peer_inbox` read.

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
/opt/homebrew/bin/codex plugin marketplace add /Users/alice/Workspace/solet --json
/opt/homebrew/bin/codex plugin add coordination-hooks@solet-development --json
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

Only the live Git-Controller may mutate files in the shared solet checkout. For a
reviewed local development update, that session runs the Codex plugin-creator
cachebuster helper against this plugin, reviews the resulting single version
suffix, and reinstalls from the existing marketplace:

```sh
python3 ~/.codex/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py \
  /Users/alice/Workspace/solet/plugins/github_midwife_plugin/codex_plugin/coordination-hooks
/opt/homebrew/bin/codex plugin add coordination-hooks@solet-development --json
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
