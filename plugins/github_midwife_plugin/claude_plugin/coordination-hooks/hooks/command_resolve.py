"""Command resolve: non-executing Git discard classification.

Revived from the reviewed retired lexer/walker; one canonical implementation.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from command_lex import is_assignment
from command_types import _BOOLEAN_GLOBALS, _KEYWORDS, _VALUE_GLOBALS


def _path(value: str, cwd: Path, fallback: Path) -> tuple[Path, tuple[str, ...]]:
    if not value or any(marker in value for marker in ("$", "`", "__RECOVERY_SUBST_", "~")):
        return fallback, ("target_unresolved",)
    return (cwd / value).resolve(), ()


def _wrapper_options(tokens: list[str], index: int, wrapper: str, cwd: Path, fallback: Path) -> tuple[int, Path, tuple[str, ...]]:
    advisories: tuple[str, ...] = ()
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        takes_value = option in {"-u", "--user", "-g", "--group", "-C", "--chdir", "--unset", "-n", "--adjustment", "-a", "-d", "-I", "-L", "-P", "-s", "-k", "-o"}
        if wrapper == "env" and option.split("=", 1)[0] in {"-C", "--chdir"}:
            value = option.partition("=")[2] if "=" in option else tokens[index + 1] if index + 1 < len(tokens) else ""
            cwd, advisory = _path(value, cwd, fallback)
            advisories += advisory
        index += 2 if takes_value else 1
    return index, cwd, advisories


def command_prefix(tokens: list[str], cwd: Path, fallback: Path) -> tuple[list[str], Path, dict[str, str], tuple[str, ...]]:
    environment: dict[str, str] = {}
    advisories: tuple[str, ...] = ()
    index = 0
    wrappers = {"command", "exec", "env", "sudo", "time", "nice", "nohup", "timeout", "xargs"}
    while index < len(tokens):
        token = tokens[index]
        if is_assignment(token):
            name, _, value = token.partition("=")
            environment[name] = value
            index += 1
        elif token in _KEYWORDS:
            index += 1
        elif Path(token).name in wrappers:
            wrapper = Path(token).name
            index, cwd, advisory = _wrapper_options(tokens, index + 1, wrapper, cwd, fallback)
            advisories += advisory
            if wrapper == "timeout" and index < len(tokens):
                index += 1
        else:
            break
    return tokens[index:], cwd, environment, advisories


def _global_value(tokens: list[str], index: int) -> tuple[str, str, int]:
    token = tokens[index]
    option, equal, inline = token.partition("=")
    if token.startswith(("-C", "-c")) and len(token) > 2:
        return token[:2], token[2:], index + 1
    value = inline if equal else tokens[index + 1] if index + 1 < len(tokens) else ""
    return option, value, index + (1 if equal else 2)


def _finish_target(cwd: Path, fallback: Path, worktree: str, gitdir: str) -> tuple[Path, tuple[str, ...]]:
    if worktree:
        return _path(worktree, cwd, fallback)
    if gitdir:
        target, advisory = _path(gitdir, cwd, fallback)
        return (fallback if advisory else target.parent), advisory
    return cwd, ()


def global_target(tokens: list[str], cwd: Path, fallback: Path, environment: Mapping[str, str]) -> tuple[list[str], Path, dict[str, str], tuple[str, ...]]:
    index, config = 1, {}
    worktree, gitdir = environment.get("GIT_WORK_TREE", ""), environment.get("GIT_DIR", "")
    advisory: tuple[str, ...] = ()
    while index < len(tokens) and tokens[index].startswith("-"):
        if tokens[index] in _BOOLEAN_GLOBALS:
            index += 1
            continue
        option, value, next_index = _global_value(tokens, index)
        if option not in _VALUE_GLOBALS:
            break
        index = next_index
        if option == "-C":
            cwd, extra = _path(value, cwd, fallback)
            advisory += extra
        elif option == "--work-tree":
            worktree = value
        elif option == "--git-dir":
            gitdir = value
        elif option == "-c":
            name, _, setting = value.partition("=")
            config[name] = setting
    cwd, extra = _finish_target(cwd, fallback, worktree, gitdir)
    return tokens[index:], cwd, config, advisory + extra
