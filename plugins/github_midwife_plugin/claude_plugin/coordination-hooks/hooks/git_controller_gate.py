#!/usr/bin/env python3
"""Git-Controller gate — PreToolUse hook enforcing an opt-in git-mutation policy.

This gate prevents trusted Claude peer sessions from accidentally invoking
git mutations directly. It is NOT an adversarial security boundary — the
threat model is peer *mistakes*, not deliberate evasion. A peer who wants to
bypass the hook can just edit the hook.

Opt-in + nameable: the gate enforces ONLY when the ``GIT_CONTROLLER_NAME``
env var names a controller role. UNSET -> the gate is OFF (every session may
run git and spawn subagents — the safe default when no controller role is in
use). Enabling the gate BLOCKS the Task tool (subagent spawning) for every
non-controller session — that IS the point: subagents spawn their own
worktrees and invoke git, which is exactly how in-progress work gets lost.

What the gate does:
  - Detects `git <subcommand>` invocations in Bash, including when wrapped
    in `bash -c "git ..."` / `eval "git ..."` / `$(git ...)`.
  - Allows read-only verbs (status, log, diff, show, etc.).
  - Blocks mutating verbs (commit, push, stash, checkout, merge, rebase,
    etc.) from any session other than the configured controller.
  - Blocks `Edit` / `Write` / `MultiEdit` / `NotebookEdit` writes into
    `<repo_root>/.git/`.
  - Blocks the `Task` tool (Agent-tool subagent spawning) from any session
    other than the configured controller, because subagents can invoke git
    from within their own session, bypassing this policy.

What the gate does NOT do:
  - Defend against ANSI-C, locale-quote, backslash-newline, here-string
    obfuscation. Trusted peers don't construct `$'\\x67\\x69\\x74'` by
    accident.
  - Detect runtime-script-source escapes (pipe-to-shell, here-doc,
    interpreter stdin, build-tool recipes). Same reason.
  - Validate substitution output content. `bash -c "$(...)"` allowed when
    the inner $(...) doesn't contain a literal `git`.

Hook contract: invoked by Claude Code as a PreToolUse handler.
`exit 2` BLOCKS the tool; any OTHER exit (including `exit 1` from an
accidental traceback) is NON-BLOCKING per Anthropic docs. The hook MUST
therefore own its error catching and explicitly return 2 on fail-closed
branches.

Stdlib-only by design — no runtime dependency beyond Python itself.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
from _git_controller_walker import walk_git_invocations

# Opt-in + nameable. The gate enforces ONLY when this env var names a
# controller role; UNSET -> the gate is OFF (every session allowed, subagents
# allowed).
GIT_CONTROLLER_ENV = "GIT_CONTROLLER_NAME"
EXPLORE_SUBAGENT = "Explore"


def git_controller_name() -> str | None:
    """The configured git-controller role name, or None when the gate is DISABLED.

    Empty/unset -> None, and the gate short-circuits to ALLOW everywhere
    (opt-in default-off).
    """
    name = os.environ.get(GIT_CONTROLLER_ENV, "").strip()
    return name or None

ALLOWED_NO_FLAG_CHECK = frozenset({
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "name-rev", "for-each-ref", "reflog", "rev-parse", "rev-list",
    "merge-base", "ls-files", "ls-tree", "ls-remote", "cat-file",
    "check-ignore", "verify-commit", "verify-tag",
    "var", "help", "version", "--version",
})

DUAL_MODE_ALLOWED: dict[str, frozenset[str]] = {
    "branch":    frozenset({"-v", "-l", "-r", "--show-current", "--list",
                            "--contains", "--no-contains", "-a", "--all"}),
    "tag":       frozenset({"-l", "-n", "--list", "--contains", "--no-contains"}),
    "stash":     frozenset({"list", "show"}),
    "remote":    frozenset({"-v", "show"}),
    "submodule": frozenset({"status", "summary"}),
    "worktree":  frozenset({"list"}),
    "bisect":    frozenset({"log", "view"}),
    "config":    frozenset({"--get", "--list", "--get-all", "--get-regexp"}),
}

UNIVERSAL_BANNED_FLAGS = frozenset({"--no-verify"})

# Inline git config keys that can affect mutation behavior — block as -c K=V.
DANGEROUS_C_KEY_PREFIXES: tuple[str, ...] = (
    "commit.gpgsign", "core.hooksPath", "gc.auto", "alias.",
)

DANGEROUS_GIT_GLOBALS = frozenset({"--git-dir", "--work-tree", "-C"})

_MUTATING_DUAL_FLAGS = frozenset({"-d", "-D", "-m", "-M", "--delete", "--force"})
_NOARG_READONLY_SUBS = frozenset(
    {"branch", "tag", "remote", "submodule", "worktree", "bisect"},
)

POLICY_MESSAGE = (
    "The git operation that was attempted must be managed by the designated "
    "controller session. If the operation you attempted is truly required in "
    "order to complete the task that you have been assigned, please escalate "
    "to the assigning agent."
)


def find_session_name() -> str | None:
    """Resolve the calling Claude Code session's ``name`` field, or None.

    Looks up ``~/.claude/sessions/<parent_pid>.json`` per the /rename
    contract. Retries once on transient read failure.
    """
    parent_pid = os.getppid()
    session_file = Path.home() / ".claude" / "sessions" / f"{parent_pid}.json"
    for _ in range(2):
        try:
            raw = json.loads(session_file.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            continue
        if isinstance(raw, dict):
            name = raw.get("name")
            if isinstance(name, str):
                return name
        return None
    return None


def _check_universal_banned(invocation: list[str]) -> tuple[bool, str]:
    """Universal flag bans + dangerous global git options + -c key=val keys."""
    rest = invocation[2:]
    for flag in UNIVERSAL_BANNED_FLAGS:
        if flag in rest:
            return True, f"banned flag {flag!r}"
    for global_opt in DANGEROUS_GIT_GLOBALS:
        if global_opt in invocation:
            return True, f"banned global git option {global_opt!r}"
    for idx in range(len(invocation) - 1):
        if invocation[idx] != "-c" or "=" not in invocation[idx + 1]:
            continue
        key, _, val = invocation[idx + 1].partition("=")
        blocked = _key_blocked(key, val)
        if blocked is not None:
            return True, blocked
    return False, ""


def _key_blocked(key: str, val: str) -> str | None:
    """Return a block reason if this -c key=val pair matches the dangerous set."""
    for prefix in DANGEROUS_C_KEY_PREFIXES:
        if not key.startswith(prefix):
            continue
        if prefix == "alias." and not val.startswith("!"):
            return None
        return f"banned inline config {key!r}"
    return None


def _check_dual_mode_subcommand(sub: str, rest: list[str]) -> tuple[bool, str]:
    """Evaluate a DUAL_MODE_ALLOWED subcommand's args against its read-only set."""
    if not rest:
        if sub == "stash":
            return False, "bare `git stash` defaults to push — banned"
        if sub in _NOARG_READONLY_SUBS:
            return True, f"{sub!r} with no args is read-only"
        return False, f"{sub!r} requires explicit read-only flag"
    first = rest[0]
    if first not in DUAL_MODE_ALLOWED[sub]:
        return False, f"{sub!r} with non-allowlisted first arg {first!r}"
    for tok in rest:
        if tok in _MUTATING_DUAL_FLAGS:
            return False, f"mutating flag {tok!r} after read-only {first!r}"
    return True, f"{sub!r} {first!r} is read-only"


