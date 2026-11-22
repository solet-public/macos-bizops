# Security notes — stock-Codex coordination-hooks

This page describes the complete stock-Codex package. The plugin currently
registers nine handler entries backed by five entry-point scripts: the fixed
`step_zero_reminder.js` and measured `session_start_context.py` scripts, the
async `context_status_reporter.py` Stop reporter, the SYNCHRONOUS
`inbox_consumer.py` Stop hook (CDX-06, 2026-08-24), and `recovery_codex.py`,
the working-tree recovery adapter, which is registered five times: `SessionStart`
(`startup|resume|clear`), `PreToolUse` (`^Bash$`), `PostToolUse`
(`^(Bash|apply_patch)$`), `Stop` (`"async": true`) and `SessionEnd`, with the
timeouts in the inventory table of `README.md`. Twenty-three further modules
ship beside them in `hooks/`: the shared recovery engine, copied byte-identical
from the repository's shared source and listed in the `MODULES` inventory of
`working_tree_recovery.py`: `command_lex.py`, `command_resolve.py`,
`command_types.py`, `command_walk.py`, `custody_store.py`, `discard_forms.py`,
`recovery_capture.py`, `recovery_cli.py`, `recovery_content.py`,
`recovery_deadline.py`, `recovery_events.py`, `recovery_git.py`,
`recovery_journal.py`, `recovery_lifecycle.py`, `recovery_metadata.py`,
`recovery_packets.py`, `recovery_rebuild.py`, `recovery_refs.py`,
`recovery_restore.py`, `recovery_retention.py`, `recovery_rules.py`,
`recovery_types.py` and `working_tree_recovery.py`. They are libraries that `recovery_codex.py` imports,
not handlers, and no manifest entry runs them; the section *Working-tree
recovery — reviewer due diligence* below answers the review checklist for them.
The
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
seed values or portable policy. The five `recovery_codex.py` definitions are new
in this version, so working-tree recovery is inactive on Codex until each one is
trusted; nothing in this package trusts them on the operator's behalf.

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
- Codex's `${PLUGIN_ROOT}` expansion in the manifest command paths;
- `recovery_codex.py` reads these payload fields in the shipped configuration:
  `hook_event_name`, `session_id`, `turn_id` (or `tool_use_id`), `cwd`,
  `tool_name`, `tool_input.command` (a string, or an array of strings) and, for
  `apply_patch`, the patch text in `tool_input` (a string, or the `patch` or `input`
  field of an object), from which only the `*** Add File:`, `*** Update File:`,
  `*** Delete File:` and `*** Move to:` paths are read. The shared engine also
  reads `tool_input.file_path`, `tool_input.notebook_path` and
  `tool_input.edits[].file_path` for any other tool name, which the shipped
  `PostToolUse` matcher never routes here. At `Stop` and `SessionEnd` it saves the
  working folder and every other folder recorded in this session's journal. It
  never reads
  `transcript_path`, `model`, `permission_mode`, `last_assistant_message` or
  `tool_response`. Its environment inputs are `SOLET_RECOVERY_ROOT`, `HOME`,
  `XDG_DATA_HOME`, `XDG_RUNTIME_DIR`, `TMPDIR` (through Python's `tempfile`) and
  `PATH`, which locates `git`. Its one configuration file is `config.json` in the
  store root, which holds limits only. It reads the user's own working files in
  order to save them, and to hash each file the session edits.

The knowledge-base reminder reads only its event name. Live session context
reads event/source and the two service responses described below. Neither
relays prompt or peer-message content.

Outputs, exhaustively:

- reminders: one JSON object containing exactly
  `hookSpecificOutput.hookEventName` and fixed
  `hookSpecificOutput.additionalContext`; live context uses the same envelope
  for the solet's own principle statement, role/count observations and failure lines;
- context reporter success/no-reading/unarmed: inert `{}` JSON, exit `0`;
- armed reporter contract or delivery failure: fixed-prefix stderr, exit `1`;
- inbox consumer, nothing pending or unarmed: inert `{}` JSON, exit `0`;
- inbox consumer, pending found: `{"decision": "block", "reason": "<fixed
  nudge>"}` JSON, exit `0` — Codex's continuation contract is driven by the
  JSON body, not the exit code (unlike the Claude sibling's Stop-hook
  contract, where exit `2` is the wake signal); best-effort diagnostic
  stderr on a failed subprocess call, never a nonzero exit either way (see
  Failure modes below — this hook must never trap the session in a failing
  Stop hook).

- `recovery_codex.py`: on `PreToolUse`, one JSON object holding only
  `systemMessage` and `hookSpecificOutput.hookEventName` plus
  `hookSpecificOutput.additionalContext`, and only when a save needs telling; on
  `SessionStart`, one JSON object holding only `hookSpecificOutput` with
  `additionalContext`, and only when recovery is inactive, over its cap or on an
  interpreter below the floor; on `PostToolUse`, `Stop` and `SessionEnd`, nothing,
  ever. It never prints `decision`, `permissionDecision`, `continue` or
  `stopReason`, and never exits non-zero. Its one `print` is the only thing that
  can reach stdout: the engine runs with stdout redirected to stderr. Faults and
  advisories are one `[recovery] ...` line each on stderr.

No handler other than the recovery adapter's engine writes a file. No handler
opens a direct network or database connection. The live-context hook reads
the inbox through the solet's bridge and the approved statement from one fixed
path of the solet's own knowledge base (through the bridge only when no file
is found). The context reporter and inbox
consumer retain their existing argv-only child contracts.
`step_zero_reminder.js` reads stdin through Node's built-in fs. The earlier
Python entry points import only standard-library modules and no sibling policy
module, so CPython writes no bytecode beside them. `recovery_codex.py` does
import its sibling engine modules, so it sets `sys.dont_write_bytecode` before
the first import, and the adapter smoke runs it from a copy to prove no
`__pycache__` appears beside the shipped files. The engine writes only its local
store and a throwaway rules directory, both described below.

