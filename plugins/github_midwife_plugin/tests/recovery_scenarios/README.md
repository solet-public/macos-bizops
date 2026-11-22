# Recovery scenarios: one acceptance set, two coding tools

This folder proves, by measurement and not by reading code, that the Claude Code adapter and the Codex adapter of the
working-tree recovery hooks make the same promise: **save before a discarding command, notify only for changes the session
did not make, save at every turn end, never block, and restore byte-identical work.**

One scenario set, S1 to S11 of design section 6.2 (the git-safety C4 recovery design of 2026-11-08), runs against
both tools with identical expected outcomes. S1, S2 and S3 are the three historical losses: the 2026-05-22 `git stash pop`
of another session's stash, the 2026-06-14 stash/checkout/reset in the shared checkout, and the stash made in a linked
worktree and popped from the main checkout.

## Files

| File | Holds |
|---|---|
| `scenarios.json` | S1 to S11 as data: setup, actor steps, expected saves, expected notices, the restore step and the expected bytes |
| `run_scenarios.py` | the entry the gate runs: argument parsing and the payload run |
| `scenario_model.py` | constants, the hooks.json reader, and the scratch world of one scenario variant |
| `scenario_expect.py` | the promises checked on every hook call, and the per-step expectation checks |
| `scenario_ops.py` | the step vocabulary and the payload driver |
| `scenario_run.py` | one cell per scenario and tool, and the printed result line |
| `scenario_live.py` | the live driver: the real tool acts, a recorder logs each hook call |
| `scenario_plan.py` | the live stage's operator surface: admission, the printed plan, the credential probe, the receipt |
| `scenario_selfcheck.py` | the instrument's own honesty checks on fixtures: Codex trust entries, per-cell raw-record names, the S4 and S7 live declarations |

The code is split into modules only to stay inside the repository's complexity and maintainability gates; there is one
interpreter and one scenario file.

## The tool-column rule

The tool is a column of the run, never a fork of a scenario. `scenarios.json` has one expectation per step. A tool may
differ from it only through a step's `known_differences` entry, which names one row of design section 5.3 listed under
`known_differences` in `scenarios.json`. A run prints `known difference <id> applied` for every one it used. Two files
that expect different things would drift; one file with a named, printed exception cannot.

Known differences the scenarios allow:

| Id | Claude Code | Codex | Effect in a scenario |
|---|---|---|---|
| `worktree_location` | worktrees in the repository on a branch | worktrees under `CODEX_HOME/worktrees`, detached HEAD | the harness creates the linked worktree detached for Codex; the saved binding records branch `detached` (S10) |
| `session_end_budget` | SessionEnd may run up to its declared timeout | 1 s default, 3 s maximum, so the save is tracked and index only | an untracked file made after the turn-end save is saved by a Claude SessionEnd and not by a Codex one (S4) |
| `hook_trust` | plugin hooks run once installed and enabled | definitions are inactive until trusted in `/hooks` | none in payload mode; live mode passes `--dangerously-bypass-hook-trust` and, because that does not cover project trust, a generated trust entry per disposable folder (see Live mode) |
| `registration_form` | exec form: `command` plus `args` | command string | none; the harness builds each command line exactly as that tool's `hooks.json` declares it |
| `shell_coverage_gap` | commands inside scripts are not seen | the same, plus `write_stdin` input | none; every scenario command is a top-level shell command |

## Payload mode (the default, and what the gate runs)

```
.venv/bin/python3 plugins/github_midwife_plugin/tests/recovery_scenarios/run_scenarios.py
```

No arguments means `--mode payload --tool both`. Each tool's recovery hook command line, exactly as its `hooks.json`
declares it, is fed that tool's payload on stdin; the actor's shell command then runs for real in a scratch repository
under a fresh temporary directory, with `HOME`, the store (`SOLET_RECOVERY_ROOT`) and `TMPDIR` all disposable. The harness
never patches or reads a candidate file other than `hooks.json`, the adapter entry points, and the shared engine's
own command line (`working_tree_recovery.py`, which is how a user lists and restores saves).

A `HONESTY SELF-CHECK` line comes first (see the live-mode section); one line per (scenario, tool) follows: `PASS` or `FAIL`, with the historical cases labelled. Any FAIL, or a failed self-check, exits 1.

