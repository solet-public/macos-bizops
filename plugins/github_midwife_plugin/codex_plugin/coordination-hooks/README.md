# coordination-hooks for Codex

This is the stock-Codex sibling of the Claude coordination plugin. The current
increment contains:

- a fixed knowledge-base reminder and measured SessionStart context;
- `context_status_reporter.py`, a background `Stop` reporter for native Codex
  context measurements;
- `inbox_consumer.py` (CDX-06, 2026-08-24), a SYNCHRONOUS `Stop` hook that
  checks the session's peer inbox for anything already queued and, if so,
  forces the turn to continue with a fixed nudge; plus its own honesty
  attestation, `report_inbox_consumption`.

The context reporter is bound with `"async": true`, as defined by the current
official Codex hook contract. Live stock-process execution remains a separate
acceptance leg. It reads the host-provided transcript, requires the hook
session/model to agree with native transcript identity, and uses only the latest
positive `token_count` after the most recent `context_compacted` boundary. It
reports the runtime's native provider, model, effort, effective context ceiling,
input tokens, cache counters, and source timestamp through
`report_context_status`. Immediately after compaction, when no positive
post-boundary reading exists yet, it deliberately reports nothing instead of
replaying a stale pre-compaction value.

## The peer-inbox consumer (CDX-06)

