"""Minimal lex helpers for the Git-Controller gate.

Sibling module to ``git_controller_gate.py``. Scope: trusted Claude peer
mistake-prevention, NOT adversarial obfuscation defense. Only the structural
pieces needed to recognize a direct ``git <subcommand>`` invocation, including
when wrapped in ``bash -c`` / ``sh -c`` / ``eval`` or ``$(...)`` substitution.

Stdlib-only.
"""

from __future__ import annotations

import re
import shlex

_SUBST_PLACEHOLDER_PREFIX = "__GCG_SUBST_"
_CMD_SUBST_RE = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

CHAIN_SEPARATORS = frozenset({";", "&&", "||", "|", "&", "(", ")"})


def punctuation_tokenize(command: str) -> list[str]:
    """Punctuation-aware shlex tokenization.

    Treats ``&&``, ``||``, ``;``, ``|``, ``&``, ``(``, ``)`` as token
    boundaries so the walker can detect compound commands like
    ``git status && git push``.
    """
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    return list(lex)


def extract_subst_pieces(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace ``$(...)`` and ``` `...` ``` with placeholders.

    Returns ``(outer_command, [(placeholder, inner_piece), ...])``. The
    walker recurses on each inner piece so ``echo $(git push)`` catches
    the inner git invocation.
    """
    pieces: list[tuple[str, str]] = []

    def repl(match: re.Match[str]) -> str:
        inner = match.group(1) or match.group(2) or ""
        placeholder = f"{_SUBST_PLACEHOLDER_PREFIX}{len(pieces)}__"
        pieces.append((placeholder, inner))
        return placeholder

    outer = _CMD_SUBST_RE.sub(repl, command)
    return outer, pieces
