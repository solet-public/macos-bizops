"""Exact runtime values of statically composed Python string literals."""

from __future__ import annotations

import ast
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ResolvedPythonString:
    """One complete, statically known Python string expression."""

    value: str
    line_number: int
    column: int
    source: str


@dataclass(frozen=True, slots=True)
class ResolvedPythonPatternMatch:
    """One regex match introduced only by resolving a static expression."""

    literal: ResolvedPythonString
    token: str


def _joined_string_value(node: ast.JoinedStr) -> str | None:
    parts: list[str] = []
    for part in node.values:
        if isinstance(part, ast.Constant) and isinstance(part.value, str):
            parts.append(part.value)
            continue
        if (
            isinstance(part, ast.FormattedValue)
            and part.conversion == -1
            and part.format_spec is None
            and (value := literal_string_value(part.value)) is not None
        ):
            parts.append(value)
            continue
        return None
    return "".join(parts)


def literal_string_value(node: ast.AST) -> str | None:
    """Return an exact string value, or ``None`` for any dynamic expression."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = literal_string_value(node.left)
        right = literal_string_value(node.right)
        return left + right if left is not None and right is not None else None
    return _joined_string_value(node) if isinstance(node, ast.JoinedStr) else None


def _source_segment(lines: list[str], node: ast.expr) -> str:
    start_line = node.lineno - 1
    end_line = (node.end_lineno or node.lineno) - 1
    start_column = node.col_offset
    end_column = node.end_col_offset or start_column
    if start_line == end_line:
        return lines[start_line].encode()[start_column:end_column].decode()
    segments = [lines[start_line].encode()[start_column:].decode()]
    segments.extend(lines[start_line + 1 : end_line])
    segments.append(lines[end_line].encode()[:end_column].decode())
    return "\n".join(segments)


class _LiteralVisitor(ast.NodeVisitor):
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.resolved: list[ResolvedPythonString] = []

    def _record(self, node: ast.expr, value: str) -> None:
        line = self._lines[node.lineno - 1]
        column = len(line.encode()[: node.col_offset].decode())
        self.resolved.append(
            ResolvedPythonString(
                value=value,
                line_number=node.lineno,
                column=column,
                source=_source_segment(self._lines, node),
            )
        )

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if not isinstance(node.op, ast.Add):
            self.generic_visit(node)
            return
        value = literal_string_value(node)
        if value is not None:
            self._record(node, value)
        # A dynamic outer concatenation is not an exact literal value. Do not
        # promote a statically foldable child fragment into the whole value.

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        value = literal_string_value(node)
        if value is not None:
            self._record(node, value)
        # As above, literal fragments of a dynamic f-string are not complete
        # values and must not be scanned independently.

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            self._record(node, node.value)


def resolved_python_strings(source: str) -> tuple[ResolvedPythonString, ...]:
    """Collect complete static string expressions from parseable Python."""

    try:
        tree = ast.parse(source)
    except (IndentationError, SyntaxError, ValueError):
        return ()
    visitor = _LiteralVisitor(source.splitlines())
    visitor.visit(tree)
    return tuple(visitor.resolved)


def newly_resolved_pattern_matches(
    source: str, pattern: re.Pattern[str]
) -> tuple[ResolvedPythonPatternMatch, ...]:
    """Return pattern hits absent from each expression's raw source bytes."""

    resolved: list[ResolvedPythonPatternMatch] = []
    for literal in resolved_python_strings(source):
        visible = Counter(match.group(0) for match in pattern.finditer(literal.source))
        for match in pattern.finditer(literal.value):
            token = match.group(0)
            if visible[token]:
                visible[token] -= 1
                continue
            resolved.append(ResolvedPythonPatternMatch(literal=literal, token=token))
    return tuple(resolved)


__all__ = [
    "ResolvedPythonPatternMatch",
    "ResolvedPythonString",
    "literal_string_value",
    "newly_resolved_pattern_matches",
    "resolved_python_strings",
]