Root cause (the project backlog's CDX-06 entry): stock Codex had NO consumer
for the peer-inbox wake event at all — `peer_send` reported success
(`delivery=queued_watcher`) and the pane never turned. Measured live against
the actual codex-0.149.0 binary this fleet runs (isolated proof, a
dev-checkout workbench evidence report, not part of this shipped bundle):
an **async** Stop hook's `decision` output is discarded — Codex does not
wait for it, so an
async hook can report telemetry but can never gate or continue a turn. A
**synchronous** hook returning `{"decision": "block", "reason": "..."}` DOES
force Codex to continue the turn, feeding `reason` back as the next input —
confirmed live, nine consecutive forced continuations in one test run. So
`inbox_consumer.py` runs synchronously; `context_status_reporter.py` stays
async because it only reports and never needs to gate anything.

`inbox_consumer.py` uses `<solet-name> wake --max-wait 0` to observe only its
already-armed sidecar spool. The paired eight-second Stop registration covers
the two-second wake and report children plus startup margin. A pending result
continues the model turn; empty is reported as an empty observation; timeout,
exception, malformed output, and non-contract exits are unknown and do not
refresh the observation. Future delivery belongs to native qualified driving
and bounded reconciliation, not a parked Stop hook. The model reads actual
message content only in its continued turn.

Because this hook observes it, the wake-hook spool is RE-ARMED for Codex:
the hydration-rendered launcher no longer passes `--no-spool` to `<name>
watch`. (It was disabled by codex-0147-dead-spool-retirement, 2026-08-13,
specifically because nothing consumed it then — that premise no longer
holds.) A `spawn_session`-managed worker also still gets
`drive_on_delivery`'s driver-channel notice, independent of this plugin's
hooks entirely, for turn initiation while idle.

`report_inbox_consumption` is called after each known pending or empty
observation — never for unknown — so its `checked_at` remains an honest
successful-check timestamp. `session_inbox_consumption_status` reads it back with the same
`resolved=False`-never-defaulted-true contract `session_context_status`
established: a session whose consumer has never run is loudly distinguishable
from one that ran and found nothing, which is distinguishable from one that
ran and found something — so a `queued_watcher` delivery can never again be
mistaken for a delivery that was actually consumed.

An operator-launched (non-fleet) Codex session or a `host=operator` seat still
gets no per-turn peer-delivery notice from this hook (it is armed only when
`AGENT_INSTANCE_ID`/`AGENT_SESSION_ID`/`AGENT_WAKE_CLI` are all present —
expected, not an error): delivery remains durable but silent, the operator
drains `peer_inbox` themselves on their own next user/model turn, and the
`SessionStart` live-context observation only fires at
startup/resume/`clear`, not per-turn.

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
| `SessionStart` (`startup`, `resume`, `clear`) | `session_start_context.py` | register-wide digest, measured inbox counts and current roles |
| `Stop` (background) | `context_status_reporter.py` | report the latest positive native context reading after the last compaction boundary |
| `Stop` (synchronous) | `inbox_consumer.py` | observe the already-queued wake spool; block/continue on pending; report known pending/empty observations only |

The knowledge-base reminder remains always armed. The live-context hook also
runs for startup, resume and clear; absent services or identity produce explicit
unavailable context. The knowledge-base reminder reads no prompts or peer
messages. Live context fetches and JSON-parses complete peer_inbox responses,
including message-body fields. For inbox data it relays only counts, bound-role
labels, and observation/error text into context, never the message bodies.


## Retired behaviours (inventory, not a replacement claim)

This section records what this plugin used to refuse and no longer does; it is
kept apart from everything the plugin provides today. The Codex Bash
Git-Controller gate (`git_controller_gate.py` and its policy modules) was removed
by operator ruling `rul_e71e43be` in commit `91b8ace14`, the same removal as the
Claude plugin's `0.9.0`. No hook shipped here arms, evaluates or checks any
behaviour below, and deleting the hook supplied no replacement mechanism.

| Retired behaviour | What the gate refused | Enforced today | Covering mechanism today |
|---|---|---|---|
| Git mutation ban | A mutating `git` command in a `Bash` tool call from any session whose launcher-owned `AGENT_ROLE` was not the role named in `GIT_CONTROLLER_NAME`. Armed only when that variable was exported; fail-open when unset. | No | None in this plugin. Git control among sessions sharing a directory is the repository's own responsibility (`rul_d8893e33`). |
| Agent and Task spawn ban | Never enforced by the Codex copy: it routed only `PreToolUse` with `tool_name == "Bash"`, and native edit and delegation tools stayed unrouted. | No (it never was) | None. |
| Bash-level `.git/` mutation block | A `Bash` command whose file-mutating command or redirect named the repository's own `.git/` directory, from any session whose `AGENT_ROLE` was not the named controller role. | No | None. |
| Inline `git -c` and global-option bans | From any session whose `AGENT_ROLE` was not the named controller role: `git -c` setting `commit.gpgsign`, `core.hooksPath`, `gc.auto`, or an `alias.` whose value starts with `!`, and the global options `--git-dir` and `--work-tree`. | No | None. |
| `--no-verify` ban and the operator-confirmation prefix | `--no-verify` on any git command from any other session. The named controller role could run `--no-verify`, a force-push, `reset --hard`, `clean -fd`, a rebase, a branch deletion or a path-discarding `checkout --` / `restore` only with a `GIT_CONTROLLER_OPERATOR_CONFIRMATION=<id>` prefix on that one git command. | No | None. No hook asks for, reads or checks that prefix. |
| Git beside a shell evaluator word or an untokenisable construct | A `Bash` command naming `git` that the shared policy could not safely inspect, refused fail-closed. The origin repository's own gate (`64e9810fb`, never shipped) additionally refused `git` beside a shell evaluator word (`bash`, `sh`, `zsh`, `dash`, `ksh`, `fish`, `eval`, `source`, `exec`) or an unreadable shell construct, for every role. | No | None. A command of this shape is no longer refused or flagged by any hook. |

`GIT_CONTROLLER_NAME` and `AGENT_ROLE` are still exported by the Codex launcher,
but no hook shipped here reads either to authorize or refuse a Git command.

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

Follow the repository's own git rules for its shared checkout. For a
reviewed local development update, the session doing the update runs the Codex plugin-creator
cachebuster helper against this plugin, reviews the resulting single version
suffix, and reinstalls from the existing marketplace:

```sh
python3 ~/.codex/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py \
  /Users/alice/Workspace/solet/plugins/github_midwife_plugin/codex_plugin/coordination-hooks
python3 deployment/scripts/codex_coordination_hooks_safe_install.py \
  --marketplace solet-development --bridge /Users/alice/.local/bin/solet-bridge
```

Do not run bare `codex plugin add` for this plugin while managed Codex lanes
are live. The safe-install wrapper queries the managed-session ledger, snapshots
the versioned cache, runs the stock install, and restores pruned prior versions
when a live Codex lane exists. This replaces the compat-symlink repair for
`iss_9959c013` and `iss_eb36b76d`; its report records every restored version.

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

The fixed knowledge-base reminder remains checked by `tests/reminder_hooks_smoke.py`.
Manifest inventory and source boundaries are checked by `tests/manifest_consistency_smoke.py`.
The always-armed knowledge-base negative control remains in `tests/reminder_hooks_smoke.py`.