## Live session context — reviewer due diligence (2026-10-08)

A compaction runs SessionStart again with source `compact`; the same two reads run under the same caps and the output has the same shape, so nothing new is read, written or retained.

`session_start_context.py` replaces the static message and role reminders on
SessionStart `startup`, `resume`, `clear`, and `compact`. It performs two read operations:

- The article principles_statement.md (knowledge base ananta_platform, folder 24_operator_communication) is read
  from the solet's own knowledge-base file,
  `ananta/knowledge_bases/ananta_platform/24_operator_communication/principles_statement.md`, under the first of
  these roots that holds it as a regular file: `CLAUDE_PROJECT_DIR` when set, the working directory, the tree the
  bridge runs from (`<root>` of `<root>/.venv/bin/solet-bridge`, `<release>` of `<release>/venv/bin/solet-bridge`; a
  bare `SOLET_BRIDGE` name is looked up in `PATH`) and that tree's `code` directory. The path is fixed and the file
  is opened for reading only: the hook reads no other file, writes none, and the text is clipped and labelled as
  before. Only when no root holds the file does the hook run
  `[SOLET_BRIDGE, "call", "service_interface::knowledge_service::read_file", JSON]` for the same article. Either
  way the hook emits the text under the `## Approved statement` heading verbatim after one label line naming the
  article, capped at 8000 UTF-8 bytes; a longer article is clipped at a line
  boundary with a one-line advisory. A file without that heading gives one typed `StatementMissing` line and no
  bridge read, and a failed bridge read gives one typed `unavailable (ErrorClass)` line and no text.
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

The principles are read before the inbox. The inbox read is capped at 3.5
seconds, and the bridge read of the principles, made only when no statement
file is found, keeps its own 3.5-second cap, so the worst case is 3.5 seconds
with the file and seven seconds without, under the thirty-second manifest
timeout. A failed
read adds one explicit `unavailable (ErrorClass)` line and exits zero; a missing
identity is `MissingIdentity`, never zero messages. No cache or static reminder
fallback exists. JSON serialization escapes all observed strings. Inbox context
is capped at 1000 UTF-8 bytes and the principle text at 8000, leaving room
for the unchanged knowledge-base reminder. Over-budget inbox output is
reported as `ByteBudgetExceeded`, never silently clipped into apparent success;
the principle text is clipped only with its one-line advisory.

This stdlib Python 3.13 hook uses `subprocess.run` with argument arrays and no
shell. It creates no files, performs no register writes, claims no role and
changes no Git policy. Hook installation/trust is separate from candidate delivery;
these are same-user coordination controls, not a privilege boundary.

The manifest consistency smoke and the reminder hooks smoke cover this candidate's manifest
and fixed reminder; the live SessionStart subprocess smoke is added by slice S3 of this Change.
This candidate makes no claim of live host deployment.
The independent review unit records the due-diligence verdict required by
`rul_af7c73e3` and `rul_f0e51f93`.

