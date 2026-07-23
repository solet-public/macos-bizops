# Grow a homunculus from seed

This repository is a **clean, public-safe homunculus seed**: a fresh
source tree with no operator identity, no credentials, and no git history. It
mints a new *homunculus* — a named agentic instance — on your own machine.

The seed itself carries **no secrets**. Credentials are provisioned locally at
genesis time (macOS Keychain + PostgreSQL) in your own terminal — never embedded
in this tree, never passed through an agent conversation. Inference stays local
(a `nomic` embedding model served on your machine); there are zero API keys.

## Getting this seed

Clone this repository to your machine, then work from inside it:

```
git clone <this-repo-url> {{HOMUNCULUS_NAME}}
cd {{HOMUNCULUS_NAME}}
```

Every command below runs from inside that clone. (If you received this seed as a
plain local folder rather than a GitHub repository, just `cd` into it — there is
nothing to clone.)

## Prerequisites

The genesis ladder installs-or-verifies each of these with the driving agent:

- macOS with Homebrew
- Python 3.13 and `git`
- PostgreSQL with the `pgvector` extension
- A local embeddings server (LM Studio / LM Server) serving a `nomic` model

## 🌱 Genesis — birth a homunculus from this seed

This section is written for the coding agent driving the birth. The agent runs
the commands, including `bootstrap.py`; the human operator only intervenes for
OS prompts, credential/tenant choices, destructive name collisions, or a state
the bootstrap reports as `needs_user_action`.

The stock path is the cold path: `git clone` → `bootstrap.py`. No parent
homunculus is required. `bootstrap.py` reads this seed's `PROVENANCE.json` and
births the matching profile (for example, `bizops_standard` →
`macos-bizops-homunculus`). Do not hand-edit `profile/config/manifest.yaml` to
change tiers. If a maintainer explicitly tells you to override the selected
profile, set `HOMUNCULUS_PROFILE=<profile-template>` before bootstrap.

1. **Agree a name** for the new homunculus with the user. It must be lowercase
   and match `[a-z][a-z0-9_-]{1,62}` — call it `<name>` below.
2. **Export the name** so the bootstrap chain can consume it:

   ```
   export HOMUNCULUS_NAME={{HOMUNCULUS_NAME}}
   ```

3. **Run the Layer-0 bootstrap** (stdlib-only — it installs/verifies Homebrew,
   PostgreSQL, pgvector, the local embeddings server, the virtualenv, and seeds
   the tree). For an agent-driven run, approve bootstrap's printed action plans
   explicitly with `HOMUNCULUS_ASSUME_YES=1`:

   ```
   HOMUNCULUS_ASSUME_YES=1 python3.13 bootstrap.py
   ```

4. The in-venv genesis chain then validates the name, materializes the newborn's
   configs, writes the required identity and global prompt files, checks for
   stale same-name vault state, and marks the attempt. Divergent host states
   stop as `needs_user_action` rather than force-fixing.

> **Machine already running a homunculus?** `bootstrap.py` will stop at its
> `role_and_db` step only for a genuine partial/colliding state. Under
> per-homunculus isolation a clean second homunculus has neither role nor
> database yet, so it takes the normal create path. If you are reusing a name
> from a failed attempt, also clear that homunculus's stale macOS Keychain
> namespace before re-birth: service `<name>-vault`, account `master-key`, plus
> any per-plugin services named `<name>.<plugin>`. Genesis refuses a fresh clone
> paired with an old `<name>-vault/master-key` entry so the newborn does not
> crash-loop under launchd.

## Driving your homunculus — the `<name>` command (no MCP required)

