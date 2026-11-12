"""``run_script`` — several statements in ONE transaction, a savepoint per statement.

PostgreSQL transactions and savepoints (``SAVEPOINT`` / ``ROLLBACK TO
SAVEPOINT`` / ``RELEASE SAVEPOINT``) are the vendor shape. The script is split
by :func:`statement_guard.split_statements` (dollar-quote, string and comment
aware), then each statement runs inside its own savepoint on one ordinary
connection:

- ``on_error="rollback"`` (default): the first failing statement rolls the
  WHOLE transaction back and raises :class:`ScriptStatementError`
  (``external_pg.script_statement_failed``), whose message names the failing
  statement's number, its leading keyword and its text, plus the server's
  classified error.
- ``on_error="continue"``: a failing statement rolls back to its own savepoint
  only; the rest run, and the failures are reported in the result.
- ``commit=false``: run everything, then roll back — a dry run of a migration.

Transaction-control statements (``BEGIN``, ``COMMIT``, ``SAVEPOINT`` …) are
refused before anything runs: the verb owns the one transaction, so a script
that ended it would silently lose the savepoint contract. Commands the server
refuses inside a transaction block (``VACUUM``, ``CREATE DATABASE``) fail with
the server's SQLSTATE 25001 and belong in ``run_statement`` with
``autocommit=true``.

No rows are returned inline: a statement that produces a result set reports
its row count only (use ``run_query`` for rows). The savepoint commands are the
only SQL literals here; the statements are the caller's own text, sent to the
FOREIGN database (S2 vendor-prefix exemption, sql_access_gate.py).
"""

from __future__ import annotations

import re
from typing import Any

from . import connection
from .constants import ERROR_SCRIPT_STATEMENT_FAILED
from .statement_guard import leading_keyword, split_statements

ON_ERROR_ROLLBACK = "rollback"
ON_ERROR_CONTINUE = "continue"
_ON_ERROR_CHOICES = (ON_ERROR_ROLLBACK, ON_ERROR_CONTINUE)
_TRANSACTION_CONTROL = frozenset({"BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE"})
# PREPARE TRANSACTION ends the transaction; a plain PREPARE (a prepared
# statement) does not, so only the two-word form counts as control.
_PREPARE_TRANSACTION = re.compile(r"^\s*PREPARE\s+TRANSACTION\b", re.IGNORECASE)
_SAVEPOINT = "SAVEPOINT epg_script_statement"
_RELEASE = "RELEASE SAVEPOINT epg_script_statement"
_ROLLBACK_TO = "ROLLBACK TO SAVEPOINT epg_script_statement"
_EXCERPT_CHARS = 200


class ScriptStatementError(RuntimeError):
    """One script statement failed and the whole transaction was rolled back."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.code = ERROR_SCRIPT_STATEMENT_FAILED


def run_script(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Run the script's statements in one transaction; see the module docstring."""
    statements, on_error, commit = _validated(params)
    results: list[dict[str, Any]] = []
    with conn.cursor() as cur:
        for index, statement in enumerate(statements, start=1):
            cur.execute(_SAVEPOINT)
            try:
                cur.execute(statement)
            except Exception as exc:
                if getattr(exc, "sqlstate", None) is None:
                    raise  # no SQLSTATE: the connection itself failed
                cur.execute(_ROLLBACK_TO)
                failure = _failure(index, len(statements), statement, exc)
                if on_error == ON_ERROR_ROLLBACK:
                    conn.rollback()
                    raise ScriptStatementError(f"{failure['message']}; the whole script was rolled back") from exc
                results.append(failure)
                continue
            results.append(
                {
                    "index": index,
                    "command": _leader(statement),
                    "status": "ok",
                    "command_tag": str(cur.statusmessage or ""),
                    "rowcount": cur.rowcount,
                    "has_result_set": cur.description is not None,
                }
            )
            cur.execute(_RELEASE)
    if commit:
        conn.commit()
    else:
        conn.rollback()
    failed = [entry for entry in results if entry["status"] == "failed"]
    return {
        "statement_count": len(statements),
        "succeeded_count": len(results) - len(failed),
        "failed_count": len(failed),
        "committed": commit,
        "on_error": on_error,
        "statements": results,
    }


def _validated(params: dict[str, Any]) -> tuple[list[str], str, bool]:
    sql = params.get("sql")
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("'sql' is required and must be a non-empty string")
    statements = split_statements(sql)
    if not statements:
        raise ValueError("'sql' contains no statement")
    on_error = params.get("on_error", ON_ERROR_ROLLBACK)
    if on_error not in _ON_ERROR_CHOICES:
        raise ValueError(f"'on_error' must be one of {list(_ON_ERROR_CHOICES)}")
    commit = params.get("commit", True)
    if not isinstance(commit, bool):
        raise ValueError("'commit' must be a boolean")
    for index, statement in enumerate(statements, start=1):
        if _leader(statement) in _TRANSACTION_CONTROL or _PREPARE_TRANSACTION.match(statement):
            raise ValueError(f"statement {index} ({_leader(statement)}) is transaction control; run_script already runs the whole script in one transaction with a savepoint per statement — remove it and use on_error/commit instead")
    return statements, str(on_error), commit


def _leader(statement: str) -> str:
    return leading_keyword(statement) or "?"


def _failure(index: int, total: int, statement: str, exc: Exception) -> dict[str, Any]:
    code, message = connection.classify_pg_error(exc)
    excerpt = " ".join(statement.split())
    if len(excerpt) > _EXCERPT_CHARS:
        excerpt = excerpt[:_EXCERPT_CHARS] + "…"
    sqlstate = str(getattr(exc, "sqlstate", ""))
    return {
        "index": index,
        "command": _leader(statement),
        "status": "failed",
        "error_code": code,
        "sqlstate": sqlstate,
        "message": (f"statement {index} of {total} ({_leader(statement)}: {excerpt}) failed [{code}, SQLSTATE {sqlstate}]: {message}"),
    }