## Working-tree recovery — reviewer due diligence

This section describes `recovery_codex.py` and the twenty-three engine modules
beside it (the `MODULES` inventory of `working_tree_recovery.py`) as shipped in
this directory. The review verdict required by `rul_af7c73e3` and `rul_f0e51f93`
is recorded separately by the independent review unit. The recovery hooks are not
a control: they never refuse and never block.

### Children executed

The engine has one process boundary, in `recovery_git.py`. Every child is `git`,
started from an argument list with no shell, with `-c core.fsmonitor=false -c
core.quotePath=false`, with `GIT_OPTIONAL_LOCKS=0`, `GIT_TERMINAL_PROMPT=0`,
`LC_ALL=C` and `GIT_NO_LAZY_FETCH=1` in its environment, under one deadline shared
by the whole hook. No child inherits the hook's stdin, stdout or stderr: an
argument-only child reads `/dev/null` and a request-stream child reads a pipe the
hook owns. When the deadline passes, the child's process group is killed. The
restore command a notice prints is text for the person or the model; no hook runs
it.

- Argument-only, read-only against the repository: `rev-parse` (object format,
  `HEAD`, `HEAD^{tree}`, the common directory, the top level, the absolute git
  directory, one branch tip, `refs/stash`), `symbolic-ref --quiet --short HEAD`,
  `config --get core.ignoreCase`, `diff-files --no-ext-diff -z --raw`, `ls-files`
  (`--stage`, and `-o --exclude-standard` with and without `--directory`),
  `ls-tree -r -z`, `clean -n`, `stash list`, `for-each-ref`,
  `rev-list --parents -n 1`, `log -1 --format=%B` and `merge-base`.
- Request-stream, read-only: `cat-file --batch`, `cat-file --batch-check` and
  `check-ignore` fed names on stdin. For the ignore files that a stash or a
  deleted branch carries, `check-ignore --no-index` runs with `--git-dir` and
  `--work-tree` pointing at a throwaway directory, and only reads the repository.
- Restore-only, run solely by an explicit `restore`, never by a hook:
  `hash-object -w --no-filters --stdin-paths`, `update-index -z --index-info`,
  `stash store` and `branch`; and, against a temporary index file inside the
  store, `read-tree`, `update-index -z --index-info`, `write-tree` and
  `commit-tree`.
- Never run: `status`, `diff-index`, `diff-tree`, `pack-objects`, `index-pack`
  and `show-ref`.

### No writes to your repository during a save

A save runs only the read-only children above, with `GIT_OPTIONAL_LOCKS=0`, so it
takes no `index.lock` and refreshes no index. It writes no ref, index entry,
configuration, hook or object in the repository, and it never commits, stashes or
creates a branch. The engine's own smoke asserts that a save leaves the
repository's `.git` directory unchanged, and this package's adapter smoke asserts
that a `PreToolUse` hook leaves the working tree and the stash list as they were.

### Repository-configured programs

Reads pass `-c core.fsmonitor=false`, use `--raw` output with `--no-ext-diff`,
and read objects without filters, so a save never runs a program named in the
repository's configuration. For restore: `hash-object` runs with `--no-filters`,
so no clean filter runs; and `restore --refs` updates refs through git, so the
repository's own `reference-transaction` hook, if it has one, runs and may refuse
that entry.

### Files written

- The store. By default `~/Library/Application Support/Solet/Recovery/v1` on
  macOS and `$XDG_DATA_HOME/Solet/Recovery/v1` elsewhere, or the folder named by
  `SOLET_RECOVERY_ROOT`; an empty or blank value is
  treated as unset. A root inside any git repository, `~/Documents`,
  `~/Desktop`, `~/Downloads`, `~/.codex`, `~/.claude` or an iCloud folder is
  ignored in favour of the default, and recovery stops with an explanation if the
  default is unsafe too. The root must be owned by the current user and is mode
  0700; packet directories are 0700 and files 0400; a `.metadata_never_index`
  marker keeps Spotlight out. It holds content-addressed packets, a per-session
  `edits.jsonl` journal (each file the session edited through `apply_patch`: its
  absolute path, a SHA-256 digest and a timestamp, plus the id of any stash the
  session created), a lifecycle history, the `restored` folder that restore
  writes into, and `config.json` only if you create it. It is not under `~/.codex`.
  That Codex's feedback upload reads only from the Codex home is an assumption
  from binary strings and has not been measured here.
