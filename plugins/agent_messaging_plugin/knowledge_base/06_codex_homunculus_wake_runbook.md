# Codex Homunculus Wake Runbook

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:codex, knowledge:tag:bridge, knowledge:tag:runbook

Article Tags: planning-stage:agent-onboarding, planning-stage:operator-runbook, evidence-category:local-ops-contract, domain:agent-messaging, domain:codex-wake

Embedding Description: Local operating runbook for the patched Codex homunculus peer-wake integration: what the patch does, which binary homunculus launchers use, why Homebrew upgrades do not update the active homunculus wake binary, how the .zshrc launcher guard and 1am daily updater work, and the safe rebase/build/install procedure.

## Purpose

Document the local Codex extension that lets a homunculus wake a live Codex CLI
session through `peer_send IMPORTANT`. This is not stock Homebrew Codex
behavior in this environment. It depends on both homunculus bridge code and a
locally patched Codex CLI binary.

## Mechanism

The wire path is:

1. A live peer sends `mcp__<server-name>__peer_send` with message prose starting
   `IMPORTANT`.
2. The homunculus persists the peer message in `core__agent_thread` /
   `core__agent_message`.
3. For a Codex recipient, `peer_dispatch.py` appends a `peer_message`
   bridge event. The delivery result is `queued_notification`, not
   `queued_wake`, because Codex does not use the homunculus's
   `peer_registry` native wake adapter table.
4. The Codex MCP bridge subprocess is registered as `agent_id="codex"`.
   `mcp_bridge/forwarder.py` emits Codex peer/post events as
   `notifications/homunculus/peer_message`.
5. The patched Codex CLI has an `McpServerNotificationSink` threaded
   through `codex-rs/rmcp-client`, `codex-rs/codex-mcp`, and
   `codex-rs/core`.
6. That sink handles only `notifications/homunculus/peer_message`, decodes the
   content and `trigger_turn` metadata, and enqueues Codex
   `InterAgentCommunication`.
7. Codex's existing mailbox path starts a normal turn when idle or queues
   the message while another turn is active.

The patch deliberately does not turn arbitrary MCP notifications into
model prompts. The accepted method is exactly
`notifications/homunculus/peer_message`.

## Local Contract

For any homunculus that needs Codex peer wake:

- `codex` must resolve to a locally patched binary named with the neutral
  `codex-homunculus-wake-<version>` convention.
- The patched binary must be built from the matching upstream Codex release
  plus the homunculus notification-sink patch.
- Stock Homebrew Codex may be installed for comparison, but it must not be the
  binary used by homunculus peer-wake launchers unless it has been verified to
  include the same notification sink.
- Launcher aliases, launchd jobs, and updater scripts are one coordinated
  contract. During a naming migration, repoint all of them to the neutral path
  in the same change; do not leave an old-named command on the active launch
  path.

The `.zshrc` homunculus launchers rely on PATH resolving `codex` to
`~/.local/bin/codex`.

## What Homebrew Does And Does Not Do

`brew update` refreshes Homebrew metadata. The shared homunculus launcher prep
does this at most once every four hours and prints outdated packages.

`brew upgrade --cask codex` updates `/opt/homebrew/bin/codex` and the
Homebrew cask payload. It does not rebuild `~/Workspace/codex-rs-wake`,
does not update `~/.local/bin/codex-homunculus-wake-*`, and does not move the
`~/.local/bin/codex` symlink.

Do not assume that a Homebrew Codex upgrade preserves the homunculus peer wake. In
this setup, the active homunculus wake binary is the patched local binary.

## Launcher Guard

The `.zshrc` `codex-yolo`, `codex-reviewer`, and restart helpers run a
small guard before `exec codex`:

- `command -v codex` must resolve to `~/.local/bin/codex`.
- `~/.local/bin/codex` must be a symlink to a filename containing
  `codex-homunculus-wake-`.
- The guard prints both the active homunculus wake version and the Homebrew
  stock version when they differ.

The guard is intentionally a status and safety check, not an automatic
rebase. Blindly rebasing and rebuilding Codex during every interactive
launch would be slow and could strand the operator without the known-good
wake binary.