Options: `--tool claude|codex|both`, `--scenario S1,S5`, `--claude-root DIR`, `--codex-root DIR`, `--list`, and
`--bulk-files N`, a development override of S6's 50,000 untracked files (the unsaved-remainder assertion is skipped and the
override is not a gate run).

### The three states of the gate invocation

A gate that nobody can pass would block the landing it belongs to, so the no-argument invocation has a pass path in every
state the landing passes through:

| State | Result |
|---|---|
| both adapters present in the default roots | all 22 cells run and must PASS |
| neither adapter present (a tree before the adapters land) | one loud line `ADVISORY: recovery adapters not installed in this tree; 0 of 22 cells run`, exit 0 |
| exactly one present | exit 1, naming the missing tool |

Explicit `--claude-root` and `--codex-root` always run the cells against what they name, so two empty directories give 22
FAIL and exit 1. That is the red-first measurement: a pass means something only if the same run can fail. A scenario that
fires no recovery hook at all also fails, because it would otherwise assert nothing.

After landing, a tree that loses both adapters degrades to the advisory. The landing step reads `22 of 22 cells run` in the
gate output; that line is the evidence.

## What counts as FAIL, and UNMEASURED

A cell fails only on a measured difference between expected and observed saves, notices, exit codes or bytes. A condition
the host cannot produce is reported `UNMEASURED(<case>): <reason>` and is never a FAIL: S8 case (g) needs an interpreter
below 3.11, and case (c) needs a non-root user. Every hook call of every scenario must exit 0, print nothing on PostToolUse,
Stop and SessionEnd, print only `systemMessage` and `hookSpecificOutput.additionalContext`, and never carry `decision`,
`permissionDecision`, `continue` or `stopReason`.

Payload shapes are the ones design 5.2 records. Where a shape is assumed until the live stage settles it, it is named here:
the Codex `apply_patch` PostToolUse `tool_input` is sent as the patch text (R2), and the Codex `tool_input.command` as a
string (R1).

## Live mode (run once per tool, not by the author lane)

```
.venv/bin/python3 plugins/github_midwife_plugin/tests/recovery_scenarios/run_scenarios.py --mode live --tool claude --dry-run
.venv/bin/python3 plugins/github_midwife_plugin/tests/recovery_scenarios/run_scenarios.py --mode live --tool codex --dry-run
```

`--dry-run` prints every command and launches nothing, and needs no model (Codex prints a `<model>` placeholder). The only
things it executes are the three read-only admission readings of the pre-flight, because it prints their values, and the
admission self-check below. A real run adds `--model <the smallest model codex exec offers>` for Codex; Claude defaults to
`claude-haiku-5-5`. Drop `--dry-run` to run.

Live mode drives the same scenarios through `claude -p` and `codex exec`, one model turn per file edit or shell command:

- A disposable directory is made under a new empty parent and deleted at the end; the receipt records that it is gone.
- Hooks are registered by a hook file the tool reads for the disposable folders. **Claude:** one `claude-hooks.json` per
  scenario, in the scenario's base directory outside every repository folder, passed with `--settings` (that flag applies
  whatever `--setting-sources` says). It sits outside the repository on purpose: S11 runs `git stash push --all`, which
  stashes ignored files, and the first live run lost its in-tree hook file that way, so the next actor's turn ran with no
  hooks (the S11 `no PreToolUse hook recorded` FAIL; the `Stop` and `SessionEnd` records it did see came from the file
  returning when the `git stash pop` restored it). **Codex:** a `hooks.json` in the folder's `.codex` directory, a location that
  is assumed until the run records where Codex really reads it, excluded from the visible paths through the repository's
  local exclude file. Codex has no out-of-tree option, so a Codex step that finds that file gone is reported
  `UNMEASURED(live)` instead of being run without hooks. A recorder wraps each recovery entry and logs the payload the
  tool actually sent, its output, exit code and seconds.
