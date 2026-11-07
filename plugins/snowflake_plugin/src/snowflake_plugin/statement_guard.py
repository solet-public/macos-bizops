"""Validate SQL input before forwarding it to Snowflake.

Snowflake's vendor role is the access boundary. This module only rejects
empty or comment-led input, then lets the existing query runner forward any
statement shape to the registered account. Snowflake's own driver handles its
single-statement contract.

Single-statement is NATIVE (Snowflake's ``MULTI_STATEMENT_COUNT`` defaults to
1, and this plugin never calls ``execute_string``) — no statement-splitting
parser is needed here.

This module contains NO SQL string literals and imports NO database driver,
so it stays FULLY GATED (no allowlist entry) — the same honesty condition as
external_postgres_plugin's statement_guard.py.
"""

from __future__ import annotations

import re

from .constants import ERROR_INVALID_PARAMS


class StatementGuardError(RuntimeError):
    """Raised when SQL input is malformed before it reaches Snowflake."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def assert_read_statement(sql: str) -> None:
    """Validate SQL input, preserving the existing internal function name.

    Comment-led input is refused outright (fail loud, no comment-stripping
    parser). Non-empty statements are passed through; vendor RBAC decides
    whether Snowflake authorizes the operation.
    """
    stripped = sql.lstrip()
    if stripped.startswith("--") or stripped.startswith("/*"):
        raise StatementGuardError(ERROR_INVALID_PARAMS, "strip comments before submitting sql")
    if not stripped:
        raise StatementGuardError(ERROR_INVALID_PARAMS, "no SQL statement was provided")


def leading_keyword(sql: str) -> str:
    """The upper-cased leading SQL keyword, tolerating a leading parenthesis."""
    head = sql.lstrip().lstrip("(").strip()
    match = re.match(r"[A-Za-z_]+", head)
    return match.group(0).upper() if match else ""
