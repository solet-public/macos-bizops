"""Discard forms: non-executing Git discard classification.

Revived from the reviewed retired lexer/walker; one canonical implementation.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from command_resolve import _path
from command_types import Discard, Query


def _short(arguments: Sequence[str]) -> str:
    return "".join(token[1:] for token in arguments if token.startswith("-") and not token.startswith("--"))


def _operands(arguments: Sequence[str]) -> tuple[str, ...]:
    if "--" in arguments:
        return tuple(arguments[arguments.index("--") + 1 :])
    return tuple(token for token in arguments if not token.startswith("-"))


def _stash_ref(folder: Path, arguments: list[str], verb: str) -> Discard:
    candidates = _operands(arguments[2:] if verb == "branch" else arguments[1:])
    reference = candidates[0] if candidates else "stash@{0}"
    number = reference[7:-1] if reference.startswith("stash@{") else reference
    kinds = (() if verb == "drop" else ("tree",)) + (f"stash({number})",)
    return Discard(folder, kinds, stashes=(reference,))


def _stash(folder: Path, arguments: list[str], _query: Query) -> Discard | None:
    verb = arguments[0] if arguments and not arguments[0].startswith("-") else "push"
    if verb in {"list", "show", "create", "store", "export", "import"}:
        return None
    if verb == "clear":
        return Discard(folder, ("stash(all)",), stashes=("all",))
    if verb in {"drop", "pop", "apply", "branch"}:
        return _stash_ref(folder, arguments, verb)
    untracked = bool(set("ua").intersection(_short(arguments)) or {"--include-untracked", "--all"}.intersection(arguments))
    paths = _operands(arguments[arguments.index("--") :]) if "--" in arguments else ()
    return Discard(folder, ("tree", "u") if untracked else ("tree",), paths=paths, stash_push=True)


def _checkout(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    letters = _short(arguments)
    if {"--force", "--patch"}.intersection(arguments) or set("fp").intersection(letters) or "--" in arguments:
        return Discard(folder, ("tree",), paths=_operands(arguments))
    if set("bB").intersection(letters) or {"--orphan", "--detach"}.intersection(arguments):
        return None
    words = _operands(arguments)
    if not words:
        return None
    if len(words) == 1 and words[0] != ".":
        if query(folder, ["rev-parse", "--verify", "-q", f"{words[0]}^{{commit}}"]):
            return None
    return Discard(folder, ("tree",), paths=words)


def _clean(folder: Path, arguments: list[str], _query: Query) -> Discard | None:
    letters = _short(arguments)
    if "n" in letters or "--dry-run" in arguments:
        return None
    kinds = ("tree", "u") if "i" in letters or "--interactive" in arguments else ("victims",)
    return Discard(folder, kinds, arguments=tuple(arguments))


def _branch(folder: Path, arguments: list[str], _query: Query) -> Discard | None:
    letters, options = _short(arguments), set(arguments)
    force = "f" in letters or "--force" in options
    delete = "d" in letters or "--delete" in options
    if "D" not in letters and not (force and delete):
        return None
    names = _operands(arguments)
    return Discard(folder, tuple(f"branch({name})" for name in names), branches=names) if names else None


def _worktree(folder: Path, arguments: list[str], _query: Query) -> Discard | None:
    force = "f" in _short(arguments) or "--force" in arguments
    if arguments[:1] != ["remove"] or not force:
        return None
    names = _operands(arguments[1:])
    target, advisory = _path(names[-1] if names else "", folder, folder)
    return Discard(target, ("tree",), advisories=advisory)


def _tree_capture(folder: Path, arguments: list[str], _query: Query) -> Discard:
    return Discard(folder, ("tree",), paths=_operands(arguments))


def _reset(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    return None if "--soft" in arguments else _tree_capture(folder, arguments, query)


def _abort(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    return _tree_capture(folder, arguments, query) if "--abort" in arguments else None


def _switch(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    force = "f" in _short(arguments) or bool({"--force", "--discard-changes"}.intersection(arguments))
    return _tree_capture(folder, arguments, query) if force else None


def _pull(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    rebase = "r" in _short(arguments) or any(token.startswith("--rebase") or token == "--autostash" for token in arguments)
    return _tree_capture(folder, arguments, query) if rebase else None


def _rm(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    return None if "--cached" in arguments else _tree_capture(folder, arguments, query)


def _checkout_index(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    return _tree_capture(folder, arguments, query) if "f" in _short(arguments) or "--force" in arguments else None


def _read_tree(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    letters = _short(arguments)
    update = "u" in letters and ("--reset" in arguments or "m" in letters)
    return _tree_capture(folder, arguments, query) if update else None


def _submodule(folder: Path, arguments: list[str], query: Query) -> Discard | None:
    force = "f" in _short(arguments) or "--force" in arguments
    return _tree_capture(folder, arguments, query) if force and arguments[:1] in (["update"], ["deinit"]) else None


_FORMS = {
    "stash": _stash,
    "checkout": _checkout,
    "clean": _clean,
    "branch": _branch,
    "worktree": _worktree,
    "restore": _tree_capture,
    "rebase": _tree_capture,
    "reset": _reset,
    "merge": _abort,
    "cherry-pick": _abort,
    "revert": _abort,
    "am": _abort,
    "switch": _switch,
    "pull": _pull,
    "rm": _rm,
    "checkout-index": _checkout_index,
    "read-tree": _read_tree,
    "submodule": _submodule,
}


def classify_form(folder: Path, tokens: list[str], query: Query) -> Discard | None:
    if not tokens or {"-h", "--help"}.intersection(tokens):
        return None
    handler = _FORMS.get(tokens[0])
    return handler(folder, tokens[1:], query) if handler else None