- **Codex project trust.** Codex disables project-local config and hooks in a folder "until the project is trusted", and
  `--dangerously-bypass-hook-trust` covers hook trust, not project trust. The first live Codex run fired no hook at all in 10
  scenarios for that likely reason (unproven; the next live run's first cell is the proof). Every Codex turn therefore carries
  ONE generated session flag naming every disposable git folder the harness installed hooks in (a worktree and its main
  repository) as a table: `-c 'projects={"<a>"={trust_level="trusted"},"<b>"={trust_level="trusted"}}'`. It is one flag
  because Codex splits a `-c` key at every dot and ignores TOML quotes, so a per-folder `projects."<folder>".trust_level` key
  never matches (measured on codex 0.163.0: `codex mcp get` exits 1 with it, 0 with the table), and two `projects=` flags
  replace each other. The `-c` parser is version specific, so the receipt records `tool_version`. The flag lasts for that process; no file is written and the real
  `config.toml` is not read (`--ignore-user-config`). A disposable `CODEX_HOME` is deliberately **not** built: with
  `--ignore-user-config` the login still lives in `CODEX_HOME` (`codex exec --help`), so a disposable one would need a copied
  credential. `--dry-run` prints the entries under `CODEX PROJECT TRUST`. If a live cell still records no hook, its FAIL line
  says that no hook of any event fired and names this step; the smallest next change is then a trust entry Codex honours
  in a config the harness generates, and it is reported, not improvised.
- **Evidence is harvested, then deleted.** The run ends with the receipt's `evidence_dir`: read the cell files, copy what the
  report needs, then `rm -rf` the folder and confirm it is gone (dry-run step 7).
- **No credential, setting, plugin list or keychain item is copied, and the user-level configuration is not touched.** The
  tool reads its real login as it always does. Claude is started with `--setting-sources project,local`, so user-level
  settings and hooks are skipped; Codex with `--ignore-user-config`, and `--dangerously-bypass-hook-trust` for this
  disposable run only (known difference `hook_trust`). One trivial call per tool first measures whether it authenticates
  this way. It runs in an empty folder that is not a git repository, so the Codex call passes `--skip-git-repo-check`
  (without it `codex exec` exits 1 before reading any login). A failed probe stops the run and prints the tool's first
  stderr line verbatim plus the exit code, and says **authentication** only when stderr carries an authentication marker
  (401/403, unauthorized, not logged in, login, API key, credential, expired or invalid token) anywhere in it: Codex opens
  with a progress line before the error, so the first line alone cannot classify. Any other failure reads "a reason that is
  not authentication, so no credential result was measured". If it does need a copied credential or setting, the run
  stops and names the step, and nobody improvises a credential path.
- Fault injection (S8) changes the hook's own environment or payload, so it is measured in payload mode only and reported
  `UNMEASURED(live)` here.
- At most 40 model turns per tool; the receipt discloses the count. Models: Claude defaults to `claude-haiku-5-5`; Codex
  needs `--model` naming the smallest model available, which the harness does not guess.
- **Every cell keeps its raw record.** A new empty evidence folder is made next to the disposable parent and is not deleted by
  the run (the receipt names it as `evidence_dir`; whoever reads it deletes it). Each cell writes `<tool>-<id>.hooks.jsonl`, the
  recorder's own bytes for that cell, one line per hook call (payload in, output out, exit code, seconds), empty when no hook
  fired, and `<tool>-<id>.reply.jsonl`, one line per tool process: command line, folder, exit code and the whole stdout (Codex's
  JSONL events, Claude's JSON result) and stderr. The cell's result line names both files as `hook_log=` and `reply_artifact=`
  (a payload cell prints `n/a`: no tool ran). A cell that stops still leaves both files.
- **Prompts name the folder and the intent.** Each turn names the scenario folder in full (the first Codex turn also gets `-C`)
  and says the command is the test in a disposable repository: a model that judged `git branch -D` unsafe (S9) issued no
  command. Before every turn the harness checks the folder is a git repository and stops the run if it is not; after a shell
  step it compares the `cwd` in the hook payload with the scenario folder, and a command the tool ran elsewhere (S6 saw "not
  a git repository") is `UNMEASURED(live)` with both folders named, not a pass or an adapter failure. When no hook fires, the
  FAIL line carries the tail of the model's reply.
- A FAIL is recorded as measured, and its result lines keep what is otherwise deleted with the disposable folder: the last
  model reply (the tool's JSON stdout tail, 1,500 characters), the last tool stderr, and the last 15 raw hook records this
  scenario logged (event, exit code, seconds, and the payload the tool sent and the output the hook gave, each field cut at
  1,500 characters). A tool process that exits nonzero (a stop) prints the same evidence before the run stops. A failed
  scenario is not rerun to see whether it passes.
- **One process per turn.** Every model turn is its own `claude -p` or `codex exec` process, so the turn end and the session
  end coincide: `Stop` and `SessionEnd` both fire at the end of every turn, and `SessionEnd` finds the tree the `Stop` just
  saved. A step whose precondition cannot occur that way is not run and not passed. It is `UNMEASURED(live)` with its
  reason, and the other steps of the scenario still run: a `session_end` step that expects a save of work changed after the
  last turn-end save (S4), a `stop` that sets `stop_hook_active` (S7), a `stop` for an actor with no live turn (S7), fault
  injection (S8), and a step after a Codex hook file was removed. A skipped step that stores a value ends the scenario
  there, because later steps read it. A scenario with a skipped step reads `UNMEASURED` and one with a measured failure
  reads `FAIL`; only a scenario in which every step ran reads `PASS`. `UNMEASURED` is not a failure and does not change the
  exit code; the receipt's `outcomes` lists every scenario's status. Live S4 therefore measures the turn-end save and the
  restore after the worktree is deleted, and S7 the dirty-repository `Stop` and a silent `SessionEnd`; the SessionEnd save of
  later work and the `stop_hook_active` case stay payload-mode measurements. The scenario data says so: S4 and S7 carry a `live`
  declaration (`status: partly measurable`, the steps that are `UNMEASURED(live)` by design, the reason), printed in the
  dry-run plan and in the cell's line, and the honesty self-check fails any scenario that has a step no live tool can produce and
  no such declaration.

### Live-stage admission rule

The purpose is that the run never competes with a landing, a deploy swap or a restore for memory.

- **Pass condition:** the 1-minute load is below 16 (0.6 x 28 cores), free swap is above 1 GB, and the process table shows no
  heavy-work invocation. The process table is read by each process's invocation (its first words, so a resident service or a
  lane whose prompt merely mentions a landing is not counted) for a scoped check-in, a smoke battery, a landing run,
  `pg_restore`, a private database being provisioned, or a deployment swap or cutover script. The readings are `uptime`,
  `sysctl vm.swapusage` and `ps`; a refusal prints the matched signature, not a truncated command line.
- **Running component and schedule:** the live runner (`--mode live`), on the session that launches the tool sessions, once per
  tool, before the run and again before scenario S6.
- **When outside bounds:** it waits and re-reads every 5 minutes and never forces; more than 60 minutes outside is a stop that
  names the readings.
- **Pass-path test:** every `--dry-run` runs an admission self-check on fixture process tables: a table of only resident
  services and lanes whose prompts mention a landing must read inside bounds, and a table with a scoped check-in and a
  landing run must read outside. A failing self-check exits 1, so the rule cannot silently turn into one nobody can pass.
- **Honesty self-check:** every `--dry-run` and every payload run (the gate's no-argument invocation, with or without
  adapters installed, printed as a `HONESTY SELF-CHECK` line before the cells; a failure exits 1) also checks, on fixtures, that the Codex probe command line contains
  `--skip-git-repo-check`, that Codex's non-git-folder stderr is classified as not authentication while 401, not-logged-in and
  invalid-key text is classified as authentication, that a scenario with a skipped step is `UNMEASURED` and a failed one `FAIL`,
  and that the SessionEnd and `stop_hook_active` steps above have no live precondition while a silent SessionEnd does. The
  gate battery runs only the payload invocation (`quality_gates/gate_smokes.d/recovery_scenarios.txt`; a register row is a
  bare path with no arguments), which is why the payload run carries this check and no live run is needed to gate it.

Every run is `nice -n 10`, one tool at a time, one scenario at a time, with a 120 s timeout per scenario (S6: 240 s). S6
creates 50,000 empty files under a fresh directory inside the disposable folder and removes it. A tool process that exits
nonzero stops the run and reports its exit code and stderr, so a harness or tool error is never recorded as an adapter result.

The receipt carries the tool and version, the command line, the disposable path, the readings before and after, the turn
count, the 11 per-scenario outcomes, and design risks R1 to R6, each `MEASURED` with evidence or `UNMEASURED` with a reason.
R1, R2, R4 and R5 are answered from the Codex receipt; R3 and R6 from the Claude receipt.