The launchers also expose a manual updater helper:

```bash
codex-homunculus-wake-update --check
codex-homunculus-wake-update
```

The helper delegates to the local operator-tooling updater under
`plugins/agent_messaging_plugin/tools/` in the operator checkout. Keep the
helper name, launchd command, and updater script name neutral and aligned.

## Daily Update Automation

The normal update path is a launchd job scheduled for 01:00 local time:

```text
~/Library/LaunchAgents/com.openai.codex-homunculus-wake-update.plist
```

It runs:

```bash
/path/to/<homunculus>/plugins/agent_messaging_plugin/tools/<neutral-codex-wake-updater>.sh
```

The updater is intentionally conservative:

1. It takes a lock under `~/.local/state` so only one update runs.
2. It fetches upstream Codex tags from `~/Workspace/codex-rs-wake`.
3. It considers only stable `rust-vX.Y.Z` tags, not prerelease tags.
4. If `~/.local/bin/codex` already points at
   `codex-homunculus-wake-<latest-version>`, it exits without building.
5. It derives the homunculus wake patch from the latest local
   `homunculus-wake-rust-v*` branch.
6. It creates or reuses `~/Workspace/codex-rs-wake-<version>`.
7. It applies the homunculus wake patch with `git apply --3way` for new target
   branches.
8. It runs focused validation:
   `cargo fmt --check`,
   `cargo test -p codex-core peer_message_notification`,
   `cargo test -p codex-rmcp-client notification --lib`,
   `cargo test -p codex-mcp notification --lib`, and the homunculus
   `forwarder_native_notification_smoke.py`.
9. It builds `cargo build --release -p codex-cli --bin codex`.
10. Only after validation and build succeed does it install
    `~/.local/bin/codex-homunculus-wake-<version>` and atomically switch
    `~/.local/bin/codex`.

If patch application, tests, or build fail, the updater exits non-zero
and leaves the existing working symlink in place. Logs are written to:

```text
~/Library/Logs/codex-homunculus-wake-update.log
~/Library/Logs/codex-homunculus-wake-update.err
```

## MCP Bridge Refresh And Stale Bridge Recovery

Codex discovers MCP servers when a session starts or when the interactive
client reconnects MCP. The homunculus entry in `~/.codex/config.toml` launches:

```toml
[mcp_servers.<server-name>]
command = "/path/to/<homunculus>/.venv/bin/python3"
args = ["-m", "agent_messaging_plugin.mcp_bridge"]

[mcp_servers.<server-name>.env]
HOMUNCULUS_NAME = "<homunculus>"
HOMUNCULUS_AGENT_IDENTITY = "codex"
```

That subprocess reads `~/.ananta/runtime/<homunculus>.bridge.port`, opens one
homunculus bridge session, receives an `agc-*` `bridge_id`, registers the
Codex peer identity, and caches the tool descriptors the MCP client
received at startup. Editing the Python bridge source or changing the
homunculus bridge behind it does not hot-swap this already-running MCP
subprocess.

The stale-bridge signature is a 404 against a closed `agc-*` route, for
example:

```text
the homunculus /api/v1/bridge/agc-e96e5e5e6001/peer/register failed (404):
bridge agc-e96e5e5e6001 not found or closed
```

Immediate recovery:

1. In an interactive Codex session, run `/mcp reconnect` if the command
   is available.
2. If reconnect is unavailable or still stale, restart the session
   through the homunculus launcher (`codex-yolo`, `codex-reviewer`, or the
   matching `codex-restart-*` helper).
3. After reconnect/relaunch, verify with `mcp__<server-name>__peer_register` or
   `mcp__<server-name>__peer_list`; the old `agc-*` id must no longer appear in
   the error path.

`codex mcp list` only shows configured MCP servers. It does not refresh
the live tool surface inside an already-running session. `brew update`,
`brew upgrade --cask codex`, and `codex update` are Codex distribution
update commands, not homunculus MCP reconnect commands.

