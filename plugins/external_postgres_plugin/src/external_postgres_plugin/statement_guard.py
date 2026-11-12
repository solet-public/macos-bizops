"""Statement SHAPE helpers — never an access-control decision.

What a statement may do is decided by the registered credential's own
server-side Postgres GRANTs (USER ruling rul_4c99a30d; operator ruling
2026-08-09 + Amendment 1). Nothing here inspects whether a statement reads or
writes. These helpers only split and count statements, with a REAL SQL parser
(``sqlparse``, BSD; operator-ratified rev-F-final over pglast/GPL) that is
dollar-quote / string / comment aware:

- :func:`assert_single_statement` — the one-statement contract of
  ``run_query``/``export_query``/``run_statement`` (predictable result and
  commit semantics; ``run_script`` is the multi-statement verb).
- :func:`split_statements` — ``run_script``'s splitter: one entry per
  top-level statement, comment/whitespace-only fragments dropped.
- :func:`leading_keyword` — the upper-cased leading keyword, used to name a
  statement in a result or an error, and to find transaction-control
  statements a one-transaction script cannot contain.

This module contains NO SQL string literals and imports NO database driver, so
it stays FULLY GATED (no allowlist entry).
"""

from __future__ import annotations

import re

import sqlparse

from .constants import ERROR_INVALID_PARAMS


class StatementGuardError(RuntimeError):
    """Raised when a statement's SHAPE does not fit the verb's contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def split_statements(sql: str) -> list[str]:
    """Split a script into its top-level statements, dropping empty fragments.

    A ``;`` inside a string, a dollar-quoted body (a function definition) or a
    comment does NOT split. Each returned statement keeps its own text with
    the trailing ``;`` stripped.
    """
    statements: list[str] = []
    for stmt in sqlparse.parse(sql):
        if stmt.token_first(skip_cm=True) is None:
            continue
        text = str(stmt).strip()
        statements.append(text[:-1].rstrip() if text.endswith(";") else text)
    return statements


def assert_single_statement(sql: str) -> None:
    """Refuse anything other than exactly one non-empty top-level statement."""
    count = len(split_statements(sql))
    if count == 0:
        raise StatementGuardError(ERROR_INVALID_PARAMS, "no SQL statement was provided")
    if count > 1:
        raise StatementGuardError(
            ERROR_INVALID_PARAMS,
            "only a single SQL statement is permitted per call; use run_script to run several statements in one transaction",
        )


def leading_keyword(sql: str) -> str:
    """The upper-cased leading SQL keyword, tolerating a leading parenthesis.

    Comments and leading whitespace are skipped (``skip_cm=True``) so the leader
    reflects the real statement, not a comment. A parenthesized leader
    (``(SELECT …)`` / ``(VALUES …)``) is unwrapped.
    """
    parsed = sqlparse.parse(sql)
    if not parsed:
        return ""
    token = parsed[0].token_first(skip_cm=True)
    if token is None:
        return ""
    # Tolerate a leading parenthesis ("(SELECT …)") and a no-space row
    # constructor ("VALUES(1)") — take the leading run of word characters.
    head = token.value.lstrip("(").strip()
    match = re.match(r"[A-Za-z_]+", head)
    return match.group(0).upper() if match else ""