def is_invocation_allowed(invocation: list[str]) -> tuple[bool, str]:
    """Evaluate a single git invocation against the allowlist + dual-mode gates."""
    if len(invocation) < 2:
        return False, "bare 'git' with no subcommand"
    sub = invocation[1]
    rest = invocation[2:]
    blocked, reason = _check_universal_banned(invocation)
    if blocked:
        return False, reason
    if sub in ALLOWED_NO_FLAG_CHECK:
        return True, f"read-only subcommand {sub!r}"
    if sub in DUAL_MODE_ALLOWED:
        return _check_dual_mode_subcommand(sub, rest)
    return False, f"subcommand {sub!r} is not in the read-only allowlist"


def check_bash(
    tool_input: dict[str, object], session_name: str | None,
) -> tuple[bool, str]:
    """Apply the Bash gate. Return ``(block, reason)``."""
    controller = git_controller_name()
    if controller is None or session_name == controller:
        return False, ""
    raw_command = tool_input.get("command", "")
    command = raw_command if isinstance(raw_command, str) else ""
    if _command_targets_dot_git(command):
        return True, "command appears to mutate `.git/` directly"
    invocations, _ = walk_git_invocations(command)
    for inv in invocations:
        allowed, reason = is_invocation_allowed(inv)
        if not allowed:
            joined = " ".join(inv[:5])
            return True, f"banned git invocation `{joined}...`: {reason}"
    return False, ""


_FS_MUTATING_VERBS = frozenset({
    "rm", "mv", "cp", "ln", "dd", "install", "chmod", "chown",
    "mkdir", "rmdir", "touch", "tee", "truncate", "shred", "unlink",
})
_SHELL_REDIRECT_TOKENS = frozenset({">", ">>", ">|"})


def _command_targets_dot_git(command: str) -> bool:
    """True if the command contains an fs-mutator or redirect targeting `.git/`.

    Mistake-prevention check only — catches `rm .git/index`,
    `echo x > .git/HEAD`, etc. Not a structural defense against derived paths.
    """
    try:
        from _git_controller_lex import punctuation_tokenize
        tokens = punctuation_tokenize(command)
    except (ValueError, ImportError):
        return False
    for i, tok in enumerate(tokens):
        if tok in _FS_MUTATING_VERBS or tok in _SHELL_REDIRECT_TOKENS:
            for j in range(i + 1, len(tokens)):
                operand = tokens[j]
                if ".git/" in operand or operand == ".git":
                    return True
    return False