Forwarder behavior after the 2026-06-07 repair: all bridge-bound MCP
tools route through the stale-bridge reconnect wrapper, and retries
build their HTTP paths from the fresh `bridge_id` after reconnect. A
running subprocess still has to be reconnected or relaunched once to load
that code and any changed tool descriptors.

## Safe Update Procedure

The normal daily path is the updater described above. Use this manual
procedure when the updater fails because the homunculus wake patch no longer
applies cleanly to a new upstream Codex release.

1. Preserve the working binary:

```bash
ls -l ~/.local/bin/codex ~/.local/bin/codex-homunculus-wake-*
~/.local/bin/codex --version
/opt/homebrew/bin/codex --version
```

2. Preserve the patch before changing the source checkout:

```bash
cd ~/Workspace/codex-rs-wake
HOMUNCULUS_REPO=/path/to/<homunculus>
git diff --binary > "$HOMUNCULUS_REPO/workbench/codex_homunculus_wake_$(date +%Y%m%d)_from_$(git describe --tags --always --dirty).patch"
```

3. Fetch upstream tags and choose the target release:

```bash
git fetch origin --tags
git tag -l 'rust-v*' | sort -V | tail
```

4. Rebase or reapply the patch on a branch for the target tag. Do this
in a new branch or worktree; keep the old detached checkout and old
binary until the new binary passes smoke tests.

```bash
git switch -c homunculus-wake-rust-vX.Y.Z rust-vX.Y.Z
git apply --3way "$HOMUNCULUS_REPO/workbench/codex_homunculus_wake_<date>_from_<base>.patch"
```

5. Resolve conflicts in the same conceptual areas:

- `codex-rs/rmcp-client`: typed `McpServerNotification` and sink callback.
- `codex-rs/codex-mcp`: pass the sink into `RmcpClient::initialize`.
- `codex-rs/core/src/session/mcp.rs`: handle
  `notifications/homunculus/peer_message` and enqueue `InterAgentCommunication`.
- `codex-rs/core/src/session/session.rs`: install the sink during MCP
  manager initialization.

6. Run focused tests:

```bash
cd ~/Workspace/codex-rs-wake/codex-rs
cargo test -p codex-core peer_message_notification
cd "$HOMUNCULUS_REPO"
.venv/bin/python3 plugins/agent_messaging_plugin/tests/forwarder_native_notification_smoke.py
```

7. Build the release binary:

```bash
cd ~/Workspace/codex-rs-wake/codex-rs
cargo build --release -p codex-cli --bin codex
```

8. Install without deleting the previous known-good binary:

```bash
new_version="$(target/release/codex --version | awk '{print $2}')"
install -m 0755 target/release/codex "$HOME/.local/bin/codex-homunculus-wake-$new_version"
ln -sfn "$HOME/.local/bin/codex-homunculus-wake-$new_version" "$HOME/.local/bin/codex.next"
mv -f "$HOME/.local/bin/codex.next" "$HOME/.local/bin/codex"
```

9. Smoke the launched path:

```bash
command -v codex
codex --version
codex-yolo --version
```

10. Roll back by restoring the symlink to an old known-good binary:

```bash
ls -l "$HOME/.local/bin/codex-homunculus-wake-"*
ln -sfn "$HOME/.local/bin/codex-homunculus-wake-<known-good-version>" "$HOME/.local/bin/codex.next"
mv -f "$HOME/.local/bin/codex.next" "$HOME/.local/bin/codex"
```

## When A Codex Wake Fails

Check in this order:

1. `command -v codex` resolves to `~/.local/bin/codex`.
2. `~/.local/bin/codex` points to `codex-homunculus-wake-*`.
3. `~/.codex/config.toml` sets `HOMUNCULUS_AGENT_IDENTITY = "codex"` under
   `[mcp_servers.<server-name>.env]`.
4. The target Codex session is registered in `mcp__<server-name>__peer_list`.
5. The sender used the `IMPORTANT` marker.
6. The receiver can see missed messages with
   `mcp__<server-name>__peer_inbox(include_important=true)`.
7. homunculus bridge forwarder smokes still pass.

If the stock Homebrew binary was launched by mistake, restart through
`codex-yolo` or `codex-reviewer` after fixing PATH/symlink state.
