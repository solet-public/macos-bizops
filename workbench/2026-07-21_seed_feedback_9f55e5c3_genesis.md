# Homunculus seed — agent setup feedback

Audience: the coding agent driving genesis/hydration of a homunculus from the
seed. Not operator docs. Captures things that were unclear or that tripped me
up while birthing `dax` (from seed `2026-07-20_local_branch_bizops_9f55e5c3`),
so the next agent (or a seed revision) can fix them.

## 0. TOP DEFECT — genesis ignores the seed's declared bundle and always births the FREE profile

This is the headline. It made the whole session look like "casting about" when it
was one upstream bug cascading:

- `PROVENANCE.json` declares `bundle.name: "bizops_standard"`.
- The seed ships the matching profile template
  `plugins/github_midwife_plugin/knowledge_base/profile_templates/macos-bizops-homunculus.yaml`
  (all 5 connectors + Google Workspace, session-ledger sources, dev tooling,
  local blue-green self-deployment, real local inference).
- But `github_midwife_plugin/genesis.py:63` hardcodes
  `_DEFAULT_PROFILE_NAME = "macos-free-homunculus"`, and `main()` (the target of
  `bootstrap.py`'s handoff) calls `run_genesis(name, clone_root)` with **no
  profile argument and no env-var/PROVENANCE consumption**. So every stock
  `bootstrap.py` run births the MINIMAL profile regardless of what the seed says
  it is.
- Downstream symptom that wasted the most time: on the free profile
  `self_deployment_service` is unbound, so `apply_manifest` (the blessed way to
  change the plugin set) rejects with `required_service_unbound` — i.e. the
  free profile can't even self-modify. That is the platform correctly saying
  "wrong tier," not an invitation to hand-edit `manifest.yaml`.

**Fix idea (pick one):** (a) `genesis.main()` reads `PROVENANCE.json`'s
`bundle` → maps to a profile template; or (b) the Seed Factory bakes the
bundle's profile into `_DEFAULT_PROFILE_NAME` when it seals a non-free seed; or
(c) `bootstrap.py` accepts `HOMUNCULUS_PROFILE` and threads it into the handoff.
Any of these makes the stock `export HOMUNCULUS_NAME=…; bootstrap.py` flow
produce the profile the seed advertises. **Workaround used here:** call
`run_genesis(..., profile_name="macos-bizops-homunculus")` directly.

## 1. The docs read as "human-run install," but genesis is agent-driven

`README.md` and `01_hydration_runbook.md` lean hard on phrases like "A human
must be present," "secure credential intake runs in YOUR terminal," and "the
driving agent." Read cold, that strongly implies the *operator* runs
`bootstrap.py` by hand in their own shell. It sent me down the path of handing
the operator a `! export HOMUNCULUS_NAME=... && python3.13 bootstrap.py` line to
run themselves — which is wrong. The intent is: **the coding agent drives
`bootstrap.py` end-to-end and answers the `[y/N]` confirm prompts itself.** The
only genuine human touchpoints are the occasional macOS Keychain / `sudo`
password prompt (and even those did not fire on an already-provisioned machine).

**Fix idea:** one explicit line at the top of the README's Genesis section —
"A coding agent runs every command in this section, including `bootstrap.py`,
and answers its confirm prompts. The only steps that need the human are an
occasional Keychain/sudo password." Drop or reframe "must be present" so it
reads as "reachable for a password prompt," not "runs the install."

## 2. `confirm_interactive` silently DECLINES on a non-tty — undocumented trap

`bootstrap.py:confirm_interactive` calls `input()` and catches `EOFError` as a
**decline**. An agent driving through a non-interactive tool (no TTY on stdin)
therefore auto-declines *every* gated step and the birth quietly no-ops with
`needs_user_action`, looking like a stall rather than an error. Nothing in the
README tells the driving agent how to answer the prompts non-interactively.

What actually works from a non-tty agent shell:
```
yes | HOMUNCULUS_NAME=<name> python3.13 bootstrap.py
```
On an already-provisioned machine only `role_and_db` and `venv_and_seed`
actually prompt (the rest probe-skip), and both prompts are intended
mutations — so `yes |` is safe here. But relying on `yes |` is a footgun in
general.

**Fix idea:** add an explicit `--assume-yes` / `--agent` flag (or honor a
`HOMUNCULUS_ASSUME_YES=1` env var) so agent intent is declared, and document it
in the README. That is safer and clearer than piping `yes`, and it lets the
confirm prompts still guard a genuine interactive run.

## 3. "Machine already running a homunculus?" note describes a superseded model

The README's callout says bootstrap "will stop at its `role_and_db` step (the
shared database role exists, but this homunculus's own database does not)."
That describes an **older shared-`ananta`-role** model. The rest of the seed
(and `bootstrap.py` itself) is on **per-homunculus isolation**: role = db =
schema = `HOMUNCULUS_NAME`, no shared role, so a clean second homunculus is
fully `ABSENT` at the probe and takes the normal create path — it does *not*
stop. The two descriptions coexist and contradict.

**Fix idea:** rewrite that callout for per-homunculus isolation, or delete it.
As written it predicts a stop that will not happen for a fresh name.

## 3b. The deferential "offer / ask the operator" framing undercuts the whole design

Operator ruling (2026-07-21): **the intent is to put the driving agent in the
driver's seat.** The hydration runbook's pervasive "make the offer, act only on
an explicit yes, do not build unasked" language pushes the agent the opposite
way — into asking permission for judgments it is supposed to make. It made me
present Step 3/4/5 as a menu of questions instead of reviewing the environment
and deciding. The runbook should tell the agent to **investigate and decide**,
reserving true operator questions for genuinely divergent / destructive /
credential choices — not for every step.

Concrete casualty: **Step 3 (shell integration)**. The runbook says "NEVER read
the user's `~/.zshrc`." That HARD rule directly contradicts "review the
environment and use your judgement," and it left me unable to integrate `dax`
intelligently alongside an existing rich `~/.zshrc` (the bizops fleet). The
operator explicitly overrode it: review the shell config, understand it, and
integrate additively. **Fix idea:** replace the blanket no-read rule with
"read to understand structure; never echo or transmit secret values; integrate
additively; whole-file replacement is the fallback, not the default."

## 3c. MCP is framed as "optional co-equal" — but it is BANNED here by admin policy

The README/runbook present the MCP bridge as an equal alternative to the
`<name>` command ("you can ALSO register the bridge"). In THIS environment
company admins **prohibit MCP servers entirely** — so MCP-free is not a
preference, it is the only lawful path, and the `<name>` command +
(coming) no-MCP push watcher are THE interface. **Fix idea:** the docs should
name "MCP restricted by policy" as a first-class supported deployment and make
the MCP-free path primary, with MCP registration clearly labeled
"only where policy permits." Do NOT register the bridge for `dax`.

## 3d. Plugin "activation" design is install-all-now, configure-on-first-use — not per-plugin offers

Operator ruling (2026-07-21): **all** domain plugins should be installed /
activated at hydration; credential setup happens lazily, the first time the
operator actually uses each one (the agent walks them through it then). Step 4b
reads instead as "pitch each plugin, set it up now only on an explicit yes,"
which is the wrong shape — it conflates *activation* (do all, now) with
*credential configuration* (defer to first use). **Fix idea:** split Step 4b
into "activate every shipped domain plugin now" (no per-plugin ask) and a
separate "first-use credential walkthrough" contract keyed off
`hydration_guidance.md`'s `## Setup` section, triggered when the plugin is first
invoked, not at hydration.

## 4. Things that were clear and good (keep them)

- **Probe-first + stop-and-ask.** Every step reads state read-only, skips if
  healthy, prints the exact command before mutating, and surfaces divergence as
  a named `needs_user_action` instead of force-fixing. Made it safe to reason
  about blast radius before running anything.
- **`pg_hba` handling is insert-only + idempotent** and re-asserts the login
  user's `trust` lines *above* the new `scram-sha-256` block. I was able to
  prove it could not lock an existing same-machine workload (bizops, which
  connects as the login user over localhost) out of Postgres.
- **Per-homunculus isolation** (own role/db/schema, PUBLIC-connect revoked,
  self-seeded password, no cross-namespace credential copy) is a clean model and
  well-explained in the runbook's Step 0.
- **`bootstrap.py`'s module docstring** (the three-layer architecture, "no layer
  installs the dependency it needs to exist to run") is genuinely good
  orientation — reading it first paid off.

## Environment where `dax` was born (for reference)
- macOS, login user `david.westgate`, Homebrew present.
- PostgreSQL 17.9 (Homebrew `postgresql@17`), `pgvector` available, blanket
  `all all trust` in `pg_hba.conf`, no prior scram block.
- LM Studio serving `text-embedding-nomic-embed-text-v1.5-embedding`.
- python3.13 via pyenv shim (3.13.7).
- Sibling workspace `bizops-knowledge-base` sharing the same Postgres, connecting
  as the login user over localhost (no password) — the thing the scram gate had
  to not break.

## 5. Bizops-profile COLD birth (bootstrap.py / run_genesis) hits successive gaps the free birth doesn't

The free profile (`macos-free-homunculus`) births perfectly via the stock cold
path: healthy bridge, KB searchable MCP-free, `dax` command on PATH. Every
failure below appeared ONLY when birthing the FULL `macos-bizops-homunculus`
profile via `run_genesis(profile_name=...)`. Strong hypothesis: the full profile
is exercised/tested via the `birth_homunculus` EDGE verb from a *running parent*
homunculus (which likely materializes more), NOT via the no-parent
`bootstrap.py` cold start. The cold path for the full tier is under-tested.

### 5a. Vault keychain not cleared on teardown → InvalidPassphraseError crash-loop
Tearing a homunculus down by dropping its DB + deleting its clone does NOT clear
its macOS Keychain namespace. Re-birthing the same name then finds a STALE
wrapped master key at `security` service `<name>-vault` / account `master-key`
(wrapped by the OLD passphrase); the freshly-seeded passphrase can't unwrap it,
so `macos_vault_plugin.prepare_for_readiness` fails and the LaunchAgent
crash-loops. **Fix idea:** (a) teardown/undertaker must delete keychain services
`<name>-vault` and every `<name>.<plugin>`; (b) genesis should DETECT a stale-
keychain-vs-fresh-DB mismatch and stop-and-ask instead of crash-looping.
**Workaround:** `security delete-generic-password -s "<name>-vault"` (loop) +
`-s "<name>.<plugin>"` before re-birth.

### 5b. Missing `profile/config/prompts/system.json` → orchestrator init crash on the full profile
`ananta/core/prompts/stages/format.py` loads a GLOBAL system prompt from
`profile/config/prompts/system.json`, and `inject_memory_service` reads it at
boot — but NO `system.json` ships anywhere in the seed and genesis's
`config_materialize` only creates the empty `prompts/` dir (it materializes
`identity.json`, not `system.json`). The free profile survives because it is
VACANT-inference and never loads the prompt pipeline; the bizops profile binds
inference + memory REAL, so the missing file hard-crashes orchestrator init:
`inject_memory_service failed: [Errno 2] No such file or directory:
'.../prompts/system.json'`. **Fix idea:** genesis must materialize a default
`system.json` (from a profile_baseline template) for any profile that binds a
real inference/memory service — same treatment `identity.json` already gets.