- A throwaway rules directory, only while classifying the ignore files that a
  stash or deleted branch carries. It is made with `mkdtemp`, mode 0700, in the
  first candidate that is not inside the store: the operating system's temporary
  directory, then on macOS the per-user temporary directory, then
  `~/Library/Caches/Solet/recovery-rules`, or `XDG_RUNTIME_DIR` on Linux. It holds
  only ignore-rule files, is removed when the classification ends, and a dead
  run's leftovers are swept by the next run. If no candidate is outside the store,
  no such classification runs and the files that needed it are not copied.
- Nothing else. No bytecode is written beside the shipped files, and nothing is
  written into the repository or into the Codex home.

### Content sensitivity

Saves are copies of the user's own working files and can contain secrets that the
user keeps tracked, or untracked but not ignored, including a tracked file that
was force-added despite an ignore pattern, because git treats tracked files as
visible. The invariant: no ignored file's content is ever persisted in the store.
Every path in every layer is decided by git's own matcher before any byte is
persisted, and a classification that cannot be completed copies nothing. Names of
ignored paths can appear in a notice and in a save's list of what was left out;
their contents cannot. The `edits.jsonl` journal holds a SHA-256 digest and the
absolute path of each file the session edited, including an ignored file; a
digest, never content. Notices name changed files and, by reference name, stash
entries this session did not create. The store is user-only and not indexed by Spotlight;
Time Machine backs it up like other user data, and this package does not change
backup settings. Nothing leaves the machine.

### Network

None. No bridge, register or solet call is made by `recovery_codex.py` or by any
engine module, and none of them imports a network module.

### Resource bounds

By default a save holds at most 512 MiB, 50,000 files and 64 MiB per file; the
store holds 5 GiB; below 10 GiB of free disk only index and tracked files are
saved; a saved packet is kept until its work is delivered plus 30 days, and a
trashed packet is purged after a day. The whole `PreToolUse` hook has an 8 second
budget, of which 1 second is reserved to publish what was captured and 2 seconds
go to finding untracked files; `Stop` has 60 seconds and `SessionEnd` 2.5
seconds, against manifest timeouts of 15, 120 and 3 seconds. A per-folder lock is
waited for at most one second (or the remaining deadline, if shorter), after which
the save goes on without it. These figures are fields of `config.json` in the store
root: `cap_bytes`, `cap_files`, `cap_file_size`, `store_bytes`, `disk_floor`,
`grace_seconds`, `trash_seconds`, `hard_seconds` (the 8 second deadline, applied to
`PreToolUse` only), `publication_seconds` and `untracked_seconds`. The 60 second
`Stop` budget, the 8 second deadline of `PostToolUse` and `SessionStart`, the 2.5
second `SessionEnd` budget and the one second lock wait are fixed in the code. An
invalid or unreadable file falls back to the defaults and says so on stderr. Going
over the cap never refuses a save. At most once an hour the `Stop` hook runs a
prune pass, which deletes only inside the store.

### Failure modes

Every path exits 0 and prints a few short `[recovery]` lines on stderr (at most
one for a fault and one for advisories).

- `git` not on PATH: no save, silent. Not a repository, or unreadable: no save for
  that folder, and the command runs.
- Store unwritable or disk full: "could not save before the command; the command
  will run", shown to the person and the model. Store over cap or disk low: saving
  continues, with only index and tracked files below the disk floor, and the
  person is told once per session.
- Deadline reached: what was captured is published and the rest is named as not
  saved. A stalled git child is killed at its share of the deadline. A file that
  changes while it is read is read once more and then named unreadable. A FIFO,
  socket or device file is never opened.
- An ignore classification that cannot be completed copies nothing from that
  request set and names the paths as unclassified.
- Malformed JSON, a non-object payload, a missing or mistyped field, or an unknown
  event: no output.
- Python below 3.11: exit 0 before any engine module is imported, one advisory at
  `SessionStart`.
- Restore never overwrites a newer file: it keeps yours and writes the saved copy
  under the store's `restored` folder.
- Codex's own timeout kills a slow hook: the tool call is assumed to proceed,
  which has not been measured here.

### Supply chain, registration and interpreter