Genesis installs a command named after your homunculus on your `PATH`
(`~/.local/bin/<name>`, a symlink into the newborn's own venv). It talks ONLY
to this homunculus, over its localhost bridge — no MCP server, no special
client capabilities, works even where MCP servers are restricted:

```bash
<name> health                       # is the homunculus up?
<name> search "how do I ..."        # knowledge-base search
<name> call <process_key> '{"arg": "value"}'   # invoke any registered process
<name> result <action_id> --wait    # fetch/await an async result
<name> schema <process_key>         # inspect a process's argument schema
```

Knowledge-base access without MCP, for example:

```bash
<name> call service_interface::knowledge_service::search '{"query": "hydration runbook", "top_k": 8}'
```

Async results are pulled with `<name> result <id> --wait`. For push — peer
and role-addressed messages reaching your session unprompted, with no MCP —
the seed ships both halves of the receive path:

```bash
<name> watch                        # register + claim this session's role,
                                    # then stream messages as JSON lines
<name> wake                         # Stop-hook waker: blocks on the watch
                                    # spool, wakes an idle session on delivery
```

`watch` reads the launcher-exported `HOMUNCULUS_AGENT_SESSION_LABEL` /
`HOMUNCULUS_AGENT_SESSION_ID` (or pass `--role`), registers the session in the
peer registry, claims the role as its durable binding, drains messages that
arrived while unwatched, then prints one JSON line per delivery and nothing
while idle — teeing each delivery into a per-session spool. `wake` is wired
as a Claude Code `Stop` hook by hydration: it blocks on that spool at zero
token cost and turns the next delivery into a session turn, on any inference
provider (it is a shell hook, not an inference channel). Both reconnect and
re-claim automatically across restarts and blue-green swaps. The optional MCP
bridge below provides a tool-native wake where policy permits MCP; neither is
a prerequisite for the other.

## Updating later

Seed releases are append-only commits to this same repository, so an update
never rewrites history and never touches what your homunculus has become
(database, memories, credentials). The short form: `git pull --ff-only` in
the clone, restart the homunculus, wait for startup to finish. The complete
procedure — including when the venv needs attention and which setup steps to
re-run — is the seed-update runbook in your homunculus's own knowledge base:

```bash
<name> call service_interface::knowledge_service::search '{"query": "seed update runbook", "top_k": 3}'
```

## Optional: register the MCP bridge (not needed for anything above)

You do not need this section — everything above, including push via
`<name> watch`, works with zero MCP, and that is the default way to run. If
you specifically want tool-native `mcp__<name>__*` access and your
environment permits MCP servers, you can additionally register the bridge
with Claude Code. In managed environments where MCP is restricted by policy,
there is nothing to miss: skip this section entirely. Genesis does not register
anything into your Claude Code configuration itself — that config is *yours*,
on the operator's side of the genesis boundary, not something newborn-venv code
should reach across and mutate. To register:

1. **Probe your own CLI first**: `claude mcp add --help`. Read its actual flags rather than assuming — CLI syntax drifts across versions.
2. **Act** — reference form as of 2026-07 (confirm against what step 1 actually showed you; treat this as a starting point, not gospel):
   ```bash
   claude mcp add --scope user \
     -e HOMUNCULUS_NAME=<name> -e HOMUNCULUS_AGENT_IDENTITY=claude_code \
     <name> -- <clone>/.venv/bin/python3 -m agent_messaging_plugin.mcp_bridge
   ```
   `--scope user` is what makes this cwd-independent — the user can open Claude Code in any directory afterward and still reach this homunculus.
3. **Verify**: `claude mcp list` shows `<name>`, and a fresh Claude Code session can see `mcp__<name>__*` tools and successfully call one (e.g. `mcp__<name>__current_identity`).
4. **If it reports already-exists**: inspect with `claude mcp get <name>` before touching anything — don't blindly remove+re-add over a working registration for a *different* purpose. If it is genuinely stale, remove then re-add.
5. **If your CLI predates the `mcp` subcommand, or step 1/2 above no longer works**: stop and ask the user how they'd like to connect — this is a sanctioned stop, not a failure on your part.

## After first boot — operator environment hydration

After first boot, have the driving agent follow the seed-hydration runbook at
`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`
(searchable: 'hydration runbook operator environment setup' once the knowledge
base is ingested — or read it as a file at that path: pre-boot, before the
knowledge base exists, the file IS the runbook) to set up the shell launcher, a
properly-named Claude Code session, no-MCP command-line operation, and
first-use connector credential guidance.

For shell startup integration, the driving agent should inspect the user's
startup file and the shipped shell templates first, without printing
secret-looking values. It should then recommend a concrete plain-English plan
and ask for approval before writing; it should not ask the user to choose
between raw `.zshrc` implementation details up front.

## License

The free homunculus seed and bundled open-core components in this repository are
licensed under the Apache License, Version 2.0. See `LICENSE` and `NOTICE`.

Forks and redistributed versions must not imply endorsement by or affiliation
with the original project except as required for attribution under the
Apache-2.0 license.

Premium plugins, hosted-service features, and commercial add-ons are licensed
separately and are not included in this free seed unless expressly stated.

This is a **publish / reference-only** seed. It does not accept contributions,
pull requests, or issues — fork it and grow your own homunculus instead. Seed
updates are published as new sealed commits. See `CONTRIBUTING.md`.
