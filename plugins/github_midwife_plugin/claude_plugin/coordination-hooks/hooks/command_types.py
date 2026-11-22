"""Command types: non-executing Git discard classification.

Revived from the reviewed retired lexer/walker; one canonical implementation.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

Query = Callable[[Path, Sequence[str]], bytes]


MAX_RECURSION = 5


_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")", "\n"})


_KEYWORDS = frozenset({"{", "}", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!"})


_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "ksh", "eval"})


_CONSUMERS = frozenset({"[", ":", "awk", "basename", "cat", "cut", "dirname", "echo", "false", "file", "grep", "head", "jq", "ls", "printf", "read", "realpath", "rg", "sed", "sort", "stat", "tail", "test", "tr", "true", "uniq", "wc"})


_VALUE_GLOBALS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--exec-path", "--html-path", "--man-path", "--info-path", "--namespace", "--super-prefix", "--config-env"})


_BOOLEAN_GLOBALS = frozenset({"-p", "--paginate", "--no-pager", "--no-replace-objects", "--bare", "--literal-pathspecs", "--no-optional-locks", "--no-advice"})


_BUILTINS = frozenset(
    {
        "add",
        "am",
        "apply",
        "archive",
        "bisect",
        "blame",
        "branch",
        "bundle",
        "cat-file",
        "check-ignore",
        "checkout",
        "checkout-index",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "describe",
        "diff",
        "fetch",
        "for-each-ref",
        "gc",
        "grep",
        "help",
        "init",
        "lfs",
        "log",
        "ls-files",
        "ls-tree",
        "merge",
        "merge-base",
        "mv",
        "notes",
        "pull",
        "push",
        "read-tree",
        "rebase",
        "reflog",
        "remote",
        "reset",
        "restore",
        "revert",
        "rev-list",
        "rev-parse",
        "rm",
        "show",
        "stash",
        "status",
        "submodule",
        "switch",
        "tag",
        "update-index",
        "worktree",
    }
)


_SUBSTITUTION = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")


_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z_][A-Za-z0-9_]*))")


@dataclass(frozen=True, slots=True)
class Discard:
    """One requested capture; handle coalesces these by physical folder."""

    folder: Path
    kinds: tuple[str, ...]
    arguments: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    stashes: tuple[str, ...] = ()
    branches: tuple[str, ...] = ()
    advisories: tuple[str, ...] = ()
    stash_push: bool = False
