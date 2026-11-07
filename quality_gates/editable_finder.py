"""Re-home literal setuptools finder mappings without executing their source."""

from __future__ import annotations

import ast
import re
from pathlib import Path

_IMPORT = re.compile(r"import (?P<name>__editable___\w+_finder);\s*(?P=name)\.install\(\)")


def finder_name(pointer: str) -> str | None:
    """Recognize the generated finder import; arbitrary executable pth stays refused."""
    match = _IMPORT.fullmatch(pointer.strip())
    return None if match is None else match.group("name")


def _assigns_mapping(statement: ast.Assign | ast.AnnAssign) -> bool:
    targets = [statement.target] if isinstance(statement, ast.AnnAssign) else statement.targets
    return any(isinstance(target, ast.Name) and target.id == "MAPPING" for target in targets)


def _mapping_node(tree: ast.Module, entry: str) -> ast.Dict:
    values = [statement.value for statement in tree.body if isinstance(statement, (ast.Assign, ast.AnnAssign)) and _assigns_mapping(statement)]
    if len(values) != 1 or not isinstance(values[0], ast.Dict) or not values[0].keys:
        raise ValueError(f"{entry}: expected one nonempty literal MAPPING dictionary")
    return values[0]


def _rehome_path(value: str, source: Path, candidate: Path, entry: str) -> str:
    original = Path(value)
    if not original.is_absolute() or not original.is_relative_to(source) or not original.resolve().is_relative_to(source.resolve()):
        raise ValueError(f"{entry}: MAPPING path {value!r} is outside shared source root {source}")
    replacement = candidate / original.relative_to(source)
    if not replacement.resolve().is_relative_to(candidate.resolve()):
        raise ValueError(f"{entry}: MAPPING path {str(replacement)!r} does not resolve inside the candidate root {candidate}")
    return str(replacement)


def _mapping_values(node: ast.Dict, source: Path, candidate: Path, entry: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or not isinstance(value, ast.Constant) or not isinstance(value.value, str):
            raise ValueError(f"{entry}: MAPPING must contain only literal string keys and paths")
        if key.value in result:
            raise ValueError(f"{entry}: duplicate MAPPING key {key.value!r}")
        result[key.value] = _rehome_path(value.value, source, candidate, entry)
    return result


def rehome_finder(payload: str, source: Path, candidate: Path, entry: str) -> str:
    """Replace only the literal MAPPING expression; retain the rest of the finder."""
    try:
        tree = ast.parse(payload)
    except SyntaxError as exc:
        raise ValueError(f"{entry}: cannot parse finder MAPPING: {exc.msg}") from exc
    node = _mapping_node(tree, entry)
    mapping = _mapping_values(node, source, candidate, entry)
    # AST columns are UTF-8 byte offsets, not string character offsets.
    lines = payload.encode("utf-8").splitlines(keepends=True)
    if node.end_lineno is None or node.end_col_offset is None:
        raise ValueError(f"{entry}: MAPPING has no source span")
    start = sum(map(len, lines[: node.lineno - 1])) + node.col_offset
    end = sum(map(len, lines[: node.end_lineno - 1])) + node.end_col_offset
    encoded = payload.encode("utf-8")
    return (encoded[:start] + repr(mapping).encode("utf-8") + encoded[end:]).decode("utf-8")


def copy_finder(shared: Path, destination: Path, source: Path, candidate: Path) -> None:
    """Validate completely before replacing a destination, never write through a link."""
    try:
        payload = shared.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{shared.name}: cannot read editable finder: {exc}") from exc
    rewritten = rehome_finder(payload, source, candidate, shared.name)
    # A partial previous build may have symlinked this module into the shared venv.
    # Removing the local directory entry first also prevents hard-link write-through.
    destination.unlink(missing_ok=True)
    destination.write_text(rewritten, encoding="utf-8")
