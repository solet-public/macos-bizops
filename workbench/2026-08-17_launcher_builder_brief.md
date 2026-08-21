# Launcher-Builder brief — BizOps Dax desktop/Dock launcher, 2026-08-17

Dispatched by: Operator session. Report back to role `Operator` when done.
Scope: build one macOS launcher; no git anywhere; then stop.

## Goal

A double-clickable launcher, on the Desktop AND in the Dock, that opens a new
iTerm2 window and starts the operator's standard work session:

1. Upgrade Claude Code via Homebrew if an update exists. The operator
   suggested `brew upgrade claude-code@latest`, but FIRST detect what is
   actually installed (`brew list --formula | grep -i claude`;
   `brew list --cask | grep -i claude`) and use the real name. Make it
   non-fatal (`|| true`) so an up-to-date install or network hiccup doesn't
   block the session.
2. `cd ~/Workspace/bizops-knowledge-base`
3. `source .venv/bin/activate` (verify that venv exists; if missing, note it
   in your report and guard the line so it doesn't hard-fail).
4. Run `claude-dax` (lives at
   /Users/david.westgate/Workspace/dax/client/bin/claude-dax, on PATH via
   .zshrc; the iTerm window runs interactive zsh so .zshrc applies — verify
   the file exists and is executable).

## Implementation

- Verify iTerm2 at /Applications/iTerm.app (AppleScript name: "iTerm").
- Build an AppleScript applet:
  `osacompile -o "$HOME/Applications/BizOps Dax.app" <script>` (create
  ~/Applications if needed). The script: tell iTerm to activate, create a
  window with default profile, `write text` the full command chain.
- Desktop: symlink `~/Desktop/BizOps Dax` → the .app (operator explicitly
  asked for a symlink).
- Dock: check `defaults read com.apple.dock persistent-apps` for an existing
  entry first (no duplicates), then `defaults write com.apple.dock
  persistent-apps -array-add '<dict><key>tile-data</key><dict><key>file-data
  </key><dict><key>_CFURLString</key><string>file:///Users/david.westgate/
  Applications/BizOps%20Dax.app/</string><key>_CFURLStringType</key>
  <integer>15</integer></dict></dict></dict>'` (single line, no spaces in
  the XML) and `killall Dock` to apply — the Dock restarting briefly is
  expected.
- Icons: only if trivially doable; don't spend time on it.

## Testing constraints

- Do NOT do a full end-to-end click test: the final step launches an
  interactive Claude session, which must not be spawned as a test.
- Instead: confirm osacompile succeeds; optionally test iTerm automation
  with a throwaway `osascript` opening a window that only runs
  `echo launcher-test-ok` (a one-time macOS Automation permission prompt may
  appear on the operator's screen — fine, mention it in the report).
- Verify the Dock plist entry exists and the Desktop symlink resolves.

## Hard constraints

- NO git commands of any kind (a PreToolUse gate blocks git mutations from
  non-controller sessions; you don't need git).
- Don't modify .zshrc or any startup script.

## Report

Send ONE report to role `Operator` (cross-session SendMessage addressed to
"Operator"; if messaging is unavailable, write it to
/Users/david.westgate/Workspace/dax/workbench/2026-08-17_launcher_builder_report.md):
exact paths created, detected brew package name, the exact command chain the
launcher runs, Dock entry added yes/no, test results, and caveats the
operator should expect on first click (Gatekeeper/Automation prompts).
