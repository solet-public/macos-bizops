# dax-fleet.zsh — multi-role session functions for the dax homunculus.
# Sourced by client/dax.zsh. Lives in the clone so it arrives via
# `git pull`, never another ~/.zshrc edit.
#
# DIFFERENT trust level than a plain `claude` session, which never passes
# --dangerously-skip-permissions. Every function below does — running
# fourteen concurrent sessions isn't practical with a permission prompt on
# every action, but that removes the per-action safety boundary. Operator
# ruling (2026-07-22): accept that tradeoff for the whole fleet.
#
# --remote-control "$role" makes each session addressable by Claude Code's
# remote-control feature from another session (what lets a coordinator drive
# or monitor others) — a Claude Code feature, no MCP involved. Peer messaging
# and role binding need no launch flag: each session's SessionStart hook arms
# the `dax watch` registered-presence watcher (register + claim + receive,
# zero MCP). The optional MCP development-channel wake is OFF by default; set
# dax_FLEET_MCP_CHANNELS=1 to opt in only where policy permits MCP.
#
# Git lock-down: every role exports GIT_CONTROLLER_NAME pointing at the one
# role allowed to touch git. Consumed by the `coordination-hooks` Claude Code
# plugin's PreToolUse gate (hooks/git_controller_gate.py), which gates git
# mutations AND the Task/subagent tool, and which resolves the calling
# session's name from ~/.claude/sessions/<pid>.json — so it matches the
# launcher's `--name "$role"` below. NOTE the older path is dead: a copy at
# ~/.claude/hooks/git_controller_gate.py wired into ~/.claude/settings.json no
# longer runs at all under enterprise strictPluginOnlyCustomization:["hooks"],
# which admits plugin-sourced hooks only. Until that plugin is internally
# hosted, reviewed, approved and installed, this export is inert and the fleet
# has NO git gate. Change _FLEET_GIT_CONTROLLER_NAME below to redesignate;
# leave it empty to disable the gate for this fleet.
_FLEET_GIT_CONTROLLER_NAME="Git-Controller"

# Provider toggle (claude-use-bedrock / claude-use-anthropic / _claude_apply_provider)
# lives in ~/.zshrc — self-contained there, not sourced from any project repo.