def _collect_tool_input_paths(tool_input: dict[str, object]) -> list[str]:
    """Extract every file_path / notebook_path / edits[*].file_path field."""
    paths: list[str] = []
    for key in ("file_path", "notebook_path", "path", "target"):
        val = tool_input.get(key)
        if isinstance(val, str):
            paths.append(val)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if isinstance(entry, dict):
                fp = entry.get("file_path")
                if isinstance(fp, str):
                    paths.append(fp)
    return paths


def _path_under_git_dir(raw: str, git_dir: Path) -> bool:
    """True if ``raw`` resolves to a path under ``git_dir``."""
    try:
        resolved = Path(raw).resolve()
    except OSError:
        return False
    try:
        resolved.relative_to(git_dir)
    except ValueError:
        return False
    return True


def check_file_path(
    tool_input: dict[str, object], session_name: str | None,
) -> tuple[bool, str]:
    """Block Edit/Write/MultiEdit/NotebookEdit into ``<repo_root>/.git/``."""
    controller = git_controller_name()
    if controller is None or session_name == controller:
        return False, ""
    paths = _collect_tool_input_paths(tool_input)
    if not paths:
        return False, ""
    repo_root = os.environ.get("CLAUDE_PROJECT_DIR")
    if not repo_root:
        return False, ""
    try:
        git_dir = (Path(repo_root) / ".git").resolve()
    except OSError:
        return False, ""
    for raw in paths:
        if _path_under_git_dir(raw, git_dir):
            return True, f"writes to {str(git_dir)!r} are forbidden (path: {raw!r})"
    return False, ""


def check_task(
    tool_input: dict[str, object], session_name: str | None,
) -> tuple[bool, str]:
    """Block the Task tool (Agent-tool subagent spawning) for non-controller sessions.

    Sub-agents extend the spawning session's identity into git territory.
    The structurally-read-only `Explore` subagent is the only exception.
    """
    controller = git_controller_name()
    if controller is None or session_name == controller:
        return False, ""
    subagent_type_raw = tool_input.get("subagent_type", "")
    subagent_type = (
        subagent_type_raw if isinstance(subagent_type_raw, str) else ""
    )
    if subagent_type == EXPLORE_SUBAGENT:
        return False, ""
    return True, (
        f"Spawning a sub-agent (Task tool, subagent_type={subagent_type!r}) "
        f"is forbidden for non-controller sessions. Sub-agents inherit "
        f"your session identity and could perform git operations on your "
        f"behalf, bypassing this policy."
    )


def _main_inner() -> tuple[bool, str, str]:
    """The hook's decision logic. Returns ``(block, reason, identity)``."""
    payload = json.loads(sys.stdin.read())
    if not isinstance(payload, dict):
        return False, "", "<unknown>"

    tool_name_raw = payload.get("tool_name", "")
    tool_name = tool_name_raw if isinstance(tool_name_raw, str) else ""
    tool_input_raw = payload.get("tool_input", {}) or {}
    tool_input: dict[str, object] = (
        tool_input_raw if isinstance(tool_input_raw, dict) else {}
    )

    session_name = find_session_name()
    identity = session_name or "<unknown>"

    if tool_name == "Bash":
        block, reason = check_bash(tool_input, session_name)
        return block, reason, identity

    if tool_name in {"Edit", "Write", "NotebookEdit", "MultiEdit"}:
        block, reason = check_file_path(tool_input, session_name)
        return block, reason, identity

    if tool_name == "Task":
        block, reason = check_task(tool_input, session_name)
        return block, reason, identity

    return False, "", identity


def main() -> int:
    """Entry point. Returns ``2`` to BLOCK, ``0`` to ALLOW.

    Per Anthropic Claude Code hook docs: any exit code other than 2 is
    non-blocking. We catch every unexpected exception and explicitly
    decide — currently allow-on-error (peer mistake-prevention scope:
    we don't break peer workflows on hook bugs).
    """
    try:
        block, reason, identity = _main_inner()
    except json.JSONDecodeError:
        return 0
    except Exception:  # noqa: BLE001 — peer-mistake-prevention scope
        return 0

    if block:
        try:
            print(
                f"[git-controller-gate] BLOCKED: session {identity!r} attempted "
                f"a restricted operation.\n"
                f"Reason: {reason}\n\n"
                f"{POLICY_MESSAGE}",
                file=sys.stderr,
            )
        except Exception:  # noqa: BLE001 — telemetry strictly best-effort
            pass
        return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
