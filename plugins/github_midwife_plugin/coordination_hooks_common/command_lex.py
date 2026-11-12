"""Command lex: non-executing Git discard classification.

Revived from the reviewed retired lexer/walker; one canonical implementation.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from command_types import _HEREDOC, _SHELLS


def tokenize(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars="();&|<>")
    lexer.whitespace_split = True
    # shlex otherwise consumes newlines as whitespace, hiding command starts.
    lexer.whitespace = " \t\r"
    return list(lexer)


def _heredoc_body(lines: list[str], index: int, delimiter: str, strip_tabs: bool) -> tuple[list[str], int, bool]:
    body: list[str] = []
    while index < len(lines):
        line = lines[index]
        index += 1
        probe = line.lstrip("\t") if strip_tabs else line
        if probe.rstrip() == delimiter:
            return body, index, True
        body.append(line)
    return body, index, False


def _is_evaluator(owner: str) -> bool:
    try:
        return any(Path(token).name in _SHELLS for token in tokenize(owner))
    except ValueError:
        return True


def split_heredocs(command: str) -> tuple[str, list[str]]:
    lines, kept, scripts = command.split("\n"), [], []
    index = 0
    while index < len(lines):
        owner = lines[index]
        kept.append(owner)
        index += 1
        for match in _HEREDOC.finditer(owner):
            delimiter = match.group(2) or match.group(3) or match.group(4)
            body, index, complete = _heredoc_body(lines, index, delimiter, bool(match.group(1)))
            if not complete:
                kept.extend(body)
            elif _is_evaluator(owner):
                scripts.append("\n".join(body))
    return "\n".join(kept), scripts


def is_assignment(token: str) -> bool:
    name, equal, _ = token.partition("=")
    return bool(equal) and name.isidentifier()
