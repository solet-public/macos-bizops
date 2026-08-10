# dax.zsh — per-homunculus shell environment, sourced by ~/.zshrc.
# Lives in the deployment directory so improvements arrive via `git pull`, never another
# ~/.zshrc replacement.
#
# Commands this puts on PATH:
#   claude-dax [label] [model] [effort]
#       start a properly-named Claude Code session connected to dax
#   codex-dax [role] [codex arguments ...]
#       start stock Codex with the no-MCP watcher and durable inbox identity armed
#   launch-dax
#       run dax in the foreground (debugging; launchd is the
#       normal run mode — see the hydration runbook before using this)

export PATH="/Users/david.westgate/Workspace/dax/client/bin:$PATH"

# Fleet coordination functions (optional multi-role setup — sourced here after
# operator accepted Step 4a of the hydration runbook).
source "/Users/david.westgate/Workspace/dax/client/dax-fleet.zsh"