_claude_for_dax() {
  local role="$1"
  local model="$2"
  local effort="$3"
  shift 3

  # Per-session id for AGENT_SESSION_ID (see ENV CONTRACT below).
  local session_id="ases-$(date +%s)-$$-${RANDOM}"

  _claude_apply_provider

  # MCP development-channel wake is OFF by default (admin policy disabled MCP;
  # sessions receive via the `dax watch` watcher the rename skill arms). Opt in
  # only where policy permits MCP: dax_FLEET_MCP_CHANNELS=1.
  typeset -a mcp_flags
  [[ "${dax_FLEET_MCP_CHANNELS:-0}" == "1" ]] && mcp_flags+=(--dangerously-load-development-channels server:dax)

  # SESSION BOOTSTRAP (2026-07-27): enterprise policy set
  # strictPluginOnlyCustomization:["hooks"] in ~/.claude/remote-settings.json,
  # disabling ALL settings.json hooks (only plugin-sourced hooks run, and
  # plugins are approval-gated / unavailable to us). That silently killed the
  # SessionStart role-reclaim hook and the UserPromptSubmit "STEP ZERO" hook.
  # We can no longer FORCE a tool call at session start, so we inject their
  # substance into the system prompt via --append-system-prompt: always present,
  # applied at launch, per-role, immune to the hook restriction. Advisory, not
  # deterministic — the model must self-enforce. Keep in sync with AGENTS.md
  # "STEP ZERO". (Note: the git-controller PreToolUse gate is also dead under
  # this policy; that is tracked separately, not solved here.)
  local bootstrap_prompt="SESSION BOOTSTRAP — dax fleet, role: $role. Enterprise policy (strictPluginOnlyCustomization: hooks) disables all settings.json hooks, so the SessionStart and UserPromptSubmit 'STEP ZERO' hooks that used to enforce the rules below NO LONGER RUN. Self-enforce them:
1) FIRST TURN: before any other tool call this session, consult dax. Run 'dax health', then 'dax search' with a plain-English description of the user's task. Do not begin repo or domain work until you have checked dax for prior decisions, runbooks, and context relevant to the request.
2) STEP ZERO (every turn): before other tool calls, decide whether the turn needs dax context (prior decisions, design records, runbooks, past-session status) and query dax first if so, using the local 'dax' command — no MCP. Only skip when merely locating a known code symbol or file.
3) ROLE RE-CLAIM: this session is $role. If this conversation has not already re-claimed the role, invoke the rename skill now (Skill tool, skill: rename, args: $role) so delivery routes refresh after /clear, restart, or reconnect.
Do not register or troubleshoot MCP unless the user says policy permits it."

  # ENV CONTRACT (2026-07-29): unprefixed AGENT_* names ONLY.
  #   The five-name per-session identity family (AGENT_IDENTITY,
  #   AGENT_INSTANCE_ID, AGENT_SESSION_LABEL, AGENT_SESSION_ID, AGENT_ROLE) is
  #   runner-neutral seed contract; agent_messaging_plugin/env_contract.py is
  #   its single source of truth and every read site uses the neutral name —
  #   local_cli/spool.py, which used to hard-code the prefixed pair, now
  #   imports from it. Operator directive: no 'dax' or 'homunculus' in any env
  #   var that interacts with the `coordination-hooks` plugin either, so the
  #   unprefixed names are authoritative on both sides.
  #   DO NOT re-add HOMUNCULUS_AGENT_SESSION_LABEL / _SESSION_ID. As of seed
  #   df192c6 exporting a prefixed member of the family is the ONLY thing that
  #   can trip env_contract.enforce_no_legacy_agent_env(), which fail-loud
  #   refuses to arm `dax watch` ("un-migrated fleet identity environment").
  #   Its docstring is explicit: legacy absent is the steady state, legacy+
  #   neutral was tolerated for the flip window only. Verified 2026-07-29:
  #   neutral-only arms and claims cleanly.
  #   HOMUNCULUS_NAME / _COLOR / _INSTANCE_ID are a DIFFERENT, still-valid
  #   family — they name the homunculus and its deployment slot, not a session,
  #   are read all over the platform, and are set by the supervisor, not here.
  #   Nothing else here may carry a 'homunculus' or 'dax' token: this launcher's
  #   env is the input to hooks we intend to ship as a REVIEWED Claude Code
  #   PLUGIN, and the seed's own coordination-hooks plugin already reads the
  #   unprefixed names (GIT_CONTROLLER_ENV = "GIT_CONTROLLER_NAME"). The
  #   HOMUNCULUS_GIT_CONTROLLER_NAME export was dropped 2026-07-29 along with
  #   the last reader of it (~/.claude/hooks/git_controller_gate.py).
  # AGENT_WAKE_CLI is a VALUE not a name, so "dax" here is fine; the hook runs
  # exactly `$AGENT_WAKE_CLI wake` with fixed argv and no shell.
  # FLEET_TRANSPORT: "watch" keeps the Stop-hook waiter armed; any other value
  # deliberately disarms it (for deployments whose live bridge already wakes).
  # DIAGNOSTIC 2026-07-23: --remote-control "$role" temporarily removed to
  # test whether child/remote sessions suppress hook execution. To RESTORE,
  # put `--remote-control "$role"` back on the --name line below.
  # NOTE: this comment MUST stay above the assignment chain — a comment between
  # a `\`-continued assignment and `exec` breaks the chain, so the vars never
  # export to the child (they become a standalone assignment-only statement).
  AGENT_SESSION_LABEL="$role" \
  AGENT_SESSION_ID="$session_id" \
  GIT_CONTROLLER_NAME="$_FLEET_GIT_CONTROLLER_NAME" \
  AGENT_WAKE_CLI="dax" \
  FLEET_TRANSPORT="${FLEET_TRANSPORT:-watch}" \
  exec claude \
      --name "$role" \
      --model "$model" --effort "$effort" \
      --append-system-prompt "$bootstrap_prompt" \
      "$@" \
      --debug \
      --dangerously-skip-permissions \
      "${mcp_flags[@]}"
}

# Clear stale aliases of the same name (zsh expands aliases at parse time,
# before functions are consulted, so a leftover alias would shadow these).
unalias claude-git-controller claude-coordinator-a claude-coordinator-b claude-coordinator-c \
        claude-architect claude-a claude-b claude-c \
        claude-reviewer-a claude-reviewer-b claude-reviewer-c \
        claude-tester-a claude-tester-b claude-tester-c 2>/dev/null

# Sole git-mutator — the only role the gate lets touch git.
function claude-git-controller { _claude_for_dax Git-Controller opus xhigh "$@" }

# Coordinators — drive work, delegate, hold the plan. Run as many concurrently
# as you have active workstreams.
function claude-coordinator-a { _claude_for_dax Coordinator-A opus xhigh "$@" }
function claude-coordinator-b { _claude_for_dax Coordinator-B opus xhigh "$@" }
function claude-coordinator-c { _claude_for_dax Coordinator-C opus xhigh "$@" }

# Design / second-opinion review, distinct from whoever is doing the work.
function claude-architect { _claude_for_dax Architect opus xhigh "$@" }

# Workers — parallel task lanes.
function claude-a { _claude_for_dax A opus xhigh "$@" }
function claude-b { _claude_for_dax B opus xhigh "$@" }
function claude-c { _claude_for_dax C opus xhigh "$@" }

# Reviewers.
function claude-reviewer-a { _claude_for_dax Reviewer-A opus xhigh "$@" }
function claude-reviewer-b { _claude_for_dax Reviewer-B opus xhigh "$@" }
function claude-reviewer-c { _claude_for_dax Reviewer-C opus xhigh "$@" }

# Testers — Sonnet 5 (proven for QA lenses + quiz-taking work).
function claude-tester-a { _claude_for_dax Tester-A sonnet xhigh "$@" }
function claude-tester-b { _claude_for_dax Tester-B sonnet xhigh "$@" }
function claude-tester-c { _claude_for_dax Tester-C sonnet xhigh "$@" }