The adapter and the engine are stdlib only and carry a Python 3.11 floor; there is
no new dependency. The twenty-three engine modules are copied from the
repository's shared source by its materializer, the repository's identity smoke
(`plugins/github_midwife_plugin/tests/hook_common_identity_smoke.py`) asserts that
the materializer reproduces the shared source byte for byte, and this package's
adapter smoke asserts that every
module in the inventory is present and, where the shared source sits alongside,
byte-identical. The manifest uses the command-string form with the
`${PLUGIN_ROOT}` expansion; the script never builds a shell command and never
interpolates hook input into one. The Manager's hook-interpreter pin rewrites a
bare `python3` hook command to the clone's own interpreter, and it covers the five
new entries; recovery runs under either a bare `python3` on PATH or the clone's
interpreter, and either must be Python 3.11 or newer.

### Known limits

Commands run inside scripts, input written to a running shell and a person's own
terminal are not seen; the turn-end save is the backstop. A dirty submodule's own
worktree is not saved: the superproject's save records the gitlink change only. A
save is exact per file, not an atomic snapshot across files. A rule that ignored a
stashed path only through `.git/info/exclude` or `core.excludesFile`, and was
removed before the save, cannot be seen; git records no marker that tells a stash
made with `-u` from one made with `--all`. Rebuilding a pruned stash entry needs
its recorded base commit, and without it restore writes the saved paths under
`restored`. While a stash's or branch's paths are classified, the ignore files
that ref carries sit briefly in the rules directory, outside the store; that can
include a `.gitignore` that is itself ignored, never the content of any other
ignored file. On Codex, recovery is inactive until the hooks are trusted.

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
for a hook that only reports telemetry. The `Stop` entry of
`recovery_codex.py` is `"async": true` for the same reason, and the manifest
smoke and the adapter smoke each require it (dropping it is a named failing
mutation in both): a turn-end save that may take up to a minute must not hold the
turn boundary, and the entry prints nothing, so there is no decision for an async
hook to lose. Every other `recovery_codex.py` entry carries NO `async` key.
`inbox_consumer.py` is the one
deliberate exception in this manifest: it carries NO `async` key (the
manifest smoke requires exactly that, for every `Stop` entry other than the
reporter and the recovery adapter), because measured live (isolated proof, a dev-checkout workbench
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
- `recovery_codex.py`: every path exits `0` and prints a few short `[recovery]`
  stderr lines (at most one for a fault and one for advisories);
  the full list is under *Working-tree recovery — reviewer due diligence*, in its
  own Failure modes subsection.

`context_status_reporter.py` never exits `2` or requests continuation — the
only blocking outcome for a Codex hook in this manifest is `inbox_consumer.py`'s
`decision:block` JSON body, which is a DIFFERENT mechanism from an exit
code (Codex's Stop-hook continuation contract is JSON-body-driven, not
exit-code-driven — measured live, see the isolated proof cited above).
`inbox_consumer.py` itself always exits `0`; it never uses exit `2`.

## Trust revocation

Recovery: each of the five `recovery_codex.py` definitions can be revoked on its
own in `/hooks`. Revoking or removing them leaves the recovery store in place. To
remove saved work, run the engine's `forget` and then `purge` for a save, or
delete the store folder named in *Files written*.

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
observation fires only at startup/resume/`clear`/`compact`, not per turn — the
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
*Retired behaviours*, separately from any replacement claim. The working-tree
recovery hooks are not a replacement and not a control: they save before a
discarding command and let it run, they never refuse, and they never print a
decision.

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

| `recovery_codex.py` prints nothing on `PostToolUse`, `Stop` and `SessionEnd`; prints only `systemMessage` and `additionalContext` on `PreToolUse` and `SessionStart`, validated against the output schemas embedded in Codex 0.163.0; exits 0 on every fault fixture and below Python 3.11; saves before a discarding command in string, array and shell-wrapped array form; treats `apply_patch` edits as the session's own; writes no bytecode; registers each entry with its declared matcher, timeout and async flag while the four earlier handlers stay byte-identical; and three named mutations (drop `async`, add output, add decision) each fail on their own assertion. Fixtures are synthetic and validated against the same embedded input schemas. | `recovery_codex_adapter_smoke.py` |
| the twenty-three engine modules are present and byte-identical to the shared source | `recovery_codex_adapter_smoke.py` (presence and, beside the repository's shared source, identity) and the repository's materializer and identity smoke |

Run every offline smoke with `python3 tests/run_all.py`. These are offline source and
behavioral checks. The live untrusted/trusted stock-process sentinel is a
separate acceptance leg because only Codex itself can prove hook trust state.
