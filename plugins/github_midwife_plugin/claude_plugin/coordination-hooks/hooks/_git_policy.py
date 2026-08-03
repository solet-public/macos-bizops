"""Runner-neutral Git-Controller mistake-prevention policy.

This module owns the policy shared by the Claude Code and Codex hook adapters.
It does not read runner identity or project environment variables: callers
resolve those bindings and pass them in explicitly.  The module is stdlib-only
because hook handlers run outside the platform virtualenv.

This is not an adversarial security boundary.  It prevents trusted peer
sessions from accidentally mutating git state or bypassing the designated
controller workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# ruff: noqa: I001, E402
# pyright: reportMissingImports=false
from _git_controller_walker import walk_git_invocations


EXPLORE_SUBAGENT = "Explore"

# Claude Code currently routes these names.  A runner adapter may expose only
# the subset whose live wire shape it has measured.
SUBAGENT_TOOL_NAMES = frozenset({"Task", "Agent"})
FILE_PATH_TOOL_NAMES = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
BASH_TOOL_NAME = "Bash"
GATED_TOOL_NAMES = (
    frozenset({BASH_TOOL_NAME}) | FILE_PATH_TOOL_NAMES | SUBAGENT_TOOL_NAMES
)
# The single-active-session exemption, ruled 2026-08-01 (D-5a.3). That ruling
# is TWO TIERS, and this constant is only the second: a solo deployment's
# hydration never sets the arming variable, so the gate never arms and its
# configuration IS the exemption (tier 1). This text serves the transiently
# solo FLEET only. It deliberately names NO mechanism the gate has — the gate
# detects nothing new here, and a claim that outran its mechanism inside a
# control designed to prevent exactly that is the one failure this cannot
# carry. Held as its own constant so the gate's copies can be asserted
# byte-equal on the clause SECTION, while the per-copy parameterized parts of
# the surrounding message stay free to differ.
EXEMPTION_CLAUSE = (
    "If only one session is active in this deployment, this policy does not "
    "apply. A session relying on that exemption must have a checkable basis "
    "for it (a peer list it has just run showing no other live session, or "
    "an explicit operator statement) and must cite that basis in band "
    "wherever the mutation is recorded. An operator instruction to proceed "
    "overrides this policy."
)

# Operator-ruled wording, 2026-08-01 (A5). Verbatim but for two typo
# normalizations, disclosed in the WS-3b operator decision sheet — do not
# re-normalize further.
POLICY_MESSAGE = (
    "Issuing git commands in a multi-session environment can lead to data "
    "loss. Policy is to delegate all repository-impacting git commands to a "
    "designated 'git controller' session. " + EXEMPTION_CLAUSE
)

ALLOWED_NO_FLAG_CHECK = frozenset({
    "status", "log", "diff", "show", "blame", "shortlog", "describe",
    "name-rev", "for-each-ref", "reflog", "rev-parse", "rev-list",
    "merge-base", "ls-files", "ls-tree", "ls-remote", "cat-file",
    "check-ignore", "verify-commit", "verify-tag",
    "var", "help", "version", "--version",
})

DUAL_MODE_ALLOWED: dict[str, frozenset[str]] = {
    "branch": frozenset({
        "-v", "-l", "-r", "--show-current", "--list", "--contains",
        "--no-contains", "-a", "--all",
    }),
    "tag": frozenset({"-l", "-n", "--list", "--contains", "--no-contains"}),
    "stash": frozenset({"list", "show"}),
    "remote": frozenset({"-v", "show"}),
    "submodule": frozenset({"status", "summary"}),
    "worktree": frozenset({"list"}),
    "bisect": frozenset({"log", "view"}),
    "config": frozenset({"--get", "--list", "--get-all", "--get-regexp"}),
}

UNIVERSAL_BANNED_FLAGS = frozenset({"--no-verify"})
DANGEROUS_C_KEY_PREFIXES: tuple[str, ...] = (
    "commit.gpgsign", "core.hooksPath", "gc.auto", "alias.",
)
DANGEROUS_GIT_GLOBALS = frozenset({"--git-dir", "--work-tree", "-C"})

_MUTATING_DUAL_FLAGS = frozenset({"-d", "-D", "-m", "-M", "--delete", "--force"})
_NOARG_READONLY_SUBS = frozenset(
    {"branch", "tag", "remote", "submodule", "worktree", "bisect"},
)
_BOOLEAN_GIT_GLOBALS = frozenset({
    "-p", "--paginate", "--no-pager", "--no-replace-objects", "--bare",
    "--literal-pathspecs", "--no-optional-locks", "--no-advice",
})
_VALUE_GIT_GLOBALS = frozenset({
    "-c", "--exec-path", "--html-path", "--man-path", "--info-path",
    "--namespace", "--super-prefix", "--config-env",
})
_FS_MUTATING_VERBS = frozenset({
    "rm", "mv", "cp", "ln", "dd", "install", "chmod", "chown",
    "mkdir", "rmdir", "touch", "tee", "truncate", "shred", "unlink",
})
_SHELL_REDIRECT_TOKENS = frozenset({">", ">>", ">|"})


def _check_universal_banned(invocation: list[str]) -> tuple[bool, str]:
    """Universal flag bans, dangerous globals, and inline config keys."""
    rest = invocation[2:]
    for flag in UNIVERSAL_BANNED_FLAGS:
        if flag in rest:
            return True, f"banned flag {flag!r}"
    for global_opt in DANGEROUS_GIT_GLOBALS:
        if global_opt in invocation:
            return True, f"banned global git option {global_opt!r}"
    for index in range(len(invocation) - 1):
        if invocation[index] != "-c" or "=" not in invocation[index + 1]:
            continue
        key, _, value = invocation[index + 1].partition("=")
        blocked = _key_blocked(key, value)
        if blocked is not None:
            return True, blocked
    return False, ""


def _key_blocked(key: str, value: str) -> str | None:
    """Return a block reason when a ``-c key=value`` pair is dangerous."""
    for prefix in DANGEROUS_C_KEY_PREFIXES:
        if not key.startswith(prefix):
            continue
        if prefix == "alias." and not value.startswith("!"):
            return None
        return f"banned inline config {key!r}"
    return None


def _check_dual_mode_subcommand(subcommand: str, rest: list[str]) -> tuple[bool, str]:
    """Evaluate a dual-mode subcommand against its explicit read-only set."""
    if not rest:
        if subcommand == "stash":
            return False, "bare `git stash` defaults to push — banned"
        if subcommand in _NOARG_READONLY_SUBS:
            return True, f"{subcommand!r} with no args is read-only"
        return False, f"{subcommand!r} requires explicit read-only flag"
    first = rest[0]
    if first not in DUAL_MODE_ALLOWED[subcommand]:
        return False, f"{subcommand!r} with non-allowlisted first arg {first!r}"
    for token in rest:
        if token in _MUTATING_DUAL_FLAGS:
            return False, f"mutating flag {token!r} after read-only {first!r}"
    return True, f"{subcommand!r} {first!r} is read-only"


def _find_subcommand_index(invocation: list[str]) -> int | None:
    """Return the real subcommand index after recognized git global options."""
    index = 1
    while index < len(invocation):
        token = invocation[index]
        if not token.startswith("-"):
            return index
        if token in _BOOLEAN_GIT_GLOBALS:
            index += 1
            continue
        base = token.split("=", 1)[0]
        if base in _VALUE_GIT_GLOBALS:
            index += 1 if "=" in token else 2
            continue
        return index
    return None


def is_invocation_allowed(invocation: list[str]) -> tuple[bool, str]:
    """Evaluate one git invocation against the read-only allowlist."""
    if len(invocation) < 2:
        return False, "bare 'git' with no subcommand"
    blocked, reason = _check_universal_banned(invocation)
    if blocked:
        return False, reason
    subcommand_index = _find_subcommand_index(invocation)
    if subcommand_index is None:
        return False, "bare 'git' with no subcommand"
    subcommand = invocation[subcommand_index]
    rest = invocation[subcommand_index + 1:]
    if subcommand in ALLOWED_NO_FLAG_CHECK:
        return True, f"read-only subcommand {subcommand!r}"
    if subcommand in DUAL_MODE_ALLOWED:
        return _check_dual_mode_subcommand(subcommand, rest)
    return False, f"subcommand {subcommand!r} is not in the read-only allowlist"


def check_bash(
    tool_input: dict[str, object],
    session_role: str | None,
    controller_role: str | None,
) -> tuple[bool, str]:
    """Apply the Bash policy and return ``(block, reason)``."""
    if controller_role is None or session_role == controller_role:
        return False, ""
    raw_command = tool_input.get("command", "")
    command = raw_command if isinstance(raw_command, str) else ""
    if _command_targets_dot_git(command):
        return True, "command appears to mutate `.git/` directly"
    invocations, _ = walk_git_invocations(command)
    for invocation in invocations:
        allowed, reason = is_invocation_allowed(invocation)
        if not allowed:
            joined = " ".join(invocation[:5])
            return True, f"banned git invocation `{joined}...`: {reason}"
    return False, ""


def _command_targets_dot_git(command: str) -> bool:
    """Return true for an fs-mutator or redirect targeting ``.git/``."""
    try:
        from _git_controller_lex import CHAIN_SEPARATORS, punctuation_tokenize

        tokens = punctuation_tokenize(command)
    except (ValueError, ImportError):
        return False
    for index, token in enumerate(tokens):
        if token not in _FS_MUTATING_VERBS and token not in _SHELL_REDIRECT_TOKENS:
            continue
        for operand in tokens[index + 1:]:
            if operand in CHAIN_SEPARATORS:
                break
            if ".git/" in operand or operand == ".git":
                return True
    return False


def _collect_tool_input_paths(tool_input: dict[str, object]) -> list[str]:
    """Extract every supported path field from one tool payload."""
    paths: list[str] = []
    for key in ("file_path", "notebook_path", "path", "target"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for entry in edits:
            if not isinstance(entry, dict):
                continue
            file_path = entry.get("file_path")
            if isinstance(file_path, str):
                paths.append(file_path)
    return paths


def _path_under_git_dir(raw: str, git_dir: Path) -> bool:
    """Return true when ``raw`` resolves under ``git_dir``."""
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
    tool_input: dict[str, object],
    session_role: str | None,
    controller_role: str | None,
    repo_root: str | None,
) -> tuple[bool, str]:
    """Block a non-controller file tool from writing under ``.git/``."""
    if controller_role is None or session_role == controller_role:
        return False, ""
    paths = _collect_tool_input_paths(tool_input)
    if not paths or not repo_root:
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
    tool_input: dict[str, object],
    session_role: str | None,
    controller_role: str | None,
) -> tuple[bool, str]:
    """Block non-controller sub-agent spawning except read-only Explore."""
    if controller_role is None or session_role == controller_role:
        return False, ""
    raw_subagent_type = tool_input.get("subagent_type", "")
    subagent_type = raw_subagent_type if isinstance(raw_subagent_type, str) else ""
    if subagent_type == EXPLORE_SUBAGENT:
        return False, ""
    return True, (
        f"Spawning a sub-agent (Task/Agent tool, subagent_type={subagent_type!r}) "
        "is forbidden for non-controller sessions. Sub-agents inherit your "
        "session identity and could perform git operations on your behalf, "
        "bypassing this policy."
    )
