"""Command walk: non-executing Git discard classification.

Revived from the reviewed retired lexer/walker; one canonical implementation.
"""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

from command_lex import split_heredocs, tokenize
from command_resolve import _path, command_prefix, global_target
from command_types import _BUILTINS, _CONSUMERS, _SEPARATORS, _SHELLS, _SUBSTITUTION, MAX_RECURSION, Discard, Query
from discard_forms import classify_form


def _empty_query(_folder: Path, _arguments: Sequence[str]) -> bytes:
    return b""


def _git(tokens: list[str], cwd: Path, fallback: Path, environment: Mapping[str, str], query: Query, depth: int) -> list[Discard]:
    tokens, target, inline, advisory = global_target(tokens, cwd, fallback, environment)
    if not tokens:
        return []
    if tokens[0] not in _BUILTINS and depth < 3:
        aliases = dict(line.split(" ", 1) for line in os.fsdecode(query(target, ["config", "--get-regexp", "^alias\\."])).splitlines() if " " in line)
        aliases.update(inline)
        alias = aliases.get(f"alias.{tokens[0]}")
        if alias is not None:
            command = alias[1:] if alias.startswith("!") else "git " + alias + " " + shlex.join(tokens[1:])
            return _walk(command, target, fallback, query, depth + 1)
    capture = classify_form(target, tokens, query)
    return [replace(capture, advisories=capture.advisories + advisory)] if capture else []


def _shell(tokens: list[str]) -> str | None:
    if tokens[0] == "eval":
        return " ".join(tokens[1:])
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        option = tokens[index]
        if option == "--":
            return None
        if not option.startswith("--") and "c" in option[1:]:
            return tokens[index + 1] if index + 1 < len(tokens) else ""
        index += 2 if option in {"-O", "-o", "-T", "-D"} else 1
    return None


def _segments(tokens: list[str]) -> list[tuple[list[str], str]]:
    result: list[tuple[list[str], str]] = []
    start = 0
    for index, token in enumerate(tokens):
        if token in _SEPARATORS:
            if index > start:
                result.append((tokens[start:index], ""))
            result.append(([], token))
            start = index + 1
    if start < len(tokens):
        result.append((tokens[start:], ""))
    return result


def _execute(segment: list[str], target: Path, fallback: Path, environment: Mapping[str, str], query: Query, depth: int) -> list[Discard]:
    executable = Path(segment[0]).name
    if executable == "git":
        return _git(segment, target, fallback, environment, query, depth)
    if executable in _SHELLS:
        source = _shell(segment)
        return _walk(source, target, fallback, query, depth + 1) if source is not None else []
    if executable not in _CONSUMERS:
        for offset, token in enumerate(segment[1:], 1):
            if Path(token).name == "git":
                return _git(segment[offset:], target, fallback, environment, query, depth)
    return []


def _walk_tokens(tokens: list[str], cwd: Path, fallback: Path, query: Query, depth: int) -> list[Discard]:
    result: list[Discard] = []
    stack: list[Path] = []
    advisory: tuple[str, ...] = ()
    for words, boundary in _segments(tokens):
        if boundary == "(":
            stack.append(cwd)
        elif boundary == ")" and stack:
            cwd = stack.pop()
        if not words:
            continue
        segment, target, environment, extra = command_prefix(words, cwd, fallback)
        if not segment:
            continue
        if Path(segment[0]).name in {"cd", "pushd"}:
            cwd, advisory = _path(segment[1] if len(segment) > 1 else "", target, fallback)
        else:
            result.extend(replace(entry, advisories=entry.advisories + advisory + extra) for entry in _execute(segment, target, fallback, environment, query, depth))
    return result


def _walk(command: str, cwd: Path, fallback: Path, query: Query, depth: int) -> list[Discard]:
    if depth > MAX_RECURSION:
        return [Discard(fallback, ("tree",), advisories=("command_unresolved",))] if "git" in command else []
    retained, scripts = split_heredocs(command)
    substitutions: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        substitutions.append(match.group(1) or match.group(2) or "")
        return f"__RECOVERY_SUBST_{len(substitutions)}__"

    retained = _SUBSTITUTION.sub(substitute, retained)
    try:
        tokens = tokenize(retained)
    except ValueError:
        return [Discard(fallback, ("tree",), advisories=("command_unresolved",))] if "git" in retained else []
    nested = [entry for source in (*scripts, *substitutions) for entry in _walk(source, cwd, fallback, query, depth + 1)]
    return nested + _walk_tokens(tokens, cwd, fallback, query, depth)


def classify(command: str, cwd: Path, *, query: Query = _empty_query) -> tuple[Discard, ...]:
    """Classify a command without running it; query performs only Git reads."""
    return tuple(_walk(command, cwd.resolve(), cwd.resolve(), query, 0))
