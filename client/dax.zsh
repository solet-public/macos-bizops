# dax.zsh — per-solet shell environment, sourced by ~/.zshrc.
# Lives in the deployment directory so improvements arrive via `git pull`, never another
# ~/.zshrc replacement.
#
# Commands this puts on PATH:
#   claude-dax [label] [model] [effort]
#       start a properly-named Claude Code session connected to dax. This is the
#       single launch command: pass a role label (e.g. Git-Controller) to arm the
#       git gate and claim that role; Dax-Main spawns and manages any further fleet
#       sessions itself via plugin::agent_messaging_plugin::spawn_session (which
#       also selects each worker's inference provider per spawn).
#   codex-dax [role] [codex arguments ...]
#       start stock Codex with the no-MCP watcher and durable inbox identity armed
#   launch-dax
#       run dax in the foreground (debugging; launchd is the
#       normal run mode — see the hydration runbook before using this)

export PATH="/Users/david.westgate/Workspace/dax/client/bin:$PATH"
